
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
import time

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

        Deux mixages paralleles :
          - mix_prox  : voix de proximite (sans effet radio). Recoit l'echo
                        grotte si _cave_echo_active.
          - mix_radio : voix deja passees par apply_radio_effect. Pas d'echo
                        grotte (les 2 effets ensemble sonneraient mal).
        Les 2 mix sont additionnes a la fin.
        """
        try:
            mix_prox  = np.zeros(frames, dtype=np.float32)
            mix_radio = np.zeros(frames, dtype=np.float32)
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
                    try:
                        frame = buf.get_nowait()
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
                        if len(frame) >= frames:
                            target += frame[:frames] * final_vol
                        else:
                            target[:len(frame)] += frame * final_vol
                    except queue.Empty:
                        pass

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

            mixed = mix_prox + mix_radio
            # Mixer le bip local (PTT feedback) si en cours.
            # Le bip est entendu par l'utilisateur lui-meme dans son casque,
            # ce n'est PAS envoye aux autres (juste pour le feedback PTT).
            with self._lock:
                if self._beep_buffer is not None and self._beep_idx < len(self._beep_buffer):
                    remaining = len(self._beep_buffer) - self._beep_idx
                    n_take = min(frames, remaining)
                    mixed[:n_take] += self._beep_buffer[self._beep_idx:self._beep_idx + n_take]
                    self._beep_idx += n_take
                    if self._beep_idx >= len(self._beep_buffer):
                        # Bip termine
                        self._beep_buffer = None
                        self._beep_idx = 0
            # Soft clip sur le mix final via tanh : transparent jusqu'a ~0.9,
            # saturation douce au-dela. Evite les distorsions en dents de scie
            # du hard clip quand plusieurs joueurs parlent fort en meme temps
            # ou quand un joueur prononce une voyelle ouverte (A, O) tres fort.
            # tanh(x) est borne en (-1, 1) par construction, donc pas besoin
            # de clip dur ensuite.
            np.tanh(mixed, out=mixed)
            outdata[:, 0] = mixed
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
                          is_radio: bool = False):
        """
        Alimente le buffer de lecture avec une trame audio recue via WebSocket.
        frame_bytes : raw PCM float32 mono, longueur = BLOCK_SIZE * 4
        is_radio    : True si la trame a deja ete filtree (effet radio PTT).
                      L'echo grotte est applique au niveau du MIX final de
                      proximite (voir _on_output_stream), pas par-trame :
                      les trames radio sont dans un mix separe et echappent
                      a l'echo, conformement a la regle "pas d'echo radio".
        """
        try:
            frame = np.frombuffer(frame_bytes, dtype=np.float32)
            if len(frame) == 0:
                return

            with self._lock:
                if sender_name not in self._remote_buffers:
                    self._remote_buffers[sender_name] = queue.Queue(maxsize=MAX_QUEUE_LEN)
                    # Volume par defaut 0 : pas audible tant qu'on ne le configure pas
                    self._remote_volumes[sender_name] = 0.0
                q = self._remote_buffers[sender_name]
                # Memoriser is_radio de la derniere trame (utilise au mix)
                self._remote_is_radio[sender_name] = is_radio

            # Anti-drift : si la file est pleine, on jette le plus ancien
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass

            self._frames_received += 1
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
          kind = "press"   -> bip aigu (880 Hz, 80ms)
          kind = "release" -> bip grave (440 Hz, 60ms)
        Le bip est mixe dans le flux de sortie audio par _on_output_block.
        Si un bip est deja en cours, le nouveau l'ecrase (pas de file d'attente).
        """
        with self._lock:
            if kind == "release":
                self._beep_buffer = self._beep_release
            else:
                self._beep_buffer = self._beep_press
            self._beep_idx = 0

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
