# -*- coding: utf-8 -*-
"""
circusvoip_core
================

Logique metier de CircusVOIP, sans interface graphique.

Ce module contient toutes les fonctions et classes utilisees par le
client (UI Qt) qui n'ont rien a voir avec le rendu graphique :

- Configuration (charger / sauver le JSON client)
- Etat global partage (classe State + instance state)
- WebSocket de controle (envoi de messages au serveur)
- Push-to-talk radio (pynput keyboard/mouse capture, RadioKeyListener)
- Game.log de Star Citizen (tail + parsing)
- Detection casque (helmet scan via OCR de la boussole)
- Boucles audio WebSocket
- Boucle OCR principale (lit la position SC en continu)

Le module s'utilise typiquement en :
    from circusvoip_core import (
        state, _load_client_cfg, _save_client_cfg, _ws_send_safe,
        _radio_listener, _ocr_loop_inner, ...
    )

Les fonctions ici sont 100% headless. Toute interaction avec l'UI passe
par un objet 'ui' qui expose des methodes set_status / refresh_players
/ etc., similaires a ce que faisait l'ancien ClientUI Tk.

Note historique : ce module est l'extraction des fonctions du fichier
historique circusvoip_client.py (Tk) qui restaient utilisables sans
modification depuis le port Qt. Le code est repris tel quel pour
preserver son comportement exact (peu de risque de regression).
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
from pathlib import Path

# numpy : utilise par _audio_ws_loop pour appliquer apply_radio_effect aux
# trames radio recues (PTT canal/profil). Sans cet import au scope module,
# np.frombuffer levait NameError dans le bloc try/except qui avalait
# silencieusement l'exception, et la trame radio etait jouee BRUTE (sans
# filtre) tout en etant routee vers mix_radio. Symptome observe (06/05/2026) :
# voix radio audible mais sans filtre radio, comme une voix de proximite a
# volume 1.0. Bug introduit lors du split legacy -> core (l'import numpy
# du legacy n'a pas ete report dans le core).
import numpy as np

# pynput (capture clavier/souris globale pour le PTT radio)
try:
    from pynput import keyboard as _pynput_kb
    from pynput import mouse as _pynput_mouse
    _KEYBOARD_AVAILABLE = True
except ImportError:
    _KEYBOARD_AVAILABLE = False

# WebSocket (asyncio)
try:
    import asyncio
    import websockets
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False



# ======================================================================
# Constantes
# ======================================================================

RADIUS_TRIGGER  = 5.0       # m : volume max (zone verte)
RADIUS_FADE     = 30.0      # m : debut du fondu (volume 0)
SCAN_INTERVAL   = 0.01      # secondes entre 2 lectures OCR
AUDIBLE_RANGE_M = 30.0      # m : limite d'audibilite proximity
SERVER_PORT     = 8888      # port WS serveur de controle
AUDIO_PORT      = 8889      # port WS serveur audio

# Delai de "linger" apres relachement du PTT radio (en millisecondes).
# Quand l'utilisateur relache la touche radio, on garde state.radio_active
# pendant ce delai supplementaire avant de couper. Ceci pour 2 raisons :
# 1. Les frames audio en transit dans la queue d'envoi (capture -> WebSocket)
#    au moment du release seraient sinon marquees "proximite" (flag 0x00) au
#    lieu de "radio" (0x01), donc les autres joueurs entendraient la fin de
#    phrase SANS l'effet radio (et avec attenuation distance).
# 2. C'est plus naturel : les vraies radios PTT ont toujours un petit "trail"
#    apres release, ca evite la coupure abrupte.
# 200ms est un compromis : assez long pour couvrir la queue audio (typiquement
# 60-120ms de jitter buffer), assez court pour ne pas etre genant entre 2 phrases.
RADIO_RELEASE_LINGER_MS = 200

# Chemin de base : on resout au repertoire de l'application appelante,
# pas du module circusvoip_core. Permet d'utiliser ce module sans coller
# son dossier au CWD de l'app appelante.
_BASE_DIR = Path(__file__).resolve().parent

# Fichier de configuration cote client. Le format est volontairement
# compatible avec l'ancienne version Tk : on lit/ecrit le meme fichier
# pour que la migration soit transparente.
CLIENT_CONFIG_FILE = _BASE_DIR / "circusvoip_client_config.json"



# ======================================================================
# Logging debug
# ======================================================================
# Le client maintient un fichier de log par session, nomme avec le pseudo
# du joueur quand il devient connu. Rotation FIFO sur _LOG_ARCHIVE_MAX
# fichiers pour eviter de saturer le disque.
#
# Usage typique :
#     _dbg_log("[INIT] demarrage")
#     _set_log_player_name("Toto")    # quand on connait le pseudo
#     _dbg_log("[WS] connecte")        # ecrit dans Toto_<timestamp>.log
#
# Mode metrics : _log_system_metrics() snapshot CPU/RAM/GPU.

DEBUG_OCR     = True
DEBUG_SCREENS = False
_DEBUG_DIR    = _BASE_DIR / "circusvoip_debug"

# Throttling des screenshots debug (par seconde)
_DEBUG_SCREEN_INTERVAL = 5.0
_last_dbg_save_ts      = 0.0

# Combien de logs on garde au max (rotation)
_LOG_ARCHIVE_MAX = 10

# Etat du logger (init paresseux)
_log_initialized = False
_log_filename    = "circusvoip_debug.log"
_log_player_name = None

def _sanitize_log_name(name: str) -> str:
    """Nettoie un nom de joueur pour en faire un nom de fichier valide."""
    if not name:
        return "Joueur"
    # Retirer les caracteres non-alphanumeriques (sauf _ et -)
    import re
    clean = re.sub(r'[^A-Za-z0-9_\-]', '_', name.strip())
    return clean or "Joueur"


def _make_log_filename(player_name: str | None) -> str:
    """Genere un nom de fichier de log unique pour cette session avec
    timestamp : circusvoip_debug_<Joueur>_YYYYMMDD_HHMMSS.log
    Si pas de joueur connu, utilise 'Joueur' generique."""
    safe_name = _sanitize_log_name(player_name)
    ts_str = time.strftime("%d%m%Y_%H%M%S")
    return f"circusvoip_debug_{safe_name}_{ts_str}.log"


def _rotate_old_logs(player_name: str | None = None):
    """Garde au maximum _LOG_ARCHIVE_MAX fichiers .log dans le dossier
    debug, TOUS PSEUDOS CONFONDUS. Les plus anciens (par mtime) sont
    supprimes. Appele au demarrage de chaque session.

    Le parametre player_name est conserve pour compatibilite mais ignore.
    Raison : le code precedent filtrait par pseudo, mais au tout premier
    _dbg_log() le pseudo etait encore None (= "Joueur"), la rotation ne
    matchait aucun fichier reel et ne supprimait rien. Resultat : les
    fichiers <pseudo>_*.log s'accumulaient indefiniment.

    En rotant globalement, on garantit la limite stricte : 10 fichiers
    max dans le dossier, point.
    """
    if not _DEBUG_DIR.exists():
        return
    try:
        logs = sorted(
            [p for p in _DEBUG_DIR.iterdir()
             if p.is_file()
             and p.name.startswith("circusvoip_debug_")
             and p.suffix == ".log"],
            key=lambda p: p.stat().st_mtime,
        )
        # On va creer un nouveau fichier juste apres : faire de la place
        # pour qu'apres ajout on soit a _LOG_ARCHIVE_MAX max.
        keep = max(0, _LOG_ARCHIVE_MAX - 1)
        excess = len(logs) - keep
        for p in logs[:max(0, excess)]:
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _set_log_player_name(name: str):
    """
    Associe un pseudo joueur au fichier de log.
    Appele des que state.my_name est connu.
    - Si aucun fichier n'a encore ete ecrit : memorise juste le nom pour
      que _dbg_log() genere le nom de fichier au premier appel.
    - Si un fichier a deja ete ecrit avec un nom generique : le renomme
      avec le pseudo + timestamp.
    """
    global _log_filename, _log_initialized, _log_player_name
    if name and name == _log_player_name:
        return
    if _log_initialized:
        # On a deja commence a ecrire : renommer le fichier existant pour
        # y incorporer le pseudo joueur.
        old_path = _DEBUG_DIR / _log_filename
        new_filename = _make_log_filename(name)
        new_path = _DEBUG_DIR / new_filename
        if old_path.exists() and old_path != new_path:
            try:
                if new_path.exists():
                    new_path.unlink()
                old_path.rename(new_path)
                _log_filename = new_filename
            except Exception:
                # Si echec, on continue avec l'ancien nom
                pass
    _log_player_name = name


def _dbg_log(msg: str):
    """Log texte debug   actif si DEBUG_OCR.
    Le fichier est cree au 1er appel avec un nom date unique.
    Les ecritures suivantes sont en append sur le meme fichier.
    """
    global _log_initialized, _log_filename
    if not DEBUG_OCR:
        return
    _DEBUG_DIR.mkdir(exist_ok=True)
    ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
    line = f"[{ts}] {msg}\n"
    print(line, end="")
    # Premiere ecriture de la session : creer le nom de fichier date,
    # purger les vieux logs en surplus, puis ecrire l'entete.
    if not _log_initialized:
        _log_filename = _make_log_filename(_log_player_name)
        _rotate_old_logs(_log_player_name)
    log_path = _DEBUG_DIR / _log_filename
    mode = "a" if _log_initialized else "w"
    with open(log_path, mode, encoding="utf-8") as f:
        if not _log_initialized:
            session_ts = time.strftime("%d/%m/%Y %H:%M:%S")
            f.write(f"=== Session demarree : {session_ts} ===\n")
            _log_initialized = True
        f.write(line)

# Etat persistant pour les metriques systeme (init paresseux pour eviter
# le cout de pynvml si jamais utilise)
_metrics_state = {
    "psutil_imported": None,   # None=pas tente, True=ok, False=indispo
    "nvml_imported": None,
    "nvml_handle": None,        # handle de la 1ere GPU NVIDIA
    "process": None,            # process psutil pour mesurer la conso de Circus
}


def _log_system_metrics(label: str = ""):
    """Log un snapshot des metriques systeme : CPU global, RAM, GPU NVIDIA
    si dispo (utilisation + VRAM), et la conso CPU/RAM/VRAM specifique du
    process CircusVOIP. Appele toutes les 30s depuis la boucle stats principale.

    label : etiquette optionnelle pour distinguer les snapshots particuliers
    (ex: "BASELINE" avant init OCR, "POST-OCR" apres init). Sans label,
    s'affiche juste comme [METRICS].

    Conception :
    - Aucune dependance bloquante : si psutil ou pynvml indisponible, on
      log juste les metriques qu'on peut sans crasher.
    - Coup d'execution typique : ~10ms (lectures rapides via pynvml + psutil).
    - Pas de thread separe : appel ponctuel toutes les 30s, surcharge minime.
    """
    parts = []                      # parts du systeme global (CPU, RAM, GPU)
    circus_str = None               # ligne specifique au process Circus (mise a la fin)

    # CPU + RAM globaux + RAM/CPU du process Circus
    if _metrics_state["psutil_imported"] is None:
        try:
            import psutil
            _metrics_state["psutil_imported"] = True
            _metrics_state["process"] = psutil.Process()
            # Premier appel a cpu_percent pour amorcer (sinon retourne 0)
            psutil.cpu_percent(interval=None)
            _metrics_state["process"].cpu_percent(interval=None)
        except Exception:
            _metrics_state["psutil_imported"] = False
    if _metrics_state["psutil_imported"]:
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            ram_used_gb = mem.used / (1024**3)
            ram_total_gb = mem.total / (1024**3)
            parts.append(f"CPU={cpu:.0f}%")
            parts.append(f"RAM={ram_used_gb:.1f}/{ram_total_gb:.1f}GB ({mem.percent:.0f}%)")
            # Conso process Circus (CPU + RAM, on ajoutera VRAM dans la
            # section pynvml plus bas si dispo)
            proc = _metrics_state["process"]
            if proc:
                proc_cpu = proc.cpu_percent(interval=None)
                proc_ram_mb = proc.memory_info().rss / (1024**2)
                # cpu_percent de psutil pour un seul process peut depasser 100%
                # si le process utilise plusieurs cores. On normalise par le
                # nombre de coeurs pour avoir un equivalent "pourcent du systeme".
                cpu_count = psutil.cpu_count() or 1
                circus_str = f"Circus={proc_cpu/cpu_count:.0f}%/{proc_ram_mb:.0f}MB"
        except Exception:
            pass

    # GPU NVIDIA (utilisation + VRAM)
    if _metrics_state["nvml_imported"] is None:
        try:
            import pynvml
            pynvml.nvmlInit()
            # Recuperer le handle de la GPU 0 (la premiere). Si plusieurs GPU,
            # on log uniquement la principale pour rester concis.
            count = pynvml.nvmlDeviceGetCount()
            if count > 0:
                _metrics_state["nvml_handle"] = pynvml.nvmlDeviceGetHandleByIndex(0)
                _metrics_state["nvml_imported"] = True
            else:
                _metrics_state["nvml_imported"] = False
        except Exception:
            _metrics_state["nvml_imported"] = False
    if _metrics_state["nvml_imported"] and _metrics_state["nvml_handle"]:
        try:
            import pynvml
            import os
            h = _metrics_state["nvml_handle"]
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            vram_used_gb = mem.used / (1024**3)
            vram_total_gb = mem.total / (1024**3)
            vram_pct = (mem.used / mem.total * 100) if mem.total else 0
            parts.append(f"GPU={util.gpu}%")
            parts.append(f"VRAM={vram_used_gb:.1f}/{vram_total_gb:.1f}GB ({vram_pct:.0f}%)")
            # VRAM specifique au process Circus : on parcourt les process
            # qui utilisent le GPU et on cherche notre PID.
            # NB : la liste contient compute (CUDA = ce que fait EasyOCR) et
            # graphics (rendu OpenGL/DX). Pour Circus, c'est uniquement compute.
            try:
                my_pid = os.getpid()
                circus_vram_mb = 0.0
                for proc in pynvml.nvmlDeviceGetComputeRunningProcesses(h):
                    if proc.pid == my_pid:
                        # usedGpuMemory en bytes, peut etre None sur certaines
                        # versions de driver (cas non admin sous Win)
                        if proc.usedGpuMemory:
                            circus_vram_mb = proc.usedGpuMemory / (1024**2)
                        break
                if circus_vram_mb > 0 and circus_str:
                    circus_str += f"+{circus_vram_mb:.0f}MB VRAM"
            except Exception:
                # nvmlDeviceGetComputeRunningProcesses peut echouer (driver,
                # permissions). Pas grave, on log les autres metriques.
                pass
        except Exception:
            pass

    # Assembler : metriques globales d'abord, conso Circus a la fin
    if circus_str:
        parts.append(circus_str)
    if parts:
        tag = f"[METRICS {label}]" if label else "[METRICS]"
        _dbg_log(tag + " " + " | ".join(parts))

# Cache : clef -> (last_ts, last_value) pour la deduplication des logs
_dbg_last: dict = {}

def _dbg_log_dedup(key: str, msg: str, value=None, min_interval: float = 5.0):
    """
    Log un message uniquement si :
      - le 'value' change par rapport au precedent pour cette cle, OU
      - 'min_interval' secondes se sont ecoulees depuis le dernier log
    Evite le spam de messages repetitifs.
    """
    if not DEBUG_OCR:
        return
    now = time.time()
    prev = _dbg_last.get(key)
    should_log = False
    if prev is None:
        should_log = True
    else:
        prev_ts, prev_val = prev
        if value is not None and value != prev_val:
            should_log = True
        elif (now - prev_ts) >= min_interval:
            should_log = True
    if should_log:
        _dbg_last[key] = (now, value)
        _dbg_log(msg)


# ======================================================================
# Configuration JSON
# ======================================================================
# Le fichier circusvoip_client_config.json stocke les preferences du
# joueur (touches assignees, dernier serveur connu, zone OCR calibree,
# etc.). Format JSON pour faciliter l'edition manuelle si besoin.

def _load_client_cfg():
    try:
        return json.loads(CLIENT_CONFIG_FILE.read_text())
    except Exception:
        return {}

def _save_client_cfg(cfg):
    CLIENT_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ======================================================================
# Etat global partage
# ======================================================================
# Singleton de l'etat partage entre les boucles (audio, OCR, WebSocket,
# radio, helmet, gamelog). Chaque module lit/ecrit directement les
# attributs de 'state'. Pas de getters/setters pour rester lightweight.

class State:
    my_pos      = None
    my_name     = "Joueur"
    players     = {}
    connected   = False
    ws          = None
    ws_loop     = None   # event loop du thread WebSocket (pour envoi thread-safe)
    zone_coords = None
    server_token = ""
    # [P4 - auth partagee] Ticket emis par le serveur positions dans le
    # message welcome. A renvoyer au serveur audio lors du join audio.
    # Le serveur audio refuse la connexion sans ce ticket. Rafraichi a
    # chaque welcome (donc a chaque reconnexion au serveur positions).
    audio_ticket = ""
    # Audio
    audio_io         = None   # instance AudioIO
    audio_ws         = None   # WebSocket audio
    audio_connected  = False
    audio_input_dev  = None
    audio_output_dev = None
    audio_muted      = False   # micro (capture) mute
    mute_proximity   = False   # ne plus entendre les autres en proximity
    mute_radio       = False   # ne plus entendre les autres en radio
    # Radio (push-to-talk)
    radio_key        = None   # nom de touche, ex: "v", "x", "ctrl"
    radio_active     = False  # True tant que la touche est enfoncee
    # Mute toggles : touches globales qui basculent les mute on/off a chaque pression
    # (differents du PTT radio qui est press/release)
    mute_mic_key     = None   # toggle micro (audio_muted)
    mute_prox_key    = None   # toggle ecoute proximity (mute_proximity)
    mute_radio_key   = None   # toggle ecoute radio (mute_radio)
    mute_all_key     = None   # toggle tout mute (active/desactive les 3 ci-dessus simultanement)
    # Proximite reduite : mode "chuchotement" qui reduit la portee audible
    # de la voip de proximite a 5m (100% volume jusqu'a 5m, 0% au-dela).
    # Utile pour les conversations discretes en jeu de role.
    proximity_short      = False  # False = 30m (defaut), True = 5m
    proximity_short_key  = None   # raccourci pour toggler le mode
    # Timestamps des dernieres trames radio recues par sender.
    # Permet d'ignorer les updates proximity sur un sender activement en radio
    # (sinon le volume oscille entre 1.0 et 0.0 selon la frequence des trames).
    radio_recv_ts    = {}     # {sender_name: timestamp}
    # ---- Mode RP ----
    # Active/desactive par le bouton MODE RP dans l'UI. Quand actif, la voix
    # de proximite passe par le filtre radio si au moins un des 2 (emetteur
    # ou recepteur) porte un casque. Filtre LOCAL (option A) : chaque joueur
    # decide de son experience, son reglage n'est pas impose aux autres.
    rp_mode          = False
    # Etat casque local. Defaut True (heuristique : la majorite des joueurs
    # portent un casque en jeu, donc en cas d'incertitude initiale on suppose
    # qu'ils en ont un - mieux que d'omettre le filtre RP au demarrage).
    # Alimente ensuite par 2 sources :
    #  1. Phase 1 (scan boussole HUD) au demarrage : confirme/infirme l'etat
    #  2. Tail Game.log : capture les changements (raccourci clavier casque)
    helmet_on        = True
    # Etats casque des autres joueurs, recus via les messages POS.
    # {player_name: bool}
    helmet_remote    = {}
    # Mode anonyme (controle par le serveur, broadcast a tous les clients).
    # Quand actif : on masque dans l'UI la zone, les coordonnees X Y Z et
    # la distance des autres joueurs. La VOIP positionnelle continue
    # normalement (les positions sont recues mais juste cachees a l'utilisateur).
    anonymous_mode   = False
    # Canaux radio (controles par le serveur).
    # - channels_list : liste des noms de canaux NORMAUX (filtres pour exclure
    #   les canaux-profils que le client ne doit pas pouvoir choisir librement)
    # - my_channel : canal sur lequel je suis (None si aucun)
    # - player_channels : {name: channel} pour les autres joueurs
    # Filtrage radio : a la reception d'une trame audio identifiee comme radio
    # (filtre actif = emetteur en train de PTT), on ignore si l'emetteur n'est
    # pas sur le meme canal que nous. La voix de proximite n'est PAS filtree.
    channels_list    = []
    my_channel       = None
    player_channels  = {}
    # Profils (tags assignes par l'admin, INDEPENDANTS des canaux).
    # - profiles_list : liste des noms de profils disponibles
    # - my_profile : profil qui m'est assigne par l'admin (None = aucun)
    # - player_profiles : {name: profile} pour les autres joueurs
    # Filtrage cote receveur : trame avec flag 0x02 (radio profil) jouee
    # uniquement si player_profiles[sender] == my_profile.
    profiles_list      = []
    my_profile         = None
    player_profiles    = {}
    # Mode 'chuchotement' (5m) des AUTRES joueurs. Recus via player_prox_short.
    # Si player_prox_short[sender] == True : on ne joue pas leur voix
    # de proximite au-dela de 5m (ils ont active le mode chuchotement).
    player_prox_short  = {}
    # Timestamps des dernieres trames RADIO recues par sender. Sert a
    # dedupliquer la trame proximity quand l'emetteur envoie 2 flux
    # simultanement (PTT radio + a portee proximite). Si on a recu la
    # radio dans les 50ms qui precedent, on jette la trame proximity du
    # meme sender (sinon on entendrait 2 fois sa voix : une avec effet
    # radio, une sans).
    # Differe de radio_recv_ts (seuil ~1s, sert a empecher la boucle
    # calcul-volume d'ecraser le volume radio).
    last_radio_seen_ts = {}
    # Touche du PTT profil (similaire a radio_key)
    profile_radio_key    = None
    profile_radio_active = False
    # Touche pour cycler les canaux (descente, boucle en haut)
    cycle_channel_key    = None
    # Overlays floating windows
    # - overlays_show : True quand le bouton "Overlay" est ON (overlays affiches)
    # - overlays_edit : True quand le bouton "Overlay Edition" est ON
    # - overlays_active : liste des overlays actuellement actives par l'utilisateur
    #   (charge depuis la config). Pour l'instant, seul "mutes" est dispo.
    # - overlays_config : {overlay_id: {"x": int, "y": int, "size": int}}
    #   Position et taille de chaque overlay (persistant dans config.json).
    overlays_show    = False
    overlays_edit    = False
    overlays_active  = []
    overlays_config  = {}
    # Etat du process Star Citizen (cible du tail Game.log).
    # True  = SC tourne et le tail est connecte (mis a True par
    #         _open_file dans le tail gamelog quand Game.log est ouvert)
    # False = SC pas lance OU le tail a perdu le fichier (jeu ferme,
    #         crash, changement LIVE/PTU sans bascule auto reussie)
    # Defaut False au boot : on attend que le tail confirme avant de
    # considerer SC comme actif. Sinon, si SC n'est pas lance au demarrage
    # du client, l'UI afficherait "En attente de position OCR..." au lieu
    # du plus informatif "Hors-jeu".
    sc_running       = False

state = State()


# ======================================================================
# Reseau : envoi WebSocket thread-safe
# ======================================================================

def _normalize_channels(raw):
    """Normalise la liste channels du serveur (str ou dicts) en liste de strings.
    Retro-compat : on accepte les anciens formats."""
    out = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            # Ancien format dict : on prend juste le nom (les profils-canaux
            # sont migres cote serveur, donc on ignore is_profile ici).
            if not item.get("is_profile", False):
                out.append(item["name"])
    return out


def _ws_send_safe(data: dict) -> bool:
    """
    Envoie un message JSON au serveur de positions depuis n'importe quel thread.
    Utilise run_coroutine_threadsafe sur le loop WS pour eviter les race conditions.
    Retourne True si l'envoi a ete planifie avec succes.
    """
    if not state.connected or state.ws is None or state.ws_loop is None:
        return False
    try:
        payload = json.dumps(data)
        # Planifier l'envoi dans le loop WS (thread-safe)
        fut = asyncio.run_coroutine_threadsafe(
            state.ws.send(payload), state.ws_loop
        )
        # Attente courte pour savoir si ca a marche (sans bloquer trop)
        try:
            fut.result(timeout=1.0)
            return True
        except Exception:
            return False
    except Exception:
        return False


# ======================================================================
# Capture clavier/souris globale (pynput)
# ======================================================================
# RadioKeyListener centralise toutes les touches globales : PTT radio,
# PTT profil, mute toggles, prox short, cycle channel. Un seul listener
# pour tout le clavier + un seul pour la souris. Les callbacks declenchent
# soit un changement d'etat dans 'state', soit appellent un toggle
# enregistre via set_toggle_callbacks().

def _normalize_pynput_key(key) -> str | None:
    """Transforme un event clavier pynput en chaine comparable stable entre
    capture et detection globale. Gere 3 cas :

    1. Touches avec caractere imprimable (ex: 'a', '1', '+') : retourne key.char en minuscules.
       Couvre : lettres, chiffres du range alpha, symboles. Pour les touches numpad
       avec VerrNum ON, pynput leur assigne aussi un char ('1', '2', '.', '+'...),
       MAIS c'est identique au char d'une touche alpha correspondante ('1' de la ligne
       chiffres et '1' du numpad donnent tous deux 'num1' via le code vk detecte plus bas).

    2. Touches numpad (pave numerique) : detecte via le Windows Virtual Key code
       (key.vk, range 0x60-0x6F) et retourne 'num0'..'num9', 'num.', 'num/', etc.
       Ca permet de distinguer le chiffre du clavier alpha et le chiffre du numpad
       (utile pour assigner un raccourci sur le numpad sans qu'il interfere avec
       la saisie normale). Prioritaire sur key.char.

    3. Touches speciales (Shift, F1..F12, Tab, etc.) : retourne key.name en minuscules.
    """
    # 1. Priorite : detecter numpad via vk (codes virtuels Windows 0x60-0x6F).
    # On verifie vk AVANT char pour que les touches numpad soient toujours
    # prefixees 'num' meme si VerrNum est actif (sinon 'num1' et '1' seraient
    # le meme raccourci, ce qui empeche de les differencier).
    try:
        vk = getattr(key, "vk", None)
        if vk is not None:
            numpad_map = {
                0x60: "num0", 0x61: "num1", 0x62: "num2", 0x63: "num3",
                0x64: "num4", 0x65: "num5", 0x66: "num6", 0x67: "num7",
                0x68: "num8", 0x69: "num9",
                0x6A: "num*", 0x6B: "num+", 0x6D: "num-",
                0x6E: "num.", 0x6F: "num/",
            }
            if vk in numpad_map:
                return numpad_map[vk]
    except Exception:
        pass
    # 2. Caractere imprimable classique
    try:
        ch = getattr(key, "char", None)
        if ch is not None:
            return ch.lower()
    except Exception:
        pass
    # 3. Touche speciale (shift, f1, tab, etc.)
    try:
        name = getattr(key, "name", None)
        if name:
            return name.lower()
    except Exception:
        pass
    return None


# ======================================================================
# Helpers pour les combinaisons de touches (raccourcis multi-touches)
# ======================================================================
#
# Format de stockage : string avec '+' separateur, modifieurs en premier
# dans un ordre canonique.
#   "m"                 -> simple touche (retro-compat avec l'ancien format)
#   "ctrl+m"            -> combo simple
#   "ctrl+shift+m"      -> multi-modifieurs
#   "ctrl+mouse:x1"     -> modifieurs + souris
#   "ctrl+a+s"          -> 2 touches normales (autorise mais rare)
#
# Cote runtime, on tracke _currently_pressed (clavier + souris) et au
# moment du press de la derniere touche non-modifieur on verifie si la
# combinaison entiere matche un raccourci configure.
#
# Note importante : ctrl_l et ctrl_r sont distincts dans pynput (idem
# shift_l/r, alt_l/alt_gr). Pour les COMBOS, on les normalise en "ctrl"
# (sans suffixe L/R) pour que "ctrl+m" matche que ce soit le ctrl
# gauche ou droit. Mais on garde "ctrl_l" valide comme touche unique
# (PTT seul) pour la retro-compat.

_COMBO_MODIFIER_KEYS = ("ctrl", "shift", "alt")
_COMBO_MODIFIER_ORDER = {"ctrl": 0, "shift": 1, "alt": 2}


def _strip_lr_suffix(key: str) -> str:
    """Retire le suffixe _l / _r / _gr pour les modifieurs.
    'ctrl_l' -> 'ctrl', 'shift_r' -> 'shift', 'alt_gr' -> 'alt'.
    Les autres touches sont retournees telles quelles.
    """
    if not key:
        return key
    k = key.lower()
    for mod in _COMBO_MODIFIER_KEYS:
        if k == f"{mod}_l" or k == f"{mod}_r":
            return mod
        if mod == "alt" and k == "alt_gr":
            return "alt"
    return k


def _canonicalize_combo(combo: str) -> str:
    """Normalise une combo : split sur '+', lowercase, dedup, modifieurs
    en premier dans l'ordre canonique (ctrl < shift < alt), reste en ordre
    alphabetique. Retire les suffixes L/R des modifieurs.

    Exemples :
      'M+CTRL'             -> 'ctrl+m'
      'shift+ctrl+m'       -> 'ctrl+shift+m'
      'ALT_L+f1'           -> 'alt+f1'
      'm'                  -> 'm'  (pas de modifieur)
      ''                   -> ''
    """
    if not combo:
        return ""
    parts_raw = [p.strip().lower() for p in combo.split("+") if p.strip()]
    # Pour chaque part, retirer suffixe L/R des modifieurs
    parts = []
    seen = set()
    for p in parts_raw:
        # Attention : pour les souris on garde le ':' (ex 'mouse:x1')
        if p.startswith("mouse:"):
            np = p
        else:
            np = _strip_lr_suffix(p)
        if np not in seen:
            seen.add(np)
            parts.append(np)
    if not parts:
        return ""
    # Tri : modifieurs d'abord (ctrl, shift, alt), puis le reste
    # (alphabetique, sauf que les modifieurs gardent leur ordre canonique).
    mods = [p for p in parts if p in _COMBO_MODIFIER_KEYS]
    others = [p for p in parts if p not in _COMBO_MODIFIER_KEYS]
    mods.sort(key=lambda m: _COMBO_MODIFIER_ORDER.get(m, 99))
    others.sort()
    return "+".join(mods + others)


def _combo_to_keys(combo: str) -> tuple[set[str], list[str]]:
    """Decoupe une combo canonique en (modifieurs_set, autres_list).
    Utilise pour le matching au runtime.
      'ctrl+shift+m' -> ({'ctrl', 'shift'}, ['m'])
      'm'            -> (set(), ['m'])
      ''             -> (set(), [])
    """
    canon = _canonicalize_combo(combo)
    if not canon:
        return set(), []
    parts = canon.split("+")
    mods = {p for p in parts if p in _COMBO_MODIFIER_KEYS}
    others = [p for p in parts if p not in _COMBO_MODIFIER_KEYS]
    return mods, others


def _pressed_normalized(currently_pressed: set) -> set[str]:
    """Normalise un set _currently_pressed (qui contient des 'ctrl_l'
    ou 'shift_r') en remplacant les modifieurs L/R par leur version
    sans suffixe. Utilise pour matcher contre les combos canoniques.
    """
    out = set()
    for k in currently_pressed:
        out.add(_strip_lr_suffix(k))
    return out


def _combo_matches_pressed(combo: str, currently_pressed: set,
                           trigger_key: str = None) -> bool:
    """Verifie si la combo configuree est satisfaite par l'etat courant
    des touches enfoncees.

    - combo : la configuration (ex: 'ctrl+m', 'ctrl+shift+m', 'm')
    - currently_pressed : set des touches actuellement enfoncees
      (au format brut, ex: {'ctrl_l', 'm'})
    - trigger_key : si fourni (str normalisee, ex: 'm'), on exige que
      cette touche soit l'une des touches non-modifieur de la combo.
      Sert a distinguer 'qui a declenche le press' : si l'utilisateur
      tient deja ctrl puis appuie M, on veut que la combo 'ctrl+m'
      matche au moment du press de M (pas du press de ctrl).

    Retourne True si :
      1. tous les modifieurs requis par la combo sont enfonces
      2. toutes les touches non-modifieur de la combo sont enfoncees
      3. (si trigger_key fourni) trigger_key est dans les touches
         non-modifieur de la combo
    """
    mods_req, others_req = _combo_to_keys(combo)
    if not mods_req and not others_req:
        return False
    pressed_norm = _pressed_normalized(currently_pressed)
    # 1. Les modifieurs requis doivent tous etre enfonces
    if not mods_req.issubset(pressed_norm):
        return False
    # 2. Les autres touches requises doivent toutes etre enfoncees
    for k in others_req:
        if k not in pressed_norm:
            return False
    # 3. Si trigger_key fourni : on exige qu'il soit dans others_req
    if trigger_key is not None:
        trigger_norm = _strip_lr_suffix(trigger_key)
        if trigger_norm not in others_req:
            return False
    return True


def _combo_is_simple(combo: str) -> bool:
    """True si la combo est une simple touche (pas de '+'), ex: 'm',
    'mouse:x1', 'num1'. Utile pour optimiser le matching dans les
    cas courants."""
    canon = _canonicalize_combo(combo)
    return bool(canon) and "+" not in canon


def canonicalize_hotkey(combo: str) -> str:
    """API publique pour l'UI : normalise une combo de raccourci avant
    de la stocker dans state.X_key. Garantit que le matching runtime
    et la sauvegarde config sont coherents.

    Idempotent : peut etre appele plusieurs fois sans effet.
    """
    return _canonicalize_combo(combo)


def format_hotkey_for_display(combo: str) -> str:
    """Formate une combo pour affichage UI : 'ctrl+m' -> 'Ctrl + M',
    'mouse:x1' -> 'Souris : X1', etc. Plus lisible que la forme brute."""
    if not combo:
        return "(aucun)"
    canon = _canonicalize_combo(combo)
    if not canon:
        return "(aucun)"
    parts = canon.split("+")
    pretty = []
    for p in parts:
        if p.startswith("mouse:"):
            btn = p.split(":", 1)[1].upper()
            pretty.append(f"Souris {btn}")
        elif p in _COMBO_MODIFIER_KEYS:
            pretty.append(p.capitalize())
        elif p.startswith("num"):
            pretty.append(f"Num{p[3:].upper()}" if len(p) > 3 else "Num")
        elif len(p) == 1:
            pretty.append(p.upper())
        else:
            pretty.append(p.replace("_", " ").title())
    return " + ".join(pretty)


class RadioKeyListener:
    """
    Ecoute globalement une touche clavier OU un bouton de souris pour declencher la radio.
    - state.radio_active = True tant que la touche/bouton est enfonce
    - La touche est stockee dans state.radio_key
    - Format : "a", "v", "ctrl" (clavier)  OU  "mouse:left", "mouse:x1", "mouse:x2" (souris)

    Gere aussi les TOGGLES de mute (audio_muted, mute_proximity, mute_radio) :
    - Declenches sur press (pas sur release) pour eviter le double-toggle
    - Les touches sont stockees dans state.mute_mic_key, mute_prox_key, mute_radio_key
    - Un debounce empeche les repetitions (hold de la touche = 1 seul toggle)
    - Les callbacks toggle_* sont enregistres par l'UI pour mettre a jour les boutons
    """
    def __init__(self):
        self._kb_listener    = None
        self._mouse_listener = None
        # Callbacks toggle : remplis par l'UI
        self._on_toggle_mic   = None
        self._on_toggle_prox  = None
        self._on_toggle_radio = None
        self._on_toggle_all   = None  # tout-mute
        self._on_toggle_prox_short = None  # toggle proximite reduite 30m/5m
        self._on_cycle_channel     = None  # cycle vers le canal suivant
        # Callbacks PTT profil : appeles au press/release de profile_radio_key.
        # Ils servent a faire un set_channel cote UI (sur le thread main).
        self._on_profile_radio_pressed  = None
        self._on_profile_radio_released = None
        # Debounce : set des touches actuellement enfoncees (pour eviter le repeat)
        self._currently_pressed = set()
        # Release linger : compteur de generation pour annuler les anciens
        # timers si l'utilisateur re-press la touche pendant le linger.
        # A chaque press, on incremente. Le timer differe verifie qu'il n'a
        # pas ete invalide avant de couper radio_active.
        self._release_gen = 0
        self._release_lock = threading.Lock()
        # Idem pour le PTT profil (independant du release_gen radio classique)
        self._profile_release_gen  = 0
        self._profile_release_lock = threading.Lock()

    def _on_radio_pressed(self):
        """Appele quand la touche/bouton radio est enfonce.

        Met radio_active=True immediatement. Si un timer release etait en
        cours (release linger), il est invalide (l'utilisateur a re-press
        avant la fin du linger -> on continue normalement)."""
        with self._release_lock:
            # Invalide tout timer release en cours
            self._release_gen += 1
        state.radio_active = True
        # Log pour debug : trace l'usage radio + le canal courant. Sans ce
        # log on n'a aucun moyen de savoir dans les sessions debug si la
        # touche radio a ete pressee, ni si le canal etait configure
        # correctement au moment du press.
        try:
            _dbg_log(
                f"[RADIO PTT] press canal={state.my_channel or '(aucun)'!r}"
            )
        except Exception:
            pass
        if state.audio_io is not None:
            try:
                state.audio_io.set_gate_force_open(True)
                state.audio_io.play_local_beep("press")
            except Exception:
                pass

    def _on_radio_released(self):
        """Appele quand la touche/bouton radio est relache.

        Au lieu de couper radio_active=False immediatement, on programme un
        timer differe (RADIO_RELEASE_LINGER_MS) qui le fera. Si l'utilisateur
        re-press la touche entre temps, le timer sera invalide (par increment
        de _release_gen)."""
        with self._release_lock:
            self._release_gen += 1
            my_gen = self._release_gen
        # Log pour debug : marque la fin du PTT.
        try:
            _dbg_log("[RADIO PTT] release")
        except Exception:
            pass
        # Forcer la fermeture immediate du gate. Sans ca, si l'utilisateur
        # continue a parler apres release (ex: parler aux gens dans la piece),
        # le gate reste ouvert tant que le RMS depasse le seuil de fermeture
        # -> les autres entendraient sa voix pendant 1-2s apres release.
        # force_gate_close() coupe net peu importe le RMS courant.
        if state.audio_io is not None:
            try:
                state.audio_io.set_gate_force_open(False)
                state.audio_io.force_gate_close()
                state.audio_io.play_local_beep("release")
            except Exception:
                pass
        # Flush de la queue d'envoi audio : jette les trames deja capturees
        # mais en attente d'envoi WebSocket. Sans ca, ces trames sortent
        # APRES le release avec le flag radio (0x01) toujours actif (queue
        # de 50 trames max = ~1s de buffer en pire cas), donc le receveur
        # entend la voix continuer 1-2s apres relachement. Bug observe le
        # 07/05/2026 par tester B en condition multi-joueurs reels.
        n_dropped = _flush_audio_send_queue()
        if n_dropped > 0:
            try:
                _dbg_log(f"[RADIO PTT] release flush: {n_dropped} trames jetees")
            except Exception:
                pass
        # Timer differe pour passer radio_active = False
        def _delayed_off():
            time.sleep(RADIO_RELEASE_LINGER_MS / 1000.0)
            with self._release_lock:
                # Si _release_gen a change pendant le sleep, c'est qu'un
                # nouveau press/release a eu lieu -> on ignore ce timer
                if my_gen != self._release_gen:
                    return
            state.radio_active = False
        threading.Thread(target=_delayed_off, daemon=True).start()

    def set_toggle_callbacks(self, on_mic=None, on_prox=None, on_radio=None,
                             on_all=None, on_prox_short=None,
                             on_cycle_channel=None,
                             on_profile_radio_pressed=None,
                             on_profile_radio_released=None):
        """Enregistre les callbacks de toggle (appeles depuis un thread non-UI).
        Les callbacks doivent etre thread-safe (utiliser root.after en interne)."""
        self._on_toggle_mic   = on_mic
        self._on_toggle_prox  = on_prox
        self._on_toggle_radio = on_radio
        self._on_toggle_all   = on_all
        self._on_toggle_prox_short = on_prox_short
        self._on_cycle_channel     = on_cycle_channel
        self._on_profile_radio_pressed  = on_profile_radio_pressed
        self._on_profile_radio_released = on_profile_radio_released

    # ---- PTT profil : meme logique que radio mais bascule aussi le canal ----
    # Au press : signale a l'UI (callback) qui va faire set_channel(my_profile)
    #            et active radio_active (pour que la trame audio sorte avec
    #            le flag binaire 0x01 = radio).
    # Au release : signale a l'UI qui restaure le canal precedent + coupe
    #              radio_active.
    # Pendant le press, profile_radio_active=True (sert a l'UI pour distinguer
    # un PTT profil d'un PTT radio classique - utile pour l'affichage).
    # Cote audio, c'est strictement equivalent a la radio classique : c'est
    # le serveur qui filtre par canal a la reception, et le canal courant
    # de l'emetteur a ete bascule sur son canal-profil.

    def _on_profile_radio_pressed_impl(self):
        """Press de la touche PTT profil : delegue le switch de canal a l'UI."""
        with self._profile_release_lock:
            self._profile_release_gen += 1
        state.profile_radio_active = True
        # Activer aussi radio_active pour que les trames audio soient flaggees
        # comme radio (0x01) - sinon elles seraient envoyees en proximity et
        # n'auraient pas l'effet radio.
        state.radio_active = True
        # Log pour debug : trace l'usage PTT profil + le profil courant.
        try:
            _dbg_log(
                f"[RADIO PTT-PROFIL] press profil={state.my_profile or '(aucun)'!r}"
            )
        except Exception:
            pass
        if state.audio_io is not None:
            try:
                state.audio_io.set_gate_force_open(True)
                state.audio_io.play_local_beep("press")
            except Exception:
                pass
        # Notifier l'UI pour qu'elle fasse le switch de canal
        cb = self._on_profile_radio_pressed
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _on_profile_radio_released_impl(self):
        """Release du PTT profil : restaure le canal precedent + coupe radio."""
        with self._profile_release_lock:
            self._profile_release_gen += 1
            my_gen = self._profile_release_gen
        # Log pour debug : marque la fin du PTT profil.
        try:
            _dbg_log("[RADIO PTT-PROFIL] release")
        except Exception:
            pass
        # Forcer la fermeture immediate du gate (meme raison que pour
        # le PTT radio classique : eviter de capter la voix pendant 1-2s
        # apres release si l'utilisateur continue de parler).
        if state.audio_io is not None:
            try:
                state.audio_io.set_gate_force_open(False)
                state.audio_io.force_gate_close()
                state.audio_io.play_local_beep("release")
            except Exception:
                pass
        # Flush de la queue d'envoi audio : meme bug que PTT radio classique
        # (cf commentaire dans _on_release_impl). Les trames buffer sortent
        # avec le flag profil (0x02) apres release sans ce flush.
        n_dropped = _flush_audio_send_queue()
        if n_dropped > 0:
            try:
                _dbg_log(f"[RADIO PTT-PROFIL] release flush: {n_dropped} trames jetees")
            except Exception:
                pass
        def _delayed_off():
            time.sleep(RADIO_RELEASE_LINGER_MS / 1000.0)
            with self._profile_release_lock:
                if my_gen != self._profile_release_gen:
                    return
            state.profile_radio_active = False
            # Couper radio_active SAUF si la touche/combo PTT radio
            # classique est actuellement satisfaite (l'utilisateur tient
            # encore le PTT radio en plus du PTT profil qu'il vient de
            # relacher) : dans ce cas, on laisse radio_active=True pour
            # ne pas couper sa voix.
            rk = state.radio_key or ""
            if rk and _combo_matches_pressed(rk, self._currently_pressed):
                # L'utilisateur tient aussi le PTT classique -> ne pas couper
                pass
            else:
                state.radio_active = False
        threading.Thread(target=_delayed_off, daemon=True).start()
        # Notifier l'UI pour restaurer le canal precedent (immediat, pas linger)
        cb = self._on_profile_radio_released
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _normalize_key(self, key):
        """Transforme un event pynput clavier en chaine comparable.
        Delegue a la fonction module-level _normalize_pynput_key pour que la
        capture (popup) et la detection (listener) utilisent exactement la
        meme logique -- critique pour que les touches numpad soient detectees
        correctement."""
        return _normalize_pynput_key(key)

    @staticmethod
    def _normalize_button(button):
        """Transforme un Button pynput en chaine 'mouse:xxx'."""
        try:
            return f"mouse:{button.name.lower()}"
        except Exception:
            return None

    def _check_toggles(self, key_str):
        """Verifie si la touche pressee finalise une combo de toggle mute
        et l'active. Appele sur press (debouncee) uniquement.

        Le matching des combos repose sur _currently_pressed (qui inclut
        deja la touche key_str au moment de l'appel) + key_str comme
        trigger_key (la derniere touche ajoutee). Cette logique evite
        de re-declencher le toggle a chaque modifieur enfonce : seul le
        press de la touche non-modifieur (ex: 'm' dans 'ctrl+shift+m')
        valide la combo.
        """
        if not key_str:
            return
        # Mic
        mk = state.mute_mic_key or ""
        if mk and self._on_toggle_mic and \
                _combo_matches_pressed(mk, self._currently_pressed, key_str):
            try: self._on_toggle_mic()
            except Exception: pass
        # Proximity
        pk = state.mute_prox_key or ""
        if pk and self._on_toggle_prox and \
                _combo_matches_pressed(pk, self._currently_pressed, key_str):
            try: self._on_toggle_prox()
            except Exception: pass
        # Radio
        rk = state.mute_radio_key or ""
        if rk and self._on_toggle_radio and \
                _combo_matches_pressed(rk, self._currently_pressed, key_str):
            try: self._on_toggle_radio()
            except Exception: pass
        # Tout mute (active/desactive simultanement les 3 mutes ci-dessus)
        ak = state.mute_all_key or ""
        if ak and self._on_toggle_all and \
                _combo_matches_pressed(ak, self._currently_pressed, key_str):
            try: self._on_toggle_all()
            except Exception: pass
        # Proximite reduite (toggle 30m / 5m)
        pxk = state.proximity_short_key or ""
        if pxk and self._on_toggle_prox_short and \
                _combo_matches_pressed(pxk, self._currently_pressed, key_str):
            try: self._on_toggle_prox_short()
            except Exception: pass
        # Cycle canal radio
        cck = state.cycle_channel_key or ""
        if cck and self._on_cycle_channel and \
                _combo_matches_pressed(cck, self._currently_pressed, key_str):
            try: self._on_cycle_channel()
            except Exception: pass

    def _check_ptt_press(self, key_str):
        """Verifie si une combo PTT (radio classique ou profil) vient
        d'etre completee par le press de key_str. Si oui, declenche le
        callback correspondant. Idempotent : verifie state.radio_active
        pour ne pas re-presser si deja actif (cas combo + repeat).
        """
        if not key_str:
            return
        # PTT radio classique
        rk = state.radio_key or ""
        if rk and _combo_matches_pressed(rk, self._currently_pressed, key_str):
            if not state.radio_active:
                self._on_radio_pressed()
        # PTT profil
        pk = state.profile_radio_key or ""
        if pk and _combo_matches_pressed(pk, self._currently_pressed, key_str):
            if not state.profile_radio_active:
                self._on_profile_radio_pressed_impl()

    def _check_ptt_release(self, key_str):
        """Verifie si une combo PTT vient d'etre rompue par le release
        de key_str. Si oui (la combo n'est plus satisfaite), declenche
        le release. La verification utilise _combo_matches_pressed sans
        trigger_key : on regarde juste si toutes les touches sont
        toujours la.
        """
        if not key_str:
            return
        # PTT radio classique : si actif et combo plus satisfaite -> release
        rk = state.radio_key or ""
        if rk and state.radio_active and \
                not _combo_matches_pressed(rk, self._currently_pressed):
            # Toutefois, NE PAS declencher le release si profile_radio_active
            # est aussi True : la radio reste active tant que le PTT profil
            # tient (cf logique existante dans _on_profile_radio_released_impl).
            if not state.profile_radio_active:
                self._on_radio_released()
            else:
                # PTT radio classique relache mais PTT profil encore actif :
                # on laisse radio_active=True (le release du profil le coupera).
                pass
        # PTT profil : si actif et combo plus satisfaite -> release
        pk = state.profile_radio_key or ""
        if pk and state.profile_radio_active and \
                not _combo_matches_pressed(pk, self._currently_pressed):
            self._on_profile_radio_released_impl()

    def _on_press(self, key):
        n = self._normalize_key(key)
        if not n:
            return
        # IMPORTANT : ajouter dans _currently_pressed AVANT de checker
        # les combos. _check_ptt_press et _check_toggles utilisent ce
        # set pour determiner quelles touches sont enfoncees.
        already_pressed = n in self._currently_pressed
        self._currently_pressed.add(n)
        # PTT (radio + profil) : check meme si already_pressed (pour
        # gerer les key repeat OS, mais _check_ptt_press est idempotent
        # via le check state.radio_active).
        self._check_ptt_press(n)
        # Toggles mute : declencher une seule fois par pression (debounce).
        # On ne veut pas double-toggle si l'OS envoie key repeat.
        if not already_pressed:
            self._check_toggles(n)

    def _on_release(self, key):
        n = self._normalize_key(key)
        if not n:
            return
        # Retirer du debounce set AVANT de checker les combos PTT pour
        # que _check_ptt_release voit l'etat post-release.
        self._currently_pressed.discard(n)
        self._check_ptt_release(n)

    def _on_click(self, x, y, button, pressed):
        n = self._normalize_button(button)
        if not n:
            return
        if pressed:
            already_pressed = n in self._currently_pressed
            self._currently_pressed.add(n)
            self._check_ptt_press(n)
            if not already_pressed:
                self._check_toggles(n)
        else:
            self._currently_pressed.discard(n)
            self._check_ptt_release(n)

    def start(self):
        if not _KEYBOARD_AVAILABLE:
            return
        # Clavier
        if self._kb_listener is None:
            self._kb_listener = _pynput_kb.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            self._kb_listener.daemon = True
            self._kb_listener.start()
        # Souris
        if self._mouse_listener is None:
            self._mouse_listener = _pynput_mouse.Listener(
                on_click=self._on_click,
            )
            self._mouse_listener.daemon = True
            self._mouse_listener.start()

    def stop(self):
        for lst_attr in ("_kb_listener", "_mouse_listener"):
            lst = getattr(self, lst_attr)
            if lst:
                try: lst.stop()
                except Exception: pass
                setattr(self, lst_attr, None)


