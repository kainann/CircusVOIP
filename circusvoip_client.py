"""
CircusVOIP - Client (port PySide6)
==================================

Client CircusVOIP basé sur Qt (PySide6). Délègue la logique métier
à `circusvoip_core` (réseau, audio, helmet, gamelog, OCR loop, radio
PTT) et le pipeline OCR à `circusvoip_sc_ocr` (capture mss + EasyOCR
+ parsing tolérant aux erreurs OCR).

Fonctionnalités :
  - Connexion serveur (port 8888) + table joueurs avec distances
  - Audio I/O (devices, gain, gate, mute, VU-mètre)
  - OCR Star Citizen + proximity audio (port 8889)
  - Calibration zone OCR (auto/manuelle, multi-écran)
  - Helmet detection + Game.log auto-switch (suit la version SC active)
  - Radio PTT (canal + profil) + Mode RP
  - Overlays floating (mutes/channel/prox_range) avec drag/resize
  - Mode anonyme (broadcast serveur)

Lancement : py -3.14 circusvoip_client.py

Config : circusvoip_client_config.json (un seul fichier qui regroupe
         toutes les preferences : audio, connexion, OCR, radio PTT,
         overlays, geometrie fenetre, Mode RP).
         Migration auto : si l'ancien circusvoip_client2_config.json
         existe, ses cles sont fusionnees au boot puis l'ancien fichier
         est renomme en .migrated.bak.

Note DPI : on force PER_MONITOR_AWARE_V2 via ctypes AVANT import Qt.
Sans ca, sur certains Windows, Qt voit DPI=96 partout (mode
SYSTEM_AWARE) et le rescaling natif entre ecrans ne marche pas.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ----------------------------------------------------------------------
# BOOT TIMING : mesure de la duree de chaque etape du lancement client.
# Sortie sur stdout. Permet de diagnostiquer "pourquoi le lancement
# est-il si long" sans modifier le code a chaque fois. Mis ici (apres
# imports stdlib, avant tout le reste) pour T0 le plus tot possible.
# ----------------------------------------------------------------------
_BOOT_T0 = time.perf_counter()
_BOOT_TIMING = True

def _boot_log(label: str) -> None:
    """Print '[BOOT TIMING] +0.234s : label' depuis le T0 du process."""
    if not _BOOT_TIMING:
        return
    elapsed = time.perf_counter() - _BOOT_T0
    print(f"[BOOT TIMING] +{elapsed:6.3f}s : {label}", flush=True)

_boot_log("imports stdlib termines (T0 du timing)")

# ----------------------------------------------------------------------
# DPI awareness Windows : DOIT etre fixe avant import Qt
# ----------------------------------------------------------------------
if sys.platform == "win32":
    try:
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)  # PER_MONITOR_AWARE_V2
            )
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
    except Exception:
        pass

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")


# ----------------------------------------------------------------------
# Bootstrap pip : auto-installation des dependances tierces
# ----------------------------------------------------------------------
# Au tout 1er lancement (ou apres une grosse MAJ qui ajoute des deps),
# certains modules peuvent manquer dans le runtime Python de l'utilisateur.
# Plutot que crasher avec un ImportError obscur, on tente une installation
# automatique via "python -m pip install <package>".
#
# Le bloc tourne SYNCHRONEMENT avant le 1er import lourd : si une dep
# manque, on bloque l'app le temps de pip install (peut prendre 30s a 5min
# selon les deps : EasyOCR + torch font ~2 GB). On affiche le progres
# directement dans la console (pas d'UI Qt encore disponible).
#
# Si l'utilisateur n'a pas internet ou pip plante, on tombe en erreur
# claire au lieu d'un import silencieux.
#
# Liste des paires (nom_module_python, package_pip) :
# - le 1er nom est ce qu'on tente d'importer pour tester la presence
# - le 2e est le nom passe a pip install
# Pour la plupart, c'est identique. Exceptions :
#   - cv2 -> opencv-python
#   - PIL -> Pillow (pas utilise mais exemple)

_REQUIRED_PACKAGES = [
    # (module_to_import, pip_package_name)
    ("PySide6",      "PySide6"),
    ("websockets",   "websockets"),
    ("numpy",        "numpy"),
    ("mss",          "mss"),
    ("cv2",          "opencv-python"),
    ("easyocr",      "easyocr"),
    ("pytesseract",  "pytesseract"),
    ("sounddevice",  "sounddevice"),
    ("pynput",       "pynput"),
    ("psutil",       "psutil"),
    # pynvml : pour les metriques GPU NVIDIA dans le log [METRICS] de
    # circusvoip_core.py. Le module Python s'appelle 'pynvml' mais le
    # package pip s'appelle 'nvidia-ml-py'. Sans ce module, les
    # metriques GPU sont silencieusement skippees mais le client tourne.
    ("pynvml",       "nvidia-ml-py"),
    # torch est tire automatiquement par easyocr (dependance), pas besoin
    # de l'inclure explicitement.
]


def _bootstrap_dependencies():
    """Verifie chaque dep dans _REQUIRED_PACKAGES et tente d'installer
    celles qui manquent via 'python -m pip install'. Bloque pendant
    l'install si necessaire.

    NOTE PERF : on utilise importlib.util.find_spec() au lieu de
    importlib.import_module(). find_spec verifie qu'un module est
    trouvable sur le sys.path SANS executer son code d'import. C'est
    crucial pour le temps de boot : import_module('easyocr') tirerait
    torch + cuda + cv2 + opencv pour ~15s. find_spec('easyocr') ne fait
    qu'un check de fichier, en quelques ms. Les vrais imports auront
    lieu plus tard, au moment ou les modules sont vraiment necessaires
    (et le cout est alors inevitable).

    Limite : find_spec ne detecte pas les modules installes mais casses
    a l'init (ex: sounddevice qui exige une lib OS absente). Dans ce
    cas, on ne tente pas un pip install (qui ne resoudrait pas le
    probleme OS) et on laisse l'erreur remonter au moment du vrai
    import plus tard, avec un message d'erreur plus precis."""
    import importlib.util
    import subprocess

    missing = []
    for mod_name, pip_name in _REQUIRED_PACKAGES:
        try:
            spec = importlib.util.find_spec(mod_name)
        except (ImportError, ValueError):
            # find_spec peut lever ImportError sur certains modules
            # avec __init__.py problematique. Traiter comme manquant.
            spec = None
        if spec is None:
            missing.append((mod_name, pip_name))

    if not missing:
        return

    print("=" * 64, flush=True)
    print("[BOOTSTRAP] Dependances manquantes detectees :", flush=True)
    for mod_name, pip_name in missing:
        print(f"  - {pip_name}  (import {mod_name})", flush=True)
    print("[BOOTSTRAP] Installation en cours via pip. Cela peut prendre", flush=True)
    print("            quelques minutes (EasyOCR + torch font ~2 GB).", flush=True)
    print("=" * 64, flush=True)

    # Verifier que pip est disponible. Si non, on ne peut rien faire.
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            check=True, capture_output=True, timeout=10
        )
    except Exception as e:
        print(f"[BOOTSTRAP] ERREUR : pip indisponible dans le runtime "
              f"Python ({e}).", flush=True)
        print(f"[BOOTSTRAP] Installer les deps manuellement :", flush=True)
        deps_str = " ".join(p for _, p in missing)
        print(f"  py -m pip install {deps_str}", flush=True)
        sys.exit(1)

    # Installation pour chaque dep manquante
    failed = []
    for mod_name, pip_name in missing:
        print(f"[BOOTSTRAP] pip install {pip_name}...", flush=True)
        try:
            # On utilise check=False pour pouvoir collecter les erreurs et
            # passer aux suivantes sans bloquer.
            # Timeout 30 min : EasyOCR + torch font ~2 GB cumules.
            # Sur connexion ADSL rurale (~500 KB/s), cela peut prendre
            # 20-25 min. 10 min etait trop court et causait des echecs
            # de setup chez certains testeurs.
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", pip_name],
                check=False, capture_output=False, timeout=1800,
            )
            if result.returncode != 0:
                failed.append(pip_name)
                print(f"[BOOTSTRAP] Echec : {pip_name} (code {result.returncode})",
                      flush=True)
        except subprocess.TimeoutExpired:
            failed.append(pip_name)
            print(f"[BOOTSTRAP] Timeout sur {pip_name} (>30 min)", flush=True)
        except Exception as e:
            failed.append(pip_name)
            print(f"[BOOTSTRAP] Erreur sur {pip_name} : {e}", flush=True)

    if failed:
        print("=" * 64, flush=True)
        print(f"[BOOTSTRAP] {len(failed)} dependance(s) ont echoue :", flush=True)
        for p in failed:
            print(f"  - {p}", flush=True)
        print(f"[BOOTSTRAP] Tentez l'installation manuelle :", flush=True)
        print(f"  py -m pip install {' '.join(failed)}", flush=True)
        print("=" * 64, flush=True)
        sys.exit(1)

    print("[BOOTSTRAP] Toutes les dependances sont installees.", flush=True)
    print("[BOOTSTRAP] Demarrage de CircusVOIP...", flush=True)


# Lancement du bootstrap. Doit imperativement preceder les imports tiers.
_boot_log("avant _bootstrap_dependencies()")
_bootstrap_dependencies()
_boot_log("apres _bootstrap_dependencies()")


from PySide6.QtCore import (
    Qt, QTimer, QObject, Signal, Slot, QThread, QPoint, QRect,
)
from PySide6.QtGui import (
    QGuiApplication, QScreen, QCursor, QPainter, QColor, QPen,
    QFont, QKeyEvent, QMouseEvent, QIcon, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
_boot_log("imports PySide6 termines")

# Optional dependency : websockets pour la connexion serveur
try:
    import websockets
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

# Audio I/O : module commun avec client1 (independant de Tk/Qt).
# On l'importe en soft pour que le client puisse demarrer meme si
# sounddevice/numpy ne sont pas la (utile pour debug, mais sans audio
# evidemment).
try:
    from circusvoip_audio_io import (
        AudioIO,
        list_input_devices,
        list_output_devices,
        default_input_device,
        default_output_device,
        SAMPLE_RATE,
        BLOCK_SIZE,
    )
    _AUDIO_AVAILABLE = True
except ImportError as _e_audio:
    _AUDIO_AVAILABLE = False
    _AUDIO_IMPORT_ERROR = str(_e_audio)
_boot_log("import circusvoip_audio_io termine")


# Modules CircusVOIP : core (logique metier headless) et sc_ocr (pipeline
# OCR autonome). Le client (UI Qt) ne fait que coordonner ces deux modules.
#
# On reutilise specifiquement de circusvoip_core :
#   - read_coords         : lecture OCR position depuis la zone HUD
#   - auto_ocr_zone       : calcul auto zone OCR selon resolution
#   - _ocr_loop_inner     : boucle OCR principale (sign-flip, jump filter...)
#   - _heartbeat_loop     : ping serveur peridoque
#   - _run_audio_ws       : WS audio (recv frames distantes + envoi via queue)
#   - _on_audio_captured  : callback frames capturees -> queue d'envoi
#   - distance / compute_proximity_volume : calculs volume positionnel
#   - state               : etat global partage entre les boucles
try:
    import circusvoip_core as _core
    _CORE_AVAILABLE = True
except Exception as _e_core:
    _CORE_AVAILABLE = False
    _CORE_IMPORT_ERROR = str(_e_core)
_boot_log("import circusvoip_core termine")

# Module OCR autonome (utilise aussi par circusvoip_core).
try:
    import circusvoip_sc_ocr as _sco
    _SCO_AVAILABLE = True
except Exception as _e_sco:
    _SCO_AVAILABLE = False
    _SCO_IMPORT_ERROR = str(_e_sco)
_boot_log("import circusvoip_sc_ocr termine")



# ======================================================================
# Constantes
# ======================================================================

SERVER_PORT = 8888
DEFAULT_NAME = "Joueur"
DEFAULT_IP = "127.0.0.1"


# ======================================================================
# Theme (palette de couleurs reprise de l'ancien client Tk)
# ======================================================================
# BG_CLIENT  : fond principal de la fenetre
# BG_PANEL   : fond des panneaux/sections
# BG_ROW     : fond des inputs et lignes (legerement plus clair)
# BORDER     : couleur des bordures et boutons neutres
# TEXT_C     : couleur du texte principal
# MUTED_C    : texte secondaire (hints, valeurs par defaut)
# GREEN_C, ORANGE_C, BLUE_C, RED_C : accents (status, MAJ, headers, erreurs)

THEME_BG_CLIENT = "#0d1117"
THEME_BG_PANEL  = "#161b22"
THEME_BG_ROW    = "#21262d"
THEME_BORDER    = "#30363d"
THEME_TEXT      = "#c9d1d9"
THEME_MUTED     = "#6e7681"
THEME_GREEN     = "#3fb950"
THEME_ORANGE    = "#d29922"
THEME_BLUE      = "#58a6ff"
THEME_RED       = "#f85149"

# Stylesheet global applique a la QMainWindow. Cible les widgets Qt
# standards (QWidget, QLabel, QLineEdit, QPushButton, QGroupBox,
# QComboBox, QTableWidget, QHeaderView, QSlider, QScrollArea, QCheckBox,
# QMessageBox). Les widgets avec un setStyleSheet specifique (overlays,
# label de statut connexion, boutons de MAJ) gardent leur style propre.
THEME_QSS = f"""
QMainWindow, QDialog {{
    background-color: {THEME_BG_CLIENT};
    color: {THEME_TEXT};
}}
QWidget {{
    background-color: {THEME_BG_CLIENT};
    color: {THEME_TEXT};
}}
QLabel {{
    color: {THEME_TEXT};
    background: transparent;
}}
QLineEdit {{
    background-color: {THEME_BG_ROW};
    color: {THEME_TEXT};
    border: 1px solid {THEME_BORDER};
    border-radius: 3px;
    padding: 4px;
    selection-background-color: {THEME_BLUE};
}}
QLineEdit:focus {{
    border: 1px solid {THEME_BLUE};
}}
QPushButton {{
    background-color: {THEME_BORDER};
    color: {THEME_TEXT};
    border: 1px solid {THEME_BORDER};
    border-radius: 3px;
    padding: 6px 10px;
}}
QPushButton:hover {{
    background-color: {THEME_BG_ROW};
    border: 1px solid {THEME_MUTED};
}}
QPushButton:pressed {{
    background-color: {THEME_BG_PANEL};
}}
QPushButton:disabled {{
    background-color: {THEME_BG_PANEL};
    color: {THEME_MUTED};
}}
QPushButton:checked {{
    background-color: {THEME_BLUE};
    color: {THEME_BG_CLIENT};
    border: 1px solid {THEME_BLUE};
}}
QGroupBox {{
    background-color: {THEME_BG_PANEL};
    color: {THEME_BLUE};
    border: 1px solid {THEME_BORDER};
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 6px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0 4px;
    color: {THEME_BLUE};
}}
QComboBox {{
    background-color: {THEME_BG_ROW};
    color: {THEME_TEXT};
    border: 1px solid {THEME_BORDER};
    border-radius: 3px;
    padding: 4px 8px;
    min-height: 18px;
}}
QComboBox:hover {{
    border: 1px solid {THEME_MUTED};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background-color: {THEME_BG_PANEL};
    color: {THEME_TEXT};
    border: 1px solid {THEME_BORDER};
    selection-background-color: {THEME_BLUE};
    selection-color: {THEME_BG_CLIENT};
}}
QTableWidget {{
    background-color: {THEME_BG_PANEL};
    color: {THEME_TEXT};
    gridline-color: {THEME_BORDER};
    border: 1px solid {THEME_BORDER};
    selection-background-color: {THEME_BG_ROW};
    selection-color: {THEME_TEXT};
}}
QHeaderView::section {{
    background-color: {THEME_BG_ROW};
    color: {THEME_BLUE};
    border: 1px solid {THEME_BORDER};
    padding: 4px;
    font-weight: bold;
}}
QSlider::groove:horizontal {{
    background: {THEME_BG_ROW};
    border: 1px solid {THEME_BORDER};
    height: 4px;
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {THEME_BLUE};
    border: 1px solid {THEME_BLUE};
    width: 12px;
    margin: -5px 0;
    border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{
    background: {THEME_TEXT};
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background-color: {THEME_BG_CLIENT};
    border: none;
}}
QScrollBar:vertical {{
    background: {THEME_BG_PANEL};
    width: 10px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {THEME_BORDER};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {THEME_MUTED};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QCheckBox {{
    color: {THEME_TEXT};
    background: transparent;
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {THEME_BORDER};
    background: {THEME_BG_ROW};
    border-radius: 2px;
}}
QCheckBox::indicator:checked {{
    background: {THEME_BLUE};
    border: 1px solid {THEME_BLUE};
}}
QProgressBar {{
    background-color: {THEME_BG_ROW};
    border: 1px solid {THEME_BORDER};
    border-radius: 3px;
    text-align: center;
    color: {THEME_TEXT};
}}
QProgressBar::chunk {{
    background-color: {THEME_GREEN};
    border-radius: 2px;
}}
QSpinBox, QDoubleSpinBox {{
    background-color: {THEME_BG_ROW};
    color: {THEME_TEXT};
    border: 1px solid {THEME_BORDER};
    border-radius: 3px;
    padding: 2px 4px;
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {THEME_BLUE};
}}
QToolTip {{
    background-color: {THEME_BG_PANEL};
    color: {THEME_TEXT};
    border: 1px solid {THEME_BORDER};
    padding: 4px;
}}
"""


# ======================================================================
# Fichiers
# ======================================================================

_BASE_DIR = Path(__file__).resolve().parent

# Fichier de configuration unique. Centralise toutes les preferences :
# audio (mic_gain, gate_threshold, devices), connexion (name, server_ip,
# token), OCR (zone_coords, ocr_force_cpu), radio PTT (radio_key,
# profile_radio_key, mute_*_key), overlays (overlays_*), Mode RP, et
# geometrie de fenetre (window_geometry, window_geometry_user_set).
#
# Historiquement, le client utilisait 2 fichiers :
#   - circusvoip_client2_config.json : settings client + geometry
#   - circusvoip_client_config.json  : settings OCR/radio/overlays
# La separation venait du fait que le legacy client (Tk) ecrivait dans le
# 2e fichier en parallele. Maintenant que core a remplace le legacy, on
# unifie tout dans le 1er pour eviter la duplication (qui creait des
# divergences sur ocr_force_cpu notamment).
#
# Au boot, _load_cfg() lit le fichier unique. S'il n'existe pas mais que
# l'ancien fichier circusvoip_client2_config.json est present, on le
# migre automatiquement (la geometrie + audio + connexion sont fusionnes
# avec les autres cles deja presentes, en preservant les valeurs de
# circusvoip_client_config.json en cas de conflit).
CLIENT_CONFIG_FILE = _BASE_DIR / "circusvoip_client_config.json"
_LEGACY_CLIENT2_CONFIG = _BASE_DIR / "circusvoip_client2_config.json"
VERSION_FILE = _BASE_DIR / "circusvoip_version.json"


def _load_version_info() -> dict:
    """Charge le fichier circusvoip_version.json a cote du script.
    Retourne un dict avec 'version' (X.Y.Z), 'channel' (alpha/beta/rc/stable),
    'build' (entier). Si le fichier n'existe pas ou est invalide, retourne
    une version par defaut '0.0.0 alpha 000' (signal qu'il y a un probleme)."""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "version": str(data.get("version", "0.0.0")),
            "channel": str(data.get("channel", "alpha")),
            "build":   int(data.get("build", 0)),
        }
    except Exception:
        return {"version": "0.0.0", "channel": "alpha", "build": 0}


def _format_version_string(info: dict = None) -> str:
    """Retourne la version sous forme lisible : '0.1.0 alpha 001'.
    Le numero de build est zero-padde sur 3 chiffres."""
    if info is None:
        info = _load_version_info()
    return f"{info['version']} {info['channel']} {info['build']:03d}"


def _load_version_string() -> str:
    """Alias retroactif : retourne la version sous forme de string."""
    return _format_version_string()


# Charger la version au demarrage. Utilisee dans le titre de fenetre,
# les logs, et la verification de mise a jour.
_VERSION_INFO   = _load_version_info()
_VERSION_STRING = _format_version_string(_VERSION_INFO)


# ============================================================
# UPDATER CLIENT
# ============================================================
# L'updater interroge le serveur HTTP de mise a jour (port 8080) pour voir
# si une nouvelle version est disponible. Le serveur est suppose tourner
# sur la meme IP que le serveur CircusVOIP (champ 'server_ip' dans la
# config client) -> pas de config supplementaire pour l'utilisateur.
#
# Comparaison de version : on compare le triplet (version, build) entre
# local et distant. Si distant > local, MAJ disponible.
#
# Reproduit a l'identique le comportement du legacy Tk (memes endpoints,
# meme format manifest, memes URL /files/... et /pip_packages/...).

UPDATE_PORT       = 8080
UPDATE_TIMEOUT    = 5   # secondes (timeout HTTP court pour ne pas bloquer)


def _is_newer_version(local: dict, remote: dict) -> bool:
    """Retourne True si la version distante est plus recente que la locale.
    Comparaison sur le triplet (version, build) :
      - version 'X.Y.Z' compare en lexicographique numerique
      - puis build a egalite
    On ignore le 'channel' : on suppose que le serveur ne sert que des
    versions du meme channel (alpha/beta/...).
    """
    def _ver_tuple(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except Exception:
            return (0, 0, 0)
    lv = _ver_tuple(local.get("version", "0.0.0"))
    rv = _ver_tuple(remote.get("version", "0.0.0"))
    if rv > lv:
        return True
    if rv < lv:
        return False
    # Versions egales : compare build
    return int(remote.get("build", 0)) > int(local.get("build", 0))


def _check_for_updates(server_ip: str) -> dict | None:
    """Interroge http://<server_ip>:8080/manifest.json et retourne le
    manifest distant si plus recent que la version locale, sinon None.
    Tout en silencieux : pas d'exception remontee, juste un log debug."""
    if not server_ip:
        return None
    try:
        import urllib.request
        url = f"http://{server_ip}:{UPDATE_PORT}/manifest.json"
        req = urllib.request.Request(
            url, headers={"User-Agent": "CircusVOIP-Client"}
        )
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        remote = json.loads(raw)
        if _is_newer_version(_VERSION_INFO, remote):
            try:
                if _CORE_AVAILABLE:
                    _core._dbg_log(
                        f"[UPDATE] Nouvelle version disponible : "
                        f"{remote.get('version','?')} "
                        f"{remote.get('channel','?')} "
                        f"{int(remote.get('build',0)):03d} "
                        f"(local : {_VERSION_STRING})"
                    )
            except Exception:
                pass
            return remote
        return None
    except Exception as e:
        try:
            if _CORE_AVAILABLE:
                _core._dbg_log(f"[UPDATE] Echec check : {e}")
        except Exception:
            pass
        return None


def _download_update_file(server_ip: str, file_meta: dict, dest_dir: Path) -> bool:
    """Telecharge un fichier depuis le serveur d'update. Verifie le SHA256
    apres telechargement. Retourne True si OK, False sinon."""
    name = file_meta.get("name")
    expected_sha = file_meta.get("sha256")
    if not name or not expected_sha:
        return False
    try:
        import urllib.request
        url = f"http://{server_ip}:{UPDATE_PORT}/files/{name}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "CircusVOIP-Client"}
        )
        dest = dest_dir / name
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        # Verifier sha256
        import hashlib
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha:
            if _CORE_AVAILABLE:
                _core._dbg_log(
                    f"[UPDATE] SHA256 mismatch sur {name} : "
                    f"attendu={expected_sha[:12]}... "
                    f"recu={actual_sha[:12]}..."
                )
            return False
        with open(dest, "wb") as f:
            f.write(data)
        if _CORE_AVAILABLE:
            _core._dbg_log(
                f"[UPDATE] Telecharge {name} ({len(data):,} bytes)"
            )
        return True
    except Exception as e:
        try:
            if _CORE_AVAILABLE:
                _core._dbg_log(f"[UPDATE] Echec download {name} : {e}")
        except Exception:
            pass
        return False


def _download_pip_wheel(server_ip: str, pkg_meta: dict, dest_dir: Path) -> bool:
    """Telecharge un wheel pip depuis le serveur d'update vers dest_dir.
    Verifie le SHA256."""
    name = pkg_meta.get("name")
    expected_sha = pkg_meta.get("sha256")
    if not name or not expected_sha:
        return False
    try:
        import urllib.request
        url = f"http://{server_ip}:{UPDATE_PORT}/pip_packages/{name}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "CircusVOIP-Client"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        import hashlib
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha:
            if _CORE_AVAILABLE:
                _core._dbg_log(f"[UPDATE] SHA256 mismatch sur wheel {name}")
            return False
        dest = dest_dir / name
        with open(dest, "wb") as f:
            f.write(data)
        if _CORE_AVAILABLE:
            _core._dbg_log(
                f"[UPDATE] Telecharge wheel {name} ({len(data):,} bytes)"
            )
        return True
    except Exception as e:
        try:
            if _CORE_AVAILABLE:
                _core._dbg_log(f"[UPDATE] Echec download wheel {name} : {e}")
        except Exception:
            pass
        return False


def _find_site_packages_dir() -> Path | None:
    """Trouve le dossier site-packages du runtime Python en cours.
    Pour un PBS embarque, c'est typiquement runtime/Lib/site-packages/.
    On cherche dans sys.path le dossier qui contient deja des packages
    standards (psutil par ex) car c'est la qu'on doit installer."""
    candidates = []
    for p in sys.path:
        if not p:
            continue
        path = Path(p)
        if path.name == "site-packages" and path.exists():
            candidates.append(path)
    if candidates:
        # Prendre celui le plus proche du runtime (preference :
        # le site-packages qui contient deja psutil)
        for c in candidates:
            if (c / "psutil").exists():
                return c
        return candidates[0]
    return None


def _install_pip_wheel(wheel_path: Path) -> tuple[bool, str]:
    """Installe un wheel en l'extrayant dans le site-packages du runtime.
    Cette approche evite d'avoir besoin de pip dans le runtime PBS
    (parfois pas installe). Limitations connues :
    - Ne gere pas les dependances : si le wheel a besoin d'autres
      packages non installes, le module ne fonctionnera pas.
    - Ne fait pas de scripts post-install."""
    import zipfile
    site_dir = _find_site_packages_dir()
    if not site_dir:
        return False, "site-packages introuvable"
    if not wheel_path.exists():
        return False, f"Wheel introuvable : {wheel_path}"
    try:
        with zipfile.ZipFile(wheel_path, "r") as z:
            names = z.namelist()
            if _CORE_AVAILABLE:
                _core._dbg_log(
                    f"[UPDATE] Extraction wheel {wheel_path.name} "
                    f"({len(names)} fichiers) vers {site_dir}"
                )
            z.extractall(site_dir)
        return True, f"Wheel {wheel_path.name} installe dans {site_dir.name}"
    except Exception as e:
        return False, f"Erreur extraction {wheel_path.name} : {e}"


def _apply_update(server_ip: str, manifest: dict) -> tuple[bool, str]:
    """Telecharge tous les fichiers du manifest dans un dossier temporaire,
    verifie chacun, puis remplace les fichiers locaux en bloc.
    Retourne (success, message). Le client doit ensuite redemarrer pour
    charger les nouveaux .py.

    Gere 2 types de contenu :
    - 'files' : fichiers .py a remplacer dans le dossier de l'app
    - 'pip_packages' : wheels Python a extraire dans site-packages"""
    import tempfile
    files = manifest.get("files", [])
    pip_pkgs = manifest.get("pip_packages", [])
    if not files and not pip_pkgs:
        return False, "Manifest vide (rien a mettre a jour)"
    tmp_dir = Path(tempfile.mkdtemp(prefix="circusvoip_update_"))
    try:
        # Phase 1 : download des .py
        for fmeta in files:
            ok = _download_update_file(server_ip, fmeta, tmp_dir)
            if not ok:
                return False, f"Echec telechargement {fmeta.get('name','?')}"
        # Phase 2 : download des wheels pip
        wheel_paths = []
        for pmeta in pip_pkgs:
            ok = _download_pip_wheel(server_ip, pmeta, tmp_dir)
            if not ok:
                return False, f"Echec telechargement wheel {pmeta.get('name','?')}"
            wheel_paths.append(tmp_dir / pmeta["name"])
        # Phase 3a : remplacer les .py en bloc
        # IMPORTANT : sur Windows, on peut ecrire sur un .py meme si Python
        # le tient ouvert (Python a deja lu son contenu). Le redemarrage
        # est juste necessaire pour charger les nouveaux modules.
        import shutil
        for fmeta in files:
            name = fmeta["name"]
            src = tmp_dir / name
            dst = _BASE_DIR / name
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                return False, f"Echec ecriture {name} : {e}"
        # Phase 3b : installer les wheels (extraction dans site-packages)
        for wheel_path in wheel_paths:
            ok, msg = _install_pip_wheel(wheel_path)
            if _CORE_AVAILABLE:
                if ok:
                    _core._dbg_log(f"[UPDATE] {msg}")
                else:
                    _core._dbg_log(f"[UPDATE] WARNING : {msg}")
        # Cleanup tmp
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        version_str = (
            f"{manifest.get('version','?')} "
            f"{manifest.get('channel','?')} "
            f"{int(manifest.get('build',0)):03d}"
        )
        if _CORE_AVAILABLE:
            _core._dbg_log(f"[UPDATE] Mise a jour appliquee : {version_str}")
        return True, f"Mise a jour {version_str} installee. Redemarrer le client."
    except Exception as e:
        return False, f"Erreur inattendue : {e}"


def _restart_client():
    """Relance le process Python en cours (utile apres une mise a jour).

    Strategie : sur Windows, os.execv() est notoirement peu fiable
    (handles ouverts, sockets, threads, audio streams) et peut planter
    silencieusement ou bloquer. On utilise donc subprocess.Popen() pour
    spawner un nouveau process independant, puis on quitte le process
    courant proprement. Sur Linux/Mac, os.execv() reste correct et plus
    leger (pas de double process pendant la transition).

    Logs systematiques avant/apres pour pouvoir diagnostiquer si la MAJ
    foire (sans ces logs, on ne sait pas si execv a meme ete appele)."""
    import subprocess

    cmd = [sys.executable] + sys.argv
    if _CORE_AVAILABLE:
        try:
            _core._dbg_log(
                f"[UPDATE] Restart en cours : exe={sys.executable} "
                f"argv={sys.argv}"
            )
        except Exception:
            pass

    if sys.platform == "win32":
        # Windows : Popen + exit. CREATE_NEW_PROCESS_GROUP detache le
        # nouveau process pour qu'il survive a la mort du parent.
        # close_fds=True evite que le child herite des sockets/handles
        # encore ouverts du parent (audio, WS, log file).
        try:
            creationflags = 0
            try:
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            except AttributeError:
                pass
            subprocess.Popen(
                cmd,
                close_fds=True,
                creationflags=creationflags,
            )
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        "[UPDATE] Nouveau process spawne, sortie du process courant."
                    )
                except Exception:
                    pass
            # Sortie immediate sans cleanup pour ne pas bloquer sur
            # threads non-daemon ou Qt event loop. os._exit() shunte
            # tout (atexit, finalizers).
            os._exit(0)
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(f"[UPDATE] Echec restart auto : {e}")
                except Exception:
                    pass
    else:
        # Unix : execv reste fiable et evite le double-process.
        try:
            os.execv(sys.executable, cmd)
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(f"[UPDATE] Echec restart auto : {e}")
                except Exception:
                    pass


