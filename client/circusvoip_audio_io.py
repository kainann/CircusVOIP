
# -*- coding: utf-8 -*-
# =============================================
#  circusvoip_audio_io.py
# =============================================
# Gestion de l'audio pour les clients CircusVOIP.
#
# - Capture du micro -> envoi via callback (vers WebSocket audio server)
# - Reception de trames audio avec nom d'emetteur -> lecture locale
# - Volume par emetteur (calcule ailleurs selon la distance)
# - Mixage des flux entrants en temps reel
#
# Format audio :
#   sample rate : 48000 Hz
#   channels    : 1 (mono)
#   dtype       : float32
#   block size  : 960 samples (20 ms)
# =============================================

import threading
import queue
import shutil
import time
import wave
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
    _SD_AVAILABLE = True
    _SD_IMPORT_ERR = None
except Exception as e:
    # On capture TOUTES les exceptions pas juste ImportError
    # (peut etre ImportError, mais aussi OSError si DLL manquante, etc.)
    _SD_AVAILABLE = False
    _SD_IMPORT_ERR = str(e)

# Suppression de bruit (RNNoise via pyrnnoise). Optionnel : si pas
# installe (cas des clients pendant la phase dev), la feature sera
# desactivee et la checkbox sera grisee dans l'UI. Pour l'installer
# en dev :
#   pip install pyrnnoise
# La version finale 0.1.0 fournira pyrnnoise via l'installateur.
try:
    from pyrnnoise import RNNoise as _PyRNNoise
    NOISE_SUPPRESSION_AVAILABLE = True
    _NS_IMPORT_ERR = None
except Exception as e:
    _PyRNNoise = None
    NOISE_SUPPRESSION_AVAILABLE = False
    _NS_IMPORT_ERR = str(e)

# Log audio RX detaille pour diagnostic crackling (ajout 02/06/2026).
# Module autonome qui ecrit dans un CSV separe quand active via l'UI.
# Si le module est absent (cas tres improbable, fichier manquant), on
# laisse _audio_rx_logger = None et tous les appels seront no-op.
try:
    import circusvoip_audio_rx_logger as _audio_rx_logger
except Exception:
    _audio_rx_logger = None


def _ns_log(msg: str):
    """Helper : log dans le fichier debug du core si possible, sinon
    fallback sur print stdout. Les print() bruts depuis ce module ne
    sont pas captures par le fichier de log debug, donc on essaie
    d'utiliser _dbg_log du core."""
    try:
        from circusvoip_core import _dbg_log
        _dbg_log(msg)
    except Exception:
        try:
            print(msg, flush=True)
        except Exception:
            pass

# ---------------------------------------------
#  Parametres audio
# ---------------------------------------------

SAMPLE_RATE = 48000
CHANNELS    = 1
BLOCK_SIZE  = 960         # 20 ms a 48 kHz
DTYPE       = "float32"
FRAME_BYTES = BLOCK_SIZE * 4   # 960 samples * 4 bytes par float32 = 3840 bytes

# Pour chaque emetteur, on garde une file de buffers a jouer
# Si la file devient trop grande, on jette les vieux (anti-drift)
MAX_QUEUE_LEN = 10   # 200 ms de buffer max

# Seuil pour distinguer un VRAI underrun (queue vide alors qu'on vient de
# recevoir une trame = jitter/drift) d'un faux underrun (sender silencieux,
# noise gate ferme). 25/05/2026 Kainan. A 50 trames/s, une trame arrive
# toutes les 20 ms : on tolere 5 trames manquees = 100 ms. Au-dela on
# considere que le sender s'est tu, pas un bug reseau.
_UNDERRUN_GAP_S = 0.1

# Jitter buffer warmup (ajout 02/06/2026). Diagnostic CSV du 02/06 avec
# Skywat : 82.6% des callbacks output trouvent la queue Skywat a 0-1 trame,
# alors meme que 98% des trames arrivent dans la fenetre 10-25 ms (jitter
# reseau normal). Cause : on commence a pop la queue des la 1ere trame
# recue, sans laisser le temps a la queue d'absorber un peu de jitter ->
# 1439 underruns / 8514 callbacks (= 16.9%) en 170s d'appel telephone.
#
# Fix : par sender, on a un etat "waiting" tant que la queue n'a pas
# atteint ce nombre de trames. Pendant ce temps, le sender contribue
# silence au mix (pas d'underrun compte, pas de pop). Une fois ce seuil
# atteint, on passe en "playing" et on consomme normalement. Si la queue
# se vide (sender silencieux ou pertes), retour en "waiting" pour le
# prochain warmup.
#
# Valeur 5 = 100 ms de latence supplementaire (depuis 02/06/2026 v3,
# anciennement 3 = 60 ms). Le test avec Alex le 02/06 a montre que 3 trames
# etaient insuffisantes : selon la microdynamique du demarrage de talkspurt,
# la queue pouvait se stabiliser a 1 trame seulement, et un sender tres
# regulier (49.5-50.5 tr/s) suffisait a generer ~1000 underruns sur 73s
# d'audio actif. Passage a 5 : queue cible 4 trames en regime, marge
# confortable contre le drift et le jitter cumule.
JITTER_BUFFER_WARMUP_FRAMES = 5

# Hysteresis sur le retour en "waiting" (ajout 02/06/2026 v2).
# Sans hysteresis : queue=0 -> immediat re-warmup (60ms de silence).
# Probleme observe sur tests Firesstones : des mini-trous de 50-100ms
# dans son flux declenchaient un re-warmup -> 60ms de silence supp ->
# pop a la reprise. Resultat : 71 pops residuels sur 222s d'appel.
#
# Fix : on ne re-bascule en "waiting" que si la queue reste a 0 sur
# JITTER_BUFFER_ZERO_STREAK callbacks consecutifs (= tolere un trou
# de N*20ms = 100ms par defaut). En-deca, on accepte l'underrun
# ponctuel (silence d'une trame = 20ms) plutot que de re-warmup
# (silence de 3 trames = 60ms). Le silence pondere mieux : 20ms est
# inaudible la plupart du temps, 60ms est un click net.
JITTER_BUFFER_ZERO_STREAK = 5

# PLC (Packet Loss Concealment) — ajout 02/06/2026 v3.
# Sur underrun en mode "playing" (queue vide alors qu'on attendait une
# trame), on ne plus ecrire silence mais on rejoue la DERNIERE trame
# audio recue de ce sender, attenuee de PLC_GAIN. Effet : on couvre le
# trou de 20ms par "quelque chose de proche" plutot que par du silence,
# ce qui supprime le pop audible.
#
# Limitations volontaires :
#   - On rejoue UN AU PLUS la derniere trame (pas en cascade). Si plusieurs
#     callbacks consecutifs ont la queue vide, seul le PREMIER beneficie
#     du PLC ; les suivants ecrivent silence comme avant. Sinon on
#     boucle 50ms d'audio en boucle pendant un long trou = artefact.
#   - On utilise la TRAME BRUTE (avant traitement radio/effet phone)
#     parce que c'est ce qui est stocke. Pour le mix phone c'est OK,
#     pour le mix radio le filtre biquad n'a pas son etat coherent
#     mais c'est masque par l'attenuation -6dB.
#
# PLC_GAIN = 0.5 = -6 dB d'attenuation. Compromis classique en VOIP :
# assez audible pour masquer le pop, assez attenue pour ne pas creer
# une "doublure" perceptible si on repete la meme trame.
PLC_GAIN = 0.5


# ---------------------------------------------
#  Chargement de fichiers .wav (sonneries / notifs)
# ---------------------------------------------
#
# Les sonneries telephone (ring/dial) et la notification MP peuvent etre
# fournies sous forme de fichier .wav dans le dossier "sounds/" a cote du
# script (ou dans le dossier app/ embarque par l'installateur).
#
# Contraintes attendues sur les .wav :
#   - Sample rate : 48 kHz (= SAMPLE_RATE) pour ne pas avoir a resampler
#   - Mono (1 canal)
#   - 16-bit PCM (sw=2). Le 24-bit (sw=3) n'est pas decode trivialement par
#     stdlib wave : convertir en 16-bit avec Audacity/sox/ffmpeg avant
#     embarquage. Le 32-bit float (sw=4) est accepte aussi.
#
# Si le .wav est absent, illisible, ou dans un format non gere, on tombe
# sur le pattern synthetique de secours (parametre synth_fallback). C'est
# voulu : la fonctionnalite degrade en douceur, jamais de crash.
#
# Calibration du volume :
#   Le gain est applique au chargement pour que le RMS du .wav matche le
#   RMS du pattern synthetique de reference (= "meme volume percu" que le
#   son d'avant). Si le .wav est silencieux, le gain n'est pas applique
#   (eviterait une division par 0).
#
# Le fichier est lu UNE FOIS au demarrage (_AudioIO.__init__), pas a chaque
# play_phone_ring. Le buffer numpy est garde en RAM pour rejouer instantanement.

# Dossier ou chercher les sons. Calcule relativement a CE fichier .py.
# Si circusvoip_audio_io.py est dans app/, alors les sons sont dans
# app/sounds/<nom>.wav. C'est compatible avec le packaging par l'installateur
# Inno Setup ([Files] embarque app/* recursivement).
import os as _os_for_sounds
# Dossier .wav partage : sonneries telephone bundlees (ring/dial/notif) +
# bips PTT custom de l'utilisateur (ptt_press.wav / ptt_release.wav).
# Path object pour pouvoir faire .mkdir / .unlink directement cote bips
# custom ; reste compatible avec os.path.join/exists des sonneries.
_SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"

# Duree maximale autorisee pour un WAV custom de bip PTT (en secondes).
# Au-dela on refuse le fichier : un bip de plusieurs secondes serait
# penible et chargerait un buffer disproportionne dans la chaine audio.
_MAX_BEEP_DURATION_SEC = 5.0


def _load_wav_as_float32(path: str) -> "np.ndarray | None":
    """Charge un fichier .wav en float32 mono 48 kHz.

    Retourne None si :
      - le fichier n'existe pas
      - il est dans un format non gere (sample rate != 48000, stereo,
        24-bit PCM, etc.)
      - une erreur survient au decodage

    On utilise stdlib wave (pas de dep pip). 16-bit PCM (sw=2) et 32-bit
    float (sw=4) sont supportes. Pour du 24-bit, il faut convertir le
    fichier en amont (cf docstring du module).
    """
    import wave
    try:
        if not _os_for_sounds.path.exists(path):
            return None
        with wave.open(path, "rb") as w:
            sr      = w.getframerate()
            nch     = w.getnchannels()
            sw      = w.getsampwidth()
            nframes = w.getnframes()
            raw     = w.readframes(nframes)
        # Validation format
        if sr != SAMPLE_RATE:
            _ns_log(f"[SOUNDS] {path} sample_rate={sr} (attendu {SAMPLE_RATE}), ignore")
            return None
        if nch != 1:
            _ns_log(f"[SOUNDS] {path} channels={nch} (attendu 1=mono), ignore")
            return None
        # Decodage selon largeur d'echantillon
        if sw == 2:
            # 16-bit PCM signed little-endian -> float32 [-1, 1]
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sw == 4:
            # 32-bit IEEE float (probable) ou 32-bit PCM int. On suppose float :
            # c'est ce que produit soundfile.write(..., subtype="FLOAT") et la
            # plupart des outils. Si c'est du PCM int32 mal etiquete, le rendu
            # sera muet/distordu, on log un warning.
            arr = np.frombuffer(raw, dtype=np.float32).copy()
            if np.abs(arr).max() > 10.0:
                _ns_log(f"[SOUNDS] {path} sw=4 mais amplitudes hors [-1,1] "
                        f"(peak={np.abs(arr).max():.1f}), probablement PCM int32, ignore")
                return None
        else:
            _ns_log(f"[SOUNDS] {path} sampwidth={sw} non supporte (attendu 2 ou 4), ignore")
            return None
        return arr
    except Exception as e:
        _ns_log(f"[SOUNDS] Echec chargement {path} : {e}")
        return None


def _load_wav_calibrated_or_synth(
    filename: str,
    synth_fallback,
    log_label: str,
) -> "np.ndarray":
    """Tente de charger sounds/<filename>. En cas de succes, calibre son
    RMS pour matcher celui du pattern synthetique de reference, puis
    retourne le buffer. En cas d'echec, retourne le resultat de synth_fallback().

    L'idee : "meme volume percu" entre l'ancien son synth et le nouveau .wav.
    Le RMS sur les portions actives (= au-dessus d'un seuil de silence) est
    une mesure correcte du volume percu pour des signaux periodiques type
    sonnerie.

    Args:
        filename: nom du fichier dans sounds/ (ex. "dial.wav")
        synth_fallback: callable() -> np.ndarray, le generateur synthetique
        log_label: label court pour les logs (ex. "dial")
    """
    synth = synth_fallback()
    path = _os_for_sounds.path.join(_SOUNDS_DIR, filename)
    wav = _load_wav_as_float32(path)
    if wav is None:
        # Fallback silencieux : pas de .wav, on garde le synth.
        # On ne log que si le fichier existe (= echec de decodage), sinon
        # c'est juste qu'aucun .wav n'a ete fourni et c'est normal.
        if _os_for_sounds.path.exists(path):
            _ns_log(f"[SOUNDS] {log_label}: fallback synth (echec chargement {path})")
        return synth

    # Calcul RMS sur les portions actives (hors silence) pour calibrer
    def _rms_active(buf: "np.ndarray", thresh: float = 0.005) -> float:
        active = buf[np.abs(buf) > thresh]
        if len(active) == 0:
            return 0.0
        return float(np.sqrt(np.mean(active.astype(np.float64) ** 2)))

    rms_wav   = _rms_active(wav)
    rms_synth = _rms_active(synth)
    if rms_wav <= 0.0 or rms_synth <= 0.0:
        # Cas degeneré : wav muet ou synth muet. On garde le wav tel quel
        # avec un gain de securite faible pour eviter saturation.
        gain = 0.20
    else:
        gain = rms_synth / rms_wav
        # Limiter le gain pour eviter qu'un fichier extremement faible
        # ne sature en sortie. Plafond a 1.0 (pas d'amplification au-dela
        # du fichier d'origine) car les .wav usuels sont deja normalises.
        gain = min(gain, 1.0)

    wav_calibrated = (wav * gain).astype(np.float32)

    # Verifier le peak final, log informatif
    peak = float(np.abs(wav_calibrated).max()) if len(wav_calibrated) > 0 else 0.0
    _ns_log(f"[SOUNDS] {log_label}: charge {path} ({len(wav)} samples, "
            f"{len(wav)/SAMPLE_RATE:.2f}s, gain={gain:.3f}, peak={peak:.3f})")

    return wav_calibrated