_radio_listener = RadioKeyListener()

def _build_gamelog_paths():
    subpaths = [
        r"Program Files\Roberts Space Industries\StarCitizen",
        r"Roberts Space Industries\StarCitizen",
        r"Star Citizen\StarCitizen",
        r"Games\StarCitizen",
        r"StarCitizen",
    ]
    versions = ["LIVE", "4.0_PREVIEW", "EPTU", "PTU", "TECH-PREVIEW"]
    drives = ["C:", "D:", "E:", "F:", "G:", "H:"]
    paths = []
    for d in drives:
        for s in subpaths:
            for v in versions:
                paths.append(fr"{d}\{s}\{v}\Game.log")
    return paths

_GAMELOG_POSSIBLE_PATHS = _build_gamelog_paths()

def _find_gamelog() -> str | None:
    """Recherche Game.log en 3 niveaux, dans l'ordre :
    1. Chemin sauve dans la config client (priorite absolue si valide)
    2. Detection via le processus StarCitizen.exe s'il tourne (psutil) :
       garantit qu'on prend le bon Game.log si plusieurs versions sont
       installees (LIVE + PTU + EPTU). Le process actif fait foi.
    3. Liste de chemins habituels codee en dur (150 combinaisons) : fallback
       si SC ne tourne pas encore au lancement du client.
    Renvoie None si aucun niveau ne trouve."""
    import os

    # Niveau 1 : chemin sauve dans la config client
    try:
        cfg = _load_client_cfg()
        saved = cfg.get("gamelog_path")
        if saved and os.path.exists(saved):
            return saved
    except Exception:
        pass

    # Niveau 2 : si SC.exe tourne, deduire le chemin depuis son executable.
    # Prioritaire sur la liste de chemins codes : si le joueur a installe
    # LIVE et PTU et qu'il joue actuellement sur PTU, on doit surveiller
    # le Game.log de PTU et pas celui de LIVE (qui existerait toujours
    # mais serait inactif).
    # Import soft de psutil (dependance optionnelle) : si psutil absent,
    # on skip ce niveau sans erreur.
    try:
        import psutil
        from pathlib import Path as _P
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name == "starcitizen.exe":
                    exe = proc.info.get("exe")
                    if not exe:
                        continue
                    # StarCitizen.exe est dans .../<VERSION>/Bin64/
                    # Remonter 2 niveaux donne .../<VERSION>/
                    live_dir = _P(exe).parent.parent
                    game_log = live_dir / "Game.log"
                    if game_log.exists():
                        return str(game_log)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass
    except Exception:
        pass

    # Niveau 3 : chemins habituels codes en dur (fallback si SC pas lance)
    for p in _GAMELOG_POSSIBLE_PATHS:
        if os.path.exists(p):
            return p

    return None