def _load_cfg() -> dict:
    """Charge la config depuis CLIENT_CONFIG_FILE.

    Migration auto : si CLIENT_CONFIG_FILE existe mais qu'il y a aussi un
    ancien circusvoip_client2_config.json non encore migre, on fusionne
    les cles client2 dans le canonique. Les cles deja presentes dans
    CLIENT_CONFIG_FILE ont priorite (elles refletent l'etat le plus recent
    pour les params OCR/radio/overlays). Les cles uniquement presentes
    dans client2 (geometrie, audio, connexion) sont importees telles
    quelles. Apres migration, l'ancien fichier est renomme .migrated.bak."""
    main_cfg = {}
    if CLIENT_CONFIG_FILE.exists():
        try:
            main_cfg = json.loads(CLIENT_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            main_cfg = {}

    # Migration auto si l'ancien fichier client2 existe encore
    if _LEGACY_CLIENT2_CONFIG.exists():
        try:
            old_cfg = json.loads(
                _LEGACY_CLIENT2_CONFIG.read_text(encoding="utf-8")
            )
        except Exception:
            old_cfg = {}
        if isinstance(old_cfg, dict) and old_cfg:
            # Fusion : main_cfg a priorite (ses cles ne sont pas ecrasees).
            # Cas pratique :
            # - Premiere migration : CLIENT_CONFIG_FILE n'existe pas encore,
            #   main_cfg={}, donc merged=old_cfg. Toutes les cles client2
            #   sont preservees.
            # - Coexistence (rare) : si pour une raison quelconque le
            #   neuf existe deja avec quelques cles ecrites par le core
            #   en parallele (ex : le core a sauve avant que le client
            #   migre), main_cfg gagne sur les cles dupliquees. On peut
            #   ainsi perdre des choix utilisateur recents de client2
            #   pour les cles qui existent deja dans le neuf. En
            #   pratique negligeable car la migration se fait au 1er
            #   boot avant que le core n'ait eu le temps d'ecrire.
            # Apres ce merge, on renomme l'ancien fichier en
            # .migrated.bak donc le cas de coexistence prolongee
            # n'arrive pas.
            merged = dict(old_cfg)
            merged.update(main_cfg)
            main_cfg = merged
            # Sauver immediatement la version unifiee dans le canonique
            try:
                CLIENT_CONFIG_FILE.write_text(
                    json.dumps(main_cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                # Renommer l'ancien pour marquer la migration
                bak_path = _LEGACY_CLIENT2_CONFIG.with_suffix(
                    _LEGACY_CLIENT2_CONFIG.suffix + ".migrated.bak"
                )
                _LEGACY_CLIENT2_CONFIG.rename(bak_path)
                print(
                    f"[CONFIG] Migration : {_LEGACY_CLIENT2_CONFIG.name} -> "
                    f"{CLIENT_CONFIG_FILE.name} (ancien renomme {bak_path.name})"
                )
            except Exception as e:
                print(
                    f"[CONFIG] Echec migration : {e}", file=sys.stderr
                )

    return main_cfg


# Cles gerees par le core (via _core._save_client_cfg) et qui ne doivent
# PAS etre re-ecrites par le client via _save_cfg(self._cfg). Sinon le
# client ecrase avec une valeur potentiellement obsolete (chargee au boot
# dans self._cfg, mais modifiee depuis par le core en cours de session).
# Bug d'origine : Overlay reste a ON apres relance meme si l'utilisateur
# l'avait mis a OFF avant de fermer.
# Cette liste doit rester synchronisee avec les cles ecrites par le core
# (chercher 'core_cfg[' et '_save_client_cfg' dans ce fichier pour
# l'inventaire complet).
_CORE_MANAGED_CFG_KEYS = frozenset({
    # Overlays
    "overlays_active", "overlays_config", "overlays_show",
    # OCR
    "ocr_force_cpu", "zone_coords", "zone_source",
    # Gamelog SC
    "gamelog_path",
    # Mode RP
    "rp_mode",
    # Hotkeys (8 raccourcis)
    "radio_key", "profile_radio_key",
    "mute_mic_key", "mute_prox_key", "mute_radio_key", "mute_all_key",
    "proximity_short_key", "cycle_channel_key",
})


def _save_cfg(cfg: dict) -> None:
    """Sauvegarde la config dans CLIENT_CONFIG_FILE.

    IMPORTANT : on fusionne avec le contenu actuel sur disque AVANT
    d'ecrire. Raison : core (via _save_client_cfg) ecrit aussi dans le
    meme fichier pour des cles distinctes (zone_coords, radio_key,
    overlays, etc.). Si on faisait write_text(json.dumps(cfg)), on
    ecraserait toutes les cles que core aurait posees depuis le dernier
    chargement par le client. Le merge garantit que les cles non
    presentes dans `cfg` sont preservees telles qu'elles sont sur
    disque.

    Strategie merge : disque + cfg (purge des core-managed) avec cfg
    qui gagne sur ses propres cles.

    Bug fix (Overlay ON au boot apres l'avoir mis OFF) : `cfg` (=
    self._cfg dans le client) est charge UNE FOIS au boot et garde
    en memoire les valeurs initiales y compris des cles gerees par
    le core (overlays_show, hotkeys, zone_coords, etc.). Si l'utilisateur
    toggle overlays OFF en cours de session, le manager ecrit `False`
    sur disque via _save_client_cfg, mais self._cfg garde `True` en
    memoire. Au close, _save_cfg(self._cfg) refait un merge ou self._cfg
    gagne -> on ecrase avec `True` -> bug. Solution : retirer de `cfg`
    toutes les cles connues comme gerees par le core avant le merge."""
    try:
        on_disk = {}
        if CLIENT_CONFIG_FILE.exists():
            try:
                on_disk = json.loads(
                    CLIENT_CONFIG_FILE.read_text(encoding="utf-8")
                )
                if not isinstance(on_disk, dict):
                    on_disk = {}
            except Exception:
                on_disk = {}
        # Purger de cfg les cles gerees par le core (qui ont leur propre
        # mecanisme de persistance via _core._save_client_cfg). Ces cles
        # sont presentes dans cfg uniquement parce qu'on a tout charge au
        # boot via _load_cfg, mais on ne veut pas qu'elles ecrasent ce
        # que le core a sauve depuis (potentiellement plus recent).
        cfg_purged = {k: v for k, v in cfg.items()
                      if k not in _CORE_MANAGED_CFG_KEYS}
        merged = dict(on_disk)
        merged.update(cfg_purged)
        CLIENT_CONFIG_FILE.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[CONFIG] Echec sauvegarde : {e}", file=sys.stderr)


# Note : _VERSION_STRING est deja defini ligne 572 a partir de
# _VERSION_INFO charge au boot. Pas besoin de le recharger ici.


# ======================================================================
# Shim : adaptateur UI pour les fonctions du client1
# ======================================================================
# Les fonctions du client1 qu'on importe (_audio_ws_loop, _ocr_loop_inner,
# etc.) attendent un objet `ui` avec quelques methodes : set_audio_status,
# update_my_pos, update_min_dist, update_player. Notre MainWindow Qt n'a
# pas tous ces noms. On expose un shim qui forward proprement.
#
# Le shim emet des signaux Qt via la MainWindow pour que les mises a jour
# d'UI se fassent dans le main thread (les fonctions client1 sont
# appelees depuis des threads Python, pas le thread Qt).

class _CoreUIShim(QObject):
    sig_audio_status = Signal(bool, str)
    sig_my_pos = Signal(dict)          # nouvelle position locale (OCR)
    sig_min_dist = Signal(float)       # distance au plus proche joueur
    sig_helmet_state = Signal(bool)    # casque ON/OFF detecte
    sig_sc_running = Signal(bool)      # SC lance (True) / ferme/perdu (False)

    def __init__(self, main_window):
        super().__init__()
        self._mw = main_window
        # Brancher les signaux sur les slots de MainWindow
        self.sig_audio_status.connect(main_window._on_audio_status)
        self.sig_my_pos.connect(main_window._on_my_pos_update)
        self.sig_min_dist.connect(main_window._on_min_dist_update)
        self.sig_helmet_state.connect(main_window._on_helmet_state)
        self.sig_sc_running.connect(main_window._on_sc_running)

    # API attendue par client1 (appellee depuis threads daemon)
    def set_audio_status(self, connected: bool, err: str = ""):
        self.sig_audio_status.emit(bool(connected), str(err) if err else "")

    def update_my_pos(self, pos: dict):
        # Forward la position du joueur local (issue de l'OCR) vers le
        # main thread Qt via signal/slot.
        self.sig_my_pos.emit(pos or {})

    def update_min_dist(self, dist: float):
        try:
            self.sig_min_dist.emit(float(dist))
        except Exception:
            self.sig_min_dist.emit(-1.0)

    def update_player(self, name: str, pos, dist):
        try:
            d = float(dist) if dist is not None else 0.0
        except Exception:
            d = 0.0
        try:
            self._mw._worker.sig_player_pos.emit(name, pos or {}, d)
        except Exception:
            pass

    def update_helmet_state(self, helmet_on: bool):
        """Appele par _gamelog_tail_loop et _helmet_scan_loop
        quand l'etat du casque change. On forward au main thread Qt."""
        self.sig_helmet_state.emit(bool(helmet_on))

    # Methodes appelees ailleurs dans client1 mais qu'on n'utilise PAS en
    # 2c (on n'importe pas les fonctions qui les utilisent). On les laisse
    # en no-op au cas ou un import indirect les declenche.
    def add_player(self, name): pass
    def remove_player(self, name): pass
    def refresh_players(self): pass
    def refresh_channels(self): pass
    def refresh_anonymous_mode(self): pass
    def set_player_offline(self, name, off): pass
    def set_status(self, *a, **kw): pass


# ======================================================================
# State partage
# ======================================================================
# Si le module client1 est importable, on PARTAGE son objet state pour
# que les fonctions OCR / audio importees du client1 (qui referencent
# `state` du client1) voient les memes donnees que nous. C'est plus
# propre que de synchroniser deux objets a chaque tick.
#
# Si le client1 n'est pas dispo (cas degrade : phase 1/2a/2b uniquement),
# on retombe sur une classe State minimale pour que le code 2a/2b
# continue de fonctionner.

if _CORE_AVAILABLE:
    state = _core.state
    # Le client1 ne definit pas tous les attributs comme attributs
    # d'instance, certains restent en class-level avec des defaults.
    # On force quelques-uns dont le client2 a besoin et qui peuvent
    # ne pas avoir ete initialises a class-level :
    if not hasattr(state, "my_pos"):
        state.my_pos = None
    if not hasattr(state, "audio_server_ip"):
        state.audio_server_ip = None  # client2 le set au moment de connecter
    # Flag de shutdown : permet aux threads daemon (OCR, watchdog, audio,
    # heartbeat, gamelog, helmet_scan, volume_safety) de detecter une
    # demande d'arret propre via state.shutdown_requested. Le closeEvent
    # set ce flag, attend brievement, puis force os._exit(0).
    if not hasattr(state, "shutdown_requested"):
        state.shutdown_requested = False
else:
    class State:
        # Fallback minimal si _CORE_AVAILABLE=False (cas degrade : core.py
        # corrompu ou absent apres MAJ partielle). On reproduit ici TOUS
        # les attributs que le client utilise, sinon le 1er message du
        # serveur fait crash AttributeError. Defaults conservateurs :
        # tout False/None/{}/[] pour rester en mode degrade.
        my_pos: Optional[dict] = None
        my_name: str = DEFAULT_NAME
        players: dict = {}
        connected: bool = False
        server_token: str = ""
        ws = None
        ws_loop = None
        zone_coords = None
        # Audio
        audio_io = None
        audio_ws = None
        audio_connected = False
        audio_input_dev = None
        audio_output_dev = None
        audio_muted = False
        audio_server_ip = None
        mute_proximity = False
        mute_radio = False
        # Radio PTT
        radio_key = None
        radio_active = False
        mute_mic_key = None
        mute_prox_key = None
        mute_radio_key = None
        mute_all_key = None
        proximity_short = False
        proximity_short_key = None
        radio_recv_ts: dict = {}
        # Mode RP / casque
        rp_mode = False
        helmet_on = True
        helmet_remote: dict = {}
        # Mode anonyme + canaux
        anonymous_mode = False
        channels_list: list = []
        my_channel = None
        player_channels: dict = {}
        profiles_list: list = []
        my_profile = None
        player_profiles: dict = {}
        player_prox_short: dict = {}
        last_radio_seen_ts: dict = {}
        profile_radio_key = None
        profile_radio_active = False
        cycle_channel_key = None
        # Overlays
        overlays_show = False
        overlays_edit = False
        overlays_active: list = []
        overlays_config: dict = {}
        # SC tail
        sc_running = False
        # Shutdown flag (cf. bug 16 : permettre aux threads daemon de
        # se terminer proprement avant os._exit)
        shutdown_requested = False

    state = State()


# ======================================================================
# Geometrie (helpers reutilises de la phase 1)
# ======================================================================

def _compute_default_size(screen_w: int, screen_h: int) -> tuple[int, int]:
    """Ratios degressifs pour la taille fenetre par defaut.
    Reproduit la logique de client1 (_compute_default_size)."""
    if screen_w >= 3000:
        ratio_w = 0.40
    elif screen_w >= 2200:
        ratio_w = 0.50
    elif screen_w >= 1800:
        ratio_w = 0.50
    else:
        ratio_w = 0.75

    if screen_h >= 1800:
        ratio_h = 0.55
    elif screen_h >= 1300:
        ratio_h = 0.65
    elif screen_h >= 1000:
        ratio_h = 0.75
    else:
        ratio_h = 0.85

    return int(screen_w * ratio_w), int(screen_h * ratio_h)


# ======================================================================
# Worker reseau : QThread + asyncio + websockets
# ======================================================================
# Le worker tourne dans son propre QThread. Il communique avec l'UI
# UNIQUEMENT via Qt signals (thread-safe par construction).
# C'est l'equivalent Qt du couplage "ui.add_player()" du client1, mais
# sans appel direct cross-thread.

class NetWorker(QObject):
    # Signaux UI <- worker (toujours emis depuis le thread worker)
    sig_status = Signal(bool, str)               # connected, message
    sig_player_joined = Signal(str)              # name
    sig_player_left = Signal(str)                # name
    sig_player_pos = Signal(str, dict, float)    # name, pos, dist
    sig_player_offline = Signal(str, bool)       # name, offline?
    sig_players_reset = Signal(list)             # liste de noms (welcome)
    sig_log = Signal(str)                        # ligne de log
    sig_invalid_token = Signal()                 # mauvais MDP serveur
    sig_anonymous_mode = Signal(bool)            # mode anonyme on/off (serveur)
    sig_channels_changed = Signal()              # liste/canal courant a rafraichir

    def __init__(self):
        super().__init__()
        self._stop_requested = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        # Bug fix : flag pour ne pas ecraser le message d'erreur dans
        # le finally de _ws_client. Set a True dans except, lu dans
        # finally, reset a False apres usage.
        self._error_status_emitted = False

    @Slot(str, str, str)
    def run_connect(self, server_ip: str, name: str, token: str):
        """Slot lance via signal depuis le main thread.
        Cree un event loop asyncio dans CE thread et lance _ws_client."""
        self._stop_requested = False
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(
                self._ws_client(server_ip, name, token)
            )
        except Exception as e:
            self.sig_log.emit(f"[NET] Erreur worker : {e}")
            self.sig_status.emit(False, f"Erreur : {e}")
        finally:
            try:
                if self._loop:
                    self._loop.close()
            except Exception:
                pass
            self._loop = None
            state.ws = None
            state.ws_loop = None
            state.connected = False

    def request_stop(self):
        """Appele depuis le main thread. Demande la fermeture propre du WS.
        Le worker retombe sur ws_close puis sort de la coroutine."""
        self._stop_requested = True
        loop = self._loop
        ws = self._ws
        if loop is not None and ws is not None:
            try:
                # Fermeture WS thread-safe : on planifie close() dans le
                # loop du worker depuis le main thread.
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass

    async def _ws_client(self, server_ip: str, name: str, token: str):
        if not _WS_AVAILABLE:
            self.sig_status.emit(False, "Module 'websockets' manquant")
            self.sig_log.emit("[NET] pip install websockets")
            return

        uri = f"ws://{server_ip}:{SERVER_PORT}"
        self.sig_log.emit(f"[NET] Connexion a {uri} (nom={name})...")
        try:
            async with websockets.connect(uri) as ws:
                self._ws = ws
                state.ws = ws
                # asyncio.get_running_loop() au lieu de get_event_loop()
                # qui est deprecated depuis Python 3.10 et emet un
                # DeprecationWarning bruyant en 3.12+. On est dans une
                # coroutine donc get_running_loop() est correct et
                # equivalent.
                state.ws_loop = asyncio.get_running_loop()
                # Bug fix 56 : avant, state.connected = True ici, AVANT
                # l'envoi du join. Si ws.send echouait apres le handshake
                # mais avant le join, d'autres threads (audio, heartbeat)
                # pouvaient voir connected=True et tenter d'envoyer sur
                # un socket mort. On marque connected=True seulement APRES
                # que le join a ete envoye avec succes (cf. plus bas).
                state.my_name = name
                state.server_token = token
                # Renommer le fichier de log avec le pseudo joueur (sinon
                # tous les logs s'ecrasent dans circusvoip_debug.log generique).
                # Format final : circusvoip_debug_<Pseudo>_JJMMAAAA_HHMMSS.log
                if _CORE_AVAILABLE:
                    try:
                        _core._set_log_player_name(name)
                    except Exception:
                        pass

                # Envoi du join. channel=None car 2a ne gere pas les canaux
                # (sera ajoute en 2c).
                await ws.send(json.dumps({
                    "type": "join",
                    "name": name,
                    "token": token,
                    "channel": None,
                }))

                # Bug fix 56 : marquer connected=True UNIQUEMENT apres
                # que le join est passe sans exception. Idem pour
                # sig_status (informe l'UI principale).
                state.connected = True
                self.sig_status.emit(True, server_ip)

                # Envoyer notre etat casque au serveur juste apres le join.
                # Sans ca, le serveur initialise helmet_on=False par defaut,
                # et les autres clients qui se connecteront ensuite recevront
                # False dans le welcome, alors que notre client demarre avec
                # helmet_on=True par defaut. Consequence sans fix : Mode RP
                # des autres clients ne filtre pas notre voix tant qu'on a
                # pas explicitement change l'etat casque (Game.log helmet
                # event ou fin de scan boussole).
                # Regression introduite lors du split client legacy -> core/client
                # (cette ligne existait au legacy ~4685-4699 et a ete oubliee
                # lors du refactor).
                try:
                    await ws.send(json.dumps({
                        "type": "helmet",
                        "helmet_on": bool(state.helmet_on),
                    }))
                except Exception:
                    pass

                async for raw in ws:
                    if self._stop_requested:
                        break
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    self._handle_message(data, name)
        except Exception as e:
            self.sig_log.emit(f"[NET] Connexion echouee : {e}")
            self.sig_status.emit(False, f"Erreur : {e}")
            # Bug fix : avant, le finally en dessous emettait
            # sig_status(False, "") qui ECRASAIT le message d'erreur.
            # L'utilisateur voyait l'erreur 1ms puis "Deconnecte" sans
            # contexte. On marque ici qu'on a deja emis un message
            # d'erreur, et le finally le respecte.
            self._error_status_emitted = True
        finally:
            self._ws = None
            state.ws = None
            state.ws_loop = None
            state.connected = False
            # Si on a deja emis un message d'erreur dans except,
            # on ne le remplace pas par "" (qui s'afficherait comme
            # juste "Deconnecte" sans cause). Sinon (deconnexion
            # normale), on emet un statut vide comme avant.
            if not getattr(self, "_error_status_emitted", False):
                self.sig_status.emit(False, "")
            self._error_status_emitted = False
            self.sig_log.emit("[NET] Deconnecte")

    def _handle_message(self, data: dict, my_name: str):
        msg_type = data.get("type")

        if msg_type == "error":
            reason = data.get("reason", "")
            self.sig_log.emit(f"[NET] error : {reason}")
            if reason == "invalid_token":
                self.sig_invalid_token.emit()
                self._stop_requested = True
            return

        if msg_type == "welcome":
            # Etat anonymous transmis au join (peut etre absent = False)
            try:
                state.anonymous_mode = bool(data.get("anonymous_mode", False))
            except Exception as e:
                state.anonymous_mode = False
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[NET] welcome anonymous_mode parse KO : {e}"
                        )
                    except Exception:
                        pass

            # Liste des canaux et profils (admin) + mes valeurs
            try:
                channels = data.get("channels", [])
                if _CORE_AVAILABLE and hasattr(_core, "_normalize_channels"):
                    state.channels_list = _core._normalize_channels(channels)
                else:
                    state.channels_list = [
                        c if isinstance(c, str) else c.get("name", "")
                        for c in channels
                    ]
                state.profiles_list = list(data.get("profiles", []))
                state.my_channel = data.get("my_channel")
                state.my_profile = data.get("my_profile")
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[NET] welcome channels/profiles parse KO : {e}"
                        )
                    except Exception:
                        pass

            # Players
            players = data.get("players", [])
            names = []
            state.players.clear()
            state.player_channels = {}
            state.player_profiles = {}
            state.player_prox_short = {}
            # Bug fix : helmet_remote etait oublie ici, ce qui laissait
            # vivre les vieilles entrees de joueurs absents apres une
            # reconnexion (deco/reco). Mineur en pratique mais incoherent.
            state.helmet_remote = {}
            for p in players:
                pname = p.get("name")
                if not pname:
                    continue
                # pos_received_ts : timestamp monotonic de la derniere
                # position recue. Lu par _volume_safety_loop pour considerer
                # une position comme perimee si elle a plus de
                # POS_STALE_TIMEOUT secondes (= l'autre joueur a freeze son
                # OCR ou perdu la connexion sans signal sc_offline).
                # Si on a une pos au welcome, on considere qu'elle vient
                # d'arriver maintenant (compromis : peut-etre qu'elle a
                # quelques secondes mais on n'a pas l'info exacte).
                state.players[pname] = {
                    "pos": p.get("pos"),
                    "dist": None,
                    "sc_online": True,
                    "pos_received_ts": time.monotonic() if p.get("pos") else 0.0,
                }
                state.player_channels[pname] = p.get("channel")
                state.player_profiles[pname] = p.get("profile")
                state.player_prox_short[pname] = bool(p.get("prox_short", False))
                state.helmet_remote[pname] = bool(p.get("helmet_on", False))
                names.append(pname)

            self.sig_log.emit(
                f"[NET] welcome : {len(names)} joueur(s) "
                f"canal={state.my_channel!r} profil={state.my_profile!r}"
                f"{' [anon]' if state.anonymous_mode else ''}"
            )
            self.sig_anonymous_mode.emit(bool(state.anonymous_mode))
            self.sig_players_reset.emit(names)
            self.sig_channels_changed.emit()
            # Recalculer le filtre RP avec les etats casque recus
            if _CORE_AVAILABLE and hasattr(_core, "_update_rp_filter"):
                try:
                    _core._update_rp_filter()
                except Exception as e:
                    try:
                        _core._dbg_log(
                            f"[NET] welcome _update_rp_filter KO : {e}"
                        )
                    except Exception:
                        pass
            return

        if msg_type == "join":
            n = data.get("name")
            if n and n != my_name:
                state.players[n] = {
                    "pos": None,
                    "dist": None,
                    "sc_online": True,
                }
                state.helmet_remote[n] = bool(data.get("helmet_on", False))
                state.player_channels[n] = data.get("channel")
                state.player_profiles[n] = data.get("profile")
                state.player_prox_short[n] = bool(data.get("prox_short", False))
                self.sig_log.emit(f"[NET] join : {n}")
                self.sig_player_joined.emit(n)
            return

        if msg_type == "leave":
            n = data.get("name")
            if n in state.players:
                del state.players[n]
                self.sig_log.emit(f"[NET] leave : {n}")
                self.sig_player_left.emit(n)
            return

        if msg_type == "pos":
            n = data.get("name")
            pos = data.get("pos")
            if n and n != my_name and pos:
                if n not in state.players:
                    state.players[n] = {"sc_online": True}
                    self.sig_player_joined.emit(n)
                state.players[n]["pos"] = pos
                # Timestamp pour _volume_safety_loop : permet de detecter
                # un joueur dont l'OCR a freeze (plus de positions recues
                # depuis POS_STALE_TIMEOUT secondes) et de couper son
                # volume au lieu de le laisser audible avec sa derniere
                # position connue.
                state.players[n]["pos_received_ts"] = time.monotonic()
                # En 2a on n'a pas encore de position locale (state.my_pos),
                # donc dist=0. L'OCR sera ajoute en 2c.
                dist = 0.0
                state.players[n]["dist"] = dist
                self.sig_player_pos.emit(n, pos, dist)
            return

        if msg_type == "sc_offline":
            n = data.get("name")
            if n in state.players:
                state.players[n]["sc_online"] = False
                self.sig_player_offline.emit(n, True)
            return

        if msg_type == "sc_online":
            n = data.get("name")
            if n in state.players:
                state.players[n]["sc_online"] = True
                self.sig_player_offline.emit(n, False)
            return

        if msg_type == "anonymous_mode":
            # Broadcast serveur : le mode anonyme a ete bascule
            try:
                state.anonymous_mode = bool(data.get("active", False))
            except Exception as e:
                state.anonymous_mode = False
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[NET] anonymous_mode parse KO : {e}"
                        )
                    except Exception:
                        pass
            self.sig_log.emit(
                f"[NET] anonymous_mode : "
                f"{'ON' if state.anonymous_mode else 'OFF'}"
            )
            self.sig_anonymous_mode.emit(bool(state.anonymous_mode))
            return

        # Les autres types non encore traites
        if msg_type == "channels_list":
            try:
                channels = data.get("channels", [])
                if _CORE_AVAILABLE and hasattr(_core, "_normalize_channels"):
                    state.channels_list = _core._normalize_channels(channels)
                else:
                    # Fallback : extraire les noms (les channels peuvent etre
                    # des strings ou des dicts {name, ...})
                    state.channels_list = [
                        c if isinstance(c, str) else c.get("name", "")
                        for c in channels
                    ]
                self.sig_log.emit(
                    f"[NET] channels_list : {len(state.channels_list)} canaux"
                )
                self.sig_channels_changed.emit()
            except Exception as e:
                self.sig_log.emit(f"[NET] channels_list KO : {e}")
            return

        if msg_type == "profiles_list":
            try:
                state.profiles_list = list(data.get("profiles", []))
            except Exception:
                pass
            return

        if msg_type == "player_channel":
            # Un joueur (peut-etre nous) a change de canal.
            try:
                pname = data.get("name")
                new_ch = data.get("channel")
                if pname:
                    state.player_channels[pname] = new_ch
                    if pname == my_name:
                        state.my_channel = new_ch
                        self.sig_log.emit(
                            f"[NET] mon canal -> {new_ch or '(aucun)'}"
                        )
                    # Toujours emettre : la combobox rebuild pour soi,
                    # le label de la table rebuild pour ce joueur.
                    self.sig_channels_changed.emit()
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[NET] player_channel KO : {e}")
                    except Exception:
                        pass
            return

        if msg_type == "player_profile":
            # L'admin a assigne/retire un profil a un joueur.
            try:
                pname = data.get("name")
                new_prof = data.get("profile")
                if pname:
                    state.player_profiles[pname] = new_prof
                    self.sig_channels_changed.emit()
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[NET] player_profile KO : {e}")
                    except Exception:
                        pass
            return

        if msg_type == "my_profile":
            # L'admin a modifie mon profil (notification dediee).
            try:
                new_prof = data.get("profile")
                state.my_profile = new_prof
                state.player_profiles[my_name] = new_prof
                self.sig_log.emit(
                    f"[NET] mon profil -> {new_prof or '(aucun)'}"
                )
                self.sig_channels_changed.emit()
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[NET] my_profile KO : {e}")
                    except Exception:
                        pass
            return

        if msg_type == "player_prox_short":
            # Un joueur a bascule son mode chuchotement (5m).
            try:
                pname = data.get("name")
                active = bool(data.get("active", False))
                if pname:
                    state.player_prox_short[pname] = active
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[NET] player_prox_short KO : {e}")
                    except Exception:
                        pass
            return

        if msg_type == "helmet":
            # Etat casque d'un autre joueur (utilise par _update_rp_filter).
            try:
                pname = data.get("name")
                helmet_on = bool(data.get("helmet_on", False))
                if pname:
                    state.helmet_remote[pname] = helmet_on
                    if _CORE_AVAILABLE and hasattr(_core, "_update_rp_filter"):
                        try:
                            _core._update_rp_filter()
                        except Exception as e:
                            try:
                                _core._dbg_log(
                                    f"[NET] helmet _update_rp_filter KO : {e}"
                                )
                            except Exception:
                                pass
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[NET] helmet parse KO : {e}")
                    except Exception:
                        pass
            return

        # Pong : reponse du serveur a notre ping heartbeat (toutes les 10s).
        # Pas d'action requise cote client : le simple fait de recevoir le
        # pong indique que la connexion est vivante. Le timestamp pourrait
        # servir a calculer une latence mais pas necessaire pour l'instant.
        if msg_type == "pong":
            return

        self.sig_log.emit(f"[NET] type inconnu : {msg_type}")


# ======================================================================
# Calibration zone OCR
# ======================================================================
# Reproduit en Qt les classes Tk RegionSelector (client1 ligne 6201) et
# pick_monitor_interactive (client1 ligne 1037).
#
# IMPORTANT : on travaille en COORDONNEES PHYSIQUES (pixels reels), pas
# en coordonnees logiques Qt. Raison : la zone OCR doit etre passee a
# mss qui scrute l'ecran en pixels physiques (avec PER_MONITOR_AWARE_V2,
# mss voit du 3840x2160 sur un 4K@150% par exemple). Si on lui donnait
# des coordonnees logiques Qt (2560x1440 sur le meme ecran), la zone
# capturee serait decalee.
#
# Conversion : on multiplie les positions/tailles Qt par
# screen.devicePixelRatio() pour obtenir les valeurs physiques.

class MonitorPickerWindow(QWidget):
    """Fenetre semi-transparente plein-ecran sur UN moniteur. Click ->
    selectionne ce moniteur. Echap -> annule.
    Affichee en parallele sur chaque ecran via plusieurs instances.
    Communication : signal global sig_picked porte le dict mss du moniteur."""

    sig_picked = Signal(object)  # dict | None (None = annulation)

    def __init__(self, mon: dict, index: int, total: int):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                          | Qt.Tool)
        self._mon = mon
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setWindowOpacity(0.75)
        # Geometrie en coords logiques Qt : on doit divisier par DPR
        # parce que mon["left"]/etc sont en pixels physiques (mss).
        # On retrouve l'ecran Qt qui correspond a ce mon mss.
        target_screen = self._find_qt_screen_for_mss_mon(mon)
        if target_screen is not None:
            geom = target_screen.geometry()
            self.setGeometry(geom)
        else:
            # Fallback : on utilise les coords mss telles quelles
            self.setGeometry(mon["left"], mon["top"],
                             mon["width"], mon["height"])

        self.setStyleSheet("background: #0066aa;")

        v = QVBoxLayout(self)
        lbl = QLabel(
            f"ECRAN {index+1} / {total}\n\n"
            f"{mon['width']} x {mon['height']}\n\n"
            f"Sur quel ecran se trouve\nStar Citizen ?"
        )
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            "color: white; font-family: Consolas, monospace; "
            "font-size: 22pt; font-weight: bold;"
        )
        v.addWidget(lbl)
        self.setCursor(QCursor(Qt.PointingHandCursor))

    @staticmethod
    def _find_qt_screen_for_mss_mon(mon: dict):
        """Trouve le QScreen qui correspond au moniteur mss (en pixels
        physiques). Avec PER_MONITOR_AWARE_V2, geometry Qt est en pixels
        logiques mais position absolue Qt = position physique / DPR.
        On compare en multipliant Qt par DPR."""
        for scr in QGuiApplication.screens():
            g = scr.geometry()
            dpr = scr.devicePixelRatio()
            phys_left = int(g.x() * dpr)
            phys_top = int(g.y() * dpr)
            phys_w = int(g.width() * dpr)
            phys_h = int(g.height() * dpr)
            if (phys_left == mon["left"] and phys_top == mon["top"] and
                phys_w == mon["width"] and phys_h == mon["height"]):
                return scr
        return None

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.sig_picked.emit(self._mon)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.sig_picked.emit(None)


class RegionSelectorWindow(QWidget):
    """Fenetre noire semi-transparente plein-ecran sur le moniteur choisi.
    L'utilisateur clique-glisse pour dessiner un rectangle. Au relachement,
    emet sig_done avec un dict {"left", "top", "width", "height"} en
    PIXELS PHYSIQUES (pour que mss puisse l'utiliser tel quel).
    Echap -> annule (sig_done emit None)."""

    sig_done = Signal(object)  # dict | None

    def __init__(self, target_mon: dict, target_screen: QScreen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                          | Qt.Tool)
        self._mon = target_mon
        self._screen = target_screen
        self._dpr = target_screen.devicePixelRatio() if target_screen else 1.0

        # On positionne via la geometry du QScreen (coords logiques)
        if target_screen is not None:
            self.setGeometry(target_screen.geometry())
        else:
            # Fallback : utiliser les coords mss en logique en supposant DPR=1
            self.setGeometry(target_mon["left"], target_mon["top"],
                             target_mon["width"], target_mon["height"])

        self.setWindowOpacity(0.30)
        self.setStyleSheet("background: black;")
        self.setCursor(QCursor(Qt.CrossCursor))
        self.setMouseTracking(True)

        # Etat de selection
        self._dragging = False
        self._start: Optional[QPoint] = None
        self._end: Optional[QPoint] = None

        # Label d'instruction (coords logiques Qt)
        self._lbl = QLabel(self)
        self._lbl.setText(
            "Cliquez-glissez pour selectionner la zone OCR du HUD Star Citizen\n"
            "Echap = annuler"
        )
        self._lbl.setStyleSheet(
            "background: rgba(0,0,0,200); color: #00e5ff; "
            "font-family: Consolas, monospace; font-size: 12pt; "
            "padding: 10px; border: 1px solid #00e5ff;"
        )
        self._lbl.move(20, 20)
        self._lbl.adjustSize()
        # showFullScreen ne marche pas sur tous les WMs avec FramelessHint,
        # on reste en show() simple : la geometry est deja celle de l'ecran.

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._start = event.position().toPoint()
            self._end = self._start
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._dragging:
            self._end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._end = event.position().toPoint()
            # Coords logiques Qt
            x1 = min(self._start.x(), self._end.x())
            y1 = min(self._start.y(), self._end.y())
            x2 = max(self._start.x(), self._end.x())
            y2 = max(self._start.y(), self._end.y())
            w_log = x2 - x1
            h_log = y2 - y1
            if w_log < 20 or h_log < 10:
                # Trop petit : on annule le rectangle, l'utilisateur peut
                # recommencer sans fermer la fenetre.
                self._start = None
                self._end = None
                self.update()
                return
            # Conversion en coords physiques pour mss :
            # on ajoute la position de l'ecran (en physique) et on
            # multiplie la taille par DPR.
            # screen.geometry().x() est en logique -> *DPR pour physique.
            scr_x_phys = int(self._screen.geometry().x() * self._dpr)
            scr_y_phys = int(self._screen.geometry().y() * self._dpr)
            phys = {
                "left":   scr_x_phys + int(x1 * self._dpr),
                "top":    scr_y_phys + int(y1 * self._dpr),
                "width":  int(w_log * self._dpr),
                "height": int(h_log * self._dpr),
                # Gamma : meme heuristique que auto_ocr_zone du client1
                "gamma":  0.3 if self._mon["width"] >= 3000 else 0.5,
            }
            self.sig_done.emit(phys)
            self.close()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.sig_done.emit(None)
            self.close()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._start and self._end:
            p = QPainter(self)
            pen = QPen(QColor("#00e5ff"))
            pen.setWidth(2)
            p.setPen(pen)
            x1 = min(self._start.x(), self._end.x())
            y1 = min(self._start.y(), self._end.y())
            x2 = max(self._start.x(), self._end.x())
            y2 = max(self._start.y(), self._end.y())
            p.drawRect(x1, y1, x2 - x1, y2 - y1)
            # Bug fix : p.end() manquait. Coherent avec les 3 autres
            # paintEvent du fichier (VUMeterWithGate, MicLevelRow,
            # _make_eye_icon) qui appellent tous p.end(). Sans ca, le
            # rendu peut etre incomplet sur Windows si le GC est lent.
            p.end()


class CalibrationFlow(QObject):
    """Orchestrateur de la calibration manuelle.

    Plus de MonitorPicker : l'utilisateur trace directement la zone, et on
    detecte automatiquement sur quel ecran il a trace en regardant la
    position du rectangle final. C'est plus simple et plus juste : si SC
    est sur l'ecran 2, l'utilisateur peut tracer sur l'ecran 2 sans avoir
    a le declarer d'abord.

    On lance un RegionSelectorWindow par ecran (chacun couvre son moniteur),
    et on connecte chacun au meme slot _on_region_done. Le premier qui se
    termine gagne, on ferme tous les autres.
    """

    sig_calibrated = Signal(object)  # dict | None

    def __init__(self, parent_window):
        super().__init__()
        self._parent = parent_window
        self._selectors: list[RegionSelectorWindow] = []

    def start(self):
        if not _SCO_AVAILABLE:
            self.sig_calibrated.emit(None)
            return
        try:
            mons = _sco.list_monitors()
        except Exception:
            mons = []
        if not mons:
            self.sig_calibrated.emit(None)
            return

        # Masquer la fenetre principale pour ne pas etre genee
        try:
            self._parent.hide()
        except Exception:
            pass
        # Petit delai pour laisser le compositor cacher la fenetre avant
        # d'ouvrir les selectors plein ecran
        QTimer.singleShot(150, lambda: self._open_all_selectors(mons))

    def _open_all_selectors(self, mons):
        for mon in mons:
            target_screen = MonitorPickerWindow._find_qt_screen_for_mss_mon(mon)
            if target_screen is None:
                continue
            sel = RegionSelectorWindow(mon, target_screen)
            sel.sig_done.connect(self._on_region_done)
            sel.show()
            self._selectors.append(sel)
        # Donner le focus au premier (necessaire pour que Echap reponde)
        if self._selectors:
            self._selectors[0].activateWindow()
            self._selectors[0].setFocus()

    def _on_region_done(self, zone):
        # Fermer tous les autres selectors (un seul gagne)
        for sel in self._selectors:
            try:
                sel.close()
            except Exception:
                pass
        self._selectors = []
        try:
            self._parent.show()
        except Exception:
            pass
        self.sig_calibrated.emit(zone)


# ======================================================================
# Capture de touche pour Radio PTT
# ======================================================================
# Popup modale Qt qui demarre temporairement un listener pynput, capture
# le premier appui clavier OU bouton souris, l'affiche, puis ferme.
# Format de retour identique a celui du client1 :
#   - Touche clavier      : "a", "v", "ctrl", "num7", "f1"...
#   - Bouton souris       : "mouse:left", "mouse:right", "mouse:x1", "mouse:x2"

class KeyCaptureDialog(QDialog):
    """Dialog modale pour capturer une touche/bouton, OU une combinaison
    de touches (ex: ctrl+shift+m, ctrl+mouse:x1).

    Mode capture parallele (a la Discord/TeamSpeak) : l'utilisateur
    maintient toutes les touches de sa combo simultanement, le dialog
    accumule les press et fige la combo au premier release.

    Format de retour identique au format de stockage (cf. core.py
    canonicalize_hotkey) :
      - Simple touche  : "a", "v", "ctrl_l", "num7", "f1", "mouse:x1"
      - Combinaison    : "ctrl+m", "ctrl+shift+m", "ctrl+mouse:x1"
    Les touches modifieurs (ctrl, shift, alt) sont normalisees sans
    suffixe L/R dans les combos.
    """

    # Signaux thread-safe entre listener pynput et thread Qt main.
    # press : ajoute une touche au set de capture en cours
    # release : declenche la finalisation de la combo
    sig_key_pressed = Signal(str)
    sig_key_released = Signal(str)

    def __init__(self, parent, label: str):
        super().__init__(parent)
        self.setWindowTitle(f"Raccourci - {label}")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumSize(500, 200)
        # Resultat final (canonicalise au moment du finalize)
        self.captured: Optional[str] = None
        self._kb_listener = None
        self._mouse_listener = None
        # Set des touches actuellement enfoncees pendant la capture
        # (au format brut : 'ctrl_l', 'm', 'mouse:x1'). Mis a jour par
        # les slots _on_press_received / _on_release_received qui
        # tournent dans le thread Qt (donc thread-safe).
        self._pressed_during_capture: set[str] = set()
        # Snapshot de l'etat au moment ou le 1er release arrive : sert
        # a figer la combo finale (sinon le release des modifieurs un
        # par un fait shrink le set).
        self._frozen_combo: Optional[str] = None
        # Flag : True une fois la combo figee, ignore tous les press/release
        # suivants pour eviter les surprises (ex: l'utilisateur tape autre
        # chose pendant les 400ms d'affichage).
        self._already_captured = False

        # Connecter les signaux au handlers main thread
        self.sig_key_pressed.connect(self._on_press_received)
        self.sig_key_released.connect(self._on_release_received)

        v = QVBoxLayout(self)
        v.setSpacing(10)

        lbl_intro = QLabel(
            f"Definissez le raccourci pour : {label}\n\n"
            "Maintenez les touches simultanement (ex: Ctrl + Shift + M).\n"
            "Relachez pour valider. Boutons souris acceptes (sauf clic gauche)."
        )
        lbl_intro.setWordWrap(True)
        v.addWidget(lbl_intro)

        self.lbl_status = QLabel("En attente...")
        self.lbl_status.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11pt; "
            "padding: 8px; background: #222; color: #88dd88; "
            "border: 1px solid #444;"
        )
        self.lbl_status.setAlignment(Qt.AlignCenter)
        v.addWidget(self.lbl_status)

        v.addStretch(1)

        h = QHBoxLayout()
        self.btn_clear = QPushButton("Effacer (aucune touche)")
        self.btn_clear.clicked.connect(self._on_clear)
        h.addWidget(self.btn_clear)
        h.addStretch(1)
        self.btn_cancel = QPushButton("Annuler")
        self.btn_cancel.clicked.connect(self.reject)
        h.addWidget(self.btn_cancel)
        v.addLayout(h)

        # Demarrer les listeners pynput
        self._start_listeners()

    def _start_listeners(self):
        if not _CORE_AVAILABLE:
            self.lbl_status.setText("Module client1 indisponible.")
            return
        try:
            from pynput import keyboard as kb, mouse as ms
        except ImportError:
            self.lbl_status.setText(
                "Module pynput manquant. pip install pynput"
            )
            return

        def on_press(key):
            try:
                norm = _core._normalize_pynput_key(key)
                if norm and not self._already_captured:
                    # Signal Qt thread-safe : update du set est fait
                    # dans le slot _on_press_received (main thread).
                    self.sig_key_pressed.emit(norm)
            except Exception as e:
                try:
                    _core._dbg_log(f"[KEYCAPTURE] on_press exception: {e}")
                except Exception:
                    pass
            return True

        def on_release(key):
            try:
                norm = _core._normalize_pynput_key(key)
                if norm and not self._already_captured:
                    self.sig_key_released.emit(norm)
            except Exception:
                pass
            return True

        def on_click(x, y, button, pressed):
            try:
                btn_name = button.name
            except Exception:
                btn_name = str(button)
            if btn_name == "left":
                return True  # ignorer le clic gauche (utilise pour valider)
            mouse_str = f"mouse:{btn_name}"
            if self._already_captured:
                return False
            if pressed:
                self.sig_key_pressed.emit(mouse_str)
            else:
                self.sig_key_released.emit(mouse_str)
            return True

        self._kb_listener = kb.Listener(
            on_press=on_press, on_release=on_release
        )
        self._kb_listener.daemon = True
        self._mouse_listener = ms.Listener(on_click=on_click)
        self._mouse_listener.daemon = True
        self._kb_listener.start()
        self._mouse_listener.start()
        try:
            _core._dbg_log("[KEYCAPTURE] Listeners pynput demarres (mode combo)")
        except Exception:
            pass

    def _stop_listeners(self):
        for lst in (self._kb_listener, self._mouse_listener):
            if lst is not None:
                try:
                    lst.stop()
                except Exception:
                    pass
        self._kb_listener = None
        self._mouse_listener = None
        try:
            _core._dbg_log("[KEYCAPTURE] Listeners pynput stoppes")
        except Exception:
            pass

    def _build_combo_str(self, pressed: set) -> str:
        """Construit la string canonique a partir d'un set de touches
        actuellement pressees. Delegue a core.canonicalize_hotkey."""
        if not pressed:
            return ""
        try:
            return _core.canonicalize_hotkey("+".join(sorted(pressed)))
        except Exception:
            return "+".join(sorted(pressed))

    def _refresh_status(self):
        """Met a jour le label de statut en live pendant la capture."""
        if self._already_captured:
            return
        if not self._pressed_during_capture:
            self.lbl_status.setText("En attente...")
            return
        combo = self._build_combo_str(self._pressed_during_capture)
        try:
            pretty = _core.format_hotkey_for_display(combo)
        except Exception:
            pretty = combo
        self.lbl_status.setText(f"En cours : {pretty}")

    @Slot(str)
    def _on_press_received(self, key_str: str):
        """Slot main thread : ajoute une touche au set de capture."""
        if self._already_captured:
            return
        self._pressed_during_capture.add(key_str)
        self._refresh_status()

    @Slot(str)
    def _on_release_received(self, key_str: str):
        """Slot main thread : finalise la combo au PREMIER release.

        Strategie : au moment du 1er release, on fige la combo telle
        qu'elle est dans _pressed_during_capture (avant qu'on retire
        key_str). Comme ca, meme si l'utilisateur relache d'abord un
        modifieur, la combo complete est preservee.
        """
        if self._already_captured:
            return
        # Si key_str n'etait pas dans le set, c'est un release fantome
        # (ex: touche releve avant la fenetre, ou release d'une touche
        # qu'on n'a jamais vu pressee). On ignore.
        if key_str not in self._pressed_during_capture:
            return
        # Ne pas finaliser si rien de "valide" : on doit avoir au moins
        # une touche non-modifieur OU une combinaison comportant que des
        # modifieurs (rare mais possible : 'ctrl' tout seul = PTT modifieur).
        # En pratique on accepte tout ce qui est non-vide.
        if not self._pressed_during_capture:
            return
        # Figer la combo MAINTENANT (avant de retirer key_str du set).
        combo = self._build_combo_str(self._pressed_during_capture)
        if not combo:
            return
        self._already_captured = True
        self.captured = combo
        try:
            pretty = _core.format_hotkey_for_display(combo)
        except Exception:
            pretty = combo
        self.lbl_status.setText(f"Capture : {pretty}")
        try:
            _core._dbg_log(
                f"[KEYCAPTURE] Combo finalisee : {combo!r} "
                f"(pressed={self._pressed_during_capture})"
            )
        except Exception:
            pass
        # Arret des listeners + delai avant accept (laisser voir le
        # resultat). Comme on est dans un slot Qt thread, le
        # singleShot(self.accept) marche.
        self._stop_listeners()
        QTimer.singleShot(400, self.accept)

    def _on_clear(self):
        self._stop_listeners()
        self._already_captured = True
        self.captured = ""  # chaine vide = "aucune touche"
        self.accept()

    def closeEvent(self, event):
        self._stop_listeners()
        super().closeEvent(event)