# ---------------------------------------------
#  Bips PTT personnalisables (custom WAV utilisateur)
# ---------------------------------------------
# Cf load_custom_beep / clear_custom_beep dans AudioIO. Le loader est
# volontairement plus permissif que _load_wav_as_float32 ci-dessus :
#   - accepte 8/16/24/32 bits
#   - mono-downmix automatique si stereo
#   - resample vers 48 kHz si necessaire (np.interp lineaire, suffisant
#     pour des bips de <100 ms)
#   - rejet si duree > _MAX_BEEP_DURATION_SEC

def _load_wav_as_mono_48k(path: "str | Path") -> "np.ndarray | None":
    """Charge un WAV PCM, le convertit en mono float32 a 48 kHz.

    Pipeline :
      1) Lecture brute via stdlib `wave` (PCM 8/16/24/32 bits supporte)
      2) Conversion en float32 normalise dans [-1, 1]
      3) Mono-downmix par moyenne si stereo
      4) Resampling lineaire vers SAMPLE_RATE si la source est a un autre taux
      5) Rejet si duree > _MAX_BEEP_DURATION_SEC ou si lecture echoue

    Retourne le numpy array (float32) ou None en cas d'echec / fichier
    invalide. Pas de raise : on veut un code d'erreur exploitable par
    l'UI pour afficher un message clair plutot qu'une exception.
    """
    try:
        with wave.open(str(path), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth  = wf.getsampwidth()
            framerate  = wf.getframerate()
            n_frames   = wf.getnframes()
            if framerate <= 0 or n_frames <= 0:
                return None
            duration = n_frames / float(framerate)
            if duration > _MAX_BEEP_DURATION_SEC:
                return None
            raw = wf.readframes(n_frames)
    except (wave.Error, FileNotFoundError, OSError):
        return None

    # Conversion PCM brut -> float32 [-1, 1]
    if sampwidth == 1:
        # PCM unsigned 8 bits, biais 128
        arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        arr = (arr - 128.0) / 128.0
    elif sampwidth == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 3:
        # PCM 24 bits little-endian : pas de dtype natif numpy, on assemble
        # en int32 par tranches de 3 octets puis on extend le bit de signe.
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        a32 = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        # Extension de signe : si le bit 23 est a 1, on remplit les bits 24-31
        a32 = np.where(a32 & 0x800000, a32 | ~0xFFFFFF, a32)
        arr = a32.astype(np.float32) / 8388608.0
    elif sampwidth == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        return None

    # Mono-downmix : moyenne des canaux
    if n_channels > 1:
        arr = arr.reshape(-1, n_channels).mean(axis=1)

    # Resampling lineaire vers 48 kHz. La qualite est suffisante pour des
    # bips courts (<100ms typiquement) ; pas la peine d'embarquer scipy
    # juste pour ca.
    if framerate != SAMPLE_RATE:
        n_in  = arr.shape[0]
        n_out = int(round(n_in * SAMPLE_RATE / framerate))
        if n_out <= 0:
            return None
        # np.interp attend des x croissants : on construit x_in [0..n_in-1]
        # et x_out [0..n_in-1] resampled a n_out points.
        x_out = np.linspace(0.0, n_in - 1, num=n_out, dtype=np.float32)
        arr = np.interp(x_out, np.arange(n_in, dtype=np.float32),
                        arr.astype(np.float32)).astype(np.float32)
    else:
        arr = arr.astype(np.float32)

    # Clip defensif (eviter les debordements > 1.0 dus a une conversion
    # imprecise sur certains WAV exotiques).
    np.clip(arr, -1.0, 1.0, out=arr)
    return arr


# ---------------------------------------------
#  Filtre radio (effet talkie-walkie)
# ---------------------------------------------
# Etat persistant du filtre passe-bande par emetteur
# (les filtres IIR ont une memoire entre les trames)
_radio_filter_state: dict[str, dict] = {}


def _radio_filter_init():
    """Coefficients Butterworth 2e ordre passe-bande 300-3400 Hz a 48kHz.
    Calcules une fois pour tous (constants tant que SAMPLE_RATE est fixe)."""
    # Filtre IIR passe-bande 2 ordre : couple de filtres HP + LP
    # HP a 300 Hz (coupe les graves), LP a 3400 Hz (coupe les aigus)
    # Formule simple : biquad coefficients pour f_c, sample_rate
    import math

    def hp_coeffs(fc, sr):
        w0 = 2 * math.pi * fc / sr
        alpha = math.sin(w0) / (2 * 0.707)  # Q = 0.707 (Butterworth)
        cos_w0 = math.cos(w0)
        b0 = (1 + cos_w0) / 2
        b1 = -(1 + cos_w0)
        b2 = (1 + cos_w0) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_w0
        a2 = 1 - alpha
        return [b0/a0, b1/a0, b2/a0, a1/a0, a2/a0]

    def lp_coeffs(fc, sr):
        w0 = 2 * math.pi * fc / sr
        alpha = math.sin(w0) / (2 * 0.707)
        cos_w0 = math.cos(w0)
        b0 = (1 - cos_w0) / 2
        b1 = 1 - cos_w0
        b2 = (1 - cos_w0) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_w0
        a2 = 1 - alpha
        return [b0/a0, b1/a0, b2/a0, a1/a0, a2/a0]

    return {
        # Passe-bande d'ENTREE (avant saturation) : large pour preserver les
        # consonnes claires. Inspire de TeamSpeak Radio FX (~370-5000 Hz).
        "hp_in": hp_coeffs(350, SAMPLE_RATE),
        "lp_in": lp_coeffs(5000, SAMPLE_RATE),
        # Passe-bande de SORTIE (apres saturation et ring mod) : nettoie les
        # harmoniques aiguës generees par la saturation. Plus etroit que l'IN
        # pour donner le son "radio" final reconnaissable.
        "hp_out": hp_coeffs(320, SAMPLE_RATE),
        "lp_out": lp_coeffs(5500, SAMPLE_RATE),
    }


_RADIO_COEFFS = _radio_filter_init()

# Parametres radio ajustables a l'execution (utilises par apply_radio_effect).
# Modifiables via set_radio_params() pour pouvoir tuner l'effet via UI sans
# relancer l'app.
#
# PROFIL ACTUEL : Inspire de TeamSpeak Radio FX (Home channel)
#   Chaine de traitement (style plugin radio pro) :
#     1. Passe-bande IN (350-5000 Hz) : coupe extremes
#     2. Ring modulator (carrier ~2700 Hz, mix 17%) : grain metallique radio
#     3. Saturation (drive 2.0) : effet de compression/distortion
#     4. Passe-bande OUT (320-5500 Hz) : nettoie les harmoniques de la satur.
#     5. Bruit blanc (gate au silence) + reverb courte
#
#   C'est le ring modulator qui donne le son "radio HF/SSB" caracteristique
#   (voix legerement vocodee, metallique). Sans lui, on n'a qu'un passe-bande
#   + saturation = son banal.
_RADIO_PARAMS = {
    "noise_base":  0.008,     # shhh constant audible quand on parle
    "noise_max":   0.025,     # bruit fort quand on parle fort
    "drive":       2.0,       # saturation (un peu moins forte qu'avant car
                              # le ring mod ajoute deja du grain)
    "gain":        1.0,       # gain neutre
    "reverb_wet":  0.15,      # peu de reverb (CB = sec)
    # Ring modulator : multiplie le signal par sin(2pi * f_ring * t).
    # Cree des bandes laterales = effet metallique/vocode reconnaissable.
    # Mix sub-100% pour rester intelligible (sinon trop robot).
    "ring_freq":   2700.0,    # frequence porteuse ~2.7 kHz (style Home TS)
    "ring_mix":    0.17,      # 17% modulé + 83% original (style Home TS)
}

def set_radio_params(**kwargs):
    """Modifie un ou plusieurs parametres radio a l'execution.
    Exemple : set_radio_params(noise_base=0.01, noise_max=0.03)
    Les parametres non fournis sont laisses inchanges."""
    for k, v in kwargs.items():
        if k in _RADIO_PARAMS:
            _RADIO_PARAMS[k] = float(v)

def get_radio_params() -> dict:
    """Retourne une copie des parametres radio actuels."""
    return dict(_RADIO_PARAMS)


def _biquad_apply(frame, coeffs, state):
    """Applique un filtre biquad IIR a une trame, en maintenant l'etat entre appels."""
    b0, b1, b2, a1, a2 = coeffs
    x1, x2, y1, y2 = state["x1"], state["x2"], state["y1"], state["y2"]
    out = np.empty_like(frame)
    for i in range(len(frame)):
        x0 = frame[i]
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[i] = y0
        x2 = x1; x1 = x0
        y2 = y1; y1 = y0
    state["x1"], state["x2"], state["y1"], state["y2"] = x1, x2, y1, y2
    return out


def apply_radio_effect(frame: np.ndarray, sender_name: str,
                        with_squelch: bool = False) -> np.ndarray:
    """
    Applique l'effet radio style SF / futuriste (Star Citizen, Mass Effect, etc.).
    Son propre, haut de gamme, sensation "passage dans un haut-parleur".

    Chaine :
    - Passe-bande 200-5000 Hz (preserve les consonnes claires)
    - Legere saturation (drive 1.3)
    - Tres peu de bruit (module par voix, 0.003-0.008 RMS)
    - Reverb courte 15ms (sensation haut-parleur metallique)
    Conserve l'etat IIR, enveloppe, reverb, et detection d'etat entre trames.

    with_squelch : si True, genere un "tchk" en debut de transmission.
    Defaut False : juge trop genant a l'usage (un pop par debut de phrase
    par sender, multiplie par le nombre de joueurs en PTT simultane).
    Garde l'option pour le reactiver si besoin sans modifier le code.
    """
    if sender_name not in _radio_filter_state:
        _radio_filter_state[sender_name] = {
            # Filtres IIR : 2 passe-bandes (IN avant traitement, OUT apres)
            "hp_in":    {"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0},
            "lp_in":    {"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0},
            "hp_out":   {"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0},
            "lp_out":   {"x1": 0.0, "x2": 0.0, "y1": 0.0, "y2": 0.0},
            "env":      0.0,    # enveloppe RMS
            # Buffer de reverb : delay circulaire de ~30ms (1440 samples a 48kHz)
            "reverb_buf": np.zeros(1440, dtype=np.float32),
            "reverb_idx": 0,
            # Etat "parle / silencieux" pour declencher le squelch pop
            "speaking":  False,
            "silence_trames": 999,  # beaucoup de trames silencieuses au debut = squelch a la 1ere parole
            # Phase du ring modulator (sin(2pi*f*t)). On maintient l'index de
            # sample entre frames pour que la sinusoide soit continue : sinon
            # on entend des clics aux limites de trames a cause de la
            # discontinuite de phase.
            "ring_phase": 0,    # index de sample (sera modulo SAMPLE_RATE pour eviter overflow)
        }
    st = _radio_filter_state[sender_name]

    n = len(frame)

    # 1. Enveloppe d'amplitude (pour bruit module + detection speaking)
    rms_in = float(np.sqrt((frame * frame).mean())) if n > 0 else 0.0
    env_prev = st["env"]
    if rms_in > env_prev:
        env_prev = 0.7 * env_prev + 0.3 * rms_in
    else:
        env_prev = 0.95 * env_prev + 0.05 * rms_in
    st["env"] = env_prev

    # 2. Detection de l'etat "speaking" pour squelch pop
    # Seuil 0.015 : au-dessus = on parle, en-dessous = silence
    threshold_speak = 0.015
    was_speaking = st["speaking"]
    is_speaking_now = rms_in > threshold_speak

    squelch_click = False
    if with_squelch and is_speaking_now and not was_speaking and st["silence_trames"] >= 5:
        # Transition silence -> parole apres au moins 5 trames de silence (~100ms)
        # -> declencher un squelch pop en debut
        squelch_click = True
    st["speaking"] = is_speaking_now
    if is_speaking_now:
        st["silence_trames"] = 0
    else:
        st["silence_trames"] += 1

    # 3. Pas de bruit ajoute (style TeamSpeak Radio FX).
    # Le cote "radio" vient uniquement de :
    #   - Ring modulator (grain metallique)
    #   - Double passe-bande (coupe les extremes)
    #   - Saturation (drive)
    # Cf retour utilisateur : le bruit "shhh" cree des artefacts de pop on/off
    # ou un fade desagreable. Sans bruit, le son est plus propre. Les params
    # noise_base / noise_max sont conserves dans _RADIO_PARAMS pour
    # reactivation eventuelle.

    # 4. Appliquer la chaine de traitement sur la voix directement.
    # 4a. Passe-bande IN : 350-5000 Hz (large, garde les consonnes)
    out = _biquad_apply(frame, _RADIO_COEFFS["hp_in"], st["hp_in"])
    out = _biquad_apply(out, _RADIO_COEFFS["lp_in"], st["lp_in"])

    # 4b. Ring modulator : multiplie le signal par une sinusoide a f_carrier
    # Cree des bandes laterales (somme et difference de frequences) qui
    # donnent l'effet metallique caracteristique des radios HF/SSB.
    # On melange une fraction (ring_mix) du signal modulé avec l'original
    # pour rester intelligible.
    #
    # Continuite de phase : on maintient l'index de sample entre trames pour
    # que la sinusoide soit continue. Sinon discontinuite = clic audible
    # toutes les 20ms (taille de frame 960 samples).
    ring_freq = float(_RADIO_PARAMS["ring_freq"])
    ring_mix  = float(_RADIO_PARAMS["ring_mix"])
    if ring_mix > 0.001 and ring_freq > 0:
        phase0 = st["ring_phase"]
        # Generer la sinusoide pour cette trame (vectorise numpy)
        t_indices = np.arange(phase0, phase0 + n, dtype=np.float64)
        carrier = np.sin(2.0 * np.pi * ring_freq * t_indices / SAMPLE_RATE).astype(np.float32)
        modulated = out * carrier
        out = (1.0 - ring_mix) * out + ring_mix * modulated
        # Avancer la phase, modulo une periode pour eviter l'overflow float
        # (apres ~16M samples, t_indices ne perd plus de precision pour sin())
        new_phase = (phase0 + n) % SAMPLE_RATE
        st["ring_phase"] = new_phase

    # 5. Gain compensateur
    out = out * _RADIO_PARAMS["gain"]

    # 6. Saturation soft-clip (drive lu depuis _RADIO_PARAMS)
    # Note : la saturation genere des harmoniques aigues qu'on nettoie
    # ensuite avec le passe-bande OUT.
    out = np.tanh(out * _RADIO_PARAMS["drive"]) * 0.85

    # 6b. Passe-bande OUT : 320-5500 Hz, nettoie les harmoniques de la sat.
    # Inspire de TeamSpeak Radio FX : un 2e passe-bande apres distortion
    # donne un son final propre et reconnaissable comme "radio".
    out = _biquad_apply(out, _RADIO_COEFFS["hp_out"], st["hp_out"])
    out = _biquad_apply(out, _RADIO_COEFFS["lp_out"], st["lp_out"])

    # 7. Squelch pop en debut de transmission
    # Petit "tchk" : ~20ms d'un clic avec attack/decay
    if squelch_click:
        # Clic simple : bruit filtre de courte duree en debut de trame
        click_len = min(50, n)  # ~1ms
        # Envelope decroissante exponentielle
        decay = np.exp(-np.arange(click_len) / 8.0).astype(np.float32)
        # Amplitude 0.08 : pop present mais pas agressif
        # (0.15 etait trop fort, 0.05 trop discret, compromis intermediaire)
        click = np.random.normal(0, 0.08, size=click_len).astype(np.float32) * decay
        out[:click_len] = out[:click_len] + click

    # 8. Reverb courte 15ms : on mixe avec une copie retardee et attenuee
    # Delay 720 samples (15ms a 48kHz), wet lu depuis _RADIO_PARAMS
    reverb_delay_samples = 720
    wet = _RADIO_PARAMS["reverb_wet"]
    buf = st["reverb_buf"]
    buf_idx = st["reverb_idx"]
    buf_len = len(buf)

    reverb_out = np.empty(n, dtype=np.float32)
    for i in range(n):
        # Lire le sample retarde
        read_idx = (buf_idx - reverb_delay_samples) % buf_len
        delayed = buf[read_idx]
        reverb_out[i] = out[i] + wet * delayed
        # Stocker le sample actuel (avec leger feedback pour etaler la reverb)
        buf[buf_idx] = out[i] + 0.3 * delayed
        buf_idx = (buf_idx + 1) % buf_len
    st["reverb_idx"] = buf_idx
    out = reverb_out

    out = np.clip(out, -1.0, 1.0)

    return out.astype(np.float32)


def reset_radio_filter(sender_name: str):
    """Reset l'etat du filtre pour un emetteur (a appeler si deconnexion)."""
    _radio_filter_state.pop(sender_name, None)


# ---------------------------------------------
#  Detection container "grotte" SC
# ---------------------------------------------
# Utilitaire expose au module pour que le client (et les outils externes)
# testent si un container_id correspond a une grotte. L'ACTIVATION de
# l'effet echo se fait au niveau du mix de proximite via
# AudioIO.set_cave_echo(True/False) ; cette fonction est juste le test de
# pattern. L'echo est uniforme global (pas par-sender) car c'est
# l'ambiance de la grotte ou JE me trouve qui cree l'echo, pas celle de
# chaque locuteur distant.

import re as _re

# Regex de detection "grotte". Tolere les separateurs mixtes (espaces,
# underscores, tirets) car le fallback "name:..." cote client preserve
# souvent les espaces du HUD SC sans les convertir.
# Le suffixe peut etre "int" ou "int-NNN" (sous-index d'une instance,
# ex: rock01_unoc_001_size04_002_int-001 est une sous-partie de la
# grotte instance 002).
# Tolere aussi les erreurs OCR typiques sur les chiffres du prefixe :
# "rocko1" au lieu de "rock01" (0 lu comme o), "sand0l" au lieu de "sand01"
# (1 lu comme l). La regex accepte o/0 et l/1 de maniere interchangeable
# aux positions des chiffres du prefixe.
_CAVE_CONTAINER_RE = _re.compile(
    r"^(?:name:)?\s*(?:rock|sand)[o0]*[l1]+[\s_-]+(?:occu|unoc)[\s_-]+.*[\s_-]+int(?:-\d+)?$"
)

def is_cave_container(container_id: str | None) -> bool:
    """True si le container correspond a une grotte SC (rock01_*, sand01_*).
    Accepte None et retourne False. Tolere les separateurs mixtes
    (underscores, espaces, tirets) pour gerer a la fois les containers
    normalises et les fallback OCR bruts avec espaces."""
    if not container_id:
        return False
    return bool(_CAVE_CONTAINER_RE.match(container_id.strip().lower()))


# ---------------------------------------------
#  Liste des peripheriques
# ---------------------------------------------

# Derniere erreur rencontree lors d'un query (pour diagnostic UI)
_last_query_error: str | None = None

def get_last_query_error() -> str | None:
    """Retourne la derniere erreur rencontree (ou None)."""
    if not _SD_AVAILABLE:
        return f"sounddevice indisponible: {_SD_IMPORT_ERR}"
    return _last_query_error


def _clean_device_name(name: str) -> str:
    """Retire les parties parasites des noms de devices."""
    # Exemples :
    #   "Microphone (Realtek(R) Audio)"  -> "Microphone (Realtek(R) Audio)"
    #   "Casque (2- WH-1000XM4 Hands-Free)" -> enleve les prefixes "2-", "3-", etc
    import re
    name = re.sub(r"^\d+-\s*", "", name)
    return name.strip()


def _filter_devices(devices, is_input: bool) -> list[tuple[int, str]]:
    """
    Filtre la liste brute des devices pour ne garder que les plus pertinents.
    - Priorite : WASAPI > MME > DirectSound > autres
    - Deduplication par nom (garde la meilleure hostapi)
    - Ignore les devices virtuels evidents
    """
    # Hostapis avec priorite (plus bas = mieux)
    hostapi_priority = {
        # Windows
        "Windows WASAPI": 0,
        "MME":            1,
        "Windows DirectSound": 2,
        "Windows WDM-KS": 3,
        "ASIO":           4,
        # Linux
        "PulseAudio":     0,   # wrapper universel, fonctionne via PipeWire aussi
        "JACK Audio Connection Kit": 1,
        "ALSA":           2,   # ALSA direct = noms bruts hw:X,Y (moins user-friendly)
        "OSS":            3,
        # macOS
        "Core Audio":     0,
    }

    # Mots-cles de devices a ignorer (virtuels, obsoletes, doublons systeme)
    blacklist_keywords = [
        # Windows
        "primary sound driver",   # WDM generique
        "primary sound capture",
        "microsoft sound mapper",
        "output sound mapper",
        # Linux ALSA : devices bas-niveau tres techniques
        "sysdefault",             # = default, doublon
        "front:",                 # front channel only
        "surround",               # channel split
        "samplerate",             # plugin
        "speexrate",              # plugin
        "upmix",                  # plugin
        "vdownmix",               # plugin
        "dmix",                   # plugin
        "dsnoop",                 # plugin
        "hdmi:",                  # sauf si c'est le seul choix
        "spdif:",
        "iec958",                 # digital passthrough
        "hw:",                    # hardware direct (trop technique)
        "plughw:",                # variante plughw
    ]

    # Collecter tous les candidats avec leur priorite
    candidates = []  # [(cleaned_name, priority, device_id, full_label)]
    for i, d in enumerate(devices):
        if is_input:
            if d.get("max_input_channels", 0) <= 0:
                continue
        else:
            if d.get("max_output_channels", 0) <= 0:
                continue
        name = d.get("name", "")
        if not name:
            continue
        name_lower = name.lower()
        if any(kw in name_lower for kw in blacklist_keywords):
            continue
        try:
            ha = sd.query_hostapis(d["hostapi"])["name"]
        except Exception:
            ha = "?"
        prio = hostapi_priority.get(ha, 99)
        cleaned = _clean_device_name(name).lower()
        label = f"{_clean_device_name(name)} [{ha}]"
        candidates.append((cleaned, prio, i, label))

    # Deduplication par cleaned name : on garde la meilleure priorite
    best_per_name = {}
    for cleaned, prio, idx, label in candidates:
        current = best_per_name.get(cleaned)
        if current is None or prio < current[0]:
            best_per_name[cleaned] = (prio, idx, label)

    # Tri par nom pour un affichage stable
    result = [(idx, label) for _, idx, label in sorted(
        best_per_name.values(), key=lambda x: x[2].lower()
    )]
    return result


def list_input_devices() -> list[tuple[int, str]]:
    """Retourne [(device_id, label), ...] pour les micros (filtre)."""
    global _last_query_error
    if not _SD_AVAILABLE:
        _last_query_error = f"sounddevice non charge ({_SD_IMPORT_ERR})"
        return []
    try:
        devices = sd.query_devices()
        if devices is None:
            _last_query_error = "query_devices() a retourne None"
            return []
        result = _filter_devices(devices, is_input=True)
        _last_query_error = None
        return result
    except Exception as e:
        _last_query_error = f"{type(e).__name__}: {e}"
        print(f"[AUDIO] list_input_devices erreur : {_last_query_error}")
        return []


def list_output_devices() -> list[tuple[int, str]]:
    """Retourne [(device_id, label), ...] pour les sorties (filtre)."""
    global _last_query_error
    if not _SD_AVAILABLE:
        _last_query_error = f"sounddevice non charge ({_SD_IMPORT_ERR})"
        return []
    try:
        devices = sd.query_devices()
        if devices is None:
            _last_query_error = "query_devices() a retourne None"
            return []
        result = _filter_devices(devices, is_input=False)
        _last_query_error = None
        return result
    except Exception as e:
        _last_query_error = f"{type(e).__name__}: {e}"
        print(f"[AUDIO] list_output_devices erreur : {_last_query_error}")
        return []

def default_input_device() -> int | None:
    try:
        return sd.default.device[0]
    except Exception:
        return None

def default_output_device() -> int | None:
    try:
        return sd.default.device[1]
    except Exception:
        return None

# ---------------------------------------------
#  Suppresseur de bruit (RNNoise)
# ---------------------------------------------

class _NoiseSuppressor:
    """Wrapper RNNoise pour denoiser des frames de 960 samples (20 ms a
    48 kHz) en streaming.

    RNNoise fonctionne nativement sur des frames de 480 samples (10 ms
    a 48 kHz). Comme notre pipeline utilise des blocs de 960 samples
    (20 ms), on decoupe chaque frame en 2 sous-frames de 480, on les
    passe une par une au denoiser (qui maintient son etat interne entre
    appels), puis on recolle.

    Si pyrnnoise n'est pas installe (cas des clients dev), instance =
    no-op : process(frame) renvoie le frame tel quel. Ainsi le code
    appelant n'a pas besoin de tester la dispo, juste d'instancier.
    """

    SUB_FRAME = 480  # taille native RNNoise (10 ms a 48 kHz)

    def __init__(self):
        self.enabled = False
        self._available = NOISE_SUPPRESSION_AVAILABLE
        self._denoiser = None
        self._init_error = None
        # Compteurs de debug pour diagnostiquer si RNNoise tourne reellement
        # ou pas. Logs au passage du seuil, puis tous les ~250 frames (5s).
        self._frames_processed = 0
        self._frames_errors = 0
        self._first_error_msg = None
        self._first_success_logged = False
        if self._available:
            try:
                # pyrnnoise.RNNoise expose denoise_frame(frame, partial)
                # qui prend un array 2D (channels, samples) en int16 (480
                # samples a 48 kHz par sous-frame) et retourne
                # (speech_prob, denoised_int16).
                #
                # IMPORTANT : pyrnnoise initialise self.channels et
                # self.denoise_states paresseusement lors du 1er appel a
                # denoise_chunk() (qui lit la shape du chunk). Si on
                # bypass et qu'on appelle directement denoise_frame(),
                # self.channels reste None et ca crash dans
                # `range(self.channels)`. On force donc l'init manuelle :
                #   - channels = 1 (mono)
                #   - dtype = int16 (ce qu'on lui passera)
                #   - denoise_states = liste de 1 etat C cree par create()
                from pyrnnoise.rnnoise import create as _rnnoise_create
                self._denoiser = _PyRNNoise(sample_rate=SAMPLE_RATE)
                self._denoiser.channels = 1
                self._denoiser.dtype = np.int16
                self._denoiser.denoise_states = [_rnnoise_create()]
                _ns_log(f"[NS] RNNoise init OK (sample_rate={SAMPLE_RATE})")
            except Exception as e:
                # Si l'init crash (DLL incompatible, etc.), on bascule
                # en no-op silencieusement.
                self._available = False
                self._init_error = str(e)
                self._denoiser = None
                _ns_log(f"[NS] RNNoise init FAIL : {e}")

    @property
    def available(self) -> bool:
        """True si pyrnnoise est installe ET que l'init a reussi."""
        return self._available

    def set_enabled(self, enabled: bool):
        """Active ou desactive la suppression de bruit a chaud.
        Si non disponible, le flag reste False quoi qu'il arrive."""
        if not self._available:
            self.enabled = False
            return
        self.enabled = bool(enabled)

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Denoise un frame de 960 samples float32 [-1.0, 1.0].
        Si non dispo ou desactive : renvoie le frame tel quel.
        Sinon : decoupe 2x480, denoise, recolle, renvoie un nouveau
        np.ndarray float32 (memes dimensions)."""
        if not self._available or not self.enabled or self._denoiser is None:
            return frame
        if len(frame) != BLOCK_SIZE:
            # Frame de taille inattendue : skip pour eviter de casser
            # le pipeline. Ne devrait jamais arriver.
            return frame
        try:
            # Conversion float32 [-1, 1] -> int16
            i16 = np.clip(frame * 32767.0, -32768.0, 32767.0).astype(np.int16)
            # Decoupe en 2 sous-frames de 480 samples
            out_parts = []
            for i in range(0, BLOCK_SIZE, self.SUB_FRAME):
                sub = i16[i:i + self.SUB_FRAME]
                # pyrnnoise attend un array 2D (channels, samples) pour
                # le mode multi-canal. En mono on passe (1, 480).
                sub_2d = sub.reshape(1, self.SUB_FRAME)
                _speech_prob, denoised = self._denoiser.denoise_frame(
                    sub_2d, partial=False
                )
                # denoised shape = (1, 480) int16. On reaplatit.
                out_parts.append(np.asarray(denoised, dtype=np.int16).flatten())
            i16_out = np.concatenate(out_parts)
            # Reconversion int16 -> float32 [-1, 1]
            result = (i16_out.astype(np.float32) / 32767.0)
            self._frames_processed += 1
            # Log explicite au tout premier succes pour confirmer que
            # le pipeline tourne reellement.
            if not self._first_success_logged:
                self._first_success_logged = True
                _ns_log(
                    f"[NS] Premier frame denoise OK "
                    f"(in_len={len(frame)}, out_len={len(result)})"
                )
            return result
        except Exception as e:
            # En cas d'erreur runtime, on renvoie le frame brut plutot
            # que de couper l'audio. La feature est best-effort. On log
            # la PREMIERE erreur pour diagnostiquer (ensuite on compte
            # juste pour eviter de spammer la console).
            self._frames_errors += 1
            if self._first_error_msg is None:
                self._first_error_msg = str(e)
                _ns_log(f"[NS] Premiere erreur denoise : {e}")
                import traceback
                _ns_log(traceback.format_exc())
            return frame

    def get_stats(self) -> dict:
        """Renvoie compteurs internes (utile pour debug UI)."""
        return {
            "enabled": self.enabled,
            "available": self._available,
            "frames_processed": self._frames_processed,
            "frames_errors": self._frames_errors,
            "first_error": self._first_error_msg,
        }


# ---------------------------------------------
#  AudioIO
# ---------------------------------------------

class AudioIO:
    """
    Gere la capture et la lecture audio pour CircusVOIP.

    Usage :
        audio = AudioIO()
        audio.set_on_capture(lambda frame: ws.send(frame.tobytes()))
        audio.start_capture(device_id)
        audio.start_playback(device_id)

        # A la reception d'une trame reseau :
        audio.feed_remote_frame(sender_name, frame_bytes)

        # Pour controler le volume :
        audio.set_user_volume("Alice", 0.5)
    """

    def __init__(self):
        # Capture
        self._input_stream = None
        self._on_capture   = None
        self._capture_muted = False
        # Dernier device_id utilise a start_capture() : memorise pour que
        # restart_streams() puisse relancer avec le bon device apres un
        # gel du process (watchdog OCR -> recovery audio).
        self._last_input_device  = None
        self._last_output_device = None

        # Parametres micro
        self._mic_gain         = 1.0     # multiplicateur 0-3
        self._gate_threshold   = 0.03    # RMS threshold 0-1 (ouverture)
        self._gate_close_thr   = 0.010   # RMS threshold de fermeture (hysteresis)
        self._gate_hold_ms     = 400     # ms avant fermeture apres silence (evite de couper les fins de phrase)
        self._gate_is_open     = False
        self._gate_last_open_t = 0.0
        self._gate_force_open  = False   # bypass gate (utilise par PTT radio)
        self._mic_rms_current  = 0.0     # pour VU-metre

        # Suppression de bruit (RNNoise). No-op si pyrnnoise non installe.
        # Toggle via set_noise_suppression(bool) ; visible via
        # noise_suppression_available pour l'UI (grise la checkbox si False).
        self._noise_suppressor = _NoiseSuppressor()

        # Bip PTT local (entendu uniquement par l'utilisateur dans son casque,
        # pas envoye aux autres). Pre-genere a l'init.
        # _beep_buffer : numpy array de samples (float32) en cours de lecture.
        # _beep_idx    : position actuelle dans le buffer.
        # Quand _beep_idx >= len(_beep_buffer), plus rien a jouer.
        self._beep_buffer: "np.ndarray | None" = None
        self._beep_idx                          = 0
        # Bip "press" (radio active) : 880 Hz aigu, 80ms.
        # Amplitude 0.0875 (= 0.125 - 30%) : retour utilisateur "trop fort"
        # le 06/05/2026 quand on PTT en parlant. Le bip release reste a
        # 0.125 car il est moins gênant (joue après le release, pas en plein
        # debut de phrase).
        self._beep_press   = self._generate_beep(freq_hz=880.0, duration_ms=80,
                                                  fade_ms=8, amplitude=0.0875)
        self._beep_release = self._generate_beep(freq_hz=440.0, duration_ms=60,
                                                  fade_ms=8, amplitude=0.125)
        # Bips PTT personnalisables : si l'utilisateur a choisi des WAV
        # custom via l'UI, ils sont stockes dans <client_dir>/sounds/
        # et auto-charges au boot. Si None, on retombe sur les bips
        # synthetiques ci-dessus. _beep_volume est un multiplicateur global
        # 0.0-1.0 applique a la lecture (sur synth comme sur custom).
        self._beep_press_custom:   "np.ndarray | None" = None
        self._beep_release_custom: "np.ndarray | None" = None
        self._beep_volume = 1.0
        # Auto-load des fichiers presents dans <client_dir>/sounds/ a l'init.
        # Echec silencieux : si un WAV est corrompu, on le laisse de cote et
        # on retombe sur le synth. L'UI peut detecter via has_custom_beep().
        for kind, fname in (("press", "ptt_press.wav"),
                            ("release", "ptt_release.wav")):
            p = _SOUNDS_DIR / fname
            if p.exists():
                arr = _load_wav_as_mono_48k(p)
                if arr is not None and arr.size > 0:
                    if kind == "press":
                        self._beep_press_custom = arr
                    else:
                        self._beep_release_custom = arr

        # Soundboard (v0.2 alpha 029). Joue un fichier WAV local mixe dans
        # le flux de sortie (comme le bip), avec son propre buffer et son
        # facteur de volume (slider "Son A" dans l'UI). Le pipeline :
        #   - play_soundboard(samples) charge un np.array float32 mono 48kHz.
        #   - _on_output_block mixe les samples au flux principal en
        #     multipliant par _soundboard_volume_factor.
        #   - Un seul son a la fois : nouvelle lecture remplace l'ancienne.
        # IMPORTANT : c'est entendu UNIQUEMENT par l'utilisateur local.
        # Pour que les AUTRES joueurs entendent le son, le client envoie
        # un message WebSocket soundboard_play au serveur, qui le broadcast
        # au canal vocal, et chaque recepteur appelle play_soundboard()
        # sur sa copie locale du fichier.
        self._soundboard_buffer: "np.ndarray | None" = None
        self._soundboard_idx = 0

        # Sonnerie telephone CircusPhone (Feature 4, D2). Contrairement au
        # bip PTT et au soundboard qui sont one-shot, la sonnerie BOUCLE :
        # quand _phone_ring_idx atteint la fin du buffer, il repart a 0.
        # La lecture ne s'arrete que sur appel explicite a stop_phone_ring().
        # Un seul buffer : un joueur est soit appelant (motif "dial"), soit
        # destinataire (motif "ring"), jamais les deux a la fois.
        #   _phone_ring_buffer : buffer du motif en cours (ou None si silence)
        #   _phone_ring_idx    : position de lecture dans le buffer (boucle)
        # Les motifs sont charges depuis sounds/ring.wav et sounds/dial.wav
        # si presents, sinon on tombe sur le pattern synthetique de secours
        # (cf docstrings de _synth_phone_*_pattern). Le chargement est fait
        # UNE FOIS ici, le buffer numpy reste en RAM pour relance instantanee.
        self._phone_ring_buffer: "np.ndarray | None" = None
        self._phone_ring_idx = 0
        # Motif "ring" (destinataire) : sounds/ring.wav ou double tonalite synth.
        self._phone_ring_pattern_ring = _load_wav_calibrated_or_synth(
            "ring.wav", self._synth_phone_ring_pattern, "ring",
        )
        # Motif "dial" (appelant) : sounds/dial.wav ou bip synth.
        self._phone_ring_pattern_dial = _load_wav_calibrated_or_synth(
            "dial.wav", self._synth_phone_dial_pattern, "dial",
        )

        # CircusPhone (D4 etape 3) : son de notification de message texte.
        # Contrairement a la sonnerie, c'est un ONE-SHOT (~1s) qui se joue
        # une fois et s'arrete (comme le bip PTT et le soundboard). Volume
        # pilote par le meme slider que la sonnerie (_phone_ring_volume_factor :
        # la spec dit "slider sonnerie tel = sonnerie + bip d'appel + notif MP").
        self._phone_notif_buffer: "np.ndarray | None" = None
        self._phone_notif_idx = 0
        # Motif notif : sounds/notif.wav ou ding-dong synth.
        self._phone_notif_pattern = _load_wav_calibrated_or_synth(
            "notif.wav", self._synth_phone_notif_pattern, "notif",
        )

        # CircusPhone (D4 etape 2) : flag "mute micro" depuis l'overlay
        # phone (ecran 'En appel'). Coupe la capture cote envoi sans
        # toucher au gate ni au PTT. False par defaut.
        self._capture_muted = False

        # Facteurs de volume controles par les sliders de l'UI
        # (v0.2 alpha 002 / consolides en alpha 029). 1.0 = 100%, 2.0 = 200%.
        # Bornes appliquees a 0.0..2.0 dans les setters.
        #   _beep_volume_factor       : slider "Bip radio" (actif sur bip PTT)
        #   _soundboard_volume_factor : slider "Son A" (actif sur soundboard)
        #   _phone_ring_volume_factor : slider "Son B" (futur, pour CircusPhone)
        self._beep_volume_factor       = 1.0
        self._soundboard_volume_factor = 1.0
        self._phone_ring_volume_factor = 1.0

        # Lecture
        self._output_stream = None
        self._remote_buffers: dict[str, queue.Queue] = {}
        self._remote_volumes: dict[str, float]       = {}
        # Multiplicateur par utilisateur (slider manuel dans l'UI)
        # 1.0 = normal, 0.0 = mute, 2.0 = x2
        self._remote_multipliers: dict[str, float]   = {}
        # Flag is_radio de la derniere trame recue par sender.
        # Utilise au mix : on applique l'echo grotte uniquement sur les
        # voix de proximite (pas celles recues en radio PTT, qui ont deja
        # leur propre effet talkie-walkie).
        self._remote_is_radio: dict[str, bool]       = {}
        # CircusPhone (D3) : flag "voix telephone" de la derniere trame
        # recue par sender. Quand True, la voix est routee vers mix_phone
        # dans _on_output_block : pas de filtre radio, pas d'echo grotte,
        # pas d'attenuation de distance (au telephone la distance physique
        # entre les 2 interlocuteurs n'existe pas).
        self._remote_is_phone: dict[str, bool]       = {}
        # Flag Mode RP : si True pour un sender, sa voix de proximite sera
        # traitee comme une radio (filtre applique localement en plus de son
        # routage normal). Ce flag est calcule par le CLIENT selon la regle :
        #   Mode RP local ACTIVE + (mon casque ON OU casque sender ON) -> True
        # Si le client a Mode RP OFF, ce flag est toujours False (comportement
        # inchange).
        self._remote_force_radio: dict[str, bool]    = {}
        # Etat de la reverb grotte (active quand le joueur local est dans une
        # zone grotte : container commencant par "rock01_" ou "sand01_").
        # Active/desactive par le client via set_cave_echo(True/False) en
        # fonction du container courant detecte par l'OCR.
        self._cave_echo_active = False
        # Reverb Schroeder compacte : 4 comb filters en parallele + 2 all-pass
        # en serie. Plus naturel que le simple delay precedent (qui donnait
        # des rebonds tres audibles, desagreables avec plusieurs locuteurs).
        # Les delays sont choisis premiers entre eux pour eviter les motifs
        # repetitifs (sinon on retombe sur un "comb filter" metallique).
        # Delays combs (en samples a 48kHz) : ~30ms, 37ms, 41ms, 47ms
        #   -> couleur "caverne naturelle", diffuse rapidement
        # Delays all-pass : 5ms, 1.7ms pour diffuser davantage sans ajouter
        # de queue (les all-pass ne changent pas l'enveloppe, seulement la phase)
        self._cave_comb_delays = [1433, 1777, 1949, 2251]   # premiers entre eux
        self._cave_comb_fbs    = [0.72, 0.70, 0.68, 0.66]   # feedback par comb
        # Low-pass dans la boucle comb : simule l'absorption des aigus par
        # les parois (coefficient 0 a 1, plus proche de 1 = plus de coupe)
        self._cave_comb_damp   = 0.35
        # Buffers des 4 combs + leur etat lowpass
        self._cave_comb_bufs   = [np.zeros(d, dtype=np.float32) for d in self._cave_comb_delays]
        self._cave_comb_idxs   = [0, 0, 0, 0]
        self._cave_comb_lp     = [0.0, 0.0, 0.0, 0.0]
        # All-pass filters : delay + coefficient diffusion
        self._cave_ap_delays   = [241, 83]   # ~5ms, 1.7ms
        self._cave_ap_coef     = 0.5
        self._cave_ap_bufs     = [np.zeros(d, dtype=np.float32) for d in self._cave_ap_delays]
        self._cave_ap_idxs     = [0, 0]
        self._lock = threading.Lock()

        # Stats
        self._frames_sent     = 0
        self._frames_received = 0

        # === Stats debug crackling (ajout 25/05/2026) ===
        # Compteurs pour tracker les pertes silencieuses de frames audio.
        # Toutes valeurs cumulatives depuis le demarrage du process. Les
        # logs [AUDIO STATS] periodiques (cf core) consultent ces compteurs
        # et calculent les deltas sur 30s.
        #
        # Cote reception (feed_remote_frame) :
        #   _frames_dropped_by_sender : dict {sender: count} - frames jetees
        #     car la queue _remote_buffers[sender] etait pleine (anti-drift).
        #   _first_drop_logged_by_sender : dict {sender: ts} - timestamp du
        #     dernier log "premier drop" pour throttle (re-logger toutes les 30s).
        #
        # Cote playback (_on_output_block) :
        #   _output_underruns_by_sender : dict {sender: count} - queue vide
        #     quand sounddevice a besoin de samples (queue.Empty).
        #   _output_truncations_by_sender : dict {sender: count} - frame plus
        #     grande que block_size (rare mais possible si block_size sounddevice
        #     est inferieur a BLOCK_SIZE source = 960).
        #   _output_silence_implicite_by_sender : dict {sender: count} - frame
        #     plus petite que block_size (= silence ajoute implicitement, click possible).
        self._frames_dropped_by_sender: dict[str, int] = {}
        self._first_drop_logged_by_sender: dict[str, float] = {}
        self._output_underruns_by_sender: dict[str, int] = {}
        self._output_truncations_by_sender: dict[str, int] = {}
        self._output_silence_implicite_by_sender: dict[str, int] = {}
        # Jitter buffer warmup (ajout 02/06/2026, cf JITTER_BUFFER_WARMUP_FRAMES).
        #   _jb_state            : "waiting" (warmup en cours) | "playing"
        #   _jb_warmup_count     : nb fois ou on est passe en waiting (cumul)
        #   _jb_silent_blocks    : nb de blocks audio ecrits silence pendant
        #                          warmup (cumul). Stat utile car ces silences
        #                          NE SONT PAS comptes comme underruns (c'est
        #                          un comportement volontaire, pas un bug).
        #   _jb_zero_streak      : nb de callbacks consecutifs ou la queue
        #                          etait a 0 alors qu'on etait en playing
        #                          (cf hysteresis JITTER_BUFFER_ZERO_STREAK).
        #                          Reset quand une trame est pop avec succes.
        self._jb_state: dict[str, str] = {}
        self._jb_warmup_count: dict[str, int] = {}
        self._jb_silent_blocks: dict[str, int] = {}
        self._jb_zero_streak: dict[str, int] = {}

        # PLC (ajout 02/06/2026 v3) :
        #   _plc_last_frame  : derniere trame brute recue par sender (np.ndarray)
        #                      utilisee pour rejouer sur underrun. Mise a jour
        #                      a chaque pop dans _on_output_block.
        #   _plc_used        : True si la derniere trame a deja servi pour
        #                      un PLC -> on n'en fait pas un 2eme (sinon
        #                      bouclage audible). Reset a chaque nouveau pop.
        #   _plc_applied_total : compteur cumulatif pour stats CSV.
        self._plc_last_frame: dict[str, "np.ndarray | None"] = {}
        self._plc_used: dict[str, bool] = {}
        self._plc_applied_total: dict[str, int] = {}
        # Fix underrun 25/05/2026 Kainan : sans ce timestamp, le compteur
        # underrun comptait aussi les periodes ou le sender ne parlait pas
        # (queue vide naturellement car noise gate ferme cote emetteur ->
        # aucune trame envoyee). Resultat : 1500 underruns/30s pour Skywat
        # silencieux pendant 30s = faux positif. Maintenant on ne compte
        # un underrun QUE si on a recu au moins une trame de ce sender
        # depuis moins de _UNDERRUN_GAP_MS (100ms par defaut). Au-dela
        # = sender silencieux = comportement normal, pas un bug reseau.
        self._last_remote_frame_ts: dict[str, float] = {}

        # Log audio RX detaille (ajout 02/06/2026, optionnel via UI).
        # _last_callback_ts : timestamp du dernier appel _on_output_block,
        #   utilise pour mesurer callback_period_ms (jitter sounddevice).
        #   Initialise a 0.0, mis a jour a chaque callback.
        self._last_callback_ts: float = 0.0

    def set_audio_rx_log_enabled(self, enabled: bool, pseudo: str = "",
                                 debug_dir=None) -> bool:
        """Active ou desactive le log audio RX detaille (CSV separe pour
        diagnostic crackling). Appele depuis l'UI quand l'utilisateur
        coche/decoche "Activer le log audio detaille" dans les Parametres.

        enabled   : True pour activer, False pour desactiver.
        pseudo    : nom du joueur (utilise dans le nom du fichier CSV).
                    Ignore si enabled=False.
        debug_dir : pathlib.Path vers le dossier 'circusvoip_debug' du
                    client. Le sous-dossier 'audio_rx/' sera cree dedans.
                    Ignore si enabled=False.

        Retourne True si l'operation a abouti, False sinon (module logger
        non importable, deja dans l'etat demande, ou echec ouverture
        fichier).
        """
        if _audio_rx_logger is None:
            return False
        try:
            if enabled:
                if _audio_rx_logger.is_enabled():
                    return False  # deja actif
                return _audio_rx_logger.enable(pseudo, debug_dir)
            else:
                if not _audio_rx_logger.is_enabled():
                    return False  # deja inactif
                _audio_rx_logger.disable()
                return True
        except Exception as e:
            print(f"[AUDIO RX LOG] Echec toggle : {e}")
            return False

    def get_audio_stats_snapshot(self) -> dict:
        """Renvoie une copie atomique des compteurs internes pour les
        logs [AUDIO STATS] periodiques. Toutes les valeurs sont cumulatives
        depuis le demarrage : l'appelant doit calculer les deltas lui-meme."""
        with self._lock:
            return {
                "frames_sent":     int(self._frames_sent),
                "frames_received": int(self._frames_received),
                "frames_dropped_by_sender": dict(self._frames_dropped_by_sender),
                "output_underruns_by_sender": dict(self._output_underruns_by_sender),
                "output_truncations_by_sender": dict(self._output_truncations_by_sender),
                "output_silence_implicite_by_sender": dict(
                    self._output_silence_implicite_by_sender
                ),
            }

    def is_available(self) -> bool:
        return _SD_AVAILABLE

    # -----------------------------------------
    #  Capture
    # -----------------------------------------

    def set_on_capture(self, callback):
        """
        Definit la fonction appelee a chaque bloc audio capture.
        Signature : callback(frame: np.ndarray float32 shape=(BLOCK_SIZE,))
        """
        self._on_capture = callback

    def start_capture(self, device_id: int = None) -> bool:
        """Demarre la capture du micro."""
        if not _SD_AVAILABLE:
            return False
        self.stop_capture()
        try:
            self._input_stream = sd.InputStream(
                device     = device_id,
                channels   = CHANNELS,
                samplerate = SAMPLE_RATE,
                blocksize  = BLOCK_SIZE,
                dtype      = DTYPE,
                callback   = self._on_input_block,
            )
            self._input_stream.start()
            # Memoriser le device pour pouvoir relancer en cas de gel
            # (restart_streams() appele par le watchdog OCR apres un freeze)
            self._last_input_device = device_id
            return True
        except Exception as e:
            print(f"[AUDIO] Erreur start_capture : {e}")
            self._input_stream = None
            return False

    def stop_capture(self):
        if self._input_stream is not None:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

    def set_capture_muted(self, muted: bool):
        """Empeche l'envoi du micro (mute logiciel)."""
        self._capture_muted = muted

    def _on_input_block(self, indata, frames, time_info, status):
        """Callback sounddevice : appelee a chaque bloc capture."""
        if self._capture_muted:
            return
        if self._on_capture is None:
            return
        try:
            # indata shape = (frames, channels) ; on veut (frames,) mono
            if indata.ndim == 2 and indata.shape[1] > 1:
                mono = indata.mean(axis=1).astype(np.float32)
            else:
                mono = indata[:, 0].astype(np.float32) if indata.ndim == 2 else indata.astype(np.float32)

            # 1. Gain micro (amplification manuelle)
            if self._mic_gain != 1.0:
                mono = mono * self._mic_gain
                # Soft clip via tanh : transparent jusqu'a ~0.9, saturation
                # douce au-dela. Evite les distorsions en dents de scie du
                # hard clip (np.clip) sur les pics de voix forts (typique :
                # voyelle "A" prononcee fort -> amplitude crete depasse 1.0
                # apres gain -> hard clip tronque net -> harmoniques aigues
                # audibles comme un crachat). Le tanh produit une saturation
                # progressive, beaucoup plus naturelle.
                # Seul le passage tanh est applique (pas de clip dur ensuite)
                # car tanh(x) est borne en (-1, 1) par construction.
                np.tanh(mono, out=mono)

            # 1.5. Suppression de bruit (RNNoise). No-op si feature
            # desactivee ou pyrnnoise non installe. On le fait AVANT le
            # calcul RMS pour que le gate se declenche sur le signal
            # nettoye (moins de faux positifs sur bruit ambiant).
            if self._noise_suppressor.enabled:
                mono = self._noise_suppressor.process(mono)

            # 2. Mesurer RMS pour VU-metre et noise gate
            rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) > 0 else 0.0
            self._mic_rms_current = rms

            # 3. Noise gate avec hysteresis : on ouvre sur gate_threshold (0.03),
            #    on maintient ouvert tant que RMS > gate_close_thr (0.010),
            #    puis on laisse le hold_ms (800ms) avant de vraiment fermer.
            # Exception : si _gate_force_open (PTT radio actif), on force ouvert
            now = time.monotonic()
            if self._gate_force_open:
                self._gate_is_open     = True
                self._gate_last_open_t = now
            elif rms >= self._gate_threshold:
                # Au-dessus du seuil d'ouverture : ouvre / maintient
                self._gate_is_open     = True
                self._gate_last_open_t = now
            elif self._gate_is_open and rms >= self._gate_close_thr:
                # Deja ouvert et RMS entre close_thr et open_thr :
                # on maintient ouvert (hysteresis : evite les on/off rapides en fin de phrase)
                self._gate_last_open_t = now
            else:
                # Rester ouvert tant que dans la fenetre de hold
                if self._gate_is_open:
                    elapsed_ms = (now - self._gate_last_open_t) * 1000.0
                    if elapsed_ms > self._gate_hold_ms:
                        self._gate_is_open = False

            # 4. Si gate ferme, on n'envoie rien (economie bande passante)
            if not self._gate_is_open:
                return

            self._on_capture(mono)
            self._frames_sent += 1
        except Exception as e:
            print(f"[AUDIO] Erreur capture : {e}")

    # -----------------------------------------
    #  Lecture
    # -----------------------------------------

    def start_playback(self, device_id: int = None) -> bool:
        """Demarre la sortie audio."""
        if not _SD_AVAILABLE:
            return False
        self.stop_playback()
        try:
            self._output_stream = sd.OutputStream(
                device     = device_id,
                channels   = CHANNELS,
                samplerate = SAMPLE_RATE,
                blocksize  = BLOCK_SIZE,
                dtype      = DTYPE,
                callback   = self._on_output_block,
            )
            self._output_stream.start()
            # Memoriser le device pour pouvoir relancer en cas de gel
            self._last_output_device = device_id
            return True
        except Exception as e:
            print(f"[AUDIO] Erreur start_playback : {e}")
            self._output_stream = None
            return False

    def restart_streams(self):
        """Relance les streams audio (input + output) avec les derniers
        devices utilises. Appele par le watchdog OCR apres detection d'un
        gel du process Python (gel CUDA ou autre), car ces gels affectent
        aussi les callbacks sounddevice et peuvent laisser les streams dans
        un etat ou ils ne reprennent pas spontanement.

        Seuls les streams actifs sont relances : si l'utilisateur a mute
        son micro ou n'avait pas demarre la sortie, on ne force rien."""
        restarted = []
        # Input : on relance seulement s'il etait actif
        if self._input_stream is not None:
            last_dev = getattr(self, "_last_input_device", None)
            try:
                if self.start_capture(last_dev):
                    restarted.append("input")
            except Exception as e:
                print(f"[AUDIO] restart_streams input erreur : {e}")
        # Output : idem
        if self._output_stream is not None:
            last_dev = getattr(self, "_last_output_device", None)
            try:
                if self.start_playback(last_dev):
                    restarted.append("output")
            except Exception as e:
                print(f"[AUDIO] restart_streams output erreur : {e}")
        return restarted

    def stop_playback(self):
        if self._output_stream is not None:
            try:
                self._output_stream.stop()
                self._output_stream.close()
            except Exception:
                pass
            self._output_stream = None

    def _on_output_block(self, outdata, frames, time_info, status):
        """
        Callback sounddevice : appelee quand la sortie audio a besoin de nouveaux samples.
        On mixe tous les buffers remote, chacun a son volume propre.

        Trois mixages paralleles :
          - mix_prox  : voix de proximite (sans effet radio). Recoit l'echo
                        grotte si _cave_echo_active.
          - mix_radio : voix deja passees par apply_radio_effect. Pas d'echo
                        grotte (les 2 effets ensemble sonneraient mal).
          - mix_phone : voix telephone CircusPhone (D3). Ni effet radio, ni
                        echo grotte : la conversation telephone est "claire
                        des deux cotes" (spec). Volume non attenue par la
                        distance (gere en amont cote core : set_user_volume
                        a 1.0 pour le correspondant).
        Les 3 mix sont additionnes a la fin.
        """
        try:
            mix_prox  = np.zeros(frames, dtype=np.float32)
            mix_radio = np.zeros(frames, dtype=np.float32)
            mix_phone = np.zeros(frames, dtype=np.float32)
            with self._lock:
                for name, buf in self._remote_buffers.items():
                    vol = self._remote_volumes.get(name, 1.0)
                    mult = self._remote_multipliers.get(name, 1.0)
                    final_vol = vol * mult
                    if final_vol <= 0.001:
                        # Vidons quand meme la file pour eviter l'accumulation
                        try:
                            buf.get_nowait()
                        except queue.Empty:
                            pass
                        continue

                    # ─── Jitter buffer warmup (ajout 02/06/2026) ───
                    # Si on est en mode "waiting", on attend d'avoir au moins
                    # JITTER_BUFFER_WARMUP_FRAMES dans la queue avant de pop.
                    # Pendant ce temps, ce sender contribue silence au mix
                    # (PAS d'underrun compte : c'est un silence volontaire).
                    # Une fois le seuil atteint, on passe en "playing".
                    # Effet : la queue oscille en regime entre WARMUP et
                    # WARMUP+1 trames, ce qui donne ~60ms de marge de jitter
                    # au lieu de 0 (cf. diagnostic CSV Skywat 02/06).
                    _jb_st = self._jb_state.get(name, "waiting")
                    if _jb_st == "waiting":
                        if buf.qsize() < JITTER_BUFFER_WARMUP_FRAMES:
                            # Pas encore assez : silence contribue, on attend.
                            self._jb_silent_blocks[name] = (
                                self._jb_silent_blocks.get(name, 0) + 1
                            )
                            continue
                        # Seuil atteint : on passe en playing pour ce callback.
                        self._jb_state[name] = "playing"

                    try:
                        frame = buf.get_nowait()
                        # PLC (02/06/2026 v3) : memoriser la trame pour la
                        # rejouer en cas de prochain underrun. Reset du flag
                        # _plc_used : on a une "nouvelle" derniere trame, on
                        # peut donc faire un PLC dessus a la prochaine
                        # occasion sans risque de bouclage audible.
                        self._plc_last_frame[name] = frame
                        self._plc_used[name] = False
                        is_phone = self._remote_is_phone.get(name, False)
                        # Detecter taille frame vs block_size sounddevice
                        # pour stats debug crackling (25/05/2026). Pas de
                        # modification du comportement existant.
                        len_frame = len(frame)
                        if len_frame > frames:
                            # Frame plus grande que ce que sounddevice demande
                            # -> tronquee (perte de la fin). Tres rare en
                            # regime normal (les emetteurs envoient BLOCK_SIZE=960
                            # = ce que sounddevice consomme), mais peut arriver
                            # si le block_size sounddevice cote receveur est
                            # different (ex : driver custom, peripherique exotique).
                            self._output_truncations_by_sender[name] = (
                                self._output_truncations_by_sender.get(name, 0) + 1
                            )
                        elif len_frame < frames:
                            # Frame plus petite -> on complete avec du silence
                            # implicite (target reste a 0 sur la fin). Click
                            # audible possible (discontinuite signal -> silence).
                            self._output_silence_implicite_by_sender[name] = (
                                self._output_silence_implicite_by_sender.get(name, 0) + 1
                            )
                        # Jitter buffer (hysteresis) : un pop reussi reinit
                        # le streak de queue vide. Le retour en "waiting"
                        # n'est plus declenche ici (qsize=0 apres pop est
                        # un cas normal et frequent), mais dans le handler
                        # queue.Empty ci-dessous, apres
                        # JITTER_BUFFER_ZERO_STREAK callbacks consecutifs
                        # avec queue vide. Cela tolere les mini-trous
                        # de 50-100ms dans le flux du sender sans imposer
                        # un re-warmup penalisant (60ms de silence supp).
                        self._jb_zero_streak[name] = 0
                        # CircusPhone (D3) : voix telephone -> mix_phone
                        # direct. Court-circuite tout traitement radio /
                        # Mode RP / echo : la voix telephone est claire.
                        if is_phone:
                            if len_frame >= frames:
                                mix_phone += frame[:frames] * final_vol
                            else:
                                mix_phone[:len_frame] += frame * final_vol
                            continue
                        is_radio = self._remote_is_radio.get(name, False)
                        # Mode RP : si le client a decide que ce sender doit
                        # etre filtre radio (un des 2 porte casque + Mode RP
                        # local actif), on applique le filtre radio MAINTENANT
                        # sur sa voix de proximite, et on la route vers
                        # mix_radio (donc pas d'echo grotte dessus, coherent).
                        # Les vraies trames radio (is_radio=True) sont deja
                        # filtrees cote emetteur, pas besoin de refiltre.
                        if not is_radio and self._remote_force_radio.get(name, False):
                            try:
                                frame = apply_radio_effect(frame, name)
                            except Exception:
                                pass
                            # Traiter comme radio pour le routage
                            is_radio = True
                        target = mix_radio if is_radio else mix_prox
                        # Ajuster taille si necessaire
                        if len_frame >= frames:
                            target += frame[:frames] * final_vol
                        else:
                            target[:len_frame] += frame * final_vol
                    except queue.Empty:
                        # Underrun POTENTIEL : la queue de ce sender etait vide
                        # alors que sounddevice demandait des samples. Mais
                        # attention au faux positif : si le sender ne parle pas
                        # (noise gate ferme cote emetteur), sa queue est vide
                        # naturellement et ce n'est PAS un bug.
                        #
                        # Fix 25/05/2026 : on ne compte un underrun QUE si on
                        # a recu une trame de ce sender depuis moins de
                        # _UNDERRUN_GAP_S secondes (100 ms par defaut). Au-dela
                        # = sender silencieux = normal, on ne compte rien.
                        # En-deca = on attendait une trame, elle n'est pas
                        # arrivee = VRAI underrun (jitter reseau, drift, etc.)
                        # qui peut causer du crackling cote ecoute.
                        #
                        # Note : on garde aussi le filtre final_vol > 0.001
                        # pour exclure les senders mute (volume 0).
                        _is_real_underrun = False
                        if final_vol > 0.001:
                            last_ts = self._last_remote_frame_ts.get(name, 0.0)
                            if last_ts > 0 and (
                                time.monotonic() - last_ts
                            ) <= _UNDERRUN_GAP_S:
                                self._output_underruns_by_sender[name] = (
                                    self._output_underruns_by_sender.get(name, 0) + 1
                                )
                                _is_real_underrun = True

                        # PLC (02/06/2026 v3) : si on est en VRAI underrun
                        # (sender actif, trame manquante) ET qu'on a une
                        # derniere trame memorisee ET qu'elle n'a pas deja
                        # servi pour un PLC, on la rejoue attenuee a la
                        # place du silence. Effet attendu : le pop audible
                        # devient un micro-prolongement de la derniere
                        # trame entendue, beaucoup plus discret a l'oreille.
                        # Conditions strictes (anti-bouclage) :
                        #   - _is_real_underrun : sender actif, pas mute
                        #   - last_frame existe (au moins 1 pop deja fait)
                        #   - _plc_used = False : on n'a pas deja PLC sur cette trame
                        # Apres usage, _plc_used = True : si nouveau underrun
                        # consecutif, on ecrira silence (pas de boucle).
                        if (_is_real_underrun
                                and self._plc_last_frame.get(name) is not None
                                and not self._plc_used.get(name, False)):
                            _plc_frame = self._plc_last_frame[name]
                            _plc_len = len(_plc_frame)
                            _plc_vol = final_vol * PLC_GAIN
                            # Routage identique au cas pop normal : telephone
                            # -> mix_phone, sinon mix_radio ou mix_prox selon
                            # is_radio. Le filtre radio (apply_radio_effect)
                            # n'est PAS reapplique sur la trame PLC : son
                            # etat biquad serait incoherent, et l'attenuation
                            # -6dB masque suffisamment l'artefact.
                            _plc_is_phone = self._remote_is_phone.get(name, False)
                            _plc_is_radio = self._remote_is_radio.get(name, False)
                            if _plc_is_phone:
                                if _plc_len >= frames:
                                    mix_phone += _plc_frame[:frames] * _plc_vol
                                else:
                                    mix_phone[:_plc_len] += _plc_frame * _plc_vol
                            else:
                                _plc_target = mix_radio if _plc_is_radio else mix_prox
                                if _plc_len >= frames:
                                    _plc_target += _plc_frame[:frames] * _plc_vol
                                else:
                                    _plc_target[:_plc_len] += _plc_frame * _plc_vol
                            # Marquer la trame comme "deja PLC-utilisee"
                            self._plc_used[name] = True
                            self._plc_applied_total[name] = (
                                self._plc_applied_total.get(name, 0) + 1
                            )

                        # Hysteresis jitter buffer (02/06/2026 v2) : on
                        # arrive ici parce que la queue etait vide alors
                        # qu'on etait en "playing". Au lieu de basculer
                        # immediatement en "waiting" (qui imposerait 60ms
                        # de silence pour rewarmup), on incremente un
                        # streak. Tant que streak < JITTER_BUFFER_ZERO_STREAK,
                        # on reste en "playing" : le silence d'une trame
                        # (20ms) est moins gravant qu'un re-warmup (60ms).
                        # Au-dela du seuil, on considere que le sender
                        # s'est vraiment tu et on bascule en "waiting".
                        _streak = self._jb_zero_streak.get(name, 0) + 1
                        self._jb_zero_streak[name] = _streak
                        if _streak >= JITTER_BUFFER_ZERO_STREAK:
                            self._jb_state[name] = "waiting"
                            self._jb_warmup_count[name] = (
                                self._jb_warmup_count.get(name, 0) + 1
                            )
                            # Reset du streak : le prochain "warmup atteint"
                            # repartira proprement.
                            self._jb_zero_streak[name] = 0

                # Reverb grotte Schroeder (4 combs parallele + 2 all-pass serie).
                # Applique sur le mix prox uniquement, tout le mix passe dans la
                # meme reverb (ambiance globale : ma grotte). Design classique :
                #   - Comb : cree la queue de reverberation
                #   - All-pass : diffuse sans ajouter de queue (casse les modes)
                #   - Low-pass dans les combs : absorbe les aigus (effet paroi)
                # Wet = 0.22 : reverb audible mais la voix directe reste en avant.
                # Dry = 1.0 : on garde tout le signal original.
                if self._cave_echo_active:
                    wet = 0.6
                    damp = self._cave_comb_damp
                    for i in range(frames):
                        inp = mix_prox[i]
                        # --- 4 combs en parallele ---
                        comb_out = 0.0
                        for c in range(4):
                            buf = self._cave_comb_bufs[c]
                            idx = self._cave_comb_idxs[c]
                            delayed = buf[idx]
                            # Low-pass dans la boucle feedback (one-pole)
                            self._cave_comb_lp[c] = (
                                delayed * (1.0 - damp) + self._cave_comb_lp[c] * damp
                            )
                            # Ecrire : input + feedback * (signal retarde filtre)
                            buf[idx] = inp + self._cave_comb_lp[c] * self._cave_comb_fbs[c]
                            comb_out += delayed
                            self._cave_comb_idxs[c] = (idx + 1) % len(buf)
                        # Moyenne des 4 combs
                        sig = comb_out * 0.25
                        # --- 2 all-pass en serie ---
                        for a in range(2):
                            buf = self._cave_ap_bufs[a]
                            idx = self._cave_ap_idxs[a]
                            delayed = buf[idx]
                            # y = -g*x + delayed + g*y ; forme compacte :
                            out_ap = -self._cave_ap_coef * sig + delayed
                            buf[idx] = sig + self._cave_ap_coef * out_ap
                            sig = out_ap
                            self._cave_ap_idxs[a] = (idx + 1) % len(buf)
                        # Mix dry + wet
                        mix_prox[i] = inp + wet * sig

            mixed = mix_prox + mix_radio + mix_phone
            # Mixer le bip local (PTT feedback) si en cours.
            # Le bip est entendu par l'utilisateur lui-meme dans son casque,
            # ce n'est PAS envoye aux autres (juste pour le feedback PTT).
            with self._lock:
                if self._beep_buffer is not None and self._beep_idx < len(self._beep_buffer):
                    remaining = len(self._beep_buffer) - self._beep_idx
                    n_take = min(frames, remaining)
                    # Le bip utilise le facteur de volume "radio_beep" pour
                    # que l'utilisateur puisse l'attenuer s'il le trouve fort.
                    factor = float(self._beep_volume_factor)
                    mixed[:n_take] += self._beep_buffer[self._beep_idx:self._beep_idx + n_take] * factor
                    self._beep_idx += n_take
                    if self._beep_idx >= len(self._beep_buffer):
                        # Bip termine
                        self._beep_buffer = None
                        self._beep_idx = 0
                # Mixer le soundboard (v0.2 alpha 029) si en cours. Volume
                # applique par _soundboard_volume_factor (slider "Son A").
                if self._soundboard_buffer is not None and self._soundboard_idx < len(self._soundboard_buffer):
                    remaining = len(self._soundboard_buffer) - self._soundboard_idx
                    n_take = min(frames, remaining)
                    factor = float(self._soundboard_volume_factor)
                    mixed[:n_take] += self._soundboard_buffer[self._soundboard_idx:self._soundboard_idx + n_take] * factor
                    self._soundboard_idx += n_take
                    if self._soundboard_idx >= len(self._soundboard_buffer):
                        # Son termine
                        self._soundboard_buffer = None
                        self._soundboard_idx = 0
                # Mixer la sonnerie telephone CircusPhone (D2) si en cours.
                # Contrairement aux 2 sons ci-dessus, ce buffer BOUCLE :
                # quand on atteint la fin, on repart au debut. Une frame de
                # sortie peut donc chevaucher la fin et le debut du motif,
                # d'ou la boucle de remplissage ci-dessous. La lecture ne
                # s'arrete jamais d'elle-meme : seul stop_phone_ring() met
                # _phone_ring_buffer a None. Volume : _phone_ring_volume_factor
                # (slider "Sonnerie telephone").
                if self._phone_ring_buffer is not None:
                    ring_buf = self._phone_ring_buffer
                    ring_len = len(ring_buf)
                    if ring_len > 0:
                        factor = float(self._phone_ring_volume_factor)
                        filled = 0
                        idx = self._phone_ring_idx
                        while filled < frames:
                            n_take = min(frames - filled, ring_len - idx)
                            mixed[filled:filled + n_take] += (
                                ring_buf[idx:idx + n_take] * factor
                            )
                            filled += n_take
                            idx += n_take
                            if idx >= ring_len:
                                idx = 0  # boucle : retour au debut du motif
                        self._phone_ring_idx = idx
                # CircusPhone (D4 etape 3) : son de notification message.
                # One-shot (~1s) : se joue une fois et s'arrete. Meme volume
                # que la sonnerie (slider "Sonnerie tel.") comme prevu dans
                # la spec ("sonnerie + bip d'appel + notif MP").
                if self._phone_notif_buffer is not None and \
                   self._phone_notif_idx < len(self._phone_notif_buffer):
                    remaining = len(self._phone_notif_buffer) - self._phone_notif_idx
                    n_take = min(frames, remaining)
                    factor = float(self._phone_ring_volume_factor)
                    mixed[:n_take] += self._phone_notif_buffer[
                        self._phone_notif_idx:self._phone_notif_idx + n_take
                    ] * factor
                    self._phone_notif_idx += n_take
                    if self._phone_notif_idx >= len(self._phone_notif_buffer):
                        self._phone_notif_buffer = None
                        self._phone_notif_idx = 0
            # Log audio RX detaille (no-op si toggle desactive). On collecte
            # le state APRES tous les pops et tous les ajouts (sonnerie,
            # beep, etc.), juste avant le soft clip tanh, pour avoir le
            # mix_peak_pre_tanh "vrai". On mesure aussi le peak APRES tanh.
            # Branchement conditionnel : on evite tout calcul si le log
            # n'est pas actif (callback temps-reel a 50 Hz).
            _log_active = (_audio_rx_logger is not None
                           and _audio_rx_logger.is_enabled())
            if _log_active:
                _peak_pre = float(np.max(np.abs(mixed))) if frames > 0 else 0.0
            # Soft clip sur le mix final via tanh : transparent jusqu'a ~0.9,
            # saturation douce au-dela. Evite les distorsions en dents de scie
            # du hard clip quand plusieurs joueurs parlent fort en meme temps
            # ou quand un joueur prononce une voyelle ouverte (A, O) tres fort.
            # tanh(x) est borne en (-1, 1) par construction, donc pas besoin
            # de clip dur ensuite.
            np.tanh(mixed, out=mixed)
            outdata[:, 0] = mixed
            # Suite du log_out apres ecriture outdata : peak post-tanh,
            # senders_state (snapshot des queues APRES pops), flags.
            if _log_active:
                _peak_post = float(np.max(np.abs(mixed))) if frames > 0 else 0.0
                _now_cb = time.monotonic()
                _cb_period_ms = ((_now_cb - self._last_callback_ts) * 1000.0
                                 if self._last_callback_ts > 0 else -1.0)
                self._last_callback_ts = _now_cb
                # Snapshot etat des queues APRES les pops effectues dans le
                # callback. On parcourt _remote_buffers et on lit qsize.
                # Detection underrun : approximation via _last_remote_frame_ts
                # (sender ayant recu une trame il y a < _UNDERRUN_GAP_S
                # secondes ET dont la queue est vide = under=True).
                _senders_state = {}
                try:
                    with self._lock:
                        for _sname, _sbuf in self._remote_buffers.items():
                            _svol = self._remote_volumes.get(_sname, 1.0)
                            _smult = self._remote_multipliers.get(_sname, 1.0)
                            _sfinal = _svol * _smult
                            _sqsz = _sbuf.qsize()
                            _slast = self._last_remote_frame_ts.get(_sname, 0.0)
                            _sunder = (
                                _sfinal > 0.001
                                and _sqsz == 0
                                and _slast > 0
                                and (_now_cb - _slast) <= _UNDERRUN_GAP_S
                            )
                            _senders_state[_sname] = {
                                "q": _sqsz,
                                "vol": round(_sfinal, 3),
                                "under": bool(_sunder),
                                "jb": self._jb_state.get(_sname, "?"),
                                "streak": self._jb_zero_streak.get(_sname, 0),
                                "plc": self._plc_applied_total.get(_sname, 0),
                            }
                except Exception:
                    _senders_state = {}
                # Cumuls truncations / silence implicite (somme sur tous les
                # senders, pas par-sender pour rester compact dans le CSV).
                try:
                    _trunc_total = sum(
                        self._output_truncations_by_sender.values()
                    )
                    _silence_total = sum(
                        self._output_silence_implicite_by_sender.values()
                    )
                except Exception:
                    _trunc_total = 0
                    _silence_total = 0
                _flags = {
                    "cave_echo": bool(self._cave_echo_active),
                    "beep": self._beep_buffer is not None,
                    "soundboard": self._soundboard_buffer is not None,
                    "sonnerie": self._phone_ring_buffer is not None,
                }
                try:
                    _audio_rx_logger.log_out(
                        callback_period_ms=_cb_period_ms,
                        senders_state=_senders_state,
                        mix_peak_pre_tanh=_peak_pre,
                        mix_peak_post_tanh=_peak_post,
                        trunc_total=_trunc_total,
                        silence_impl_total=_silence_total,
                        flags=_flags,
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[AUDIO] Erreur lecture : {e}")
            outdata.fill(0)

    # -----------------------------------------
    #  Echo grotte (active/desactive par le client selon container courant)
    # -----------------------------------------

    def set_cave_echo(self, active: bool):
        """Active ou desactive la reverb des grottes sur le mix de proximite.

        Appele par le client chaque fois que le container du joueur local
        change : True si dans une grotte (rock01_*, sand01_*), False sinon.
        A la desactivation, les buffers sont laisses tels quels mais ne
        sont plus relus -> la queue de reverb s'eteint naturellement.
        """
        with self._lock:
            was_active = self._cave_echo_active
            self._cave_echo_active = bool(active)
            # A l'activation, vider tous les buffers pour eviter un echo
            # "parasite" laisse par une grotte precedente. En mode desactive
            # on n'ecrit plus dans les buffers donc pas besoin de reset.
            if self._cave_echo_active and not was_active:
                for b in self._cave_comb_bufs:
                    b.fill(0.0)
                for b in self._cave_ap_bufs:
                    b.fill(0.0)
                self._cave_comb_idxs = [0] * 4
                self._cave_ap_idxs   = [0] * 2
                self._cave_comb_lp   = [0.0] * 4
        if was_active != self._cave_echo_active:
            status = "ACTIVE" if self._cave_echo_active else "INACTIVE"
            print(f"[CAVE ECHO] {status}")

    # -----------------------------------------
    #  Flux entrants
    # -----------------------------------------

    def feed_remote_frame(self, sender_name: str, frame_bytes: bytes,
                          is_radio: bool = False, is_phone: bool = False):
        """
        Alimente le buffer de lecture avec une trame audio recue via WebSocket.
        frame_bytes : raw PCM float32 mono, longueur = BLOCK_SIZE * 4
        is_radio    : True si la trame a deja ete filtree (effet radio PTT).
                      L'echo grotte est applique au niveau du MIX final de
                      proximite (voir _on_output_stream), pas par-trame :
                      les trames radio sont dans un mix separe et echappent
                      a l'echo, conformement a la regle "pas d'echo radio".
        is_phone    : True si c'est de la voix telephone CircusPhone (D3).
                      Routee vers mix_phone : ni filtre radio, ni echo
                      grotte, ni attenuation de distance. is_phone est
                      prioritaire sur is_radio (les deux ne sont jamais
                      vrais en meme temps en pratique, mais par securite
                      le mix traite is_phone en premier).
        """
        try:
            frame = np.frombuffer(frame_bytes, dtype=np.float32)
            if len(frame) == 0:
                return

            # Variables pour log audio RX detaille (si actif). Calculees au
            # fil du flow normal, consommees dans l'appel log_rx en fin de
            # methode. Cout negligeable meme si log inactif (juste qsize()
            # et un get sur dict).
            _rx_outcome = "OK"

            with self._lock:
                if sender_name not in self._remote_buffers:
                    self._remote_buffers[sender_name] = queue.Queue(maxsize=MAX_QUEUE_LEN)
                    # Volume par defaut 0 : pas audible tant qu'on ne le configure pas
                    self._remote_volumes[sender_name] = 0.0
                    # Nouveau sender : on demarre en mode warmup pour
                    # laisser la queue se remplir avant de jouer (anti-crackling).
                    self._jb_state[sender_name] = "waiting"
                q = self._remote_buffers[sender_name]
                # Memoriser is_radio / is_phone de la derniere trame
                # (utilises au mix dans _on_output_block).
                self._remote_is_radio[sender_name] = is_radio
                self._remote_is_phone[sender_name] = is_phone

            # Snapshot taille queue AVANT push (utile pour log_rx). On le
            # capture hors lock : qsize() est une operation atomique sur
            # queue.Queue et reste indicative.
            _rx_q_before = q.qsize()

            # Anti-drift : si la file est pleine, on jette le plus ancien
            # NOTE 25/05/2026 : ce drop etait silencieux jusqu'a present, ce
            # qui empechait de detecter une cause potentielle de crackling
            # cote reception. Maintenant on incremente un compteur (lu par
            # get_audio_stats_snapshot toutes les 30s) et on logge le premier
            # drop par sender + un par 30s (anti-spam) avec [AUDIO DROP RX].
            if q.full():
                try:
                    q.get_nowait()
                    # Compteur cumulatif par sender.
                    self._frames_dropped_by_sender[sender_name] = (
                        self._frames_dropped_by_sender.get(sender_name, 0) + 1
                    )
                    _rx_outcome = "DROP_QUEUE_FULL"
                    # Niveau B : log throttle 30s.
                    now_log = time.monotonic()
                    last_logged = self._first_drop_logged_by_sender.get(
                        sender_name, 0.0
                    )
                    if (now_log - last_logged) >= 30.0:
                        _ns_log(
                            f"[AUDIO DROP RX] queue pleine pour {sender_name!r} "
                            f"(total drops: "
                            f"{self._frames_dropped_by_sender[sender_name]}, "
                            f"maxsize={MAX_QUEUE_LEN})"
                        )
                        self._first_drop_logged_by_sender[sender_name] = now_log
                except queue.Empty:
                    pass
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass

            # Snapshot taille queue APRES push.
            _rx_q_after = q.qsize()

            self._frames_received += 1
            # Fix underrun 25/05/2026 : memoriser le timestamp de la derniere
            # trame recue pour ce sender. _on_output_block s'en sert pour
            # distinguer un VRAI underrun (queue vide alors qu'on vient de
            # recevoir = jitter reel) d'un faux underrun (sender silencieux
            # depuis > 100ms = comportement normal du noise gate emetteur).
            # NB : on calcule delta_ms (depuis trame precedente du meme
            # sender) AVANT d'ecraser _last_remote_frame_ts, pour log_rx.
            _now_mono = time.monotonic()
            _prev_ts = self._last_remote_frame_ts.get(sender_name, 0.0)
            _rx_delta_ms = ((_now_mono - _prev_ts) * 1000.0
                            if _prev_ts > 0 else -1.0)
            self._last_remote_frame_ts[sender_name] = _now_mono

            # Log audio RX detaille (no-op si toggle desactive).
            if _audio_rx_logger is not None:
                # Reconstruction du flag depuis is_radio/is_phone. Note :
                # is_radio melange 0x01 (radio canal) et 0x02 (radio profil),
                # on ne peut pas les distinguer ici. On utilise 0x01 par
                # convention pour les 2 cas radio. Si tu veux la distinction,
                # il faut faire remonter le flag exact depuis core.
                if is_phone:
                    _rx_type = 0x03
                elif is_radio:
                    _rx_type = 0x01
                else:
                    _rx_type = 0x00
                _audio_rx_logger.log_rx(
                    sender=sender_name,
                    msg_type=_rx_type,
                    size=len(frame_bytes),
                    delta_ms=_rx_delta_ms,
                    q_before=_rx_q_before,
                    q_after=_rx_q_after,
                    outcome=_rx_outcome,
                )
        except Exception as e:
            print(f"[AUDIO] Erreur feed : {e}")

    # -----------------------------------------
    #  Controle volume par utilisateur
    # -----------------------------------------

    def set_user_volume(self, name: str, volume: float):
        """Definit le volume (0.0 a 1.0+) pour un emetteur donne."""
        with self._lock:
            self._remote_volumes[name] = max(0.0, float(volume))

    def set_user_volume_multiplier(self, name: str, mult: float):
        """
        Definit un multiplicateur manuel (slider UI) pour un emetteur.
        - 0.0 = mute total
        - 1.0 = normal
        - 2.0 = x2 (ampli)
        Applique en plus du volume de proximite.
        """
        with self._lock:
            self._remote_multipliers[name] = max(0.0, float(mult))

    def set_mic_gain(self, gain: float):
        """Gain micro (0.0-3.0). 1.0 = normal."""
        self._mic_gain = max(0.0, float(gain))

    def set_noise_suppression(self, enabled: bool):
        """Active/desactive la suppression de bruit (RNNoise). Si
        pyrnnoise n'est pas installe, l'appel est silencieusement
        ignore (le flag reste False)."""
        self._noise_suppressor.set_enabled(bool(enabled))

    def is_noise_suppression_available(self) -> bool:
        """True si pyrnnoise est installe et l'init a reussi.
        L'UI utilise ca pour griser la checkbox quand non dispo."""
        return self._noise_suppressor.available

    def is_noise_suppression_enabled(self) -> bool:
        """True si la feature est actuellement active."""
        return self._noise_suppressor.enabled

    def set_gate_threshold(self, threshold: float):
        """Seuil du noise gate (0.0-1.0 en RMS). 0.0 = gate toujours ouvert.
        Le seuil de fermeture (hysteresis) est automatiquement fixe a 1/3
        du seuil d'ouverture, pour eviter de couper les fins de phrase."""
        self._gate_threshold = max(0.0, min(1.0, float(threshold)))
        self._gate_close_thr = self._gate_threshold / 3.0

    def set_gate_force_open(self, force_open: bool):
        """Force le noise gate a rester ouvert (bypass seuil RMS).
        A utiliser pour le PTT radio : quand la touche est enfoncee, on veut
        transmettre TOUT le son y compris les consonnes initiales faibles,
        sans attendre que le RMS atteigne le seuil."""
        self._gate_force_open = bool(force_open)

    def force_gate_close(self):
        """Force la fermeture immediate du noise gate, peu importe le RMS courant
        ou le hold_ms restant.
        A utiliser au release du PTT radio/profil : sans ca, si l'utilisateur
        continue a parler apres release (ex: parler a quelqu'un dans la piece),
        le gate adaptatif maintient l'ouverture parce que le RMS reste au-dessus
        du seuil de fermeture, et la voix continue d'etre transmise pendant
        plusieurs secondes. Avec force_gate_close(), on coupe net : il faut
        attendre que le RMS retombe au-dessus du seuil d'ouverture pour que
        le gate se rouvre (mais comme on n'est plus en PTT, ca ne transmet
        plus en radio de toute facon)."""
        self._gate_is_open = False
        # On reset aussi gate_last_open_t a 0 pour eviter qu'un appel ulterieur
        # avec un RMS entre close_thr et open_thr re-allume le gate par
        # hysteresis. La prochaine ouverture devra venir d'un RMS >= open_thr.
        self._gate_last_open_t = 0.0

    @staticmethod
    def _generate_beep(freq_hz: float, duration_ms: int,
                       fade_ms: int = 8, amplitude: float = 0.25) -> "np.ndarray":
        """Genere un buffer de bip sinusoidal avec fade in/out pour eviter
        les clics. Retourne un numpy array float32 mono.
          freq_hz     : frequence du bip (Hz). 880 = aigu, 440 = grave
          duration_ms : duree totale du bip en millisecondes
          fade_ms     : duree des rampes fade-in et fade-out (anti-clic)
          amplitude   : volume du bip (0.0 a 1.0). 0.25 = pas trop fort
        """
        n = int(SAMPLE_RATE * duration_ms / 1000.0)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
        sig = np.sin(2.0 * np.pi * freq_hz * t).astype(np.float32) * amplitude
        # Fades pour eviter les clics au debut/fin
        n_fade = int(SAMPLE_RATE * fade_ms / 1000.0)
        if n_fade > 0 and 2 * n_fade < n:
            fade_in  = np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
            fade_out = np.linspace(1.0, 0.0, n_fade, dtype=np.float32)
            sig[:n_fade]  *= fade_in
            sig[-n_fade:] *= fade_out
        return sig

    def play_local_beep(self, kind: str = "press"):
        """Joue un bip local (entendu uniquement par l'utilisateur dans son
        casque). Utilise pour le feedback PTT : confirme que la touche radio
        est bien prise en compte.
          kind = "press"   -> bip aigu (880 Hz, 80ms) ou WAV custom
          kind = "release" -> bip grave (440 Hz, 60ms) ou WAV custom
        Le bip est mixe dans le flux de sortie audio par _on_output_block.
        Si un bip est deja en cours, le nouveau l'ecrase (pas de file d'attente).
        Si un WAV custom a ete charge pour ce kind (cf load_custom_beep),
        il est joue a la place du synth. Le volume global _beep_volume est
        applique a la lecture (multiplicateur sur l'amplitude).
        """
        with self._lock:
            if kind == "release":
                src = self._beep_release_custom \
                    if self._beep_release_custom is not None \
                    else self._beep_release
            else:
                src = self._beep_press_custom \
                    if self._beep_press_custom is not None \
                    else self._beep_press
            # Multiplier in-place serait dangereux (modifierait le synth
            # source). On copie. Cout = quelques milliers de floats, OK.
            if self._beep_volume == 1.0:
                self._beep_buffer = src
            else:
                self._beep_buffer = (src * self._beep_volume).astype(np.float32)
            self._beep_idx = 0

    # -----------------------------------------
    # Volumes des effets locaux (v0.2 alpha 002/029)
    # -----------------------------------------
    # Sliders dans l'UI : "Bip radio", "Son A" (= soundboard), "Son B"
    # (= sonnerie telephone, futur). Ces facteurs s'appliquent uniquement
    # cote LECTURE (sortie casque), pas sur ce qu'on envoie aux autres.
    # Plage acceptee : 0.0 (muet) a 2.0 (200%, double volume).

    def set_radio_beep_volume(self, factor: float):
        """Multiplicateur applique au bip PTT (slider 'Bip radio')."""
        with self._lock:
            self._beep_volume_factor = max(0.0, min(2.0, float(factor)))

    def set_soundboard_volume(self, factor: float):
        """Multiplicateur applique aux sons du soundboard ('Son A')."""
        with self._lock:
            self._soundboard_volume_factor = max(0.0, min(2.0, float(factor)))

    def set_phone_ring_volume(self, factor: float):
        """Multiplicateur applique a la sonnerie telephone ('Son B'). Pas
        encore branche cote UI : prevu pour CircusPhone (feature 4 du
        cycle 0.2)."""
        with self._lock:
            self._phone_ring_volume_factor = max(0.0, min(2.0, float(factor)))

    def play_soundboard(self, samples) -> bool:
        """Joue un buffer audio mono float32 a 48kHz dans le mix local.
        Entendu UNIQUEMENT par l'utilisateur lui-meme. Pour que les
        autres joueurs entendent, le client doit aussi emettre un
        message WebSocket soundboard_play qui declenchera la lecture
        sur leur cote (chacun joue son propre fichier local).

        v0.2 alpha 034 : la regle "un seul son a la fois" est appliquee
        ici cote audio_io. Si un son est deja en cours, retourne False
        sans rien faire (le nouveau son est ignore, l'ancien continue).
        Retourne True si la lecture a commence.

        Le slider 'Son A' applique son facteur via _on_output_block.

        samples doit etre un np.ndarray float32 mono a 48kHz (= SAMPLE_RATE).
        Si different, le caller (cote client.py) convertit avant d'appeler."""
        try:
            if samples is None:
                return False
            buf = np.asarray(samples, dtype=np.float32)
            # Si c'est un array stereo (n, 2), on prend la moyenne pour
            # rester mono.
            if buf.ndim == 2:
                buf = buf.mean(axis=1).astype(np.float32)
            elif buf.ndim != 1:
                return False  # forme inattendue
            with self._lock:
                # Verifier si un son est deja en cours. Si oui, on REFUSE
                # le nouveau (regle "un seul son a la fois" du cycle 0.2).
                if (self._soundboard_buffer is not None
                        and self._soundboard_idx < len(self._soundboard_buffer)):
                    return False
                self._soundboard_buffer = buf
                self._soundboard_idx    = 0
            return True
        except Exception:
            return False

    def is_soundboard_playing(self) -> bool:
        """Retourne True si un son du soundboard est en cours de lecture.
        Lue par le client (UI) pour griser le bouton du son pendant la
        lecture, et le re-activer quand la lecture est terminee."""
        with self._lock:
            if self._soundboard_buffer is None:
                return False
            return self._soundboard_idx < len(self._soundboard_buffer)

    # -----------------------------------------
    # Sonnerie telephone CircusPhone (Feature 4, D2)
    # -----------------------------------------
    # Deux motifs placeholder synthetises (pas de fichier son pour l'instant).
    # Le motif entier est concu pour boucler proprement : il commence et
    # finit sur du silence, donc la repetition ne produit pas de clic, et
    # une coupure nette (stop_phone_ring) tombe le plus souvent sur une
    # zone de faible amplitude.

    @staticmethod
    def _synth_phone_ring_pattern() -> "np.ndarray":
        """Motif sonnerie destinataire SYNTHETIQUE (fallback si sounds/ring.wav
        absent). Structure type sonnerie classique : deux bouffees de
        tonalite rapprochees puis une pause, le tout boucle par
        _on_output_block. Tonalite a deux frequences melangees pour un rendu
        un peu plus 'telephone' qu'un sinus pur. Amplitude moderee (0.22)."""
        sr = SAMPLE_RATE
        amp = 0.22
        # Une bouffee : 2 frequences melangees, fade in/out anti-clic.
        burst_ms = 400
        n_burst = int(sr * burst_ms / 1000.0)
        t = np.arange(n_burst, dtype=np.float32) / sr
        burst = (np.sin(2.0 * np.pi * 440.0 * t)
                 + 0.6 * np.sin(2.0 * np.pi * 480.0 * t)).astype(np.float32)
        burst *= amp / 1.6
        n_fade = int(sr * 12 / 1000.0)
        if n_fade > 0 and 2 * n_fade < n_burst:
            burst[:n_fade]  *= np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
            burst[-n_fade:] *= np.linspace(1.0, 0.0, n_fade, dtype=np.float32)
        # Silences : court entre les 2 bouffees, long apres.
        gap_short = np.zeros(int(sr * 0.20), dtype=np.float32)
        gap_long  = np.zeros(int(sr * 1.80), dtype=np.float32)
        # Motif complet : burst - gap_short - burst - gap_long
        return np.concatenate([burst, gap_short, burst, gap_long])

    @staticmethod
    def _synth_phone_dial_pattern() -> "np.ndarray":
        """Motif bip d'appel cote appelant SYNTHETIQUE (fallback si
        sounds/dial.wav absent). Une bouffee de tonalite unique repetee
        avec une pause longue : le 'tuut ... tuut' classique entendu
        pendant qu'on attend que le correspondant decroche.

        Amplitude 0.159 : alignee sur le RMS actif du ring synth
        (-19 dBFS) pour que les 3 sons telephone (ring/dial/notif) aient
        le meme niveau percu sous un slider unique. Avant : 0.20."""
        sr = SAMPLE_RATE
        amp = 0.159
        burst_ms = 500
        n_burst = int(sr * burst_ms / 1000.0)
        t = np.arange(n_burst, dtype=np.float32) / sr
        burst = np.sin(2.0 * np.pi * 420.0 * t).astype(np.float32) * amp
        n_fade = int(sr * 12 / 1000.0)
        if n_fade > 0 and 2 * n_fade < n_burst:
            burst[:n_fade]  *= np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
            burst[-n_fade:] *= np.linspace(1.0, 0.0, n_fade, dtype=np.float32)
        gap = np.zeros(int(sr * 2.50), dtype=np.float32)
        return np.concatenate([burst, gap])

    def play_phone_ring(self, kind: str = "ring"):
        """Demarre la sonnerie telephone EN BOUCLE.
          kind = "ring" -> motif sonnerie (destinataire d'un appel entrant)
          kind = "dial" -> motif bip d'appel (appelant, en attente de reponse)
        La lecture boucle jusqu'a stop_phone_ring(). Si une sonnerie est
        deja en cours, elle est remplacee par le nouveau motif (reset a 0).
        Entendu UNIQUEMENT par l'utilisateur local (jamais transmis)."""
        with self._lock:
            if kind == "dial":
                self._phone_ring_buffer = self._phone_ring_pattern_dial
            else:
                self._phone_ring_buffer = self._phone_ring_pattern_ring
            self._phone_ring_idx = 0

    def stop_phone_ring(self):
        """Coupe la sonnerie telephone immediatement (coupure nette, pas
        de fade-out). Idempotent : appeler quand rien ne sonne ne fait
        rien. Appele sur chaque transition d'appel (decroche, refus,
        timeout, raccrochage, deconnexion)."""
        with self._lock:
            self._phone_ring_buffer = None
            self._phone_ring_idx = 0

    def is_phone_ringing(self) -> bool:
        """Retourne True si la sonnerie telephone est en cours de lecture."""
        with self._lock:
            return self._phone_ring_buffer is not None

    @staticmethod
    def _synth_phone_notif_pattern() -> "np.ndarray":
        """Motif notification de message texte SYNTHETIQUE (fallback si
        sounds/notif.wav absent). Deux bips courts ascendants type
        'ding-dong', conçus pour etre clairement audibles sans etre
        agressifs. Comme tous les motifs phone, commence et finit a 0.0
        (anti-clic).

        Amplitude 0.162 : alignee sur le RMS actif du ring synth
        (-19 dBFS) pour que les 3 sons telephone (ring/dial/notif) aient
        le meme niveau percu sous un slider unique. Avant : 0.22."""
        sr = SAMPLE_RATE
        amp = 0.162

        def burst(freq_hz: float, ms: int) -> "np.ndarray":
            n = int(sr * ms / 1000.0)
            t = np.arange(n, dtype=np.float32) / sr
            b = np.sin(2.0 * np.pi * freq_hz * t).astype(np.float32) * amp
            n_fade = int(sr * 10 / 1000.0)
            if n_fade > 0 and 2 * n_fade < n:
                b[:n_fade]  *= np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
                b[-n_fade:] *= np.linspace(1.0, 0.0, n_fade, dtype=np.float32)
            return b

        # Deux bips : un grave (440Hz) puis un aigu (660Hz). Pause courte
        # entre les 2 + silence final pour donner le motif "ding-dong".
        b1 = burst(440.0, 180)
        b2 = burst(660.0, 220)
        gap_short = np.zeros(int(sr * 0.06), dtype=np.float32)
        gap_end   = np.zeros(int(sr * 0.50), dtype=np.float32)
        return np.concatenate([b1, gap_short, b2, gap_end])

    def play_phone_notif(self):
        """Joue le son de notification d'un message recu (one-shot ~1s).
        Si une notif est deja en train de jouer, elle est remplacee (reset
        a 0). Le volume est pilote par _phone_ring_volume_factor (meme
        slider que la sonnerie - cf spec)."""
        with self._lock:
            self._phone_notif_buffer = self._phone_notif_pattern
            self._phone_notif_idx = 0

    # ─────────────────────────────────────────────
    # Mute micro depuis l'overlay phone (D4 etape 2)
    # ─────────────────────────────────────────────
    def set_capture_muted(self, muted: bool):
        """Active/desactive le mute micro depuis l'overlay phone (bouton
        mute de l'ecran 'En appel'). Quand muted=True, la capture continue
        de tourner (pour pouvoir reprendre instantanement) mais aucune
        trame n'est emise sur le reseau : le micro est silencieux pour
        les autres joueurs. La spec impose la coupure NETTE (pas de fade)."""
        self._capture_muted = bool(muted)

    def is_capture_muted(self) -> bool:
        """Retourne True si le micro est actuellement coupe par l'overlay."""
        return self._capture_muted

    # -----------------------------------------
    # Bips PTT custom (WAV utilisateur + volume global)
    # -----------------------------------------
    # Permet de remplacer les bips synthetiques par defaut par un WAV
    # choisi via l'UI ("Sons PTT" dans l'onglet Audio). Le buffer est
    # decode et resample une fois au chargement ; play_local_beep() pioche
    # dans _beep_<kind>_custom si present, sinon dans le synth. _beep_volume
    # est un multiplicateur additionnel applique a la lecture, en sus du
    # slider "Bip radio" (_beep_volume_factor) qui agit cote mixage output.

    def load_custom_beep(self, kind: str, src_path: "str | Path") -> bool:
        """Charge un WAV en bip PTT custom pour `kind` ('press' ou 'release').

        Effets de bord :
          - Decode et resample le WAV (cf _load_wav_as_mono_48k).
          - Copie le fichier source vers <client_dir>/sounds/ptt_<kind>.wav
            pour qu'il survive aux redemarrages (auto-charge a l'init).
          - Met a jour self._beep_<kind>_custom avec le buffer en memoire.

        Retourne True si le chargement reussit, False sinon (WAV invalide,
        trop long, format non supporte, ecriture impossible). L'UI doit
        afficher un message d'erreur generique en cas d'echec, sans details
        (le test "fichier corrompu" du QA renvoie False sans crasher)."""
        if kind not in ("press", "release"):
            return False
        arr = _load_wav_as_mono_48k(src_path)
        if arr is None or arr.size == 0:
            return False
        try:
            _SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
            dst = _SOUNDS_DIR / f"ptt_{kind}.wav"
            # Copie atomique-ish : on ecrit le fichier dans son emplacement
            # final. shutil.copyfile suffit ici (pas de rename pour eviter
            # cross-device issues sur certaines configs Windows).
            shutil.copyfile(str(src_path), str(dst))
        except OSError:
            return False
        with self._lock:
            if kind == "press":
                self._beep_press_custom = arr
            else:
                self._beep_release_custom = arr
        return True

    def clear_custom_beep(self, kind: str) -> bool:
        """Supprime le WAV custom pour `kind` ('press' ou 'release') et
        revient au bip synthetique par defaut. Best-effort : retourne True
        meme si le fichier sur disque n'existait pas."""
        if kind not in ("press", "release"):
            return False
        try:
            (_SOUNDS_DIR / f"ptt_{kind}.wav").unlink(missing_ok=True)
        except OSError:
            pass
        with self._lock:
            if kind == "press":
                self._beep_press_custom = None
            else:
                self._beep_release_custom = None
        return True

    def has_custom_beep(self, kind: str) -> bool:
        """True si un bip custom est actuellement charge pour `kind`."""
        if kind == "press":
            return self._beep_press_custom is not None
        if kind == "release":
            return self._beep_release_custom is not None
        return False

    def set_beep_volume(self, v: float):
        """Definit le volume des bips PTT (synth ET custom).
        Plage : 0.0 (muet) a 1.0 (volume natif). Hors plage : clampe."""
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        self._beep_volume = max(0.0, min(1.0, v))

    def set_gate_hold_ms(self, hold_ms: int):
        """Duree en ms pendant laquelle le gate reste ouvert apres silence."""
        self._gate_hold_ms = max(0, int(hold_ms))

    def get_mic_rms(self) -> float:
        """Retourne le RMS actuel du micro (pour VU-metre)."""
        return self._mic_rms_current

    def is_gate_open(self) -> bool:
        """True si le noise gate laisse passer du son actuellement."""
        return self._gate_is_open

    def remove_user(self, name: str):
        """Supprime un utilisateur du buffer de lecture."""
        with self._lock:
            self._remote_buffers.pop(name, None)
            self._remote_volumes.pop(name, None)
            self._remote_multipliers.pop(name, None)
            self._remote_is_radio.pop(name, None)
            self._remote_is_phone.pop(name, None)
            self._remote_force_radio.pop(name, None)
        # Nettoyer l'etat filtre radio (garde le module leger)
        reset_radio_filter(name)

    def set_force_radio(self, sender_name: str, force: bool):
        """Active ou desactive le forcage radio sur un sender donne.
        Appele par le client quand le Mode RP est active et qu'il a calcule
        que ce sender doit etre filtre radio (emetteur et/ou recepteur
        porte un casque).

        L'effet : la trame PROXIMITE de ce sender passera par apply_radio_effect
        dans le mix, puis sera routee vers mix_radio au lieu de mix_prox
        (donc pas d'echo grotte dessus, coherent avec la regle "pas d'echo
        en radio").

        Pour les trames deja radio (is_radio=True cote emetteur = PTT radio),
        ce flag n'a aucun effet : elles sont deja traitees comme radio.
        """
        with self._lock:
            self._remote_force_radio[sender_name] = bool(force)

    def clear_force_radio(self):
        """Reset complet du forcage radio (utilise quand le client desactive
        le Mode RP : toutes les voix repassent en proximite normale)."""
        with self._lock:
            self._remote_force_radio.clear()

    def list_users(self) -> list[str]:
        with self._lock:
            return list(self._remote_buffers.keys())

    # -----------------------------------------
    #  Stats
    # -----------------------------------------

    def stats(self) -> dict:
        return {
            "frames_sent"     : self._frames_sent,
            "frames_received" : self._frames_received,
            "users_listening" : len(self._remote_buffers),
        }

    def close(self):
        self.stop_capture()
        self.stop_playback()
        with self._lock:
            self._remote_buffers.clear()
            self._remote_volumes.clear()
            self._remote_multipliers.clear()
            self._remote_is_radio.clear()
            self._remote_is_phone.clear()