# Regex de detection event casque.
# Pattern observe dans SC 4.x :
#   [Notice] <AttachmentReceived> Player[nom] Attachment[<id>, <nom>, ...]
#           Status[persistent] Port[<port>] Elapsed[...]
# Le Port[Armor_Helmet] indique casque sur la tete, tout autre port = retire.
# On filtre sur le nom d'attachment qui doit contenir "helmet".
_GAMELOG_HELMET_RE = re.compile(
    r"<AttachmentReceived>\s+"
    r"Player\[(?P<player>[^\]]+)\]\s+"
    r"Attachment\[(?P<att>[^\]]+)\]\s+"
    r"Status\[(?P<status>[^\]]+)\]\s+"
    r"Port\[(?P<port>[^\]]+)\]"
)

def _gamelog_tail_loop(ui: "ClientUI"):
    """Thread qui tail Game.log pour detecter l'etat du casque.
    Met a jour state.helmet_on et envoie un message WS pour que les
    autres clients soient informes.

    Resilient : si Game.log introuvable ou devient illisible, le thread
    dort 30s et re-tente. Ne bloque jamais le reste du client."""
    import os

    log_path = None
    last_check_time = 0.0

    while not getattr(ui, "_closing", False):
        # Recherche periodique du fichier Game.log (utile si SC n'est pas
        # encore lance ou si l'utilisateur l'installe pendant la session)
        if log_path is None or not os.path.exists(log_path):
            now = time.time()
            if now - last_check_time > 30.0:
                log_path = _find_gamelog()
                last_check_time = now
            if log_path is None:
                time.sleep(5.0)
                continue
            _dbg_log(f"[GAMELOG] fichier trouve : {log_path}")

        try:
            # Ouvrir et se placer en fin de fichier (ignore l'historique).
            # Memoriser la taille initiale pour detecter les rotations/reset
            # du fichier (SC reecrit Game.log au demarrage).
            initial_size = os.path.getsize(log_path)
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, 2)   # fin de fichier
                while not getattr(ui, "_closing", False):
                    line = f.readline()
                    if not line:
                        # Pas de nouvelle ligne : attendre un peu
                        time.sleep(0.2)
                        # Detecter si le fichier a ete tronque (SC a redemarre)
                        try:
                            cur_size = os.path.getsize(log_path)
                            if cur_size < initial_size:
                                _dbg_log("[GAMELOG] fichier tronque, reprise en cours")
                                # SC a redemarre : reset etat casque a defaut True.
                                # Si un scan rapide est en cours on le stoppe ;
                                # le prochain toggle Mode RP relancera un scan.
                                state.helmet_on = True
                                _helmet_scan.active = False
                                _dbg_log("[HELMET SCAN] Reset suite redemarrage SC")
                                break   # rouvrir le fichier
                            initial_size = max(initial_size, cur_size)
                        except OSError:
                            # Fichier supprime ou inaccessible
                            log_path = None
                            break
                        continue
                    # Parser la ligne
                    _process_gamelog_line(line, ui)
        except Exception as e:
            _dbg_log(f"[GAMELOG] erreur lecture : {e}")
            time.sleep(5.0)