# ======================================================================
# Popup de saisie chemin Game.log
# ======================================================================

class GameLogPathDialog(QDialog):
    """Demande a l'utilisateur le chemin du dossier LIVE/PTU de Star Citizen
    quand _find_gamelog() ne le trouve pas tout seul. Le chemin valide est
    sauvegarde dans circusvoip_client_config.json sous la cle 'gamelog_path'.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Chemin Star Citizen")
        self.setModal(True)
        self.setMinimumSize(540, 220)
        self.validated_path: Optional[str] = None

        v = QVBoxLayout(self)
        v.setSpacing(8)

        v.addWidget(QLabel("Game.log introuvable automatiquement."))
        v.addWidget(QLabel(
            "Indique le dossier LIVE (ou PTU, EPTU, etc.) de ton "
            "installation Star Citizen.\n"
            "Exemple : C:\\Program Files\\Roberts Space Industries\\StarCitizen\\LIVE"
        ))

        h = QHBoxLayout()
        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("Chemin du dossier LIVE...")
        h.addWidget(self.ed_path)
        self.btn_browse = QPushButton("Parcourir...")
        self.btn_browse.clicked.connect(self._on_browse)
        h.addWidget(self.btn_browse)
        v.addLayout(h)

        self.lbl_err = QLabel("")
        self.lbl_err.setStyleSheet("color: #ff6666;")
        self.lbl_err.setWordWrap(True)
        v.addWidget(self.lbl_err)

        v.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_validate)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _on_browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choisir le dossier LIVE")
        if d:
            self.ed_path.setText(d)

    def _on_validate(self):
        path = self.ed_path.text().strip()
        if not path:
            self.lbl_err.setText("Chemin vide.")
            return
        # On verifie que le chemin existe et qu'il contient Game.log
        # (ou pourrait le contenir : on accepte que SC ne tourne pas encore)
        if not os.path.isdir(path):
            self.lbl_err.setText("Le dossier n'existe pas.")
            return
        # Verifier la presence de Data.p4k ou de StarCitizen.exe pour
        # confirmer que c'est bien un dossier LIVE/PTU
        candidates = [
            os.path.join(path, "Data.p4k"),
            os.path.join(path, "StarCitizen.exe"),
            os.path.join(path, "Bin64", "StarCitizen.exe"),
        ]
        if not any(os.path.exists(c) for c in candidates):
            self.lbl_err.setText(
                "Ce dossier ne ressemble pas a un dossier LIVE/PTU. "
                "Cherchez celui qui contient Data.p4k ou StarCitizen.exe."
            )
            return
        self.validated_path = path
        self.accept()


# ======================================================================
# Overlays floating (mutes / channel / prox_range)
# ======================================================================
# Reproduit en Qt les 3 overlays Tk du client1 (lignes 8628+, 8893+, 9026+).
# Differences voulues vs le client1 :
#   - Transparence Qt native (Qt.WA_TranslucentBackground) au lieu du
#     hack Tk transparentcolor magenta. Plus propre, pas de halo violet.
#   - Pas d'orientation horizontale pour 'mutes' (vertical par defaut).
#     Si vous voulez horizontal, c'est le bouton rotate qui est skippe ;
#     a reactiver plus tard si besoin.
#   - Une classe OverlayWindow generique parametree par ov_id, au lieu
#     de 3 _build_overlay_X distincts.
#
# Compatible config client1 :
#   - cfg["overlays_active"] : liste des ids actifs (["mutes", "channel",...])
#   - cfg["overlays_config"] : {ov_id: {"x": int, "y": int, "size": int}}
# Les positions/tailles sont LUES depuis circusvoip_client_config.json
# (le config du client1) pour que vous n'ayez pas a tout reconfigurer.
# Les modifications sont ECRITES dans le meme config.

OVERLAY_CATALOG = ("mutes", "channel", "prox_range")


class OverlayWindow(QWidget):
    """Fenetre flottante topmost semi-transparente. Type d'overlay defini
    par ov_id. Mode edition montre header (drag/active/close) + footer
    (resize +/-). Mode normal montre juste le body."""

    # Signaux : la MainWindow ecoute pour persister
    sig_moved = Signal(str, int, int)        # ov_id, new x, new y (body)
    sig_resized = Signal(str, int)           # ov_id, new size (1..3)
    sig_active_toggled = Signal(str, bool)   # ov_id, active?

    def __init__(self, ov_id: str, is_edit: bool, is_active: bool,
                 cfg: dict, main_window):
        # On donne main_window comme parent (comme Tk Toplevel(parent_root))
        # mais on garde Qt.Window pour que ce soit une vraie top-level
        # window independante (pas un widget enfant). Qt.Tool donnait des
        # fenetres parfois invisibles sous Windows avec Frameless ; Qt.Window
        # est plus fiable.
        super().__init__(main_window,
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint)
        self._ov_id = ov_id
        self._is_edit = is_edit
        self._is_active = is_active
        self._size = max(1, min(3, int(cfg.get("size", 1))))
        self._cfg = cfg
        self._mw = main_window
        self._dragging = False
        self._drag_start_global: Optional[QPoint] = None
        self._drag_start_window: Optional[QPoint] = None

        # La window elle-meme n'a PAS de fond : transparente. Seuls les
        # widgets internes (header/body/footer) ont leur fond #1a1a1a +
        # bordure #444. Sans ca, on avait un sandwich visuel
        # "contour gris / bande noire / contour gris" : la bande noire
        # etait le fond #1a1a1a de la window qui depassait autour du body.
        #
        # WA_TranslucentBackground active la transparence Qt native ;
        # WA_NoSystemBackground evite que Qt repeigne le fond a chaque
        # repaint (sinon on voit clignoter en arriere-plan).
        self.setObjectName("OverlayWindow")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        # QSS minimal : on force background transparent sur la window
        # (surclasse le theme global qui mettrait un fond) et on style
        # les QLabel internes (qui sont tous "contenu" sur le body opaque,
        # donc background transparent).
        self.setStyleSheet(
            "QWidget#OverlayWindow { background: transparent; }"
            "QWidget#OverlayWindow QLabel { background: transparent; "
            "  color: #c9d1d9; }"
        )
        if is_edit and not is_active:
            # Mode edition + inactif : tres transparent pour bien voir
            # qu'il n'est pas active
            self.setWindowOpacity(0.55)
        else:
            # Actif (que ce soit en edition ou en mode normal) : opaque
            # complet pour la meilleure lisibilite en jeu.
            self.setWindowOpacity(1.0)

        # Calculer la largeur du body en avance pour pouvoir contraindre
        # le header et footer a la meme largeur. Sinon ils s'etalent et
        # rendent l'overlay disgracieux en mode edit.
        if ov_id == "mutes":
            cell = 20 + 15 * self._size  # 35 / 50 / 65
            body_w = cell + 8  # cellule + marge
            self._body_h = (cell + 2) * 3 + 4
        elif ov_id == "channel":
            self._body_h = 40 + 8 * self._size
            body_w = 90 + 30 * self._size
        elif ov_id == "prox_range":
            self._body_h = 40 + 8 * self._size
            body_w = 90 + 30 * self._size
        else:
            body_w, self._body_h = 100, 40

        # Pas de largeur minimum imposee : la fenetre garde la largeur
        # exacte du body, qu'on soit en mode edit ou normal. Comme ca,
        # un overlay colle au bord droit de l'ecran en mode edit reste
        # colle en mode normal (pas de decalage fantome a cause d'un
        # header plus large que le body).
        # Pour 'mutes' taille 1 : body=43 px, le header devient tres
        # serre mais ✚ + ✕ a 11pt tiennent encore (~18 px chacun).
        self._body_w = body_w
        self.setFixedWidth(self._body_w)

        # Layout vertical : header (edit) + body + footer (edit)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        if is_edit:
            self._build_header(v)
        self._build_body(v)
        if is_edit:
            self._build_footer(v)

        # Calculer la taille finale et positionner
        self.adjustSize()
        # Pas de minimumWidth artificiel : la largeur du body suffit.

        # Position : cfg.x/y stocke la position du BODY en pixels PHYSIQUES
        # (le client1 sauvegarde en physique car il a active
        # SetProcessDpiAwareness(2)). Qt utilise des pixels LOGIQUES qui
        # different sur les ecrans HiDPI (un 4K@150% a DPR=1.5, donc
        # logique = physique / 1.5).
        # On convertit phys -> logique en trouvant le QScreen qui contient
        # la position physique demandee, puis en divisant par son DPR.
        x_phys = cfg.get("x")
        y_phys = cfg.get("y")
        if x_phys is None or y_phys is None:
            x_log, y_log = 200, 200
        else:
            x_log, y_log = self._phys_to_logical(int(x_phys), int(y_phys))
        # Bug fix : avant, on lisait self._header_widget.height() qui
        # peut retourner 0 ou la sizeHint au lieu des 22px definis si
        # Qt n'a pas encore fini son layout. On force l'evaluation via
        # adjustSize() puis on lit sizeHint() (toujours coherent) avec
        # height() en fallback. Le max() protege contre les valeurs
        # nulles intermittentes.
        header_h = 0
        if hasattr(self, "_header_widget"):
            self._header_widget.adjustSize()
            header_h = max(
                self._header_widget.sizeHint().height(),
                self._header_widget.height(),
            )
        self.move(x_log, y_log - header_h)

        # Timer de refresh pour les contenus dynamiques (couleurs M/P/R,
        # nom canal, mode prox). 250ms suffit, c'est de l'affichage.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self._refresh_dynamic)
        self._refresh_timer.start()
        # Refresh une fois immediatement
        self._refresh_dynamic()

    # ------------------------------------------------------------------
    # Build : header / body / footer
    # ------------------------------------------------------------------
    def _build_header(self, parent_layout):
        h = QWidget()
        h.setFixedHeight(22)
        h.setObjectName("OverlayHeader")
        # Selecteur ID pour bloquer la cascade : sinon les QLabel enfants
        # heritent du border et on a un double cadre.
        h.setStyleSheet(
            "QWidget#OverlayHeader { background: #1a1a1a; "
            "border: 1px solid #444; }"
        )
        hl = QHBoxLayout(h)
        hl.setContentsMargins(3, 0, 3, 0)
        hl.setSpacing(2)

        # Drag handle (✚)
        self._drag_handle = QLabel("✚")
        self._drag_handle.setStyleSheet(
            "color: #cccccc; font-size: 11pt; font-weight: bold;"
        )
        self._drag_handle.setCursor(QCursor(Qt.SizeAllCursor))
        hl.addWidget(self._drag_handle)
        hl.addStretch(1)

        # Bouton activer (✓) ou retirer (✕) selon etat
        if self._is_active:
            btn = QLabel("✕")
            btn.setStyleSheet(
                "color: #ff6666; font-size: 11pt; font-weight: bold;"
            )
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn._target_active = False
        else:
            btn = QLabel("✓")
            btn.setStyleSheet(
                "color: #66dd66; font-size: 11pt; font-weight: bold;"
            )
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn._target_active = True
        btn.mousePressEvent = lambda e, b=btn: self._on_active_btn_clicked(b)
        hl.addWidget(btn)

        self._header_widget = h
        parent_layout.addWidget(h)

    def _build_footer(self, parent_layout):
        f = QWidget()
        f.setFixedHeight(22)
        f.setObjectName("OverlayFooter")
        f.setStyleSheet(
            "QWidget#OverlayFooter { background: #1a1a1a; "
            "border: 1px solid #444; }"
        )
        fl = QHBoxLayout(f)
        fl.setContentsMargins(3, 0, 3, 0)
        fl.setSpacing(2)

        btn_minus = QLabel("−")
        btn_minus.setStyleSheet(
            "color: #cccccc; font-size: 13pt; font-weight: bold;"
        )
        btn_minus.setCursor(QCursor(Qt.PointingHandCursor))
        btn_minus.mousePressEvent = lambda e: self._on_resize(-1)
        fl.addWidget(btn_minus)

        fl.addStretch(1)

        # Affichage taille courante : seulement si la largeur le permet
        # (sinon il deborde et pousse les boutons dehors).
        if self._body_w >= 60:
            lbl_sz = QLabel(f"{self._size}/3")
            lbl_sz.setStyleSheet("color: #888; font-size: 8pt;")
            fl.addWidget(lbl_sz)
            self._lbl_size = lbl_sz
            fl.addStretch(1)

        btn_plus = QLabel("+")
        btn_plus.setStyleSheet(
            "color: #cccccc; font-size: 13pt; font-weight: bold;"
        )
        btn_plus.setCursor(QCursor(Qt.PointingHandCursor))
        btn_plus.mousePressEvent = lambda e: self._on_resize(+1)
        fl.addWidget(btn_plus)

        self._footer_widget = f
        parent_layout.addWidget(f)

    def _build_body(self, parent_layout):
        if self._ov_id == "mutes":
            self._build_body_mutes(parent_layout)
        elif self._ov_id == "channel":
            self._build_body_channel(parent_layout)
        elif self._ov_id == "prox_range":
            self._build_body_prox_range(parent_layout)
        else:
            # Inconnu : placeholder
            lbl = QLabel(f"?{self._ov_id}?")
            lbl.setStyleSheet(
                "background: rgba(26,26,26,230); color: #f88; "
                "padding: 8px;"
            )
            parent_layout.addWidget(lbl)

    def _build_body_mutes(self, parent_layout):
        """3 cellules empilees verticalement : M (mic), P (prox), R (radio).
        Chaque cellule = un carre avec une lettre, couleur selon mute.

        Pas de fond/marges sur le widget body : avec
        WA_TranslucentBackground sur la window, tout pixel non couvert
        par une cellule est totalement transparent (le jeu se voit
        derriere). Comme ca, on n'a pas de "bande grise" autour des
        cellules - juste les 3 cellules avec leur bordure.
        """
        cell = 20 + 15 * self._size  # 35 / 50 / 65
        body = QWidget()
        # Fond transparent (par defaut grace a WA_TranslucentBackground
        # sur la window parent, sauf override par le QSS global qui
        # peut imposer un fond). On force explicit pour etre sur.
        body.setStyleSheet("background: transparent;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        bl.setAlignment(Qt.AlignHCenter)
        self._mute_cells = []
        items = [
            ("M", lambda: getattr(state, "audio_muted", False)),
            ("P", lambda: getattr(state, "mute_proximity", False)),
            ("R", lambda: getattr(state, "mute_radio", False)),
        ]
        for letter, fn in items:
            lbl = QLabel(letter)
            lbl.setFixedSize(cell, cell)
            lbl.setAlignment(Qt.AlignCenter)
            font_pt = max(10, int(cell * 0.45))
            lbl.setStyleSheet(
                f"background: #1a1a1a; "
                f"color: #cccccc; "
                f"font-family: Arial; font-size: {font_pt}pt; "
                f"font-weight: bold; border: 1px solid #444;"
            )
            bl.addWidget(lbl, alignment=Qt.AlignHCenter)
            self._mute_cells.append((lbl, fn))
        parent_layout.addWidget(body)

    def _build_body_channel(self, parent_layout):
        """Affiche le nom du canal courant (state.my_channel)."""
        body_h = 40 + 8 * self._size
        body_w = 90 + 30 * self._size
        body = QWidget()
        body.setFixedSize(body_w, body_h)
        body.setObjectName("OverlayBodyChannel")
        body.setStyleSheet(
            "QWidget#OverlayBodyChannel { background: #1a1a1a; "
            "border: 1px solid #444; }"
        )
        bl = QVBoxLayout(body)
        bl.setContentsMargins(4, 4, 4, 4)
        bl.setSpacing(0)
        title = QLabel("CANAL")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "color: #888; font-family: Consolas, monospace; font-size: 8pt;"
        )
        bl.addWidget(title)
        self._channel_value = QLabel("(aucun)")
        self._channel_value.setAlignment(Qt.AlignCenter)
        font_pt = 10 + 2 * self._size
        self._channel_value.setStyleSheet(
            f"color: #66dd66; font-family: Consolas, monospace; "
            f"font-size: {font_pt}pt; font-weight: bold;"
        )
        bl.addWidget(self._channel_value)
        parent_layout.addWidget(body)

    def _build_body_prox_range(self, parent_layout):
        """Affiche '5 m' ou '30 m' selon state.proximity_short."""
        body_h = 40 + 8 * self._size
        # Bug fix : avant, on calculait localement body_w = 60+20*size
        # (80/100/120) alors que __init__ a deja set self._body_w =
        # 90+30*size (120/150/180) pour la fenetre externe. Resultat :
        # zone vide a droite du "5 m"/"30 m". On reutilise la largeur
        # deja calculee pour rester coherent.
        body_w = self._body_w
        body = QWidget()
        body.setFixedSize(body_w, body_h)
        body.setObjectName("OverlayBodyProxRange")
        body.setStyleSheet(
            "QWidget#OverlayBodyProxRange { background: #1a1a1a; "
            "border: 1px solid #444; }"
        )
        bl = QVBoxLayout(body)
        bl.setContentsMargins(4, 4, 4, 4)
        bl.setSpacing(0)
        title = QLabel("PROX")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "color: #888; font-family: Consolas, monospace; font-size: 8pt;"
        )
        bl.addWidget(title)
        self._prox_value = QLabel("30 m")
        self._prox_value.setAlignment(Qt.AlignCenter)
        font_pt = 12 + 2 * self._size
        self._prox_value.setStyleSheet(
            f"color: #66dddd; font-family: Consolas, monospace; "
            f"font-size: {font_pt}pt; font-weight: bold;"
        )
        bl.addWidget(self._prox_value)
        parent_layout.addWidget(body)

    # ------------------------------------------------------------------
    # Refresh dynamique (timer 250ms)
    # ------------------------------------------------------------------
    def _refresh_dynamic(self):
        try:
            if self._ov_id == "mutes":
                for lbl, fn in self._mute_cells:
                    muted = bool(fn())
                    cell_size = lbl.size().width()
                    color = "#ff7777" if muted else "#77ff77"
                    font_pt = max(10, int(cell_size * 0.45))
                    lbl.setStyleSheet(
                        f"background: #1a1a1a; "
                        f"color: {color}; "
                        f"font-family: Arial; font-size: {font_pt}pt; "
                        f"font-weight: bold; border: 1px solid #444;"
                    )
            elif self._ov_id == "channel":
                ch = getattr(state, "my_channel", None) or "(aucun)"
                self._channel_value.setText(str(ch))
            elif self._ov_id == "prox_range":
                short = bool(getattr(state, "proximity_short", False))
                # Bug fix : avant, le retour 30m gardait la couleur orange
                # car styleSheet().replace("#66dddd","#ffaa44") en mode 5m
                # ne reverse PAS la modification quand on revient en 30m.
                # On reconstruit le styleSheet complet avec la couleur
                # voulue dans les deux branches.
                font_pt = 12 + 2 * self._size
                color = "#ffaa44" if short else "#66dddd"
                self._prox_value.setText("5 m" if short else "30 m")
                self._prox_value.setStyleSheet(
                    f"color: {color}; font-family: Consolas, monospace; "
                    f"font-size: {font_pt}pt; font-weight: bold;"
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Conversion coords physiques <-> logiques Qt
    # ------------------------------------------------------------------
    # Le client1 sauve les positions en pixels physiques (il fait
    # SetProcessDpiAwareness(2) et utilise winfo_x/y qui retourne du
    # physique). Qt avec PER_MONITOR_AWARE_V2 utilise des pixels logiques
    # (= physiques / DPR pour un ecran HiDPI).
    # Sur un 4K@150% : DPR=1.5, physique 0..3840, logique 0..2560.
    # Pour positionner correctement les overlays via QWidget.move() (qui
    # attend du logique), il faut convertir les coords physiques du config.

    @staticmethod
    def _phys_to_logical(x_phys: int, y_phys: int) -> tuple[int, int]:
        """Convertit une coord pixel physique en coord logique Qt."""
        for scr in QGuiApplication.screens():
            geom = scr.geometry()
            dpr = scr.devicePixelRatio()
            phys_left = int(geom.x() * dpr)
            phys_top = int(geom.y() * dpr)
            phys_right = phys_left + int(geom.width() * dpr)
            phys_bottom = phys_top + int(geom.height() * dpr)
            if (phys_left <= x_phys < phys_right and
                phys_top <= y_phys < phys_bottom):
                rel_x_phys = x_phys - phys_left
                rel_y_phys = y_phys - phys_top
                rel_x_log = int(rel_x_phys / dpr)
                rel_y_log = int(rel_y_phys / dpr)
                return geom.x() + rel_x_log, geom.y() + rel_y_log
        # Hors ecran connu : fallback primaire
        primary = QGuiApplication.primaryScreen()
        g = primary.geometry()
        return g.x() + 100, g.y() + 100

    @staticmethod
    def _logical_to_phys(x_log: int, y_log: int) -> tuple[int, int]:
        """Inverse : coord logique Qt -> coord physique pour le config."""
        for scr in QGuiApplication.screens():
            geom = scr.geometry()
            dpr = scr.devicePixelRatio()
            if (geom.x() <= x_log < geom.x() + geom.width() and
                geom.y() <= y_log < geom.y() + geom.height()):
                rel_x_log = x_log - geom.x()
                rel_y_log = y_log - geom.y()
                rel_x_phys = int(rel_x_log * dpr)
                rel_y_phys = int(rel_y_log * dpr)
                phys_left = int(geom.x() * dpr)
                phys_top = int(geom.y() * dpr)
                return phys_left + rel_x_phys, phys_top + rel_y_phys
        return x_log, y_log

    # ------------------------------------------------------------------
    # Drag (header ✚)
    # ------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent):
        if not self._is_edit:
            return
        if event.button() != Qt.LeftButton:
            return
        # On accepte le drag uniquement si le clic est sur le header
        # (le drag handle ✚ ou la barre header en general). Pour faire
        # simple : drag possible si clic dans la zone du header_widget.
        if hasattr(self, "_header_widget"):
            local = event.position().toPoint()
            if self._header_widget.geometry().contains(local):
                self._dragging = True
                self._drag_start_global = event.globalPosition().toPoint()
                self._drag_start_window = self.pos()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._dragging and self._drag_start_global is not None:
            delta = event.globalPosition().toPoint() - self._drag_start_global
            self.move(self._drag_start_window + delta)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._dragging:
            self._dragging = False
            # Sauver la position du BODY en PIXELS PHYSIQUES (compatible
            # client1). body_y_logical = window_y_logical + header_height.
            # Au moment du release, le widget est visible et son height()
            # est fiable, mais on garde le fallback sizeHint() pour
            # coherence avec le reste du fichier.
            header_h = 0
            if hasattr(self, "_header_widget"):
                header_h = max(
                    self._header_widget.sizeHint().height(),
                    self._header_widget.height(),
                )
            body_x_log = self.x()
            body_y_log = self.y() + header_h
            body_x_phys, body_y_phys = self._logical_to_phys(
                body_x_log, body_y_log
            )
            self.sig_moved.emit(self._ov_id, body_x_phys, body_y_phys)

    # ------------------------------------------------------------------
    # Slots boutons
    # ------------------------------------------------------------------
    def _on_resize(self, delta: int):
        new_size = max(1, min(3, self._size + delta))
        if new_size == self._size:
            return
        self.sig_resized.emit(self._ov_id, new_size)

    def _on_active_btn_clicked(self, btn):
        target = getattr(btn, "_target_active", None)
        if target is None:
            return
        self.sig_active_toggled.emit(self._ov_id, bool(target))


class OverlayManager(QObject):
    """Gere l'ouverture/fermeture des overlays selon (overlays_show,
    overlays_edit, overlays_active). Un seul OverlayManager pour le
    client. Persiste les changements dans circusvoip_client_config.json."""

    def __init__(self, main_window):
        super().__init__()
        self._mw = main_window
        self._windows: dict[str, OverlayWindow] = {}
        # Etat (pas dans state global pour eviter les conflits avec
        # client1 si jamais relance)
        self.show_mode = False  # bouton "Overlay"
        self.edit_mode = False  # bouton "Overlay Edition"
        # Liste des actifs et config positions/tailles : on lit depuis
        # circusvoip_client_config.json au boot
        self.active: list[str] = []
        self.cfg: dict = {}
        self._load_from_core_cfg()

    def _load_from_core_cfg(self):
        if not _CORE_AVAILABLE:
            return
        try:
            core_cfg = _core._load_client_cfg()
            self.active = list(core_cfg.get("overlays_active", []))
            self.cfg = dict(core_cfg.get("overlays_config", {}))
            # Bug fix : avant, show_mode n'etait jamais persiste/restaure.
            # A chaque relance, l'utilisateur recommencait a OFF meme s'il
            # avait laisse les overlays affiches. On charge la valeur
            # sauvee (defaut False = comportement historique).
            self.show_mode = bool(core_cfg.get("overlays_show", False))
            # Synchroniser dans state pour que le client1 (si import) voit
            # les memes
            state.overlays_active = list(self.active)
            state.overlays_config = dict(self.cfg)
            state.overlays_show = bool(self.show_mode)
        except Exception as e:
            self.active = []
            self.cfg = {}
            self.show_mode = False
            try:
                self._mw._on_log(f"[OVERLAY] Echec chargement config : {e}")
            except Exception:
                pass

    def _persist(self):
        if not _CORE_AVAILABLE:
            return
        try:
            core_cfg = _core._load_client_cfg()
            core_cfg["overlays_active"] = list(self.active)
            core_cfg["overlays_config"] = dict(self.cfg)
            # Bug fix : persister show_mode (cf. _load_from_core_cfg)
            core_cfg["overlays_show"] = bool(self.show_mode)
            _core._save_client_cfg(core_cfg)
        except Exception as e:
            try:
                self._mw._on_log(f"[OVERLAY] Echec sauvegarde : {e}")
            except Exception:
                pass

    # ---- Toggles boutons UI ----
    # Bug fix : les methodes toggle_show() et toggle_edit() ont ete
    # supprimees ici (elles n'etaient appelees nulle part dans le
    # projet). Le toggle des modes show/edit se fait directement
    # via les setters _on_overlay_show_toggled / _on_overlay_edit_toggled
    # de MainWindow qui set show_mode / edit_mode puis appellent refresh().

    # ---- Rebuild ----
    def refresh(self):
        """Ferme tous les overlays existants puis ouvre ceux qu'il faut.
        - edit ON : ouvre tous les overlays du catalogue
        - edit OFF + show ON : ouvre seulement les actifs
        - edit OFF + show OFF : tout fermer"""
        # 1. Fermer tout
        for ov_id, win in list(self._windows.items()):
            try:
                if hasattr(win, "_refresh_timer"):
                    win._refresh_timer.stop()
                win.close()
                win.deleteLater()
            except Exception:
                pass
        self._windows.clear()

        # 2. Determiner ce qu'il faut afficher
        if self.edit_mode:
            to_show = list(OVERLAY_CATALOG)
        elif self.show_mode:
            to_show = [oid for oid in OVERLAY_CATALOG if oid in self.active]
        else:
            to_show = []

        # 3. Creer les fenetres
        for ov_id in to_show:
            cfg = self.cfg.get(ov_id, {})
            is_active = ov_id in self.active
            try:
                win = OverlayWindow(
                    ov_id, self.edit_mode, is_active, cfg, self._mw
                )
                win.sig_moved.connect(self._on_moved)
                win.sig_resized.connect(self._on_resized)
                win.sig_active_toggled.connect(self._on_active_toggled)
                win.show()
                self._windows[ov_id] = win
            except Exception as e:
                try:
                    self._mw._on_log(f"[OVERLAY] {ov_id} CRASH : {e}")
                    import traceback
                    for line in traceback.format_exc().rstrip().split("\n"):
                        self._mw._on_log(f"  {line}")
                except Exception:
                    pass

    # ---- Slots changements depuis OverlayWindow ----
    @Slot(str, int, int)
    def _on_moved(self, ov_id: str, x: int, y: int):
        c = self.cfg.setdefault(ov_id, {})
        c["x"] = int(x)
        c["y"] = int(y)
        self._persist()

    @Slot(str, int)
    def _on_resized(self, ov_id: str, new_size: int):
        c = self.cfg.setdefault(ov_id, {})
        c["size"] = int(new_size)
        self._persist()
        # Reconstruire pour appliquer la nouvelle taille
        self.refresh()

    @Slot(str, bool)
    def _on_active_toggled(self, ov_id: str, active: bool):
        if active:
            if ov_id not in self.active:
                self.active.append(ov_id)
        else:
            if ov_id in self.active:
                self.active.remove(ov_id)
        # Synchroniser avec state aussi
        state.overlays_active = list(self.active)
        self._persist()
        self.refresh()

    def close_all(self):
        for win in list(self._windows.values()):
            try:
                if hasattr(win, "_refresh_timer"):
                    win._refresh_timer.stop()
                win.close()
                win.deleteLater()
            except Exception:
                pass
        self._windows.clear()


# ======================================================================
# Fenetre principale
# ======================================================================

# ======================================================================
# Popup volume par joueur
# ======================================================================
# Mini popup non-modal qui affiche un slider 0-200% pour regler le volume
# d'un joueur specifique. Sauve dans cfg client1 sous "player_volumes".
# Applique en live via state.audio_io.set_user_volume_multiplier(name, ratio).

class VolumePopup(QDialog):
    """Mini popup volume joueur. Non-modal pour pouvoir cliquer ailleurs
    dans l'UI sans la fermer. Se referme avec le bouton Fermer ou Echap."""

    def __init__(self, parent, player_name: str):
        super().__init__(parent)
        self._name = player_name
        self.setWindowTitle(f"Volume - {player_name}")
        self.setWindowFlags(self.windowFlags() | Qt.Tool)
        self.setMinimumSize(320, 130)

        # Charger la valeur courante
        saved = 100
        if _CORE_AVAILABLE:
            try:
                core_cfg = _core._load_client_cfg()
                saved = int(core_cfg.get("player_volumes", {}).get(player_name, 100))
            except Exception:
                pass

        v = QVBoxLayout(self)
        v.setSpacing(8)

        title = QLabel(f"Volume de <b>{player_name}</b>")
        title.setStyleSheet("font-size: 11pt;")
        v.addWidget(title)

        h = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 200)
        self.slider.setValue(saved)
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setTickInterval(50)
        self.slider.valueChanged.connect(self._on_changed)
        h.addWidget(self.slider, stretch=1)

        self.lbl_value = QLabel(f"{saved}%")
        self.lbl_value.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11pt; "
            "min-width: 50px; padding: 4px;"
        )
        self.lbl_value.setAlignment(Qt.AlignCenter)
        h.addWidget(self.lbl_value)
        v.addLayout(h)

        h2 = QHBoxLayout()
        btn_reset = QPushButton("Reset (100%)")
        btn_reset.clicked.connect(lambda: self.slider.setValue(100))
        h2.addWidget(btn_reset)
        h2.addStretch(1)
        btn_close = QPushButton("Fermer")
        btn_close.clicked.connect(self.accept)
        h2.addWidget(btn_close)
        v.addLayout(h2)

    @Slot(int)
    def _on_changed(self, value: int):
        self.lbl_value.setText(f"{value}%")
        # Appliquer en live a audio_io
        if state.audio_io is not None:
            try:
                state.audio_io.set_user_volume_multiplier(
                    self._name, value / 100.0
                )
            except Exception:
                pass
        # Persister dans cfg client1
        if _CORE_AVAILABLE:
            try:
                core_cfg = _core._load_client_cfg()
                pv = core_cfg.setdefault("player_volumes", {})
                pv[self._name] = int(value)
                _core._save_client_cfg(core_cfg)
            except Exception:
                pass


# ======================================================================
# Helper : formattage position joueur courant
# ======================================================================

def _format_axes(pos: dict) -> str:
    """Formatte X/Y/Z avec unite par axe selon la magnitude de chaque axe.

    Logique simulant ce que le HUD SC affiche reellement :
      - |val| < 10000 m  -> affichage en metres : X:370.14(m)
      - |val| >= 10000 m -> affichage en km     : X:600.45(km)

    Chaque axe est traite INDEPENDAMMENT (un X en km, un Z en m sont OK).
    Cas typique planete : X:600.45(km)  Y:-1200.01(km)  Z:-320.84(m).

    2 decimales partout pour rester compact et coherent. Format compact
    sans espace dans la parenthese. Format inspire de l'affichage HUD SC
    qui montre l'unite la plus naturelle pour chaque axe.

    Pourquoi pas le format legacy (m / km / Mkm avec unite globale) :
    sur une planete tu peux avoir Z=-320m alors que X et Y sont en km.
    Forcer une unite globale donnerait Z:-0.3208(km) qui n'est pas ce
    que l'OCR a lu. Notre format respecte mieux l'affichage natif SC.
    """
    try:
        x = float(pos.get("x", 0))
        y = float(pos.get("y", 0))
        z = float(pos.get("z", 0))
    except Exception:
        return "(coords invalides)"

    def _fmt_axis(label: str, val: float) -> str:
        if abs(val) < 10_000:
            # Affichage en metres avec 2 decimales. round() en amont evite
            # l'affichage "-0.00" pour les valeurs entre -0.005 et 0
            # (genre val=-0.001 -> f"{val:.2f}" = "-0.00"). round(val, 2)
            # garantit que les valeurs proches de 0 sont normalisees a +0.0.
            v = round(val, 2)
            # Re-normaliser le signe : round peut encore retourner -0.0 sur
            # certains floats. v + 0.0 force le +0.0 canonique.
            if v == 0.0:
                v = 0.0
            return f"{label}:{v:.2f}(m)"
        else:
            v = round(val / 1000, 2)
            if v == 0.0:
                v = 0.0
            return f"{label}:{v:.2f}(km)"

    return f"{_fmt_axis('X', x)}  {_fmt_axis('Y', y)}  {_fmt_axis('Z', z)}"


def _format_my_pos(pos: dict) -> str:
    """Formate une position OCR pour affichage UI : container + coords.

    Format sur 2 lignes :
      <ContainerNamePretty>
      X:...  Y:...  Z:...  (unite)

    L'unite et la precision dependent de la magnitude de la position :
      - mag < 10_000 m       : metres, sans decimales (intra-container)
      - 10_000 <= mag < 10M  : kilometres, 4 decimales (~10cm de resolution)
      - mag >= 10M           : Mkm, 7 decimales (resolution 1m, echelle systeme)
    """
    try:
        x = float(pos.get("x", 0))
        y = float(pos.get("y", 0))
        z = float(pos.get("z", 0))
    except Exception:
        return "(position invalide)"
    raw_container = pos.get("zone") or pos.get("container_name") or "?"
    # NB : on prend `zone` en priorite car c'est la version canonique
    # validee contre _KNOWN_ZONES (lowercase, underscore_separated). Le
    # `container_name` lui garde la casse OCR brute (V majuscule un coup,
    # v minuscule l'autre selon la luminance des pixels) et donne un
    # affichage incoherent. La canonicalisation lowercase a aussi un effet
    # de bord positif : "ll" reste lisible la ou "Ll" + Consolas pouvait
    # ressembler a "L1".
    if _SCO_AVAILABLE:
        try:
            container = _sco._pretty_container_name(raw_container)
        except Exception:
            container = raw_container
    else:
        container = raw_container
    mag = math.sqrt(x * x + y * y + z * z)
    if mag < 10_000:
        # Coords en metres : utiliser round() au lieu de f-string ".0f" pour
        # eviter l'affichage "-0" sur les valeurs entre -0.5 et 0 (genre
        # x=-0.05 -> f"{x:.0f}" = "-0"). round(-0.05) renvoie 0 (sans signe),
        # donc l'affichage reste "0" stable. Visuellement le signe ne
        # clignote plus quand le joueur est immobile sub-metrique a l'origine.
        coords = f"X:{round(x)}  Y:{round(y)}  Z:{round(z)}  (m)"
    elif mag < 10_000_000:
        coords = (
            f"X:{x/1000:.4f}  Y:{y/1000:.4f}  Z:{z/1000:.4f}  (km)"
        )
    else:
        coords = (
            f"X:{x/1_000_000:.7f}  "
            f"Y:{y/1_000_000:.7f}  "
            f"Z:{z/1_000_000:.7f}  (Mkm)"
        )
    return f"{container}\n{coords}"


def _make_eye_icon(open_state: bool, color_hex: str, size: int = 20) -> QIcon:
    """Genere une icone oeil en line-art simple (1px stroke, monochrome).
    Pas d'emoji, pas de couleur realiste : juste deux courbes + cercle
    pour l'oeil ouvert, et la meme avec une barre oblique pour ferme.

    Args:
        open_state: True = oeil ouvert (mot de passe visible),
                    False = oeil ferme/barre (mot de passe masque)
        color_hex: couleur du trait (ex: "#6e7681")
        size: taille du pixmap en px (carre)

    Returns:
        QIcon que l'on peut passer a btn.setIcon()."""
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))  # transparent
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color_hex))
    pen.setWidthF(1.4)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    # L'amande de l'oeil = 2 arcs (paupiere haut + paupiere bas) qui se
    # rejoignent. On dessine via drawArc dans un rectangle qui represente
    # l'ellipse complete. startAngle/spanAngle en 1/16 de degre.
    margin = 2
    cx = size / 2
    cy = size / 2
    half_w = (size - 2 * margin) / 2  # demi-largeur de l'amande
    half_h = half_w * 0.55             # demi-hauteur (ratio amande)
    rect_arc = QRect(
        int(cx - half_w), int(cy - half_h),
        int(half_w * 2), int(half_h * 2),
    )
    p.drawArc(rect_arc, 0, 180 * 16)        # paupiere superieure
    p.drawArc(rect_arc, 180 * 16, 180 * 16) # paupiere inferieure

    # Pupille : petit cercle au centre
    pup_r = half_w * 0.30
    p.drawEllipse(
        int(cx - pup_r), int(cy - pup_r),
        int(pup_r * 2), int(pup_r * 2),
    )

    # Si oeil ferme : barre oblique de bas-gauche a haut-droit
    if not open_state:
        p.drawLine(
            int(margin), int(size - margin),
            int(size - margin), int(margin),
        )

    p.end()
    return QIcon(pix)