def _process_gamelog_line(line: str, ui: "ClientUI"):
    """Analyse une ligne de Game.log et met a jour l'etat casque si besoin."""
    m = _GAMELOG_HELMET_RE.search(line)
    if not m:
        return
    att = m.group("att").lower()
    # Filtrer sur casque uniquement (pas autres pieces d'armure)
    if "helmet" not in att:
        return
    # Ignorer le visor qui est toujours sur helmet_visor (pas un indicateur
    # d'etat on/off du casque)
    port = m.group("port")
    if att.startswith("fp_visor") or "visor" in port.lower():
        return
    # Armor_Helmet = casque sur la tete, tout autre port = retire
    new_state = (port == "Armor_Helmet")
    # Dedup : n'emit que si changement reel d'etat
    if new_state == state.helmet_on:
        return
    state.helmet_on = new_state
    status = "ON" if new_state else "OFF"
    _dbg_log(f"[HELMET] casque {status} (port={port})")
    # Game.log a confirme l'etat casque : on peut stopper le scan rapide
    # en cours (Game.log fait foi, plus besoin de scan visuel).
    if _helmet_scan.active:
        _helmet_scan.active = False
        _dbg_log("[HELMET SCAN] Scan rapide stoppe (Game.log a confirme l'etat)")
    # Informer l'UI pour affichage visuel (label/couleur)
    try:
        ui.update_helmet_state(new_state)
    except Exception:
        pass
    # Envoyer l'info au serveur pour que les autres clients soient au courant.
    # Le serveur relaie aux autres via un message "helmet" type dedie.
    _ws_send_safe({"type": "helmet", "helmet_on": new_state})
    # Recalculer le filtrage RP local (mon casque change -> potentiellement
    # plus ou moins de voix a filtrer radio)
    _update_rp_filter()





# ======================================================================
# Detection casque par scan visuel de la boussole HUD
# ======================================================================
# Au lieu de se baser uniquement sur Game.log (qui ne reflete pas toujours
# l'etat reel : les events <AttachmentReceived> ne sont pas systematiques),
# on confirme/infirme par un scan visuel de la zone boussole pendant 5s.
# Vote majoritaire des lectures.

# Lazy import cv2 (utilise par _scan_helmet_compass)
_cv2_helmet = None
def _get_cv2_for_helmet():
    global _cv2_helmet
    if _cv2_helmet is None:
        import cv2 as _c
        _cv2_helmet = _c
    return _cv2_helmet

# Capture region + get_screen_resolution : on les importe depuis sc_ocr.
# _capture_region utilise le wrapper _capture_with_backoff qui gere les
# echecs Windows (BitBlt: Acces refuse, cause non identifiee) avec sleep
# adaptatif et log dedup. Sans ca, les appels en boucle (helmet scan
# toutes les 200ms, OCR a 4/s) saturaient le log d'erreurs identiques.
def _capture_region(region):
    """Wrapper sur circusvoip_sc_ocr._capture_with_backoff."""
    from circusvoip_sc_ocr import _capture_with_backoff as _cr
    return _cr(region)

def _get_screen_resolution():
    """Wrapper sur circusvoip_sc_ocr.get_screen_resolution."""
    from circusvoip_sc_ocr import get_screen_resolution as _gsr
    return _gsr()


class _HelmetScanState:
    """Etat du scan boussole pour detecter si le joueur porte un casque.

    Le scan est declenche a chaque activation du Mode RP : il dure 5 secondes
    pendant lesquelles on lit la zone boussole HUD plusieurs fois et on
    applique un vote majoritaire pour decider si le casque est present.

    Avant : scan continu de 60s au demarrage uniquement (peu fiable car on ne
    sait pas a quel moment le joueur a son casque ouvert/visible). Maintenant :
    scan court declenche par l'utilisateur quand il entre en Mode RP, ce qui
    garantit que SC est en jeu et que la boussole est dans son etat normal.
    """
    # True quand le scan rapide est en cours
    active = False
    # Timestamp de demarrage du scan
    started_at = 0.0
    # Duree du scan rapide (en secondes)
    scan_duration = 5.0
    # Intervalle entre 2 lectures (en secondes) -> ~25 lectures sur 5s
    scan_interval = 0.2
    # Compteurs du scan en cours (reset a chaque _start_helmet_scan_quick)
    n_detected = 0
    n_total    = 0

_helmet_scan = _HelmetScanState()



def _scan_helmet_compass() -> bool | None:
    """Scan de la zone boussole en haut-centre de l'ecran ou tourne SC.
    Retourne True si la boussole est detectee (presence pixels clairs),
    False si zone noire/sombre, None si erreur de capture."""
    region = _get_helmet_scan_region()
    if region is None:
        return None
    try:
        img = _capture_region(region)
    except Exception:
        return None
    cv2 = _get_cv2_for_helmet()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Seuil : pixels > 100/255 = clairs. Boussole detectee si > 0.5%.
    bright_pixels = (gray > 100).sum()
    total_pixels  = gray.size
    ratio = bright_pixels / max(total_pixels, 1)
    return ratio > 0.005


def _get_helmet_scan_region() -> dict | None:
    """Retourne la zone qui sera scannee (utile pour debug/affichage).
    Calcule a partir de la zone OCR coords + resolution ecran."""
    z = state.zone_coords
    if not z:
        return None
    sc_width = z["left"] + z["width"]
    try:
        sw, sh = _get_screen_resolution()
        if sw == sc_width:
            sc_height = sh
        else:
            sc_height = int(sc_width * 9 / 16)
    except Exception:
        sc_height = int(sc_width * 9 / 16)
    sc_width_hud = int(sc_height * 16 / 9)
    sc_width_hud = min(sc_width_hud, sc_width)
    region_w = int(sc_width_hud * 0.30)
    region_h = int(sc_height * 0.05)
    region_x = (sc_width - region_w) // 2
    region_y = z["top"] + int(sc_height * 0.01)
    return {
        "left":   region_x,
        "top":    region_y,
        "width":  region_w,
        "height": region_h,
    }


def _start_helmet_scan_quick(ui=None):
    """Declenche un scan boussole de 5s. Resette les compteurs et marque
    le scan comme actif. Le thread _helmet_scan_loop fait le reste.

    Si un scan est deja en cours, on le redemarre a zero (cas rare : double
    clic rapide sur Mode RP).

    Le resultat (helmet_on True/False) est applique a la fin des 5s par vote
    majoritaire et broadcast au serveur."""
    _helmet_scan.active = True
    _helmet_scan.started_at = time.time()
    _helmet_scan.n_detected = 0
    _helmet_scan.n_total = 0
    _dbg_log(
        f"[HELMET SCAN] Demarrage scan rapide ({_helmet_scan.scan_duration}s, "
        f"intervalle {_helmet_scan.scan_interval}s)"
    )


def _helmet_scan_loop(ui: "ClientUI"):
    """Thread qui execute les scans boussole quand `_helmet_scan.active=True`.

    A chaque activation par `_start_helmet_scan_quick()`, on fait des lectures
    visuelles toutes les `scan_interval` secondes pendant `scan_duration`
    secondes, on accumule les votes (boussole detectee oui/non), puis on
    applique le resultat par vote majoritaire et on l'envoie au serveur.

    Quand `active=False`, le thread dort et n'execute aucune lecture.
    Cela permet d'eviter toute consommation CPU/GPU quand le Mode RP n'est
    pas active.
    """
    while not getattr(ui, "_closing", False):
        time.sleep(_helmet_scan.scan_interval)
        if not _helmet_scan.active:
            continue

        # Verifier le timeout : 5s ecoulees -> conclure et arreter
        elapsed = time.time() - _helmet_scan.started_at
        if elapsed > _helmet_scan.scan_duration:
            _helmet_scan.active = False
            n_det = _helmet_scan.n_detected
            n_tot = _helmet_scan.n_total
            if n_tot == 0:
                # Aucune lecture reussie (probablement zone capture echouee).
                # On garde l'etat helmet_on actuel (pas de changement).
                _dbg_log(
                    f"[HELMET SCAN] Aucune lecture valide en {_helmet_scan.scan_duration}s, "
                    f"helmet_on={state.helmet_on} inchange"
                )
                continue
            # Vote majoritaire : >50% des lectures = casque detecte
            ratio = n_det / n_tot
            new_state = ratio > 0.5
            _dbg_log(
                f"[HELMET SCAN] Termine : {n_det}/{n_tot} lectures avec boussole "
                f"({100*ratio:.0f}%) -> casque {'ON' if new_state else 'OFF'}"
            )
            # Appliquer le nouvel etat si different
            if new_state != state.helmet_on:
                state.helmet_on = new_state
                _ws_send_safe({"type": "helmet", "helmet_on": new_state})
                _update_rp_filter()
                try:
                    ui.update_helmet_state(new_state)
                except Exception:
                    pass
            continue

        # Faire une lecture
        result = _scan_helmet_compass()
        if result is None:
            continue   # erreur de capture (zone vide, ecran eteint, etc.)
        _helmet_scan.n_total += 1
        if result:
            _helmet_scan.n_detected += 1




# ======================================================================
# Filtre Mode RP (radio forcee selon casques)
# ======================================================================

def _update_rp_filter():
    """Recalcule le forcage radio sur chaque sender selon le Mode RP local
    et les etats de casques. Regle : pour chaque autre joueur, on active
    le filtre radio si :
      - Mon Mode RP est actif
      - ET (moi OU le sender) porte un casque
    Sinon, le filtre est desactive (voix de proximite normale).

    Appele a chaque changement de state.rp_mode, state.helmet_on, ou
    state.helmet_remote[sender]. Pas de cout si l'audio_io n'est pas pret.
    """
    if state.audio_io is None:
        return
    if not state.rp_mode:
        # Mode RP desactive : reset complet du forcage, toutes les voix
        # repassent en proximite normale
        try:
            state.audio_io.clear_force_radio()
        except Exception:
            pass
        return
    # Mode RP actif : parcourir chaque sender connu
    my_helmet = bool(state.helmet_on)
    for sender, remote_helmet in state.helmet_remote.items():
        force = my_helmet or bool(remote_helmet)
        try:
            state.audio_io.set_force_radio(sender, force)
        except Exception:
            pass



# ======================================================================
# Audio WebSocket (streaming + envoi)
# ======================================================================

# ---------------------------------------------

# Queue thread-safe utilisee pour passer les trames audio captures au thread
# asyncio. L'envoi WebSocket doit se faire dans la boucle asyncio, pas dans
# le callback sounddevice (qui tourne dans un thread audio realtime).
_audio_send_queue: "queue.Queue[bytes]" = None


def _flush_audio_send_queue() -> int:
    """Vide la queue d'envoi audio. Retourne le nombre de trames jetees.

    Utilise au release PTT radio/profil pour eviter le bug observe le
    07/05/2026 : la queue (maxsize=50) accumulait jusqu'a ~1s de trames
    audio (50 trames * 20ms = 1s) en cas de reseau lent ou de ralentissement
    de l'envoi WS. En PTT mode radio, ces trames etaient envoyees au
    serveur APRES le release avec le flag radio (0x01), donc le receveur
    entendait la voix continuer pendant 1-2 secondes apres relachement
    du bouton.

    Avec ce flush au release : les trames deja capturees mais non envoyees
    sont jetees immediatement. Le force_gate_close() couplé bloque deja la
    capture des futures trames, donc le silence est immediat cote receveur.

    Note : ce flush ne casse pas les trames deja effectivement envoyees au
    serveur (celles-la sont definitivement parties), il jette uniquement
    celles en attente locale.
    """
    if _audio_send_queue is None:
        return 0
    n_dropped = 0
    while not _audio_send_queue.empty():
        try:
            _audio_send_queue.get_nowait()
            n_dropped += 1
        except Exception:
            break
    return n_dropped


async def _audio_sender(ws):
    """Task asyncio : sort les trames de la queue et les envoie sur le WS."""
    global _audio_send_queue
    loop = asyncio.get_event_loop()
    while True:
        try:
            # run_in_executor pour que queue.get() ne bloque pas la boucle
            data = await loop.run_in_executor(None, _audio_send_queue.get)
        except Exception:
            return
        if data is None:
            # signal de fin
            return
        try:
            await ws.send(data)
        except Exception:
            return

async def _audio_ws_loop(ui):
    """
    Se connecte au serveur audio et gere l'echange de trames.
    - Reception : chaque message binaire est prefixe du nom de l'emetteur.
    - Envoi : un task dedie consomme la queue alimentee par la capture.
    """
    global _audio_send_queue
    import queue as _q
    _audio_send_queue = _q.Queue(maxsize=50)

    while True:
        ip = getattr(state, "audio_server_ip", None)
        if not ip or state.audio_io is None:
            await asyncio.sleep(1)
            continue

        # Vider la queue des vieilles trames d'une precedente connexion
        while not _audio_send_queue.empty():
            try:
                _audio_send_queue.get_nowait()
            except Exception:
                break

        # [P1 - TLS] Connexion CHIFFREE en wss://.
        # Le serveur audio utilise un certificat auto-signe (genere
        # automatiquement au demarrage du serveur). build_client_ssl_context_insecure
        # construit un contexte SSL qui accepte ce certificat sans
        # verifier l'identite du serveur : connexion chiffree mais pas
        # d'authentification stricte. C'est acceptable ici car l'auth
        # se fait via le token + le ticket dans le join (cf [P4]).
        from circusvoip_security import build_client_ssl_context_insecure
        uri = f"wss://{ip}:{AUDIO_PORT}"
        _ssl_ctx = build_client_ssl_context_insecure()
        auth_failed = False
        try:
            async with websockets.connect(uri, ssl=_ssl_ctx, max_size=2*1024*1024) as ws:
                state.audio_ws        = ws
                state.audio_connected = True
                ui.set_audio_status(True, "")
                await ws.send(json.dumps({
                    "type": "join",
                    "name": state.my_name,
                    "token": state.server_token,
                    # [P4 - auth partagee] Ticket emis par le serveur
                    # positions (recu dans le welcome). Sans ce ticket, ou
                    # avec un ticket expire, le serveur audio refuse la
                    # connexion (reason="invalid_ticket").
                    "audio_ticket": getattr(state, "audio_ticket", "") or "",
                }))

                # Lancer le task d'envoi en parallele de la reception
                sender_task = asyncio.create_task(_audio_sender(ws))

                try:
                    async for msg in ws:
                        if isinstance(msg, bytes):
                            if len(msg) < 3:
                                continue
                            name_len = int.from_bytes(msg[:2], "big")
                            if len(msg) < 2 + name_len:
                                continue
                            sender = msg[2:2+name_len].decode("utf-8", errors="replace")
                            payload = msg[2+name_len:]
                            if len(payload) < 1:
                                continue
                            # 1er byte = flag de mode :
                            #   0x00 = proximity (sans PTT)
                            #   0x01 = radio classique (PTT canal)
                            #   0x02 = radio profil (PTT profil)
                            flag = payload[0]
                            is_radio_canal  = (flag == 1)
                            is_radio_profil = (flag == 2)
                            is_radio = is_radio_canal or is_radio_profil  # alias pour audio_io
                            frame    = payload[1:]

                            # Mute radio : coupe les 2 modes radio
                            if is_radio and state.mute_radio:
                                continue
                            # Mute proximity : coupe seulement les trames proximity
                            if (not is_radio) and state.mute_proximity:
                                continue

                            # FILTRAGE selon le mode :
                            if is_radio_canal:
                                # Filtre par CANAL : meme canal sinon on jette
                                sender_ch = state.player_channels.get(sender)
                                if state.my_channel != sender_ch:
                                    continue
                                # Match : noter le timestamp pour dedup proximity
                                state.last_radio_seen_ts[sender] = time.monotonic()
                            elif is_radio_profil:
                                # Filtre par PROFIL : meme profil sinon on jette
                                # Note : si le sender n'a pas de profil, ou si moi
                                # je n'en ai pas, on n'entend rien. Faut le meme.
                                sender_prof = state.player_profiles.get(sender)
                                if not sender_prof or sender_prof != state.my_profile:
                                    continue
                                state.last_radio_seen_ts[sender] = time.monotonic()
                            else:
                                # Proximity : dedup si on a recu une radio (canal
                                # ou profil) du meme sender dans les 50ms qui precedent.
                                last_radio = state.last_radio_seen_ts.get(sender, 0)
                                if (time.monotonic() - last_radio) < 0.05:
                                    continue

                            if state.audio_io is not None:
                                if is_radio:
                                    # Noter que ce sender est en radio active
                                    # pour que le canal proximity n'ecrase pas son volume
                                    state.radio_recv_ts[sender] = time.monotonic()
                                    # Radio : volume max, court-circuit la proximity
                                    state.audio_io.set_user_volume(sender, 1.0)
                                    # Appliquer effet radio (passe-bande + soft clip)
                                    # Si l'application du filtre echoue, on logge :
                                    # avant ce changement, le except avalait
                                    # silencieusement les erreurs, ce qui a masque
                                    # pendant des semaines un NameError "np not
                                    # defined" (import numpy oublie au split
                                    # legacy -> core). Resultat : voix radio jouee
                                    # brute, sans filtre. Maintenant si ca recasse,
                                    # le log de debug capturera le traceback.
                                    try:
                                        from circusvoip_audio_io import apply_radio_effect
                                        arr = np.frombuffer(frame, dtype=np.float32).copy()
                                        arr = apply_radio_effect(arr, sender)
                                        frame = arr.tobytes()
                                    except Exception as _radio_err:
                                        try:
                                            _dbg_log(
                                                f"[RADIO FILTER FAIL] sender={sender} : "
                                                f"{type(_radio_err).__name__}: {_radio_err}"
                                            )
                                        except Exception:
                                            pass
                                state.audio_io.feed_remote_frame(sender, frame, is_radio=is_radio)
                        else:
                            # Messages JSON (controle)
                            try:
                                data = json.loads(msg)
                            except Exception:
                                continue
                            if data.get("type") == "error":
                                reason = data.get("reason")
                                if reason == "invalid_token":
                                    auth_failed = True
                                    ui.set_audio_status(False, "Mot de passe invalide")
                                    break
                                if reason == "invalid_ticket":
                                    # [P4] Ticket absent / expire / invalide.
                                    # On NE retente PAS en boucle ici : il
                                    # faut d'abord se reconnecter au serveur
                                    # positions pour obtenir un ticket frais.
                                    auth_failed = True
                                    ui.set_audio_status(
                                        False,
                                        "Reconnecte-toi au serveur principal"
                                    )
                                    try:
                                        _dbg_log(
                                            "[AUDIO] Ticket refuse par le "
                                            "serveur audio : reconnexion au "
                                            "serveur positions necessaire"
                                        )
                                    except Exception:
                                        pass
                                    break
                finally:
                    sender_task.cancel()
                    try:
                        await sender_task
                    except Exception:
                        pass
        except websockets.exceptions.ConnectionClosedError as e:
            # Code 1008 = connexion refusee par le serveur (token OU ticket).
            if getattr(e, "code", None) == 1008:
                auth_failed = True
                # [P4] Distinguer token invalide et ticket invalide pour
                # afficher un message utile. Le serveur audio met la raison
                # dans le champ 'reason' de la fermeture.
                close_reason = (getattr(e, "reason", "") or "")
                if close_reason == "invalid_ticket":
                    ui.set_audio_status(
                        False, "Reconnecte-toi au serveur principal"
                    )
                elif close_reason == "banned":
                    ui.set_audio_status(
                        False, "Trop de tentatives - reessaie plus tard"
                    )
                else:
                    ui.set_audio_status(False, "Mot de passe invalide")
            else:
                ui.set_audio_status(False, f"Deconnecte ({e.code})")
        except Exception as e:
            ui.set_audio_status(False, str(e)[:40])

        state.audio_ws        = None
        state.audio_connected = False
        if auth_failed:
            # Ne pas retenter automatiquement si le token est refuse
            return
        await asyncio.sleep(3)

def _run_audio_ws(ui):
    asyncio.run(_audio_ws_loop(ui))


def _has_player_in_range() -> bool:
    """Retourne True si au moins un autre joueur est a portee proximite
    (selon le mode 30m/5m courant). Utilise pour decider si on envoie un
    2e flux proximity en plus du flux radio quand on est en PTT radio.
    Si on est seul a portee, pas la peine de doubler la bande passante."""
    if not state.players:
        return False
    max_range = 5.0 if state.proximity_short else AUDIBLE_RANGE_M
    for info in state.players.values():
        if not info.get("sc_online", True):
            continue
        dist = info.get("dist")
        if dist is not None and dist <= max_range:
            return True
    return False

def _on_audio_captured(frame_np):
    """
    Callback sounddevice : appele pour chaque bloc audio capture.
    On se contente de deposer la trame dans la queue ; l'envoi WS est
    fait par le task _audio_sender dans sa propre boucle asyncio.

    3 modes de transmission selon les PTT actifs :
      - PTT profil (state.profile_radio_active)  : flag 0x02 (radio profil)
      - PTT radio  (state.radio_active sans profil) : flag 0x01 (radio canal)
      - Aucun                                       : flag 0x00 (proximity)

    Quand on est en PTT radio OU PTT profil, on envoie en plus une trame
    proximity (0x00) en parallele SI un joueur est a portee. Ca permet aux
    joueurs a cote (mais pas sur le canal/profil) d'entendre ma voix en
    proximite. Le receveur depulique via state.last_radio_seen_ts (50ms).
    Optimisation : pas de 2e flux si personne a portee.
    """
    if not state.audio_connected:
        return
    if _audio_send_queue is None:
        return
    try:
        frame_bytes = frame_np.tobytes()

        def _put(data):
            if _audio_send_queue.full():
                try:
                    _audio_send_queue.get_nowait()
                except Exception:
                    pass
            _audio_send_queue.put_nowait(data)

        if state.profile_radio_active:
            # PTT profil : flag 0x02 + (eventuellement) proximity
            _put(b"\x02" + frame_bytes)
            if _has_player_in_range():
                _put(b"\x00" + frame_bytes)
        elif state.radio_active:
            # PTT radio classique : flag 0x01 + (eventuellement) proximity
            _put(b"\x01" + frame_bytes)
            if _has_player_in_range():
                _put(b"\x00" + frame_bytes)
        else:
            # Pas de PTT : juste proximity
            _put(b"\x00" + frame_bytes)
    except Exception:
        pass


# ======================================================================
# Heartbeat (evite timeout WS serveur)
# ======================================================================

def _heartbeat_loop(ui: "ClientUI"):
    """Envoie un ping WebSocket toutes les 10s si connecte."""
    try:
        while True:
            time.sleep(10)
            _ws_send_safe({"type": "ping"})
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        print(f"[HEARTBEAT CRASH] {e}", flush=True)
        print(tb_str, flush=True)
        try:
            _dbg_log(f"[HEARTBEAT CRASH] {e}")
            for line in tb_str.rstrip().split("\n"):
                _dbg_log(f"  {line}")
        except Exception:
            pass