class VUMeterWithGate(QWidget):
    """VU-metre custom qui dessine la barre de niveau ET un trait vertical
    indiquant le seuil du gate. Permet a l'utilisateur de voir directement
    sur le VU si sa voix passe au-dessus du gate (donc est transmise) ou
    pas (donc est coupee). Remplace QProgressBar pour pouvoir superposer
    le trait du gate (impossible avec QProgressBar standard).

    API minimaliste compatible avec QProgressBar pour pouvoir swap :
        setValue(level_0_100)  -> niveau audio courant
        setGate(gate_0_100)    -> position du seuil du gate
    Couleur de la barre selon niveau : vert < 60, orange 60-85, rouge > 85.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0       # niveau audio 0..100
        self._gate = 0        # seuil gate 0..100 (trait blanc vertical)
        self.setMinimumHeight(18)
        self.setMaximumHeight(18)
        # Le QSS global ne s'applique pas aux paintEvent custom, mais on
        # le definit quand meme pour eviter qu'un fond parasite apparaisse.
        self.setStyleSheet("background: transparent;")

    def setValue(self, level: int):
        """Niveau audio courant (0-100). Repaint si change."""
        new = max(0, min(100, int(level)))
        if new != self._level:
            self._level = new
            self.update()

    def setGate(self, gate: int):
        """Position du trait du gate (0-100). Repaint si change."""
        new = max(0, min(100, int(gate)))
        if new != self._gate:
            self._gate = new
            self.update()

    def paintEvent(self, ev):
        """Dessin custom : fond sombre + chunk de couleur selon niveau
        + trait blanc vertical au seuil du gate."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w = self.width()
        h = self.height()

        # Fond + bordure (meme look que l'ancien QProgressBar)
        p.fillRect(0, 0, w, h, QColor("#222"))
        pen = QPen(QColor("#444"))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(0, 0, w - 1, h - 1)

        # Couleur du chunk selon niveau audio
        if self._level >= 85:
            chunk_color = QColor("#ff5555")  # rouge sature
        elif self._level >= 60:
            chunk_color = QColor("#ffaa44")  # orange correct
        else:
            chunk_color = QColor("#44cc66")  # vert ok

        # Chunk : barre proportionnelle au niveau, avec 1px de marge
        # interne pour ne pas mordre sur la bordure.
        if self._level > 0:
            chunk_w = int((w - 2) * self._level / 100)
            p.fillRect(1, 1, chunk_w, h - 2, chunk_color)

        # Trait du gate : ligne verticale blanche a la position du seuil.
        # 2px de large pour bien le voir, va de haut en bas avec un peu
        # de marge pour ne pas toucher la bordure.
        gate_x = int((w - 2) * self._gate / 100) + 1
        pen_gate = QPen(QColor("#ffffff"))
        pen_gate.setWidth(2)
        p.setPen(pen_gate)
        p.drawLine(gate_x, 1, gate_x, h - 2)

        p.end()