# ---------------------------------------------
#  Thread safety : coupe le son des joueurs qui ne doivent pas etre audibles
# ---------------------------------------------



# ======================================================================
# OCR loop : import des helpers depuis circusvoip_sc_ocr
# ======================================================================
# La boucle OCR principale (_ocr_loop_inner) utilise plusieurs fonctions
# du module circusvoip_sc_ocr : read_coords (capture+OCR), distance
# (euclidien 3D), _apply_sign_memory (correction signe), _is_sign_flip
# (validation), _are_containers_similar, _is_cave_container.
# On les importe ici pour les avoir au niveau module.

from circusvoip_sc_ocr import (
    read_coords,
    distance,
    compute_proximity_volume,
    auto_ocr_zone,
    list_monitors,
    _apply_sign_memory,
    _is_sign_flip,
    _are_containers_similar,
    _is_cave_container,
)

# Le flag _minus_was_restored est lu par la boucle pour savoir si la
# detection visuelle de tirets a corrige une lecture (auquel cas le
# filtre sign-flip est court-circuite). Il est defini dans sc_ocr
# (mis a jour par _restore_minus_signs).
import circusvoip_sc_ocr as _sco_mod

# Brancher le logger de sc_ocr sur notre _dbg_log pour que les messages
# [OCR INIT], [COORDS], [SIGN MEMORY], etc. apparaissent dans le fichier
# de log unifie (au lieu de partir dans le lambda silencieux par defaut).
try:
    _sco_mod.set_logger(_dbg_log)
except Exception:
    pass

# Brancher aussi le hook de metriques systeme : les appels BASELINE et
# POST-OCR autour de l'init EasyOCR amorcent psutil et donnent un point
# de comparaison de la conso CPU/RAM/VRAM avant/apres init OCR. Sans ce
# branchement, le 1er [METRICS] de la boucle stats (T+30s) afficherait
# CPU=0% car l'amorcage paresseux et la 1ere mesure se feraient dans le
# meme appel a quelques ms d'ecart (fenetre cpu_percent trop courte).
try:
    _sco_mod.set_log_system_metrics(_log_system_metrics)
except Exception:
    pass

# Proxy d'acces : la lecture directe de _sco_mod._minus_was_restored
# fonctionne parce que c'est une variable de module simple.
def _get_minus_was_restored():
    return getattr(_sco_mod, "_minus_was_restored", False)


# ---------------------------------------------
#  Thread OCR
# ---------------------------------------------

# Timestamp (time.monotonic()) de la derniere iteration de la boucle OCR.
# Mis a jour a chaque tour par _ocr_loop_inner, lu par _ocr_watchdog_loop.
# 0 = pas encore demarre, ne pas declencher de warning.
_ocr_last_tick = 0.0

def _ocr_watchdog_loop(ui: "ClientUI", restart_callback=None):
    """
    Surveille que la boucle OCR continue de tourner.

    Si aucune iteration n'a eu lieu pendant WATCHDOG_TIMEOUT secondes alors
    que la boucle OCR est censee etre active (zone configuree, session
    demarree), on logue un warning. Si l'inactivite depasse RESTART_TIMEOUT
    et qu'un restart_callback est fourni, on le declenche pour respawner
    le thread OCR.

    Raison d'etre : capturer les crashs silencieux du thread OCR qui NE SONT
    PAS des exceptions Python et donc echappent a _ocr_loop's try/except.
    Exemples :
      - Segfault natif dans torch/CUDA/easyocr (tue le thread sans exception)
      - Freeze GPU (driver NVIDIA timeout, OOM CUDA non-reporte)
      - Deadlock sur un lock interne (rare mais possible)

    Observe : tester A (4K 16:9), session 22/04/2026 ~22:25, l'OCR s'arrete sans
    [OCR LOOP CRASH] dans le log. Le watchdog aurait permis de dater l'arret.

    MAJ 06/05/2026 : tester B (2K 21:9 ultrawide), session 17:46:24, freeze
    silencieux de l'OCR pendant 3 minutes (jusqu'a ce que le joueur quitte
    et relance le client). Le watchdog detectait bien (16.8s) mais ne
    redemarrait pas. Ajout du restart_callback pour respawner le thread OCR
    apres RESTART_TIMEOUT secondes d'inactivite confirmee. Cooldown
    RESTART_COOLDOWN entre 2 tentatives pour eviter le boucle infinie de
    redemarrage si le hardware est fondamentalement instable.
    """
    WATCHDOG_TIMEOUT = 15.0   # log un warning apres 15s sans tick
    RESTART_TIMEOUT  = 30.0   # tente un respawn apres 30s sans tick
    RESTART_COOLDOWN = 60.0   # min entre 2 tentatives de respawn
    CHECK_INTERVAL   = 5.0    # frequence de verification
    # Etat interne pour ne logger qu'une seule fois par episode d'inactivite
    # (au passage actif -> inactif), pas en continu tant que l'OCR est mort.
    already_warned   = False
    last_restart_ts  = 0.0    # monotonic du dernier respawn declenche

    try:
        while True:
            time.sleep(CHECK_INTERVAL)

            # Si la zone OCR n'est pas configuree, la boucle OCR ne tick pas
            # volontairement (elle attend en sleep(3) ligne par ligne).
            # Dans ce cas on ne peut pas considerer ca comme un probleme.
            if not state.zone_coords:
                already_warned = False
                continue

            # Si _ocr_last_tick est encore 0, la boucle n'a pas encore demarre
            # (thread lance mais en sleep(1) initial). Ignorer.
            if _ocr_last_tick <= 0:
                continue

            silence = time.monotonic() - _ocr_last_tick

            if silence > WATCHDOG_TIMEOUT:
                if not already_warned:
                    _dbg_log(
                        f"[OCR WATCHDOG] Aucune activite OCR depuis {silence:.1f}s "
                        f"(seuil={WATCHDOG_TIMEOUT}s). Le thread OCR est probablement "
                        f"fige (crash natif torch/CUDA, freeze GPU, ou deadlock). "
                        f"Aucune exception Python capturee -> crash silencieux."
                    )
                    already_warned = True
                # Si le silence depasse RESTART_TIMEOUT et qu'on a un callback
                # de respawn ET que le cooldown est ecoule depuis le dernier
                # respawn -> on tente de relancer le thread OCR.
                if (silence > RESTART_TIMEOUT
                        and restart_callback is not None
                        and (time.monotonic() - last_restart_ts) > RESTART_COOLDOWN):
                    _dbg_log(
                        f"[OCR WATCHDOG] Silence > {RESTART_TIMEOUT}s, "
                        f"tentative de respawn du thread OCR..."
                    )
                    try:
                        restart_callback()
                        last_restart_ts = time.monotonic()
                        _dbg_log("[OCR WATCHDOG] Respawn OCR demande au client.")
                    except Exception as _e:
                        _dbg_log(f"[OCR WATCHDOG] Respawn echoue : {_e}")
                # Continuer a checker : si l'OCR repart, on le note aussi
            else:
                if already_warned:
                    _dbg_log(
                        f"[OCR WATCHDOG] OCR a repris (silence etait {silence:.1f}s)"
                    )
                    already_warned = False
                    # Le gel OCR (gel CUDA typiquement) bloque aussi les
                    # callbacks sounddevice : apres la reprise, l'audio est
                    # souvent figee. On declenche une relance des streams
                    # 2s apres pour laisser le systeme se stabiliser.
                    # Solution retenue (vs watchdog audio independant) car
                    # elle evite les faux positifs : un utilisateur qui mute
                    # son micro aurait declenche un watchdog independant, ici
                    # on relance uniquement apres un evenement OCR confirme.
                    def _delayed_audio_restart():
                        time.sleep(2.0)
                        if state.audio_io is not None:
                            try:
                                restarted = state.audio_io.restart_streams()
                                if restarted:
                                    _dbg_log(
                                        f"[AUDIO RECOVERY] streams relances apres gel OCR : "
                                        f"{', '.join(restarted)}"
                                    )
                            except Exception as e:
                                _dbg_log(f"[AUDIO RECOVERY] erreur : {e}")
                    threading.Thread(target=_delayed_audio_restart, daemon=True).start()
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        print(f"[OCR WATCHDOG CRASH] {e}", flush=True)
        print(tb_str, flush=True)
        try:
            _dbg_log(f"[OCR WATCHDOG CRASH] {e}")
            for line in tb_str.rstrip().split("\n"):
                _dbg_log(f"  {line}")
        except Exception:
            pass


# Seuil de "position perimee" pour _volume_safety_loop. Si un joueur n'a pas
# envoye de position depuis ce nombre de secondes, on coupe son volume meme
# si son sc_online est encore True. Couvre le cas observe (06/05/2026, tester B
# 2K 21:9) ou l'OCR a freeze 3 minutes : sans signal sc_offline, l'autre
# joueur restait audible avec sa derniere position connue, hors-zone et hors
# de portee. Choix de 10s : > 2x cadence OCR normale (1-2 lectures/s) pour
# tolerer les variations, mais assez court pour couper rapidement quand un
# joueur disparait reellement.
POS_STALE_TIMEOUT = 10.0

# Intervalle minimum entre 2 logs [SIGN MEMORY APPLY] identiques. Quand l'OCR
# rate le '-' d'un axe en boucle (cas typique : Pyro avec petit HUD ou tester B
# 2K 21:9 ultrawide), la memoire de signe corrige correctement chaque frame
# mais loggue chaque correction. Sur 5 minutes ca remplit le log debug avec
# 300+ messages identiques. Avec ce dedup : log la 1ere occurrence d'une
# valeur de correction, puis re-log seulement toutes les N secondes si elle
# continue. Une nouvelle valeur est loggee immediatement (cle de dedup
# differente), donc on garde la reactivite en debug.
SIGN_APPLY_LOG_INTERVAL = 30.0

# Etat interne de dedup pour les logs SIGN MEMORY APPLY.
# Cle = signature des corrections (axe+valeur arrondie), valeur = monotonic ts
# du dernier log. Ce dict croit doucement (1 entree par signature unique),
# bornee en pratique par le nombre de containers x axes : negligeable.
_sign_apply_log_state: dict[str, float] = {}


def _volume_safety_loop(ui: "ClientUI"):
    """
    Tourne toutes les secondes et force volume=0 pour tout joueur qui :
    - est sc_offline (SC ferme chez lui)
    - n'a pas de position connue (jamais en jeu, ou en menu/frontend)
    - n'a pas de distance calculee (en attente de notre 1er OCR)
    - a une position perimee (pas d'update depuis POS_STALE_TIMEOUT secondes :
      OCR freeze chez l'autre, deconnexion silencieuse, etc.)

    Evite le cas ou un joueur reste audible avec une position obsolete apres
    un freeze OCR ou apres une deconnexion qui n'a pas declenche sc_offline.
    Ne touche PAS au volume si le joueur est en radio active dans la derniere
    seconde (la radio gere son propre volume).

    Restauration du legacy 06/05/2026 : ce thread existait dans le legacy
    Tk et a ete oublie lors du split en core/client. Resultat : sans cette
    safety loop, un joueur dont l'OCR freeze (cas tester B observe) restait
    audible a sa derniere position pendant tout le freeze.
    """
    try:
        while True:
            time.sleep(1.0)
            try:
                if state.audio_io is None:
                    continue
                now_mono = time.monotonic()
                for name, info in list(state.players.items()):
                    # Ne pas toucher si radio active dans la derniere seconde
                    last_radio = state.radio_recv_ts.get(name, 0)
                    if (now_mono - last_radio) < 1.0:
                        continue
                    # Pas en jeu chez l'autre (sc_offline) -> volume 0
                    if not info.get("sc_online", True):
                        state.audio_io.set_user_volume(name, 0.0)
                        continue
                    # Pas de position connue -> volume 0
                    if info.get("pos") is None:
                        state.audio_io.set_user_volume(name, 0.0)
                        continue
                    # Position connue mais pas encore de distance calculee
                    # (on attend que la boucle OCR principale la calcule)
                    if info.get("dist") is None:
                        state.audio_io.set_user_volume(name, 0.0)
                        continue
                    # Position perimee : pas d'update depuis POS_STALE_TIMEOUT.
                    # Couvre le cas freeze OCR chez l'autre joueur (pas de
                    # signal sc_offline mais plus de positions envoyees).
                    pos_ts = info.get("pos_received_ts", 0.0)
                    if pos_ts > 0 and (now_mono - pos_ts) > POS_STALE_TIMEOUT:
                        state.audio_io.set_user_volume(name, 0.0)
                        # Log une fois par episode (flag sur info pour eviter
                        # spam log a chaque seconde du loop)
                        if not info.get("_stale_logged", False):
                            try:
                                _dbg_log(
                                    f"[VOLUME SAFETY] position {name} perimee "
                                    f"({(now_mono - pos_ts):.1f}s sans update), "
                                    f"volume coupe"
                                )
                            except Exception:
                                pass
                            info["_stale_logged"] = True
                        continue
                    # Reset le flag de log si la position est a nouveau fraiche
                    if info.get("_stale_logged"):
                        info["_stale_logged"] = False
                        try:
                            _dbg_log(f"[VOLUME SAFETY] position {name} a repris")
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception as e:
        # Crash hors du try interne (ex: erreur sur time.sleep ou systeme)
        import traceback
        tb_str = traceback.format_exc()
        print(f"[VOLUME SAFETY CRASH] {e}", flush=True)
        print(tb_str, flush=True)
        try:
            _dbg_log(f"[VOLUME SAFETY CRASH] {e}")
            for line in tb_str.rstrip().split("\n"):
                _dbg_log(f"  {line}")
        except Exception:
            pass


def _ocr_loop(ui: "ClientUI"):
    try:
        _ocr_loop_inner(ui)
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        # Log console (stdout) : visible dans l'invite cmd pendant l'execution
        print(f"[OCR LOOP CRASH] {e}", flush=True)
        print(tb_str, flush=True)
        # Log fichier : capture la trace complete pour analyse post-mortem.
        # AVANT ce fix, le traceback partait uniquement sur stderr de l'invite
        # cmd et etait perdu si l'utilisateur fermait la fenetre avant de le
        # copier-coller. Ex observe : tester A (4K 16:9) ~22:25 (session du 22/04/2026),
        # log s'arrete brutalement, aucune trace dans le fichier alors que
        # l'erreur etait visible dans l'invite de commande.
        try:
            _dbg_log(f"[OCR LOOP CRASH] {e}")
            for line in tb_str.rstrip().split("\n"):
                _dbg_log(f"  {line}")
        except Exception:
            pass

def _ocr_loop_inner(ui: "ClientUI"):
    time.sleep(1)
    last_pos     = None
    MAX_JUMP     = 500
    reject_count = 0
    _pending_pos = None

    # Compteur de sign-flips consecutifs : si on en a plusieurs d'affilee
    # vers la meme valeur (signe oppose a last_pos mais identique entre eux),
    # c'est que c'est last_pos qui etait faux. On bascule au bout de N.
    # Voir bug du Perseus : tu es a y=+30 reel, OCR ajoute parfois un '-'
    # parasite -> last_pos="(-30)" -> tous les retours a "y=+30" rejetes.
    # Le vote permet de re-basculer vers la vraie valeur.
    _sign_flip_pending     = None  # pos candidate pour bascule
    _sign_flip_consec      = 0     # nb de sign-flips consecutifs identiques
    SIGN_FLIP_VOTE_TARGET  = 3     # nb de votes avant bascule

    # Mecanisme de convergence du filtre AXIS_MAX_JUMP. Sans ca, le filtre
    # peut bloquer indefiniment si last_pos est faux (ex : parsing OCR
    # foireux + sign memory inversee a tort, observe sur Skywat 07/05/2026
    # qui est reste 1 minute avec position figee fausse car tous les retours
    # corrects depassaient AXIS_MAX_JUMP=35m).
    # Logique : si AXIS_JUMP_CONVERGE_TARGET frames consecutives convergent
    # vers une nouvelle zone (proches entre elles, < AXIS_JUMP_CONVERGE_MAX_M),
    # on bascule sur cette zone, on accepte que last_pos etait faux.
    _axis_jump_pending     = None  # pos candidate pour bascule
    _axis_jump_consec      = 0     # nb de frames convergentes consecutives
    AXIS_JUMP_CONVERGE_TARGET = 3  # nb de frames cohérentes avant bascule
    AXIS_JUMP_CONVERGE_MAX_M  = 50.0  # distance max entre frames pour considerer "coherent"

    # Detection SC ferme : si pas de parse OK depuis 60s, on notifie le serveur
    # Mais uniquement si on a DEJA eu une position valide (sinon le joueur
    # vient juste de lancer SC et est encore au menu/chargement)
    SC_OFFLINE_TIMEOUT = 60.0
    last_parse_ok_time = time.time()
    sc_online          = True   # on considere SC actif au demarrage
    has_ever_had_pos   = False  # True apres la 1ere position valide

    # Stats : comptage reel des lectures OCR
    stats_t0       = time.time()
    stats_tried    = 0    # lectures tentees
    stats_parsed   = 0    # lectures avec position valide
    stats_rejected = 0    # rejetees par MAX_JUMP
    stats_cid_similar = 0 # corrections de container_id par _are_containers_similar
                          # (chiffres OCR confondus type 3 vs 8 sur l'id numerique)

    # Backoff CPU hors-jeu :
    # - Quand SC n'est pas en jeu (launcher, login, menu charge mais hors session),
    #   l'OCR tournait en continu a ~15 tentatives/s sans rien trouver, consommant
    #   ~45% CPU pour rien. On detecte 2 etats :
    #   1) SC explicitement absent (state.sc_running = False, mis a jour par le
    #      tail gamelog) -> sleep long (2s) entre chaque tentative
    #   2) SC actif mais OCR ne trouve rien depuis N tentatives consecutives
    #      (loading screen, transitions, menu cargaison/map ouvertes en
    #      surimpression) -> sleep progressif jusqu'a 500ms entre tentatives
    # Des qu'un parse reussit, le compteur est reset et la cadence reprend sans
    # delai (~4/s en jeu standard).
    SC_OFFLINE_SLEEP_S       = 2.0   # sleep entre tentatives si SC pas lance
    OCR_EMPTY_BACKOFF_STAGES = (
        # (nb de vides consecutifs, sleep en secondes)
        (10,  0.1),   # apres 10 vides : 100ms entre frames (cadence ~10/s -> ~6/s)
        (30,  0.3),   # apres 30 vides : 300ms (~3/s)
        (60,  0.5),   # apres 60 vides : 500ms (~2/s)
    )
    consecutive_empty = 0  # nb de tentatives consecutives sans parse OK

    while True:
        # Signaler au watchdog qu'on est toujours vivant. A chaque tour, meme
        # avant le read_coords : si l'OCR freeze pendant un read, le watchdog
        # verra que _ocr_last_tick n'avance plus et logguera.
        global _ocr_last_tick
        _ocr_last_tick = time.monotonic()

        zone = state.zone_coords
        if not zone:
            # Envoyer un ping pour garder la connexion active
            _ws_send_safe({"type": "ping"})
            time.sleep(3)
            continue

        # Backoff CPU si SC n'est pas en jeu : ne pas tourner a fond a vide
        # pendant le launcher / login / menu principal de SC. state.sc_running
        # est mis a False par le gamelog tail quand SC ferme ou est au
        # launcher. On garde quand meme un read_coords periodique : ca permet
        # de detecter le retour en jeu (apres un nouveau spawn par exemple)
        # sans attendre que le gamelog tail le signale.
        if not state.sc_running:
            time.sleep(SC_OFFLINE_SLEEP_S)
            # Ping pour garder la WS active malgre l'inactivite OCR.
            _ws_send_safe({"type": "ping"})
            # On continue quand meme pour faire un read_coords (qui peut
            # detecter un retour en jeu via OCR si le gamelog est lent).
            # consecutive_empty n'est PAS incremente ici : c'est specifique
            # a "OCR a tente mais rien trouve", pas "OCR pas tente".

        pos = read_coords(zone)
        stats_tried += 1

        # Backoff CPU sur OCR vide en serie : si l'OCR ne trouve rien pendant
        # plusieurs tentatives consecutives (loading screen, menu cargaison
        # ouverte, transition de zone, etc.), on ralentit progressivement
        # pour eviter de consommer du CPU/GPU a tourner a 15/s sans rien
        # trouver. Le sleep est applique ICI (avant le traitement) pour
        # que le compteur soit reset au premier parse OK suivant : on
        # remonte alors immediatement en cadence normale (~4/s) sans
        # rallonger le 1er parse reussi.
        if pos is None:
            consecutive_empty += 1
            # Determiner le sleep selon le stage atteint. On parcourt en
            # ordre inverse pour trouver le seuil le plus eleve atteint.
            empty_sleep = 0.0
            for threshold, sleep_s in reversed(OCR_EMPTY_BACKOFF_STAGES):
                if consecutive_empty >= threshold:
                    empty_sleep = sleep_s
                    break
            if empty_sleep > 0:
                time.sleep(empty_sleep)
        # else : pos OK, le reset a 0 est fait plus bas dans le bloc 'if pos:'.

        # Detection SC ferme : uniquement si on a DEJA eu une pos valide,
        # sinon on est juste en train de charger SC (menu, login, etc.)
        if sc_online and has_ever_had_pos and (time.time() - last_parse_ok_time) > SC_OFFLINE_TIMEOUT:
            sc_online = False
            _dbg_log(f"[SC OFFLINE] pas de position valide depuis {SC_OFFLINE_TIMEOUT}s")
            _ws_send_safe({"type": "sc_offline"})

        # Stats periodiques (toutes les 30s)
        if time.time() - stats_t0 >= 30.0:
            dur = time.time() - stats_t0
            _dbg_log(
                f"[STATS 30s] tentes={stats_tried} parses_ok={stats_parsed} "
                f"rejetes_jump={stats_rejected} cid_similar={stats_cid_similar} "
                f"taux={100*stats_parsed/max(stats_tried,1):.0f}% "
                f"cadence={stats_tried/dur:.1f}/s"
            )
            # Log des metriques systeme (CPU, RAM, GPU NVIDIA, VRAM, conso
            # du process Circus). Aide a diagnostiquer les soucis perf des
            # joueurs (ex: surcharge CPU qui creerait des gresillements
            # micro Discord).
            try:
                _log_system_metrics()
            except Exception:
                pass
            stats_t0       = time.time()
            stats_tried    = 0
            stats_parsed   = 0
            stats_rejected = 0
            stats_cid_similar = 0

            # Cleanup VRAM CUDA periodique : libere la memoire fragmentee
            # accumulee par les nombreuses lectures EasyOCR. Sur les petites
            # cartes (RTX 2060 6GB) qui partagent la VRAM avec Star Citizen,
            # ca evite les freezes OCR quand SC fait un pic memoire.
            # Operation legere (~5-10ms) faite uniquement toutes les 30s.
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                # Pas critique si ca echoue
                pass

        if pos:
            stats_parsed += 1
            last_parse_ok_time = time.time()
            has_ever_had_pos   = True  # on considerera sc_offline si plus rien apres ca
            # Reset du compteur de vides : on a un parse valide, on revient en
            # cadence normale (le backoff vide sera reactive si on re-enchaine
            # des frames vides).
            consecutive_empty = 0

            # ETAPE PROACTIVE : appliquer la memoire de signe avant tout
            # autre traitement. Si OCR a rate un '-' sur un nombre clair
            # (|val| >= SIGN_NEAR_ZERO), on force le signe memorise.
            # Ne corrige PAS les valeurs proches de 0 (oscillations OCR
            # legitimes ou vrais passages par l'origine).
            pos, _sign_corrections = _apply_sign_memory(pos)
            if _sign_corrections:
                _details = ", ".join(f"{ax}: {old:+.2f} -> {new:+.2f}"
                                     for ax, old, new in _sign_corrections)
                # Dedup pour eviter le spam log : si l'OCR rate le '-' sur
                # un axe en boucle (cas Pyro 07/05/2026 : z=-67.1854km lu
                # +67189m a chaque frame), on logguait 300+ corrections
                # identiques en 5 min. Le mecanisme de correction lui-meme
                # est utile (memoire = bonne valeur, on rattrape l'OCR), mais
                # le log spam pollue. On agrege par signature (axe+valeur
                # arrondie) : log la 1ere occurrence et au plus 1 fois toutes
                # les SIGN_APPLY_LOG_INTERVAL secondes si la serie continue.
                _dedup_sig = "|".join(
                    f"{ax}:{round(old)}->{round(new)}"
                    for ax, old, new in _sign_corrections
                )
                _now_mono = time.monotonic()
                _last_log = _sign_apply_log_state.get(_dedup_sig)
                # 1ere occurrence (jamais vue) : log immediat. Sinon : log
                # seulement si l'intervalle minimum est ecoule. Ce sentinel
                # (None vs 0.0) evite que la 1ere occurrence soit ratee si
                # time.monotonic() est petit au demarrage du thread.
                if _last_log is None or (_now_mono - _last_log) >= SIGN_APPLY_LOG_INTERVAL:
                    _sign_apply_log_state[_dedup_sig] = _now_mono
                    _dbg_log(f"[SIGN MEMORY APPLY] correction {_details}")

            # SC etait considere offline ? on notifie qu'on est revenu
            if not sc_online:
                sc_online = True
                _dbg_log("[SC ONLINE] OCR refonctionne")
                _ws_send_safe({"type": "sc_online"})
            if last_pos is not None:
                # 0. Filtre anti-sign-flip : si la nouvelle position est identique
                #    en valeur absolue a la derniere mais avec des signes differents,
                #    c'est un artefact OCR (le '-' a ete rate ou ajoute a cause de
                #    la pollution 2e ligne HUD). On rejette silencieusement.
                #
                #    EXCEPTION : si _restore_minus_signs() a explicitement
                #    detecte et restaure un '-' dans cette lecture, on fait
                #    confiance a la correction (elle est basee sur scan pixel
                #    direct, plus fiable qu'EasyOCR sur ce point precis).
                #    Sinon on aurait un cas pathologique : EasyOCR rate le '-'
                #    sur N lectures consecutives -> last_pos est positif ->
                #    quand le tiret est enfin detecte (par scan ou par EasyOCR),
                #    le sign-flip rejette comme aberration -> on reste bloque
                #    sur la valeur fausse.
                if _is_sign_flip(last_pos, pos) and not _get_minus_was_restored():
                    # Mecanisme de VOTE : si N sign-flips consecutifs vont vers
                    # la meme nouvelle valeur, c'est que last_pos etait FAUX.
                    # On bascule alors la reference vers la nouvelle valeur.
                    #
                    # Bug Perseus typique : tu es a (0, +30, 4) reel. EasyOCR
                    # ajoute parfois un '-' parasite -> 1ere lecture (0, -30, 4)
                    # devient last_pos. Toutes les lectures correctes suivantes
                    # (0, +30, 4) seront rejetees comme sign-flip. Avec le vote,
                    # apres 3 (0, +30, 4) consecutifs, on bascule -> (0, +30, 4)
                    # devient la nouvelle reference.
                    #
                    # Critere "meme valeur" : meme container + chaque axe a
                    # tolerance_flipped pres (5m). _is_sign_flip(last_pos, pos)
                    # garantit deja la coherence de pos avec lui-meme : il
                    # suffit de comparer pos avec _sign_flip_pending.
                    if _sign_flip_pending is not None and not _is_sign_flip(_sign_flip_pending, pos, tolerance_flipped=3.0, tolerance_unflipped=8.0):
                        # pos ressemble fortement au pending -> meme vote
                        # (note: si is_sign_flip retourne False entre 2 valeurs
                        # similaires, c'est qu'elles n'ont pas de flip = sont
                        # "identiques" en realite)
                        cid_match = pos.get("container_id") == _sign_flip_pending.get("container_id")
                        axes_close = all(
                            abs(pos.get(k, 0) - _sign_flip_pending.get(k, 0)) < 3.0
                            for k in ("x", "y", "z")
                        )
                        if cid_match and axes_close:
                            _sign_flip_consec += 1
                        else:
                            _sign_flip_pending = pos
                            _sign_flip_consec = 1
                    else:
                        _sign_flip_pending = pos
                        _sign_flip_consec = 1

                    if _sign_flip_consec >= SIGN_FLIP_VOTE_TARGET:
                        # On a vu N fois la meme nouvelle valeur -> bascule
                        _dbg_log(
                            f"[SIGN FLIP BASCULE] apres {_sign_flip_consec} votes consecutifs, "
                            f"on accepte : last={last_pos.get('x')},{last_pos.get('y')},{last_pos.get('z')} "
                            f"-> nouveau={pos.get('x')},{pos.get('y')},{pos.get('z')}"
                        )
                        # Vote a bascule = la memoire de signe etait fausse.
                        # On la FORCE a coller a la nouvelle realite. Sinon
                        # _apply_sign_memory continuerait a re-corriger les
                        # lectures futures vers l'ancien signe -> boucle.
                        _cid = pos.get("container_id")
                        if _cid:
                            # IMPORTANT : ces 3 dicts sont definis dans
                            # circusvoip_sc_ocr.py (variables module). On y
                            # accede via le proxy _sco_mod, pas par nom direct,
                            # sinon NameError au runtime (les variables ne
                            # sont pas importees explicitement dans le namespace
                            # du core). Bug observe le 08/05/2026 (Vanguard
                            # Hoplite) : crash thread OCR a la 1ere bascule
                            # de vote, watchdog respawn 30s plus tard.
                            _mem = _sco_mod._sign_memory_per_container.setdefault(_cid, {})
                            for _ax in ("x", "y", "z"):
                                _v = pos.get(_ax)
                                if _v is not None and abs(_v) >= _sco_mod.SIGN_NEAR_ZERO:
                                    _mem[_ax] = -1 if _v < 0 else +1
                                # Reset le streak pour cet axe : la memoire
                                # vient d'etre re-validee par bascule de vote
                                _sco_mod._sign_correction_streak.pop((_cid, _ax), None)
                                _sco_mod._sign_correction_history.pop((_cid, _ax), None)
                            _dbg_log(f"[SIGN MEMORY UPDATE] memoire mise a jour suite au vote pour {_cid}")
                        _sign_flip_pending = None
                        _sign_flip_consec = 0
                        # Bypasser aussi le filtre AXIS JUMP ci-dessous
                        sign_flip_accepted_via_vote = True
                    else:
                        _dbg_log(f"[SIGN FLIP IGNORE] last={last_pos.get('x')},{last_pos.get('y')},{last_pos.get('z')} "
                                 f"nouveau={pos.get('x')},{pos.get('y')},{pos.get('z')} "
                                 f"(vote {_sign_flip_consec}/{SIGN_FLIP_VOTE_TARGET})")
                        stats_rejected += 1
                        time.sleep(0.05)
                        continue
                else:
                    # Pas un sign-flip : reset le compteur de vote
                    _sign_flip_pending = None
                    _sign_flip_consec = 0
                    sign_flip_accepted_via_vote = False
                # Si SIGN FLIP est ACCEPTE, on bypass aussi le filtre AXIS JUMP
                # ci-dessous : la nouvelle valeur (signe corrige par detection
                # visuelle de tiret OU vote sur lectures repetees) est PLUS fiable
                # que last_pos. Sans ce bypass, le saut de signe (-417 -> +417 =
                # dx=835m) declenche AXIS JUMP et rejette la correction, on reste
                # bloque sur l'ancien signe.
                sign_flip_accepted = sign_flip_accepted_via_vote  # init avec le vote
                if _is_sign_flip(last_pos, pos) and _get_minus_was_restored():
                    # Bug fix 1080p (Option 4) : avant d'accepter le bypass,
                    # verifier que la mémoire de signe ne contredit pas la
                    # nouvelle valeur sur un axe stable a grande magnitude.
                    # Cas observe en 1080p : EasyOCR rate parfois le `-` sur
                    # un axe (ex: z=+114 au lieu de -114), mais detecte le `-`
                    # d'un autre axe (ex: x=-3.5). Le scan visuel de tiret
                    # leve _minus_was_restored=True (sur l'axe correct), mais
                    # _is_sign_flip est True a cause de l'axe foireux (z).
                    # Resultat : SIGN FLIP ACCEPT bascule sur la mauvaise pos.
                    #
                    # Garde : si la memoire de signe a un streak >= 5 sur un
                    # axe avec |val| >= 50m (donc memoire tres fiable, ce
                    # n'est pas une oscillation pres de zero), et que la
                    # nouvelle position contredit cette memoire sur cet axe,
                    # on n'accepte PAS le bypass. La frame sera evaluee par
                    # AXIS JUMP IGNORE ci-dessous (qui la rejettera car le
                    # saut de signe est >= 100m sur cet axe), et SIGN MEMORY
                    # APPLY corrigera la prochaine frame correcte.
                    cid = pos.get("container_id")
                    contradicts_stable_memory = False
                    if cid:
                        try:
                            mem = _sco_mod._sign_memory_per_container.get(cid, {})
                            for axis in ("x", "y", "z"):
                                memorized_sign = mem.get(axis)
                                if memorized_sign is None or memorized_sign == 0:
                                    continue
                                streak = _sco_mod._sign_correction_streak.get(
                                    (cid, axis), 0
                                )
                                if streak < _sco_mod.SIGN_RESTORE_TRUST_MIN_STREAK:
                                    continue
                                val_new = pos.get(axis, 0) or 0
                                # Magnitude elevee uniquement (>= 50m), pour
                                # eviter les faux positifs sur les axes pres
                                # de l'origine ou le signe oscille naturellement.
                                if abs(val_new) < 50.0:
                                    continue
                                current_sign = -1 if val_new < 0 else +1
                                if current_sign != memorized_sign:
                                    contradicts_stable_memory = True
                                    _dbg_log(
                                        f"[SIGN FLIP REJECT] sign-flip detecte "
                                        f"mais memoire stable contredit sur {axis} "
                                        f"(streak={streak}, val={val_new}, "
                                        f"memorized={memorized_sign}). "
                                        f"On NE bascule PAS, AXIS JUMP / SIGN MEMORY "
                                        f"prendront le relais."
                                    )
                                    break
                        except Exception:
                            contradicts_stable_memory = False
                    if not contradicts_stable_memory:
                        _dbg_log(
                            f"[SIGN FLIP ACCEPT] tiret restaure visuellement, "
                            f"on bascule : last={last_pos.get('x')},{last_pos.get('y')} "
                            f"-> nouveau={pos.get('x')},{pos.get('y')}"
                        )
                        sign_flip_accepted = True

                # 0a. Filtre anti-aberration axe-par-axe : si un seul axe explose
                #     (>50m de diff, impossible physiquement pour un joueur a pied),
                #     c'est un bug de parsing OCR (ex: "7.74m" lu "774m", "0.97m" lu
                #     "97m"). On rejette silencieusement.
                #     Ne s'applique QUE si le container_id est le meme : un vrai
                #     changement de container peut legitimement changer toutes les
                #     coords d'un coup (passage hangar -> vaisseau).
                #     Seuil : 35m > vitesse max joueur (~10m/s) * intervalle typique (~0.5s)
                #     avec marge x7. Un vrai deplacement a pied depasse rarement 20m
                #     entre 2 lectures OCR a cadence ~2-5/s.
                #     EXCEPTION : si SIGN FLIP ACCEPT vient d'etre log, on bypass
                #     (le saut est legitime, c'est une correction de signe).
                if (not sign_flip_accepted
                    and pos.get("container_id") == last_pos.get("container_id")
                    and last_pos.get("container_id") is not None):
                    dx = abs((pos.get("x") or 0) - (last_pos.get("x") or 0))
                    dy = abs((pos.get("y") or 0) - (last_pos.get("y") or 0))
                    dz = abs((pos.get("z") or 0) - (last_pos.get("z") or 0))
                    AXIS_MAX_JUMP = 35.0  # metres sur un seul axe
                    if dx > AXIS_MAX_JUMP or dy > AXIS_MAX_JUMP or dz > AXIS_MAX_JUMP:
                        # MECANISME DE CONVERGENCE :
                        # Sans ca, si last_pos est faux (ex: parsing OCR foireux
                        # combine avec sign memory qui inverse a tort), TOUTES
                        # les frames suivantes correctes sont rejetees, et
                        # last_pos reste eternellement bloque sur la valeur
                        # fausse. Bug observe le 07/05/2026 sur Skywat (1 minute
                        # entiere de positions rejetees, aucune position envoyee
                        # au serveur, VOIP positionnelle KO).
                        #
                        # Solution : on memorise la position rejetee. Si N frames
                        # consecutives convergent vers une zone proche les unes
                        # des autres (cohérent), on bascule sur cette nouvelle
                        # zone (le user a probablement vraiment teleporte ou
                        # last_pos etait faux). Memes parametres que le filtre
                        # MAX_JUMP en aval (3 frames, distance < 50m entre elles).
                        if _axis_jump_pending is None:
                            _axis_jump_pending = pos
                            _axis_jump_consec  = 1
                        else:
                            try:
                                d_to_pending = distance(pos, _axis_jump_pending)
                            except Exception:
                                d_to_pending = float("inf")
                            if d_to_pending < AXIS_JUMP_CONVERGE_MAX_M:
                                _axis_jump_consec += 1
                                if _axis_jump_consec >= AXIS_JUMP_CONVERGE_TARGET:
                                    # Bug fix 1080p : avant de basculer, verifier
                                    # que la "nouvelle zone" n'est pas juste un
                                    # sign-flip de last_pos (memes valeurs absolues
                                    # avec un signe different sur 1+ axes). Cas
                                    # typique en 1080p ou EasyOCR rate parfois
                                    # le meme `-` sur 3 frames consecutives ->
                                    # avant ce garde, on basculait sur la fausse
                                    # position (z=+114 au lieu de -114) et le
                                    # SIGN FLIP ACCEPT devait corriger apres coup.
                                    # On n'utilise PAS _is_sign_flip ici car il
                                    # exige meme container_id, ce qui est deja le
                                    # cas dans cette branche, mais on veut une
                                    # logique plus simple : valeurs absolues
                                    # similaires + signe oppose sur >= 1 axe.
                                    is_sign_flip_candidate = False
                                    try:
                                        for axis in ("x", "y", "z"):
                                            va = last_pos.get(axis, 0) or 0
                                            vb = pos.get(axis, 0) or 0
                                            # Signe oppose et |val| similaire (tolerance 5m)
                                            if (va * vb < 0
                                                and abs(abs(va) - abs(vb)) < 5.0
                                                and max(abs(va), abs(vb)) >= 2.0):
                                                is_sign_flip_candidate = True
                                                break
                                    except Exception:
                                        is_sign_flip_candidate = False
                                    if is_sign_flip_candidate:
                                        # Probable sign-flip OCR : NE PAS basculer.
                                        # On rejette ces 3 frames et on attend une
                                        # frame "vraie" qui sera detectee soit par
                                        # SIGN FLIP ACCEPT (visuellement le -),
                                        # soit par SIGN MEMORY APPLY.
                                        _dbg_log(
                                            f"[AXIS JUMP CONVERGE BLOCKED] "
                                            f"sign-flip detecte vs last_pos, "
                                            f"on NE bascule PAS. "
                                            f"last=({last_pos.get('x')},{last_pos.get('y')},{last_pos.get('z')}) "
                                            f"pending=({pos.get('x')},{pos.get('y')},{pos.get('z')})"
                                        )
                                        # Reset le pending : on n'a pas converge
                                        _axis_jump_pending = None
                                        _axis_jump_consec  = 0
                                        # Rejet de la frame courante
                                        stats_rejected += 1
                                        time.sleep(0.05)
                                        continue
                                    # Convergence legitime : on bascule sur la nouvelle zone
                                    _dbg_log(
                                        f"[AXIS JUMP CONVERGE] {AXIS_JUMP_CONVERGE_TARGET} "
                                        f"frames consecutives convergent vers "
                                        f"({pos.get('x')},{pos.get('y')},{pos.get('z')}) "
                                        f"alors que last_pos=({last_pos.get('x')},"
                                        f"{last_pos.get('y')},{last_pos.get('z')}) "
                                        f"-> bascule sur la nouvelle zone (last_pos "
                                        f"etait probablement faux)"
                                    )
                                    _axis_jump_pending = None
                                    _axis_jump_consec  = 0
                                    # On laisse continuer le pipeline : la frame
                                    # courante sera acceptee comme nouveau last_pos.
                                else:
                                    # Pas encore assez de votes, continuer a memoriser
                                    _axis_jump_pending = pos
                            else:
                                # La frame courante diverge du pending : reset
                                _axis_jump_pending = pos
                                _axis_jump_consec  = 1

                        # Si pas encore de convergence, rejeter la frame
                        if _axis_jump_consec > 0 and _axis_jump_consec < AXIS_JUMP_CONVERGE_TARGET:
                            _dbg_log(f"[AXIS JUMP IGNORE] last=({last_pos.get('x')},{last_pos.get('y')},{last_pos.get('z')}) "
                                     f"nouveau=({pos.get('x')},{pos.get('y')},{pos.get('z')}) "
                                     f"dx={dx:.1f} dy={dy:.1f} dz={dz:.1f} "
                                     f"converge={_axis_jump_consec}/{AXIS_JUMP_CONVERGE_TARGET}")
                            stats_rejected += 1
                            time.sleep(0.05)
                            continue
                    else:
                        # Frame coherente avec last_pos : reset le compteur
                        # de convergence si on en avait un en cours.
                        if _axis_jump_consec > 0:
                            _axis_jump_pending = None
                            _axis_jump_consec  = 0

                # 0b. Filtre anti-container-similaire : si le container_id a change
                #     mais n'est different que de 1-2 chars (erreur OCR sur un chiffre
                #     ou lettre), ET que les coords sont proches, on considere que
                #     c'est le MEME container. On reecrit le container_id pour le
                #     forcer a etre stable et eviter un faux JUMP.
                cid_last = last_pos.get("container_id")
                cid_new  = pos.get("container_id")
                if (cid_last != cid_new
                    and _are_containers_similar(cid_last, cid_new)
                    and abs(pos.get("x", 0) - last_pos.get("x", 0)) < 5
                    and abs(pos.get("y", 0) - last_pos.get("y", 0)) < 5
                    and abs(pos.get("z", 0) - last_pos.get("z", 0)) < 5):
                    _dbg_log(f"[CID SIMILAIRE] {cid_new!r} ~= {cid_last!r} "
                             f"(reecrit en {cid_last!r})")
                    pos["container_id"]   = cid_last
                    pos["container_name"] = last_pos.get("container_name", pos.get("container_name"))
                    stats_cid_similar += 1

                d = distance(pos, last_pos)
                if d > MAX_JUMP:
                    # La position est loin de la derniere connue.
                    # On la rejette SAUF si plusieurs lectures consecutives
                    # convergent vers cette nouvelle zone (deplacement reel).
                    reject_count += 1
                    stats_rejected += 1
                    if reject_count < 3:
                        # Memoriser cette position candidate : si on en obtient
                        # 2 autres proches, on valide
                        if reject_count == 1:
                            _pending_pos = pos
                        elif reject_count >= 2:
                            # Verifier si cette nouvelle position est proche
                            # de la candidate memorisee
                            try:
                                d2 = distance(pos, _pending_pos)
                                if d2 < MAX_JUMP:
                                    # Convergence -> accepter la nouvelle zone
                                    _dbg_log(f"[JUMP CONFIRMED] ancien={last_pos} nouveau={pos}")
                                    last_pos      = pos
                                    reject_count  = 0
                                    stats_rejected -= 2  # on annule les rejets
                            except Exception:
                                pass
                        time.sleep(0.05)
                        continue
                    else:
                        # Trop de rejets : accepter quand meme
                        reject_count = 0
            _pending_pos = None
            reject_count  = 0
            last_pos      = pos
            state.my_pos  = pos

            # Mettre a jour l'UI
            ui.update_my_pos(pos)

            # Activer/desactiver l'echo grotte selon le container courant.
            # Appele a chaque nouvelle pos (OCR) pour reagir des l'entree/sortie
            # d'une grotte. Interne a audio_io : toggle sans cout si valeur
            # inchangee.
            if state.audio_io is not None:
                in_cave = _is_cave_container(
                    pos.get("container_id") or "",
                    pos.get("container_name") or "",
                )
                state.audio_io.set_cave_echo(in_cave)

            for name, info in state.players.items():
                if info.get("pos"):
                    # Check container_id : si on est dans un container different
                    # de l'autre joueur (ex: moi dans hangar, lui dans tram),
                    # la distance est infinie (silence). Bug observe le 07/05/2026 :
                    # tester A sortie d'ascenseur, tester B reste dedans, le client
                    # affichait ~100m alors qu'on etait dans 2 containers separes
                    # -> tester A entendait tester B en proximity a tort.
                    # Le legacy avait deja cette logique (ligne 4009 : "Containers
                    # differents -> silence"), regression introduite lors du split.
                    my_cid    = pos.get("container_id")
                    their_cid = info["pos"].get("container_id")
                    if my_cid != their_cid:
                        # Containers differents -> hors de portee
                        d = float("inf")
                    else:
                        d = distance(pos, info["pos"])
                    info["dist"] = d
                    # Rafraichir l'UI de ce joueur
                    ui.update_player(name, info["pos"], d)

            # Appliquer le volume audio pour chaque joueur selon sa distance
            if state.audio_io is not None:
                now_mono = time.monotonic()
                for name, info in state.players.items():
                    # Si le joueur est actuellement en radio (trame radio recue
                    # il y a moins de 1s), ne PAS ecraser son volume - laisser la
                    # reception audio gerer (volume 1.0 pendant radio).
                    last_radio = state.radio_recv_ts.get(name, 0)
                    if (now_mono - last_radio) < 1.0:
                        continue
                    # Si SC est ferme chez l'autre -> volume 0
                    if not info.get("sc_online", True):
                        state.audio_io.set_user_volume(name, 0.0)
                        continue
                    # Si l'autre n'a pas de position connue (jamais en jeu, ou
                    # en menu/frontend) -> volume 0 (proximity impossible)
                    if info.get("pos") is None:
                        state.audio_io.set_user_volume(name, 0.0)
                        continue
                    dist = info.get("dist")
                    if dist is None:
                        # Distance pas encore calculee (on vient juste de recevoir
                        # sa position, mais la boucle de calcul n'a pas tourne)
                        # -> silencieux par prudence
                        state.audio_io.set_user_volume(name, 0.0)
                        continue
                    vol = compute_proximity_volume(
                        dist,
                        AUDIBLE_RANGE_M,
                        force_short=bool(state.player_prox_short.get(name, False)),
                    )
                    state.audio_io.set_user_volume(name, vol)

            if state.players:
                min_dist = min(
                    (info["dist"] for info in state.players.values() if info.get("dist") is not None),
                    default=None
                )
                if min_dist is not None:
                    ui.update_min_dist(min_dist)

            _ws_send_safe({
                "type": "pos",
                "pos": pos,
                "ts_capture": time.time(),  # timestamp Unix pour mesurer latence
            })

# ---------------------------------------------
#  Interface
# ---------------------------------------------

BG_CLIENT = "#0d1117"
BG_PANEL  = "#161b22"
BG_ROW    = "#21262d"
BORDER    = "#30363d"
TEXT_C    = "#c9d1d9"
MUTED_C   = "#6e7681"
GREEN_C   = "#3fb950"
ORANGE_C  = "#d29922"
BLUE_C    = "#58a6ff"
RED_C     = "#f85149"