class MicLevelRow(QWidget):
    """Une ligne du picker mic : marqueur ● selection + nom + bordure
    verte qui pulse selon le niveau RMS capte. Click = selection.

    Le RMS est mis a jour de l'exterieur via set_level(). La couleur
    de bordure est interpolee de THEME_BORDER (gris) a THEME_GREEN (vert)
    selon le niveau (0..1.0)."""

    sig_clicked = Signal(int)  # device_idx

    def __init__(self, dev_idx: int, label: str, is_current: bool, parent=None):
        super().__init__(parent)
        self._dev_idx = dev_idx
        self._level = 0.0     # 0.0..1.0 (RMS clampe)
        self._is_current = is_current
        # Bug fix : init explicite de _hover (avant, set seulement dans
        # enterEvent/leaveEvent et lu via getattr defensif). Coherent
        # avec OutputRow qui l'init bien dans __init__.
        self._hover: bool = False
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFixedHeight(32)

        # Layout : marqueur (●/  ) + nom du device
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(6)
        self._marker = QLabel("●" if is_current else "  ")
        self._marker.setStyleSheet(
            f"color: {THEME_GREEN if is_current else THEME_MUTED}; "
            "font-family: Consolas, monospace; font-size: 10pt;"
        )
        self._marker.setFixedWidth(14)
        h.addWidget(self._marker)
        self._name = QLabel(label)
        self._name.setStyleSheet(
            f"color: {THEME_TEXT}; font-family: Consolas, monospace; "
            "font-size: 9pt; background: transparent;"
        )
        # Permettre au label de retrecir si le nom est tres long (sinon
        # la popup s'etire au-dela de la fenetre).
        self._name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._name.setMinimumWidth(0)
        h.addWidget(self._name, stretch=1)

    def set_level(self, rms: float):
        """RMS recu d'un sd.InputStream. Repaint si change significatif."""
        new = max(0.0, min(1.0, float(rms) * 6.0))  # boost x6 pour visibilite
        # Repaint seulement si le changement est suffisant pour ne pas
        # spammer 30fps avec des micro-changements.
        if abs(new - self._level) > 0.02:
            self._level = new
            self.update()

    def set_current(self, is_current: bool):
        """Met a jour le marqueur ● apres selection."""
        self._is_current = is_current
        self._marker.setText("●" if is_current else "  ")
        self._marker.setStyleSheet(
            f"color: {THEME_GREEN if is_current else THEME_MUTED}; "
            "font-family: Consolas, monospace; font-size: 10pt;"
        )

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.sig_clicked.emit(self._dev_idx)
        super().mousePressEvent(ev)

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        """Dessin custom : fond + bordure verte qui pulse selon le niveau."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()
        # Fond (legerement plus clair au hover)
        bg = THEME_BG_PANEL if self._hover else THEME_BG_ROW
        p.fillRect(0, 0, w, h, QColor(bg))
        # Bordure : interpolation gris -> vert selon niveau
        # Au repos (level=0) : THEME_BORDER. Pic (level=1) : THEME_GREEN
        # vif. Mix lineaire des composantes RGB.
        c0 = QColor(THEME_BORDER)
        c1 = QColor(THEME_GREEN)
        t = self._level
        r = int(c0.red()   * (1 - t) + c1.red()   * t)
        g = int(c0.green() * (1 - t) + c1.green() * t)
        b = int(c0.blue()  * (1 - t) + c1.blue()  * t)
        pen_w = 1 + int(t * 2)  # 1px au repos, jusqu'a 3px au pic
        pen = QPen(QColor(r, g, b))
        pen.setWidth(pen_w)
        p.setPen(pen)
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


class MicPickerDialog(QDialog):
    """Popup qui liste tous les micros disponibles. Pour chacun, ouvre un
    sd.InputStream parallele en silence, mesure le RMS, et fait pulser
    une bordure verte autour de la ligne. L'utilisateur parle, voit
    quelle ligne pulse (= son micro), clique dessus pour le selectionner.

    Le picker se ferme :
      - Click sur une ligne (selection)
      - Click en dehors (Qt.Popup auto-closes)
      - ESC

    Les streams sont fermes automatiquement a la destruction du dialog."""

    sig_mic_selected = Signal(int, str)  # device_idx, label

    def __init__(self, devices: list, current_label: str, parent=None):
        # Qt.Popup : se ferme automatiquement si l'utilisateur clique
        # ailleurs. Pas besoin de gerer FocusOut manuellement.
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._devices = devices
        self._streams = []
        self._rows_by_idx = {}
        # Buffer thread-safe pour les RMS recus dans les callbacks audio.
        # Lu par le QTimer du main thread pour eviter cross-thread Qt.
        self._rms_dict = {}
        self._rms_lock = threading.Lock()

        self.setStyleSheet(
            f"QDialog {{ background: {THEME_BG_PANEL}; "
            f"  border: 1px solid {THEME_BORDER}; }}"
        )

        # Layout : titre + scrollable list
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Titre / hint
        hint = QLabel("Parlez : la bordure verte indique votre micro. "
                      "Click pour selectionner.")
        hint.setStyleSheet(
            f"color: {THEME_MUTED}; padding: 6px 10px; "
            f"background: {THEME_BG_PANEL}; font-size: 9pt;"
        )
        hint.setWordWrap(True)
        v.addWidget(hint)

        # Liste scrollable
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        v_inner = QVBoxLayout(inner)
        v_inner.setContentsMargins(4, 4, 4, 4)
        v_inner.setSpacing(2)
        for dev_id, label in devices:
            row = MicLevelRow(dev_id, label, is_current=(label == current_label))
            row.sig_clicked.connect(self._on_row_clicked)
            v_inner.addWidget(row)
            self._rows_by_idx[dev_id] = row
        v_inner.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, stretch=1)

        # Taille raisonnable. Largeur fixe pour eviter que des noms tres
        # longs (devices virtuels MME/WASAPI) ne fassent deborder la popup
        # au-dela de l'ecran.
        self.setFixedWidth(480)
        self.setMaximumHeight(min(420, 60 + len(devices) * 36))

        # Demarrer les streams sounddevice en parallele (un par mic)
        self._start_streams()

        # QTimer 30fps pour pousser les RMS du buffer thread-safe vers
        # les MicLevelRow (operations Qt = main thread uniquement).
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)  # ~30 fps
        self._anim_timer.timeout.connect(self._refresh_levels)
        self._anim_timer.start()

    def _start_streams(self):
        """Ouvre un sd.InputStream silencieux sur chaque micro. Ecrit le
        RMS dans self._rms_dict via callback. Les devices qui refusent
        d'ouvrir (deja utilises, sample rate non supporte, exclusive mode)
        sont ignores silencieusement avec un log."""
        try:
            import sounddevice as sd
            import numpy as np
        except ImportError:
            return

        def make_callback(device_idx):
            def _cb(indata, frames, time_info, status):
                try:
                    rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
                    with self._rms_lock:
                        self._rms_dict[device_idx] = rms
                except Exception:
                    pass
            return _cb

        opened = 0
        for dev_id, label in self._devices:
            try:
                s = sd.InputStream(
                    device=dev_id,
                    channels=1,
                    samplerate=48000,
                    blocksize=480,  # 10ms
                    dtype="float32",
                    callback=make_callback(dev_id),
                    latency="low",
                )
                s.start()
                self._streams.append(s)
                opened += 1
            except Exception as e:
                # Device pas dispo (deja utilise, sample rate refuse,
                # exclusive mode bloque, etc.) -> on ignore.
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[MIC PICKER] '{label}' (idx={dev_id}) "
                            f"non ouvert : {type(e).__name__}: {e}"
                        )
                    except Exception:
                        pass
        if _CORE_AVAILABLE:
            try:
                _core._dbg_log(
                    f"[MIC PICKER] {opened} streams ouverts "
                    f"sur {len(self._devices)} micros"
                )
            except Exception:
                pass

    def _refresh_levels(self):
        """Tick QTimer (~30fps). Lit le buffer RMS thread-safe et pousse
        chaque valeur dans la MicLevelRow correspondante."""
        with self._rms_lock:
            snapshot = dict(self._rms_dict)
        for dev_id, rms in snapshot.items():
            row = self._rows_by_idx.get(dev_id)
            if row is not None:
                row.set_level(rms)

    @Slot(int)
    def _on_row_clicked(self, dev_idx: int):
        """L'utilisateur a clique sur une ligne. Trouve le label, emit
        le signal de selection, ferme le picker."""
        for dev_id, label in self._devices:
            if dev_id == dev_idx:
                self.sig_mic_selected.emit(dev_idx, label)
                break
        self.close()

    def closeEvent(self, ev):
        """Cleanup : stop le timer, ferme tous les streams sounddevice
        pour liberer les devices."""
        try:
            self._anim_timer.stop()
        except Exception:
            pass
        for s in self._streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self._streams = []
        super().closeEvent(ev)


class OutputRow(QWidget):
    """Une ligne du picker sortie : marqueur ● selection + nom + bouton
    '▶ Test' qui joue 2 bips sur cette sortie. Click sur la zone nom = selection."""

    sig_clicked = Signal(int)        # device_idx
    sig_test_clicked = Signal(int, str)  # device_idx, label

    def __init__(self, dev_idx: int, label: str, is_current: bool, parent=None):
        super().__init__(parent)
        self._dev_idx = dev_idx
        self._label = label
        self._is_current = is_current
        self._hover = False
        self.setFixedHeight(36)

        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(6)
        # Marqueur ● pour selection
        self._marker = QLabel("●" if is_current else "  ")
        self._marker.setStyleSheet(
            f"color: {THEME_GREEN if is_current else THEME_MUTED}; "
            "font-family: Consolas, monospace; font-size: 10pt;"
        )
        self._marker.setFixedWidth(14)
        h.addWidget(self._marker)
        # Zone clickable nom. setMinimumWidth(0) + size policy pour que
        # le label puisse retrecir et laisser de la place au bouton Test.
        # Les noms longs sont tronques avec ... grace a Qt.ElideRight.
        self._name = QLabel(label)
        self._name.setStyleSheet(
            f"color: {THEME_TEXT}; font-family: Consolas, monospace; "
            "font-size: 9pt; background: transparent;"
        )
        self._name.setMinimumWidth(0)
        # Permet a QLabel de retrecir. Sans ca, sizeHint() = taille naturelle
        # du texte (souvent > largeur popup) et le bouton Test sort.
        self._name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._name.setTextInteractionFlags(Qt.NoTextInteraction)
        self._name.setCursor(QCursor(Qt.PointingHandCursor))
        self._name.mousePressEvent = self._on_name_clicked
        h.addWidget(self._name, stretch=1)
        # Bouton Test sur la ligne
        self._btn_test = QPushButton("▶ Test")
        self._btn_test.setMaximumWidth(75)
        self._btn_test.setStyleSheet(
            f"QPushButton {{ background: {THEME_BORDER}; color: {THEME_TEXT}; "
            f"  border: 1px solid {THEME_BORDER}; border-radius: 3px; "
            "  padding: 3px 8px; font-size: 9pt; }"
            f"QPushButton:hover {{ border: 1px solid {THEME_BLUE}; }}"
        )
        self._btn_test.clicked.connect(self._on_test_clicked)
        h.addWidget(self._btn_test)

    def _on_name_clicked(self, ev):
        if ev.button() == Qt.LeftButton:
            self.sig_clicked.emit(self._dev_idx)

    def _on_test_clicked(self):
        # Feedback visuel : bouton vert pendant 800ms
        self._btn_test.setStyleSheet(
            f"QPushButton {{ background: {THEME_GREEN}; "
            f"  color: {THEME_BG_CLIENT}; border: 1px solid {THEME_GREEN}; "
            "  border-radius: 3px; padding: 3px 8px; font-size: 9pt; "
            "  font-weight: bold; }"
        )
        # On utilise un QTimer enfant du widget plutot que QTimer.singleShot
        # global. Quand le widget est detruit (popup fermee), le timer
        # enfant est tue automatiquement par Qt -> pas de slot appele sur
        # un widget detruit (= SIGSEGV). singleShot global survit a la
        # destruction et plante.
        if not hasattr(self, "_reset_timer"):
            self._reset_timer = QTimer(self)
            self._reset_timer.setSingleShot(True)
            self._reset_timer.timeout.connect(self._reset_test_btn)
        self._reset_timer.start(800)
        self.sig_test_clicked.emit(self._dev_idx, self._label)

    @Slot()
    def _reset_test_btn(self):
        # Try/except defensif au cas ou Qt destroie le bouton entre le
        # check et l'appel (rare mais possible avec WA_DeleteOnClose).
        try:
            self._btn_test.setStyleSheet(
                f"QPushButton {{ background: {THEME_BORDER}; color: {THEME_TEXT}; "
                f"  border: 1px solid {THEME_BORDER}; border-radius: 3px; "
                "  padding: 3px 8px; font-size: 9pt; }"
                f"QPushButton:hover {{ border: 1px solid {THEME_BLUE}; }}"
            )
        except Exception:
            pass

    def set_current(self, is_current: bool):
        self._is_current = is_current
        self._marker.setText("●" if is_current else "  ")
        self._marker.setStyleSheet(
            f"color: {THEME_GREEN if is_current else THEME_MUTED}; "
            "font-family: Consolas, monospace; font-size: 10pt;"
        )

    def enterEvent(self, ev):
        self._hover = True
        self.setStyleSheet(f"background: {THEME_BG_PANEL};")
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.setStyleSheet("")
        super().leaveEvent(ev)


class OutputPickerDialog(QDialog):
    """Popup qui liste toutes les sorties audio. Pour chacune, un bouton
    '▶ Test' joue 2 bips (440Hz + 880Hz) sur cette sortie pour identifier
    visuellement le bon casque (utile avec GoXLR / StreamDeck qui exposent
    plusieurs peripheriques virtuels).

    Click sur le nom = selection. Click sur Test = bips. Click ailleurs = ferme."""

    sig_out_selected = Signal(int, str)  # device_idx, label

    def __init__(self, devices: list, current_label: str, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._devices = devices

        self.setStyleSheet(
            f"QDialog {{ background: {THEME_BG_PANEL}; "
            f"  border: 1px solid {THEME_BORDER}; }}"
        )

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        hint = QLabel("Click ▶ Test pour identifier la sortie. "
                      "Click sur le nom pour selectionner.")
        hint.setStyleSheet(
            f"color: {THEME_MUTED}; padding: 6px 10px; "
            f"background: {THEME_BG_PANEL}; font-size: 9pt;"
        )
        hint.setWordWrap(True)
        v.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        v_inner = QVBoxLayout(inner)
        v_inner.setContentsMargins(4, 4, 4, 4)
        v_inner.setSpacing(2)
        for dev_id, label in devices:
            row = OutputRow(dev_id, label, is_current=(label == current_label))
            row.sig_clicked.connect(self._on_row_clicked)
            row.sig_test_clicked.connect(self._on_row_test_clicked)
            v_inner.addWidget(row)
        v_inner.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, stretch=1)

        # Largeur stricte : sans ca les noms longs forcent la popup a
        # s'etirer et le bouton ▶ Test sort de l'ecran a droite.
        # 480px laisse 75px pour le bouton + ~360px pour le nom (tronque
        # en ellipsis si trop long) + scrollbar.
        self.setFixedWidth(480)
        self.setMaximumHeight(min(420, 60 + len(devices) * 40))

    @Slot(int)
    def _on_row_clicked(self, dev_idx: int):
        for dev_id, label in self._devices:
            if dev_id == dev_idx:
                self.sig_out_selected.emit(dev_idx, label)
                break
        self.close()

    @Slot(int, str)
    def _on_row_test_clicked(self, dev_idx: int, label: str):
        """Play les bips dans un thread daemon (pas bloquant pour l'UI).
        Le picker reste ouvert : l'utilisateur peut tester d'autres sorties."""
        threading.Thread(
            target=_play_test_beeps,
            args=(dev_idx, label),
            daemon=True,
            name="c2-test-beeps",
        ).start()


def _play_test_beeps(out_idx: int, out_label: str):
    """Joue 2 bips (440Hz puis 880Hz, 0.25s chacun) sur la sortie audio
    selectionnee. Permet a l'utilisateur de verifier que c'est bien son
    casque (utile avec GoXLR / StreamDeck / VB-Audio qui exposent
    plusieurs peripheriques virtuels).

    IMPORTANT : la lecture est isolee dans un SOUS-PROCESS Python car
    sounddevice/PortAudio peuvent crasher au niveau natif (SIGSEGV) sur
    certains devices virtuels MME (VB-Cable, Voicemeeter, ...). Un crash
    natif passe a travers le try/except Python et tue le process Python
    courant. Avec un sous-process, le crash ne tue que le sous-process,
    pas le client principal.

    Le sous-process est totalement detache : on n'attend pas son resultat
    (fire-and-forget). Il se ferme tout seul apres ~0.55s (duree des bips).

    Synchrone : a appeler dans un thread daemon. Le thread bloque ~0.5s
    pendant que le sous-process se lance puis retourne, mais il ne
    bloque PAS pendant la lecture des bips elle-meme."""
    import subprocess
    # Code Python a executer dans le sous-process. Reproduit la generation
    # des bips et la lecture via sounddevice. Pas d'imports tiers en
    # dehors de sounddevice et numpy qui sont deja installes pour la
    # pipeline VOIP du client.
    code = f"""
import sys
try:
    import sounddevice as sd
    import numpy as np
    sample_rate = 48000
    beep_duration = 0.25
    silence_duration = 0.05
    def make_beep(freq):
        n = int(sample_rate * beep_duration)
        t = np.arange(n) / sample_rate
        wave = 0.3 * np.sin(2 * np.pi * freq * t)
        fade_n = int(0.01 * sample_rate)
        if fade_n > 0:
            wave[:fade_n] *= np.linspace(0, 1, fade_n)
            wave[-fade_n:] *= np.linspace(1, 0, fade_n)
        return wave.astype(np.float32)
    beep1 = make_beep(440)
    beep2 = make_beep(880)
    silence = np.zeros(int(sample_rate * silence_duration), dtype=np.float32)
    full = np.concatenate([beep1, silence, beep2])
    sd.play(full, samplerate=sample_rate, device={int(out_idx)}, blocking=True)
except Exception as e:
    sys.stderr.write(f'TEST BEEPS ERROR: {{type(e).__name__}}: {{e}}\\n')
    sys.exit(1)
"""
    try:
        # Lancer le sous-process en mode totalement detache. Sur Windows,
        # CREATE_NO_WINDOW evite qu'une fenetre console parasite apparaisse
        # (sinon python.exe ouvre une console). DETACHED_PROCESS rend le
        # sous-process independant : meme si le client crash, il continue
        # (pas grave, il sera tue par Windows quand il aura fini).
        creationflags = 0
        if sys.platform == "win32":
            try:
                creationflags = (
                    subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
                    | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                )
            except AttributeError:
                pass
        # On utilise sys.executable (= le runtime Python du client) pour
        # avoir acces aux memes packages (sounddevice, numpy) installes.
        subprocess.Popen(
            [sys.executable, "-c", code],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if _CORE_AVAILABLE:
            try:
                _core._dbg_log(
                    f"[TEST OUTPUT] Sous-process lance pour '{out_label}' "
                    f"(idx={out_idx})"
                )
            except Exception:
                pass
    except Exception as e:
        if _CORE_AVAILABLE:
            try:
                _core._dbg_log(
                    f"[TEST OUTPUT] Echec lancement sous-process pour "
                    f"'{out_label}' : {type(e).__name__}: {e}"
                )
            except Exception:
                pass


class PlayerCard(QWidget):
    """Card representant un joueur connecte. Remplace l'ancien
    QTableWidget par un design plus visuel et lisible : le nom et les
    badges canal/profil restent toujours visibles meme si la fenetre
    est etroite (les infos secondaires - zone, position, distance -
    passent sur la 2e ligne et tronquent gracieusement).

    Layout :
        ┌─────────────────────────────────────────────┐
        │ Mannequin_01    [General] (Profil1)  ●  🔊 │  <- ligne 1
        │ ooc_stanton_4_microtech · 371,-102,-434 ·   │  <- ligne 2
        │ 1.2km                                       │
        └─────────────────────────────────────────────┘

    Etats :
      - Online (defaut) : couleurs normales
      - Offline (perdu connexion) : tout grise
      - Mode anonyme : zone/position/distance affichent "(masque)"
    """

    sig_volume_clicked = Signal(str)  # name

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._offline = False
        self._anonymous = False
        # Etat affichage interne pour _refresh
        self._zone = "-"
        self._pos_str = "-"
        self._dist_str = "-"
        self._dist_meters: float | None = None  # valeur brute en m (None = inconnue)

        self.setObjectName("PlayerCard")
        self.setStyleSheet(
            f"QWidget#PlayerCard {{ background: {THEME_BG_PANEL}; "
            f"  border: 1px solid {THEME_BORDER}; border-radius: 6px; }}"
            "QLabel { background: transparent; }"
        )

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        # Ligne 1 : nom + badges + indicateur SC + bouton volume
        h1 = QHBoxLayout()
        h1.setSpacing(6)

        self._lbl_name = QLabel(name)
        self._lbl_name.setStyleSheet(
            f"color: {THEME_TEXT}; font-weight: bold; font-size: 11pt;"
        )
        h1.addWidget(self._lbl_name)

        # Badge canal : rectangle colore avec le nom du canal
        self._lbl_channel = QLabel("")
        self._lbl_channel.setVisible(False)
        h1.addWidget(self._lbl_channel)

        # Badge profil : pareil mais en violet
        self._lbl_profile = QLabel("")
        self._lbl_profile.setVisible(False)
        h1.addWidget(self._lbl_profile)

        h1.addStretch(1)

        # Indicateur SC (joue a Star Citizen ou pas) : ● vert si oui,
        # ○ gris si non. Petit, discret.
        self._lbl_sc = QLabel("●")
        self._lbl_sc.setStyleSheet(
            f"color: {THEME_GREEN}; font-size: 12pt;"
        )
        self._lbl_sc.setToolTip("En jeu (Star Citizen detecte)")
        h1.addWidget(self._lbl_sc)

        # Bouton volume
        self._btn_vol = QPushButton("🔊")
        self._btn_vol.setStyleSheet(
            f"QPushButton {{ background: {THEME_BG_ROW}; "
            f"  border: 1px solid {THEME_BORDER}; "
            "  border-radius: 3px; padding: 2px 6px; font-size: 12pt; }"
            f"QPushButton:hover {{ border: 1px solid {THEME_BLUE}; }}"
        )
        self._btn_vol.setFixedWidth(40)
        self._btn_vol.setToolTip(f"Reglage volume de {name}")
        self._btn_vol.clicked.connect(
            lambda _=False: self.sig_volume_clicked.emit(self._name)
        )
        h1.addWidget(self._btn_vol)

        v.addLayout(h1)

        # Ligne 2 : [zone · position] (gauche, muted)  +  [Distance: XX m]
        # (droite, colore selon proximite : vert <=5m, orange 5-30m, gris au-dela).
        # On separe en 2 labels pour pouvoir colorer uniquement la distance,
        # qui est l'info la plus utile a l'utilisateur (zone audible ou non).
        h2 = QHBoxLayout()
        h2.setSpacing(8)
        h2.setContentsMargins(0, 0, 0, 0)

        self._lbl_info = QLabel("-")
        self._lbl_info.setStyleSheet(
            f"color: {THEME_MUTED}; font-family: Consolas, monospace; "
            "font-size: 9pt;"
        )
        # Permet au label de retrecir avec ellipsis si la fenetre est
        # etroite, plutot que de pousser la card hors largeur.
        self._lbl_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        h2.addWidget(self._lbl_info, 1)  # stretch=1 : prend tout l'espace dispo

        # Label distance dedie. Couleur dynamique selon la proximite :
        #   - vert  : d <= 5m       (volume 100%, parfaitement audible)
        #   - orange: 5m < d <= 30m (volume reduit mais audible)
        #   - gris  : d > 30m       (faible volume ou silence)
        #   - gris  : "hors de portee" (containers differents)
        # Format : "Distance: 12 m" / "Distance: 1.2 km" / "Distance: hors de portee"
        self._lbl_dist = QLabel("")
        self._lbl_dist.setStyleSheet(
            f"color: {THEME_MUTED}; font-family: Consolas, monospace; "
            "font-size: 9pt; font-weight: bold;"
        )
        self._lbl_dist.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        h2.addWidget(self._lbl_dist, 0)

        v.addLayout(h2)

    @property
    def name(self) -> str:
        return self._name

    def set_channel_profile(self, channel: Optional[str], profile: Optional[str]):
        """Met a jour les badges Canal/Profil. None = badge cache."""
        # Badge canal : bleu pale, fond gris fonce
        if channel:
            self._lbl_channel.setText(f" {channel} ")
            self._lbl_channel.setStyleSheet(
                f"color: {THEME_BLUE}; background: {THEME_BG_ROW}; "
                f"border: 1px solid {THEME_BORDER}; border-radius: 3px; "
                "padding: 1px 6px; font-size: 9pt; font-weight: bold;"
            )
            self._lbl_channel.setVisible(True)
        else:
            self._lbl_channel.setVisible(False)
        # Badge profil : violet
        # On evite la double mention quand canal == profil (cas PTT
        # profil temporaire serveur-side).
        if profile and profile != channel:
            self._lbl_profile.setText(f" {profile} ")
            self._lbl_profile.setStyleSheet(
                f"color: #bc8cff; background: {THEME_BG_ROW}; "
                f"border: 1px solid {THEME_BORDER}; border-radius: 3px; "
                "padding: 1px 6px; font-size: 9pt; font-weight: bold;"
            )
            self._lbl_profile.setVisible(True)
        else:
            self._lbl_profile.setVisible(False)

    def set_position(self, zone: str, pos_str: str, dist_str: str,
                     dist_meters: float | None = None):
        """Stocke et reaffiche la ligne d'info zone+pos+dist.
          dist_meters : valeur brute en metres (None = inconnue, inf = hors
                        de portee = container different). Sert a colorer le
                        label distance selon la proximite. Optionnel pour
                        retrocompat avec d'eventuels appelants externes.
        """
        self._zone = zone or "-"
        self._pos_str = pos_str or "-"
        self._dist_str = dist_str or "-"
        self._dist_meters = dist_meters
        self._refresh_info_label()

    def set_anonymous(self, anonymous: bool):
        """Mode anonyme : on masque la zone/position/distance."""
        self._anonymous = anonymous
        self._refresh_info_label()

    def set_offline(self, offline: bool):
        """Joueur deconnecte (ou reconnecte). Tout grise quand offline."""
        self._offline = offline
        # Indicateur SC : gris/vert selon online
        if offline:
            self._lbl_sc.setStyleSheet(
                f"color: {THEME_MUTED}; font-size: 12pt;"
            )
            self._lbl_sc.setText("○")
            self._lbl_sc.setToolTip("Hors ligne")
            self._lbl_name.setStyleSheet(
                f"color: {THEME_MUTED}; font-weight: bold; font-size: 11pt;"
            )
        else:
            self._lbl_sc.setStyleSheet(
                f"color: {THEME_GREEN}; font-size: 12pt;"
            )
            self._lbl_sc.setText("●")
            self._lbl_sc.setToolTip("En jeu (Star Citizen detecte)")
            self._lbl_name.setStyleSheet(
                f"color: {THEME_TEXT}; font-weight: bold; font-size: 11pt;"
            )

    def _refresh_info_label(self):
        if self._anonymous:
            self._lbl_info.setText("(masque - mode anonyme)")
            self._lbl_dist.setText("")
            return
        # Partie gauche : zone · position (sans distance)
        parts = []
        if self._zone and self._zone != "-":
            parts.append(self._zone)
        if self._pos_str and self._pos_str != "-":
            parts.append(self._pos_str)
        if parts:
            self._lbl_info.setText("  ·  ".join(parts))
        else:
            self._lbl_info.setText("(en attente de position)")

        # Partie droite : "Distance: XX m" colore selon proximite.
        # Couleur :
        #   - vert  : d <= 5m  (volume 100%)
        #   - orange: 5 < d <= 30m (audible)
        #   - gris  : d > 30m ou hors de portee (faible/silence)
        d = self._dist_meters
        if d is None or self._dist_str == "-":
            # Pas encore de distance connue
            self._lbl_dist.setText("")
            return

        if d == float("inf"):
            # Containers differents : silence
            label = "Distance: hors de portée"
            color = THEME_MUTED
        else:
            # Format adaptatif : m, km, Mkm
            if d < 1000:
                label = f"Distance: {d:.0f} m"
            elif d < 1_000_000:
                label = f"Distance: {d/1000:.1f} km"
            else:
                label = f"Distance: {d/1_000_000:.2f} Mkm"
            # Couleur selon proximite
            if d <= 5.0:
                color = THEME_GREEN
            elif d <= 30.0:
                color = THEME_ORANGE
            else:
                color = THEME_MUTED

        self._lbl_dist.setText(label)
        self._lbl_dist.setStyleSheet(
            f"color: {color}; font-family: Consolas, monospace; "
            "font-size: 9pt; font-weight: bold;"
        )


class MainWindow(QMainWindow):
    # Signal interne pour declencher run_connect dans le worker thread
    _sig_start_connect = Signal(str, str, str)
    # Signal interne pour faire passer un evenement hotkey du thread
    # pynput au thread Qt main (un signal Qt utilise auto une
    # QueuedConnection en cross-thread, ce qui est thread-safe ;
    # contrairement a QTimer.singleShot qui exige un thread Qt).
    _sig_hotkey = Signal(str)  # nom du hotkey (ex: "mute_mic", "mute_all"...)

    # Signal emis par le worker de check MAJ (thread daemon -> main thread).
    # Le main thread met a jour le bouton et stocke le manifest distant.
    _sig_update_available = Signal(dict)

    # Signal emis par le worker d'application MAJ (thread daemon -> main).
    # Args : (success: bool, msg: str). On utilise un signal plutot que
    # QTimer.singleShot car singleShot depuis un thread non-Qt n'est PAS
    # thread-safe sur Windows et meurt parfois en silence (bug observe :
    # le download fini, _on_result jamais appele, bouton fige sur
    # "Telechargement..."). Un Signal Qt traverse le boundary thread via
    # QueuedConnection automatiquement.
    _sig_update_applied = Signal(bool, str)

    # Signal emis par le worker de check manuel MAJ (thread daemon -> main).
    # Args : (manifest_or_none: dict, err_msg: str). Meme raison que
    # _sig_update_applied : evite le bug singleShot cross-thread.
    # Le manifest est un dict vide {} si pas de MAJ trouvee (= deja a jour).
    _sig_update_check_done = Signal(dict, str)

    def __init__(self, cfg: dict):
        super().__init__()
        self._cfg = cfg
        self._user_resized = False
        self._initial_geom_set = False
        self._last_size: Optional[tuple[int, int]] = None
        # Bug fix : tracker la position fenetre pour distinguer un vrai
        # drag user d'un re-positionnement WM lors d'un hide/show.
        self._last_pos: Optional[tuple[int, int]] = None
        self._current_screen: Optional[QScreen] = None
        # Bug fix : flag pour ne connecter screenChanged qu'une seule
        # fois (avant on accumulait les connexions a chaque showEvent,
        # i.e. a chaque hide/show de calibration).
        self._screen_signal_connected: bool = False

        # Threads daemon
        self._ocr_thread = None
        self._ocr_watchdog_thread = None
        self._audio_ws_thread = None
        self._heartbeat_thread = None
        self._core_threads_started = False
        # Threads daemon optionnels crees a la volee plus tard.
        # Init explicite pour eviter les getattr defensifs partout
        # (cf. bug 36 : facilite la detection des typos).
        self._volume_safety_thread = None
        self._gamelog_thread = None
        self._helmet_scan_thread = None
        # Flags de notification manquante pour psutil (un seul warning
        # par session, pas un par tick).
        self._psutil_warned: bool = False
        self._psutil_warned_missing: bool = False
        # CalibrationFlow : instancie au moment du clic 'Calibrer la zone'.
        self._calib_flow = None
        # Liste des MonitorPickerWindow ouvertes pendant l'auto-zone
        # (calibration sans clic, scan tous les ecrans).
        self._auto_zone_pickers: list = []

        # Updater : manifest distant en attente d'application (s'il y en a)
        self._pending_update: Optional[dict] = None
        # Manifest en cours d'application (set juste avant de lancer le
        # thread _do_apply, lu dans _on_update_applied pour le cleanup
        # en cas d'echec). Init explicite pour eviter le getattr defensif.
        self._pending_apply_manifest: Optional[dict] = None
        # Flag : True une fois que les signaux currentIndexChanged des
        # combos audio ont ete connectes. Permet a _populate_audio_devices
        # de skipper le disconnect au 1er appel (sinon RuntimeWarning).
        self._audio_signals_connected: bool = False

        self.setWindowTitle("CircusVOIP Client — 0.1")
        # Appliquer le theme sombre global. On le met sur la
        # QApplication pour que toutes les dialogs creees plus tard
        # (QMessageBox, QFileDialog, etc.) heritent automatiquement.
        try:
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(THEME_QSS)
        except Exception:
            pass
        # Icone de la fenetre + barre des taches : StarCircus.ico
        # qui est dans le meme dossier que le client. Fallback silencieux
        # si le fichier est absent (pas critique).
        try:
            ico_path = _BASE_DIR / "StarCircus.ico"
            if ico_path.exists():
                icon = QIcon(str(ico_path))
                self.setWindowIcon(icon)
                # setWindowIcon sur la QApplication affecte aussi la
                # barre des taches Windows. Sans ca, c'est l'icone Qt
                # par defaut qui est utilisee.
                app = QApplication.instance()
                if app is not None:
                    app.setWindowIcon(icon)
        except Exception:
            pass

        self._build_ui()
        self._build_worker()
        self._apply_initial_geometry()

        # Shim UI pour les fonctions importees du client1
        if _CORE_AVAILABLE:
            self._core_shim = _CoreUIShim(self)
            self._init_zone_ocr()
            # Initialiser le nom dans le fichier de log : si le pseudo
            # est deja connu (lu du config au boot), le fichier de log
            # sera nomme correctement des le 1er _dbg_log. Sinon on
            # fallbackera au nom generique jusqu'a la 1ere connexion.
            try:
                _name = (cfg.get("name") or "").strip() or DEFAULT_NAME
                _core._set_log_player_name(_name)
            except Exception:
                pass
        else:
            self._core_shim = None

        # Manager des overlays floating
        self._overlay_manager = OverlayManager(self)
        # Bug fix : si la config sauvee a overlays_show=True, ouvrir les
        # overlays au boot (sinon l'utilisateur doit recliquer a chaque
        # demarrage). _build_ui() s'est deja execute donc btn_overlay_show
        # existe. On synchronise le bouton avec la valeur restauree puis
        # on declenche refresh() pour ouvrir effectivement les overlays.
        try:
            self._refresh_overlay_buttons()
            if self._overlay_manager.show_mode:
                self._overlay_manager.refresh()
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[OVERLAY] init refresh KO : {e}"
                    )
                except Exception:
                    pass

        # Audio : peupler les devices puis demarrer en local.
        if _AUDIO_AVAILABLE:
            QTimer.singleShot(100, self._populate_audio_devices)
            self._vu_timer = QTimer(self)
            self._vu_timer.setInterval(33)
            self._vu_timer.timeout.connect(self._vu_tick)
            self._vu_timer.start()

        # Check des mises a jour en arriere-plan : DESACTIVE en release 0.1.
        # Le serveur d'update n'est plus expose en prod, et le bouton MAJ
        # est masque dans l'UI. On garde le worker _update_check_worker
        # pour usage dev (debug, build interne) mais on ne le lance plus
        # automatiquement au boot.
        # Pour reactiver : decommenter le bloc ci-dessous.
        # threading.Thread(
        #     target=self._update_check_worker,
        #     daemon=True,
        #     name="c2-update-check",
        # ).start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # --- Bandeau du haut : statuts a gauche, bouton PARAMETRES a droite.
        # Le bouton "Verifier les MAJ" est dans la page Parametres.
        # Le statut connexion etait avant une ligne pleine largeur sous
        # le formulaire (gachis vertical), maintenant compact en haut. ---
        h_top = QHBoxLayout()
        h_top.setSpacing(12)

        # Statut serveur (Connecte / Deconnecte)
        self.lbl_status = QLabel("Deconnecte")
        self.lbl_status.setStyleSheet(
            f"color: {THEME_RED}; font-weight: bold; "
            "padding: 2px 6px; font-size: 10pt;"
        )
        h_top.addWidget(self.lbl_status)

        # Separateur visuel discret
        sep = QLabel("•")
        sep.setStyleSheet(f"color: {THEME_MUTED}; font-size: 10pt;")
        h_top.addWidget(sep)

        # Statut audio (Audio: OK / KO). Cache par defaut tant qu'on
        # n'a pas eu de retour du serveur audio (apres connexion).
        self.lbl_audio_status = QLabel("Audio : —")
        self.lbl_audio_status.setStyleSheet(
            f"color: {THEME_MUTED}; padding: 2px 6px; font-size: 10pt;"
        )
        h_top.addWidget(self.lbl_audio_status)

        h_top.addStretch(1)

        self.btn_settings = QPushButton("PARAMETRES")
        self.btn_settings.setCheckable(True)
        self.btn_settings.setMinimumWidth(110)
        self.btn_settings.setStyleSheet(
            "padding: 4px 10px; font-size: 9pt;"
        )
        self.btn_settings.clicked.connect(self._on_settings_toggled)
        h_top.addWidget(self.btn_settings)
        root.addLayout(h_top)

        # --- Conteneur "header de page principale" ---
        # Regroupe le formulaire de connexion + le label position OCR.
        # Visible uniquement en page principale ; masque en page Parametres
        # (toggle dans _on_settings_toggled). Ces 2 widgets ne servent a
        # rien dans la page Parametres et prennent de la place.
        self._main_header_box = QWidget()
        v_main_header = QVBoxLayout(self._main_header_box)
        v_main_header.setContentsMargins(0, 0, 0, 0)
        v_main_header.setSpacing(10)

        # --- Connexion : nom / IP / mot de passe / boutons ---
        form = QHBoxLayout()
        form.setSpacing(8)

        lbl_name = QLabel("Nom :")
        self.ed_name = QLineEdit(self._cfg.get("name", DEFAULT_NAME))
        self.ed_name.setMaximumWidth(180)
        # Limite a 20 caracteres pour eviter que les pseudos longs ne
        # cassent l'affichage des cards joueurs (badges canal/profil
        # pousses hors ecran). La plupart des pseudos SC font 8-15 chars.
        self.ed_name.setMaxLength(20)

        lbl_ip = QLabel("Serveur :")
        self.ed_ip = QLineEdit(self._cfg.get("server_ip", DEFAULT_IP))
        self.ed_ip.setMaximumWidth(180)
        self.ed_ip.setEchoMode(QLineEdit.Password)  # masque par defaut
        # Style minimaliste pour les boutons oeil (line-art, pas d'emoji).
        # Surclasse le QSS global qui donnerait un fond bleu vif :checked.
        _eye_btn_qss = (
            "QPushButton {"
            f" background: {THEME_BG_ROW};"
            f" border: 1px solid {THEME_BORDER};"
            " border-radius: 3px;"
            " padding: 2px;"
            " }"
            "QPushButton:hover {"
            f" border: 1px solid {THEME_MUTED};"
            " }"
            "QPushButton:checked {"
            f" background: {THEME_BG_ROW};"
            f" border: 1px solid {THEME_BLUE};"
            " }"
        )
        # Pre-generer les 2 icones (ferme = MUTED, ouvert = BLUE) pour
        # pouvoir swap selon l'etat checked sans regenerer a chaque fois.
        self._icon_eye_closed = _make_eye_icon(False, THEME_MUTED, 18)
        self._icon_eye_open   = _make_eye_icon(True,  THEME_BLUE,  18)

        self.btn_show_ip = QPushButton()
        self.btn_show_ip.setIcon(self._icon_eye_closed)
        self.btn_show_ip.setCheckable(True)
        self.btn_show_ip.setMaximumWidth(28)
        self.btn_show_ip.setToolTip("Afficher / masquer l'IP")
        self.btn_show_ip.setStyleSheet(_eye_btn_qss)

        def _toggle_ip_eye(checked: bool):
            self.ed_ip.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
            self.btn_show_ip.setIcon(
                self._icon_eye_open if checked else self._icon_eye_closed
            )
        self.btn_show_ip.clicked.connect(_toggle_ip_eye)

        lbl_pw = QLabel("MDP :")
        self.ed_pw = QLineEdit(self._cfg.get("token", ""))
        self.ed_pw.setEchoMode(QLineEdit.Password)
        self.ed_pw.setMaximumWidth(160)
        self.btn_show_pw = QPushButton()
        self.btn_show_pw.setIcon(self._icon_eye_closed)
        self.btn_show_pw.setCheckable(True)
        self.btn_show_pw.setMaximumWidth(28)
        self.btn_show_pw.setToolTip("Afficher / masquer le mot de passe")
        self.btn_show_pw.setStyleSheet(_eye_btn_qss)

        def _toggle_pw_eye(checked: bool):
            self.ed_pw.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
            self.btn_show_pw.setIcon(
                self._icon_eye_open if checked else self._icon_eye_closed
            )
        self.btn_show_pw.clicked.connect(_toggle_pw_eye)

        self.btn_toggle = QPushButton("CONNECTER")
        self.btn_toggle.setMinimumWidth(140)
        self.btn_toggle.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_toggle.clicked.connect(self._on_toggle_connect)

        form.addWidget(lbl_name)
        form.addWidget(self.ed_name)
        form.addSpacing(8)
        form.addWidget(lbl_ip)
        form.addWidget(self.ed_ip)
        form.addWidget(self.btn_show_ip)
        form.addSpacing(8)
        form.addWidget(lbl_pw)
        form.addWidget(self.ed_pw)
        form.addWidget(self.btn_show_pw)
        form.addStretch(1)
        form.addWidget(self.btn_toggle)
        v_main_header.addLayout(form)

        # Note: lbl_status est cree dans la barre du haut maintenant
        # (compact, a cote du statut audio). Plus de ligne pleine largeur.

        # --- Position OCR du joueur ---
        # Mis a jour par l'OCR via _on_my_pos_update (~5 fois/sec). Affiche
        # le container courant + coords formatees selon l'echelle (m/km/Mkm).
        # En mode anonyme, le texte est remplace par "(masque - mode anonyme)".
        self.lbl_my_pos = QLabel("En attente de position OCR...")
        self.lbl_my_pos.setStyleSheet(
            "background:#161b22; color:#6e7681; padding:6px; "
            "border-radius:4px; font-family: 'Consolas', 'Courier New', monospace;"
        )
        self.lbl_my_pos.setAlignment(Qt.AlignCenter)
        self.lbl_my_pos.setWordWrap(True)
        v_main_header.addWidget(self.lbl_my_pos)

        root.addWidget(self._main_header_box)

        # --- Stacked : page main / page settings ---
        self.stack = QStackedWidget()
        self._build_page_main()
        self._build_page_settings()
        self.stack.addWidget(self._page_main)
        self.stack.addWidget(self._page_settings)
        self.stack.setCurrentWidget(self._page_main)
        root.addWidget(self.stack, stretch=1)

        self.setMinimumSize(640, 480)

    def _build_page_main(self):
        """Page principale (vue jeu) : 2 colonnes : MUTES a gauche,
        Mode RP/Overlay/Canal + table joueurs a droite. La config audio
        (devices, gain, gate, VU) est dans la page Parametres."""
        self._page_main = QWidget()
        v = QVBoxLayout(self._page_main)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # --- Body : 2 colonnes ---
        body = QHBoxLayout()
        body.setSpacing(10)

        # === COLONNE GAUCHE : MUTES + OVERLAY (largeur fixe) ===
        # Largeur fixee pour qu'elle ne grossisse pas avec la fenetre.
        # 190px : juste de quoi accueillir les boutons en colonne.
        left_panel = QWidget()
        left_panel.setFixedWidth(190)
        v_left = QVBoxLayout(left_panel)
        v_left.setContentsMargins(0, 0, 0, 0)
        v_left.setSpacing(6)

        # Section MUTES
        gb_mutes = QGroupBox("MUTES")
        v_mutes = QVBoxLayout(gb_mutes)
        v_mutes.setSpacing(6)

        # MUTE MICRO (deplace du panneau audio)
        # Ordre des boutons aligne sur l'ordre de la liste des raccourcis :
        # 1. Mute micro, 2. Mute proximite, 3. Mute radio.
        self.btn_mute = QPushButton("MUTE MICRO")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setMinimumHeight(32)
        # Bug fix : les 3 boutons MUTE sont setCheckable(True), il faut
        # donc utiliser le 'checked' du clic (sinon le state Qt du bouton
        # peut se desynchroniser du state global si un hotkey pynput
        # bascule entre temps). On utilise des slots dedies _on_mute_X_toggled
        # qui set state.X = checked (les hotkeys pynput utilisent toujours
        # _do_toggle_mute_X qui INVERSE + _refresh_mute_button).
        self.btn_mute.clicked.connect(self._on_mute_toggled)
        v_mutes.addWidget(self.btn_mute)

        # MUTE proximité
        self.btn_mute_prox = QPushButton("MUTE PROXIMITE")
        self.btn_mute_prox.setCheckable(True)
        self.btn_mute_prox.setMinimumHeight(32)
        self.btn_mute_prox.clicked.connect(self._on_mute_prox_toggled)
        v_mutes.addWidget(self.btn_mute_prox)

        # MUTE radio
        self.btn_mute_radio = QPushButton("MUTE RADIO")
        self.btn_mute_radio.setCheckable(True)
        self.btn_mute_radio.setMinimumHeight(32)
        self.btn_mute_radio.clicked.connect(self._on_mute_radio_toggled)
        v_mutes.addWidget(self.btn_mute_radio)

        v_left.addWidget(gb_mutes)

        # Section OVERLAY (sous MUTES, meme style)
        gb_overlay = QGroupBox("OVERLAY")
        v_overlay = QVBoxLayout(gb_overlay)
        v_overlay.setSpacing(6)

        # Affichage overlay (toggle ON/OFF)
        self.btn_overlay_show = QPushButton("Overlay : OFF")
        self.btn_overlay_show.setCheckable(True)
        self.btn_overlay_show.setMinimumHeight(32)
        self.btn_overlay_show.setStyleSheet(
            "padding: 6px 12px;"
        )
        self.btn_overlay_show.clicked.connect(self._on_overlay_show_toggled)
        v_overlay.addWidget(self.btn_overlay_show)

        # Mode edition (positionner les overlays)
        self.btn_overlay_edit = QPushButton("Edition : OFF")
        self.btn_overlay_edit.setCheckable(True)
        self.btn_overlay_edit.setMinimumHeight(32)
        self.btn_overlay_edit.setStyleSheet(
            "padding: 6px 12px;"
        )
        self.btn_overlay_edit.clicked.connect(self._on_overlay_edit_toggled)
        v_overlay.addWidget(self.btn_overlay_edit)

        v_left.addWidget(gb_overlay)
        v_left.addStretch(1)

        body.addWidget(left_panel)

        # === COLONNE DROITE : Mode RP (centre) + Canal (droite) + table ===
        right_panel = QWidget()
        v_right = QVBoxLayout(right_panel)
        v_right.setContentsMargins(0, 0, 0, 0)
        v_right.setSpacing(10)

        # --- Ligne du haut : Mode RP centre + Canal a droite ---
        # Pas de label "Casque" affiche : le client1 lui-meme ne l'affiche
        # pas dans son UI (update_helmet_state est un placeholder vide).
        # Le filtre audio s'applique automatiquement en interne, l'utilisateur
        # n'a pas besoin de voir l'etat detecte.
        h_rp = QHBoxLayout()
        h_rp.setSpacing(8)

        # Stretch a gauche pour pousser Mode RP vers le centre
        h_rp.addStretch(1)

        self.btn_rp_mode = QPushButton("Mode RP : OFF")
        self.btn_rp_mode.setCheckable(True)
        self.btn_rp_mode.setMinimumWidth(160)
        self.btn_rp_mode.setMinimumHeight(32)
        self.btn_rp_mode.setStyleSheet(
            "padding: 6px 12px; font-weight: bold;"
        )
        self.btn_rp_mode.clicked.connect(self._on_rp_mode_toggled)
        h_rp.addWidget(self.btn_rp_mode)

        # Stretch entre Mode RP et Profil/Canal pour pousser a droite
        h_rp.addStretch(1)

        # Mon profil : label readonly affichant le profil assigne par
        # l'admin serveur. Pas une combo (l'utilisateur ne choisit pas
        # son profil, c'est l'admin qui assigne). En "(aucun)" gris si
        # pas de profil, sinon en violet avec le nom assigne. Sert pour
        # le PTT profil : appuyer sur le PTT profil parle uniquement aux
        # joueurs avec le meme profil.
        h_rp.addWidget(QLabel("Profil :"))
        self.lbl_my_profile = QLabel("(aucun)")
        self.lbl_my_profile.setMinimumWidth(100)
        # Padding 4px 8px = meme hauteur visuelle que le QComboBox Canal
        # (qui a 4px de padding interne par defaut + bordure 1px).
        # Pas de font-family imposee : on herite de la police du theme
        # global (comme le QComboBox Canal a cote), pour avoir la meme
        # apparence sur les "(aucun)" des deux widgets.
        self.lbl_my_profile.setStyleSheet(
            f"color: {THEME_MUTED}; "
            "font-weight: bold; padding: 4px 8px; "
            f"background: {THEME_BG_ROW}; border: 1px solid {THEME_BORDER}; "
            "border-radius: 3px;"
        )
        # Forcer la meme hauteur que le combo Canal (qui est setMinimumWidth
        # 140 + hauteur naturelle Qt). On utilise sizeHint d'un QComboBox
        # temporaire pour matcher exactement, mais en pratique fixer la
        # hauteur a celle d'un QLineEdit standard suffit.
        self.lbl_my_profile.setFixedHeight(26)
        h_rp.addWidget(self.lbl_my_profile)

        h_rp.addSpacing(8)

        # Combobox Canal (a droite)
        h_rp.addWidget(QLabel("Canal :"))
        self.cmb_channel = QComboBox()
        self.cmb_channel.setMinimumWidth(140)
        self.cmb_channel.addItem("(aucun)")
        # On marque qu'on veut ignorer les changements provoques par
        # _refresh_channels (pour ne pas re-emettre set_channel en boucle)
        self._channel_combo_updating = False
        self.cmb_channel.currentTextChanged.connect(self._on_channel_selected)
        h_rp.addWidget(self.cmb_channel)

        v_right.addLayout(h_rp)

        # --- Liste joueurs en cards ---
        # Remplace l'ancien QTableWidget par un QScrollArea contenant
        # une suite de PlayerCard. Plus visuel, et les pseudos longs +
        # badges canal/profil restent toujours visibles meme si la
        # fenetre est etroite (le tableau tronquait silencieusement).
        # On stocke les cards dans un dict {name: PlayerCard} pour les
        # update O(1) (anciennement on parcourait le QTableWidget).
        self._player_cards: dict[str, "PlayerCard"] = {}

        scroll_players = QScrollArea()
        scroll_players.setObjectName("PlayersList")
        scroll_players.setWidgetResizable(True)
        # On laisse le frame Qt natif desactive : on dessine notre propre
        # cadre via QSS pour avoir le border-radius (le frame Qt natif
        # n'a pas de coins arrondis).
        scroll_players.setFrameShape(QFrame.NoFrame)
        scroll_players.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Cadre discret autour de la liste : surclasse la regle globale
        # `QScrollArea { border: none; }` du THEME_QSS grace au selecteur
        # ID (specificite plus forte).
        # IMPORTANT : des qu'on met un setStyleSheet local sur un widget,
        # Qt isole ce widget du THEME_QSS global pour le rendu de ses
        # sous-elements. Donc on doit RE-DECLARER ici les regles QScrollBar
        # (sinon la scrollbar retombe en rendu Windows par defaut = blanc).
        # Les regles ci-dessous sont une copie identique de celles du
        # THEME_QSS pour preserver le look sombre coherent.
        scroll_players.setStyleSheet(
            f"QScrollArea#PlayersList {{ "
            f"  border: 1px solid {THEME_BORDER}; "
            f"  border-radius: 4px; "
            f"  background: {THEME_BG_CLIENT}; "
            f"}}"
            f"QScrollArea#PlayersList QScrollBar:vertical {{ "
            f"  background: {THEME_BG_PANEL}; "
            f"  width: 10px; "
            f"  border: none; "
            f"}}"
            f"QScrollArea#PlayersList QScrollBar::handle:vertical {{ "
            f"  background: {THEME_BORDER}; "
            f"  border-radius: 3px; "
            f"  min-height: 20px; "
            f"}}"
            f"QScrollArea#PlayersList QScrollBar::handle:vertical:hover {{ "
            f"  background: {THEME_MUTED}; "
            f"}}"
            f"QScrollArea#PlayersList QScrollBar::add-line:vertical, "
            f"QScrollArea#PlayersList QScrollBar::sub-line:vertical {{ "
            f"  height: 0; "
            f"}}"
        )

        self._players_container = QWidget()
        self._players_layout = QVBoxLayout(self._players_container)
        self._players_layout.setContentsMargins(2, 2, 2, 2)
        self._players_layout.setSpacing(6)
        self._players_layout.addStretch(1)  # pousse les cards vers le haut
        scroll_players.setWidget(self._players_container)

        v_right.addWidget(scroll_players, stretch=1)

        body.addWidget(right_panel, stretch=1)

        v.addLayout(body, stretch=1)

    def _build_page_settings(self):
        """Page settings : 2 colonnes.
        - Colonne gauche : Raccourcis (PTT + toggles mute) + Mise a jour
        - Colonne droite : Audio (devices, gain, gate, VU) + OCR avance
                           + Zone OCR

        La page entiere est encapsulee dans un QScrollArea pour permettre
        de scroller verticalement si la fenetre est petite (raccourcis = 8
        lignes, peut depasser sur des ecrans 1080p)."""
        # Conteneur interne qui recevra le layout 2 colonnes
        inner = QWidget()
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        # Layout 2 colonnes
        cols = QHBoxLayout()
        cols.setSpacing(12)

        # === COLONNE GAUCHE : Raccourcis + MAJ ===
        col_left = QWidget()
        v_left = QVBoxLayout(col_left)
        v_left.setContentsMargins(0, 0, 0, 0)
        v_left.setSpacing(12)

        # Section Raccourcis
        gb_radio = QGroupBox("Raccourcis")
        gb_radio.setStyleSheet("QGroupBox { font-weight: bold; padding-top: 14px; }")
        v_radio = QVBoxLayout(gb_radio)
        v_radio.setSpacing(6)

        def _make_key_row(parent_layout, label_txt: str, kind_id: str):
            """Helper : cree une ligne 'Label  [valeur]  [Definir...]'."""
            h = QHBoxLayout()
            lbl = QLabel(label_txt)
            lbl.setMinimumWidth(170)
            h.addWidget(lbl)
            val_lbl = QLabel("(aucune)")
            val_lbl.setStyleSheet(
                "font-family: Consolas, monospace; font-size: 10pt; "
                "padding: 4px 8px; background: #222; color: #ccc; "
                "border: 1px solid #444; min-width: 100px;"
            )
            h.addWidget(val_lbl, stretch=1)
            btn = QPushButton("Definir...")
            btn.clicked.connect(lambda _=False, k=kind_id: self._capture_key(k))
            h.addWidget(btn)
            parent_layout.addLayout(h)
            return val_lbl

        self.lbl_radio_key      = _make_key_row(v_radio, "Radio canal (PTT) :",      "radio")
        self.lbl_profile_key    = _make_key_row(v_radio, "Radio profil (PTT) :",     "profile")
        self.lbl_mute_mic_key   = _make_key_row(v_radio, "Mute micro :",             "mute_mic")
        self.lbl_mute_prox_key  = _make_key_row(v_radio, "Mute proximite :",         "mute_prox")
        self.lbl_mute_radio_key = _make_key_row(v_radio, "Mute radio :",             "mute_radio")
        self.lbl_mute_all_key   = _make_key_row(v_radio, "Mute tout :",              "mute_all")
        self.lbl_prox_short_key = _make_key_row(v_radio, "Proximite 30m / 5m :",     "prox_short")
        self.lbl_cycle_ch_key   = _make_key_row(v_radio, "Cycle canal radio :",      "cycle_channel")

        v_left.addWidget(gb_radio)
        # Initialiser l'affichage des touches depuis state
        self._refresh_radio_key_labels()

        # Section Mise a jour : MASQUEE en release 0.1 publique.
        # On garde le QPushButton instancie parce qu'il est reference par
        # plusieurs methodes (_on_check_update_clicked, _set_update_button_style,
        # les callbacks de _check_for_updates, etc.) et le code derriere
        # reste fonctionnel pour les builds dev (qui peuvent appeler la
        # verification via menu cache, debug, ou raccourci).
        # Pour reactiver l'UI : remettre les v_upd.addWidget(self.btn_check_update)
        # et v_left.addWidget(gb_upd) ci-dessous.
        gb_upd = QGroupBox("Mise a jour")
        gb_upd.setStyleSheet("QGroupBox { font-weight: bold; padding-top: 14px; }")
        v_upd = QVBoxLayout(gb_upd)
        v_upd.setSpacing(6)
        self.btn_check_update = QPushButton("Verifier les MAJ")
        self.btn_check_update.setMinimumHeight(28)
        self._set_update_button_style(False)
        self.btn_check_update.clicked.connect(self._on_check_update_clicked)
        # v_upd.addWidget(self.btn_check_update)  # masque en release 0.1
        # v_left.addWidget(gb_upd)                # masque en release 0.1

        v_left.addStretch(1)
        cols.addWidget(col_left, stretch=1)

        # === COLONNE DROITE : Audio + OCR + Zone OCR ===
        col_right = QWidget()
        v_right = QVBoxLayout(col_right)
        v_right.setContentsMargins(0, 0, 0, 0)
        v_right.setSpacing(12)

        # Section Audio (devices + gain + gate + VU). Reutilise la
        # methode existante qui ajoute un QGroupBox "Audio" complet.
        self._build_audio_panel(v_right)

        # Section OCR
        gb_ocr = QGroupBox("OCR (avance)")
        gb_ocr.setStyleSheet("QGroupBox { font-weight: bold; padding-top: 14px; }")
        v_ocr = QVBoxLayout(gb_ocr)
        v_ocr.setSpacing(8)

        self.cb_ocr_force_cpu = QCheckBox("Forcer le mode CPU pour l'OCR (au lieu du GPU)")
        # Etat initial depuis la config client1 si dispo, sinon false
        force_cpu = False
        if _CORE_AVAILABLE:
            try:
                core_cfg = _core._load_client_cfg()
                force_cpu = bool(core_cfg.get("ocr_force_cpu", False))
            except Exception:
                pass
        else:
            force_cpu = bool(self._cfg.get("ocr_force_cpu", False))
        self.cb_ocr_force_cpu.setChecked(force_cpu)
        self.cb_ocr_force_cpu.toggled.connect(self._on_ocr_force_cpu_toggled)
        v_ocr.addWidget(self.cb_ocr_force_cpu)

        self.lbl_ocr_mode_info = QLabel("")
        self.lbl_ocr_mode_info.setStyleSheet("color: #888; font-size: 9pt;")
        self.lbl_ocr_mode_info.setWordWrap(True)
        self._refresh_ocr_mode_info()
        v_ocr.addWidget(self.lbl_ocr_mode_info)

        v_right.addWidget(gb_ocr)

        # Section Zone OCR
        gb_zone = QGroupBox("Zone OCR (HUD Star Citizen)")
        gb_zone.setStyleSheet("QGroupBox { font-weight: bold; padding-top: 14px; }")
        v_zone = QVBoxLayout(gb_zone)
        v_zone.setSpacing(8)

        self.lbl_zone_info = QLabel("")
        self.lbl_zone_info.setStyleSheet("color: #ccc; font-size: 9pt;")
        self.lbl_zone_info.setWordWrap(True)
        v_zone.addWidget(self.lbl_zone_info)

        h_zone_btns = QHBoxLayout()
        self.btn_zone_recalc = QPushButton("Recalculer auto")
        self.btn_zone_recalc.clicked.connect(self._on_zone_recalc)
        h_zone_btns.addWidget(self.btn_zone_recalc)
        # Bouton calibration manuelle
        self.btn_zone_manual = QPushButton("Calibrer manuellement")
        self.btn_zone_manual.clicked.connect(self._on_zone_calibrate_manual)
        h_zone_btns.addWidget(self.btn_zone_manual)
        h_zone_btns.addStretch(1)
        v_zone.addLayout(h_zone_btns)

        self._refresh_zone_info()

        v_right.addWidget(gb_zone)

        v_right.addStretch(1)
        cols.addWidget(col_right, stretch=1)

        outer.addLayout(cols)

        # Encapsuler dans un QScrollArea pour permettre le scroll vertical
        # sur de petites fenetres ou ecrans peu hauts. setWidgetResizable
        # garantit que le widget interne s'adapte a la largeur du scroll
        # area (sinon il aurait sa taille naturelle, qui est petite, et la
        # zone droite de la fenetre serait vide).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.NoFrame)  # pas de bordure visible
        # _page_settings est ce qu'on ajoute au QStackedWidget : c'est la
        # scroll area, pas le widget interne.
        self._page_settings = scroll

    # ------------------------------------------------------------------
    # Updater : check au boot + bouton + dialog + apply
    # ------------------------------------------------------------------
    def _update_check_worker(self):
        """Worker thread qui interroge le serveur d'update au demarrage.
        S'execute une seule fois en arriere-plan, n'a pas d'impact si le
        serveur n'est pas joignable. Si MAJ dispo, le signal Qt remonte
        l'info au main thread qui met a jour le bouton."""
        # Petit delai pour ne pas concurrencer les autres threads d'init
        time.sleep(2.0)
        try:
            ip = (self._cfg.get("server_ip") or "").strip()
        except Exception:
            ip = ""
        if not ip:
            return
        try:
            manifest = _check_for_updates(ip)
        except Exception:
            manifest = None
        if manifest:
            try:
                # Emettre dans le main thread via le signal Qt.
                # _sig_update_available est connecte a _on_update_available
                # plus bas dans __init__ (au moment du _build_ui).
                self._sig_update_available.emit(manifest)
            except Exception:
                pass

    @Slot(dict)
    def _on_update_available(self, manifest: dict):
        """Slot appele par _update_check_worker quand une MAJ est detectee.
        Stocke le manifest et passe le bouton en orange."""
        self._pending_update = manifest
        self._set_update_button_style(True, manifest)

    def _set_update_button_style(self, has_update: bool, manifest: dict = None):
        """Style le bouton 'Verifier les MAJ' selon qu'il y en a une dispo
        ou pas. has_update=False -> gris (defaut). has_update=True -> orange,
        avec le numero de version dans le label."""
        try:
            if has_update and manifest:
                ver = (
                    f"{manifest.get('version','?')} "
                    f"{manifest.get('channel','?')} "
                    f"{int(manifest.get('build',0)):03d}"
                )
                self.btn_check_update.setText(f"MAJ : {ver}")
                # Orange (#d29922) pour attirer l'attention
                self.btn_check_update.setStyleSheet(
                    "padding: 4px 10px; font-size: 9pt; "
                    "background:#d29922; color:#0d1117; font-weight: bold;"
                )
            else:
                self.btn_check_update.setText("Verifier les MAJ")
                # Gris discret (couleur par defaut PySide)
                self.btn_check_update.setStyleSheet(
                    "padding: 4px 10px; font-size: 9pt;"
                )
        except Exception:
            pass

    def _on_check_update_clicked(self):
        """Clic sur le bouton 'Verifier les mises a jour'."""
        # Si un manifest est deja en attente, demander confirmation pour
        # appliquer.
        if self._pending_update:
            self._show_update_dialog(self._pending_update)
            return
        # Sinon : relancer un check (avec retour visuel cette fois).
        ip = (self._cfg.get("server_ip") or "").strip()
        if not ip:
            QMessageBox.warning(
                self,
                "CircusVOIP - Mise a jour",
                "Pas d'IP serveur configuree.\n"
                "Configurez d'abord l'IP serveur dans le champ ci-dessus."
            )
            return
        # Feedback visuel immediat : on grise le bouton et on indique
        # "Verification..." pour que l'utilisateur sache que le clic a ete
        # pris en compte (sinon clic silencieux = on doute si ca marche).
        self.btn_check_update.setEnabled(False)
        self.btn_check_update.setText("Verification...")
        self._on_log(f"[UPDATE] Verification manuelle (serveur {ip})...")

        # Lance le check dans un thread daemon (timeout 5s).
        # Try/except global pour eviter qu'une exception non catchee tue
        # le thread silencieusement et laisse le bouton fige.
        def _do_check():
            try:
                manifest = _check_for_updates(ip)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[UPDATE] Exception check : {e}")
                    except Exception:
                        pass
                manifest = None
                err_msg = str(e)
            else:
                err_msg = ""
            # Signal -> main thread (thread-safe, contrairement a
            # QTimer.singleShot qui meurt en silence cross-thread).
            # On envoie {} pour "pas de MAJ" (le dict est non-nullable
            # dans la signature Signal Qt).
            self._sig_update_check_done.emit(manifest or {}, err_msg)

        threading.Thread(
            target=_do_check, daemon=True, name="c2-update-recheck"
        ).start()

    @Slot(dict, str)
    def _on_update_check_done(self, manifest: dict, err_msg: str):
        """Slot appele dans le main thread quand _do_check a termine.
        Branche par _sig_update_check_done (thread-safe cross-thread).
        manifest = {} signifie "pas de MAJ disponible" ; err_msg vide
        signifie "pas d'erreur"."""
        # Restaurer le bouton dans tous les cas
        self.btn_check_update.setEnabled(True)
        if manifest:
            # _set_update_button_style va remettre le bon texte
            # ("MAJ : ...") + couleur orange
            self._set_update_button_style(True, manifest)
            self._show_update_dialog(manifest)
            return
        self._set_update_button_style(False, None)
        if err_msg:
            self._on_log(f"[UPDATE] Verification echouee : {err_msg}")
            box = QMessageBox(
                QMessageBox.Warning,
                "CircusVOIP - Mise a jour",
                f"Impossible de verifier les MAJ :\n\n{err_msg}\n\n"
                f"Verifiez l'IP serveur et la connexion reseau.",
                QMessageBox.Ok,
                self,
            )
        else:
            self._on_log(f"[UPDATE] Deja a jour : {_VERSION_STRING}")
            box = QMessageBox(
                QMessageBox.Information,
                "CircusVOIP - Mise a jour",
                f"Vous avez deja la derniere version :\n"
                f"{_VERSION_STRING}",
                QMessageBox.Ok,
                self,
            )
        # Forcer la box au premier plan : sans ces flags, elle peut
        # s'ouvrir derriere la fenetre principale selon le focus Windows
        # et passer inapercue.
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.raise_()
        box.activateWindow()
        box.exec()

    def _show_update_dialog(self, manifest: dict):
        """Boite de dialogue qui affiche les notes de release et propose
        d'appliquer ou de differer la MAJ."""
        ver = (
            f"{manifest.get('version','?')} "
            f"{manifest.get('channel','?')} "
            f"{int(manifest.get('build',0)):03d}"
        )
        notes = manifest.get("release_notes", "(pas de notes)")
        date  = manifest.get("release_date", "?")
        n_files = len(manifest.get("files", []))
        n_pip   = len(manifest.get("pip_packages", []))
        msg = (
            f"Une nouvelle version est disponible :\n\n"
            f"  Version : {ver}\n"
            f"  Date    : {date}\n"
            f"  Fichiers : {n_files} | Wheels pip : {n_pip}\n"
            f"  Local   : {_VERSION_STRING}\n\n"
            f"Notes :\n{notes}\n\n"
            f"Appliquer maintenant ? Le client redemarrera automatiquement."
        )
        # Construire la box manuellement plutot que QMessageBox.question()
        # pour pouvoir la forcer au premier plan (sinon elle peut s'ouvrir
        # derriere la fenetre principale et passer inapercue).
        box = QMessageBox(
            QMessageBox.Question,
            "CircusVOIP - Mise a jour disponible",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            self,
        )
        box.setDefaultButton(QMessageBox.Yes)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.raise_()
        box.activateWindow()
        ret = box.exec()
        if ret != QMessageBox.Yes:
            return
        self._apply_pending_update(manifest)

    def _apply_pending_update(self, manifest: dict):
        """Lance _apply_update dans un thread (download peut prendre
        quelques secondes), puis relance le client si OK."""
        ip = (self._cfg.get("server_ip") or "").strip()
        if not ip:
            QMessageBox.warning(
                self,
                "CircusVOIP - Mise a jour",
                "Pas d'IP serveur configuree."
            )
            return
        # Log + UI : on indique qu'on telecharge
        self._on_log("[UPDATE] Telechargement en cours...")
        self.btn_check_update.setEnabled(False)
        self.btn_check_update.setText("Telechargement...")

        def _do_apply():
            # Try/except global pour eviter qu'une exception non catchee
            # tue le thread silencieusement (le bouton resterait fige sur
            # "Telechargement..." sans aucun feedback).
            try:
                success, msg = _apply_update(ip, manifest)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[UPDATE] Exception apply : {e}"
                        )
                    except Exception:
                        pass
                success, msg = False, f"Exception : {e}"
            # Stocker le manifest pour le slot (qui en a besoin pour le
            # cas d'erreur, restaurer le bouton orange).
            self._pending_apply_manifest = manifest
            # Emit signal Qt -> main thread via QueuedConnection auto.
            # Thread-safe contrairement a QTimer.singleShot.
            self._sig_update_applied.emit(success, msg)

        threading.Thread(
            target=_do_apply, daemon=True, name="c2-update-apply"
        ).start()

    @Slot(bool, str)
    def _on_update_applied(self, success: bool, msg: str):
        """Slot appele dans le main thread quand _do_apply a termine.
        Branche par _sig_update_applied (thread-safe cross-thread)."""
        manifest = self._pending_apply_manifest or {}
        self.btn_check_update.setEnabled(True)
        if success:
            # Sauver la config courante avant restart pour ne rien
            # perdre. _save_cfg fait un merge avec disque.
            try:
                _save_cfg(self._cfg)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[UPDATE] _save_cfg pre-restart : {e}"
                        )
                    except Exception:
                        pass
            # MAJ appliquee : on nettoie le manifest en attente. Pas
            # vital car le process meurt juste apres, mais coherent.
            self._pending_update = None
            self._pending_apply_manifest = None
            # Log clair et restart immediat. Pas de QMessageBox
            # bloquante ici : si on en met une, le restart
            # n'arrive qu'apres clic OK utilisateur (ou jamais
            # si la box passe derriere la fenetre principale).
            self._on_log(
                f"[UPDATE] {msg} - Redemarrage immediat..."
            )
            # Petit delai pour que le log s'ecrive sur disque
            # avant qu'on tue le process. 200ms suffisent.
            # singleShot ici est OK car on est deja dans le main thread.
            QTimer.singleShot(200, _restart_client)
        else:
            # Echec : on remet le bouton en orange (MAJ toujours dispo) mais
            # on EFFACE _pending_update pour forcer un re-check au prochain
            # clic. Sinon on reapplique aveuglement le meme manifest, qui
            # peut etre obsolete si le serveur en a publie un nouveau
            # entre-temps (cas typique : 1ere tentative echoue car le
            # serveur poussait justement la build suivante).
            self._pending_update = None
            self._pending_apply_manifest = None
            box = QMessageBox(
                QMessageBox.Critical,
                "CircusVOIP - Echec mise a jour",
                f"La mise a jour a echoue :\n\n{msg}\n\n"
                f"Le client continue en version actuelle "
                f"({_VERSION_STRING}).",
                QMessageBox.Ok,
                self,
            )
            box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            box.raise_()
            box.activateWindow()
            box.exec()
            # Apres echec : on remet le bouton en gris pour forcer un
            # re-check au prochain clic (cf. bug fix : sinon on
            # reapplique aveuglement le meme manifest meme si le
            # serveur en a publie un nouveau entre-temps).
            self._set_update_button_style(False, None)

    @Slot(bool)
    def _on_settings_toggled(self, checked: bool):
        """Bouton PARAMETRES en haut a droite : swap entre les 2 pages.
        Le formulaire de connexion + le label position OCR sont aussi
        masques en page Parametres (ils ne servent a rien la-bas)."""
        if checked:
            self._refresh_zone_info()  # rafraichir au cas ou la zone a change
            self.stack.setCurrentWidget(self._page_settings)
            self.btn_settings.setText("RETOUR MENU")
            if hasattr(self, "_main_header_box"):
                self._main_header_box.setVisible(False)
        else:
            self.stack.setCurrentWidget(self._page_main)
            self.btn_settings.setText("PARAMETRES")
            if hasattr(self, "_main_header_box"):
                self._main_header_box.setVisible(True)

    @Slot(bool)
    def _on_ocr_force_cpu_toggled(self, checked: bool):
        """Toggle OCR force CPU : ecrit dans la config CLIENT1
        (circusvoip_client_config.json) car c'est ce config-la que
        circusvoip_client.py lit au demarrage pour decider GPU vs CPU.
        Notre config client2 n'est pas lue par le code OCR du client1."""
        if _CORE_AVAILABLE:
            try:
                core_cfg = _core._load_client_cfg()
                core_cfg["ocr_force_cpu"] = checked
                _core._save_client_cfg(core_cfg)
                self._on_log(f"[OCR] force_cpu={checked} sauve dans "
                             f"circusvoip_client_config.json")
            except Exception as e:
                self._on_log(f"[OCR] Echec ecriture config client1 : {e}")
        # On garde aussi une copie dans notre config (au cas ou)
        self._cfg["ocr_force_cpu"] = checked
        _save_cfg(self._cfg)
        self._refresh_ocr_mode_info()
        QMessageBox.information(
            self,
            "CircusVOIP",
            f"Mode OCR : {'CPU force' if checked else 'GPU (auto)'}\n\n"
            "Le changement sera applique au prochain demarrage de "
            "CircusVOIP (EasyOCR ne peut pas etre reinitialise a chaud).",
        )

    def _refresh_ocr_mode_info(self):
        if not hasattr(self, "lbl_ocr_mode_info"):
            return
        if self.cb_ocr_force_cpu.isChecked():
            self.lbl_ocr_mode_info.setText(
                "Mode actuel : CPU (force).\n"
                "Plus lent mais marche sans GPU ou avec GPU instable. "
                "Effectif au prochain demarrage."
            )
            self.lbl_ocr_mode_info.setStyleSheet(
                "color: #ffaa44; font-size: 9pt;"
            )
        else:
            self.lbl_ocr_mode_info.setText(
                "Mode actuel : GPU automatique.\n"
                "Plus rapide. Si l'OCR plante ou que le GPU sature, "
                "basculer en CPU."
            )
            self.lbl_ocr_mode_info.setStyleSheet(
                "color: #88dd88; font-size: 9pt;"
            )

    def _refresh_zone_info(self):
        """Met a jour le label d'info zone OCR avec la zone actuelle."""
        if not hasattr(self, "lbl_zone_info"):
            return
        z = getattr(state, "zone_coords", None)
        if not z:
            self.lbl_zone_info.setText("Zone non initialisee.")
            return
        try:
            txt = (f"Position : ({z['left']}, {z['top']})\n"
                   f"Taille   : {z['width']} x {z['height']} px\n"
                   f"Gamma    : {z.get('gamma', 0.5)}")
            self.lbl_zone_info.setText(txt)
        except Exception as e:
            self.lbl_zone_info.setText(f"(zone illisible : {e})")

    @Slot()
    def _on_zone_recalc(self):
        """Recalcule la zone via auto_ocr_zone() apres avoir demande a
        l'utilisateur sur quel ecran tourne Star Citizen.
        Reproduit le comportement de _auto_zone du client1 (ligne 9623+).
        Sur 1 ecran : pas de question, calcule directement."""
        if not _CORE_AVAILABLE:
            QMessageBox.warning(self, "CircusVOIP",
                                "Module client1 non disponible.")
            return
        try:
            mons = _sco.list_monitors()
        except Exception as e:
            QMessageBox.critical(self, "CircusVOIP",
                                 f"Impossible de lister les ecrans : {e}")
            return
        if not mons:
            QMessageBox.warning(self, "CircusVOIP",
                                "Aucun ecran detecte.")
            return
        if len(mons) == 1:
            # Un seul ecran : skip le picker
            self._apply_auto_zone(mons[0])
            return

        # Plusieurs ecrans : demander a l'utilisateur via MonitorPicker
        # On reutilise le flow existant mais avec un callback qui appelle
        # auto_ocr_zone(mon) au lieu d'ouvrir un selecteur de region.
        try:
            self.hide()
        except Exception:
            pass
        self._auto_zone_pickers = []
        for i, mon in enumerate(mons):
            picker = MonitorPickerWindow(mon, i, len(mons))
            picker.sig_picked.connect(self._on_auto_zone_monitor_picked)
            picker.show()
            self._auto_zone_pickers.append(picker)

    @Slot(object)
    def _on_auto_zone_monitor_picked(self, mon):
        # Fermer tous les pickers
        for p in getattr(self, "_auto_zone_pickers", []):
            try:
                p.close()
            except Exception:
                pass
        self._auto_zone_pickers = []
        try:
            self.show()
        except Exception:
            pass
        if mon is None:
            self._on_log("[OCR] Recalcul auto annule")
            return
        self._apply_auto_zone(mon)

    def _apply_auto_zone(self, mon: dict):
        """Calcule auto_ocr_zone(mon) et applique."""
        try:
            new_zone = _sco.auto_ocr_zone(mon)
            state.zone_coords = new_zone
            self._on_log(f"[OCR] Zone auto recalculee sur ecran "
                         f"{mon['width']}x{mon['height']} : "
                         f"{new_zone['width']}x{new_zone['height']} a "
                         f"({new_zone['left']},{new_zone['top']})")
            # Sauver dans le config client1 comme le fait _auto_zone du client1
            try:
                core_cfg = _core._load_client_cfg()
                core_cfg["zone_coords"] = new_zone
                core_cfg["zone_source"] = "auto"
                _core._save_client_cfg(core_cfg)
            except Exception as e:
                self._on_log(f"[OCR] Echec ecriture config client1 : {e}")
            self._refresh_zone_info()
            QMessageBox.information(
                self,
                "CircusVOIP",
                f"Nouvelle zone : {new_zone['width']}x{new_zone['height']} "
                f"a ({new_zone['left']},{new_zone['top']})\n\n"
                "L'OCR utilisera cette zone immediatement, pas besoin de "
                "redemarrer."
            )
        except Exception as e:
            QMessageBox.critical(self, "CircusVOIP", f"Echec : {e}")

    @Slot()
    def _on_zone_calibrate_manual(self):
        """Lance le flow de calibration manuelle :
        1. Si plus d'un ecran -> picker bleu sur chaque ecran
        2. Sur l'ecran choisi : selecteur noir avec rectangle a tracer
        3. Sauve la zone dans state.zone_coords + dans les 2 configs."""
        if not _CORE_AVAILABLE:
            QMessageBox.warning(self, "CircusVOIP",
                                "Module client1 non disponible, "
                                "calibration impossible.")
            return
        # Garder une ref pour eviter le garbage collect du QObject
        self._calib_flow = CalibrationFlow(self)
        self._calib_flow.sig_calibrated.connect(self._on_calibration_result)
        self._calib_flow.start()

    @Slot(object)
    def _on_calibration_result(self, zone):
        """Callback de fin de calibration : zone est un dict ou None."""
        # Liberer la reference au flow (le QObject sera garbage-collecte)
        self._calib_flow = None
        if zone is None:
            self._on_log("[OCR] Calibration manuelle annulee")
            return
        try:
            state.zone_coords = zone
            self._on_log(f"[OCR] Zone calibree manuellement : "
                         f"{zone['width']}x{zone['height']} a "
                         f"({zone['left']},{zone['top']}) gamma={zone.get('gamma', 0.5)}")
            # Sauvegarder dans le config CLIENT1 (c'est lui qui est lu au
            # demarrage de l'OCR via _load_client_cfg). On preserve aussi
            # zone_source = "manuel" comme le client1 le fait.
            try:
                core_cfg = _core._load_client_cfg()
                core_cfg["zone_coords"] = zone
                core_cfg["zone_source"] = "manuel"
                _core._save_client_cfg(core_cfg)
                self._on_log("[OCR] Zone sauvee dans circusvoip_client_config.json")
            except Exception as e:
                self._on_log(f"[OCR] Echec ecriture config client1 : {e}")
            self._refresh_zone_info()
            QMessageBox.information(
                self,
                "CircusVOIP",
                f"Zone calibree : {zone['width']}x{zone['height']} a "
                f"({zone['left']},{zone['top']})\n\n"
                "L'OCR utilisera cette zone immediatement."
            )
        except Exception as e:
            QMessageBox.critical(self, "CircusVOIP",
                                 f"Echec sauvegarde calibration : {e}")

    # ------------------------------------------------------------------
    # Radio PTT + Mode RP + Helmet detection
    # ------------------------------------------------------------------

    def _refresh_radio_key_labels(self):
        """Met a jour l'affichage de toutes les touches dans la page settings.
        Utilise core.format_hotkey_for_display pour transformer la forme
        canonique stockee ('ctrl+shift+m') en forme utilisateur lisible
        ('Ctrl + Shift + M')."""
        if not hasattr(self, "lbl_radio_key"):
            return
        # Liste de tuples (attr_label, attr_state)
        rows = [
            ("lbl_radio_key",      "radio_key"),
            ("lbl_profile_key",    "profile_radio_key"),
            ("lbl_mute_mic_key",   "mute_mic_key"),
            ("lbl_mute_prox_key",  "mute_prox_key"),
            ("lbl_mute_radio_key", "mute_radio_key"),
            ("lbl_mute_all_key",   "mute_all_key"),
            ("lbl_prox_short_key", "proximity_short_key"),
            ("lbl_cycle_ch_key",   "cycle_channel_key"),
        ]
        for lbl_attr, state_attr in rows:
            lbl = getattr(self, lbl_attr, None)
            if lbl is None:
                continue
            val = getattr(state, state_attr, None)
            if val:
                try:
                    pretty = _core.format_hotkey_for_display(val)
                except Exception:
                    pretty = str(val)
            else:
                pretty = "(aucune)"
            lbl.setText(pretty)

    # ---- Callbacks pynput (appeles depuis thread pynput) ----
    # Ils emit un signal Qt qui passe automatiquement par QueuedConnection
    # vers le main thread Qt (thread-safe par design des signaux Qt).
    # On NE PEUT PAS utiliser QTimer.singleShot ici car on est dans un
    # thread non-Qt (thread pynput).

    def _on_hotkey_mute_mic(self):
        try: _core._dbg_log("[HOTKEY] mute_mic press")
        except Exception: pass
        self._sig_hotkey.emit("mute_mic")

    def _on_hotkey_mute_prox(self):
        try: _core._dbg_log("[HOTKEY] mute_prox press")
        except Exception: pass
        self._sig_hotkey.emit("mute_prox")

    def _on_hotkey_mute_radio(self):
        try: _core._dbg_log("[HOTKEY] mute_radio press")
        except Exception: pass
        self._sig_hotkey.emit("mute_radio")

    def _on_hotkey_mute_all(self):
        try: _core._dbg_log("[HOTKEY] mute_all press")
        except Exception: pass
        self._sig_hotkey.emit("mute_all")

    def _on_hotkey_prox_short(self):
        try: _core._dbg_log("[HOTKEY] prox_short press")
        except Exception: pass
        self._sig_hotkey.emit("prox_short")

    def _on_hotkey_cycle_channel(self):
        try: _core._dbg_log("[HOTKEY] cycle_channel press")
        except Exception: pass
        self._sig_hotkey.emit("cycle_channel")

    def _on_hotkey_profile_pressed(self):
        try: state.profile_radio_active = True
        except Exception: pass

    def _on_hotkey_profile_released(self):
        try: state.profile_radio_active = False
        except Exception: pass

    @Slot(str)
    def _on_hotkey_dispatch(self, name: str):
        """Recoit un evenement hotkey emis depuis un thread pynput. Appelle
        l'action correspondante dans le main thread Qt."""
        try: _core._dbg_log(f"[HOTKEY] dispatch (Qt thread) : {name}")
        except Exception: pass
        actions = {
            "mute_mic":      self._do_toggle_mute_mic,
            "mute_prox":     self._do_toggle_mute_prox,
            "mute_radio":    self._do_toggle_mute_radio,
            "mute_all":      self._do_toggle_mute_all,
            "prox_short":    self._do_toggle_prox_short,
            "cycle_channel": self._do_cycle_channel,
        }
        action = actions.get(name)
        if action is not None:
            try:
                action()
            except Exception as e:
                try: _core._dbg_log(f"[HOTKEY] action {name} KO : {e}")
                except Exception: pass

    # ---- Toggles effectifs (main thread Qt) ----
    # Reproduisent _toggle_mute*, _toggle_proximity_short, _cycle_channel
    # du client1.

    def _do_toggle_mute_mic(self):
        if state.audio_io is None:
            return
        state.audio_muted = not state.audio_muted
        try:
            state.audio_io.set_capture_muted(state.audio_muted)
        except Exception as e:
            # Race possible : audio_io peut devenir None entre les deux
            # checks si un cleanup closeEvent tourne en parallele d'un
            # hotkey pynput. On log pour diagnostiquer plutot que
            # d'avaler l'erreur.
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MUTE] set_capture_muted KO : {e}"
                    )
                except Exception:
                    pass
        self._refresh_mute_button()
        self._on_log(f"[MUTE] mic = {state.audio_muted}")

    def _do_toggle_mute_prox(self):
        state.mute_proximity = not state.mute_proximity
        self._on_log(f"[MUTE] proximity = {state.mute_proximity}")
        self._refresh_mute_button()

    def _do_toggle_mute_radio(self):
        state.mute_radio = not state.mute_radio
        self._on_log(f"[MUTE] radio = {state.mute_radio}")
        self._refresh_mute_button()

    def _do_toggle_mute_all(self):
        """Interrupteur 2 positions stateful (option A) :
        - 1ere pression -> mute tout (peu importe l'etat individuel courant)
        - 2eme pression -> demute tout
        Le state.mute_all_state suit la position de l'interrupteur. Si
        l'utilisateur (de)mute individuellement entre temps, l'interrupteur
        garde sa position : la prochaine pression inverse simplement la
        position. Cela evite la situation 'j'ai mute tout, j'ai mute mic
        manuellement, je presse mute_all : ca demute tout' qui est
        contre-intuitive."""
        # Initialiser le state si premier appui de la session
        new_state = not bool(getattr(state, "mute_all_state", False))
        state.mute_all_state = new_state

        # Mic
        if state.audio_io is not None:
            state.audio_muted = new_state
            try:
                state.audio_io.set_capture_muted(new_state)
            except Exception:
                pass
        # Proximity
        state.mute_proximity = new_state
        # Radio
        state.mute_radio = new_state

        self._refresh_mute_button()
        self._on_log(f"[MUTE] tout {'mute' if new_state else 'demute'}")

    def _do_toggle_prox_short(self):
        state.proximity_short = not getattr(state, "proximity_short", False)
        self._on_log(f"[PROX] proximity_short = {state.proximity_short}")
        # Diffuser au serveur (les autres clients filtreront notre voix)
        try:
            if _CORE_AVAILABLE:
                _core._ws_send_safe({
                    "type": "prox_short",
                    "active": bool(state.proximity_short),
                })
        except Exception:
            pass

    def _do_cycle_channel(self):
        """Cycle parmi state.channels_list. Sequence (aucun) puis les canaux,
        boucle a la fin. Fait un set_channel via WS (le serveur broadcast
        et state.my_channel sera mis a jour au retour)."""
        if not getattr(state, "connected", False):
            self._on_log("[CHANNEL] Cycle ignore : pas connecte")
            return
        if not _CORE_AVAILABLE:
            return
        try:
            channels = list(getattr(state, "channels_list", []) or [])
            # Sequence : None (aucun) puis les canaux
            sequence = [None] + channels
            try:
                idx = sequence.index(getattr(state, "my_channel", None))
            except ValueError:
                idx = -1  # current pas dans la liste, on prendra le premier
            new_ch = sequence[(idx + 1) % len(sequence)]
            ok = _core._ws_send_safe({"type": "set_channel", "channel": new_ch})
            if ok:
                self._on_log(f"[CHANNEL] Cycle vers : {new_ch or '(aucun)'}")
            else:
                self._on_log("[CHANNEL] Cycle echoue (WS pas pret)")
        except Exception as e:
            self._on_log(f"[CHANNEL] Cycle KO : {e}")

    def _capture_key(self, kind: str):
        """Ouvre un dialog de capture de touche, applique le resultat.
        kind dans : 'radio', 'profile', 'mute_mic', 'mute_prox',
        'mute_radio', 'mute_all', 'prox_short', 'cycle_channel'."""
        if not _CORE_AVAILABLE:
            QMessageBox.warning(self, "CircusVOIP",
                                "Module client1 non disponible.")
            return
        # kind -> (label dialog, attribut state, cle config)
        kinds = {
            "radio":         ("Radio canal (PTT)",     "radio_key",           "radio_key"),
            "profile":       ("Radio profil (PTT)",    "profile_radio_key",   "profile_radio_key"),
            "mute_mic":      ("Mute micro (toggle)",   "mute_mic_key",        "mute_mic_key"),
            "mute_prox":     ("Mute proximite",        "mute_prox_key",       "mute_prox_key"),
            "mute_radio":    ("Mute radio",            "mute_radio_key",      "mute_radio_key"),
            "mute_all":      ("Mute tout",              "mute_all_key",        "mute_all_key"),
            "prox_short":    ("Proximite 30m / 5m",    "proximity_short_key", "proximity_short_key"),
            "cycle_channel": ("Cycle canal radio",     "cycle_channel_key",   "cycle_channel_key"),
        }
        if kind not in kinds:
            return
        label, state_attr, cfg_key = kinds[kind]
        dlg = KeyCaptureDialog(self, label)
        if dlg.exec() == QDialog.Accepted:
            captured = dlg.captured  # peut etre "" (vide = aucune)
            # Canonicalisation defensive : KeyCaptureDialog renvoie deja
            # une combo canonique, mais on re-canonicalise au cas ou
            # (idempotent + protege contre les futures modifs du dialog).
            if captured:
                try:
                    captured = _core.canonicalize_hotkey(captured)
                except Exception:
                    pass
            new_key = captured if captured else None
            setattr(state, state_attr, new_key)
            # Persister dans le config client1
            try:
                core_cfg = _core._load_client_cfg()
                if new_key is None:
                    core_cfg.pop(cfg_key, None)
                else:
                    core_cfg[cfg_key] = new_key
                _core._save_client_cfg(core_cfg)
            except Exception as e:
                self._on_log(f"[CONFIG] Echec ecriture : {e}")
            self._refresh_radio_key_labels()
            self._on_log(f"[RADIO] {cfg_key} = {new_key!r}")

    @Slot(bool)
    def _on_rp_mode_toggled(self, checked: bool):
        """Bouton Mode RP : reproduit la logique de _toggle_rp_mode du
        client1 (ligne 8316). Si activation et Game.log introuvable :
        ouvrir popup pour saisir le chemin. Au switch ON : declencher un
        scan helmet rapide pour partir avec un etat correct."""
        if not _CORE_AVAILABLE:
            self.btn_rp_mode.setChecked(False)
            return

        if checked:
            # Activation : verifier Game.log
            try:
                gamelog = _core._find_gamelog()
            except Exception:
                gamelog = None
            if gamelog is None:
                # Demander le chemin a l'utilisateur
                dlg = GameLogPathDialog(self)
                if dlg.exec() != QDialog.Accepted or not dlg.validated_path:
                    # Annule : on revert le toggle
                    self.btn_rp_mode.setChecked(False)
                    self._refresh_rp_button()
                    return
                # Sauver le chemin dans le config client1
                try:
                    core_cfg = _core._load_client_cfg()
                    core_cfg["gamelog_path"] = dlg.validated_path
                    _core._save_client_cfg(core_cfg)
                    self._on_log(f"[GAMELOG] Chemin force = {dlg.validated_path}")
                except Exception as e:
                    self._on_log(f"[CONFIG] Echec ecriture gamelog_path : {e}")
            # Activer le mode RP + lancer un scan helmet rapide (5s) pour
            # detecter l'etat casque par boussole HUD.
            state.rp_mode = True
            try:
                _core._start_helmet_scan_quick(self._core_shim)
            except Exception as e:
                self._on_log(f"[HELMET] _start_helmet_scan_quick KO : {e}")
        else:
            state.rp_mode = False

        # Persister
        try:
            core_cfg = _core._load_client_cfg()
            core_cfg["rp_mode"] = state.rp_mode
            _core._save_client_cfg(core_cfg)
        except Exception:
            pass

        # Recalculer le filtrage RP (active/desactive le filtre radio sur
        # tous les senders concernes). _update_rp_filter est dans client1.
        try:
            _core._update_rp_filter()
        except Exception as e:
            self._on_log(f"[HELMET] _update_rp_filter KO : {e}")

        self._refresh_rp_button()
        self._on_log(f"[HELMET] Mode RP {'ACTIVE' if state.rp_mode else 'DESACTIVE'}")

    def _refresh_rp_button(self):
        """Met a jour le label/style du bouton Mode RP selon state.rp_mode."""
        if not hasattr(self, "btn_rp_mode"):
            return
        if state.rp_mode:
            self.btn_rp_mode.setText("Mode RP : ON")
            self.btn_rp_mode.setStyleSheet(
                "padding: 6px 12px; font-weight: bold; "
                "background: #2a4a2a; color: #88dd88;"
            )
            self.btn_rp_mode.setChecked(True)
        else:
            self.btn_rp_mode.setText("Mode RP : OFF")
            self.btn_rp_mode.setStyleSheet(
                "padding: 6px 12px; font-weight: bold;"
            )
            self.btn_rp_mode.setChecked(False)

    # ---- Slots overlays ----

    @Slot(bool)
    def _on_overlay_show_toggled(self, checked: bool):
        if not hasattr(self, "_overlay_manager"):
            return
        self._overlay_manager.show_mode = checked
        self._overlay_manager.refresh()
        # Bug fix : persister la valeur pour qu'elle soit restauree
        # au prochain demarrage (cf. bug 50).
        try:
            self._overlay_manager._persist()
        except Exception:
            pass
        self._refresh_overlay_buttons()

    @Slot(bool)
    def _on_overlay_edit_toggled(self, checked: bool):
        if not hasattr(self, "_overlay_manager"):
            return
        self._overlay_manager.edit_mode = checked
        self._overlay_manager.refresh()
        # Note : edit_mode n'est PAS persiste volontairement. Le mode
        # edition est temporaire (deplacer/redimensionner les overlays)
        # et on ne veut pas qu'un utilisateur qui ferme en mode edit
        # rouvre en mode edit.
        self._refresh_overlay_buttons()

    def _refresh_overlay_buttons(self):
        """Met a jour le style des 2 boutons overlay selon leur etat."""
        if not hasattr(self, "btn_overlay_show"):
            return
        # Bug fix : synchroniser le state Qt 'checked' du bouton avec
        # la valeur logique. Important au boot quand show_mode est
        # restaure depuis la config (sinon le bouton affiche "ON" mais
        # son etat Qt reste False, et le 1er clic ne basculera pas
        # comme attendu).
        self.btn_overlay_show.setChecked(self._overlay_manager.show_mode)
        if self._overlay_manager.show_mode:
            self.btn_overlay_show.setText("Overlay : ON")
            self.btn_overlay_show.setStyleSheet(
                "padding: 6px 12px; background: #2a4a2a; color: #88dd88;"
            )
        else:
            self.btn_overlay_show.setText("Overlay : OFF")
            self.btn_overlay_show.setStyleSheet("padding: 6px 12px;")
        if hasattr(self, "btn_overlay_edit"):
            self.btn_overlay_edit.setChecked(self._overlay_manager.edit_mode)
        if self._overlay_manager.edit_mode:
            self.btn_overlay_edit.setText("Edition : ON")
            self.btn_overlay_edit.setStyleSheet(
                "padding: 6px 12px; background: #2a3a4a; color: #88bbdd;"
            )
        else:
            self.btn_overlay_edit.setText("Edition : OFF")
            self.btn_overlay_edit.setStyleSheet("padding: 6px 12px;")

    @Slot(bool)
    def _on_helmet_state(self, helmet_on: bool):
        """Slot appele par le shim quand _gamelog_tail_loop ou
        _helmet_scan_loop detecte un CHANGEMENT d'etat casque.
        Ne fait rien : le client1 lui-meme ne reflete pas cet etat
        dans son UI (update_helmet_state est un placeholder).
        Le filtre audio est applique en interne par _update_rp_filter."""
        pass

    def _set_status_style(self, connected: bool, warning: bool = False):
        """Style le label de statut serveur (haut-gauche). Format compact :
        juste une couleur de texte sans fond ni bordure (tout est inline
        dans la barre du haut maintenant)."""
        if connected:
            color = THEME_GREEN
        elif warning:
            color = THEME_ORANGE
        else:
            color = THEME_RED
        self.lbl_status.setStyleSheet(
            f"color: {color}; font-weight: bold; "
            "padding: 2px 6px; font-size: 10pt;"
        )

    # ------------------------------------------------------------------
    # Panneau audio
    # ------------------------------------------------------------------
    def _build_audio_panel(self, parent_layout):
        """Cree le bloc UI : selection devices, gain micro, gate, mute, VU.

        En 2b, l'audio fonctionne en LOCAL UNIQUEMENT (capture + lecture
        des frames distantes pas encore branchee). Le VU-metre permet
        de valider que la capture passe avant de brancher le reseau."""
        box = QGroupBox("Audio")
        box.setStyleSheet("QGroupBox { font-weight: bold; padding-top: 14px; }")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        if not _AUDIO_AVAILABLE:
            err = QLabel(
                "Module audio indisponible : circusvoip_audio_io non importable.\n"
                "Verifier que le fichier est dans le dossier et que sounddevice + numpy sont installes."
            )
            err.setStyleSheet("color: #ff8888;")
            err.setWordWrap(True)
            v.addWidget(err)
            parent_layout.addWidget(box)
            return

        # Largeur fixe pour les labels (Micro / Sortie / Gain / Gate / VU)
        # afin que tous les controles s'alignent verticalement.
        _audio_lbl_w = 50

        # Style commun pour les boutons-picker (Micro / Sortie). Ils
        # remplacent les anciens QComboBox + bouton "Identifier" (doublon).
        # Click = ouvre une popup avec la liste + detection visuelle.
        # Note : on garde des QComboBox caches en interne comme SOURCE DE
        # VERITE pour la selection (compat avec _populate_audio_devices,
        # _on_audio_device_change, _start_or_restart_audio qui lisent
        # currentText() et currentData()). Les boutons-picker ne sont que
        # la facade visuelle ; ils reflètent l'etat des combos caches.
        _picker_btn_qss = (
            "QPushButton {"
            f" background: {THEME_BG_ROW};"
            f" color: {THEME_TEXT};"
            f" border: 1px solid {THEME_BORDER};"
            " border-radius: 3px;"
            " padding: 5px 10px;"
            " text-align: left;"
            " }"
            "QPushButton:hover {"
            f" border: 1px solid {THEME_MUTED};"
            " }"
        )

        # ComboBox caches : source de verite pour la selection
        self.cb_mic = QComboBox()
        self.cb_mic.setVisible(False)
        self.cb_out = QComboBox()
        self.cb_out.setVisible(False)
        # Quand le combo cache change (via _populate ou _on_mic_picked),
        # on met a jour le label du bouton-picker.
        self.cb_mic.currentTextChanged.connect(self._refresh_mic_pick_label)
        self.cb_out.currentTextChanged.connect(self._refresh_out_pick_label)

        # Ligne 1 : Micro (label + bouton-picker)
        h_mic = QHBoxLayout()
        h_mic.setSpacing(8)
        lbl_mic = QLabel("Micro :")
        lbl_mic.setMinimumWidth(_audio_lbl_w)
        h_mic.addWidget(lbl_mic)
        self.btn_mic_pick = QPushButton("(aucun)  ▾")
        self.btn_mic_pick.setStyleSheet(_picker_btn_qss)
        self.btn_mic_pick.setMinimumWidth(280)
        self.btn_mic_pick.setToolTip(
            "Click pour selectionner votre micro. La bordure verte indique "
            "le niveau capte par chaque micro - parlez et regardez lequel pulse."
        )
        self.btn_mic_pick.clicked.connect(self._on_mic_pick_clicked)
        h_mic.addWidget(self.btn_mic_pick, stretch=1)
        # Le combo cache est ajoute au layout pour que blockSignals/etc
        # marchent dans le contexte Qt habituel (parente).
        h_mic.addWidget(self.cb_mic)
        v.addLayout(h_mic)

        # Ligne 2 : Sortie (label + bouton-picker)
        h_out = QHBoxLayout()
        h_out.setSpacing(8)
        lbl_out = QLabel("Sortie :")
        lbl_out.setMinimumWidth(_audio_lbl_w)
        h_out.addWidget(lbl_out)
        self.btn_out_pick = QPushButton("(aucun)  ▾")
        self.btn_out_pick.setStyleSheet(_picker_btn_qss)
        self.btn_out_pick.setMinimumWidth(280)
        self.btn_out_pick.setToolTip(
            "Click pour selectionner votre sortie. Bouton ▶ Test sur chaque "
            "ligne pour ecouter 2 bips et identifier la bonne sortie."
        )
        self.btn_out_pick.clicked.connect(self._on_out_pick_clicked)
        h_out.addWidget(self.btn_out_pick, stretch=1)
        h_out.addWidget(self.cb_out)
        v.addLayout(h_out)

        # Note : pas de bouton "Rafraichir devices". Les devices audio
        # changent rarement en cours de session, et un changement detecte
        # par sounddevice ne met pas a jour la liste sans recharge propre
        # du panneau. Si besoin, redemarrer le client suffit.

        # Ligne 3 : Gain micro (slider 0-300 = 0.0-3.0)
        h_gain = QHBoxLayout()
        h_gain.setSpacing(8)
        lbl_gain = QLabel("Gain :")
        lbl_gain.setMinimumWidth(_audio_lbl_w)
        h_gain.addWidget(lbl_gain)
        self.sl_gain = QSlider(Qt.Horizontal)
        self.sl_gain.setRange(0, 300)
        self.sl_gain.setValue(int(self._cfg.get("mic_gain", 100)))
        self.sl_gain.valueChanged.connect(self._on_gain_changed)
        h_gain.addWidget(self.sl_gain, stretch=1)
        self.lbl_gain_val = QLabel(f"{self.sl_gain.value()}%")
        self.lbl_gain_val.setMinimumWidth(45)
        h_gain.addWidget(self.lbl_gain_val)
        v.addLayout(h_gain)

        # Ligne 4 : Gate threshold.
        # Le slider Qt ne gere que des entiers, donc on travaille en
        # demi-points : range 0..60, et la valeur reelle affichee a
        # l'utilisateur = slider/2.
        # Exemples :
        #   slider=0  -> affiche 0.0  -> envoye 0.000 a audio_io
        #   slider=1  -> affiche 0.5  -> envoye 0.005
        #   slider=6  -> affiche 3.0  -> envoye 0.030 (identique a l'ancien gate=3)
        #   slider=60 -> affiche 30.0 -> envoye 0.300 (identique a l'ancien gate=30)
        # Stockage config : la valeur brute du slider est sauvee sous
        # la cle 'gate_threshold_x2' pour eviter l'ambiguite avec
        # l'ancienne cle 'gate_threshold' (qui etait dans la plage 0..30).
        # Voir la logique de migration plus bas.
        h_gate = QHBoxLayout()
        h_gate.setSpacing(8)
        lbl_gate = QLabel("Gate :")
        lbl_gate.setMinimumWidth(_audio_lbl_w)
        h_gate.addWidget(lbl_gate)
        self.sl_gate = QSlider(Qt.Horizontal)
        self.sl_gate.setRange(0, 60)
        # Snap visuel sur les pas de 0.5 (= 1 cran de slider). Permet
        # aussi a l'utilisateur de cliquer dans la zone du slider et
        # d'aller au cran le plus proche, plutot que de tomber au pixel.
        self.sl_gate.setSingleStep(1)
        self.sl_gate.setPageStep(2)
        # Cle config : on utilise 'gate_threshold_x2' pour le nouveau
        # format (slider 0..60 = pas de 0.5). L'ancienne cle
        # 'gate_threshold' (entiers 0..30) est encore lue pour la
        # migration des anciens configs, mais plus jamais ecrite.
        # Au boot :
        #   1. Si gate_threshold_x2 present -> on l'utilise (nouveau format)
        #   2. Sinon, si gate_threshold present -> on le multiplie par 2
        #      (migration ancien format)
        #   3. Sinon -> defaut = 6 (= 3.0, identique a l'ancien defaut 3%)
        if "gate_threshold_x2" in self._cfg:
            try:
                saved_gate = int(float(self._cfg.get("gate_threshold_x2", 6)))
            except (TypeError, ValueError):
                saved_gate = 6
        else:
            try:
                old_gate = int(float(self._cfg.get("gate_threshold", 3)))
            except (TypeError, ValueError):
                old_gate = 3
            saved_gate = old_gate * 2
        # Clamp dans la nouvelle plage
        saved_gate = max(0, min(60, saved_gate))
        self.sl_gate.setValue(saved_gate)
        self.sl_gate.valueChanged.connect(self._on_gate_changed)
        h_gate.addWidget(self.sl_gate, stretch=1)
        # Affichage sans % : "3.0", "2.5", "0.5", etc. Format toujours
        # avec une decimale meme pour les valeurs entieres pour que la
        # largeur du label ne saute pas (3.0 -> 3.5 -> 4.0).
        gate_display = self.sl_gate.value() / 2.0
        self.lbl_gate_val = QLabel(f"{gate_display:.1f}")
        self.lbl_gate_val.setMinimumWidth(45)
        h_gate.addWidget(self.lbl_gate_val)
        v.addLayout(h_gate)

        # Ligne 5 : VU-metre
        # Note: le bouton MUTE MICRO n'est plus dans le panneau audio,
        # il est maintenant dans le panneau gauche de la page principale
        # avec MUTE proximité et MUTE radio (regroupement par fonction).
        # Le VU est custom (VUMeterWithGate) pour superposer un trait
        # blanc indiquant le seuil du gate : tout ce qui passe a gauche
        # du trait est coupe par le gate, tout ce qui depasse est transmis.
        h_vu = QHBoxLayout()
        h_vu.setSpacing(8)
        lbl_vu = QLabel("VU :")
        lbl_vu.setMinimumWidth(_audio_lbl_w)
        h_vu.addWidget(lbl_vu)
        self.vu = VUMeterWithGate()
        # Initialiser le trait du gate avec la valeur courante du slider.
        # Le slider est maintenant en 0..60 (= demi-points de la plage
        # 0.0..30.0), donc on divise par 60 pour obtenir une fraction
        # 0..1 puis on multiplie par 100 pour le VU (0..100).
        # Equivalent : value * 100 / 60, soit value*5/3 arrondi.
        self.vu.setGate(int(self.sl_gate.value() * 100 / 60))
        h_vu.addWidget(self.vu, stretch=1)
        v.addLayout(h_vu)

        # Ligne 6 : Checkbox suppression de bruit (RNNoise).
        # Si pyrnnoise n'est pas installe sur le client, la checkbox est
        # grisee avec un tooltip explicatif. Etat par defaut : True (la
        # version finale 0.1.0 fournira pyrnnoise via l'installateur).
        self.cb_noise_suppression = QCheckBox(
            "Suppression de bruit (RNNoise)"
        )
        # On ne peut pas encore demander la dispo a state.audio_io (pas
        # encore initialise au moment du build du panneau). On utilise
        # le flag global du module audio_io.
        try:
            from circusvoip_audio_io import (
                NOISE_SUPPRESSION_AVAILABLE,
                _NS_IMPORT_ERR,
            )
            ns_available = bool(NOISE_SUPPRESSION_AVAILABLE)
            ns_import_err = _NS_IMPORT_ERR
        except Exception as e:
            ns_available = False
            ns_import_err = str(e)
        ns_default = bool(self._cfg.get("noise_suppression_enabled", True))
        if ns_available:
            self.cb_noise_suppression.setChecked(ns_default)
            self.cb_noise_suppression.setToolTip(
                "Filtre les bruits de fond (clavier, ventilateur, "
                "souffle) pendant que vous parlez."
            )
        else:
            self.cb_noise_suppression.setChecked(False)
            self.cb_noise_suppression.setEnabled(False)
            err_detail = (
                f"\n\nDetail : {ns_import_err}" if ns_import_err else ""
            )
            self.cb_noise_suppression.setToolTip(
                "Module pyrnnoise non installe sur ce client.\n"
                "La version finale 0.1.0 l'inclura automatiquement."
                f"{err_detail}"
            )
            # Log l'erreur d'import dans la console pour debug : utile
            # pour diagnostiquer en dev (mauvaise version Python, DLL
            # manquante, etc.).
            try:
                self._on_log(
                    f"[AUDIO] pyrnnoise indisponible : {ns_import_err}"
                )
            except Exception:
                pass
        self.cb_noise_suppression.toggled.connect(
            self._on_noise_suppression_toggled
        )
        v.addWidget(self.cb_noise_suppression)

        parent_layout.addWidget(box)

    def _apply_vu_style(self, level_0_100: int):
        """Compat : ancienne fonction qui stylait le QProgressBar selon
        le niveau (vert/orange/rouge). VUMeterWithGate gere maintenant
        ses couleurs lui-meme dans paintEvent. Garde pour compat ascendante
        si du code externe l'appelle ; sinon no-op."""
        return

    def _populate_audio_devices(self):
        """Remplit les dropdowns micro/sortie et restaure la selection sauvee."""
        if not _AUDIO_AVAILABLE:
            return
        try:
            inputs = list_input_devices()   # list[(id, label)]
            outputs = list_output_devices()
        except Exception as e:
            self._on_log(f"[AUDIO] Erreur enumeration devices : {e}")
            return

        self.cb_mic.blockSignals(True)
        self.cb_out.blockSignals(True)
        self.cb_mic.clear()
        self.cb_out.clear()
        self.cb_mic.addItem("(aucun)", -1)
        self.cb_out.addItem("(aucun)", -1)
        for dev_id, label in inputs:
            self.cb_mic.addItem(label, dev_id)
        for dev_id, label in outputs:
            self.cb_out.addItem(label, dev_id)
        self.cb_mic.blockSignals(False)
        self.cb_out.blockSignals(False)

        # Restaurer la selection sauvee (par label)
        saved_mic = self._cfg.get("mic_label")
        saved_out = self._cfg.get("out_label")
        if saved_mic:
            idx = self.cb_mic.findText(saved_mic)
            if idx >= 0:
                self.cb_mic.setCurrentIndex(idx)
        else:
            # Defaut : device par defaut systeme
            matched = False
            try:
                default_id = default_input_device()
                if default_id is not None:
                    for i in range(self.cb_mic.count()):
                        if self.cb_mic.itemData(i) == default_id:
                            self.cb_mic.setCurrentIndex(i)
                            matched = True
                            break
                    if not matched and _CORE_AVAILABLE:
                        # Le device par defaut n'est pas dans notre
                        # enumeration : situation rare mais possible
                        # (filtrage WASAPI ou device exclusif). On
                        # log pour diagnostiquer plutot que de laisser
                        # le combo a "(aucun)" sans explication.
                        try:
                            _core._dbg_log(
                                f"[AUDIO] default_input_device={default_id} "
                                f"absent de la liste enumeree (mic)"
                            )
                        except Exception:
                            pass
                elif _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            "[AUDIO] default_input_device=None (mic)"
                        )
                    except Exception:
                        pass
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[AUDIO] default_input_device KO : {e}"
                        )
                    except Exception:
                        pass

            # Fallback : si le default Windows n'est pas trouvable (None
            # ou absent de l'enumeration), prendre le 1er micro valide
            # de la liste pour eviter que le client demarre sans micro
            # et bloque l'utilisateur. Cas typique : nouvelle install
            # sur un PC ou le casque par defaut Windows est eteint /
            # absent au moment du lancement, ou ou WASAPI filtre le
            # device par defaut.
            if not matched:
                for i in range(self.cb_mic.count()):
                    if self.cb_mic.itemData(i) is not None and self.cb_mic.itemData(i) >= 0:
                        self.cb_mic.setCurrentIndex(i)
                        if _CORE_AVAILABLE:
                            try:
                                _core._dbg_log(
                                    f"[AUDIO] fallback mic : {self.cb_mic.itemText(i)} "
                                    f"(id={self.cb_mic.itemData(i)})"
                                )
                            except Exception:
                                pass
                        break
        if saved_out:
            idx = self.cb_out.findText(saved_out)
            if idx >= 0:
                self.cb_out.setCurrentIndex(idx)
        else:
            matched = False
            try:
                default_id = default_output_device()
                if default_id is not None:
                    for i in range(self.cb_out.count()):
                        if self.cb_out.itemData(i) == default_id:
                            self.cb_out.setCurrentIndex(i)
                            matched = True
                            break
                    if not matched and _CORE_AVAILABLE:
                        try:
                            _core._dbg_log(
                                f"[AUDIO] default_output_device={default_id} "
                                f"absent de la liste enumeree (out)"
                            )
                        except Exception:
                            pass
                elif _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            "[AUDIO] default_output_device=None (out)"
                        )
                    except Exception:
                        pass
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[AUDIO] default_output_device KO : {e}"
                        )
                    except Exception:
                        pass

            # Fallback : meme principe que pour le micro. Si le default
            # Windows n'est pas trouvable, prendre la 1ere sortie valide.
            if not matched:
                for i in range(self.cb_out.count()):
                    if self.cb_out.itemData(i) is not None and self.cb_out.itemData(i) >= 0:
                        self.cb_out.setCurrentIndex(i)
                        if _CORE_AVAILABLE:
                            try:
                                _core._dbg_log(
                                    f"[AUDIO] fallback out : {self.cb_out.itemText(i)} "
                                    f"(id={self.cb_out.itemData(i)})"
                                )
                            except Exception:
                                pass
                        break

        # Connecter les signaux APRES restauration (eviter callbacks inutiles
        # pendant le populate). Au premier appel, currentIndexChanged n'est
        # pas encore connecte : disconnect() leve un RuntimeWarning. On
        # detecte ce cas via un flag plutot qu'avec try/except (le warning
        # est emis avant que l'exception n'arrive a l'except).
        # Le flag est initialise dans MainWindow.__init__.
        if self._audio_signals_connected:
            for cb in (self.cb_mic, self.cb_out):
                try:
                    cb.currentIndexChanged.disconnect(self._on_audio_device_change)
                except (TypeError, RuntimeError):
                    pass
        self.cb_mic.currentIndexChanged.connect(self._on_audio_device_change)
        self.cb_out.currentIndexChanged.connect(self._on_audio_device_change)
        self._audio_signals_connected = True

        # Une fois les devices peuples, demarrer la capture+lecture.
        # On le fait ICI (pas dans un singleShot separe) pour eviter une
        # course : si l'enumeration sounddevice est lente (>100ms), un
        # singleShot independant trouverait des dropdowns encore vides
        # et sortirait sans rien faire.
        self._start_or_restart_audio()

    def _refresh_audio_devices(self):
        """Bouton "Rafraichir" : re-enumere les devices (utile si le user a
        branche/debranche un casque pendant que le client tourne)."""
        self._on_log("[AUDIO] Rafraichissement liste devices...")
        self._populate_audio_devices()

    @Slot()
    def _on_audio_device_change(self):
        """Appele quand le user change micro ou sortie. Sauve dans le
        config et redemarre la capture/playback."""
        if not _AUDIO_AVAILABLE:
            return
        mic_label = self.cb_mic.currentText()
        out_label = self.cb_out.currentText()
        if mic_label == "(aucun)" or out_label == "(aucun)":
            return
        self._cfg["mic_label"] = mic_label
        self._cfg["out_label"] = out_label
        _save_cfg(self._cfg)
        self._start_or_restart_audio()

    def _refresh_mic_pick_label(self, *args):
        """Slot : mis a jour quand cb_mic.currentText() change. Reflete
        le nom du device courant + chevron sur le bouton-picker."""
        if hasattr(self, "btn_mic_pick"):
            txt = self.cb_mic.currentText() or "(aucun)"
            self.btn_mic_pick.setText(f"{txt}  ▾")

    def _refresh_out_pick_label(self, *args):
        """Slot : mis a jour quand cb_out.currentText() change."""
        if hasattr(self, "btn_out_pick"):
            txt = self.cb_out.currentText() or "(aucun)"
            self.btn_out_pick.setText(f"{txt}  ▾")

    @Slot()
    def _on_mic_pick_clicked(self):
        """Click sur le bouton-picker Micro : ouvre la popup listing avec
        bordure verte qui pulse selon le niveau capte. Permet de retrouver
        son micro quand il y a 20+ devices virtuels."""
        if not _AUDIO_AVAILABLE:
            return
        try:
            inputs = list_input_devices()
        except Exception as e:
            self._on_log(f"[MIC PICKER] Erreur enumeration : {e}")
            return
        if not inputs:
            QMessageBox.warning(
                self,
                "CircusVOIP",
                "Aucun micro detecte. Verifiez que sounddevice fonctionne "
                "et qu'au moins un peripherique d'entree est connecte."
            )
            return
        current_label = self.cb_mic.currentText()
        # IMPORTANT : la pipeline VOIP a deja le micro courant en exclusive
        # (selon driver). Le picker ouvrira un 2e stream sur ce device qui
        # peut echouer silencieusement (cf. log "non ouvert" dans MicPicker).
        dlg = MicPickerDialog(inputs, current_label, parent=self)
        # Position du popup : juste sous le bouton-picker
        try:
            pos = self.btn_mic_pick.mapToGlobal(
                QPoint(0, self.btn_mic_pick.height())
            )
            dlg.move(pos)
        except Exception:
            pass
        dlg.sig_mic_selected.connect(self._on_mic_picked_from_dialog)
        dlg.show()

    @Slot(int, str)
    def _on_mic_picked_from_dialog(self, dev_idx: int, label: str):
        """L'utilisateur a clique sur une ligne du picker mic. Selectionne
        ce device dans le combo cache, ce qui declenche
        _on_audio_device_change (sauve config + redemarre capture)."""
        idx = self.cb_mic.findText(label)
        if idx >= 0:
            self.cb_mic.setCurrentIndex(idx)
            self._on_log(f"[MIC PICKER] Micro selectionne : {label}")

    @Slot()
    def _on_out_pick_clicked(self):
        """Click sur le bouton-picker Sortie : ouvre la popup listing avec
        bouton ▶ Test sur chaque ligne pour identifier la bonne sortie."""
        if not _AUDIO_AVAILABLE:
            return
        try:
            outputs = list_output_devices()
        except Exception as e:
            self._on_log(f"[OUT PICKER] Erreur enumeration : {e}")
            return
        if not outputs:
            QMessageBox.warning(
                self,
                "CircusVOIP",
                "Aucune sortie audio detectee."
            )
            return
        current_label = self.cb_out.currentText()
        dlg = OutputPickerDialog(outputs, current_label, parent=self)
        try:
            pos = self.btn_out_pick.mapToGlobal(
                QPoint(0, self.btn_out_pick.height())
            )
            dlg.move(pos)
        except Exception:
            pass
        dlg.sig_out_selected.connect(self._on_out_picked_from_dialog)
        dlg.show()

    @Slot(int, str)
    def _on_out_picked_from_dialog(self, dev_idx: int, label: str):
        """L'utilisateur a clique sur une ligne du picker sortie."""
        idx = self.cb_out.findText(label)
        if idx >= 0:
            self.cb_out.setCurrentIndex(idx)
            self._on_log(f"[OUT PICKER] Sortie selectionnee : {label}")

    def _start_or_restart_audio(self):
        """(Re)demarre la capture micro et la lecture sortie selon les
        devices selectionnes dans les dropdowns. Cree state.audio_io
        au premier appel, applique les parametres mic_gain/gate sauves."""
        if not _AUDIO_AVAILABLE:
            return
        mic_label = self.cb_mic.currentText()
        out_label = self.cb_out.currentText()
        mic_id = self.cb_mic.currentData()
        out_id = self.cb_out.currentData()

        # Logs de diagnostic : utiles si rien ne demarre, on voit pourquoi
        self._on_log(f"[AUDIO] Selection : mic='{mic_label}' (id={mic_id}) "
                     f"out='{out_label}' (id={out_id})")

        if mic_id is None or mic_id < 0 or out_id is None or out_id < 0:
            self._on_log("[AUDIO] Selection invalide (aucun device choisi), "
                         "demarrage annule. Choisir un micro et une sortie.")
            return

        # Premier appel : creer l'instance AudioIO
        if state.audio_io is None:
            try:
                state.audio_io = AudioIO()
            except Exception as e:
                self._on_log(f"[AUDIO] AudioIO() KO : {e}")
                return
            # On installe un callback no-op pour que le pipeline capture
            # ne sorte PAS prematurement (cf. audio_io ligne 685 :
            # "if self._on_capture is None: return"). Sans callback, le
            # RMS n'est jamais mis a jour -> le VU-metre reste a 0.
            # En 2c on remplacera par un callback qui envoie sur le WS audio.
            state.audio_io.set_on_capture(self._audio_capture_noop)
            try:
                gain = self.sl_gain.value() / 100.0
                state.audio_io.set_mic_gain(gain)
                # Slider gate en 0..60 (demi-points), valeur reelle 0..30,
                # set_gate_threshold attend 0.0..1.0 -> divise par 200.
                # Cf. _on_gate_changed pour le rationale complet.
                gate = self.sl_gate.value() / 200.0
                state.audio_io.set_gate_threshold(gate)
                # Suppression de bruit : appliquer l'etat de la checkbox.
                # Si pyrnnoise n'est pas dispo, la checkbox est deja
                # forcee a False/disabled au build du panneau.
                if hasattr(self, "cb_noise_suppression"):
                    state.audio_io.set_noise_suppression(
                        self.cb_noise_suppression.isChecked()
                    )
            except Exception as e:
                self._on_log(f"[AUDIO] set_mic_gain/gate KO : {e}")

        try:
            ok_in = state.audio_io.start_capture(mic_id)
            ok_out = state.audio_io.start_playback(out_id)
        except Exception as e:
            self._on_log(f"[AUDIO] start KO : {e}")
            return

        state.audio_input_dev = mic_id
        state.audio_output_dev = out_id

        if ok_in and ok_out:
            self._on_log(f"[AUDIO] Capture + lecture OK "
                         f"(mic_id={mic_id}, out_id={out_id})")
            # Premier demarrage audio reussi : c'est le moment de lancer
            # les threads OCR/heartbeat qui ont besoin de audio_io existant
            # pour brancher set_on_capture.
            if not self._core_threads_started:
                self._start_boot_threads()
        else:
            self._on_log(f"[AUDIO] Demarrage partiel : "
                         f"in={ok_in} out={ok_out}")

    def _audio_capture_noop(self, frame_np):
        """Callback de capture par defaut en 2b : ne fait rien.
        Sa simple presence (au lieu de None) suffit a activer la mesure
        RMS dans audio_io, donc a faire vivre le VU-metre.
        En 2c, sera remplace par un callback qui envoie la frame sur le
        WebSocket audio si state.audio_connected est True."""
        return

    @Slot(int)
    def _on_gain_changed(self, value: int):
        self.lbl_gain_val.setText(f"{value}%")
        if state.audio_io is not None:
            try:
                state.audio_io.set_mic_gain(value / 100.0)
            except Exception:
                pass
        self._cfg["mic_gain"] = value
        # Pas de save immediat : evite l'I/O sur chaque move de slider.
        # La sauvegarde aura lieu au closeEvent.

    @Slot(int)
    def _on_gate_changed(self, value: int):
        # Le slider est en 0..60 = demi-points de la plage 0.0..30.0.
        # Valeur reelle affichee = value / 2 (ex: slider=5 -> 2.5).
        gate_display = value / 2.0
        self.lbl_gate_val.setText(f"{gate_display:.1f}")
        if state.audio_io is not None:
            try:
                # set_gate_threshold attend une fraction 0.0..1.0.
                # On divise par 200 (= 100*2) pour mapper le slider
                # entier 0..60 vers 0.000..0.300. Identique a l'ancien
                # comportement pour les valeurs entieres de l'ancienne
                # plage : slider=6 (= 3.0) -> 0.03, comme avant slider=3.
                state.audio_io.set_gate_threshold(value / 200.0)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[AUDIO] set_gate_threshold KO : {e}"
                        )
                    except Exception:
                        pass
        # Sauve dans la nouvelle cle 'gate_threshold_x2'. Cf.
        # _build_audio_panel pour le rationale (evite l'ambiguite de
        # l'ancienne plage 0..30 vs nouvelle 0..60).
        self._cfg["gate_threshold_x2"] = value
        # Mettre a jour le trait sur le VU. Le slider est en 0..60 mais
        # le VU est en 0..100, d'ou la mise a l'echelle. Voir le commentaire
        # dans _build_audio_panel pour le rationale (la position du trait
        # reste identique pour un meme reglage utilisateur, comparee a
        # l'ancienne plage 0..30).
        if hasattr(self, "vu") and isinstance(self.vu, VUMeterWithGate):
            self.vu.setGate(int(value * 100 / 60))

    @Slot(bool)
    def _on_noise_suppression_toggled(self, checked: bool):
        """Toggle suppression de bruit (RNNoise via pyrnnoise).
        Si pyrnnoise n'est pas installe, le set_noise_suppression sur
        audio_io sera silencieusement ignore (no-op)."""
        if state.audio_io is not None:
            try:
                state.audio_io.set_noise_suppression(checked)
            except Exception as e:
                self._on_log(
                    f"[AUDIO] set_noise_suppression KO : {e}"
                )
        self._cfg["noise_suppression_enabled"] = bool(checked)

    @Slot(bool)
    def _on_mute_toggled(self, checked: bool):
        state.audio_muted = checked
        if state.audio_io is not None:
            try:
                state.audio_io.set_capture_muted(checked)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[MUTE] set_capture_muted KO : {e}"
                        )
                    except Exception:
                        pass
        self._refresh_mute_button()

    @Slot(bool)
    def _on_mute_prox_toggled(self, checked: bool):
        """Bug fix 31 : slot dedie au CLIC du bouton MUTE PROXIMITE.
        Utilise checked plutot que d'inverser state.mute_proximity, ce
        qui evitait la desynchronisation entre etat Qt du bouton et
        etat global quand un hotkey pynput bascule entre temps."""
        state.mute_proximity = bool(checked)
        self._on_log(f"[MUTE] proximity = {state.mute_proximity}")
        self._refresh_mute_button()

    @Slot(bool)
    def _on_mute_radio_toggled(self, checked: bool):
        """Idem pour le bouton MUTE RADIO."""
        state.mute_radio = bool(checked)
        self._on_log(f"[MUTE] radio = {state.mute_radio}")
        self._refresh_mute_button()

    def _refresh_mute_button(self):
        """Synchronise l'apparence des boutons MUTE (mic, proximite, radio)
        avec l'etat global. Utilise quand un toggle vient d'un hotkey
        pynput, ou pour rafraichir au boot."""
        # MUTE MICRO
        if hasattr(self, "btn_mute"):
            muted = bool(getattr(state, "audio_muted", False))
            self.btn_mute.setChecked(muted)
            if muted:
                self.btn_mute.setStyleSheet(
                    "background: #aa3333; color: white; font-weight: bold;"
                )
            else:
                self.btn_mute.setStyleSheet("")
        # MUTE PROXIMITE
        if hasattr(self, "btn_mute_prox"):
            mp = bool(getattr(state, "mute_proximity", False))
            self.btn_mute_prox.setChecked(mp)
            if mp:
                self.btn_mute_prox.setStyleSheet(
                    "background: #aa3333; color: white; font-weight: bold;"
                )
            else:
                self.btn_mute_prox.setStyleSheet("")
        # MUTE RADIO
        if hasattr(self, "btn_mute_radio"):
            mr = bool(getattr(state, "mute_radio", False))
            self.btn_mute_radio.setChecked(mr)
            if mr:
                self.btn_mute_radio.setStyleSheet(
                    "background: #aa3333; color: white; font-weight: bold;"
                )
            else:
                self.btn_mute_radio.setStyleSheet("")

    @Slot()
    def _vu_tick(self):
        """Timer ~30Hz : lit le RMS courant et met a jour la barre VU."""
        if state.audio_io is None:
            return
        try:
            rms = state.audio_io.get_mic_rms()
        except Exception:
            return
        # Conversion RMS -> 0..100 avec une courbe perceptuelle.
        # RMS audio est lineaire, l'oreille est logarithmique. On utilise
        # une racine pour donner un VU plus parlant a faible niveau.
        # rms typiquement 0..0.3 en parole normale, ~0.5+ en cri.
        if rms <= 0:
            level = 0
        else:
            # sqrt + cap : 0.05 RMS -> ~22%, 0.10 -> 32%, 0.20 -> 45%,
            # 0.40 -> 63%, 0.70 -> 84%, 1.0 -> 100%
            level = int(math.sqrt(min(rms, 1.0)) * 100)
            level = max(0, min(100, level))
        self.vu.setValue(level)
        self._apply_vu_style(level)

    # ------------------------------------------------------------------
    # Worker reseau dans son thread
    # ------------------------------------------------------------------
    def _build_worker(self):
        self._worker_thread = QThread(self)
        self._worker = NetWorker()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.start()

        # Signaux worker -> UI : queued automatiquement (cross-thread)
        self._worker.sig_status.connect(self._on_status)
        self._worker.sig_player_joined.connect(self._on_player_joined)
        self._worker.sig_player_left.connect(self._on_player_left)
        self._worker.sig_player_pos.connect(self._on_player_pos)
        self._worker.sig_player_offline.connect(self._on_player_offline)
        self._worker.sig_players_reset.connect(self._on_players_reset)
        self._worker.sig_log.connect(self._on_log)
        self._worker.sig_invalid_token.connect(self._on_invalid_token)
        self._worker.sig_anonymous_mode.connect(self._on_anonymous_mode)
        self._worker.sig_channels_changed.connect(self._refresh_channels_combo)

        # Signal main -> worker (queued vers le thread worker)
        self._sig_start_connect.connect(self._worker.run_connect)
        # Signal hotkey (thread pynput -> thread Qt main)
        self._sig_hotkey.connect(self._on_hotkey_dispatch)
        # Signal updater (thread daemon -> main thread)
        self._sig_update_available.connect(self._on_update_available)
        self._sig_update_applied.connect(self._on_update_applied)
        self._sig_update_check_done.connect(self._on_update_check_done)

    # ------------------------------------------------------------------
    # Slots UI (main thread)
    # ------------------------------------------------------------------
    @Slot()
    def _on_toggle_connect(self):
        if state.connected:
            self._do_disconnect()
        else:
            self._do_connect()

    def _do_connect(self):
        name = self.ed_name.text().strip() or DEFAULT_NAME
        ip = self.ed_ip.text().strip() or DEFAULT_IP
        pw = self.ed_pw.text()

        # Bug fix 55 : validation simple IP et pseudo. Avant, si
        # l'utilisateur tapait "http://1.2.3.4:8888" ou "ws://...",
        # l'URL devenait "ws://http://1.2.3.4:8888:8888" et la
        # connexion echouait avec un message obscur. On nettoie ici.
        # On ne fait PAS de validation stricte (tester pourrait avoir
        # un nom de domaine custom ou un hostname local), juste un
        # nettoyage des prefixes scheme classiques.
        for scheme in ("http://", "https://", "ws://", "wss://"):
            if ip.lower().startswith(scheme):
                ip = ip[len(scheme):]
                break
        # Si l'IP contient un /path apres le host, on coupe.
        if "/" in ip:
            ip = ip.split("/", 1)[0]
        # Si l'utilisateur a explicitement mis un port (ex: "1.2.3.4:8888")
        # on le retire car SERVER_PORT est constant.
        if ip.count(":") == 1:
            ip = ip.split(":", 1)[0]
        ip = ip.strip()

        # Pseudo : caracteres autorises pour eviter les surprises
        # (le serveur valide aussi mais autant le faire cote client).
        # On accepte alphanum + - _ et espaces internes.
        if not re.match(r"^[A-Za-z0-9_\- ]+$", name):
            self.lbl_status.setText(
                "Pseudo invalide (alphanum, _, -, espace)"
            )
            self._set_status_style(False, warning=True)
            return
        # IP : doit avoir au moins un caractere apres nettoyage
        if not ip:
            self.lbl_status.setText("IP serveur vide")
            self._set_status_style(False, warning=True)
            return

        # Sauvegarder dans le config (le client1 sauve aussi le mdp)
        self._cfg["name"] = name
        self._cfg["server_ip"] = ip
        self._cfg["token"] = pw
        _save_cfg(self._cfg)

        self.lbl_status.setText("Connexion...")
        self._set_status_style(False, warning=True)
        self.btn_toggle.setEnabled(False)
        self.btn_toggle.setText("...")

        # Demarre le worker dans son thread (queued)
        self._sig_start_connect.emit(ip, name, pw)

    def _do_disconnect(self):
        self.lbl_status.setText("Deconnexion...")
        self._set_status_style(False, warning=True)
        self.btn_toggle.setEnabled(False)
        self._worker.request_stop()

    @Slot(bool, str)
    def _on_status(self, connected: bool, message: str):
        if connected:
            # On n'affiche pas l'IP dans le label public : si l'utilisateur
            # screenshote ou stream, l'IP serveur reste cachee.
            self.lbl_status.setText("Connecte")
            self._set_status_style(True)
            self.btn_toggle.setText("DECONNECTER")
            # Demarrer les threads OCR + WS audio + heartbeat
            self._start_core_threads_if_needed(message)
        else:
            txt = "Deconnecte"
            if message:
                txt = f"Deconnecte ({message})"
            self.lbl_status.setText(txt)
            self._set_status_style(False)
            self.btn_toggle.setText("CONNECTER")
            # Vider les cards joueurs
            for name in list(self._player_cards.keys()):
                card = self._player_cards.pop(name)
                self._players_layout.removeWidget(card)
                card.deleteLater()
            # Reset du statut audio : pas de connexion -> pas d'audio
            if hasattr(self, "lbl_audio_status"):
                self.lbl_audio_status.setText("Audio : —")
                self.lbl_audio_status.setStyleSheet(
                    f"color: {THEME_MUTED}; padding: 2px 6px; font-size: 10pt;"
                )
            # Couper l'envoi audio + forcer la fermeture du WebSocket
            # audio existant. Sans ca, le thread WS audio reste bloque
            # dans 'async for msg in ws' (la connexion peut sembler
            # active du cote client meme si le serveur l'a fermee), donc
            # a la prochaine connexion serveur principal, il ne se
            # reconnecte pas et ne re-emet pas set_audio_status(True)
            # -> le label "Audio : OK" ne revient jamais.
            #
            # On ferme via transport.close() qui est synchrone (pas
            # besoin du loop asyncio). Le 'async for msg in ws' va alors
            # lever ConnectionClosed et la boucle redemarre proprement.
            try:
                if getattr(state, "audio_ws", None) is not None:
                    try:
                        # ws.close_connection() est async, mais on peut
                        # taper directement sur le transport asyncio qui
                        # est synchrone.
                        transport = getattr(
                            state.audio_ws, "transport", None
                        )
                        if transport is not None:
                            transport.close()
                    except Exception:
                        pass
                state.audio_ws = None
                state.audio_connected = False
                state.audio_server_ip = None
            except Exception:
                pass
        self.btn_toggle.setEnabled(True)

    @Slot(str)
    def _on_player_joined(self, name: str):
        """Cree une nouvelle PlayerCard si pas deja presente."""
        if name in self._player_cards:
            return
        card = PlayerCard(name)
        card.sig_volume_clicked.connect(self._open_volume_popup)
        self._player_cards[name] = card
        # Insere avant le stretch final (qui pousse les cards en haut)
        self._players_layout.insertWidget(
            self._players_layout.count() - 1, card
        )
        # Met a jour les badges canal/profil depuis state
        self._refresh_player_card(name)
        # Appliquer immediatement le volume sauvegarde (s'il existe).
        self._apply_saved_volume(name)

    def _refresh_player_card(self, name: str):
        """Met a jour les badges Canal/Profil de la card du joueur.
        Appele quand canal/profil change."""
        card = self._player_cards.get(name)
        if card is None:
            return
        try:
            ch = state.player_channels.get(name)
            prof = state.player_profiles.get(name)
        except Exception:
            ch = None
            prof = None
        card.set_channel_profile(ch, prof)

    def _refresh_all_player_labels(self):
        """Rafraichit les badges Canal/Profil de toutes les cards.
        Appele quand on recoit un broadcast channels/profiles du serveur."""
        for name in list(self._player_cards.keys()):
            self._refresh_player_card(name)

    def _apply_saved_volume(self, name: str):
        """Lit cfg client1 ['player_volumes'][name] et applique au audio_io."""
        if not _CORE_AVAILABLE or state.audio_io is None:
            return
        try:
            core_cfg = _core._load_client_cfg()
            saved = int(core_cfg.get("player_volumes", {}).get(name, 100))
            state.audio_io.set_user_volume_multiplier(name, saved / 100.0)
        except Exception:
            pass

    def _open_volume_popup(self, name: str):
        """Mini popup avec slider 0-200% pour ajuster le volume du joueur."""
        if not _CORE_AVAILABLE:
            return
        dlg = VolumePopup(self, name)
        dlg.show()

    @Slot(str)
    def _on_player_left(self, name: str):
        card = self._player_cards.pop(name, None)
        if card is not None:
            self._players_layout.removeWidget(card)
            card.deleteLater()

    @Slot(str, dict, float)
    def _on_player_pos(self, name: str, pos: dict, dist: float):
        # Auto-create la card si le joueur arrive avec sa position avant
        # le _on_player_joined explicite (cas welcome avec pos initiale).
        card = self._player_cards.get(name)
        if card is None:
            self._on_player_joined(name)
            card = self._player_cards.get(name)
            if card is None:
                return

        # Mode anonyme actif : on n'affiche ni zone ni position ni distance.
        # On stocke quand meme dans state.players pour quand le mode sera
        # desactive (le refresh card relira state.players).
        if isinstance(pos, dict) and name in state.players:
            try:
                state.players[name]["pos"] = pos
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[POS] state.players[{name}].pos write KO : {e}"
                        )
                    except Exception:
                        pass
        if getattr(state, "anonymous_mode", False):
            card.set_anonymous(True)
            return
        card.set_anonymous(False)

        zone = pos.get("zone", "-") if isinstance(pos, dict) else "-"
        # Format axes avec unite par axe (m si <10km, km sinon), 2 decimales.
        # Cf _format_axes() qui simule l'affichage HUD SC : on respecte
        # l'unite naturelle de chaque axe (un X en km peut cohabiter avec
        # un Z en m sur une planete).
        if isinstance(pos, dict):
            pos_str = _format_axes(pos)
        else:
            pos_str = "-"

        # Distance : on RECALCULE a partir de state.my_pos pour avoir
        # toujours la valeur a jour. Le `dist` passe en parametre n'est
        # pas fiable : NetWorker emet dist=0 (il ne connait pas notre
        # position locale), seul le shim OCR fournit la vraie distance.
        # En recalculant ici, on est coherent peu importe la source.
        if state.my_pos is None or not isinstance(pos, dict):
            dist_str = "-"
            d_meters = None
        else:
            d_meters = None
            try:
                # Check container_id : si on est dans un container different
                # de l'autre joueur, distance = infinie (silence). Cf le check
                # equivalent cote core (ligne ~2448) qui controle le volume
                # audio. Ici c'est juste l'affichage UI, mais doit etre
                # coherent : sinon la card affiche "100m" alors que le user
                # n'entend rien (containers separes), ce qui est trompeur.
                # Bug observe le 07/05/2026 : tester A sortie d'ascenseur,
                # tester B reste dedans -> UI affichait ~100m alors que
                # containers differents.
                my_cid    = state.my_pos.get("container_id")
                their_cid = pos.get("container_id")
                if my_cid != their_cid:
                    d = float("inf")
                elif _CORE_AVAILABLE:
                    d = _sco.distance(state.my_pos, pos)
                else:
                    dx = pos.get("x", 0) - state.my_pos.get("x", 0)
                    dy = pos.get("y", 0) - state.my_pos.get("y", 0)
                    dz = pos.get("z", 0) - state.my_pos.get("z", 0)
                    d = math.sqrt(dx*dx + dy*dy + dz*dz)
                d_meters = d
                # Format adaptatif : m, km, Mkm. Si distance infinie
                # (containers differents), on affiche "hors de portee"
                # plutot qu'une valeur trompeuse.
                if d == float("inf"):
                    dist_str = "hors de portee"
                elif d < 1000:
                    dist_str = f"{d:.0f} m"
                elif d < 1_000_000:
                    dist_str = f"{d/1000:.1f} km"
                else:
                    dist_str = f"{d/1_000_000:.2f} Mkm"
                if name in state.players and isinstance(state.players[name], dict):
                    state.players[name]["dist"] = d
            except Exception as e:
                dist_str = "?"
                d_meters = None
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[POS] calcul distance pour {name} KO : {e}"
                        )
                    except Exception:
                        pass

        card.set_position(str(zone), pos_str, dist_str, dist_meters=d_meters)

    @Slot(str, bool)
    def _on_player_offline(self, name: str, offline: bool):
        card = self._player_cards.get(name)
        if card is None:
            return
        card.set_offline(offline)

    @Slot(list)
    def _on_players_reset(self, names: list):
        """Repeuple les cards apres un welcome."""
        # Vider les cards existantes
        for name in list(self._player_cards.keys()):
            card = self._player_cards.pop(name)
            self._players_layout.removeWidget(card)
            card.deleteLater()
        for name in names:
            self._on_player_joined(name)
            info = state.players.get(name, {})
            pos = info.get("pos")
            if pos:
                self._on_player_pos(name, pos, 0.0)

    @Slot(str)
    def _on_log(self, line: str):
        """Tous les logs du client2 vont dans le fichier debug du client1
        (circusvoip_debug_*.log) via _core._dbg_log. Ca evite d'avoir une
        mini-console UI a entretenir et regroupe tous les logs au meme
        endroit pour faciliter le debug.
        Si client1 indisponible, fallback sur stdout."""
        if _CORE_AVAILABLE:
            try:
                _core._dbg_log(line)
                return
            except Exception:
                pass
        # Fallback : print stdout
        print(line, flush=True)

    @Slot()
    def _on_invalid_token(self):
        QMessageBox.critical(
            self,
            "CircusVOIP",
            "Mot de passe invalide. Verifiez le mot de passe "
            "fourni par l'hebergeur du serveur.",
        )

    @Slot(bool)
    def _on_anonymous_mode(self, anon: bool):
        """Slot appele quand le serveur (broadcast ou welcome) annonce un
        changement du mode anonyme. Mode anonyme = decision admin serveur,
        le client ne fait que refleter l'etat. Quand actif :
          - Zone et Position des autres joueurs masquees dans la table
          - Position locale (lbl_my_pos) reste visible : seul le serveur
            filtre la diffusion aux autres. Localement, on doit toujours
            voir ou on est (debug, coherence UI, etat OCR).
        Pas de modification du titre fenetre.
        Le filtrage audio (volumes constants au lieu de varier) est fait
        cote client1 via state.anonymous_mode (deja mis a jour avant le
        signal). On ne s'occupe ici que du visuel.

        lbl_status est un statut de connexion compact (vert/rouge),
        independant du mode anonyme : pas de modification ici.
        """
        # Refresh de la position locale : meme si elle n'est plus masquee
        # par le mode anonyme, ce refresh garde l'affichage coherent en
        # cas de transition (ex : retour d'un ancien etat masque).
        try:
            self._refresh_local_pos_label()
        except Exception:
            pass

        # 2. Cards joueurs : masquer Zone et Position pour toutes les cards
        try:
            for name, card in self._player_cards.items():
                if anon:
                    card.set_anonymous(True)
                else:
                    # Restaurer depuis state.players via _on_player_pos
                    card.set_anonymous(False)
                    info = (state.players or {}).get(name) or {}
                    pos = info.get("pos") if isinstance(info, dict) else None
                    if isinstance(pos, dict):
                        # Re-trigger l'affichage complet (zone + pos + dist)
                        self._on_player_pos(name, pos, 0.0)
                    else:
                        card.set_position("-", "-", "-")
        except Exception:
            pass

        self._on_log(f"[ANON] Mode anonyme {'ACTIVE' if anon else 'desactive'}")

    # ---- Slot canal (combobox) ----

    @Slot()
    def _refresh_channels_combo(self):
        """Synchronise le combobox 'Canal' avec state.channels_list et
        state.my_channel, ET rafraichit le label 'Profil' (state.my_profile)
        ET les labels des joueurs (canal/profil) dans la table.
        Appele a chaque changement de canal/profil par le worker."""
        # 1. Combobox canal
        if hasattr(self, "cmb_channel"):
            self._channel_combo_updating = True
            try:
                self.cmb_channel.clear()
                self.cmb_channel.addItem("(aucun)")
                for ch in (state.channels_list or []):
                    self.cmb_channel.addItem(str(ch))
                cur = state.my_channel
                if cur and cur in (state.channels_list or []):
                    idx = self.cmb_channel.findText(cur)
                    if idx >= 0:
                        self.cmb_channel.setCurrentIndex(idx)
                else:
                    self.cmb_channel.setCurrentIndex(0)
            finally:
                self._channel_combo_updating = False
        # 2. Label "Mon profil" : violet si assigne, gris (aucun) sinon.
        # Le profil est mis dans state.my_profile par le worker quand il
        # recoit "my_profile" ou "channels" du serveur (cf. msg_type
        # handlers dans NetWorker).
        if hasattr(self, "lbl_my_profile"):
            try:
                prof = getattr(state, "my_profile", None)
                if prof:
                    self.lbl_my_profile.setText(str(prof))
                    # Violet (#bc8cff) comme dans le legacy
                    self.lbl_my_profile.setStyleSheet(
                        "color: #bc8cff; "
                        "font-weight: bold; padding: 4px 8px; "
                        f"background: {THEME_BG_ROW}; "
                        f"border: 1px solid {THEME_BORDER}; "
                        "border-radius: 3px;"
                    )
                else:
                    self.lbl_my_profile.setText("(aucun)")
                    self.lbl_my_profile.setStyleSheet(
                        f"color: {THEME_MUTED}; "
                        "font-weight: bold; padding: 4px 8px; "
                        f"background: {THEME_BG_ROW}; "
                        f"border: 1px solid {THEME_BORDER}; "
                        "border-radius: 3px;"
                    )
            except Exception:
                pass
        # 3. Labels joueurs (col 0)
        try:
            self._refresh_all_player_labels()
        except Exception:
            pass

    @Slot(str)
    def _on_channel_selected(self, text: str):
        """L'utilisateur a selectionne un canal dans la combobox.
        Envoie set_channel au serveur. Le serveur broadcast le changement,
        on recoit player_channel et on met a jour state.my_channel
        (qui peut differer de notre selection si le serveur refuse, par
        exemple si le canal n'existe plus)."""
        if getattr(self, "_channel_combo_updating", False):
            return  # mise a jour programmatique, on ignore
        if not _CORE_AVAILABLE:
            return
        if not getattr(state, "connected", False):
            self._on_log("[CHANNEL] Selection ignoree : pas connecte")
            return
        # "(aucun)" -> envoi None ; sinon le nom du canal
        new_ch = None if text == "(aucun)" else text
        try:
            ok = _core._ws_send_safe({"type": "set_channel", "channel": new_ch})
            if ok:
                self._on_log(f"[CHANNEL] set_channel -> {new_ch or '(aucun)'}")
            else:
                self._on_log("[CHANNEL] set_channel echoue (WS pas pret)")
        except Exception as e:
            self._on_log(f"[CHANNEL] set_channel KO : {e}")

    # ---- Slots utilises par le shim client1 ----

    @Slot(bool, str)
    def _on_audio_status(self, connected: bool, err: str):
        """Statut WS audio (port 8889). Affiche dans lbl_audio_status
        qui est un label dedie dans la barre du haut (a cote du statut
        serveur). Couleur vert si OK, rouge sinon, gris muted si neutre."""
        # Log defensif : permet de tracer les transitions audio dans le
        # log de debug. Si l'utilisateur signale "Audio : —" qui ne revient
        # pas a "OK" apres reconnexion, on saura si _on_audio_status est
        # appele ou pas.
        try:
            self._on_log(
                f"[AUDIO] _on_audio_status connected={connected} err={err!r}"
            )
        except Exception as e:
            # Pas de re-log via _on_log (recursion). On ecrit directement
            # via _dbg_log si dispo.
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[AUDIO] _on_audio_status log KO : {e}"
                    )
                except Exception:
                    pass
        if not hasattr(self, "lbl_audio_status"):
            return
        if connected:
            self.lbl_audio_status.setText("Audio : OK")
            self.lbl_audio_status.setStyleSheet(
                f"color: {THEME_GREEN}; padding: 2px 6px; font-size: 10pt;"
            )
        else:
            tag = err[:25] if err else "KO"
            self.lbl_audio_status.setText(f"Audio : {tag}")
            self.lbl_audio_status.setStyleSheet(
                f"color: {THEME_RED}; padding: 2px 6px; font-size: 10pt;"
            )

    @Slot(dict)
    def _on_my_pos_update(self, pos: dict):
        """OCR : nouvelle position locale du joueur. Mise a jour du
        timer principal qui recalcule les distances+volumes."""
        # Le state.my_pos est deja mis a jour par _ocr_loop_inner avant
        # qu'il appelle ui.update_my_pos. On rafraichit le label local
        # ET on recalcule toutes les distances de la table joueurs (sinon
        # elles ne bougent que quand l'autre joueur bouge, donnant des
        # distances obsoletes quand c'est nous qui bougeons).
        self._refresh_local_pos_label()
        self._refresh_all_distances()

    def _refresh_all_distances(self):
        """Recalcule la distance pour tous les joueurs (cards).
        Appele a chaque update de state.my_pos (OCR)."""
        if state.my_pos is None:
            return
        for name, info in (state.players or {}).items():
            if not isinstance(info, dict):
                continue
            pos = info.get("pos")
            if not isinstance(pos, dict):
                continue
            card = self._player_cards.get(name)
            if card is None:
                continue
            try:
                # Check container_id : si on est dans un container different
                # de l'autre joueur, distance = infinie (silence). Coherent
                # avec _on_player_pos et le check audio cote core.
                my_cid    = state.my_pos.get("container_id")
                their_cid = pos.get("container_id")
                if my_cid != their_cid:
                    d = float("inf")
                elif _CORE_AVAILABLE:
                    d = _sco.distance(state.my_pos, pos)
                else:
                    dx = pos.get("x", 0) - state.my_pos.get("x", 0)
                    dy = pos.get("y", 0) - state.my_pos.get("y", 0)
                    dz = pos.get("z", 0) - state.my_pos.get("z", 0)
                    d = math.sqrt(dx*dx + dy*dy + dz*dz)
                # Format adaptatif : m, km, Mkm
                if d == float("inf"):
                    dist_str = "hors de portee"
                elif d < 1000:
                    dist_str = f"{d:.0f} m"
                elif d < 1_000_000:
                    dist_str = f"{d/1000:.1f} km"
                else:
                    dist_str = f"{d/1_000_000:.2f} Mkm"
                # Recuperer la zone et position courante depuis state pour
                # ne pas perdre cette info en updatant juste la distance.
                zone = pos.get("zone", "-")
                # Format axes avec unite par axe (cf _format_axes).
                pos_str = _format_axes(pos)
                card.set_position(str(zone), pos_str, dist_str, dist_meters=d)
                state.players[name]["dist"] = d
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[POS] _refresh_all_distances {name} KO : {e}"
                        )
                    except Exception:
                        pass

    @Slot(float)
    def _on_min_dist_update(self, dist: float):
        """Distance au plus proche joueur (calcul fait dans _ocr_loop_inner).
        On ne l'affiche pas dans l'UI (decision : info pas pertinente),
        mais on garde le signal cable car le shim l'emet et certaines
        fonctions futures (overlays prox_range) pourraient l'utiliser."""
        pass

    @Slot(bool)
    def _on_sc_running(self, running: bool):
        """Etat du process SC. Mis a jour par le tail Game.log :
        - True quand on (re)ouvre Game.log (SC tourne, tail OK)
        - False quand on perd la cible (jeu ferme, crash, bascule LIVE/PTU
          en cours sans nouveau Game.log encore lisible).
        Quand False, on reset state.my_pos pour eviter qu'une vieille
        position fantome reste dans la VOIP positionnelle (les autres
        joueurs continueraient a recevoir notre derniere position OCR
        comme si on etait encore la), puis on rafraichit le label local
        qui passe en 'Hors-jeu' via _refresh_local_pos_label.
        """
        state.sc_running = bool(running)
        if not running:
            # Reset position pour ne pas spammer la position fantome aux
            # autres joueurs (la VOIP positionnelle utilise state.my_pos).
            state.my_pos = None
        # Rafraichir l'affichage local immediatement (sinon on attendrait
        # la prochaine position OCR qui n'arrivera pas si SC est ferme).
        try:
            self._refresh_local_pos_label()
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[POS] _on_sc_running refresh_local KO : {e}"
                    )
                except Exception:
                    pass

    def _refresh_local_pos_label(self):
        """Met a jour lbl_my_pos avec la position courante.

        Appele depuis _on_my_pos_update (apres OCR) et _on_anonymous_mode
        (changement d'etat anonyme). Format : 2 lignes
            <ContainerName>
            X:... Y:... Z:... (m | km | Mkm selon echelle)
        Le mode anonyme ne masque PAS la position locale : seule la
        diffusion reseau aux autres joueurs est filtree (cote serveur).
        Localement, l'utilisateur doit toujours voir ou il se trouve
        (utile pour debug, coherence UI, et savoir si l'OCR fonctionne).
        Si SC est ferme/perdu, state.my_pos est reset a None par
        _on_sc_running -> on retombe naturellement sur la branche
        'En attente de position OCR...' (gris), pas de message dedie.
        """
        try:
            pos = state.my_pos
            if not isinstance(pos, dict) or pos.get("x") is None:
                self.lbl_my_pos.setText("En attente de position OCR...")
                self.lbl_my_pos.setStyleSheet(
                    "background:#161b22; color:#6e7681; padding:6px; "
                    "border-radius:4px; "
                    "font-family: 'Consolas', 'Courier New', monospace;"
                )
                return
            self.lbl_my_pos.setText(_format_my_pos(pos))
            self.lbl_my_pos.setStyleSheet(
                "background:#161b22; color:#c9d1d9; padding:6px; "
                "border-radius:4px; "
                "font-family: 'Consolas', 'Courier New', monospace;"
            )
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[POS] _refresh_local_pos_label KO : {e}"
                    )
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Init zone OCR
    # ------------------------------------------------------------------
    def _init_zone_ocr(self):
        """Initialise state.zone_coords pour l'OCR. Lit la zone calibree
        sauvegardee dans la config (circusvoip_client_config.json) en
        LECTURE SEULE pour ne pas casser ses donnees.
        Si pas de zone sauvee, calcule une zone auto via auto_ocr_zone()."""
        if not _SCO_AVAILABLE:
            return
        try:
            try:
                mons = _sco.list_monitors()
            except Exception as e:
                self._on_log(f"[OCR] list_monitors KO : {e}")
                mons = []

            # 1. Tenter de lire la zone depuis le config (via core qui
            # utilise le meme fichier circusvoip_client_config.json)
            saved_zone = None
            if _CORE_AVAILABLE:
                try:
                    core_cfg = _core._load_client_cfg()
                    saved_zone = core_cfg.get("zone_coords")
                except Exception:
                    saved_zone = None
            if saved_zone and isinstance(saved_zone, dict):
                z = saved_zone
                ok = False
                for mon in mons:
                    m_right = mon["left"] + mon["width"]
                    m_bottom = mon["top"] + mon["height"]
                    if (mon["left"] <= z.get("left", 0) and
                        z.get("left", 0) + z.get("width", 0) <= m_right and
                        mon["top"] <= z.get("top", 0) and
                        z.get("top", 0) + z.get("height", 0) <= m_bottom):
                        ok = True
                        break
                if ok:
                    state.zone_coords = saved_zone
                    return
                else:
                    self._on_log("[OCR] Zone sauvee hors des ecrans connus, "
                                 "fallback vers auto_ocr_zone")

            # 2. Sinon : zone auto calculee depuis la resolution
            state.zone_coords = _sco.auto_ocr_zone()
            z = state.zone_coords
            self._on_log(f"[OCR] Zone auto : "
                         f"{z.get('width')}x{z.get('height')} "
                         f"a ({z.get('left')},{z.get('top')})")
        except Exception as e:
            self._on_log(f"[OCR] Init zone KO : {e}")

    def _start_core_threads_if_needed(self, server_ip: str):
        """Au moment de la connexion : (re)demarre le WS audio.
        L'OCR et le heartbeat tournent deja depuis le boot,
        on ne les retouche pas.

        On redemarre TOUJOURS un nouveau thread audio plutot que de
        compter sur la reconnexion auto de l'ancien. Raison : le thread
        audio peut etre bloque dans 'async for msg in ws' meme apres
        que le serveur a ferme sa side, et nos tentatives de fermeture
        forcee (transport.close()) ne reveillent pas le 'async for' de
        maniere fiable. Avec un nouveau thread + nouveau loop asyncio,
        on a une connexion neuve garantie.

        L'ancien thread (s'il existe) finira par sortir tout seul
        (audio_server_ip=None pendant la transition, puis remis a la
        nouvelle IP) ou mourra avec le process. Pas de leak observable
        car ce sont des daemon threads."""
        if not _CORE_AVAILABLE or not self._core_shim:
            self._on_log("[AUDIO] client1 non importable, WS audio desactive")
            return

        # Forcer la fermeture du ws existant si il y en a un. Le transport
        # close() peut ne pas reveiller le 'async for' tout de suite, mais
        # ca n'est plus grave puisqu'on demarre un nouveau thread quoi
        # qu'il arrive (l'ancien finira son loop tout seul).
        try:
            if getattr(state, "audio_ws", None) is not None:
                try:
                    transport = getattr(state.audio_ws, "transport", None)
                    if transport is not None:
                        transport.close()
                except Exception:
                    pass
                state.audio_ws = None
        except Exception:
            pass

        # Set l'IP audio AVANT que le nouveau thread ne s'en serve
        state.audio_server_ip = server_ip
        state.audio_connected = False  # sera mis a True par le nouveau thread

        # Si un ancien thread tourne encore, on le laisse mourir tout seul.
        # On ne peut pas killer un thread Python proprement, mais l'ancien
        # va voir audio_server_ip change (potentiellement reconnect a la
        # meme IP : pas grave, le serveur fermera l'ancienne session) ou
        # rester en sleep et mourir avec le process.
        if self._audio_ws_thread is not None and self._audio_ws_thread.is_alive():
            self._on_log(
                "[AUDIO] Ancien thread WS audio detecte, on en demarre "
                "un nouveau (l'ancien va mourir naturellement)"
            )

        # Demarrer un nouveau thread WS audio (port 8889)
        self._audio_ws_thread = threading.Thread(
            target=_core._run_audio_ws,
            args=(self._core_shim,),
            daemon=True,
            name="c2-audio-ws",
        )
        self._audio_ws_thread.start()
        self._on_log(f"[AUDIO] Thread WS audio demarre (serveur {server_ip}:8889)")

    def _start_boot_threads(self):
        """Au boot : demarre OCR + heartbeat + helmet + radio listener.
        Independant de la connexion serveur (l'OCR sert meme hors-ligne
        pour connaitre sa position, et le heartbeat boucle a vide si pas
        connecte). Reproduit le comportement du client1 (cf.
        ClientUI.__init__ ligne 6794+)."""
        if not _CORE_AVAILABLE or not self._core_shim:
            return
        if self._core_threads_started:
            return

        # Brancher le callback de capture sur _on_audio_captured du client1.
        # Cette fonction depose les frames dans _audio_send_queue, qui sera
        # consommee par _audio_sender (lance dans _audio_ws_loop) UNIQUEMENT
        # si state.audio_connected=True. Donc en l'absence de connexion,
        # _on_audio_captured tourne mais ne fait rien d'observable.
        # Avantage : pas de switch de callback a la connexion, le pipeline
        # capture/RMS reste continu (le VU continue de marcher hors-ligne).
        if state.audio_io is not None:
            try:
                state.audio_io.set_on_capture(_core._on_audio_captured)
                self._on_log("[AUDIO] Callback capture branche sur _core._on_audio_captured")
            except Exception as e:
                self._on_log(f"[AUDIO] set_on_capture KO : {e}")

        # Charger les touches PTT et le mode RP depuis la
        # config client1 dans state, puis demarrer les listeners pynput.
        try:
            core_cfg = _core._load_client_cfg()
            # Helper local : canonicaliser une combo lue depuis la config.
            # Garantit que le matching runtime fonctionne meme si la config
            # a ete editee a la main (ex: 'M+CTRL' devient 'ctrl+m'). Pour
            # les anciens raccourcis simple-touche ('m', 'mouse:x1'), la
            # canonicalisation est l'identite -> retro-compat totale.
            def _canon(k):
                if not k:
                    return k
                try:
                    return _core.canonicalize_hotkey(k)
                except Exception:
                    return k  # fallback : laisser la valeur brute
            state.radio_key            = _canon(core_cfg.get("radio_key"))
            state.profile_radio_key    = _canon(core_cfg.get("profile_radio_key"))
            state.mute_mic_key         = _canon(core_cfg.get("mute_mic_key"))
            state.mute_prox_key        = _canon(core_cfg.get("mute_prox_key"))
            state.mute_radio_key       = _canon(core_cfg.get("mute_radio_key"))
            state.mute_all_key         = _canon(core_cfg.get("mute_all_key"))
            state.proximity_short_key  = _canon(core_cfg.get("proximity_short_key"))
            state.cycle_channel_key    = _canon(core_cfg.get("cycle_channel_key"))
            state.rp_mode              = bool(core_cfg.get("rp_mode", False))
            self._on_log(
                f"[CONFIG] Chargee : radio_key={state.radio_key!r} "
                f"profile_key={state.profile_radio_key!r} "
                f"rp_mode={state.rp_mode}"
            )
        except Exception as e:
            self._on_log(f"[CONFIG] Erreur chargement : {e}")

        # Brancher les callbacks de toggle (mute mic/prox/radio/all,
        # cycle canal, prox short, profile radio PTT).
        try:
            _core._radio_listener.set_toggle_callbacks(
                on_mic           = self._on_hotkey_mute_mic,
                on_prox          = self._on_hotkey_mute_prox,
                on_radio         = self._on_hotkey_mute_radio,
                on_all           = self._on_hotkey_mute_all,
                on_prox_short    = self._on_hotkey_prox_short,
                on_cycle_channel = self._on_hotkey_cycle_channel,
                on_profile_radio_pressed  = self._on_hotkey_profile_pressed,
                on_profile_radio_released = self._on_hotkey_profile_released,
            )
            self._on_log("[RADIO] set_toggle_callbacks OK")
        except Exception as e:
            self._on_log(f"[RADIO] set_toggle_callbacks KO : {e}")

        # Demarrer le RadioKeyListener du client1 (gere PTT + flags audio).
        try:
            _core._radio_listener.start()
            self._on_log("[RADIO] RadioKeyListener demarre (PTT + toggles actifs)")
        except Exception as e:
            self._on_log(f"[RADIO] RadioKeyListener.start() KO : {e}")

        # Thread OCR (lit zone HUD, met a jour state.my_pos, calcule
        # distances et appelle audio_io.set_user_volume sur les autres
        # joueurs connus).
        # On utilise _ocr_loop (avec try/except) au lieu de _ocr_loop_inner
        # direct : sinon une exception Python remonte et tue le thread sans
        # log. _ocr_loop wrappe et logge la stack via _dbg_log.
        def _spawn_ocr_thread():
            """Spawn (ou re-spawn) le thread OCR. Appele au demarrage,
            et appele aussi par le watchdog si l'OCR freeze plus de 30s."""
            t = threading.Thread(
                target=_core._ocr_loop,
                args=(self._core_shim,),
                daemon=True,
                name="c2-ocr",
            )
            t.start()
            self._ocr_thread = t
        _spawn_ocr_thread()

        # Thread watchdog OCR : detecte les freezes silencieux de la boucle
        # OCR (segfault torch/CUDA, deadlock GPU, etc.). Si l'OCR ne tick
        # plus depuis 15s, log un warning. Si plus de 30s, demande au
        # client de respawner le thread OCR via le callback.
        # Quand l'OCR repart, declenche aussi un redemarrage des streams
        # audio (les freezes CUDA bloquent aussi les callbacks sounddevice).
        self._ocr_watchdog_thread = threading.Thread(
            target=_core._ocr_watchdog_loop,
            args=(self._core_shim,),
            kwargs={"restart_callback": _spawn_ocr_thread},
            daemon=True,
            name="c2-ocr-watchdog",
        )
        self._ocr_watchdog_thread.start()

        # Thread volume safety : tourne toutes les secondes et force volume=0
        # pour les joueurs sc_offline / sans position / position perimee.
        # Restauration du legacy (oublie au split). Couvre notamment le cas
        # freeze OCR chez un autre joueur : sans cette safety, il restait
        # audible avec sa derniere position connue. Cf POS_STALE_TIMEOUT.
        self._volume_safety_thread = threading.Thread(
            target=_core._volume_safety_loop,
            args=(self._core_shim,),
            daemon=True,
            name="c2-volume-safety",
        )
        self._volume_safety_thread.start()

        # Thread heartbeat (boucle vide tant que state.ws is None)
        self._heartbeat_thread = threading.Thread(
            target=_core._heartbeat_loop,
            args=(self._core_shim,),
            daemon=True,
            name="c2-heartbeat",
        )
        self._heartbeat_thread.start()

        # Threads helmet (Game.log tail + scan boussole).
        # Ils tournent en idle si state.rp_mode=False ; pas d'impact CPU.
        #
        # Note : on N'utilise PAS _core._gamelog_tail_loop directement, parce
        # qu'il choisit le Game.log au demarrage et n'en change plus, meme
        # si l'utilisateur lance ensuite SC sur une autre version (LIVE vs
        # PTU vs EPTU). On le remplace par notre propre tail loop qui suit
        # psutil dynamiquement (cf. _gamelog_tail_loop_smart).
        try:
            self._gamelog_thread = threading.Thread(
                target=self._gamelog_tail_loop_smart,
                daemon=True,
                name="c2-gamelog-tail-smart",
            )
            self._gamelog_thread.start()
            self._helmet_scan_thread = threading.Thread(
                target=_core._helmet_scan_loop,
                args=(self._core_shim,),
                daemon=True,
                name="c2-helmet-scan",
            )
            self._helmet_scan_thread.start()
            self._on_log("[HELMET] Threads (gamelog tail smart + scan) demarres")
        except Exception as e:
            self._on_log(f"[HELMET] Erreur lancement threads : {e}")

        # Refresh UI a partir de l'etat charge depuis la config
        try:
            self._refresh_radio_key_labels()
            self._refresh_rp_button()
        except Exception:
            pass

        self._core_threads_started = True
        self._on_log("[OCR] Threads demarres (au boot, "
                     "avant connexion)")

    # ------------------------------------------------------------------
    # Tail Game.log "smart"
    # ------------------------------------------------------------------
    # Equivalent du _gamelog_tail_loop du client1 mais qui suit psutil en
    # continu pour detecter un changement de version SC active. Si
    # l'utilisateur lance LIVE puis ferme SC et lance PTU, on bascule
    # automatiquement de Game.log sans redemarrer le client.
    #
    # Logique :
    #   1. Toutes les 3s, regarder quel StarCitizen.exe tourne (s'il y en a)
    #   2. En deduire le Game.log a tailer
    #   3. Si different du fichier qu'on tail actuellement -> bascule
    #   4. Le chemin force par l'utilisateur (cfg["gamelog_path"]) garde
    #      la priorite sur la detection psutil
    #   5. Si rien ne tourne et pas de chemin force, fallback sur
    #      _core._find_gamelog() (niveau 3 = chemins habituels)
    #
    # Le thread garde son tail file ouvert tant que possible, ne ferme/rouvre
    # que si la cible change. Chaque ligne est passee a _core._process_gamelog_line
    # qui gere les events helmet (regex + state.helmet_on + WS broadcast).

    def _gamelog_tail_loop_smart(self):
        """Wrapper qui catche les exceptions du thread pour qu'elles soient
        visibles dans le log au lieu de tuer silencieusement le thread."""
        try:
            self._gamelog_tail_loop_smart_impl()
        except Exception as e:
            import traceback
            self._on_log(f"[GAMELOG SMART] CRASH thread : {e}")
            for line in traceback.format_exc().rstrip().split("\n"):
                self._on_log(f"  {line}")

    def _gamelog_tail_loop_smart_impl(self):
        # os et time sont importes en haut du fichier.
        f = None
        cur_path: Optional[str] = None
        last_psutil_check = 0.0
        psutil_interval = 3.0  # secondes entre 2 checks psutil

        def _close_file():
            nonlocal f
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
                f = None

        def _open_file(path: str):
            nonlocal f, cur_path
            try:
                f_new = open(path, "r", encoding="utf-8", errors="ignore")
                f_new.seek(0, 2)  # fin de fichier (pas d'historique)
                _close_file()
                f = f_new
                cur_path = path
                self._on_log(f"[GAMELOG] Tail demarre sur : {path}")
                return True
            except Exception as e:
                self._on_log(f"[GAMELOG] Echec ouverture {path} : {e}")
                return False

        def _resolve_target() -> Optional[str]:
            """Determine quel Game.log on doit tailer en ce moment.
            Priorites :
              1. cfg["gamelog_path"] (force par l'utilisateur)
              2. StarCitizen.exe actif via psutil (suit la version qui
                 tourne reellement)

            Si SC ne tourne PAS et qu'aucun chemin n'est force : retourne
            None. On attend que SC demarre pour tailer le bon fichier,
            plutot que de tomber sur LIVE/Game.log par defaut alors que
            l'utilisateur joue peut-etre sur PTU.
            """
            # 1. Chemin force ?
            try:
                core_cfg = _core._load_client_cfg()
                forced = core_cfg.get("gamelog_path")
                if forced:
                    # Peut etre soit un dossier (LIVE/PTU/...) soit
                    # directement un chemin Game.log.
                    if os.path.isdir(forced):
                        candidate = os.path.join(forced, "Game.log")
                    else:
                        candidate = forced
                    if os.path.exists(candidate):
                        return candidate
            except Exception:
                pass

            # 2. Process SC actif ?
            seen_problems: list[str] = []  # cas vus mais inutilisables
            try:
                import psutil
                # Path est deja importe en haut du fichier
                for proc in psutil.process_iter(["name", "exe", "pid"]):
                    try:
                        name_raw = proc.info.get("name") or ""
                        name = name_raw.lower()
                        if "starcitizen" in name and name.endswith(".exe"):
                            exe = proc.info.get("exe")
                            if not exe:
                                seen_problems.append(
                                    f"{name_raw} (pid={proc.info.get('pid')}) "
                                    f"sans exe lisible (admin/EAC ?)"
                                )
                                continue
                            # .../<VERSION>/Bin64/StarCitizen.exe
                            game_log = Path(exe).parent.parent / "Game.log"
                            if game_log.exists():
                                self._psutil_warned = False
                                return str(game_log)
                            else:
                                seen_problems.append(
                                    f"{name_raw} : Game.log absent ({game_log})"
                                )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except ImportError:
                # psutil pas installe : on ne fait rien, fallback sera None
                if not getattr(self, "_psutil_warned_missing", False):
                    self._on_log("[GAMELOG] psutil non installe, "
                                 "detection auto SC desactivee. "
                                 "pip install psutil")
                    self._psutil_warned_missing = True
            except Exception:
                pass

            # Vu mais inutilisable : on logue une fois pour aider au diag
            if seen_problems and not getattr(self, "_psutil_warned", False):
                for p in seen_problems:
                    self._on_log(f"[GAMELOG] Process SC vu mais inutilisable : {p}")
                self._psutil_warned = True

            # SC pas en cours et pas de chemin force : on ne tail rien.
            return None

        while True:
            now = time.time()
            # Re-evaluer la cible toutes les psutil_interval secondes
            if now - last_psutil_check > psutil_interval:
                last_psutil_check = now
                target = _resolve_target()
                if target != cur_path:
                    if target is None:
                        # Plus rien : fermer le fichier
                        if cur_path is not None:
                            self._on_log("[GAMELOG] Cible perdue, fermeture")
                            _close_file()
                            cur_path = None
                            # Notifier l'UI : SC ferme/perdu -> afficher "Hors-jeu"
                            # a la place de la position locale (qui sinon resterait
                            # figee sur la derniere valeur OCR connue).
                            try:
                                if self._core_shim:
                                    self._core_shim.sig_sc_running.emit(False)
                            except Exception:
                                pass
                    else:
                        # Bascule de fichier
                        if cur_path is not None:
                            self._on_log(f"[GAMELOG] Bascule : {cur_path} -> {target}")
                        _open_file(target)
                        # Cible retrouvee (ouverture initiale OU bascule
                        # LIVE/PTU/EPTU) -> repasser l'UI en mode normal.
                        # La 1ere position OCR a venir remplacera le placeholder
                        # "En attente de position OCR..." par la vraie position.
                        try:
                            if self._core_shim:
                                self._core_shim.sig_sc_running.emit(True)
                        except Exception:
                            pass

            # Lire les nouvelles lignes du fichier ouvert
            if f is None:
                time.sleep(1.0)
                continue
            try:
                line = f.readline()
            except Exception as e:
                self._on_log(f"[GAMELOG] Erreur readline : {e}")
                _close_file()
                cur_path = None
                time.sleep(2.0)
                continue
            if not line:
                # Pas de nouvelle ligne : check rotation/troncation
                try:
                    cur_size = os.path.getsize(cur_path) if cur_path else 0
                    pos = f.tell()
                    if cur_size < pos:
                        # Fichier tronque (SC a redemarre) : rouvrir
                        self._on_log("[GAMELOG] Fichier tronque, reprise")
                        try:
                            state.helmet_on = True  # reset etat par defaut
                            _core._helmet_scan.active = False
                        except Exception:
                            pass
                        if cur_path:
                            _open_file(cur_path)
                except OSError:
                    # Fichier supprime
                    self._on_log("[GAMELOG] Fichier supprime")
                    _close_file()
                    cur_path = None
                time.sleep(0.2)
                continue

            # Parser la ligne (delegue au client1)
            try:
                _core._process_gamelog_line(line, self._core_shim)
            except Exception as e:
                self._on_log(f"[GAMELOG] _process_gamelog_line KO : {e}")

    # _row_for() supprime : remplace par self._player_cards[name].
    # Ancienne implementation parcourait QTableWidget pour matcher le
    # nom de base ; maintenant le dict donne O(1).

    # ------------------------------------------------------------------
    # Geometrie (recopie phase 1)
    # ------------------------------------------------------------------
    def _apply_initial_geometry(self):
        saved = self._cfg.get("window_geometry")
        user_set = bool(self._cfg.get("window_geometry_user_set", False))

        if saved and user_set and isinstance(saved, dict):
            try:
                x = int(saved["x"])
                y = int(saved["y"])
                w = int(saved["w"])
                h = int(saved["h"])
                cx, cy = x + w // 2, y + h // 2
                # QPoint est deja importe en haut du fichier (ligne 202)
                screen = QGuiApplication.screenAt(QPoint(cx, cy))
                if screen is not None:
                    self.setGeometry(x, y, w, h)
                    print(f"[WINDOW] Geometry restauree (user_set) : "
                          f"{w}x{h} a ({x},{y}) sur '{screen.name()}'")
                    return
                else:
                    print("[WINDOW] Geometry sauvee hors ecran connu, defaut")
            except Exception as e:
                print(f"[WINDOW] Geometry sauvee invalide ({e}), defaut")

        cursor_pos = QCursor.pos()
        target = QGuiApplication.screenAt(cursor_pos)
        if target is None:
            target = QGuiApplication.primaryScreen()

        avail = target.availableGeometry()
        win_w, win_h = _compute_default_size(avail.width(), avail.height())
        pos_x = avail.x() + (avail.width() - win_w) // 2
        pos_y = avail.y() + max(10, (avail.height() - win_h) // 3)

        self.setGeometry(pos_x, pos_y, win_w, win_h)
        print(f"[WINDOW] Geometry par defaut : {win_w}x{win_h} a "
              f"({pos_x},{pos_y}) sur '{target.name()}' "
              f"(DPR={target.devicePixelRatio()})")

    # ------------------------------------------------------------------
    # Hooks Qt (gestion DPI / geometry / fermeture)
    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        h = self.windowHandle()
        if h is not None:
            self._current_screen = h.screen()
            # Bug fix : avant, on connectait screenChanged a chaque
            # showEvent. Or showEvent est appele a chaque hide/show
            # (calibration, recalibration, etc.) -> on accumulait des
            # connexions et _on_screen_changed etait appele N fois pour
            # 1 seul changement d'ecran. Maintenant on ne connecte
            # qu'une fois via un flag.
            if not getattr(self, "_screen_signal_connected", False):
                h.screenChanged.connect(self._on_screen_changed)
                self._screen_signal_connected = True
                print(f"[WINDOW] Hook screenChanged installe. Ecran : "
                      f"'{self._current_screen.name()}' "
                      f"DPR={self._current_screen.devicePixelRatio()}")
            else:
                # Re-show apres hide : on note juste l'ecran courant,
                # le signal est deja branche.
                print(f"[WINDOW] Re-show. Ecran : "
                      f"'{self._current_screen.name()}' "
                      f"DPR={self._current_screen.devicePixelRatio()}")
        QTimer.singleShot(800, self._arm_resize_detection)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._initial_geom_set:
            cur = (self.width(), self.height())
            if self._last_size is not None and cur != self._last_size:
                self._user_resized = True
            self._last_size = cur

    def moveEvent(self, event):
        super().moveEvent(event)
        # Bug fix : avant, moveEvent set _user_resized=True a chaque
        # mouvement apres _initial_geom_set, y compris les mouvements
        # generes par le WM lors d'un hide/show (calibration). On
        # finissait par sauver une geometry "user_set" alors que
        # l'utilisateur n'avait rien fait. Maintenant on verifie que
        # la position a vraiment change (nouvelle position different
        # de la precedente connue).
        if self._initial_geom_set:
            cur_pos = (self.x(), self.y())
            last_pos = getattr(self, "_last_pos", None)
            if last_pos is not None and cur_pos != last_pos:
                self._user_resized = True
            self._last_pos = cur_pos

    def closeEvent(self, event):
        # 0. Signaler aux threads daemon (OCR, watchdog, audio_ws,
        # heartbeat, gamelog, helmet_scan, volume_safety) qu'on demande
        # un arret. Ils peuvent verifier state.shutdown_requested dans
        # leurs boucles pour sortir proprement avant qu'on les tue.
        # Sans ce flag, ils continuent a tourner jusqu'a ce que
        # os._exit(0) les termine brutalement (ce qui est le cas a la
        # fin de cette fonction de toute facon, mais certains peuvent
        # ecrire sur disque ou dans des sockets entre-temps).
        try:
            state.shutdown_requested = True
        except Exception:
            pass

        # 1. Sauvegarde geometry si user_resized
        try:
            # IMPORTANT : self._cfg a ete charge au boot de l'app. Pendant
            # la session, le manager d'overlays et le core ont pu ecrire
            # dans le fichier disque (via _save_client_cfg) des cles que
            # self._cfg ignore : overlays_active, overlays_config,
            # zone_coords, hotkeys, etc. Si on appelle _save_cfg(self._cfg)
            # tel quel, le merge fait `disque + self._cfg` avec self._cfg
            # qui gagne -> on ECRASE les positions d'overlays sauvees en
            # cours de session par les anciennes valeurs du boot.
            #
            # Solution : resynchroniser self._cfg avec le disque pour les
            # cles gerees par d'autres composants AVANT de saver.
            try:
                if CLIENT_CONFIG_FILE.exists():
                    on_disk = json.loads(
                        CLIENT_CONFIG_FILE.read_text(encoding="utf-8")
                    )
                    if isinstance(on_disk, dict):
                        for key in (
                            "overlays_active",
                            "overlays_config",
                            "zone_coords",
                            "zone_source",
                            "radio_key",
                            "profile_radio_key",
                            "mute_mic_key",
                            "mute_prox_key",
                            "mute_radio_key",
                            "mute_all_key",
                            "proximity_short_key",
                            "cycle_channel_key",
                            "player_volumes",
                            "rp_mode",
                        ):
                            if key in on_disk:
                                self._cfg[key] = on_disk[key]
            except Exception as e:
                print(
                    f"[CLOSE] Resync cfg disque echoue : {e}",
                    file=sys.stderr,
                )

            if self._user_resized:
                self._cfg["window_geometry"] = {
                    "x": self.x(),
                    "y": self.y(),
                    "w": self.width(),
                    "h": self.height(),
                }
                self._cfg["window_geometry_user_set"] = True
                print(f"[CLOSE] Geometry user sauvee : "
                      f"{self.width()}x{self.height()} a "
                      f"({self.x()},{self.y()})")
            # Toujours sauver les reglages audio (les sliders ont pu bouger)
            if hasattr(self, "sl_gain"):
                self._cfg["mic_gain"] = self.sl_gain.value()
            if hasattr(self, "sl_gate"):
                self._cfg["gate_threshold_x2"] = self.sl_gate.value()
            _save_cfg(self._cfg)
        except Exception as e:
            print(f"[CLOSE] Erreur sauvegarde config : {e}", file=sys.stderr)

        # 2. Couper l'audio reseau et la capture
        try:
            state.audio_connected = False
            state.audio_server_ip = None
        except Exception:
            pass
        # Stopper le RadioKeyListener pynput
        try:
            if _CORE_AVAILABLE:
                _core._radio_listener.stop()
        except Exception:
            pass
        # Fermer tous les overlays floating
        try:
            if hasattr(self, "_overlay_manager"):
                self._overlay_manager.close_all()
        except Exception:
            pass
        try:
            if hasattr(self, "_vu_timer"):
                self._vu_timer.stop()
        except Exception:
            pass
        try:
            if state.audio_io is not None:
                state.audio_io.stop_capture()
                state.audio_io.stop_playback()
                print("[CLOSE] Audio stoppe")
        except Exception as e:
            print(f"[CLOSE] Erreur arret audio : {e}", file=sys.stderr)

        # 3. Couper la connexion proprement et stopper le thread worker
        try:
            if state.connected:
                self._worker.request_stop()
        except Exception:
            pass
        try:
            self._worker_thread.quit()
            if not self._worker_thread.wait(1500):
                print("[CLOSE] Worker thread n'a pas termine en 1.5s, "
                      "termination forcee")
                self._worker_thread.terminate()
                self._worker_thread.wait(500)
        except Exception as e:
            print(f"[CLOSE] Erreur arret thread : {e}", file=sys.stderr)

        super().closeEvent(event)

        # Forcer la sortie : les threads daemon importes du client1
        # (OCR, WS audio, heartbeat) ainsi que les pools internes de
        # PyTorch/EasyOCR ont des atexit hooks qui attendent leur join().
        # Ces threads font du GPU compute et ne s'arretent pas
        # spontanement, ce qui fait que le process reste suspendu
        # indefiniment apres la fermeture de la fenetre Qt.
        # Le client1 a exactement le meme fix dans son _on_close.
        os._exit(0)

    @Slot()
    def _arm_resize_detection(self):
        self._last_size = (self.width(), self.height())
        self._initial_geom_set = True

    @Slot(QScreen)
    def _on_screen_changed(self, new_screen: QScreen):
        old_name = self._current_screen.name() if self._current_screen else "?"
        old_dpr = (self._current_screen.devicePixelRatio()
                   if self._current_screen else 0)
        new_dpr = new_screen.devicePixelRatio()
        print(f"[SCREEN] '{old_name}' (DPR={old_dpr}) -> "
              f"'{new_screen.name()}' (DPR={new_dpr})")
        self._current_screen = new_screen


# ======================================================================
# main
# ======================================================================

def main():
    # Parsing CLI minimaliste (pas argparse pour eviter une dep et garder
    # le boot ultra simple). Les flags actuels :
    #   --debug-ocr    Active la sauvegarde des images du pipeline OCR
    #                  dans ./circusvoip_debug/. Utile pour diagnostiquer
    #                  les lectures qui ratent (ex: signe `-` perdu en
    #                  1080p sur une frame). Throttle 5s + rotation 50.
    #   --debug-dir=D  Dossier de sauvegarde (defaut : ./circusvoip_debug/)
    #   -h | --help    Affiche l'aide et quitte.
    debug_ocr = False
    debug_dir = None
    cli_args = sys.argv[1:]
    if "-h" in cli_args or "--help" in cli_args:
        print(
            "Usage : python circusvoip_client.py [options]\n"
            "\n"
            "Options :\n"
            "  --debug-ocr        Sauvegarde les images du pipeline OCR\n"
            "                     (raw, easy_in, tess_in, easyocr) pour\n"
            "                     analyse. Throttle 5s + rotation 50.\n"
            "  --debug-dir=DIR    Dossier de sauvegarde\n"
            "                     (defaut : ./circusvoip_debug/).\n"
            "  -h, --help         Affiche cette aide.\n"
        )
        sys.exit(0)
    # Extraire nos flags et les retirer de sys.argv pour eviter qu'ils
    # ne soient passes a QApplication (Qt ignore les flags inconnus mais
    # peut emettre un warning et c'est plus propre de les retirer).
    qt_argv = [sys.argv[0]]
    for arg in cli_args:
        if arg == "--debug-ocr":
            debug_ocr = True
        elif arg.startswith("--debug-dir="):
            debug_dir = arg.split("=", 1)[1]
        else:
            # Garder pour Qt (style, platform, etc.)
            qt_argv.append(arg)
    sys.argv[:] = qt_argv

    print(f"[BOOT] CircusVOIP Client2 - {_VERSION_STRING}")
    print(f"[BOOT] Python {sys.version.split()[0]}")
    if debug_ocr:
        print(f"[BOOT] DEBUG OCR : actif (--debug-ocr)")
    try:
        import PySide6
        print(f"[BOOT] PySide6 {PySide6.__version__}")
    except Exception:
        pass
    print(f"[BOOT] websockets : {'OK' if _WS_AVAILABLE else 'MANQUANT (pip install websockets)'}")
    if _AUDIO_AVAILABLE:
        print(f"[BOOT] audio_io : OK (SAMPLE_RATE={SAMPLE_RATE}, BLOCK_SIZE={BLOCK_SIZE})")
    else:
        print(f"[BOOT] audio_io : MANQUANT ({_AUDIO_IMPORT_ERROR})")
    if _CORE_AVAILABLE:
        print(f"[BOOT] core module : OK (OCR loop, WS audio, helmet, gamelog)")
    else:
        print(f"[BOOT] core module : MANQUANT ({_CORE_IMPORT_ERROR}) "
              f"-> OCR + WS audio desactives, audio en local seulement")
    print(f"[BOOT] Config : {CLIENT_CONFIG_FILE}")

    if sys.platform == "win32":
        try:
            import ctypes as _ct
            ctx = _ct.windll.user32.GetThreadDpiAwarenessContext()
            cmp_v2 = _ct.windll.user32.AreDpiAwarenessContextsEqual(
                ctx, _ct.c_void_p(-4)
            )
            label = ("PER_MONITOR_AWARE_V2 (OK)" if cmp_v2
                     else "PAS V2 (rescaling DPI degrade)")
            print(f"[BOOT] DPI awareness : {label}")
        except Exception:
            pass

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    _boot_log("avant QApplication()")
    app = QApplication(sys.argv)
    _boot_log("QApplication() cree")

    for i, scr in enumerate(QGuiApplication.screens()):
        g = scr.geometry()
        print(f"[BOOT] Ecran {i} : '{scr.name()}'  "
              f"{g.width()}x{g.height()} a ({g.x()},{g.y()})  "
              f"DPR={scr.devicePixelRatio()}")

    cfg = _load_cfg()
    _boot_log("config chargee")

    # Activer la sauvegarde debug des images OCR si demande en CLI.
    # On le fait apres le _load_cfg (au cas ou la config ait un override)
    # mais avant la creation de MainWindow (qui demarre les threads OCR).
    if debug_ocr:
        try:
            import circusvoip_sc_ocr as _sco_dbg
            _sco_dbg.enable_debug_screens(debug_dir)
        except Exception as e:
            print(f"[BOOT] Impossible d'activer le debug OCR : {e}")

    _boot_log("avant MainWindow()")
    win = MainWindow(cfg)
    _boot_log("apres MainWindow() (constructeur termine)")
    win.show()
    _boot_log("apres win.show() - FENETRE VISIBLE")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
