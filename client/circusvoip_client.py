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
from datetime import datetime
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
    QRectF, QPointF,
    QMetaObject, QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QGuiApplication, QScreen, QCursor, QPainter, QPainterPath, QColor, QPen,
    QFont, QKeyEvent, QMouseEvent, QIcon, QPixmap, QImage, QTextCursor,
    QImageReader,
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
    QDoubleSpinBox,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
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
THEME_PURPLE    = "#bc8cff"

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


def _load_oriented_pixmap(path):
    """Charge une image en APPLIQUANT son orientation EXIF (sinon une photo
    portrait stockee en paysage + tag EXIF, comme certaines captures SC,
    s'affiche couchee)."""
    try:
        rd = QImageReader(str(path))
        rd.setAutoTransform(True)
        img = rd.read()
        if not img.isNull():
            return QPixmap.fromImage(img)
    except Exception:
        pass
    return QPixmap(str(path))

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
# CircusPhone (Feature 4, D4) : annuaire local des contacts. Fichier JSON
# auto-enrichi a chaque session avec les joueurs vus connectes en meme
# temps que l'utilisateur. Ne purge jamais tout seul (l'utilisateur peut
# retirer un contact via le bouton "oublier").
PHONE_ANNUAIRE_FILE = _BASE_DIR / "circusphone_annuaire.json"
# CircusPhone (D4 etape 3) : conversations privees + brouillons. Stockage
# local par contact : 10 envoyes + 10 recus (max 20 messages par contact),
# tronques aux plus recents quand la limite est atteinte. Les brouillons
# (texte en cours de redaction quand on quitte l'ecran conversation) sont
# stockes dans le meme fichier pour pouvoir etre repris a l'ouverture
# suivante de la conversation.
PHONE_MESSAGES_FILE = _BASE_DIR / "circusphone_messages.json"
# --- Images dans les MP (screenshots) ---
PHONE_IMAGES_DIR   = _BASE_DIR / "circusphone_images"   # cache local
PHONE_IMG_PREFIX   = "[img]"     # body special : "[img]<fichier>" = bulle image
PHONE_MAX_IMG_B64  = 900_000     # taille max du base64 (frame WS < 1 Mo)
# Limites de la messagerie (cf spec D4).
PHONE_MAX_BODY_LEN  = 500   # taille max d'un message texte
# Cap GLOBAL : envoyes + recus fusionnes, trie par ts, on garde les N plus
# recents toutes categories confondues. Remplace les anciens PHONE_MAX_SENT
# et PHONE_MAX_RECEIVED (cap a 10 chacun) qui creaient un bug d'historique
# incoherent : quand un user envoyait beaucoup de messages, ses anciens
# envoyes etaient tronques mais les recus correspondants restaient, donnant
# des messages du contact "qui repondent a rien" dans le fil. Fix 23/05/2026
# Kainan. La structure JSON est conservee (sent[] + received[] separes) pour
# retrocompat, le cap est applique apres fusion logique via _phone_trim_convo.
PHONE_MAX_MESSAGES  = 20    # nb total de messages conserves par contact
# Constantes obsoletes conservees temporairement comme alias (au cas ou du
# code legacy / scripts de migration les utilise). A retirer apres v0.2.
PHONE_MAX_SENT      = PHONE_MAX_MESSAGES  # deprecated
PHONE_MAX_RECEIVED  = PHONE_MAX_MESSAGES  # deprecated

# [D5] Photo de profil locale + cache des pairs. La photo locale est
# stockee en JPEG compresse (200x200 q80, generalement 15-30 Ko). Un
# fichier .meta.json suit local_hash, uploaded_hash et updated_at pour
# detecter une desynchro avec le serveur (changement hors-ligne) et
# repousser automatiquement a la prochaine reco. Le cache des pairs vit
# dans un dossier dedie : 1 JPEG par pseudo + un index JSON {pseudo:hash}.
PHONE_PROFILE_PHOTO_FILE       = _BASE_DIR / "circusvoip_profile_photo.jpg"
PHONE_PROFILE_PHOTO_META_FILE  = _BASE_DIR / "circusvoip_profile_photo.meta.json"
# [D5+] Photo source non compressee (PNG pour preserver la qualite). Sert
# de base aux re-compressions quand l'utilisateur ajuste le zoom +/-
# sans dégradation cumulative. Le format PNG est volontaire : meme si la
# source originale est un JPEG, on convertit en PNG sans perte pour
# pouvoir re-cropper a volonte.
PHONE_PROFILE_PHOTO_SOURCE_FILE = _BASE_DIR / "circusvoip_profile_photo_source.png"
PHONE_PROFILE_CACHE_DIR        = _BASE_DIR / "circusvoip_profile_photo_cache"
PHONE_PROFILE_CACHE_INDEX_FILE = PHONE_PROFILE_CACHE_DIR / "_index.json"
# Limite stricte cote client (doit etre coherente avec _PROFILE_PHOTO_MAX_BYTES
# cote serveur). On compresse jusqu'a tenir sous cette limite.
PHONE_PROFILE_PHOTO_MAX_BYTES  = 200_000
PHONE_PROFILE_PHOTO_DIM        = 200    # cote max en pixels
# [D5+] Zoom factor pour le crop carre.
#   1.0 = prendre tout le carre central possible (cadrage le plus large
#         qui rentre dans l'image, limite par min(w, h)).
#   0.40 = ne prendre que les 40% centraux (zoom marque, portrait visage).
#   >1.0 = "dezoomer" au-dela de l'image : on prend un carre virtuel plus
#          grand que l'image, et les zones manquantes sont remplies avec
#          des bandes noires (padding). Utile pour les photos paysage ou
#          portrait pour montrer toute la largeur ou la hauteur dans le
#          carre, au prix de bandes esthétiquement moyennes.
# Pas : 0.05 par clic +/-.
PHONE_PROFILE_ZOOM_MIN         = 0.40
PHONE_PROFILE_ZOOM_MAX         = 2.00
PHONE_PROFILE_ZOOM_STEP        = 0.05
PHONE_PROFILE_ZOOM_DEFAULT     = 1.00    # comme avant par defaut


# ─────────────────────────────────────────────
#  [D5] ProfilePhotoManager : photo locale + cache des pairs
# ─────────────────────────────────────────────
#
# Singleton instancie au demarrage du MainWindow. Centralise :
#   - La photo de profil locale (compression Pillow, ecriture disque,
#     hash SHA-256, meta pour suivre la synchro avec le serveur).
#   - Le cache disque des photos des pairs (1 JPEG par pseudo +
#     index JSON {pseudo: hash}).
#   - L'envoi de profile_photo_upload (avec retry a chaque reconnexion).
#   - L'envoi de profile_photo_request et le traitement de
#     profile_photo_response (mise a jour du cache + notification UI).
#
# Pas thread-safe au sens strict : toutes les operations critiques sont
# faites dans le thread principal Qt (l'envoi WS est deja relai par
# _core._ws_send_safe qui est thread-safe). Les operations disque sont
# courtes (quelques Ko) donc le blocage est negligeable.

class _ProfilePhotoManager:
    """Gere la photo locale et le cache des pairs (D5)."""

    def __init__(self, owner):
        # owner : MainWindow, sert pour les signaux Qt et l'acces _core.
        self._owner = owner
        # Hash SHA-256 hex de la photo locale actuelle (None si pas de photo).
        self._local_hash: str | None = None
        # Hash deja confirme uploade au serveur (None si jamais uploade
        # ou si on a change la photo en mode offline).
        self._uploaded_hash: str | None = None
        # [D5+] Zoom factor courant (1.0 = cadrage large, 0.40 = zoom max).
        # Persiste dans la meta pour survivre aux redemarrages.
        self._zoom: float = PHONE_PROFILE_ZOOM_DEFAULT
        # [D5+] Offset du centre du crop par rapport au centre de l'image
        # source, en pixels. (0, 0) = centre = comportement par defaut.
        # Persiste dans la meta. Borne par _recompress_from_source pour
        # que le crop reste dans l'image (le crop ne peut pas sortir).
        self._offset_x: int = 0
        self._offset_y: int = 0
        # Cache memoire des photos des pairs : {pseudo: (bytes_jpeg, hash)}.
        # Les bytes restent en RAM pour eviter de relire le disque a chaque
        # affichage. Quelques Mo max pour des dizaines de pairs.
        self._peer_cache: dict[str, tuple[bytes, str]] = {}
        # Hash des photos pairs en cache disque : {pseudo: hash}. Sert au
        # if-none-match (on demande au serveur "j'ai deja ce hash, change-le
        # si tu en as un autre").
        self._peer_hashes: dict[str, str] = {}
        # Pseudos pour lesquels une request est en cours (anti-double-shot).
        self._pending_requests: set[str] = set()
        # Charge les meta locales et l'index pairs.
        self._load_meta()
        self._load_peer_index()

    # ─── persistance locale (photo de l'utilisateur) ───────────────

    def _load_meta(self):
        """Lit circusvoip_profile_photo.meta.json. Si absent ou corrompu,
        on repart d'un etat 'pas de photo'. Si le fichier JPEG existe mais
        pas la meta, on recalcule le hash et on considere la photo non
        encore uploadee (sera repoussee a la prochaine reco)."""
        meta_path = PHONE_PROFILE_PHOTO_META_FILE
        photo_path = PHONE_PROFILE_PHOTO_FILE
        loaded = None
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception:
                loaded = None
        if isinstance(loaded, dict):
            lh = loaded.get("local_hash")
            uh = loaded.get("uploaded_hash")
            if isinstance(lh, str) and lh:
                self._local_hash = lh
            if isinstance(uh, str) and uh:
                self._uploaded_hash = uh
            # [D5+] Zoom factor persiste (par defaut si absent ou hors bornes).
            z = loaded.get("zoom")
            try:
                z = float(z)
                if PHONE_PROFILE_ZOOM_MIN <= z <= PHONE_PROFILE_ZOOM_MAX:
                    self._zoom = z
            except (TypeError, ValueError):
                pass
            # [D5+] Offsets de crop persistes (int en pixels).
            ox = loaded.get("offset_x")
            oy = loaded.get("offset_y")
            try:
                if ox is not None:
                    self._offset_x = int(ox)
            except (TypeError, ValueError):
                pass
            try:
                if oy is not None:
                    self._offset_y = int(oy)
            except (TypeError, ValueError):
                pass
        # Si on a une meta mais pas le fichier photo, on reset (incoherent).
        if self._local_hash and not photo_path.exists():
            self._local_hash = None
            self._uploaded_hash = None
            self._save_meta()
            return
        # Si on a un fichier photo mais pas de meta, on recalcule le hash.
        if photo_path.exists() and not self._local_hash:
            try:
                import hashlib
                with open(photo_path, "rb") as f:
                    self._local_hash = hashlib.sha256(f.read()).hexdigest()
                self._uploaded_hash = None
                self._save_meta()
            except Exception:
                pass

    def _save_meta(self):
        """Ecrit le fichier meta. Best-effort."""
        try:
            data = {
                "local_hash":    self._local_hash,
                "uploaded_hash": self._uploaded_hash,
                "zoom":          round(self._zoom, 4),
                "offset_x":      int(self._offset_x),
                "offset_y":      int(self._offset_y),
                "updated_at":    time.time(),
            }
            with open(PHONE_PROFILE_PHOTO_META_FILE, "w",
                      encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def has_local_photo(self) -> bool:
        return self._local_hash is not None and PHONE_PROFILE_PHOTO_FILE.exists()

    def local_photo_path(self):
        return PHONE_PROFILE_PHOTO_FILE if self.has_local_photo() else None

    def needs_upload(self) -> bool:
        """True si on a une photo locale qui n'a pas (encore) ete
        confirmee uploadee au serveur (changement hors-ligne, ou jamais
        envoyee)."""
        if not self.has_local_photo():
            return False
        return self._local_hash != self._uploaded_hash

    def get_zoom(self) -> float:
        """Retourne le zoom factor courant (1.0 = large, 0.40 = max)."""
        return self._zoom

    def has_source(self) -> bool:
        """True si on a une copie de la source non compressee permettant
        de re-cropper a volonte sans degradation."""
        return PHONE_PROFILE_PHOTO_SOURCE_FILE.exists()

    def set_local_photo_from_file(self, src_path: str) -> tuple[bool, str]:
        """Selection utilisateur : copie la source en PNG (conserve sans
        perte) puis applique le zoom courant pour produire le JPEG final
        a 200x200. Retourne (ok, message). Si le zoom courant est invalide
        (premiere selection), on part du zoom par defaut (1.0)."""
        try:
            from PIL import Image
        except Exception:
            return False, "Pillow indisponible (pip install Pillow)."
        try:
            img = Image.open(src_path)
            # Conversion RGB pour homogeneiser (l'alpha n'a pas de sens
            # pour un avatar opaque).
            img = img.convert("RGB")
            # Sauvegarde de la source non compressee (PNG, sans perte).
            # On redimensionne raisonnablement si l'image est trop grosse
            # (eviter 30-50 Mo de PNG sur le disque pour une photo 4K).
            # Borne : 2048 px sur le plus grand cote, ce qui laisse une
            # grosse marge pour zoomer/cadrer fin sans degradation visible
            # (rendu final = 200x200, donc 2048 -> 200 est largement suffisant).
            max_src_dim = 2048
            if max(img.size) > max_src_dim:
                img.thumbnail((max_src_dim, max_src_dim), Image.LANCZOS)
            img.save(PHONE_PROFILE_PHOTO_SOURCE_FILE, "PNG", optimize=True)
        except Exception as e:
            return False, f"Echec import : {e}"
        # Applique le zoom courant (ou defaut si pas encore defini).
        if not (PHONE_PROFILE_ZOOM_MIN <= self._zoom <= PHONE_PROFILE_ZOOM_MAX):
            self._zoom = PHONE_PROFILE_ZOOM_DEFAULT
        # Nouvelle photo = on recentre (offset (0, 0)). Sinon un vieux
        # offset d'une autre image risquerait de mal cadrer.
        self._offset_x = 0
        self._offset_y = 0
        ok, msg = self._recompress_from_source(self._zoom)
        if not ok:
            return False, msg
        # Tentative d'upload immediate (best-effort).
        self.try_upload_local()
        return True, "Photo enregistree."

    def adjust_zoom(self, delta: float) -> tuple[bool, str]:
        """Ajuste le zoom factor de `delta` (positif = zoom +, negatif =
        zoom -). Borne au range [MIN, MAX]. Recompresse a partir de la
        source et re-uploade. Retourne (ok, message). No-op si pas de
        source disponible (besoin de Choisir une photo d'abord)."""
        if not self.has_source():
            return False, "Pas de photo source. Choisissez une photo d'abord."
        new_zoom = max(PHONE_PROFILE_ZOOM_MIN,
                       min(PHONE_PROFILE_ZOOM_MAX, self._zoom + delta))
        # Si on est deja a la borne dans la direction demandee, no-op
        # (pas d'erreur, c'est juste sans effet).
        if abs(new_zoom - self._zoom) < 1e-6:
            return True, "Zoom inchange (borne atteinte)."
        self._zoom = new_zoom
        ok, msg = self._recompress_from_source(new_zoom)
        if not ok:
            return False, msg
        self.try_upload_local()
        return True, f"Zoom : {int(new_zoom * 100)}%"

    def get_offset(self) -> tuple[int, int]:
        """Retourne l'offset courant (dx, dy) en pixels par rapport au
        centre de l'image source."""
        return (self._offset_x, self._offset_y)

    def get_offset_step(self) -> int:
        """Retourne le pas de deplacement conseille pour une fleche
        directionnelle, en pixels source. Proportionnel a la taille du
        carre courant (10%) pour que le mouvement reste cohérent visuelle-
        ment quel que soit le zoom. Minimum 4 pour garantir un mouvement
        visible meme sur petite source."""
        if not self.has_source():
            return 8
        try:
            from PIL import Image
            img = Image.open(PHONE_PROFILE_PHOTO_SOURCE_FILE)
            w, h = img.size
        except Exception:
            return 8
        base_side = min(w, h)
        side = int(base_side * self._zoom)
        step = max(4, int(side * 0.10))
        return step

    def adjust_offset(self, dx: int, dy: int) -> tuple[bool, str]:
        """Deplace le centre du crop de (dx, dy) pixels. Bornes
        appliquees selon le zoom :
          - zoom <= 1.0 : le crop ne peut pas sortir de l'image
          - zoom >  1.0 : l'image ne peut pas sortir du carre (sinon
                          on n'aurait que du noir)
        Recompresse depuis la source et re-uploade."""
        if not self.has_source():
            return False, "Pas de photo source. Choisissez une photo d'abord."
        new_ox = self._offset_x + int(dx)
        new_oy = self._offset_y + int(dy)
        try:
            from PIL import Image
            img = Image.open(PHONE_PROFILE_PHOTO_SOURCE_FILE)
            w, h = img.size
        except Exception:
            return False, "Impossible de lire la source."
        base_side = min(w, h)
        side = int(base_side * self._zoom)
        if self._zoom <= 1.0:
            # Crop dans l'image : limite par les bords de l'image.
            max_dx = max(0, (w - side) // 2)
            max_dy = max(0, (h - side) // 2)
        else:
            # Carre virtuel plus grand que l'image : limite par le canevas.
            # On garde toujours au moins 1 pixel d'image visible.
            max_dx = max(0, side // 2 - 1)
            max_dy = max(0, side // 2 - 1)
        new_ox = max(-max_dx, min(max_dx, new_ox))
        new_oy = max(-max_dy, min(max_dy, new_oy))
        if new_ox == self._offset_x and new_oy == self._offset_y:
            return True, "Position inchangee (borne atteinte)."
        self._offset_x = new_ox
        self._offset_y = new_oy
        ok, msg = self._recompress_from_source(self._zoom)
        if not ok:
            return False, msg
        self.try_upload_local()
        return True, f"Decalage : ({new_ox}, {new_oy})"

    def reset_offset(self) -> tuple[bool, str]:
        """Recentre l'image (offset 0, 0). Recompresse + ré-uploade.
        No-op si deja a (0, 0)."""
        if not self.has_source():
            return False, "Pas de photo source. Choisissez une photo d'abord."
        if self._offset_x == 0 and self._offset_y == 0:
            return True, "Deja centre."
        self._offset_x = 0
        self._offset_y = 0
        ok, msg = self._recompress_from_source(self._zoom)
        if not ok:
            return False, msg
        self.try_upload_local()
        return True, "Recentre."

    def _recompress_from_source(self, zoom: float) -> tuple[bool, str]:
        """Relit la source PNG, applique un crop carre de cote
        max(w,h)*zoom_normalise au centre (+ offset), redimensionne en
        200x200, compresse en JPEG q80, ecrit le fichier final + meta +
        reset uploaded_hash. Source-of-truth pour la generation du JPEG
        final.

        Comportement selon zoom :
          zoom <= 1.0 : crop carre dans l'image (cote = min(w,h)*zoom).
          zoom > 1.0  : carre virtuel plus grand que l'image. La zone
                        qui depasse de l'image est remplie de noir
                        (padding). Permet de voir toute la largeur d'une
                        photo paysage par exemple.

        L'offset (self._offset_x, self._offset_y) deplace le centre du
        carre. Pour zoom <= 1.0, l'offset est clamp pour que le carre
        reste dans l'image. Pour zoom > 1.0, l'offset est libre (mais
        plafonne pour eviter de pousser l'image hors du carre)."""
        if not PHONE_PROFILE_PHOTO_SOURCE_FILE.exists():
            return False, "Source manquante."
        try:
            from PIL import Image
        except Exception:
            return False, "Pillow indisponible."
        try:
            img = Image.open(PHONE_PROFILE_PHOTO_SOURCE_FILE)
            img = img.convert("RGB")
            w, h = img.size
            # Cote du carre : base = min(w,h) (le plus grand carre qui
            # rentre), puis multiplie par le zoom.
            #   zoom = 1.0 -> cote = min(w, h)             [crop normal]
            #   zoom = 0.5 -> cote = min(w, h) * 0.5       [zoom IN]
            #   zoom = 1.5 -> cote = min(w, h) * 1.5       [zoom OUT + padding]
            base_side = min(w, h)
            side = int(base_side * zoom)
            if side < 8:
                side = 8

            if zoom <= 1.0:
                # ─── Cas normal : crop dans l'image ─────────────────────
                # Clamp l'offset pour que le rectangle reste dans l'image.
                max_dx = max(0, (w - side) // 2)
                max_dy = max(0, (h - side) // 2)
                ox = max(-max_dx, min(max_dx, self._offset_x))
                oy = max(-max_dy, min(max_dy, self._offset_y))
                self._offset_x = ox
                self._offset_y = oy
                cx = w // 2 + ox
                cy = h // 2 + oy
                left = cx - side // 2
                top  = cy - side // 2
                right  = left + side
                bottom = top + side
                # Garde-fou defensif.
                left = max(0, left)
                top = max(0, top)
                right = min(w, right)
                bottom = min(h, bottom)
                cropped = img.crop((left, top, right, bottom))
            else:
                # ─── Cas zoom > 1.0 : carre virtuel + padding noir ─────
                # On cree un canevas noir de taille side x side et on y
                # colle l'image source centree (+ offset). Les zones non
                # couvertes par l'image restent noires.
                # Clamp l'offset pour empecher l'image de sortir
                # totalement du carre (sinon on aurait juste du noir) :
                # on autorise un offset max = (side - w) // 2 + w // 2
                # pour x, mais la limite pratique est de ne pas pousser
                # l'image au-dela du bord du canevas.
                # Plus simple : on clamp pour que l'image soit toujours
                # au moins partiellement visible. On limite l'offset a
                # +-(side // 2) - 1 pour ne pas tout noircir.
                max_dx = max(0, side // 2 - 1)
                max_dy = max(0, side // 2 - 1)
                ox = max(-max_dx, min(max_dx, self._offset_x))
                oy = max(-max_dy, min(max_dy, self._offset_y))
                self._offset_x = ox
                self._offset_y = oy
                # Canevas noir de cote `side`.
                cropped = Image.new("RGB", (side, side), (0, 0, 0))
                # Position du coin haut-gauche de l'image dans le canevas :
                # centre du canevas = (side//2, side//2)
                # centre voulu de l'image = centre canevas - offset
                # coin haut-gauche = centre voulu - (w//2, h//2)
                cx_target = side // 2 - ox
                cy_target = side // 2 - oy
                paste_x = cx_target - w // 2
                paste_y = cy_target - h // 2
                cropped.paste(img, (paste_x, paste_y))

            cropped.thumbnail(
                (PHONE_PROFILE_PHOTO_DIM, PHONE_PROFILE_PHOTO_DIM),
                Image.LANCZOS,
            )
            import io
            quality = 80
            jpeg_bytes = None
            for _ in range(5):
                buf = io.BytesIO()
                cropped.save(buf, "JPEG", quality=quality, optimize=True)
                candidate = buf.getvalue()
                if len(candidate) <= PHONE_PROFILE_PHOTO_MAX_BYTES:
                    jpeg_bytes = candidate
                    break
                quality -= 10
                if quality < 40:
                    jpeg_bytes = candidate
                    break
            if jpeg_bytes is None or len(jpeg_bytes) > PHONE_PROFILE_PHOTO_MAX_BYTES:
                return False, (f"Photo trop volumineuse apres compression "
                               f"({len(jpeg_bytes or b'')} bytes).")
            with open(PHONE_PROFILE_PHOTO_FILE, "wb") as f:
                f.write(jpeg_bytes)
            import hashlib
            self._local_hash = hashlib.sha256(jpeg_bytes).hexdigest()
            self._uploaded_hash = None
            self._save_meta()
            return True, "OK"
        except Exception as e:
            return False, f"Echec recompression : {e}"

    def clear_local_photo(self):
        """Supprime la photo locale (JPEG + meta + source PNG). Pas de
        notification serveur : la photo reste sur le serveur tant que
        l'utilisateur n'en met pas une autre. Comportement volontaire :
        on n'a pas de message 'profile_photo_delete' cote serveur pour
        rester simple."""
        for p in (PHONE_PROFILE_PHOTO_FILE,
                  PHONE_PROFILE_PHOTO_META_FILE,
                  PHONE_PROFILE_PHOTO_SOURCE_FILE):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        self._local_hash = None
        self._uploaded_hash = None
        self._zoom = PHONE_PROFILE_ZOOM_DEFAULT
        self._offset_x = 0
        self._offset_y = 0

    def try_upload_local(self) -> bool:
        """Tente d'uploader la photo locale au serveur si necessaire.
        Retourne True si une tentative a ete faite (succes WS), False
        si pas de photo ou pas connecte."""
        if not self.needs_upload():
            return False
        if not (_CORE_AVAILABLE and state.connected):
            return False
        try:
            with open(PHONE_PROFILE_PHOTO_FILE, "rb") as f:
                jpeg_bytes = f.read()
        except Exception:
            return False
        try:
            import base64
            data_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
            ok = _core._ws_send_safe({
                "type": "profile_photo_upload",
                "data_b64": data_b64,
                "hash": self._local_hash,
            })
            if ok:
                # On marque comme uploade en optimiste : si le serveur
                # rejette silencieusement, la prochaine reco re-tentera
                # (uploaded_hash != local_hash via le meta a la reload).
                # Mais pour la session courante on evite de spammer.
                # NB : si on veut etre strict, il faudrait un ack du
                # serveur, mais on a decide de garder le protocole sobre.
                self._uploaded_hash = self._local_hash
                self._save_meta()
                return True
        except Exception:
            pass
        return False

    # ─── cache des photos des pairs ─────────────────────────────────

    def _load_peer_index(self):
        """Charge l'index disque des photos pairs. Cree le dossier si
        absent. Format : {pseudo: hash}."""
        try:
            PHONE_PROFILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        if not PHONE_PROFILE_CACHE_INDEX_FILE.exists():
            return
        try:
            with open(PHONE_PROFILE_CACHE_INDEX_FILE, "r",
                      encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cleaned = {}
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str) and v:
                        cleaned[k] = v
                self._peer_hashes = cleaned
        except Exception:
            self._peer_hashes = {}

    def _save_peer_index(self):
        try:
            with open(PHONE_PROFILE_CACHE_INDEX_FILE, "w",
                      encoding="utf-8") as f:
                json.dump(self._peer_hashes, f, indent=2)
        except Exception:
            pass

    def _peer_cache_path(self, pseudo: str):
        # Sanitisation defensive (le pseudo vient du serveur, deja filtre,
        # mais on couvre les cas exotiques).
        if (not isinstance(pseudo, str) or not pseudo
                or "/" in pseudo or "\\" in pseudo or pseudo in (".", "..")):
            return None
        return PHONE_PROFILE_CACHE_DIR / f"{pseudo}.jpg"

    def get_peer_photo_bytes(self, pseudo: str) -> bytes | None:
        """Retourne les bytes JPEG d'un pair s'ils sont en cache. None
        sinon. Charge depuis le disque si pas encore en cache memoire."""
        if not pseudo:
            return None
        cached = self._peer_cache.get(pseudo)
        if cached is not None:
            return cached[0]
        # Pas en RAM : on tente le disque.
        path = self._peer_cache_path(pseudo)
        if path is None or not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                b = f.read()
        except Exception:
            return None
        h = self._peer_hashes.get(pseudo, "")
        self._peer_cache[pseudo] = (b, h)
        return b

    def request_peer_photo(self, pseudo: str):
        """Demande la photo d'un pair au serveur (avec if-none-match si
        on a deja un hash en cache). Idempotent : un seul request en vol
        par pseudo. La reponse arrivera via handle_response()."""
        if not pseudo:
            return
        if pseudo in self._pending_requests:
            return
        if not (_CORE_AVAILABLE and state.connected):
            return
        my_name = getattr(state, "my_name", "") or ""
        if pseudo == my_name:
            # Inutile de demander sa propre photo au serveur, on l'a en
            # local. Cas a gerer ailleurs (affichage de soi-meme).
            return
        try:
            ok = _core._ws_send_safe({
                "type": "profile_photo_request",
                "target": pseudo,
                "if_none_match": self._peer_hashes.get(pseudo) or "",
            })
            if ok:
                self._pending_requests.add(pseudo)
        except Exception:
            pass

    def handle_response(self, target: str, status: str,
                        new_hash: str | None, data_b64: str | None) -> bool:
        """Traite un profile_photo_response. Retourne True si le cache a
        ete mis a jour (la UI doit se rafraichir pour ce pseudo)."""
        self._pending_requests.discard(target)
        if not target:
            return False
        if status == "unchanged":
            return False
        if status == "none":
            # Le serveur n'a pas de photo pour ce pseudo. Si on en avait
            # une en cache (cas rare : photo supprimee cote owner), on la
            # garde quand meme : pas de mecanisme de delete. Comportement
            # volontaire selon la spec (pas de cleanup tant que pas de
            # nouvelle photo).
            return False
        if status != "ok" or not data_b64 or not new_hash:
            return False
        try:
            import base64
            jpeg_bytes = base64.b64decode(data_b64, validate=True)
        except Exception:
            return False
        if len(jpeg_bytes) > PHONE_PROFILE_PHOTO_MAX_BYTES:
            return False
        # Ecriture disque + cache memoire + index.
        path = self._peer_cache_path(target)
        if path is None:
            return False
        try:
            with open(path, "wb") as f:
                f.write(jpeg_bytes)
        except Exception:
            return False
        self._peer_cache[target] = (jpeg_bytes, new_hash)
        self._peer_hashes[target] = new_hash
        self._save_peer_index()
        return True


def _load_version_info() -> dict:
    """Charge le fichier circusvoip_version.json a cote du script.
    Retourne un dict avec 'version' (X.Y.Z), 'channel' (alpha/beta/rc/stable),
    'build' (entier). Si le fichier n'existe pas ou est invalide, retourne
    une version par defaut '0.0.0 alpha 000' (signal qu'il y a un probleme).

    encoding="utf-8-sig" : tolere un BOM UTF-8 optionnel en tete de fichier.
    Necessaire car certains outils (notamment ISPP SaveStringToFile avec
    UTF8=1) ajoutent un BOM, et "utf-8" brut ne le consomme pas -> exception
    "Unexpected character" -> tombe sur le default "0.0.0 alpha 000".
    "utf-8-sig" lit indifferemment avec ou sans BOM."""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return {
            "version": str(data.get("version", "0.0.0")),
            "channel": str(data.get("channel", "alpha")),
            "build":   int(data.get("build", 0)),
        }
    except Exception:
        return {"version": "0.0.0", "channel": "alpha", "build": 0}


def _format_version_string(info: dict = None) -> str:
    """Retourne la version sous forme lisible.

    Deux formats selon le canal :
      - channel = 'stable' : version seule, ex. '0.1.2'
                             (pour les releases publiques sur GitHub : pas
                             besoin d'exposer le numero de build interne)
      - autres channels    : version complete, ex. '0.1.2 alpha 035'
        (alpha/beta/rc...)   (pour le dev : on veut savoir quel build precis
                             on teste, et identifier rapidement un canal
                             pre-release)

    Le numero de build est zero-padde sur 3 chiffres dans le format complet."""
    if info is None:
        info = _load_version_info()
    if info.get('channel') == 'stable':
        return info['version']
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
    # Validation anti-path-traversal : un manifest malveillant pourrait
    # contenir "name": "../../Windows/System32/foo.dll" ce qui ecrirait
    # en dehors de dest_dir. On normalise puis verifie que le chemin reste
    # dans dest_dir. On refuse aussi les chemins absolus.
    if (
        ".." in name.replace("\\", "/").split("/")
        or name.startswith("/")
        or name.startswith("\\")
        or (len(name) > 1 and name[1] == ":")  # chemin Windows absolu type "C:..."
    ):
        try:
            if _CORE_AVAILABLE:
                _core._dbg_log(f"[UPDATE] Nom de fichier suspect refuse : {name}")
        except Exception:
            pass
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
        # Creer les dossiers parents si le nom contient un sous-chemin
        # (ex: "sounds/dial.wav" -> creer dest_dir/sounds/ avant l'open).
        # Sans ca, l'open echoue avec FileNotFoundError sur le parent.
        dest.parent.mkdir(parents=True, exist_ok=True)
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
                # Creer les dossiers parents au cas ou name contient
                # un sous-chemin (ex: "sounds/dial.wav").
                dst.parent.mkdir(parents=True, exist_ok=True)
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
    # Hotkeys (8 raccourcis + 5 CircusPhone D4)
    "radio_key", "profile_radio_key",
    "mute_mic_key", "mute_prox_key", "mute_radio_key", "mute_all_key",
    "proximity_short_key", "cycle_channel_key",
    "phone_open_key", "phone_accept_key", "phone_decline_key",
    "phone_mute_key", "phone_speaker_key",
    # Fond d'ecran du CircusPhone (ecrit en cours de session par l'app
    # Parametres via _core._save_client_cfg ; sans ca, _save_cfg(self._cfg)
    # reecraserait au close avec la valeur du boot -> fond non sauvegarde).
    "phone_wallpaper", "phone_wallpaper_key",
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
# CircusPhone (Feature 4, D4) : annuaire local des contacts
# ======================================================================
# L'annuaire est un simple fichier JSON a cote du script. Structure :
#   {
#     "contacts": {
#       "<pseudo>": {
#         "first_seen": "<iso8601>",   # 1re fois vu connecte avec moi
#         "last_seen":  "<iso8601>",   # derniere fois vu connecte avec moi
#       },
#       ...
#     }
#   }
# Enrichissement : a chaque fois que le client recoit la liste des joueurs
# connectes (welcome / join), tout pseudo absent est ajoute, tout pseudo
# deja present voit son last_seen mis a jour. On ne purge jamais
# automatiquement : seul l'utilisateur retire un contact ("oublier").
# Le statut connecte/deconnecte n'est PAS stocke dans le fichier : il est
# calcule a l'affichage en croisant avec state.players (joueurs en ligne).

def _phone_load_annuaire() -> dict:
    """Charge l'annuaire depuis PHONE_ANNUAIRE_FILE. Retourne toujours un
    dict de forme {"contacts": {...}} (vide si fichier absent ou illisible)."""
    try:
        if PHONE_ANNUAIRE_FILE.exists():
            data = json.loads(PHONE_ANNUAIRE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("contacts"), dict):
                return data
    except Exception as e:
        print(f"[PHONE] Echec lecture annuaire : {e}", file=sys.stderr)
    return {"contacts": {}}


def _phone_save_annuaire(annuaire: dict) -> bool:
    """Sauvegarde l'annuaire dans PHONE_ANNUAIRE_FILE. Best-effort :
    retourne True si l'ecriture a reussi, False sinon (jamais d'exception
    remontee a l'appelant)."""
    try:
        PHONE_ANNUAIRE_FILE.write_text(
            json.dumps(annuaire, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(f"[PHONE] Echec sauvegarde annuaire : {e}", file=sys.stderr)
        return False


def _phone_enrich_annuaire(annuaire: dict, pseudos, my_name: str = "") -> bool:
    """Enrichit l'annuaire avec une liste de pseudos vus connectes.
      - pseudo absent  -> ajoute (first_seen = last_seen = maintenant)
      - pseudo present -> last_seen mis a jour (en memoire seulement)
      - mon propre pseudo (my_name) est ignore (on ne s'ajoute pas soi-meme)
    Modifie `annuaire` en place. Retourne True UNIQUEMENT si au moins un
    nouveau contact a ete ajoute (donc qu'il faut sauvegarder le fichier).
    Les simples mises a jour de last_seen ne declenchent PAS de save :
    sinon on ecrirait le fichier a chaque pos recue (toutes les secondes
    par joueur connecte), ce qui userait le disque sans valeur ajoutee."""
    contacts = annuaire.setdefault("contacts", {})
    now_iso = datetime.now().isoformat(timespec="seconds")
    added = False
    for pseudo in pseudos:
        if not isinstance(pseudo, str) or not pseudo:
            continue
        if my_name and pseudo == my_name:
            continue
        entry = contacts.get(pseudo)
        if entry is None:
            contacts[pseudo] = {"first_seen": now_iso, "last_seen": now_iso}
            added = True
        else:
            # Update memoire uniquement. Pas de signal de save : on
            # economise les I/O disque.
            entry["last_seen"] = now_iso
    return added


def _phone_forget_contact(annuaire: dict, pseudo: str) -> bool:
    """Retire un contact de l'annuaire ("oublier ce contact"). Modifie
    `annuaire` en place. Retourne True si le contact existait et a ete
    retire, False s'il n'etait pas la."""
    contacts = annuaire.get("contacts", {})
    if pseudo in contacts:
        del contacts[pseudo]
        return True
    return False


# ======================================================================
# CircusPhone (Feature 4, D4 etape 3) : messagerie privee
# ======================================================================
# Stockage d'une conversation par contact. Structure du fichier :
#   {
#     "conversations": {
#       "<pseudo>": {
#         "sent":     [{"ts": <float>, "body": "<str>"}, ...max 10],
#         "received": [{"ts": <float>, "body": "<str>"}, ...max 10],
#         "draft":    "<str>",    # brouillon en cours (vide si aucun)
#         "unread":   <int>,      # nb de messages non lus
#       },
#       ...
#     }
#   }
# 10 envoyes + 10 recus par contact (cf spec). Le 'sent' est tronque
# par l'avant (on retire les plus anciens). 'received' idem. 'draft' est
# vide par defaut, rempli quand on quitte l'ecran conversation avec du
# texte non envoye, vide quand on envoie. 'unread' incremente a chaque
# reception, remis a 0 quand on ouvre la conversation.

def _phone_load_messages() -> dict:
    """Charge le fichier de conversations. Retourne toujours un dict de
    forme {"conversations": {...}} (vide si fichier absent / illisible).
    Applique _phone_trim_convo sur chaque conversation chargee pour
    normaliser les fichiers anciens (cap 10+10) au nouveau cap global
    (20 messages total). Pas d'ecriture disque ici : la normalisation
    sera persistee au prochain save naturel (envoi/reception/draft)."""
    try:
        if PHONE_MESSAGES_FILE.exists():
            data = json.loads(PHONE_MESSAGES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(
                data.get("conversations"), dict
            ):
                # Normalisation : applique le cap global a chaque convo.
                # Idempotent si deja au format propre.
                for convo in data["conversations"].values():
                    if isinstance(convo, dict):
                        # _phone_get_convo-like garde-fous pour les vieux
                        # fichiers qui manqueraient une cle.
                        convo.setdefault("sent", [])
                        convo.setdefault("received", [])
                        convo.setdefault("draft", "")
                        convo.setdefault("unread", 0)
                        _phone_trim_convo(convo)
                return data
    except Exception as e:
        print(f"[PHONE] Echec lecture messages : {e}", file=sys.stderr)
    return {"conversations": {}}


def _phone_save_messages(messages: dict) -> bool:
    """Sauvegarde les conversations dans PHONE_MESSAGES_FILE. Best-effort :
    retourne True si l'ecriture a reussi, False sinon."""
    try:
        PHONE_MESSAGES_FILE.write_text(
            json.dumps(messages, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(f"[PHONE] Echec sauvegarde messages : {e}", file=sys.stderr)
        return False


def _phone_get_convo(messages: dict, pseudo: str) -> dict:
    """Retourne (ou cree) le dict de conversation pour ce contact. Modifie
    `messages` en place si la conversation n'existait pas encore."""
    convos = messages.setdefault("conversations", {})
    convo = convos.get(pseudo)
    if convo is None:
        convo = {"sent": [], "received": [], "draft": "", "unread": 0}
        convos[pseudo] = convo
    else:
        # Garde-fous : si un fichier ancien manque des cles, on complete.
        convo.setdefault("sent", [])
        convo.setdefault("received", [])
        convo.setdefault("draft", "")
        convo.setdefault("unread", 0)
    return convo


def _phone_trim_convo(convo: dict) -> None:
    """Tronque une conversation a PHONE_MAX_MESSAGES messages au TOTAL
    (envoyes + recus). Fusionne sent[] et received[] par timestamp, garde
    les N plus recents, puis re-separe en sent[]/received[] pour preserver
    la structure JSON (retrocompat).

    Sans cette fusion, le cap separe sur sent et received creait un fil
    incoherent : si on envoyait beaucoup, nos vieux envoyes etaient
    tronques mais les recus de l'epoque restaient -> messages du contact
    "qui repondent a rien". Bug observe 23/05/2026 Kainan.

    Modifie convo en place. Idempotent.
    """
    sent = convo.get("sent", []) or []
    received = convo.get("received", []) or []
    total = len(sent) + len(received)
    if total <= PHONE_MAX_MESSAGES:
        return
    # Fusionner avec marqueur de provenance (True = sent, False = received).
    merged = []
    for m in sent:
        merged.append((float(m.get("ts", 0.0)), True, m))
    for m in received:
        merged.append((float(m.get("ts", 0.0)), False, m))
    # Trier par ts croissant (les plus anciens en tete).
    merged.sort(key=lambda x: x[0])
    # Garder les N plus recents (queue de la liste).
    kept = merged[-PHONE_MAX_MESSAGES:]
    # Re-separer en sent / received en conservant l'ordre par ts.
    new_sent = [item[2] for item in kept if item[1]]
    new_received = [item[2] for item in kept if not item[1]]
    convo["sent"] = new_sent
    convo["received"] = new_received


def _phone_append_sent(messages: dict, pseudo: str, body: str,
                       ts: float) -> None:
    """Ajoute un message envoye a la conversation avec `pseudo`. Le cap
    a PHONE_MAX_MESSAGES (envoyes + recus combines) est applique via
    _phone_trim_convo apres l'ajout. Modifie en place."""
    convo = _phone_get_convo(messages, pseudo)
    convo["sent"].append({"ts": ts, "body": body})
    _phone_trim_convo(convo)


def _phone_append_received(messages: dict, pseudo: str, body: str,
                           ts: float) -> None:
    """Ajoute un message recu a la conversation avec `pseudo`. Cap global
    via _phone_trim_convo (envoyes + recus combines). Incremente le
    compteur unread. Modifie en place."""
    convo = _phone_get_convo(messages, pseudo)
    convo["received"].append({"ts": ts, "body": body})
    _phone_trim_convo(convo)
    convo["unread"] = int(convo.get("unread", 0)) + 1


def _phone_mark_read(messages: dict, pseudo: str) -> bool:
    """Marque la conversation avec `pseudo` comme lue (unread = 0).
    Retourne True si quelque chose a change (donc qu'il faut sauvegarder)."""
    convo = messages.get("conversations", {}).get(pseudo)
    if convo is None:
        return False
    if int(convo.get("unread", 0)) == 0:
        return False
    convo["unread"] = 0
    return True


def _phone_set_draft(messages: dict, pseudo: str, draft: str) -> None:
    """Met a jour le brouillon de la conversation avec `pseudo`. Modifie
    en place. La conversation est creee si elle n'existait pas (pour ne
    pas perdre un brouillon vers un contact a qui on n'a jamais parle)."""
    convo = _phone_get_convo(messages, pseudo)
    convo["draft"] = draft or ""


def _phone_merge_messages(messages: dict, pseudo: str):
    """Construit la liste chronologique (envoyes + recus melanges, tries
    par ts croissant) pour affichage. Chaque element : (ts, body, is_me)
    ou is_me=True si c'est un message que j'ai envoye."""
    convo = messages.get("conversations", {}).get(pseudo)
    if convo is None:
        return []
    items = []
    for m in convo.get("sent", []):
        items.append((float(m.get("ts", 0.0)), m.get("body", ""), True))
    for m in convo.get("received", []):
        items.append((float(m.get("ts", 0.0)), m.get("body", ""), False))
    items.sort(key=lambda x: x[0])
    return items


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
    # v0.2 : timestamp monotonic de la derniere position locale OCR.
    # Utilise par le masque DisplayInfo pour determiner si l'OCR a lu
    # une position recente. 0.0 = jamais lu encore -> mask cache.
    if not hasattr(state, "my_pos_ts"):
        state.my_pos_ts = 0.0
    # v0.2 alpha 055 : flag controle par la machine d'etat clavier qui
    # detecte la mobiglass (F1/F2/F11) et le menu options (Echap). Quand
    # True, le masque DisplayInfo est cache en plus des autres conditions
    # (case cochee, OCR frais, etc.). Resync auto sur changement de
    # position OCR (le joueur a la main sur le perso = mobiglass fermee).
    if not hasattr(state, "mask_force_hidden"):
        state.mask_force_hidden = False
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
        my_pos_ts: float = 0.0  # v0.2 : timestamp OCR pour mask DisplayInfo
        mask_force_hidden: bool = False  # v0.2 alpha 055 : machine etat clavier
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
    # v0.2 alpha 029 : un joueur du canal vocal courant a declenche un son
    # du soundboard. Args : (sound_id, sender_name). Sera relie a
    # MainWindow._play_soundboard_local via QueuedConnection (thread-safe
    # cross-thread : on est dans NetWorker, le slot tourne dans le thread Qt
    # main qui possede audio_io et le cache des sons).
    sig_soundboard_play = Signal(str, str)        # sound_id, sender_name
    # v0.2 alpha 035 : signal emis quand le serveur push de nouvelles
    # permissions sur mon profil (welcome ou my_profile). Args :
    # (perm_key, value). MainWindow ecoute et adapte l'UI (montre/cache
    # la section soundboard, le bouton Soundboard, etc.).
    sig_my_perm_changed = Signal(str, bool)       # perm_key, value
    # CircusPhone (Feature 4, D1) : signaux du cycle de vie d'appel.
    # Tous emis depuis le thread worker WS, relies a des slots
    # MainWindow._on_phone_* via QueuedConnection (thread-safe).
    sig_phone_ringing  = Signal(str, str)         # call_id, target
    sig_phone_incoming = Signal(str, str)         # call_id, caller
    sig_phone_accepted = Signal(str, str, str)    # call_id, caller, callee
    sig_phone_declined = Signal(str)              # call_id
    sig_phone_busy     = Signal(str, str)         # target, cause
    sig_phone_missed   = Signal(str, str, str)    # call_id, caller, callee
    sig_phone_ended    = Signal(str, str)         # call_id, reason
    # CircusPhone (D4 etape 3) : reception d'un MP texte. Relaye au thread
    # Qt via slot _on_phone_message_received dans MainWindow.
    sig_phone_message_received = Signal(str, str, float)  # sender, body, ts
    sig_phone_image_received = Signal(str, str, float)    # sender, b64, ts
    sig_phone_server_msg = Signal(dict)   # message mp_* (multijoueur) -> overlay
    # [D5] Reponse a un profile_photo_request. Champs : target, status,
    # hash, data_b64. status est l'un de : "ok", "unchanged", "none".
    sig_profile_photo_response = Signal(str, str, str, str)
    #                                  target, status, hash, data_b64

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

        # [P1 - TLS] Connexion CHIFFREE en wss://.
        # Le serveur positions utilise un certificat auto-signe (genere
        # automatiquement). build_client_ssl_context_insecure() construit
        # un contexte SSL qui accepte ce cert sans verifier l'identite :
        # connexion chiffree mais pas d'authentification stricte. C'est
        # acceptable car l'auth client se fait ensuite via le token dans
        # le message "join" (compare_digest cote serveur, cf [P1]).
        from circusvoip_security import build_client_ssl_context_insecure
        uri = f"wss://{server_ip}:{SERVER_PORT}"
        _ssl_ctx = build_client_ssl_context_insecure()
        self.sig_log.emit(f"[NET] Connexion a {uri} (nom={name})...")
        try:
            async with websockets.connect(uri, ssl=_ssl_ctx) as ws:
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

        # Multijoueur (mp_*) et scores partages (hs_*) : vers l'overlay
        # (app courante, qui expose handle_server_msg).
        if isinstance(msg_type, str) and (msg_type.startswith("mp_")
                                          or msg_type.startswith("hs_")):
            self.sig_phone_server_msg.emit(data)
            return

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
                # [P4 - auth partagee] Recuperer le ticket audio emis par
                # le serveur positions. On le stocke dans state pour que
                # _audio_ws_loop (dans core.py) puisse le presenter au
                # serveur audio. Rafraichi a chaque welcome : si on se
                # reconnecte, on obtient un nouveau ticket et l'ancien est
                # ecrase (l'ancien ne vaut plus rien cote serveur).
                state.audio_ticket = data.get("audio_ticket", "") or ""
                # v0.2 alpha 035 : permissions du profil. Au welcome,
                # le serveur envoie False par defaut (pas encore de profil
                # assigne). Stocke pour usage UI + emet le signal pour
                # mise a jour immediate (cacher la section soundboard).
                sb_allowed = bool(data.get("soundboard_allowed", False))
                state.my_profile_soundboard_allowed = sb_allowed
                self.sig_my_perm_changed.emit("soundboard_allowed", sb_allowed)
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
                # v0.2 alpha 035 : permissions associees au nouveau profil
                # (envoyees par le serveur dans le meme message). Si pas
                # de profil, toutes les permissions sont False.
                sb_allowed = bool(data.get("soundboard_allowed", False))
                state.my_profile_soundboard_allowed = sb_allowed
                self.sig_log.emit(
                    f"[NET] mon profil -> {new_prof or '(aucun)'} "
                    f"(soundboard={'OUI' if sb_allowed else 'NON'})"
                )
                self.sig_channels_changed.emit()
                self.sig_my_perm_changed.emit("soundboard_allowed", sb_allowed)
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

        if msg_type == "soundboard_play":
            # v0.2 alpha 029/031 : un joueur du canal vocal a declenche un
            # son du soundboard. NetWorker tourne dans son propre thread,
            # _play_soundboard_local est sur MainWindow (thread Qt main).
            # On emet un signal Qt -> traverse les threads via
            # QueuedConnection -> _play_soundboard_local est invoque dans
            # le thread Qt main, qui possede state.audio_io et le cache.
            try:
                sound_id = data.get("sound_id")
                sender   = data.get("name") or ""
                if isinstance(sound_id, str) and sound_id:
                    self.sig_soundboard_play.emit(sound_id, sender)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[SOUNDBOARD] play recv KO : {e}")
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

        # ─────────────────────────────────────────────
        #  CircusPhone (Feature 4, D1) : cycle de vie d'appel
        # ─────────────────────────────────────────────
        # On relaie chaque message au thread Qt via un signal dedie.
        # MainWindow porte l'etat d'appel et l'UI de la page Phone Debug.
        if msg_type == "phone_call_ringing":
            self.sig_phone_ringing.emit(
                data.get("call_id") or "", data.get("target") or "")
            return
        if msg_type == "phone_call_incoming":
            self.sig_phone_incoming.emit(
                data.get("call_id") or "", data.get("caller") or "")
            return
        if msg_type == "phone_call_accepted":
            self.sig_phone_accepted.emit(
                data.get("call_id") or "", data.get("caller") or "",
                data.get("callee") or "")
            return
        if msg_type == "phone_call_declined":
            self.sig_phone_declined.emit(data.get("call_id") or "")
            return
        if msg_type == "phone_call_busy":
            self.sig_phone_busy.emit(
                data.get("target") or "", data.get("cause") or "")
            return
        if msg_type == "phone_call_missed":
            self.sig_phone_missed.emit(
                data.get("call_id") or "", data.get("caller") or "",
                data.get("callee") or "")
            return
        if msg_type == "phone_call_ended":
            self.sig_phone_ended.emit(
                data.get("call_id") or "", data.get("reason") or "")
            return
        # CircusPhone D4b : on est un voisin du proprietaire d'un HP.
        # On est maintenant autorise a entendre les trames 0x03 venant
        # du peer 'peer' (la voix de l'autre partie de l'appel).
        if msg_type == "phone_hp_active":
            peer = data.get("peer") or ""
            owner = data.get("owner") or ""
            if peer and owner:
                state.hp_speakers_allowed[peer] = owner
                self.sig_log.emit(
                    f"[HP] J'entends maintenant {peer} en HP (owner={owner})"
                )
            return
        # CircusPhone D4b : autorisation revoquee (le proprietaire a
        # eteint son HP, ou j'ai quitte le rayon 5m, ou l'appel a fini).
        if msg_type == "phone_hp_inactive":
            peer = data.get("peer") or ""
            owner = data.get("owner") or ""
            # On retire l'entree uniquement si elle correspond a l'owner
            # qui notifie : si plusieurs HP m'autorisaient sur le meme
            # peer (cas tordu), on ne casse pas les autres.
            cur_owner = state.hp_speakers_allowed.get(peer)
            if cur_owner == owner:
                state.hp_speakers_allowed.pop(peer, None)
                self.sig_log.emit(
                    f"[HP] Je n'entends plus {peer} en HP (owner={owner})"
                )
            return
        # CircusPhone D4b : je suis le peer d'un proprietaire HP. La liste
        # des voisins du proprietaire (dont je peux entendre la prox 0x00)
        # vient d'etre mise a jour par le serveur. On la remplace en bloc.
        if msg_type == "phone_hp_neighbors_update":
            owner = data.get("owner") or ""
            new_neighbors = data.get("neighbors") or []
            if not owner:
                return
            # Retirer toutes les entrees liees a cet owner (peut-etre des
            # voisins qui ne sont plus voisins), puis ajouter les nouveaux.
            for nb in list(state.hp_proxies_allowed.keys()):
                if state.hp_proxies_allowed.get(nb) == owner:
                    state.hp_proxies_allowed.pop(nb, None)
            for nb in new_neighbors:
                if isinstance(nb, str) and nb:
                    state.hp_proxies_allowed[nb] = owner
            self.sig_log.emit(
                f"[HP] Voisins de {owner} mis a jour : "
                f"{len(state.hp_proxies_allowed)} pseudos autorises"
            )
            return
        # CircusPhone (D4 etape 3) : un MP texte est arrive.
        if msg_type == "phone_message_received":
            sender = data.get("sender") or ""
            body   = data.get("body") or ""
            try:
                ts = float(data.get("ts") or 0.0)
            except Exception:
                ts = 0.0
            self.sig_phone_message_received.emit(sender, body, ts)
            return

        # Screenshots dans les MP : une image (base64) est arrivee.
        if msg_type == "phone_image_received":
            sender = data.get("sender") or ""
            b64    = data.get("data") or ""
            try:
                ts = float(data.get("ts") or 0.0)
            except Exception:
                ts = 0.0
            self.sig_phone_image_received.emit(sender, b64, ts)
            return

        # [D5] Reponse a une demande de photo de profil.
        if msg_type == "profile_photo_response":
            target = data.get("target") or ""
            status = data.get("status") or ""
            new_hash = data.get("hash") or ""
            data_b64 = data.get("data_b64") or ""
            self.sig_profile_photo_response.emit(
                target, status, new_hash, data_b64
            )
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
# Masque DisplayInfo (v0.2, feature 3)
# ======================================================================
# Overlay topmost qui dessine un rectangle opaque noir par-dessus la
# zone DisplayInfo de Star Citizen (le HUD qui affiche le nom de la
# zone / planete / station). Certains joueurs (notamment streamers)
# le trouvent imposant ou veulent eviter de leak la localisation.
#
# Le mask est :
#   - Click-through (Qt.WindowTransparentForInput) : les clics passent
#     au jeu en-dessous.
#   - Topmost (Qt.WindowStaysOnTopHint) : reste visible meme en
#     plein-ecran fenetre du jeu.
#   - Frameless (Qt.FramelessWindowHint) : pas de barre de titre.
#   - Place sur l'ecran qui contient la zone OCR (pas forcement
#     l'ecran principal Windows).
#
# Position et taille : derivees de la resolution de l'ecran cible a
# partir de la reference 4K ci-dessous. Le rectangle est colle au
# bord droit de l'ecran (cf. comportement du DisplayInfo SC sur
# ultrawide), et scale proportionnellement a la largeur/hauteur ecran.
#
# Affichage conditionnel : visible uniquement si
#   - la case dans Parametres > OCR (avance) est cochee, ET
#   - state.my_pos_ts a moins de DISPLAYINFO_MASK_STALE_S secondes
#     (= l'OCR a lu une position recente, donc le joueur est en jeu
#     plutot que sur le bureau Windows / dans le menu).
# Un QTimer 500ms cote MainWindow check ces conditions.

# Reference 4K (3840x2160). Mesuree sur ecran 4K standard, position de
# la zone DisplayInfo Star Citizen.
#   x_4k       : px depuis le bord gauche de l'ecran (= 2496 -> reste
#                1344 px jusqu'au bord droit, donc colle a droite)
#   y_4k       : px depuis le bord haut (= 0 -> colle en haut)
#   width_4k   : largeur en px (= 1344)
#   height_4k  : hauteur en px (= 500)
# Pour les autres resolutions, on scale :
#   - largeur : width_4k * (screen_w / 3840)
#   - hauteur : height_4k * (screen_h / 2160)
#   - x       : screen_w - largeur calculee (colle au bord droit, donc
#               independant de la position x_4k -> c'est volontaire,
#               cf. comportement HUD SC en ultrawide)
#   - y       : y_4k * (screen_h / 2160) (proportionnel a la hauteur)
DISPLAYINFO_MASK_REF_4K = {
    "x":      2496,
    "y":      0,
    "width":  1344,
    "height": 500,
}
DISPLAYINFO_MASK_REF_SCREEN_W = 3840
DISPLAYINFO_MASK_REF_SCREEN_H = 2160

# Apparence du rectangle (v0.2 alpha 005, retour utilisateur "le bloc noir
# opaque etait trop visible et flagrant"). On garde un rectangle simple
# (pas de flou ni de detection de pixels du texte) mais on le rend
# semi-transparent avec coins arrondis pour qu'il se fonde mieux dans
# l'image du jeu.
#   DISPLAYINFO_MASK_OPACITY : 0.0 (totalement transparent, invisible)
#                              a 1.0 (totalement opaque, comme avant).
#                              0.5 = compromis : le texte est nettement
#                              attenue mais le decor du jeu en dessous
#                              reste devine, evite l'effet "gros pate noir".
#   DISPLAYINFO_MASK_RADIUS  : rayon des coins arrondis en pixels.
#                              Calcule en pixels logiques de l'ecran cible.
#                              0 = coins droits comme avant.
#                              10 = leger arrondi, juste pour adoucir.
DISPLAYINFO_MASK_OPACITY = 0.5
DISPLAYINFO_MASK_RADIUS  = 10

# Mode "smart" du masque (v0.2 alpha 006). Au lieu de dessiner un
# rectangle uniforme, on capture la zone DisplayInfo et on detecte les
# pixels appartenant au texte du HUD. On ne masque QUE ces pixels-la,
# en les remplacant par la couleur moyenne du decor environnant.
# Resultat : le texte disparait, mais le decor du jeu reste totalement
# visible entre les lignes et les caracteres.
#
# Detection adaptative (v0.2 alpha 012) : remplace les seuils RGB fixes
# qui souffraient de 2 problemes :
#   1. Certains textes (gris terne, contours antialiases, couleurs hors
#      criteres) passaient sous les seuils -> bouts de texte visibles.
#   2. Sur une frame avec une luminosite globale differente (fondu,
#      changement de scene), aucun pixel ne passait les seuils ->
#      frame entiere ou le masque ne cachait rien.
#
# Methode : pour chaque pixel, on calcule la moyenne de luminosite dans
# un grand voisinage (fenetre WINDOW_PX x WINDOW_PX). Un pixel est
# considere "texte" si sa luminosite est BRIGHT_DIFF plus elevee que
# cette moyenne locale. C'est exactement la maniere dont un humain
# detecte le texte : ce qui ressort du fond, peu importe la couleur.
#
#   DISPLAYINFO_TEXT_BRIGHT_DIFF : seuil de difference de luminosite
#                                  par rapport a la moyenne locale.
#                                  20 = tres agressif (attrape tout, y
#                                       compris faux positifs sur
#                                       reflets / lumieres).
#                                  30 = compromis recommande.
#                                  50 = selectif, ne loupe pas les
#                                       textes brillants mais peut
#                                       louper le gris fonce.
#   DISPLAYINFO_TEXT_WINDOW_PX   : taille du voisinage pour calcul de
#                                  la moyenne locale. Doit etre plus
#                                  grand que l'epaisseur d'un caractere
#                                  (sinon le caractere lui-meme tire
#                                  la moyenne et echappe la detection).
#                                  21 px = bon defaut pour le texte du
#                                  HUD SC (lignes hautes de ~15-20 px).
DISPLAYINFO_TEXT_BRIGHT_DIFF = 15
DISPLAYINFO_TEXT_WINDOW_PX   = 51

# Dilate du masque texte : on etend le masque de N pixels autour de chaque
# pixel detecte pour bien couvrir les bordures antialiasees des caracteres
# (qui sont moins brillantes que le centre du trait et ne passent pas le
# seuil). 1 ou 2 px suffit en general.
DISPLAYINFO_TEXT_DILATE_PX = 3

# Taille maximale d'un cluster de pixels detectes avant filtrage
# (v0.2 alpha 038). Pour rejeter les zones lumineuses du JEU (lampes,
# voyants, reflets, etoiles, parties eclairees d'un vaisseau) qui sont
# detectees a tort comme du texte HUD par le critere de luminosite
# locale. Un caractere du HUD a cette resolution fait typiquement
# entre 5 et 30 pixels une fois detecte. Un cluster nettement plus gros
# (lampe, blob lumineux, gros voyant) est presque toujours un element
# du jeu et NON du texte.
#
# Calibrage :
#   - 60 : preserve tous les caracteres standards, rejette les blobs
#          de taille moyenne+.
#   - 100 : plus large, garde aussi quelques gros caracteres (chiffres
#           gras, certains glyphes), rejette les vrais gros blobs.
#   - 200 : seulement les enormes blobs (lampe pleine ecran) sont rejetes.
# Defaut prudent a 80 px : si un caractere depasse cette taille, c'est
# probablement deja un faux positif sur un blob lumineux.
DISPLAYINFO_TEXT_MAX_BLOB_PX = 80

# Voisinage utilise pour calculer la couleur moyenne du decor a la place
# des pixels texte. On prend les pixels NON-texte dans une fenetre de
# (2*N+1)x(2*N+1) autour de chaque pixel texte et on moyenne. Plus N est
# grand, plus le remplacement est lisse mais flou (et plus c'est cher).
# v0.2 alpha 047 : monte de 6 a 16 pour eviter les bandes noires sur
# les lignes denses du HUD. Avec N=6, le voisinage 13x13 ne capturait
# pas assez de pixels "decor" pour les zones ou une ligne du HUD est
# entierement remplie de texte (= la moyenne tombait a quasi-noir).
# Avec N=16, on echantillonne 33x33 px (= au-dessus et en-dessous d'une
# ligne de texte, on trouve toujours du decor). Cout CPU negligeable
# car le boxFilter est applique en demi-resolution.
DISPLAYINFO_DECOR_RADIUS_PX = 16

# Periode de rafraichissement du masque smart (ms). 16ms = ~60 FPS,
# match la cadence du DWM Windows et donc la fluidite de SC.
# A plus haute frequence, on ne gagne plus rien visuellement.
# Cout estime ~25-30 % CPU d'un coeur a 5 FPS pour une zone 1344x500,
# donc ~300% (= 3 coeurs equivalents) a 60 FPS. Reste OK sur PC moderne.
DISPLAYINFO_SMART_PERIOD_MS = 16

# Memoire temporelle du masque (v0.2 alpha 007). Le texte du HUD change a
# chaque frame (FPS, coords, timers qui defilent), donc la detection des
# pixels du texte change aussi a chaque frame -> clignotement visible.
# Pour stabiliser : on garde en memoire les N derniers masques detectes et
# on les combine via OR logique. Un pixel reste masque tant qu'il a ete
# detecte "texte" dans au moins UN des N derniers frames.
# Compromis :
#   N=1  : comportement initial, clignotement maximal.
#   N=5  : 1 seconde de memoire a 5 FPS, bon compromis stabilite/etalement.
#   N=10 : 2 secondes de memoire, plus stable mais le masque s'etale plus.
#   N=20 : tres stable mais converge vers des bandes pleines.
DISPLAYINFO_TEXT_HISTORY_FRAMES = 5

# Delai (secondes) au-dela duquel state.my_pos_ts est considere comme
# perime -> le mask se cache (le joueur a probablement quitte le jeu,
# alt-tab, ou est sur un ecran de chargement). Mis a 20s pour tolerer
# les interruptions courtes d'OCR (boussoles, menus rapides) sans faire
# clignoter le mask.
DISPLAYINFO_MASK_STALE_S = 20.0

# Periode du timer de check (ms). 500ms = check 2x/sec, suffisant pour
# faire apparaitre/disparaitre le mask de maniere fluide sans charger
# la boucle Qt.
DISPLAYINFO_MASK_CHECK_MS = 500

# ----------------------------------------------------------------------
# FLAGS DEBUG MASQUE (v0.2 alpha 016)
# ----------------------------------------------------------------------
# Permettent d'isoler le masque de l'OCR pour reproduire/diagnostiquer
# des problemes (clignotement, frames vides). En production : laisser
# les valeurs par defaut.
#
# DEBUG_ENABLE_OCR :
#   True  (defaut) = OCR fonctionne normalement
#   False          = l'OCR n'est PAS demarre du tout. Le client tourne
#                    en mode "VOIP only", sans lecture de position SC,
#                    sans capture MSS de la zone OCR, sans callbacks
#                    pre/post sur le masque. Permet de tester si le
#                    clignotement vient d'une interaction OCR ↔ masque.
#                    Effet de bord : state.my_pos reste a None ->
#                    l'overlay des autres joueurs / proximite ne marche
#                    plus, mais on s'en fout pour le debug du masque.
#
# DEBUG_MASK_BYPASS_OCR_CHECK :
#   False (defaut) = le masque ne s'affiche que si l'OCR a lu une
#                    position recente (state.my_pos_ts < 20s).
#   True           = le masque s'affiche des que la case est cochee,
#                    sans verifier l'etat de l'OCR. Necessaire si
#                    DEBUG_ENABLE_OCR=False (sinon le masque ne s'affiche
#                    jamais, faute de position OCR).
#
# Trois configs de test recommandees :
#   1. Normal              : DEBUG_ENABLE_OCR=True,  BYPASS=False
#   2. Sans OCR du tout    : DEBUG_ENABLE_OCR=False, BYPASS=True
#   3. OCR sans gate       : DEBUG_ENABLE_OCR=True,  BYPASS=True
DEBUG_ENABLE_OCR             = True
DEBUG_MASK_BYPASS_OCR_CHECK  = False


def _compute_displayinfo_mask_rect(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """Calcule (x, y, w, h) du rectangle masque DisplayInfo pour une
    resolution d'ecran donnee, en pixels PHYSIQUES de cet ecran.

    Methode : on scale la largeur a partir de la largeur ecran et la
    hauteur a partir de la hauteur ecran (par rapport a la reference 4K).
    Le rectangle est ensuite colle au bord droit en calculant
    x = screen_w - largeur_calculee. Le y est scale proportionnellement
    a la hauteur (= 0 puisque y_4k = 0).

    Retourne 4 entiers : (x_left, y_top, width, height).
    """
    if screen_w <= 0 or screen_h <= 0:
        return (0, 0, 0, 0)
    ratio_w = screen_w / float(DISPLAYINFO_MASK_REF_SCREEN_W)
    ratio_h = screen_h / float(DISPLAYINFO_MASK_REF_SCREEN_H)
    w = int(round(DISPLAYINFO_MASK_REF_4K["width"]  * ratio_w))
    h = int(round(DISPLAYINFO_MASK_REF_4K["height"] * ratio_h))
    y = int(round(DISPLAYINFO_MASK_REF_4K["y"]      * ratio_h))
    # Colle au bord droit : x = screen_w - w. Si w > screen_w (cas
    # theorique improbable), on clamp a 0 pour eviter un x negatif.
    x = max(0, screen_w - w)
    return (x, y, w, h)


# ----------------------------------------------------------------------
# Machine d'etat clavier qui suit les ouvertures de mobiglass / menu
# options dans Star Citizen, pour cacher le masque DisplayInfo quand
# le HUD n'est plus visible derriere une de ces interfaces (v0.2 alpha 055).
# ----------------------------------------------------------------------
# Probleme resolu : avant cette classe, le masque ne se cachait que
# lorsque l'OCR n'avait pas lu de position depuis 20s (DISPLAYINFO_MASK_STALE_S).
# Quand le joueur ouvre la mobiglass ou le menu options, le HUD
# DisplayInfo disparait, mais l'OCR continue parfois a "lire" via la
# derniere position connue cote sc_ocr -> my_pos_ts reste recent -> le
# masque continue de couvrir une zone vide, voire cache un bout de la
# mobiglass ou du menu.
#
# Solution : detecter les touches F1/F2/F11 (mobiglass) et Echap (menu)
# au niveau global via pynput. Tenir une petite machine d'etat (IDLE /
# MOBIGLASS / MENU_OPTIONS) qui dit si le HUD est presumablement cache.
# Resync sur changement de position OCR : si la position bouge, c'est
# que le joueur a la main sur le perso -> mobiglass forcement fermee
# -> on revient en IDLE (filet de securite contre desync, par ex. si
# l'utilisateur ferme la mobiglass au clic souris).
#
# Regles de transition (cf. discussion utilisateur) :
#   IDLE :
#     - F1/F2/F11 -> MOBIGLASS (memorise la touche)
#     - ECHAP     -> MENU_OPTIONS
#   MOBIGLASS (touche ouvrante memorisee = K_open) :
#     - touche == K_open  -> IDLE
#     - F1 (specialement) -> IDLE (F1 ferme toujours la mobiglass meme
#                            si ouverte par F2/F11)
#     - F2/F11 autres     -> reste MOBIGLASS, K_open est remplacee
#     - ECHAP             -> IDLE
#     - position change   -> IDLE
#   MENU_OPTIONS :
#     - ECHAP             -> IDLE
#     - position change   -> IDLE
#
# Touches en dur (F1/F2/F11/escape) pour cette premiere version.
# Si SC remap les controles, on rendra ca configurable ulterieurement.
class _DisplayInfoMaskKeyTracker:
    """Listener pynput dedie au masque DisplayInfo. Suit les ouvertures de
    mobiglass (F1/F2/F11) et menu options (Echap), met a jour
    state.mask_force_hidden, et appelle un callback quand l'etat change
    (pour rafraichir immediatement le masque sans attendre le timer 500ms).

    Tourne en thread pynput (daemon). start()/stop() controlent le cycle
    de vie. Les transitions d'etat sont protegees par un lock car le
    listener pynput et le timer Qt peuvent y acceder en concurrence
    (resync sur position).
    """

    STATE_IDLE         = "IDLE"
    STATE_MOBIGLASS    = "MOBIGLASS"
    STATE_MENU_OPTIONS = "MENU_OPTIONS"

    # Touches surveillees. Format = sortie de _normalize_pynput_key.
    KEYS_MOBIGLASS = ("f1", "f2", "f11")
    KEY_ESCAPE     = "esc"
    KEY_F1         = "f1"  # la touche speciale qui ferme toujours

    def __init__(self, on_state_changed=None):
        # Callback appele quand state.mask_force_hidden change. Le caller
        # peut s'en servir pour declencher un refresh immediat du masque.
        # Appele depuis le thread pynput : il doit etre thread-safe (en
        # pratique on emet un signal Qt pour rebondir sur le thread main).
        self._on_state_changed = on_state_changed
        self._lock = threading.Lock()
        self._state = self.STATE_IDLE
        self._mobiglass_open_key: str | None = None  # touche qui a ouvert
        self._kb_listener = None
        # Derniere position observee pour la detection de changement.
        # Initialise a None : on prend la premiere comme reference.
        self._last_pos: tuple[float, float, float] | None = None
        # Seuil de detection de changement de position (metres). Choisi
        # par l'utilisateur : 0.1m absorbe le bruit OCR sans louper un
        # vrai mouvement.
        self._pos_threshold_m = 0.1

    def start(self) -> None:
        """Demarre le listener pynput dans un thread daemon."""
        if self._kb_listener is not None:
            return
        try:
            from pynput import keyboard as kb
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK KEYS] pynput indisponible : {e}"
                    )
                except Exception:
                    pass
            return

        def _on_press(key):
            try:
                # Reutilise le normaliseur du core pour rester coherent
                # avec les autres listeners (case-insensitive, gestion
                # des touches speciales et numpad).
                if _CORE_AVAILABLE:
                    norm = _core._normalize_pynput_key(key)
                else:
                    # Fallback minimal si core absent : on prend juste
                    # key.name s'il existe (suffit pour F1/F2/F11/esc).
                    norm = getattr(key, "name", None)
                    if norm:
                        norm = norm.lower()
                if not norm:
                    return
                self._handle_key(norm)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[MASK KEYS] on_press exception : {e}"
                        )
                    except Exception:
                        pass

        try:
            self._kb_listener = kb.Listener(on_press=_on_press)
            self._kb_listener.daemon = True
            self._kb_listener.start()
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        "[MASK KEYS] listener pynput demarre "
                        "(F1/F2/F11/Esc)"
                    )
                except Exception:
                    pass
        except Exception as e:
            self._kb_listener = None
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK KEYS] start KO : {e}"
                    )
                except Exception:
                    pass

    def stop(self) -> None:
        """Arrete le listener pynput (utile au shutdown du client)."""
        if self._kb_listener is None:
            return
        try:
            self._kb_listener.stop()
        except Exception:
            pass
        self._kb_listener = None

    def _handle_key(self, key: str) -> None:
        """Applique les regles de la machine d'etat. Appele depuis le
        thread pynput. Protege par le lock car la fonction
        check_position_change() peut tourner en parallele depuis le
        thread Qt."""
        with self._lock:
            old_force_hidden = (self._state != self.STATE_IDLE)
            if self._state == self.STATE_IDLE:
                if key in self.KEYS_MOBIGLASS:
                    self._state = self.STATE_MOBIGLASS
                    self._mobiglass_open_key = key
                elif key == self.KEY_ESCAPE:
                    self._state = self.STATE_MENU_OPTIONS
                    self._mobiglass_open_key = None
                else:
                    return  # touche non surveillee
            elif self._state == self.STATE_MOBIGLASS:
                if key == self._mobiglass_open_key:
                    # Re-appui sur la touche d'ouverture -> ferme
                    self._state = self.STATE_IDLE
                    self._mobiglass_open_key = None
                elif key == self.KEY_F1:
                    # F1 ferme toujours la mobiglass, meme si ouverte
                    # par F2 ou F11 (regle utilisateur).
                    self._state = self.STATE_IDLE
                    self._mobiglass_open_key = None
                elif key in self.KEYS_MOBIGLASS:
                    # F2 ou F11 alors que la mobiglass est ouverte par
                    # une autre touche : on remplace la touche memorisee
                    # (le re-appui sur cette nouvelle touche fermera).
                    self._mobiglass_open_key = key
                elif key == self.KEY_ESCAPE:
                    # Echap ferme la mobiglass
                    self._state = self.STATE_IDLE
                    self._mobiglass_open_key = None
                else:
                    return
            elif self._state == self.STATE_MENU_OPTIONS:
                if key == self.KEY_ESCAPE:
                    self._state = self.STATE_IDLE
                else:
                    return
            new_force_hidden = (self._state != self.STATE_IDLE)
            changed = (new_force_hidden != old_force_hidden)
            # Reset de la reference de position quand on entre dans un
            # etat "cache" : la prochaine position lue sera la reference
            # pour detecter un mouvement. Sinon on risquerait de resync
            # immediatement avec une vieille position qui differe.
            if new_force_hidden and not old_force_hidden:
                self._last_pos = None

        # Mettre a jour le flag global hors du lock (l'ecriture d'un bool
        # est atomique en Python, pas besoin de synchroniser).
        try:
            state.mask_force_hidden = new_force_hidden
        except Exception:
            pass

        if changed and self._on_state_changed is not None:
            try:
                self._on_state_changed()
            except Exception:
                pass

        if _CORE_AVAILABLE:
            try:
                _core._dbg_log(
                    f"[MASK KEYS] key={key} -> state={self._state} "
                    f"open_key={self._mobiglass_open_key} "
                    f"hidden={new_force_hidden}"
                )
            except Exception:
                pass

    def check_position_change(self, pos: dict | None) -> None:
        """Verifie si la position OCR a change depuis le dernier appel.
        Si oui ET qu'on est en MOBIGLASS ou MENU_OPTIONS, on resync en
        IDLE (le joueur a forcement la main sur son perso pour qu'il
        bouge). Appele depuis le timer Qt main thread du masque.

        pos peut etre None (pas encore de position OCR) : dans ce cas
        on ne fait rien."""
        if not isinstance(pos, dict):
            return
        try:
            x = float(pos.get("x", 0.0))
            y = float(pos.get("y", 0.0))
            z = float(pos.get("z", 0.0))
        except Exception:
            return
        resync = False
        moved_distance = 0.0  # rempli si resync, pour le log
        with self._lock:
            if self._state == self.STATE_IDLE:
                # En IDLE on ne fait rien (le masque n'est pas force cache),
                # mais on garde la position courante comme reference pour
                # plus tard.
                self._last_pos = (x, y, z)
                return
            if self._last_pos is None:
                # Premiere position observee depuis l'entree dans l'etat
                # cache : on l'enregistre comme reference, sans resync.
                self._last_pos = (x, y, z)
                return
            dx = x - self._last_pos[0]
            dy = y - self._last_pos[1]
            dz = z - self._last_pos[2]
            # Distance euclidienne. On compare au seuil au carre pour
            # eviter le sqrt (gain marginal mais cout 0).
            dist_sq = dx*dx + dy*dy + dz*dz
            thr_sq = self._pos_threshold_m * self._pos_threshold_m
            if dist_sq < thr_sq:
                # Pas de mouvement significatif, on garde l'etat.
                # La reference n'est PAS mise a jour : sinon un drift
                # lent (un pas par cycle, en dessous du seuil) finirait
                # par cumuler sans jamais resync.
                return
            # Mouvement detecte -> resync forcee en IDLE.
            self._state = self.STATE_IDLE
            self._mobiglass_open_key = None
            self._last_pos = (x, y, z)
            resync = True
            moved_distance = dist_sq ** 0.5

        if resync:
            try:
                state.mask_force_hidden = False
            except Exception:
                pass
            # NOTE : on N'APPELLE PAS self._on_state_changed() ici.
            # check_position_change est appelee depuis le thread Qt main
            # (depuis _update_displayinfo_mask). Le callback est connecte
            # au signal _sig_mask_state_changed qui en AutoConnection
            # depuis le main thread vers un slot main thread devient une
            # DirectConnection -> appel SYNCHRONE recursif vers
            # _update_displayinfo_mask, qui voit alors une etape
            # intermediaire de son propre etat (race) et peut creer 2
            # instances de la fenetre masque (-> conflit bettercam DXGI
            # observe en alpha 055, log "bettercam grab KO" en boucle).
            # Le caller relit state.mask_force_hidden apres notre appel,
            # ce qui suffit pour reagir au resync sans recursion.
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        "[MASK KEYS] position change detected "
                        f"(d={moved_distance:.2f}m > {self._pos_threshold_m}m) "
                        "-> resync IDLE"
                    )
                except Exception:
                    pass


class _DisplayInfoMaskWorker(QObject):
    """Worker thread qui capture la zone DisplayInfo via MSS, detecte les
    pixels du texte HUD, calcule un remplacement par la couleur du decor
    moyen autour, et emit le resultat sous forme d'un QImage RGBA pret a
    afficher.

    Le QImage retourne fait la taille de la zone du masque (en pixels
    PHYSIQUES de l'ecran cible). Les pixels alpha=0 sont totalement
    transparents (laissent passer le jeu), les pixels alpha>0 sont la
    couleur du decor moyen (qui remplace visuellement le texte detecte).

    Signal :
        sig_image_ready(QImage) : nouveau masque pret a redessiner.

    Slot :
        request_stop() : demande l'arret propre de la boucle.

    La capture mss est creee dans ce thread (thread-local, mss n'est pas
    partageable entre threads). Le traitement numpy + cv2 est aussi fait
    ici, donc le thread Qt n'est pas bloque.
    """

    sig_image_ready = Signal(object)  # QImage, mais object pour souplesse

    def __init__(self, owner_window=None):
        super().__init__()
        # Cible : (left, top, width, height) en pixels physiques. Mis a
        # jour par set_region() depuis le thread Qt principal. None tant
        # que la geometrie n'a pas ete calculee (= avant le premier show).
        self._region: dict | None = None
        # Flag d'arret coopératif (lu par la boucle a chaque iteration).
        self._stop = False
        # Lock pour la lecture/ecriture de _region depuis le thread Qt
        # (set_region) et le thread worker (run).
        import threading as _th
        self._lock = _th.Lock()
        # owner_window : reserve pour un usage futur (hide/show de la
        # fenetre owner pendant les captures du worker, anti-larsen).
        # Actuellement non utilise apres simplification alpha 012 :
        # les slots _slot_hide_for_worker / _slot_show_after_worker
        # n'existent pas dans la fenetre, donc les appels invokeMethod
        # echouaient et laissaient parfois la fenetre cachee (bug
        # observe alpha 011 : frame entiere sans masque).
        self._owner = owner_window

        # DEBUG alpha 017 : compteur de cycle public, lu depuis l'UI pour
        # correler avec les marques utilisateur "[USER MARK] texte
        # visible". Incremente a chaque capture reussie.
        self.cycle_n = 0
        self.last_n_pixels = 0
        self.last_brightness_mean = 0.0

        # v0.2 alpha 027 : periode du worker modifiable a chaud (via
        # set_period) pour permettre a l'UI de changer la frequence
        # (5/10/20/30/60 FPS) sans recreer le worker.
        # Initialisee a la valeur constante par defaut, sera ecrasee
        # par set_period si l'UI l'appelle apres l'init.
        self._period_s = max(0.005, float(DISPLAYINFO_SMART_PERIOD_MS) / 1000.0)

    @Slot(float)
    def set_period(self, period_s: float):
        """Change la periode du worker a chaud. Thread-safe (atomique
        Python sur l'attribut). Effet immediat sur le prochain cycle."""
        try:
            p = float(period_s)
            if p < 0.005:
                p = 0.005
            self._period_s = p
        except (TypeError, ValueError):
            pass

    @Slot(int, int, int, int)
    def set_region(self, left: int, top: int, width: int, height: int):
        """Met a jour la region a capturer. Thread-safe (lock)."""
        with self._lock:
            if width <= 0 or height <= 0:
                self._region = None
            else:
                self._region = {
                    "left":   int(left),
                    "top":    int(top),
                    "width":  int(width),
                    "height": int(height),
                }

    @Slot()
    def request_stop(self):
        """Demande l'arret de la boucle. Non-bloquant."""
        self._stop = True

    @Slot()
    def run(self):
        """Boucle principale du worker. Tourne dans le thread du QThread.
        Capture + traite + emit en continu jusqu'a request_stop."""
        # Imports lazy : pas la peine de charger numpy/cv2/mss au boot du
        # client si l'utilisateur n'active jamais le masque smart.
        try:
            import mss as _mss
            import numpy as _np
            import cv2 as _cv2
            import time as _time
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK SMART] Imports KO (mss/numpy/cv2) : {e}"
                    )
                except Exception:
                    pass
            return

        # v0.2 alpha 042 : tentative d'utiliser bettercam (Desktop
        # Duplication API DirectX) pour la capture, beaucoup plus rapide
        # que MSS (1-3ms vs 17ms sur la meme zone). Si bettercam n'est
        # pas installe ou plante au demarrage, on fallback sur MSS.
        # bettercam.create() peut prendre 500ms-1s au 1er appel (init
        # COM + duplication API), on le fait au demarrage du worker.
        _bettercam_cam = None
        try:
            import bettercam as _bettercam
            # output_color="BGR" pour eviter une conversion supplementaire
            # (notre pipeline travaille en BGR comme MSS).
            # output_idx selon l'ecran cible : ici on prend l'output qui
            # contient la zone de capture. Par defaut output 0 (ecran
            # principal). Si zone est sur un 2e ecran, peut etre ajuste
            # plus tard.
            _bettercam_cam = _bettercam.create(output_idx=0, output_color="BGR")
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        "[MASK SMART] bettercam OK (Desktop Duplication API). "
                        "Capture rapide active."
                    )
                except Exception:
                    pass
        except Exception as e:
            _bettercam_cam = None
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK SMART] bettercam indisponible ({e}), "
                        f"fallback MSS."
                    )
                except Exception:
                    pass

        # Cache du dernier frame valide : bettercam.grab() peut retourner
        # None si le contenu n'a pas change depuis le dernier appel.
        # Dans ce cas on reutilise le dernier bgr connu pour eviter de
        # rater un cycle complet (sinon on perd du temps).
        _last_bgr = None

        # v0.2 alpha 056 : throttle des logs "bettercam grab KO". En cas
        # d'erreur persistante (DXGI invalide, etc.), on logue la 1ere
        # erreur immediatement puis on agrege le compteur d'erreurs et
        # on re-logue toutes les 30s avec le total accumule. Reset au
        # premier grab reussi (suivant). Evite de saturer le fichier
        # de debug avec des centaines de lignes identiques.
        _grab_err_count   = 0      # nb d'erreurs depuis le dernier log
        _grab_err_last_t  = 0.0    # monotonic du dernier log emis
        _grab_err_last_e  = None   # derniere exception (pour replay)
        _GRAB_ERR_LOG_PERIOD_S = 30.0

        # Constantes lues une fois pour eviter les accesses globaux dans
        # la boucle chaude.
        BRIGHT_DIFF = int(DISPLAYINFO_TEXT_BRIGHT_DIFF)
        WINDOW_PX   = max(3, int(DISPLAYINFO_TEXT_WINDOW_PX))
        # WINDOW_PX doit etre impair pour cv2.boxFilter (kernel centre).
        if WINDOW_PX % 2 == 0:
            WINDOW_PX += 1
        DILATE_PX  = max(0, int(DISPLAYINFO_TEXT_DILATE_PX))
        DECOR_R    = max(1, int(DISPLAYINFO_DECOR_RADIUS_PX))
        MAX_BLOB_PX = max(1, int(DISPLAYINFO_TEXT_MAX_BLOB_PX))
        # v0.2 alpha 027 : self._period_s supprime comme constante locale.
        # On lit maintenant self._period_s a chaque cycle (modifiable a
        # chaud via worker.set_period() depuis l'UI).
        HIST_FRAMES = max(1, int(DISPLAYINFO_TEXT_HISTORY_FRAMES))

        # Historique temporel des masques is_text (apres dilate). Le masque
        # affiche = OR logique des HIST_FRAMES derniers : un pixel reste
        # masque tant qu'il a ete detecte au moins une fois dans la fenetre
        # glissante. Anti-clignotement quand le texte change a chaque frame
        # (FPS, coords, timers qui defilent).
        # On stocke les masques sous forme de uint8 0/1 (pas bool) pour les
        # additionner facilement via np.bitwise_or sans cast intermediaire.
        # collections.deque(maxlen=N) gere automatiquement l'eviction des
        # plus vieux masques.
        from collections import deque as _deque
        history = _deque(maxlen=HIST_FRAMES)
        history_size = (0, 0)  # (h, w) des masques en historique

        # Pre-construire le kernel de dilatation une fois.
        if DILATE_PX > 0:
            ks = 2 * DILATE_PX + 1
            dilate_kernel = _np.ones((ks, ks), dtype=_np.uint8)
        else:
            dilate_kernel = None

        # Kernel size de la boxFilter pour la moyenne decor
        decor_ksize = (2 * DECOR_R + 1, 2 * DECOR_R + 1)

        try:
            with _mss.mss() as sct:
                while not self._stop:
                    t0 = _time.monotonic()
                    # Snapshot region (lock court)
                    with self._lock:
                        region = self._region.copy() if self._region else None
                    if region is None:
                        _time.sleep(self._period_s)
                        continue

                    # v0.2 alpha 022 : plus besoin de hide/show le owner
                    # avant le grab. La fenetre est maintenant opaque
                    # (sans WA_TranslucentBackground) et compatible avec
                    # SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE).
                    # Donc MSS voit l'image SC pure (sans le masque).
                    grab_ok = False
                    # v0.2 alpha 042 : si bettercam est dispo, on l'utilise
                    # en priorite (1-3ms vs 17ms MSS). Sinon fallback MSS.
                    if _bettercam_cam is not None:
                        try:
                            # bettercam.grab() prend un tuple (left, top, right, bottom).
                            # MSS prenait (left, top, width, height) en dict.
                            bc_region = (
                                int(region["left"]),
                                int(region["top"]),
                                int(region["left"]) + int(region["width"]),
                                int(region["top"]) + int(region["height"]),
                            )
                            bc_frame = _bettercam_cam.grab(region=bc_region)
                            if bc_frame is not None:
                                # v0.2 alpha 051 : detecter et rejeter les
                                # frames anormalement noires retournees par
                                # bettercam. Symptome rapporte : clignotement
                                # noir en immobilite. Cause probable : DXGI
                                # peut renvoyer un frame transitoire entre
                                # 2 etats du compositor (changement de focus,
                                # overlay system, etc.). Le frame est noir
                                # alors que l'image SC ne l'est pas.
                                #
                                # Detection : on prend l'echantillon d'un
                                # coin (8x8 px, ultra rapide) et on regarde
                                # la moyenne. Si < 5 (= quasi noir total)
                                # ET qu'on a deja un _last_bgr valide, on
                                # rejette ce frame comme suspect.
                                #
                                # On ne calcule pas .mean() sur tout le
                                # frame (couteux), juste un sample 8x8.
                                try:
                                    sample = bc_frame[:8, :8].mean()
                                except Exception:
                                    sample = 255.0  # par defaut, accepter
                                if sample < 5.0 and _last_bgr is not None:
                                    # Frame suspect, on garde l'ancien.
                                    bgr = _last_bgr
                                    grab_ok = True
                                else:
                                    # Frame neuve OK : on l'utilise et on cache.
                                    # .copy() (alpha 050) pour ne PAS partager
                                    # le buffer interne de bettercam.
                                    bgr = bc_frame.copy()
                                    _last_bgr = bgr
                                    grab_ok = True
                            elif _last_bgr is not None:
                                # bettercam dit "pas de mise a jour" :
                                # on reutilise le dernier frame valide
                                # (deja copie a la frame precedente).
                                bgr = _last_bgr
                                grab_ok = True
                            else:
                                # Aucun frame jamais recu : sleep et retente.
                                _time.sleep(self._period_s)
                                continue
                        except Exception as e:
                            # v0.2 alpha 056 : throttle pour eviter le spam
                            # (cf. variables initialisees en haut de run()).
                            # 1ere erreur : log immediat.
                            # Erreurs suivantes : comptees silencieusement,
                            # 1 log toutes les 30s avec le total cumule
                            # depuis le debut de la rafale.
                            # Reset complet : uniquement sur un grab reussi
                            # (cf. branche else: ci-dessous).
                            _grab_err_count += 1
                            _grab_err_last_e = e
                            _now = _time.monotonic()
                            _should_log = False
                            if _grab_err_count == 1:
                                # 1ere erreur de la rafale.
                                _should_log = True
                            elif _now - _grab_err_last_t >= _GRAB_ERR_LOG_PERIOD_S:
                                # Rafale persistante depuis >=30s : recap.
                                _should_log = True
                            if _should_log and _CORE_AVAILABLE:
                                try:
                                    if _grab_err_count > 1:
                                        _core._dbg_log(
                                            f"[MASK SMART] bettercam grab KO "
                                            f"(x{_grab_err_count} depuis le debut "
                                            f"de la rafale) : {e}, "
                                            f"fallback MSS pour ce cycle."
                                        )
                                    else:
                                        _core._dbg_log(
                                            f"[MASK SMART] bettercam grab KO : "
                                            f"{e}, fallback MSS pour ce cycle."
                                        )
                                except Exception:
                                    pass
                            if _should_log:
                                # Marqueur du dernier log : la prochaine
                                # log ne se redeclenchera qu'apres 30s.
                                _grab_err_last_t = _now
                            # Fallback MSS pour ce cycle (on retry bettercam
                            # au prochain cycle, peut-etre que c'etait
                            # transitoire).
                        else:
                            # v0.2 alpha 056 : pas d'exception levee = grab
                            # bettercam reussi. Si on avait des erreurs
                            # accumulees, on log un "recovery" puis on reset.
                            if _grab_err_count > 0:
                                if _CORE_AVAILABLE:
                                    try:
                                        _core._dbg_log(
                                            f"[MASK SMART] bettercam grab OK "
                                            f"apres {_grab_err_count} erreur(s) "
                                            f"(derniere : {_grab_err_last_e})"
                                        )
                                    except Exception:
                                        pass
                                _grab_err_count  = 0
                                _grab_err_last_t = 0.0
                                _grab_err_last_e = None
                    if not grab_ok:
                        # MSS classique (soit pas de bettercam, soit fallback).
                        try:
                            raw = sct.grab(region)
                            # mss BGRA -> numpy BGR
                            img = _np.frombuffer(
                                raw.bgra, dtype=_np.uint8
                            ).reshape(raw.height, raw.width, 4)
                            # On garde BGR (drop alpha).
                            bgr = img[:, :, :3]
                            grab_ok = True
                        except Exception as e:
                            if _CORE_AVAILABLE:
                                try:
                                    _core._dbg_log(
                                        f"[MASK SMART] grab MSS KO : {e}"
                                    )
                                except Exception:
                                    pass
                    if not grab_ok:
                        _time.sleep(self._period_s)
                        continue
                    t_grab = _time.monotonic()

                    h_img, w_img = bgr.shape[:2]
                    # Detection adaptative : un pixel est "texte" si sa
                    # luminosite depasse la moyenne locale de BRIGHT_DIFF.
                    # bgr indices : 0=B, 1=G, 2=R
                    # On calcule la luminosite par pixel (moyenne R+G+B),
                    # puis la moyenne locale via cv2.boxFilter (rapide).
                    # Travail en float32 pour eviter l'overflow uint8.
                    bgr_f = bgr.astype(_np.float32)
                    brightness = (
                        bgr_f[:, :, 0] + bgr_f[:, :, 1] + bgr_f[:, :, 2]
                    ) / 3.0
                    # Moyenne locale dans fenetre WINDOW_PX x WINDOW_PX.
                    # BORDER_REPLICATE pour eviter les artefacts de bord
                    # (sinon la moyenne sur les bords serait biaisee).
                    local_mean = _cv2.boxFilter(
                        brightness, -1, (WINDOW_PX, WINDOW_PX),
                        normalize=True,
                        borderType=_cv2.BORDER_REPLICATE,
                    )
                    is_text = (brightness >= (local_mean + BRIGHT_DIFF))
                    t_detect = _time.monotonic()

                    # v0.2 alpha 038 : filtrage par taille de cluster.
                    # Un caractere du HUD a cette resolution fait
                    # typiquement quelques dizaines de pixels une fois
                    # detecte. Une zone lumineuse du JEU (lampe, voyant,
                    # blob lumineux) fait souvent beaucoup plus.
                    # cv2.connectedComponentsWithStats etiquette chaque
                    # cluster et donne sa taille. On garde uniquement
                    # les clusters dont la taille <= MAX_BLOB_PX.
                    # Connexite 8 (diagonales incluses) pour bien
                    # rassembler les caracteres et les blobs.
                    is_text_u8_pre = is_text.astype(_np.uint8)
                    n_labels, labels, stats, _centroids = _cv2.connectedComponentsWithStats(
                        is_text_u8_pre, connectivity=8
                    )
                    # stats[i] = [x, y, w, h, area]. Label 0 = fond, on
                    # l'ignore. On construit un masque qui ne garde que
                    # les labels dont l'area <= seuil.
                    if n_labels > 1:
                        # Tableau bool indexe par label : True si on garde
                        # le cluster, False sinon.
                        keep = _np.zeros(n_labels, dtype=bool)
                        # cv2.CC_STAT_AREA = 4
                        areas = stats[:, 4]
                        keep[1:] = areas[1:] <= MAX_BLOB_PX
                        # On reconstruit is_text en gardant uniquement
                        # les pixels appartenant a un label conserve.
                        is_text = keep[labels]
                    # Sinon (n_labels == 1, donc que du fond), is_text
                    # est deja entierement False, rien a filtrer.
                    t_blob = _time.monotonic()

                    n_pixels = int(is_text.sum())
                    brightness_mean = float(brightness.mean())
                    self.cycle_n += 1
                    self.last_n_pixels = n_pixels
                    self.last_brightness_mean = brightness_mean
                    # v0.2 alpha 027 : log aggrege 1x/sec au lieu de
                    # 1x/cycle (sinon a 60 FPS ca spamme 60 lignes/sec).
                    # On accumule min/max/avg et on flush toutes les
                    # ~1 sec.
                    if not hasattr(self, "_log_acc"):
                        self._log_acc = {
                            "t_start":  _time.monotonic(),
                            "frames":   0,
                            "px_min":   None,
                            "px_max":   0,
                            "px_sum":   0,
                            "bm_sum":   0.0,
                        }
                    acc = self._log_acc
                    acc["frames"] += 1
                    if acc["px_min"] is None or n_pixels < acc["px_min"]:
                        acc["px_min"] = n_pixels
                    if n_pixels > acc["px_max"]:
                        acc["px_max"] = n_pixels
                    acc["px_sum"] += n_pixels
                    acc["bm_sum"] += brightness_mean
                    # v0.2 alpha 056 : log periodique passe de 1s a 30s
                    # (sur demande utilisateur) pour reduire le bruit dans
                    # le fichier de debug. Les agregats min/max/avg restent
                    # representatifs sur 30s.
                    if _time.monotonic() - acc["t_start"] >= 30.0:
                        if _CORE_AVAILABLE:
                            try:
                                avg_px = acc["px_sum"] / max(1, acc["frames"])
                                avg_bm = acc["bm_sum"] / max(1, acc["frames"])
                                _core._dbg_log(
                                    f"[MASK SMART 30s] frames={acc['frames']} "
                                    f"px_min={acc['px_min']} max={acc['px_max']} "
                                    f"avg={avg_px:.0f} "
                                    f"bright_avg={avg_bm:.1f}"
                                )
                            except Exception:
                                pass
                        self._log_acc = {
                            "t_start":  _time.monotonic(),
                            "frames":   0,
                            "px_min":   None,
                            "px_max":   0,
                            "px_sum":   0,
                            "bm_sum":   0.0,
                        }

                    # Dilate : etend le masque d'un pixel autour pour
                    # capturer les bords antialiasees des caracteres.
                    if dilate_kernel is not None:
                        is_text_u8 = is_text.astype(_np.uint8)
                        is_text_u8 = _cv2.dilate(is_text_u8, dilate_kernel)
                    else:
                        is_text_u8 = is_text.astype(_np.uint8)

                    # Stabilisation temporelle : on combine les HIST_FRAMES
                    # derniers masques via OR logique. Si la taille de l'image
                    # a change (changement de resolution ou d'ecran), on reset
                    # l'historique pour eviter un mismatch numpy.
                    cur_size = (h_img, w_img)
                    if cur_size != history_size:
                        history.clear()
                        history_size = cur_size
                    history.append(is_text_u8)
                    if len(history) == 1:
                        # Premier frame de l'historique : pas besoin de stack.
                        is_text_combined_u8 = is_text_u8
                    else:
                        # OR via reduce sur la deque. np.bitwise_or.reduce
                        # est efficace et evite un stack intermediaire.
                        is_text_combined_u8 = _np.bitwise_or.reduce(
                            _np.stack(history, axis=0), axis=0
                        )
                    is_text = is_text_combined_u8.astype(bool)
                    t_hist = _time.monotonic()

                    # Si aucun pixel detecte : ON N'EMET PAS un QImage
                    # transparent (sinon la fenetre devient invisible et
                    # le DisplayInfo SC se montre a nu pendant cette
                    # frame -> "frame qui ne cache rien" rapporte par
                    # l'utilisateur). On garde plutot le dernier masque
                    # affiche (le paintEvent dessine self._mask_pixmap
                    # tant qu'il existe). Quand un nouveau cycle aura
                    # detecte du texte, le masque sera mis a jour.
                    # Pas de pixels detectes : on emet quand meme un QImage
                    # opaque = image SC pure (sans transformation). v0.2
                    # alpha 022 : la fenetre est maintenant opaque, donc
                    # il faut TOUJOURS lui donner un contenu a afficher,
                    # sinon elle resterait noire ou montrerait l'ancien
                    # masque (perimee). Image SC = on copie bgr -> rgba
                    # avec alpha = 255.
                    if not is_text.any():
                        if _CORE_AVAILABLE:
                            try:
                                _core._dbg_log(
                                    f"[MASK SMART] cycle {self.cycle_n} : "
                                    f"aucun pixel texte detecte - on affiche "
                                    f"l'image SC telle quelle"
                                )
                            except Exception:
                                pass
                        # Construire QImage opaque = image SC pure
                        rgba = _np.empty((h_img, w_img, 4), dtype=_np.uint8)
                        rgba[..., 0] = bgr[..., 2]
                        rgba[..., 1] = bgr[..., 1]
                        rgba[..., 2] = bgr[..., 0]
                        rgba[..., 3] = 255
                        qimg = _qimage_from_rgba(rgba)
                        try:
                            self.sig_image_ready.emit(qimg)
                        except Exception:
                            pass
                        elapsed = _time.monotonic() - t0
                        if elapsed < self._period_s:
                            _time.sleep(self._period_s - elapsed)
                        continue

                    # Couleur du decor moyen autour de chaque pixel.
                    # v0.2 alpha 046 : revert downscale 4x -> 2x. Le
                    # downscale 4x faisait apparaitre des bandes noires
                    # quand la hauteur du masque etait reduite (kernel
                    # boxFilter de 3x3 en quart de res devenait trop
                    # petit pour couvrir le voisinage decor reel).
                    # v0.2 alpha 044 : multiply en basse res (gain garde).
                    # Le decor est lisse, donc 2x suffit pour la qualite
                    # visuelle.
                    h_lo = (h_img + 1) // 2
                    w_lo = (w_img + 1) // 2
                    # Downscale bgr_f -> demi res.
                    bgr_f_lo = _cv2.resize(
                        bgr_f, (w_lo, h_lo),
                        interpolation=_cv2.INTER_AREA,
                    )
                    # Downscale not_text -> demi res.
                    not_text_u8 = (1 - is_text.astype(_np.uint8))
                    not_text_lo = _cv2.resize(
                        not_text_u8.astype(_np.float32),
                        (w_lo, h_lo),
                        interpolation=_cv2.INTER_AREA,
                    )
                    # Multiply en demi res (gain alpha 044 preserve :
                    # on ne multiplie plus 650k px en full res).
                    bgr_zeroed_lo = bgr_f_lo * not_text_lo[..., None]
                    # Kernel boxFilter
                    decor_r_lo = max(1, DECOR_R // 2)
                    decor_ksize_lo = (2 * decor_r_lo + 1, 2 * decor_r_lo + 1)
                    decor_sum_lo = _cv2.boxFilter(
                        bgr_zeroed_lo, -1, decor_ksize_lo,
                        normalize=False, borderType=_cv2.BORDER_REPLICATE,
                    )
                    weight_sum_lo = _cv2.boxFilter(
                        not_text_lo, -1, decor_ksize_lo,
                        normalize=False, borderType=_cv2.BORDER_REPLICATE,
                    )
                    # Division safe (cf. commentaire bas) en demi res.
                    weight_safe_lo = _np.maximum(weight_sum_lo, 1.0)
                    decor_mean_lo = decor_sum_lo / weight_safe_lo[..., None]
                    no_decor_lo = (weight_sum_lo == 0)
                    if no_decor_lo.any():
                        decor_mean_lo[no_decor_lo] = 0.0
                    # Upscale a la taille originale.
                    decor_mean = _cv2.resize(
                        decor_mean_lo, (w_img, h_img),
                        interpolation=_cv2.INTER_LINEAR,
                    )
                    decor_mean = _np.clip(decor_mean, 0, 255).astype(_np.uint8)
                    t_decor = _time.monotonic()

                    # Construction du QImage final OPAQUE (v0.2 alpha 022) :
                    # On REJOUE l'image SC capturee, avec les pixels texte
                    # remplaces par leur decor moyen. Alpha = 255 partout
                    # (la fenetre est opaque depuis l'alpha 022 pour que
                    # SetWindowDisplayAffinity fonctionne).
                    #   - Pixels texte    : RGBA = (decor_mean R, G, B, 255)
                    #   - Pixels non-texte: RGBA = (sc_pixel R, G, B, 255)
                    # En BGR du grab mss, indices : 0=B 1=G 2=R.
                    # En RGBA pour QImage Format_RGBA8888, on swap.
                    rgba = _np.empty((h_img, w_img, 4), dtype=_np.uint8)
                    # Par defaut : copier l'image SC capturee (bgr -> rgb).
                    rgba[..., 0] = bgr[..., 2]  # R = bgr.R
                    rgba[..., 1] = bgr[..., 1]  # G = bgr.G
                    rgba[..., 2] = bgr[..., 0]  # B = bgr.B
                    rgba[..., 3] = 255          # alpha plein partout
                    # Remplacer les pixels texte par la couleur du decor
                    # moyen (calculee plus haut, en BGR -> swap RGB).
                    if is_text.any():
                        rgb_decor = decor_mean[..., ::-1]  # B,G,R -> R,G,B
                        rgba[..., 0][is_text] = rgb_decor[..., 0][is_text]
                        rgba[..., 1][is_text] = rgb_decor[..., 1][is_text]
                        rgba[..., 2][is_text] = rgb_decor[..., 2][is_text]
                        # alpha reste a 255

                    qimg = _qimage_from_rgba(rgba)
                    try:
                        self.sig_image_ready.emit(qimg)
                    except Exception:
                        pass
                    t_emit = _time.monotonic()

                    # v0.2 alpha 040 : log des durees par etape pour
                    # identifier le goulot du pipeline. Aggrege 1x/sec.
                    if not hasattr(self, "_perf_acc"):
                        self._perf_acc = {
                            "t_start": _time.monotonic(),
                            "frames":  0,
                            "grab":    0.0,
                            "detect":  0.0,
                            "blob":    0.0,
                            "hist":    0.0,
                            "decor":   0.0,
                            "emit":    0.0,
                            "total":   0.0,
                            "grab_max":   0.0,
                            "detect_max": 0.0,
                            "blob_max":   0.0,
                            "hist_max":   0.0,
                            "decor_max":  0.0,
                            "emit_max":   0.0,
                        }
                    pa = self._perf_acc
                    pa["frames"] += 1
                    d_grab   = t_grab   - t0
                    d_detect = t_detect - t_grab
                    d_blob   = t_blob   - t_detect
                    d_hist   = t_hist   - t_blob
                    d_decor  = t_decor  - t_hist
                    d_emit   = t_emit   - t_decor
                    d_total  = t_emit   - t0
                    pa["grab"]   += d_grab
                    pa["detect"] += d_detect
                    pa["blob"]   += d_blob
                    pa["hist"]   += d_hist
                    pa["decor"]  += d_decor
                    pa["emit"]   += d_emit
                    pa["total"]  += d_total
                    if d_grab   > pa["grab_max"]:   pa["grab_max"]   = d_grab
                    if d_detect > pa["detect_max"]: pa["detect_max"] = d_detect
                    if d_blob   > pa["blob_max"]:   pa["blob_max"]   = d_blob
                    if d_hist   > pa["hist_max"]:   pa["hist_max"]   = d_hist
                    if d_decor  > pa["decor_max"]:  pa["decor_max"]  = d_decor
                    if d_emit   > pa["emit_max"]:   pa["emit_max"]   = d_emit
                    # v0.2 alpha 056 : log periodique passe de 1s a 30s
                    # (sur demande utilisateur). Les agregats avg/max
                    # restent representatifs sur 30s.
                    if _time.monotonic() - pa["t_start"] >= 30.0:
                        if _CORE_AVAILABLE:
                            try:
                                n = max(1, pa["frames"])
                                _core._dbg_log(
                                    f"[MASK PERF 30s] frames={n} "
                                    f"total_avg={1000*pa['total']/n:.1f}ms "
                                    f"grab={1000*pa['grab']/n:.1f}/{1000*pa['grab_max']:.1f} "
                                    f"detect={1000*pa['detect']/n:.1f}/{1000*pa['detect_max']:.1f} "
                                    f"blob={1000*pa['blob']/n:.1f}/{1000*pa['blob_max']:.1f} "
                                    f"hist={1000*pa['hist']/n:.1f}/{1000*pa['hist_max']:.1f} "
                                    f"decor={1000*pa['decor']/n:.1f}/{1000*pa['decor_max']:.1f} "
                                    f"emit={1000*pa['emit']/n:.1f}/{1000*pa['emit_max']:.1f} "
                                    f"(avg/max ms)"
                                )
                            except Exception:
                                pass
                        self._perf_acc = {
                            "t_start": _time.monotonic(),
                            "frames":  0,
                            "grab":    0.0, "detect": 0.0, "blob": 0.0,
                            "hist":    0.0, "decor":  0.0, "emit": 0.0,
                            "total":   0.0,
                            "grab_max":   0.0, "detect_max": 0.0,
                            "blob_max":   0.0, "hist_max":   0.0,
                            "decor_max":  0.0, "emit_max":   0.0,
                        }

                    elapsed = _time.monotonic() - t0
                    if elapsed < self._period_s:
                        _time.sleep(self._period_s - elapsed)
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK SMART] worker run KO : {e}"
                    )
                except Exception:
                    pass
        finally:
            # v0.2 alpha 042 : liberer bettercam proprement. Sinon
            # l'objet COM persiste et peut empecher une nouvelle creation
            # ou bloquer la sortie propre du process.
            if _bettercam_cam is not None:
                try:
                    _bettercam_cam.release()
                except Exception:
                    pass


def _qimage_from_rgba(rgba_array):
    """Construit un QImage RGBA8888 depuis un numpy array de shape (H,W,4)
    et dtype uint8. Le QImage est en COPIE des donnees (sinon il garde
    juste un pointeur vers le buffer numpy qui peut etre libere apres)."""
    h, w = rgba_array.shape[:2]
    # bytesPerLine = stride. Pour un array C-contiguous (4 canaux uint8) :
    bpl = w * 4
    # PySide6 : passer .tobytes() est plus sur que le pointeur direct
    # car le QImage prend ownership du buffer.
    raw = rgba_array.tobytes()
    qimg = QImage(raw, w, h, bpl, QImage.Format_RGBA8888)
    # .copy() pour que QImage ne reference pas le buffer Python local
    # (sinon corruption quand 'raw' sort de scope).
    return qimg.copy()


# ======================================================================
# Service partage du worker de capture du masque DisplayInfo
# (v0.2 alpha 060)
# ======================================================================
# Encapsule le QThread + _DisplayInfoMaskWorker, et expose un compteur
# d'attache : les consommateurs (DisplayInfoMaskWindow, DisplayInfoMaskWindowOBS)
# appellent attach() pour s'abonner au signal sig_image_ready et detach()
# pour se desabonner. Quand le compteur passe de 0 a 1, le worker demarre ;
# quand il passe de 1 a 0, il s'arrete.
#
# Avantage : la fenetre OBS peut maintenant tourner SEULE (sans fenetre
# ecran active). Inversement, la fenetre ecran peut tourner seule comme
# avant. Et si les deux sont actives, un seul worker tourne et alimente
# les deux consommateurs avec le meme QImage.
#
# Refactor de l'alpha 060 : avant, _DisplayInfoMaskWorker etait cree par
# DisplayInfoMaskWindow elle-meme. Maintenant, ce service est instancie
# une fois par ClientUI au boot, et les deux fenetres s'y attachent.

class _DisplayInfoMaskWorkerService(QObject):
    """Service singleton (au sein du ClientUI) qui gere le cycle de vie
    du worker de capture du masque. Compteur d'attache pour auto-start /
    auto-stop."""

    # Re-emis sur le signal sig_image_ready des consommateurs attaches.
    # Identique a _DisplayInfoMaskWorker.sig_image_ready (QImage).
    sig_image_ready = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: "_DisplayInfoMaskWorker | None" = None
        self._worker_thread: "QThread | None" = None
        # Liste des callbacks attaches (consumer_id -> callback). On
        # utilise un dict pour permettre detach par ID, plus simple
        # qu'une liste qu'on devrait scanner.
        self._consumers: dict[int, "callable"] = {}
        self._next_consumer_id = 1
        # Region courante (cf. set_region). Stockee ici aussi pour pouvoir
        # re-pousser au worker s'il est restart apres un stop.
        self._region: tuple[int, int, int, int] | None = None
        # Periode courante (FPS). Idem : stockee pour restart.
        self._period_s: float = 1.0 / 60.0

    def attach(self, callback) -> int:
        """Attache un consommateur. callback(QImage) sera appele a chaque
        nouvelle image. Retourne un consumer_id a passer a detach().

        Demarre le worker si c'est le 1er consommateur."""
        cid = self._next_consumer_id
        self._next_consumer_id += 1
        self._consumers[cid] = callback
        # Connecter ce consommateur au signal aggregat. On utilise une
        # connexion unique par consommateur (le signal Qt accepte plusieurs
        # slots, mais Qt n'a pas d'API directe pour "deconnecter le slot
        # ajoute a la position N", donc on garde notre propre liste).
        try:
            self.sig_image_ready.connect(callback)
        except Exception:
            pass
        # Demarrer le worker si pas deja en cours.
        if len(self._consumers) == 1 and self._worker is None:
            self._start_worker()
        if _CORE_AVAILABLE:
            try:
                _core._dbg_log(
                    f"[MASK SVC] attach consumer #{cid} "
                    f"(total : {len(self._consumers)})"
                )
            except Exception:
                pass
        return cid

    def detach(self, consumer_id: int) -> None:
        """Detache un consommateur. Arrete le worker si plus de
        consommateur."""
        cb = self._consumers.pop(consumer_id, None)
        if cb is not None:
            try:
                self.sig_image_ready.disconnect(cb)
            except Exception:
                pass
        if _CORE_AVAILABLE:
            try:
                _core._dbg_log(
                    f"[MASK SVC] detach consumer #{consumer_id} "
                    f"(restants : {len(self._consumers)})"
                )
            except Exception:
                pass
        if not self._consumers and self._worker is not None:
            self._stop_worker()

    def set_region(self, left: int, top: int, width: int, height: int) -> None:
        """Met a jour la region a capturer. Memorise et transmet au worker
        s'il tourne deja."""
        self._region = (int(left), int(top), int(width), int(height))
        if self._worker is not None:
            try:
                self._worker.set_region(left, top, width, height)
            except Exception:
                pass

    def set_period(self, period_s: float) -> None:
        """Met a jour la periode (FPS) du worker. Memorise et transmet
        au worker s'il tourne deja."""
        try:
            self._period_s = max(0.005, float(period_s))
        except Exception:
            return
        if self._worker is not None:
            try:
                self._worker.set_period(self._period_s)
            except Exception:
                pass

    def is_running(self) -> bool:
        return self._worker is not None

    # ------------------------------------------------------------------
    # Gestion interne du worker
    # ------------------------------------------------------------------
    def _on_worker_image(self, qimg):
        """Slot interne connecte au signal du worker. Re-emit sur notre
        propre signal aggregat (auquel les consommateurs sont connectes).
        Permet aux consommateurs d'etre reconnectes proprement meme si
        le worker est detruit / recree."""
        try:
            self.sig_image_ready.emit(qimg)
        except Exception:
            pass

    def _start_worker(self) -> None:
        if self._worker is not None:
            return
        try:
            self._worker_thread = QThread()
            self._worker = _DisplayInfoMaskWorker(owner_window=None)
            # Appliquer la periode memorisee AVANT moveToThread (encore
            # accessible depuis ce thread).
            try:
                self._worker.set_period(self._period_s)
            except Exception:
                pass
            self._worker.moveToThread(self._worker_thread)
            # Le worker emet sur _on_worker_image (slot du service, qui
            # re-emit sur sig_image_ready pour les consommateurs).
            self._worker.sig_image_ready.connect(self._on_worker_image)
            self._worker_thread.started.connect(self._worker.run)
            self._worker_thread.start()
            # Pousser la region courante au worker s'il y en a une.
            if self._region is not None:
                try:
                    self._worker.set_region(*self._region)
                except Exception:
                    pass
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK SVC] worker demarre "
                        f"(periode {self._period_s*1000:.0f}ms)"
                    )
                except Exception:
                    pass
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(f"[MASK SVC] _start_worker KO : {e}")
                except Exception:
                    pass
            self._worker = None
            self._worker_thread = None

    def _stop_worker(self) -> None:
        try:
            if self._worker is not None:
                try:
                    self._worker.request_stop()
                except Exception:
                    pass
            if self._worker_thread is not None:
                try:
                    self._worker_thread.quit()
                    if not self._worker_thread.wait(500):
                        self._worker_thread.terminate()
                        self._worker_thread.wait(200)
                except Exception:
                    pass
        finally:
            self._worker = None
            self._worker_thread = None
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log("[MASK SVC] worker arrete")
                except Exception:
                    pass

    def shutdown(self) -> None:
        """Arret total : detache tous les consommateurs et arrete le
        worker. A appeler au closeEvent du ClientUI."""
        for cid, cb in list(self._consumers.items()):
            try:
                self.sig_image_ready.disconnect(cb)
            except Exception:
                pass
        self._consumers.clear()
        if self._worker is not None:
            self._stop_worker()


class DisplayInfoMaskWindow(QWidget):
    """Overlay topmost frameless click-through qui masque la zone
    DisplayInfo du HUD Star Citizen.

    Le placement est calcule a partir de la geometrie d'un QScreen donne
    (l'ecran qui contient la zone OCR). Click-through via
    Qt.WindowTransparentForInput : les clics passent au jeu en-dessous.

    Mode de rendu (v0.2 alpha 006) : "smart mask" base sur la detection
    des pixels du texte HUD. Un thread worker capture la zone via MSS,
    detecte les pixels appartenant au texte (luminosite + canaux R/G
    eleves) et calcule un remplacement par la couleur du decor moyen
    autour. Resultat : seuls les caracteres sont masques, le decor du
    jeu reste totalement visible entre les lignes.

    Tant que le worker n'a pas encore produit sa premiere image (premier
    capture en cours), on dessine en fallback un rectangle arrondi
    semi-transparent (= comportement v0.2 alpha 005). Idem si la capture
    echoue de maniere repetee : on a au moins un masque visible plutot
    que rien.

    Cette fenetre est intentionnellement minimale : pas d'edition, pas
    de drag, pas de redimensionnement utilisateur, pas de header."""

    def __init__(self, screen_obj, fps: int = 60, service=None):
        # fps : frequence de rafraichissement du worker (FPS).
        # Modifiable a chaud via update_fps() depuis l'UI.
        # service : v0.2 alpha 060, _DisplayInfoMaskWorkerService partage
        # injecte par ClientUI. Si None, la fenetre ne pourra pas afficher
        # de masque calcule (juste le fallback rectangle).
        self._fps = max(1, int(fps))
        self._service = service
        # ID retourne par service.attach() pour pouvoir se detach.
        # None tant qu'on n'est pas attache.
        self._service_consumer_id: int | None = None
        # Pas de parent (top-level), pour pouvoir traverser les ecrans
        # sans contrainte. Flags :
        #   FramelessWindowHint           : pas de barre de titre
        #   WindowStaysOnTopHint          : reste au-dessus du jeu
        #   Tool                          : ne s'affiche pas dans la
        #                                    barre des taches Windows
        #   WindowTransparentForInput     : les clics passent a travers
        #   NoDropShadowWindowHint        : pas d'ombre portee Windows
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.NoDropShadowWindowHint
        )
        # Empeche Qt de marquer la fenetre comme activable (sinon elle
        # peut voler le focus au jeu au moment du show()).
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # v0.2 alpha 022 : on N'utilise PLUS WA_TranslucentBackground.
        # Raison : avec cet attribut, la fenetre est dans la categorie
        # UpdateLayeredWindow (per-pixel alpha), et Windows refuse
        # SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) sur ces
        # fenetres (error 0x08). Sans exclusion -> MSS capture notre
        # propre masque -> larsen visuel.
        # Nouvelle approche : fenetre OPAQUE. SetWindowDisplayAffinity
        # accepte. MSS voit l'image SC pure (sans le masque). Le worker
        # produit un QImage opaque qui REJOUE l'image SC capturee avec
        # les pixels texte remplaces par leur decor moyen. La fenetre
        # dessine ce QImage a plein ecran. Resultat : l'utilisateur voit
        # l'image SC sans le texte, sans clignotement.
        # WA_NoSystemBackground garde pour eviter le flash du fond
        # par defaut Qt avant le premier paintEvent.
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        self._screen = screen_obj

        # QPixmap recu du worker smart (None tant qu'aucune image n'a
        # encore ete produite -> fallback rectangle arrondi).
        self._mask_pixmap: "QPixmap | None" = None
        # Cache du QImage brut pour rescaling eventuel (non utilise pour
        # l'instant ; le worker emet a la taille exacte de la fenetre).
        self._last_qimage = None

        # Statut de l'exclusion de capture (WDA_EXCLUDEFROMCAPTURE).
        # Le worker smart n'est demarre QU'APRES confirmation, sinon
        # son rendu se capture lui-meme et cree un larsen visuel
        # (le masque flou contient son ancien masque, qui contient
        # l'ancien, etc.). En cas d'echec definitif, on reste sur le
        # fallback rectangle arrondi semi-transparent.
        self._capture_exclusion_ok    = False
        self._capture_exclusion_tried = 0  # nombre de retries effectues

        # Workers : crees plus tard, apres confirmation exclusion.
        self._worker_thread: "QThread | None" = None
        self._worker: "_DisplayInfoMaskWorker | None" = None

        # Etat memoire pour les callbacks OCR (cf. _slot_hide_for_ocr).
        self._was_visible_before_ocr = False
        self._ocr_pre_cb  = None
        self._ocr_post_cb = None
        # Etat memoire pour le hide/show du worker (cf. _slot_hide_for_worker).
        # Le worker se cache lui-meme juste avant son grab MSS pour eviter
        # le larsen visuel (capture de son propre rendu).
        self._was_visible_before_worker = False

        # Appliquer la geometrie initiale (le worker, s'il sera demarre,
        # recevra la region apres son demarrage).
        self._apply_geometry()

        # v0.2 alpha 023 : callbacks pre/post OCR DESACTIVES.
        # Avant l'alpha 022, on avait besoin de cacher la fenetre du
        # masque pendant chaque capture OCR parce que MSS la capturait
        # sinon (l'API SetWindowDisplayAffinity refusait l'exclusion
        # avec WA_TranslucentBackground -> err 0x08).
        # Depuis l'alpha 022, la fenetre est opaque, l'exclusion marche,
        # MSS ne voit plus le masque. Plus besoin de hide/show 4-5x/sec
        # autour de chaque capture OCR. Et c'etait justement ce hide/show
        # qui causait le clignotement visible (texte SC qui reapparait
        # brievement a chaque flash invisible/visible).
        # Code conserve commente au cas ou. Pour reactiver si l'exclusion
        # de capture redevient KO sur un PC particulier, decommenter.
        # self._register_ocr_callbacks()

    def _start_worker(self):
        """v0.2 alpha 060 : REFACTOR.
        Avant, on creait ici notre propre QThread + _DisplayInfoMaskWorker.
        Desormais, on s'attache au service partage du ClientUI
        (self._service). Le service gere le worker thread, mutualise avec
        la fenetre OBS si elle est aussi active, et auto-stop le worker
        quand le dernier consommateur se detache.

        Cette methode reste appelee uniquement APRES confirmation de
        l'exclusion de capture (cf. _try_capture_exclusion_then_start_worker).
        """
        try:
            if self._service is None:
                # Pas de service injecte : on ne peut pas demarrer.
                # Ne devrait pas arriver, voir __init__ qui prend le service.
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            "[MASK SMART] _start_worker : service absent, "
                            "fenetre ecran ne pourra pas afficher de masque."
                        )
                    except Exception:
                        pass
                return
            # Periode demandee par cette fenetre.
            try:
                self._service.set_period(1.0 / float(self._fps))
            except Exception:
                pass
            # Attache : le service demarre le worker si pas deja, et nous
            # connecte au signal sig_image_ready (= _on_image_ready).
            if self._service_consumer_id is None:
                self._service_consumer_id = self._service.attach(
                    self._on_image_ready
                )
            # Pousser la geometrie courante au service (qui la transmet
            # au worker).
            self._apply_geometry()
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK SMART] _start_worker KO : {e}"
                    )
                except Exception:
                    pass

    def _stop_worker(self):
        """v0.2 alpha 060 : REFACTOR.
        Se detache du service partage. Le service auto-stoppera le worker
        si on etait le dernier consommateur."""
        try:
            if self._service is not None and self._service_consumer_id is not None:
                self._service.detach(self._service_consumer_id)
                self._service_consumer_id = None
        except Exception:
            pass

    def _apply_geometry(self):
        """Calcule la geometrie du rectangle a partir de la zone OCR
        et la positionne avec une hauteur multipliee par 19 (= nombre
        approximatif de lignes affichees par r_DisplayInfo dans SC).

        v0.2 alpha 027 : avant on scalait depuis une reference 4K
        (DISPLAYINFO_MASK_REF_4K). Maintenant on utilise directement la
        zone OCR du HUD (state.zone_coords) qui represente la PREMIERE
        LIGNE du DisplayInfo. La hauteur totale du masque = hauteur de
        cette ligne x 19, ce qui couvre les 19 lignes du HUD.

        Avantages :
          - S'adapte automatiquement a la calibration utilisateur.
          - Plus de dependance a une reference 4K stricte.
          - Position pile-poil sur le HUD reel, pas approximative.

        Subtilite DPI : Qt setGeometry attend des coords LOGIQUES.
        zone_coords est en pixels PHYSIQUES. On divise par DPR de
        l'ecran cible pour convertir."""
        if self._screen is None:
            return
        try:
            geo = self._screen.geometry()  # QRect en pixels logiques
            dpr = self._screen.devicePixelRatio() or 1.0
        except Exception:
            return
        # Recuperer la zone OCR (en pixels physiques absolus du desktop).
        zone = getattr(state, "zone_coords", None) if _CORE_AVAILABLE else None
        if not isinstance(zone, dict):
            # Fallback : si pas de zone OCR (cas debug avec bypass), on
            # garde l'ancien comportement base sur la reference 4K.
            sw = geo.width()
            sh = geo.height()
            x, y, w, h = _compute_displayinfo_mask_rect(sw, sh)
            gx = geo.x() + x
            gy = geo.y() + y
            self.setGeometry(gx, gy, w, h)
            phys_left   = int(round((geo.x() + x) * dpr))
            phys_top    = int(round((geo.y() + y) * dpr))
            phys_width  = int(round(w * dpr))
            phys_height = int(round(h * dpr))
        else:
            # Nouveau : on construit la fenetre a partir de la zone OCR.
            # v0.2 alpha 045 : facteurs hauteur/largeur configurables.
            # Hauteur = zone OCR.h x hauteur_factor (defaut 19, = 19
            # lignes du HUD DisplayInfo). Largeur = zone OCR.w x
            # largeur_factor (defaut 1.0). Le masque reste colle au
            # bord droit : si largeur > 1.0, il s'etend vers la gauche.
            try:
                _cfg = _load_cfg() if _CORE_AVAILABLE else {}
                height_factor = int(_cfg.get("displayinfo_mask_height_factor", 19))
                width_factor  = float(_cfg.get("displayinfo_mask_width_factor", 1.0))
            except Exception:
                height_factor = 19
                width_factor  = 1.0
            try:
                zone_left   = int(zone.get("left",   0))
                zone_top    = int(zone.get("top",    0))
                zone_width  = int(zone.get("width",  0))
                zone_height = int(zone.get("height", 0))
                phys_top    = zone_top
                phys_height = zone_height * max(1, height_factor)
                # Largeur ajustee : on garde le bord DROIT colle a la
                # zone OCR. Si width_factor > 1, on etend vers la gauche
                # (= phys_left decroit). Si < 1, on retrecit (phys_left
                # croit).
                new_width = int(round(zone_width * max(0.1, width_factor)))
                right_edge = zone_left + zone_width
                phys_left  = right_edge - new_width
                phys_width = new_width
            except (TypeError, ValueError):
                return
            if phys_width <= 0 or phys_height <= 0:
                return
            # Conversion physique -> logique pour setGeometry Qt.
            gx = int(round(phys_left   / dpr))
            gy = int(round(phys_top    / dpr))
            gw = int(round(phys_width  / dpr))
            gh = int(round(phys_height / dpr))
            self.setGeometry(gx, gy, gw, gh)

        # Envoyer la region en pixels PHYSIQUES au service partage (qui la
        # transmet au worker). mss/bettercam travaillent en pixels
        # physiques (= absolus du desktop).
        if self._service is not None:
            try:
                self._service.set_region(
                    phys_left, phys_top, phys_width, phys_height
                )
            except Exception:
                pass
        # Une nouvelle geometrie invalide l'ancien pixmap (il etait
        # dimensionne pour l'ancienne taille). On le drop pour que le
        # paintEvent retombe sur le fallback en attendant la prochaine
        # image du worker.
        self._mask_pixmap = None

    def update_for_screen(self, screen_obj):
        """Re-applique la geometrie pour un nouvel ecran (utilise si la
        zone OCR change d'ecran)."""
        self._screen = screen_obj
        self._apply_geometry()

    def update_fps(self, fps: int):
        """Change la frequence du worker a chaud via le service."""
        try:
            self._fps = max(1, int(fps))
        except (TypeError, ValueError):
            return
        if self._service is not None:
            try:
                self._service.set_period(1.0 / float(self._fps))
            except Exception:
                pass

    @Slot(object)
    def _on_image_ready(self, qimg):
        """Recoit le QImage produit par le worker (signal sig_image_ready).
        Convertit en QPixmap (operation main-thread-only) et redessine."""
        try:
            if qimg is None:
                return
            # Si la taille du qimage ne correspond pas a la fenetre courante
            # (race condition : geometry change pendant qu'on traitait),
            # on l'ignore : la prochaine emit aura la bonne taille.
            qw = qimg.width()
            qh = qimg.height()
            # La fenetre est en logique, le qimage en physique (taille
            # de la zone MSS capturee). On accepte tant que le qimage
            # est >= 1 px. Lors du dessin on stretchera si besoin.
            if qw <= 0 or qh <= 0:
                return
            self._last_qimage = qimg
            self._mask_pixmap = QPixmap.fromImage(qimg)
            self.update()
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK SMART] _on_image_ready KO : {e}"
                    )
                except Exception:
                    pass

    def paintEvent(self, event):
        """Dessine le smart mask si dispo, sinon un rectangle arrondi
        semi-transparent en fallback."""
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            if self._mask_pixmap is not None and not self._mask_pixmap.isNull():
                # Smart mask dispo : dessine le pixmap. drawPixmap avec
                # un QRect cible scale automatiquement le pixmap a la
                # taille de la fenetre (pour le cas DPR > 1 ou les races
                # de geometry change).
                painter.drawPixmap(self.rect(), self._mask_pixmap)
            else:
                # Fallback : rectangle noir arrondi semi-transparent
                # (= comportement v0.2 alpha 005). On voit cela pendant
                # ~200-300ms apres l'apparition du masque, le temps que
                # le worker produise sa premiere image.
                opacity = max(0.0, min(1.0, float(DISPLAYINFO_MASK_OPACITY)))
                alpha   = int(round(opacity * 255))
                color   = QColor(0, 0, 0, alpha)
                painter.setBrush(color)
                painter.setPen(Qt.NoPen)
                radius = max(0, int(DISPLAYINFO_MASK_RADIUS))
                painter.drawRoundedRect(self.rect(), radius, radius)
            painter.end()
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK] paintEvent KO : {e}"
                    )
                except Exception:
                    pass

    def showEvent(self, event):
        """Appelle SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) la
        premiere fois que la fenetre est montree. Doit etre fait apres
        show() pour que le HWND existe.

        WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+) : la fenetre reste
        visible a l'oeil mais est *invisible* aux APIs de capture
        d'ecran (DXGI, BitBlt, GDI). Donc :
          - L'utilisateur voit le masque par-dessus la zone DisplayInfo
            de SC : la feature visuelle marche.
          - L'OCR (MSS = BitBlt) capture l'image SC SANS le masque :
            la lecture des coordonnees continue normalement.
          - OBS / autres outils de capture ne voient pas le masque
            non plus, ce qui est aligne avec le cas d'usage streamer.
          - Le worker smart, qui utilise aussi MSS, voit l'image SC
            SANS le masque (= il capture le texte original, pas son
            propre rendu). Pas de larsen visuel infini.

        Le worker smart n'est demarre QU'APRES confirmation de
        l'exclusion : sans elle, le worker capturerait sa propre sortie
        et creerait un larsen visuel + casserait l'OCR (vu en
        production : err=0x00000008 au 1er essai, taux OCR a 20%).

        Robustesse : si l'API echoue au 1er essai (timing du compositeur
        Windows, hwnd pas encore pret), on retry jusqu'a 5 fois avec
        un delai de 100ms entre chaque. Si toujours en echec apres
        5 tentatives, on bascule en mode degrade (rectangle arrondi
        semi-transparent, sans worker smart) pour ne PAS casser l'OCR.
        """
        try:
            super().showEvent(event)
        except Exception:
            pass
        # Une seule sequence d'init par instance (ignore re-show apres hide).
        if getattr(self, "_capture_exclusion_started", False):
            return
        self._capture_exclusion_started = True
        # Tentative immediate. Si echec, retry differe.
        self._try_capture_exclusion_then_start_worker()

    def _try_capture_exclusion_then_start_worker(self):
        """Tente l'exclusion de capture. Si succes : tres bien, le smart
        mask est invisible aux APIs de capture (OCR + OBS).

        Si echec et qu'il reste des retries : reprogramme dans 100ms.

        v0.2 alpha 010 : meme si l'exclusion echoue definitivement, on
        demarre quand meme le worker smart (avant l'alpha 010 on tombait
        en mode degrade rectangle uniforme). La raison : l'OCR est deja
        protege par les callbacks pre/post capture (hide ~5ms le temps
        de la capture MSS de l'OCR). Donc l'OCR voit l'image SC pure,
        meme sans l'exclusion d'affinity.

        Cote worker smart : il continuera a capturer son propre rendu
        (larsen visuel), mais le rendu reste lisible visuellement
        (degrade mais utilisable). Le hide cote worker sera fait dans
        une iteration future si necessaire."""
        MAX_RETRIES = 5
        ok = self._apply_capture_exclusion()
        if ok:
            self._capture_exclusion_ok = True
            # OK -> demarre le worker smart, qui ne capturera PAS son
            # propre rendu (cas ideal).
            self._start_worker()
            return
        self._capture_exclusion_tried += 1
        if self._capture_exclusion_tried < MAX_RETRIES:
            # Retry differe. QTimer.singleShot s'execute sur le thread Qt
            # (= ici), donc thread-safe pour rappeler la methode.
            QTimer.singleShot(
                100, self._try_capture_exclusion_then_start_worker
            )
        else:
            # Echec definitif des retries. On demarre quand meme le
            # worker smart : l'OCR est protege par les callbacks
            # pre/post (hide 5ms autour de chaque capture OCR), donc
            # pas de pollution OCR meme sans l'exclusion. Le worker
            # va capturer son propre rendu en larsen visuel (degrade
            # mais utilisable), traitable plus tard si necessaire.
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK] Capture exclusion impossible apres "
                        f"{MAX_RETRIES} essais. Demarrage du smart mask "
                        f"quand meme (OCR protege par les callbacks "
                        f"pre/post). Larsen visuel possible cote worker."
                    )
                except Exception:
                    pass
            self._start_worker()

    def closeEvent(self, event):
        """Arret propre du worker thread quand la fenetre est detruite.
        Debranche aussi les callbacks pre/post OCR pour eviter qu'ils
        referencent une fenetre detruite."""
        try:
            self._unregister_ocr_callbacks()
        except Exception:
            pass
        try:
            self._stop_worker()
        except Exception:
            pass
        try:
            super().closeEvent(event)
        except Exception:
            pass

    # ------------------------------------------------------------
    # Hide/show pendant la capture OCR (v0.2 alpha 009)
    # ------------------------------------------------------------
    # L'OCR (circusvoip_sc_ocr.capture_region) appelle MSS pour capturer
    # la zone des coordonnees du HUD. Si notre masque est visible au
    # moment de cette capture, MSS le voit aussi (sur Windows avec
    # WA_TranslucentBackground, l'API SetWindowDisplayAffinity refuse
    # l'exclusion -> bug Win10/11 ERROR_NOT_ENOUGH_MEMORY 0x08). Resultat :
    # l'OCR voit un masque opaque/semi-transparent au-dessus du texte SC
    # -> taux de parses chute a 20%.
    #
    # Workaround : on cache la fenetre Qt pendant CHAQUE capture OCR
    # (~5ms par capture, 2-3 captures/sec). L'oeil ne percoit pas un
    # masque cache 5ms toutes les 333ms. L'OCR voit l'image SC pure.
    #
    # Coordination :
    #   - Le client expose 2 slots Qt (_slot_hide_for_ocr / _slot_show_after_ocr)
    #     qui s'executent dans le thread Qt principal.
    #   - circusvoip_sc_ocr.set_capture_callbacks(pre, post) recoit 2
    #     fonctions qui invoke ces slots en BlockingQueuedConnection
    #     depuis le thread OCR -> le thread OCR attend que Qt ait
    #     effectivement cache la fenetre AVANT de capturer.

    @Slot()
    def _slot_hide_for_ocr(self):
        """Slot Qt invoque depuis le thread OCR. Cache la fenetre du
        masque (mais garde son etat en memoire pour la remettre apres).
        Tourne dans le thread Qt principal."""
        try:
            if self.isVisible():
                self._was_visible_before_ocr = True
                self.setVisible(False)
            else:
                self._was_visible_before_ocr = False
        except Exception:
            pass

    @Slot()
    def _slot_show_after_ocr(self):
        """Slot Qt invoque apres la capture OCR. Remontre la fenetre
        si elle etait visible avant. Tourne dans le thread Qt principal."""
        try:
            if getattr(self, "_was_visible_before_ocr", False):
                self.setVisible(True)
                self._was_visible_before_ocr = False
        except Exception:
            pass

    @Slot()
    def _slot_hide_for_worker(self):
        """Slot Qt invoque depuis le thread WORKER juste avant son grab
        MSS. v0.2 alpha 021 : test setWindowOpacity au lieu de move
        (qui s'etait fait clamper par Windows a x=32767 et n'avait pas
        empeche MSS de capturer la fenetre). L'opacite est censee etre
        moins lourde a appliquer que setVisible() ou move() : pas de
        re-composition globale, juste un changement de blending."""
        try:
            # Memorise l'opacite actuelle (normalement 1.0 mais on
            # ne fait pas d'hypothese).
            self._opacity_before_worker = self.windowOpacity()
            self.setWindowOpacity(0.0)
        except Exception:
            pass

    @Slot()
    def _slot_show_after_worker(self):
        """Slot Qt invoque par le worker apres son grab MSS. Restaure
        l'opacite d'origine."""
        try:
            op_before = getattr(self, "_opacity_before_worker", None)
            if op_before is not None:
                self.setWindowOpacity(op_before)
                self._opacity_before_worker = None
        except Exception:
            pass

    def _register_ocr_callbacks(self):
        """Branche les callbacks pre/post capture sur circusvoip_sc_ocr.
        A appeler au boot de la fenetre. Les callbacks invoquent les
        slots Qt en BlockingQueuedConnection -> le thread OCR attend
        que Qt ait traite le show/hide AVANT de continuer."""
        try:
            # _sco est importe directement dans ce module (ligne ~325).
            # Si l'import a echoue (cas degrade : OCR indispo), _sco
            # n'existe pas ou est None.
            sco_mod = _sco if "_sco" in globals() else None
            if sco_mod is None:
                return
            set_cb = getattr(sco_mod, "set_capture_callbacks", None)
            if set_cb is None:
                # Version sc_ocr trop ancienne, pas de support callbacks.
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            "[MASK] sc_ocr.set_capture_callbacks indispo "
                            "(version trop ancienne). OCR sera pollue."
                        )
                    except Exception:
                        pass
                return
            # Wrappers fermeture sur self pour exposer les bons handlers
            # au module sc_ocr. Les wrappers font le invokeMethod en
            # blocking pour que le thread OCR attende.
            def _pre():
                try:
                    QMetaObject.invokeMethod(
                        self, "_slot_hide_for_ocr",
                        Qt.BlockingQueuedConnection,
                    )
                except Exception:
                    pass

            def _post():
                try:
                    QMetaObject.invokeMethod(
                        self, "_slot_show_after_ocr",
                        Qt.BlockingQueuedConnection,
                    )
                except Exception:
                    pass

            self._ocr_pre_cb  = _pre
            self._ocr_post_cb = _post
            set_cb(_pre, _post)
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        "[MASK] OCR pre/post callbacks branches : le "
                        "masque sera cache ~5ms autour de chaque capture "
                        "OCR (OCR pur, pas de scintillement perceptible)."
                    )
                except Exception:
                    pass
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK] _register_ocr_callbacks KO : {e}"
                    )
                except Exception:
                    pass

    def _unregister_ocr_callbacks(self):
        """Debranche les callbacks. A appeler au closeEvent pour eviter
        que sc_ocr appelle un slot d'une fenetre detruite."""
        try:
            sco_mod = _sco if "_sco" in globals() else None
            if sco_mod is None:
                return
            set_cb = getattr(sco_mod, "set_capture_callbacks", None)
            if set_cb is None:
                return
            set_cb(None, None)
            self._ocr_pre_cb  = None
            self._ocr_post_cb = None
        except Exception:
            pass

    def _apply_capture_exclusion(self) -> bool:
        """Exclut la fenetre de la capture d'ecran via l'API Win32
        SetWindowDisplayAffinity. Retourne True si succes, False sinon.
        No-op sur non-Windows (retourne True : pas de larsen attendu
        de toute facon hors Windows car le worker MSS n'existe pas)."""
        # Import paresseux : on ne tape pas dans ctypes au boot si on
        # n'utilise jamais le masque.
        try:
            import sys as _sys
            if not _sys.platform.startswith("win"):
                return True  # rien a exclure, mais on autorise le worker
            import ctypes
            hwnd = int(self.winId())
            if hwnd == 0:
                return False
            WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 2004+
            user32 = ctypes.windll.user32
            # Signature : BOOL SetWindowDisplayAffinity(HWND hwnd, DWORD dwAffinity)
            user32.SetWindowDisplayAffinity.argtypes = [
                ctypes.c_void_p, ctypes.c_uint
            ]
            user32.SetWindowDisplayAffinity.restype = ctypes.c_int
            ok = user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE
            )
            if not ok:
                # Recupere le code d'erreur pour diagnostic. Codes typiques :
                #   0x00000008 (ERROR_NOT_ENOUGH_MEMORY) - timing, retry
                #   0x00000057 (ERROR_INVALID_PARAMETER) - Windows trop ancien
                #   0x00000578 (ERROR_INVALID_WINDOW_HANDLE)
                #   0x000005AA (ERROR_NO_SYSTEM_RESOURCES)
                err = ctypes.windll.kernel32.GetLastError()
                if _CORE_AVAILABLE:
                    try:
                        retry_n = getattr(self, "_capture_exclusion_tried", 0)
                        _core._dbg_log(
                            f"[MASK] SetWindowDisplayAffinity KO "
                            f"(err=0x{err:08X}, essai {retry_n + 1}). "
                            f"Retry differe..."
                        )
                    except Exception:
                        pass
                return False
            else:
                if _CORE_AVAILABLE:
                    try:
                        retry_n = getattr(self, "_capture_exclusion_tried", 0)
                        _core._dbg_log(
                            f"[MASK] SetWindowDisplayAffinity OK "
                            f"(essai {retry_n + 1}). Masque exclu de la "
                            f"capture d'ecran (OCR + OBS)."
                        )
                    except Exception:
                        pass
                return True
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK] _apply_capture_exclusion KO : {e}"
                    )
                except Exception:
                    pass
            return False


# ======================================================================
# Fenetre principale
# ======================================================================

# ======================================================================
# Popup volume par joueur
# ======================================================================
# Mini popup non-modal qui affiche un slider 0-200% pour regler le volume
# d'un joueur specifique. Sauve dans cfg client1 sous "player_volumes".
# Applique en live via state.audio_io.set_user_volume_multiplier(name, ratio).


# ======================================================================
# Source OBS du masque DisplayInfo (v0.2 alpha 058, refactor alpha 060)
# ======================================================================
# Fenetre "soeur" de DisplayInfoMaskWindow qui sert de source de capture
# pour OBS Studio. Elle :
#   - est positionnee HORS ECRAN (x=-3000) pour ne PAS gener l'utilisateur
#   - n'a PAS WDA_EXCLUDEFROMCAPTURE : elle est intentionnellement
#     capturable par OBS (et donc aussi par d'autres APIs de capture, mais
#     elle n'a aucun pixel "sensible" : juste un masque deja calcule)
#   - v0.2 alpha 060 : s'attache au service partage
#     _DisplayInfoMaskWorkerService qui mutualise le worker entre la
#     fenetre ecran et la fenetre OBS. La fenetre OBS peut donc tourner
#     SEULE (sans fenetre ecran) : utile pour les streamers qui veulent
#     cacher leur HUD aux viewers SANS l'avoir masque sur leur propre
#     ecran de jeu.
#
# Le streamer ajoute dans OBS une source "Window Capture" / "Capture de
# fenetre" en mode "Windows 10 Graphics Capture" pointant sur la fenetre
# de titre "CircusVOIP - Mask Source for OBS". Il positionne cette source
# dans sa scene OBS par-dessus son Game Capture, sur la zone HUD.

class DisplayInfoMaskWindowOBS(QWidget):
    """Fenetre offscreen qui rejoue le QImage du masque pour qu'OBS puisse
    la capturer en tant que source dediee.

    Positionnee hors ecran (x=-3000, y=-3000) avec la meme taille que la
    zone HUD. Pas de SetWindowDisplayAffinity : elle est intentionnellement
    capturable. v0.2 alpha 060 : s'attache au service partage
    _DisplayInfoMaskWorkerService au showEvent / s'en detache au closeEvent.

    Titre de fenetre stable : "CircusVOIP - Mask Source for OBS" (utilise
    par le streamer pour identifier la source dans OBS).
    """

    OBS_WINDOW_TITLE = "CircusVOIP - Mask Source for OBS"

    def __init__(self, screen_obj, service=None):
        # service : v0.2 alpha 060, _DisplayInfoMaskWorkerService partage
        # injecte par ClientUI. Si None, la fenetre ne pourra pas afficher
        # de masque calcule.
        # Pas de parent (top-level). Flags :
        #   FramelessWindowHint           : pas de barre de titre
        #   WindowTransparentForInput     : par securite, meme si la
        #                                    fenetre est hors ecran et
        #                                    en theorie inatteignable
        #                                    par les clics
        #   NoDropShadowWindowHint        : pas d'ombre portee Windows
        #
        # v0.2 alpha 059 : on N'utilise PLUS Qt.Tool. Raison : Qt.Tool
        # ajoute WS_EX_TOOLWINDOW au style Win32, et OBS "Window Capture"
        # (mode Windows 10 Graphics Capture) filtre les fenetres avec ce
        # flag (considerees comme des outils flottants type Discord
        # overlay). Resultat : la fenetre n'apparaissait pas dans la liste
        # OBS. Sans Qt.Tool, la fenetre apparait dans la taskbar Windows
        # et Alt-Tab, mais c'est le prix a payer pour qu'OBS la voit. Le
        # titre explicite ("CircusVOIP - Mask Source for OBS") rend
        # l'entree comprehensible pour l'utilisateur.
        #
        # NB : on N'utilise PAS WindowStaysOnTopHint (rien a montrer a
        # l'utilisateur). On N'utilise PAS WA_TranslucentBackground (on
        # veut une fenetre opaque comme la fenetre ecran, le QImage emis
        # par le worker est deja opaque cf. alpha 022).
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowTransparentForInput
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        # Titre de fenetre explicite : c'est ce que le streamer va chercher
        # dans OBS (Window Capture) ET ce qui apparaitra dans la taskbar
        # / Alt-Tab.
        self.setWindowTitle(self.OBS_WINDOW_TITLE)

        self._screen = screen_obj
        self._mask_pixmap: "QPixmap | None" = None
        self._last_qimage = None

        # v0.2 alpha 060 : service partage du worker masque.
        self._service = service
        self._service_consumer_id: int | None = None

        # Geometrie : meme taille que la zone HUD masquee. Position : hors
        # ecran (x = -3000, y = -3000). Si l'utilisateur a plusieurs ecrans
        # qui s'etendent loin a gauche, -3000 ne suffira peut-etre pas,
        # mais c'est extremement rare. Si besoin on adaptera.
        self._refresh_geometry()

    def _refresh_geometry(self, ref_geometry=None) -> None:
        """(Re)calcule la geometrie de la fenetre offscreen.

        Si `ref_geometry` (un QRect) est fourni, on prend ses dimensions
        (la fenetre OBS aura exactement la meme taille que la fenetre
        masque ecran). C'est l'appel normal depuis _update_displayinfo_mask
        une fois que la fenetre ecran a calcule sa propre geometrie.

        Sinon, on fait un best-effort minimal avec _compute_displayinfo_mask_rect
        sur la geometrie de l'ecran cible. Dans les deux cas, la position
        est forcee hors ecran (-3000, -3000)."""
        try:
            if ref_geometry is not None:
                w = max(1, int(ref_geometry.width()))
                h = max(1, int(ref_geometry.height()))
            else:
                # Fallback : calcul approximatif depuis l'ecran cible. Ne
                # tient pas compte des facteurs height/width configurables.
                # Sera corrige des qu'on aura ref_geometry au prochain tick.
                geo = self._screen.geometry()
                _x, _y, w, h = _compute_displayinfo_mask_rect(
                    geo.width(), geo.height()
                )
                if w <= 0 or h <= 0:
                    w, h = 600, 400
        except Exception:
            w, h = 600, 400
        # Position fixe hors ecran : pas visible pour l'utilisateur, mais
        # toujours rendue par DWM donc capturable par OBS via "Windows 10
        # Graphics Capture".
        self.setGeometry(-3000, -3000, int(w), int(h))

    @Slot(object)
    def on_image_ready(self, qimg):
        """Recoit le QImage produit par le worker (signal sig_image_ready
        du worker de la fenetre ECRAN, qu'on reutilise). Convertit en
        QPixmap (main-thread-only) et redessine.

        Le QImage transmit est exactement celui qu'affiche la fenetre
        ecran : meme qualite, meme latence. Le streamer aura donc le
        meme rendu chez ses viewers que ce qu'il voit lui-meme a l'ecran
        (modulo le decalage temporel entre Game Capture OBS et notre
        fenetre offscreen, cf. discussion alpha 058)."""
        try:
            if qimg is None:
                return
            if qimg.width() <= 0 or qimg.height() <= 0:
                return
            self._last_qimage = qimg
            self._mask_pixmap = QPixmap.fromImage(qimg)
            self.update()
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK OBS] on_image_ready KO : {e}"
                    )
                except Exception:
                    pass

    def paintEvent(self, event):
        """Dessine le mask pixmap si dispo, sinon rectangle gris fallback
        (au cas ou la fenetre est visible avant que le worker ait emit
        sa premiere image)."""
        try:
            from PySide6.QtGui import QPainter, QColor
            painter = QPainter(self)
            try:
                if self._mask_pixmap is not None:
                    # Stretch pour remplir toute la fenetre. Normalement
                    # le QImage du worker fait deja la bonne taille
                    # (calculee depuis la meme geometrie).
                    painter.drawPixmap(self.rect(), self._mask_pixmap)
                else:
                    # Fallback rectangle gris fonce opaque le temps que
                    # le worker emit sa 1ere image. Mieux que du blanc
                    # par defaut Qt qui pourrait flasher chez les viewers.
                    painter.fillRect(self.rect(), QColor(32, 32, 32, 255))
            finally:
                painter.end()
        except Exception:
            pass

    def showEvent(self, event):
        """v0.2 alpha 060 : s'attache au service partage au premier show.
        Le service auto-demarre le worker s'il n'est pas deja en cours."""
        try:
            super().showEvent(event)
        except Exception:
            pass
        if self._service is None:
            return
        if self._service_consumer_id is None:
            try:
                self._service_consumer_id = self._service.attach(
                    self.on_image_ready
                )
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            "[MASK OBS] attachee au service partage "
                            f"(consumer_id={self._service_consumer_id})"
                        )
                    except Exception:
                        pass
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(f"[MASK OBS] attach KO : {e}")
                    except Exception:
                        pass

    def closeEvent(self, event):
        """v0.2 alpha 060 : se detache du service partage. Le service
        auto-stoppera le worker si c'etait le dernier consommateur."""
        try:
            if (self._service is not None
                    and self._service_consumer_id is not None):
                self._service.detach(self._service_consumer_id)
                self._service_consumer_id = None
        except Exception:
            pass
        try:
            super().closeEvent(event)
        except Exception:
            pass


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


class SoundboardWindow(QWidget):
    """Fenetre flottante du soundboard. Apparait au clic du bouton
    'Soundboard' dans le panneau principal, disparait au reclic.
    Contient un bouton par son disponible.

    v0.2 alpha 029 : fenetre simple, 1 son (alarme).
    v0.2 alpha 034 : ajout cooldown 2s par bouton + grisage de tous les
    boutons pendant la lecture (regle "un seul son a la fois").
    Future evolution : raccourcis clavier, permissions.

    Le parent doit etre la MainWindow (qui possede les methodes
    _on_soundboard_sound_clicked et le cache _soundboard_cache)."""

    # Duree de cooldown par bouton (en secondes). Pendant ce delai, les
    # clics sur le MEME bouton sont ignores. Cooldown distinct de
    # "un seul son a la fois" : le cooldown empeche le spam d'un meme
    # son, "un seul son" empeche de declencher un autre son par dessus.
    COOLDOWN_S = 2.0

    def __init__(self, parent=None):
        # v0.2 alpha 037 : Qt.Popup au lieu de Qt.Tool. Une fenetre Popup
        # se ferme automatiquement des qu'un clic survient en dehors de
        # son perimetre (comportement standard d'un menu deroulant).
        # Combine avec FramelessWindowHint pour pas de barre de titre.
        # Plus besoin de WA_ShowWithoutActivating : Popup prend
        # toujours le focus, c'est justement comme ca qu'il sait quand
        # se fermer.
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self._main = parent
        self.setStyleSheet(
            "QWidget { background-color: #1e1e22; border: 1px solid #555; }"
            "QPushButton { background-color: #2a2a30; color: #ddd; "
            "padding: 8px 12px; border: 1px solid #444; font-size: 10pt; }"
            "QPushButton:hover { background-color: #3a3a45; border-color: #888; }"
            "QPushButton:pressed { background-color: #4a4a55; }"
            "QPushButton:disabled { background-color: #1a1a1d; color: #555; "
            "border-color: #2a2a2d; }"
        )
        # Layout : 1 bouton par son dispo. Pour 1 son au demarrage,
        # tres simple. Quand on aura plus de sons, on les groupera en
        # grille (ex: 4 par ligne) plus tard.
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        # Construction des boutons depuis MainWindow.SOUNDBOARD_FILES.
        # On garde une reference au QPushButton et le timestamp du
        # dernier clic accepte pour gerer le cooldown 2s par bouton.
        self._buttons: dict = {}        # sound_id -> QPushButton
        self._last_click_ts: dict = {}  # sound_id -> monotonic ts du dernier clic
        # Etat global "un son joue actuellement" (regle un seul son a la
        # fois). Quand True, tous les boutons sont disabled visuellement.
        self._is_playing_now = False
        if self._main is not None and hasattr(self._main, "SOUNDBOARD_FILES"):
            files = self._main.SOUNDBOARD_FILES
        else:
            files = {}
        if not files:
            v.addWidget(QLabel("(Aucun son disponible)"))
        for sound_id in files.keys():
            label = self._format_label(sound_id)
            btn = QPushButton(label)
            btn.clicked.connect(
                lambda checked=False, sid=sound_id: self._on_clicked(sid)
            )
            self._buttons[sound_id] = btn
            v.addWidget(btn)
        # Taille auto au contenu
        self.adjustSize()

    @staticmethod
    def _format_label(sound_id: str) -> str:
        """Met en forme le label d'un bouton a partir du sound_id."""
        emoji_map = {
            "alarme": "🚨",
        }
        emoji = emoji_map.get(sound_id, "🔊")
        # Capitalise sound_id pour affichage
        return f"{emoji}  {sound_id.capitalize()}"

    def _on_clicked(self, sound_id: str):
        """Relaie le clic vers MainWindow._on_soundboard_sound_clicked.
        Applique d'abord :
          1. Cooldown 2s par bouton (anti-spam, ignore les clics rapides).
          2. Regle "un seul son a la fois" : si un son joue actuellement,
             on rejette le nouveau clic sans rien envoyer.
        Si le clic passe les 2 filtres, on emet le message WS au serveur.
        Le ts du dernier clic accepte est stocke pour le cooldown."""
        now = time.monotonic()
        # 1. Cooldown 2s
        last_ts = self._last_click_ts.get(sound_id, 0.0)
        if now - last_ts < self.COOLDOWN_S:
            # Trop tot, on ignore silencieusement (pas de log spam).
            return
        # 2. "Un seul son a la fois" - on demande l'etat a audio_io.
        # Note : on consulte audio_io directement plutot que
        # self._is_playing_now parce que ce flag est mis a jour via
        # un QTimer cote MainWindow et peut etre un peu en retard
        # (race condition possible juste apres un clic).
        try:
            audio = getattr(state, "audio_io", None) if _CORE_AVAILABLE else None
            if audio is not None and audio.is_soundboard_playing():
                # Un son joue deja -> on ignore. Pas de log non plus
                # (l'utilisateur a peut-etre clique vite par mistake,
                # pas la peine de spammer).
                return
        except Exception:
            pass
        # Clic accepte : on memorise et on transmet.
        self._last_click_ts[sound_id] = now
        if self._main is not None:
            try:
                self._main._on_soundboard_sound_clicked(sound_id)
            except Exception as e:
                try:
                    self._main._on_log(
                        f"[SOUNDBOARD] click handler KO : {e}"
                    )
                except Exception:
                    pass

    def set_playing_state(self, playing: bool):
        """Met a jour l'etat de lecture. Appele par MainWindow via un
        QTimer qui interroge audio_io.is_soundboard_playing() toutes les
        100ms. Quand playing change, on grise/degrise tous les boutons
        visuellement (l'utilisateur sait qu'il ne peut pas relancer)."""
        if playing == self._is_playing_now:
            return  # rien a faire
        self._is_playing_now = playing
        for btn in self._buttons.values():
            try:
                btn.setEnabled(not playing)
            except Exception:
                pass


# ======================================================================
# CircusPhone (Feature 4, D4) : overlay smartphone
# ======================================================================
# Fenetre overlay frameless + topmost qui reproduit un smartphone :
# corps noir arrondi, bandeau "CircusPhone", grand ecran. L'ecran affiche
# differents "ecrans" (pages internes) ; D4 etape 1 ne gere que l'ecran
# par defaut : la liste de l'annuaire (contacts connectes / deconnectes).
# Les ecrans appel entrant / en cours / messagerie viendront ensuite.
#
# Apparition : animation de montee depuis le bas (400ms), position finale
# aux 3/4 droite de l'ecran. Taille adaptative selon la resolution.

# --- Palette du smartphone (reprise du mockup SVG) ---
_PHONE_BODY_COLOR    = "#1a1a1a"   # corps du telephone
_PHONE_BTN_COLOR     = "#0a0a0a"   # boutons lateraux
_PHONE_SCREEN_BG     = "#ffffff"   # fond de l'ecran
_PHONE_BANNER_GREY   = "#888888"   # "Circus" (petit, gris)
_PHONE_BANNER_WHITE  = "#ffffff"   # "Phone" (grand, blanc)
_PHONE_DOT_ONLINE    = "#3fb950"   # pastille joueur connecte (vert)
_PHONE_DOT_OFFLINE   = "#7a1f1f"   # pastille joueur deconnecte (rouge fonce)
_PHONE_NAME_ONLINE   = "#1a1a1a"   # nom d'un connecte (sombre, lisible)
_PHONE_NAME_OFFLINE  = "#9aa0a6"   # nom d'un deconnecte (gris)
_PHONE_ACCENT        = "#2f6fed"   # accent (icones cliquables)
_PHONE_SCREEN_TXT    = "#3a3f44"   # texte courant sur l'ecran
# Boutons d'appel (ecrans incoming / outgoing / in_call)
_PHONE_BTN_ACCEPT       = "#3fb950"   # decrocher (vert)
_PHONE_BTN_HANGUP       = "#f85149"   # refuser / raccrocher (rouge)
_PHONE_BTN_TOGGLE_OFF   = "#6e7681"   # toggle inactif (gris)
_PHONE_BTN_TOGGLE_ON    = "#f85149"   # toggle mute actif (rouge : muet)
_PHONE_BTN_TOGGLE_SPEAKER = "#ffffff" # toggle HP actif (blanc, spec)


class _AvatarWidget(QLabel):
    """[D5] Avatar rond pour les emplacements CircusPhone (header MP,
    header appel, item contact). Affiche un JPEG passe en bytes, clippe
    par un cercle (QPainterPath). Si aucune photo n'est fournie (bytes
    None ou vides), le widget reste vide visuellement (transparent) :
    selon la spec D5, pas de placeholder graphique - l'emplacement
    'disparait' simplement.

    Pour gerer le 'pas d'avatar = pas d'emplacement', le parent doit
    appeler set_photo_bytes(None) ou setVisible(False) selon le rendu
    voulu. Ce widget ne cache pas tout seul (un layout qui le contient
    peut decider de le hider via setVisible).
    """

    def __init__(self, size: int, parent=None):
        super().__init__(parent)
        self._size = int(size)
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background:transparent;")
        self._has_photo: bool = False
        self._pixmap: QPixmap | None = None

    def set_photo_bytes(self, jpeg_bytes):
        """Charge un JPEG depuis des bytes. None ou bytes vides -> pas de
        photo (widget transparent)."""
        if not jpeg_bytes:
            self._has_photo = False
            self._pixmap = None
            self.update()
            return
        try:
            pm = QPixmap()
            ok = pm.loadFromData(jpeg_bytes, "JPEG") or pm.loadFromData(jpeg_bytes)
            if not ok or pm.isNull():
                self._has_photo = False
                self._pixmap = None
                self.update()
                return
            # Resize au max au cote du widget pour limiter le travail
            # de QPainter et eviter l'aliasing.
            self._pixmap = pm.scaled(
                self._size * 2, self._size * 2,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            self._has_photo = True
        except Exception:
            self._has_photo = False
            self._pixmap = None
        self.update()

    def has_photo(self) -> bool:
        return self._has_photo

    def paintEvent(self, ev):
        if not self._has_photo or self._pixmap is None:
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            # Clip circulaire.
            path = QPainterPath()
            path.addEllipse(0, 0, self._size, self._size)
            p.setClipPath(path)
            # Centrer le pixmap (qui est plus grand que le widget pour le
            # crop center via KeepAspectRatioByExpanding).
            pm = self._pixmap
            x = (self._size - pm.width()) // 2
            y = (self._size - pm.height()) // 2
            p.drawPixmap(x, y, pm)
        finally:
            p.end()


class _PhoneIconLabel(QLabel):
    """Petite icone vectorielle dessinee en QPainter (combine / enveloppe).
    Cliquable : emet sig_clicked si enabled. Utilisee dans les lignes de
    contact (icone telephone + icone lettre en bout de ligne).
    Support optionnel d'un badge "unread" : un petit cercle rouge dans le
    coin superieur droit, utilise pour signaler des MP non lus sur
    l'enveloppe (D4 etape 3)."""

    sig_clicked = Signal()

    def __init__(self, kind: str, size: int, enabled: bool, parent=None):
        super().__init__(parent)
        self._kind = kind          # "phone" | "letter" | "forget"
        self._sz = size
        self._enabled_click = enabled
        self._badge = False
        # Surbrillance navigation clavier (D-pad) : quand True, on dessine
        # un halo arrondi derriere l'icone pour montrer que cette action
        # (Appeler / Message) est celle qui sera declenchee par Entree.
        self._nav_sel = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)

    def set_badge(self, on: bool):
        """Active/desactive le badge rouge "unread". Repeint si change."""
        on = bool(on)
        if self._badge != on:
            self._badge = on
            self.update()

    def set_nav_selected(self, on: bool):
        """Active/desactive le halo de selection navigation clavier."""
        on = bool(on)
        if self._nav_sel != on:
            self._nav_sel = on
            self.update()

    def mousePressEvent(self, ev):
        if self._enabled_click and ev.button() == Qt.LeftButton:
            self.sig_clicked.emit()
        super().mousePressEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = self._sz
        # Carre de selection navigation clavier : dessine EN PREMIER (sous
        # l'icone) pour signaler l'action ciblee par le D-pad. Carre a bords
        # arrondis, bleu accent avec transparence (fond leger).
        if self._nav_sel:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(_PHONE_ACCENT))
            p.setOpacity(0.22)
            p.drawRoundedRect(0, 0, s, s, s * 0.28, s * 0.28)
            p.setOpacity(1.0)
        # Couleur : accent si cliquable, gris si non.
        col = QColor(_PHONE_ACCENT if self._enabled_click else "#c2c6cb")
        if self._kind == "phone":
            # TODO REVERT (icone v2) : combine telephone "fluide" en plein
            # (style smartphone moderne). Pour revenir a l'ancien arc + 2 ronds :
            # restaurer le bloc drawArc + 2 drawEllipse precedent.
            #
            # Forme : combine type iOS, plein, en diagonale. Path normalise
            # sur un viewport 80x80 (avec une marge ~10% autour) puis
            # rescaled a s. Toutes les coords sont en proportions de s.
            from PySide6.QtGui import QPainterPath
            path = QPainterPath()
            def P(x, y):
                # 80x80 source -> s actuel
                return (x / 80.0) * s, (y / 80.0) * s
            # On reproduit le path SVG choisi (proposition 2).
            # Origine : haut-gauche du combine
            x, y = P(18, 18)
            path.moveTo(x, y)
            # Q 18 12, 24 12
            cx1, cy1 = P(18, 12); ex, ey = P(24, 12)
            path.quadTo(cx1, cy1, ex, ey)
            # L 32 12
            ex, ey = P(32, 12)
            path.lineTo(ex, ey)
            # Q 38 12, 38 18
            cx1, cy1 = P(38, 12); ex, ey = P(38, 18)
            path.quadTo(cx1, cy1, ex, ey)
            # L 38 24
            ex, ey = P(38, 24)
            path.lineTo(ex, ey)
            # Q 38 30, 34 32
            cx1, cy1 = P(38, 30); ex, ey = P(34, 32)
            path.quadTo(cx1, cy1, ex, ey)
            # Q 32 33, 32 36
            cx1, cy1 = P(32, 33); ex, ey = P(32, 36)
            path.quadTo(cx1, cy1, ex, ey)
            # Q 32 44, 40 52
            cx1, cy1 = P(32, 44); ex, ey = P(40, 52)
            path.quadTo(cx1, cy1, ex, ey)
            # Q 48 60, 56 60
            cx1, cy1 = P(48, 60); ex, ey = P(56, 60)
            path.quadTo(cx1, cy1, ex, ey)
            # Q 59 60, 60 58
            cx1, cy1 = P(59, 60); ex, ey = P(60, 58)
            path.quadTo(cx1, cy1, ex, ey)
            # Q 62 54, 68 54
            cx1, cy1 = P(62, 54); ex, ey = P(68, 54)
            path.quadTo(cx1, cy1, ex, ey)
            # L 74 54
            ex, ey = P(74, 54)
            path.lineTo(ex, ey)
            # Q 80 54, 80 60
            cx1, cy1 = P(80, 54); ex, ey = P(80, 60)
            path.quadTo(cx1, cy1, ex, ey)
            # L 80 68
            ex, ey = P(80, 68)
            path.lineTo(ex, ey)
            # Q 80 74, 74 74
            cx1, cy1 = P(80, 74); ex, ey = P(74, 74)
            path.quadTo(cx1, cy1, ex, ey)
            # Q 50 74, 30 54
            cx1, cy1 = P(50, 74); ex, ey = P(30, 54)
            path.quadTo(cx1, cy1, ex, ey)
            # Q 18 38, 18 28
            cx1, cy1 = P(18, 38); ex, ey = P(18, 28)
            path.quadTo(cx1, cy1, ex, ey)
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawPath(path)
        elif self._kind == "letter":
            # Enveloppe : rectangle + rabat en V.
            pen = QPen(col, max(1.6, s * 0.11))
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            m = s * 0.20
            w = s - 2 * m
            h = w * 0.68
            top = (s - h) / 2
            p.drawRect(int(m), int(top), int(w), int(h))
            # Rabat
            p.drawLine(int(m), int(top), int(s / 2), int(top + h * 0.55))
            p.drawLine(int(s / 2), int(top + h * 0.55),
                       int(m + w), int(top))
        elif self._kind == "forget":
            # Croix "oublier ce contact".
            pen = QPen(col, max(1.6, s * 0.14))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            m = s * 0.30
            p.drawLine(int(m), int(m), int(s - m), int(s - m))
            p.drawLine(int(s - m), int(m), int(m), int(s - m))
        elif self._kind == "back":
            # Fleche retour vers la gauche (chevron).
            pen = QPen(col, max(2.0, s * 0.14))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            cx = s * 0.56
            cy = s * 0.5
            arm = s * 0.22
            p.drawLine(int(cx), int(cy - arm), int(cx - arm), int(cy))
            p.drawLine(int(cx - arm), int(cy), int(cx), int(cy + arm))
            # Petite barre horizontale pour suggerer "retour".
            p.drawLine(int(cx - arm), int(cy), int(s * 0.84), int(cy))
        elif self._kind == "send":
            # Avion en papier / fleche d'envoi (triangle vers la droite).
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            from PySide6.QtGui import QPolygon
            poly = QPolygon([
                QPoint(int(s * 0.18), int(s * 0.30)),
                QPoint(int(s * 0.82), int(s * 0.50)),
                QPoint(int(s * 0.18), int(s * 0.70)),
                QPoint(int(s * 0.30), int(s * 0.50)),
            ])
            p.drawPolygon(poly)
        elif self._kind == "clip":
            # Trombone (piece jointe). Dessin en primitives simples
            # (drawLine/drawArc), incline a -45°. En cas de pepin, repli
            # texte "📎" + log pour diagnostiquer.
            try:
                pen = QPen(col, max(2.0, s * 0.10))
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.save()
                p.translate(s / 2.0, s / 2.0)
                p.rotate(-45)
                p.translate(-s / 2.0, -s / 2.0)
                xL, xR = s * 0.34, s * 0.66
                yT, cB = s * 0.30, s * 0.58
                R1 = (xR - xL) / 2.0
                # cote gauche
                p.drawLine(QPointF(xL, yT), QPointF(xL, cB))
                # arc bas exterieur (de gauche a droite par le bas)
                p.drawArc(QRectF(xL, cB - R1, 2 * R1, 2 * R1),
                          180 * 16, 180 * 16)
                # cote droit
                yT2 = s * 0.26
                p.drawLine(QPointF(xR, cB), QPointF(xR, yT2))
                # arc haut (de droite vers l'interieur par le haut)
                R2 = s * 0.105
                p.drawArc(QRectF(xR - 2 * R2, yT2 - R2, 2 * R2, 2 * R2),
                          0, 180 * 16)
                # descente interieure
                xI = xR - 2 * R2
                cI = s * 0.53
                p.drawLine(QPointF(xI, yT2), QPointF(xI, cI))
                # petit arc bas interieur
                R3 = s * 0.05
                p.drawArc(QRectF(xI, cI - R3, 2 * R3, 2 * R3),
                          180 * 16, 180 * 16)
                p.restore()
            except Exception as e:
                try:
                    p.restore()
                except Exception:
                    pass
                print(f"[PHONE ICON] clip paint KO : {e}")
                f = QFont()
                f.setPixelSize(int(s * 0.7))
                p.setFont(f)
                p.setPen(col)
                p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, "📎")
        elif self._kind == "gear":
            # Engrenage (D5 a venir : reglages profil). 8 dents
            # rectangulaires reparties tous les 45° autour d'un anneau
            # central + trou au milieu. Dessin en plein pour bien voir
            # a petite taille.
            cx = s / 2.0
            cy = s / 2.0
            # Rayon externe (pointe des dents) et interne (anneau)
            r_out = s * 0.46
            r_in  = s * 0.34
            # Largeur angulaire de chaque dent
            tooth_half_deg = 14  # demi-angle en degres
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            # 1) Dents : 8 polygones trapezoidaux
            import math
            for i in range(8):
                base_angle = i * 45  # degres
                a1 = math.radians(base_angle - tooth_half_deg)
                a2 = math.radians(base_angle + tooth_half_deg)
                # 4 sommets : 2 sur le cercle interne, 2 sur l'externe
                from PySide6.QtGui import QPolygonF
                from PySide6.QtCore import QPointF
                poly = QPolygonF([
                    QPointF(cx + r_in * math.cos(a1), cy + r_in * math.sin(a1)),
                    QPointF(cx + r_out * math.cos(a1), cy + r_out * math.sin(a1)),
                    QPointF(cx + r_out * math.cos(a2), cy + r_out * math.sin(a2)),
                    QPointF(cx + r_in * math.cos(a2), cy + r_in * math.sin(a2)),
                ])
                p.drawPolygon(poly)
            # 2) Anneau central : disque plein
            p.drawEllipse(QPointF(cx, cy), r_in, r_in)
            # 3) Trou au milieu : on dessine un cercle blanc (= couleur de
            #    fond du telephone) plutot que de "trouer" avec
            #    CompositionMode_Clear. Avant, le clear creait une vraie
            #    transparence dans le QPixmap, ce qui faisait apparaitre
            #    ce qui est derriere le widget icone (noir, jaune selon
            #    contexte) au lieu du fond blanc du telephone. Un cercle
            #    blanc plein est plus simple et fiable.
            hole_r = s * 0.13
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(QPointF(cx, cy), hole_r, hole_r)
        # Badge "unread" : cercle rouge dans le coin superieur droit. Pose
        # par-dessus l'icone. Taille proportionnelle a l'icone.
        if self._badge:
            br = max(4, int(s * 0.30))
            bx = s - br - max(1, int(s * 0.04))
            by = max(1, int(s * 0.04))
            p.setPen(Qt.NoPen)
            # Liseret blanc fin pour bien detacher du fond.
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(bx - 1, by - 1, br + 2, br + 2)
            p.setBrush(QColor("#f85149"))
            p.drawEllipse(bx, by, br, br)
        p.end()


class _PhoneContactRow(QWidget):
    """Une ligne de contact dans l'ecran annuaire : pastille de statut,
    [D5 optionnel : avatar rond si photo disponible], nom, et en bout de
    ligne soit (telephone + lettre) si connecte, soit (croix "oublier") si
    deconnecte."""

    sig_call    = Signal(str)   # pseudo : clic sur l'icone telephone
    sig_message = Signal(str)   # pseudo : clic sur l'icone lettre
    sig_forget  = Signal(str)   # pseudo : clic sur la croix "oublier"

    def __init__(self, pseudo: str, online: bool, row_h: int,
                 unread: bool = False, photo_bytes=None, parent=None,
                 mode: str = "full"):
        super().__init__(parent)
        # Sans cet attribut, un QWidget simple NE PEINT PAS le 'background'
        # defini en stylesheet (la surbrillance de navigation resterait
        # invisible). Indispensable pour le halo de selection en mode message.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._pseudo = pseudo
        self._online = online
        self._mode = mode          # "full" (tel+lettre) ou "message" (ligne)
        self.setFixedHeight(row_h)
        icon_sz = max(14, int(row_h * 0.55))
        dot_sz  = max(7, int(row_h * 0.28))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)

        # Pastille de statut (cercle plein dessine via QSS border-radius).
        dot = QLabel()
        dot.setFixedSize(dot_sz, dot_sz)
        dot_col = _PHONE_DOT_ONLINE if online else _PHONE_DOT_OFFLINE
        dot.setStyleSheet(
            f"background:{dot_col}; border-radius:{dot_sz // 2}px;"
        )
        lay.addWidget(dot)

        # [D5] Avatar : insere SEULEMENT si on a des bytes valides.
        # Pas de photo -> rien (spec : on ne reserve pas d'emplacement).
        self._avatar = None
        if photo_bytes:
            av_sz = max(20, int(row_h * 0.80))
            self._avatar = _AvatarWidget(av_sz, self)
            self._avatar.set_photo_bytes(photo_bytes)
            if self._avatar.has_photo():
                lay.addWidget(self._avatar)
            else:
                # Bytes corrompus : on ne montre rien.
                self._avatar.deleteLater()
                self._avatar = None

        # Nom du contact.
        name = QLabel(pseudo)
        name_col = _PHONE_NAME_ONLINE if online else _PHONE_NAME_OFFLINE
        weight = "600" if online else "400"
        name.setStyleSheet(
            f"color:{name_col}; font-size:10pt; font-weight:{weight}; "
            "background:transparent;"
        )
        lay.addWidget(name, stretch=1)

        # Actions en bout de ligne.
        # Refs exposees pour la navigation clavier (D-pad). None si l'icone
        # n'existe pas sur cette ligne (ex: contact deconnecte = pas
        # d'icones phone/letter).
        self._ic_phone = None
        self._ic_letter = None
        if self._mode == "message":
            # Mode MESSAGERIE : aucune icone d'action. La LIGNE entiere est
            # cliquable et ouvre la conversation avec ce contact (sig_message).
            # On signale les non-lus par une pastille rouge en bout de ligne.
            if unread:
                badge = QLabel()
                bsz = max(8, int(row_h * 0.26))
                badge.setFixedSize(bsz, bsz)
                badge.setStyleSheet(
                    f"background:#e5484d; border-radius:{bsz // 2}px;")
                lay.addWidget(badge)
            self.setCursor(Qt.PointingHandCursor)
        elif self._mode == "call":
            # Onglet Appels > Menu : la LIGNE entiere appelle le contact (si
            # connecte). Hors-ligne -> croix "oublier" (non appelable).
            if online:
                ic_phone = _PhoneIconLabel("phone", icon_sz, True, self)
                ic_phone.sig_clicked.connect(
                    lambda: self.sig_call.emit(self._pseudo))
                lay.addWidget(ic_phone)
                self._ic_phone = ic_phone
                self.setCursor(Qt.PointingHandCursor)
            else:
                ic_forget = _PhoneIconLabel("forget", icon_sz, True, self)
                ic_forget.sig_clicked.connect(
                    lambda: self.sig_forget.emit(self._pseudo))
                lay.addWidget(ic_forget)
        elif online:
            # Connecte : telephone + lettre, tous deux cliquables.
            ic_phone = _PhoneIconLabel("phone", icon_sz, True, self)
            ic_phone.sig_clicked.connect(
                lambda: self.sig_call.emit(self._pseudo)
            )
            lay.addWidget(ic_phone)
            self._ic_phone = ic_phone
            ic_letter = _PhoneIconLabel("letter", icon_sz, True, self)
            ic_letter.set_badge(bool(unread))
            ic_letter.sig_clicked.connect(
                lambda: self.sig_message.emit(self._pseudo)
            )
            lay.addWidget(ic_letter)
            self._ic_letter = ic_letter
        else:
            # Deconnecte : seulement la croix "oublier".
            ic_forget = _PhoneIconLabel("forget", icon_sz, True, self)
            ic_forget.sig_clicked.connect(
                lambda: self.sig_forget.emit(self._pseudo)
            )
            lay.addWidget(ic_forget)

    def mousePressEvent(self, ev):
        # Clic sur la ligne : ouvre la conversation (message) ou lance l'appel
        # (call, si le contact est connecte).
        if self._mode == "message":
            try:
                self.sig_message.emit(self._pseudo)
            except Exception:
                pass
            ev.accept()
            return
        if self._mode == "call" and self._online:
            try:
                self.sig_call.emit(self._pseudo)
            except Exception:
                pass
            ev.accept()
            return
        super().mousePressEvent(ev)

    def pseudo(self) -> str:
        """Pseudo du contact de cette ligne."""
        return self._pseudo

    def is_online(self) -> bool:
        """True si le contact est connecte (donc navigable au D-pad)."""
        return self._online

    def set_nav_highlight(self, selected: bool, action: int = 0):
        """Surbrillance navigation clavier.
          selected : True si cette ligne est la ligne courante du D-pad.
          action   : 0 = Appeler (phone), 1 = Message (letter). Ignore si
                     la ligne n'est pas selectionnee.
        En mode messagerie (pas d'icones), on surligne le FOND de la ligne.
        Sinon halo sur l'icone de l'action ciblee. Ligne deconnectee : rien."""
        if self._mode in ("message", "call"):
            self.setStyleSheet(
                "background:rgba(63,185,80,0.22); border-radius:8px;"
                if selected else "background:transparent;")
            return
        if self._ic_phone is not None:
            self._ic_phone.set_nav_selected(selected and action == 0)
        if self._ic_letter is not None:
            self._ic_letter.set_nav_selected(selected and action == 1)


class _CallHistoryRow(QWidget):
    """Une ligne du journal d'appels (onglet Appels > Historique) : fleche de
    sens (sortant/entrant), nom du pair, issue + heure. Cliquable / navigable
    pour rappeler le contact."""

    sig_call = Signal(str)

    def __init__(self, peer: str, direction: str, outcome: str, ts: float,
                 row_h: int, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._peer = peer
        self.setFixedHeight(row_h)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(8)

        missed = outcome in ("missed", "declined", "busy", "offline")
        arrow = "\u2197" if direction == "out" else "\u2199"  # ↗ sortant ↙ entrant
        col = "#e5484d" if missed else "#3fb950"
        ic = QLabel(arrow)
        ic.setStyleSheet(
            f"color:{col}; font-size:13pt; font-weight:700; "
            "background:transparent;")
        lay.addWidget(ic)

        name = QLabel(peer or "?")
        name.setStyleSheet(
            f"color:{_PHONE_NAME_ONLINE}; font-size:10pt; font-weight:600; "
            "background:transparent;")
        lay.addWidget(name, stretch=1)

        sub = QLabel(self._fmt(direction, outcome, ts))
        sub.setStyleSheet(
            "color:#9aa0a6; font-size:8pt; background:transparent;")
        lay.addWidget(sub)
        self.setCursor(Qt.PointingHandCursor)

    @staticmethod
    def _fmt(direction: str, outcome: str, ts: float) -> str:
        lbl = {
            "answered": "répondu",
            "missed":   "sans réponse" if direction == "out" else "manqué",
            "declined": "refusé",
            "busy":     "occupé",
            "offline":  "hors ligne",
        }.get(outcome, outcome)
        when = ""
        try:
            t = time.localtime(float(ts))
            now = time.localtime()
            hm = time.strftime("%H:%M", t)
            if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
                when = hm
            else:
                when = time.strftime("%d/%m", t) + " " + hm
        except Exception:
            pass
        return f"{lbl} · {when}" if when else lbl

    def pseudo(self) -> str:
        return self._peer

    def is_online(self) -> bool:
        return True

    def set_nav_highlight(self, selected: bool, action: int = 0):
        self.setStyleSheet(
            "background:rgba(63,185,80,0.22); border-radius:8px;"
            if selected else "background:transparent;")

    def mousePressEvent(self, ev):
        try:
            self.sig_call.emit(self._peer)
        except Exception:
            pass
        ev.accept()


class _ScreenshotRow(QWidget):
    """Une capture dans la page Screenshots : vignette (ratio conserve) +
    nom de fichier + date. Cliquer = choisir cette image a envoyer."""

    sig_pick = Signal(str)

    def __init__(self, path: str, row_h: float, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._path = path
        self.setFixedHeight(int(row_h))
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(8)

        thumb = QLabel()
        th = int(row_h - 10)
        tw = int(th * 16 / 9)
        thumb.setFixedSize(tw, th)
        thumb.setStyleSheet(
            "background:#e8eaed; border-radius:6px;")
        thumb.setAlignment(Qt.AlignCenter)
        try:
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(
                    tw, th, Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation))
                thumb.setScaledContents(False)
        except Exception:
            pass
        lay.addWidget(thumb)

        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(os.path.basename(path))
        name.setStyleSheet(
            f"color:{_PHONE_NAME_ONLINE}; font-size:8pt; font-weight:600; "
            "background:transparent;")
        try:
            name.setText(name.fontMetrics().elidedText(
                os.path.basename(path), Qt.ElideMiddle, 140))
        except Exception:
            pass
        col.addWidget(name)
        date = QLabel()
        try:
            mt = os.path.getmtime(path)
            date.setText(time.strftime("%d/%m/%y %H:%M", time.localtime(mt)))
        except Exception:
            pass
        date.setStyleSheet(
            "color:#9aa0a6; font-size:7pt; background:transparent;")
        col.addWidget(date)
        col.addStretch(1)
        lay.addLayout(col, stretch=1)

    def mousePressEvent(self, ev):
        try:
            self.sig_pick.emit(self._path)
        except Exception:
            pass
        ev.accept()

    def path(self) -> str:
        return self._path

    def set_nav_highlight(self, selected: bool):
        self.setStyleSheet(
            "background:rgba(63,185,80,0.22); border-radius:8px;"
            if selected else "background:transparent;")


class _PhoneMessageInput(QTextEdit):
    """Champ de saisie multi-lignes pour la messagerie. Compact a vide
    (~1.5 ligne), grandit jusqu'a 4 lignes max quand on tape, puis scroll
    interne. Entree envoie le message (sig_submit), Shift+Entree insere
    une nouvelle ligne. Limite la longueur a max_chars caracteres."""

    sig_submit  = Signal()
    sig_changed = Signal(str)

    # Styles QSS : normal vs selectionne par la navigation clavier (bordure
    # bleue accent + fond legerement teinte, coherent avec le carre des
    # icones).
    _QSS_NORMAL = (
        "QTextEdit { background:#f0f1f3; color:#1a1a1a; "
        "font-size:10pt; border:1px solid #d0d3d7; border-radius:6px; "
        "padding:4px 6px; }"
    )
    _QSS_NAV_SEL = (
        "QTextEdit { background:#eaf0fd; color:#1a1a1a; "
        "font-size:10pt; border:1px solid %s; border-radius:6px; "
        "padding:4px 6px; }" % _PHONE_ACCENT
    )

    def set_nav_selected(self, on: bool):
        """Surbrillance navigation clavier : bordure accent quand le champ
        est la cible courante du D-pad (avant d'y entrer pour taper)."""
        self.setStyleSheet(self._QSS_NAV_SEL if on else self._QSS_NORMAL)

    def __init__(self, max_chars: int = 500, parent=None):
        super().__init__(parent)
        self._max_chars = max_chars
        self._silent = False
        self.setAcceptRichText(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet(self._QSS_NORMAL)
        # Hauteur initiale ~ 1.5 ligne.
        fm = self.fontMetrics()
        self._line_h = fm.lineSpacing()
        self.setFixedHeight(self._line_h + 14)
        self._max_height = self._line_h * 4 + 14
        self.textChanged.connect(self._on_text_changed)

    def _on_text_changed(self):
        # Tronquer si depasse max_chars.
        txt = self.toPlainText()
        if len(txt) > self._max_chars:
            txt = txt[:self._max_chars]
            self.blockSignals(True)
            self.setPlainText(txt)
            # Replacer le curseur a la fin.
            c = self.textCursor()
            c.movePosition(QTextCursor.End)
            self.setTextCursor(c)
            self.blockSignals(False)
        # Adapter la hauteur (entre 1 ligne + padding et max 4 lignes).
        doc_h = int(self.document().size().height()) + 10
        new_h = max(self._line_h + 14, min(self._max_height, doc_h))
        if new_h != self.height():
            self.setFixedHeight(new_h)
        if not self._silent:
            self.sig_changed.emit(txt)

    def set_text_silent(self, text: str):
        """Remplit le champ SANS emettre sig_changed (utilise pour
        restaurer un brouillon sans declencher de boucle de sauvegarde)."""
        self._silent = True
        try:
            self.setPlainText(text or "")
            # Curseur en fin.
            c = self.textCursor()
            c.movePosition(QTextCursor.End)
            self.setTextCursor(c)
        finally:
            self._silent = False

    def keyPressEvent(self, ev):
        # Entree sans modificateurs : envoyer. Shift+Entree : nouvelle ligne.
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            if ev.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(ev)
            else:
                self.sig_submit.emit()
                return
        else:
            super().keyPressEvent(ev)


class _PhoneMessageBubble(QFrame):
    """Bulle d'un message dans la conversation. is_me=True : a droite,
    couleur accent ; is_me=False : a gauche, couleur grise. Largeur max
    a ~75% de la largeur d'ecran pour laisser de la respiration.

    Le timestamp est affiche en petit en bas a droite de la bulle (style
    messagerie type WhatsApp). Format "JJ/MM HH:MM" (ajout 23/05/2026)."""

    def __init__(self, body: str, is_me: bool, screen_w: int,
                 ts: float = 0.0, parent=None, screen_h: int = 0):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self._img_path = None        # chemin absolu si bulle image (sinon None)
        self._bubble_frame = None    # QFrame stylise (pour la surbrillance nav)
        self._bubble_base_qss = ""   # style de base (restaure a la deselection)
        # On utilise un QHBoxLayout exterieur pour pousser la bulle a
        # gauche ou a droite via un stretch sur l'autre cote.
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Conteneur interne : body + timestamp empiles verticalement, le
        # tout dans un QFrame stylise (la bulle).
        bubble = QFrame(self)
        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(10, 6, 10, 4)
        inner.setSpacing(2)
        # Body : texte, OU image si body = "[img]<fichier>" (screenshot
        # envoye/recu, fichier dans le cache PHONE_IMAGES_DIR).
        lbl_body = QLabel()
        is_img = isinstance(body, str) and body.startswith(PHONE_IMG_PREFIX)
        if is_img:
            fname = body[len(PHONE_IMG_PREFIX):]
            pix = QPixmap()
            try:
                fpath = PHONE_IMAGES_DIR / fname
                if fpath.exists():
                    pix = QPixmap(str(fpath))
                    self._img_path = str(fpath)   # pour l'ouverture plein ecran
            except Exception:
                pass
            if not pix.isNull():
                # Vignette bornee en largeur ET en hauteur (sinon une photo
                # VERTICALE, limitee seulement en largeur, devient enorme).
                # On conserve les proportions et on ne fait que REDUIRE.
                maxw = int(screen_w * 0.62)
                maxh = int(screen_h * 0.42) if screen_h else maxw
                pw, ph = pix.width(), pix.height()
                if pw > 0 and ph > 0:
                    f = min(maxw / pw, maxh / ph, 1.0)
                    if f < 1.0:
                        pix = pix.scaled(max(1, int(pw * f)),
                                         max(1, int(ph * f)),
                                         Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
                lbl_body.setPixmap(pix)
                lbl_body.setStyleSheet(
                    "background:transparent; border-radius:6px;")
            else:
                lbl_body.setText("📷 image indisponible")
                lbl_body.setStyleSheet(
                    "background:transparent; font-size:10pt; "
                    "font-style:italic;")
        else:
            lbl_body.setText(body or "")
            lbl_body.setWordWrap(True)
            lbl_body.setStyleSheet("background:transparent; font-size:12pt;")
        inner.addWidget(lbl_body)
        # Timestamp : format "JJ/MM HH:MM" (ex: "23/05 12:34"). Si ts=0
        # (vieux message sans ts dans le JSON, ou bug), on n'affiche rien
        # plutot que "01/01 01:00" qui serait trompeur.
        ts_text = ""
        if ts and ts > 0:
            try:
                ts_text = time.strftime("%d/%m %H:%M", time.localtime(ts))
            except Exception:
                ts_text = ""
        if ts_text:
            lbl_ts = QLabel(ts_text)
            # Alignement a droite dans tous les cas (style messagerie).
            lbl_ts.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            inner.addWidget(lbl_ts)
        else:
            lbl_ts = None
        # Largeur max sur le conteneur (pas sur le QLabel body, sinon Qt
        # peut wrap differemment).
        bubble.setMaximumWidth(int(screen_w * 0.72))
        # Style de la bulle + couleur du timestamp selon l'expediteur.
        if is_me:
            bubble.setStyleSheet(
                f"QFrame {{ background:{_PHONE_ACCENT}; border-radius:10px; }}"
            )
            lbl_body.setStyleSheet(
                "background:transparent; color:#ffffff; font-size:12pt;"
            )
            if lbl_ts is not None:
                # Timestamp semi-transparent sur fond accent : blanc 60%.
                lbl_ts.setStyleSheet(
                    "background:transparent; color:rgba(255,255,255,160); "
                    "font-size:8pt;"
                )
            outer.addStretch(1)
            outer.addWidget(bubble)
        else:
            bubble.setStyleSheet(
                "QFrame { background:#e8eaed; border-radius:10px; }"
            )
            lbl_body.setStyleSheet(
                "background:transparent; color:#1a1a1a; font-size:12pt;"
            )
            if lbl_ts is not None:
                # Timestamp semi-transparent sur fond clair : gris fonce 55%.
                lbl_ts.setStyleSheet(
                    "background:transparent; color:rgba(26,26,26,140); "
                    "font-size:8pt;"
                )
            outer.addWidget(bubble)
            outer.addStretch(1)

        # Reference + style de base pour la surbrillance de navigation.
        self._bubble_frame = bubble
        self._bubble_base_qss = bubble.styleSheet()

    def image_path(self):
        """Chemin absolu de l'image si la bulle en est une (et fichier
        present), sinon None."""
        return self._img_path

    def set_nav_selected(self, sel: bool):
        """Surbrillance de navigation : liseré bleu autour de la bulle."""
        if self._bubble_frame is None:
            return
        if sel:
            base = self._bubble_base_qss.rstrip().rstrip("}")
            # Injecte une bordure dans le bloc QFrame existant.
            if "border-radius" in base:
                self._bubble_frame.setStyleSheet(
                    base + " border:2px solid #2f6fed; }")
            else:
                self._bubble_frame.setStyleSheet(
                    self._bubble_base_qss
                    + " QFrame { border:2px solid #2f6fed; }")
        else:
            self._bubble_frame.setStyleSheet(self._bubble_base_qss)


class _PhoneCircleButton(QLabel):
    """Bouton rond plein dessine en QPainter, avec une icone vectorielle
    blanche au centre. Utilise pour les actions principales des ecrans
    d'appel : decrocher (vert), raccrocher / refuser (rouge).
      kind : "phone_acc"  (combine droit, decrocher)
             "phone_hang" (combine penche, raccrocher / refuser)
    """

    sig_clicked = Signal()

    def __init__(self, kind: str, bg_color: str, size: int, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._bg = bg_color
        self._sz = size
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.sig_clicked.emit()
        super().mousePressEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = self._sz
        # Cercle de fond.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._bg))
        p.drawEllipse(0, 0, s, s)
        # TODO REVERT (icone v2) : icone combine "fluide" en plein, blanc
        # sur fond colore. Memes courbes que _PhoneIconLabel proposition 2.
        # Pour le bouton "raccrocher", on pivote l'icone de 135° (combine
        # basculé). Pour revenir a l'ancien arc + 2 ronds : restaurer
        # l'ancien bloc drawArc + 2 drawEllipse.
        from PySide6.QtGui import QPainterPath
        # Le combine est dessine dans un viewport 80x80 source, mais la
        # forme reelle occupe x=12..80 (largeur 68) et y=12..74 (hauteur 62).
        # Son centre est donc en (46, 43) et NON (40, 40). Pour bien le
        # centrer dans le bouton rond, on doit donc decaler en consequence.
        icon_scale = (s * 0.72) / 80.0    # echelle viewport -> bouton
        # Centre voulu dans le bouton : (s/2, s/2)
        # Centre actuel de l'icone apres scale : (46 * icon_scale, 43 * icon_scale)
        # Offset = (s/2) - (centre_icone_apres_scale)
        ox = s / 2.0 - 46 * icon_scale
        oy = s / 2.0 - 43 * icon_scale
        def P(x, y):
            return ox + x * icon_scale, oy + y * icon_scale
        path = QPainterPath()
        x, y = P(18, 18); path.moveTo(x, y)
        cx1, cy1 = P(18, 12); ex, ey = P(24, 12); path.quadTo(cx1, cy1, ex, ey)
        ex, ey = P(32, 12); path.lineTo(ex, ey)
        cx1, cy1 = P(38, 12); ex, ey = P(38, 18); path.quadTo(cx1, cy1, ex, ey)
        ex, ey = P(38, 24); path.lineTo(ex, ey)
        cx1, cy1 = P(38, 30); ex, ey = P(34, 32); path.quadTo(cx1, cy1, ex, ey)
        cx1, cy1 = P(32, 33); ex, ey = P(32, 36); path.quadTo(cx1, cy1, ex, ey)
        cx1, cy1 = P(32, 44); ex, ey = P(40, 52); path.quadTo(cx1, cy1, ex, ey)
        cx1, cy1 = P(48, 60); ex, ey = P(56, 60); path.quadTo(cx1, cy1, ex, ey)
        cx1, cy1 = P(59, 60); ex, ey = P(60, 58); path.quadTo(cx1, cy1, ex, ey)
        cx1, cy1 = P(62, 54); ex, ey = P(68, 54); path.quadTo(cx1, cy1, ex, ey)
        ex, ey = P(74, 54); path.lineTo(ex, ey)
        cx1, cy1 = P(80, 54); ex, ey = P(80, 60); path.quadTo(cx1, cy1, ex, ey)
        ex, ey = P(80, 68); path.lineTo(ex, ey)
        cx1, cy1 = P(80, 74); ex, ey = P(74, 74); path.quadTo(cx1, cy1, ex, ey)
        cx1, cy1 = P(50, 74); ex, ey = P(30, 54); path.quadTo(cx1, cy1, ex, ey)
        cx1, cy1 = P(18, 38); ex, ey = P(18, 28); path.quadTo(cx1, cy1, ex, ey)
        path.closeSubpath()
        # Bouton "raccrocher" : on pivote l'icone 135° autour du centre.
        if self._kind == "phone_hang":
            p.save()
            p.translate(s / 2, s / 2)
            p.rotate(135)
            p.translate(-s / 2, -s / 2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawPath(path)
        if self._kind == "phone_hang":
            p.restore()
        p.end()


class _PhoneToggleButton(QLabel):
    """Bouton circulaire toggleable (mute micro / haut-parleur). Change de
    couleur de fond entre l'etat inactif et actif. Une icone vectorielle
    (microphone ou haut-parleur) est dessinee par-dessus.
      kind : "mic"     (microphone, barre quand actif = mute)
             "speaker" (haut-parleur)
    """

    sig_toggled = Signal(bool)   # emis avec le nouvel etat actif

    def __init__(self, kind: str, color_off: str, color_on: str,
                 size: int, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._color_off = color_off
        self._color_on  = color_on
        self._sz = size
        self._active = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool, emit: bool = True):
        """Force l'etat actif/inactif. Si emit=False, ne reemet pas le
        signal (utilise au reset entre 2 appels)."""
        if self._active == bool(active):
            return
        self._active = bool(active)
        self.update()
        if emit:
            self.sig_toggled.emit(self._active)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.set_active(not self._active, emit=True)
        super().mousePressEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = self._sz
        bg = self._color_on if self._active else self._color_off
        # Cercle de fond.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        p.drawEllipse(0, 0, s, s)
        # Icone : la couleur de l'icone contraste avec le fond. Pour le HP
        # actif (fond blanc), on dessine l'icone en noir ; sinon en blanc.
        ic_color = "#1a1a1a" if (self._active and self._kind == "speaker") \
                   else "#ffffff"
        pen = QPen(QColor(ic_color), max(2.0, s * 0.10))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if self._kind == "mic":
            # Microphone : ovale en haut + arc + pied.
            cx = s / 2
            top = s * 0.22
            bot = s * 0.56
            w = s * 0.22
            # Capsule ovale du micro.
            p.setBrush(QColor(ic_color))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(int(cx - w / 2), int(top),
                              int(w), int(bot - top),
                              int(w / 2), int(w / 2))
            # Arc de support + pied.
            p.setBrush(Qt.NoBrush)
            p.setPen(pen)
            arc_left = s * 0.30
            arc_right = s * 0.70
            arc_top = s * 0.46
            arc_bot = s * 0.66
            p.drawArc(int(arc_left), int(arc_top),
                      int(arc_right - arc_left), int(arc_bot - arc_top),
                      180 * 16, 180 * 16)
            p.drawLine(int(cx), int(arc_bot), int(cx), int(s * 0.78))
            # Barre "muted" : ligne diagonale quand actif.
            if self._active:
                pen2 = QPen(QColor(ic_color), max(2.5, s * 0.12))
                pen2.setCapStyle(Qt.RoundCap)
                p.setPen(pen2)
                p.drawLine(int(s * 0.22), int(s * 0.22),
                           int(s * 0.78), int(s * 0.78))
        elif self._kind == "speaker":
            # Haut-parleur : carre trapezoidale + 2 arcs d'ondes.
            p.setBrush(QColor(ic_color))
            p.setPen(Qt.NoPen)
            # Corps du HP (petit rect + trapeze)
            box_l = s * 0.26
            box_r = s * 0.42
            box_t = s * 0.40
            box_b = s * 0.60
            from PySide6.QtGui import QPolygon
            poly = QPolygon([
                QPoint(int(box_l), int(box_t)),
                QPoint(int(box_r), int(box_t)),
                QPoint(int(s * 0.58), int(s * 0.28)),
                QPoint(int(s * 0.58), int(s * 0.72)),
                QPoint(int(box_r), int(box_b)),
                QPoint(int(box_l), int(box_b)),
            ])
            p.drawPolygon(poly)
            # 2 arcs d'ondes a droite.
            pen2 = QPen(QColor(ic_color), max(1.8, s * 0.07))
            pen2.setCapStyle(Qt.RoundCap)
            p.setPen(pen2)
            p.setBrush(Qt.NoBrush)
            p.drawArc(int(s * 0.60), int(s * 0.36),
                      int(s * 0.16), int(s * 0.28),
                      -60 * 16, 120 * 16)
            p.drawArc(int(s * 0.66), int(s * 0.30),
                      int(s * 0.22), int(s * 0.40),
                      -60 * 16, 120 * 16)
        p.end()


class _PhoneNavKeyListener:
    """Listener pynput dedie a la navigation clavier du CircusPhone quand
    l'overlay est ouvert. Capte uniquement les 5 touches D-pad : fleches
    up/down/left/right + enter. Demarre a l'ouverture de l'overlay, arrete
    a la fermeture, pour ne pas reserver ces touches en permanence.

    Tourne dans un thread pynput daemon. Pour chaque touche pertinente, il
    appelle le callback `on_nav(direction)` fourni (direction in
    {'up','down','left','right','enter'}). Le callback DOIT etre thread-safe
    (typiquement : emettre un signal Qt vers le main thread). Calque sur
    DisplayInfoMaskKeyListener (meme modele start/stop, meme normaliseur).

    NB : ZQSD gere le deplacement dans Star Citizen, donc capter les
    fleches pendant que le telephone est ouvert ne gene pas le mouvement
    du joueur. Le listener n'observe que (sans les consommer) les fleches /
    enter ; l'action RETOUR est la touche Retour arriere (remplace Echap,
    qui ouvrait le menu Options de SC). Hors champ de saisie, Retour arriere
    est SUPPRIMEE au niveau du hook Windows tant que le telephone est ouvert,
    pour qu'elle revienne au home sans atteindre SC (cf. win32_event_filter) ;
    dans un champ de saisie, elle reste disponible pour effacer du texte."""

    _NAV_KEYS = {"up", "down", "left", "right", "enter", "esc"}
    # Touches utilisees par les JEUX (apres suppression de ZQSD). Quand un jeu
    # capture-clavier est actif, ces touches lui sont injectees en evenements
    # synthetiques (sans voler le focus a SC) ET supprimees au hook pour ne
    # pas perturber le perso dans SC. NB : on N'inclut PAS backspace/esc ici
    # (geres a part : retour).
    # NB : ni Espace (saut) ni C (s'allonger) ne sont interceptes : ce sont
    # des touches de deplacement SC. V remplace C dans les jeux.
    _GAME_KEYS = {"up", "down", "left", "right", "enter",
                  "p", "r", "f", "t", "v"}
    # norm -> code virtuel Windows, pour la suppression au hook.
    # NB : Espace (0x20) n'est PLUS une touche de jeu/action : c'est la touche
    # de SAUT de SC. Elle n'est jamais interceptee ni avalee -> Entree la
    # remplace partout comme touche de validation.
    _GAME_VKS = {
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "enter": 0x0D,
        "p": 0x50, "r": 0x52, "f": 0x46, "t": 0x54,
        "v": 0x56,
    }

    def __init__(self, on_nav, is_text_field=None,
                 on_game_key=None, is_game_active=None,
                 on_camera_key=None, is_camera_active=None,
                 is_viewer_active=None, on_viewer_close=None):
        self._on_nav = on_nav
        # Predicat thread-safe (lecture d'un bool) : True quand un champ de
        # saisie texte du telephone a le focus (composeur de message). Sert a
        # NE PAS detourner Retour arriere (edition) ni le supprimer cote OS.
        self._is_text_field = is_text_field or (lambda: False)
        # on_game_key(norm) : appele (thread pynput) quand un jeu est actif et
        # qu'une touche de jeu est pressee. L'overlay injecte alors un
        # evenement clavier synthetique dans le jeu (cf. _on_game_key).
        self._on_game_key = on_game_key or (lambda _k: None)
        # is_game_active() : True quand l'app courante est un jeu capture-clavier.
        self._is_game_active = is_game_active or (lambda: False)
        # Camera : fenetre SEPAREE qui n'a pas le focus clavier en jeu (SC le
        # garde). On route donc SES touches (R/Entree/Espace/Retour) via CE
        # listener global, comme pour les jeux.
        self._on_camera_key = on_camera_key or (lambda _k: None)
        self._is_camera_active = is_camera_active or (lambda: False)
        # Visionneuse photo plein ecran (moniteur) : Retour arriere la ferme.
        self._is_viewer_active = is_viewer_active or (lambda: False)
        self._on_viewer_close = on_viewer_close or (lambda: None)
        self._kb_listener = None

    def start(self) -> None:
        if self._kb_listener is not None:
            return
        try:
            from pynput import keyboard as kb
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE NAV] pynput indisponible : {e}")
                except Exception: pass
            return

        def _on_press(key):
            try:
                if _CORE_AVAILABLE:
                    norm = _core._normalize_pynput_key(key)
                else:
                    norm = getattr(key, "name", None)
                    if norm: norm = norm.lower()
                # VISIONNEUSE photo plein ecran ouverte : Retour arriere/Echap
                # la ferme (priorite absolue). Les autres touches passent
                # normalement (SC garde Espace pour sauter, etc.).
                if self._is_viewer_active():
                    if norm in ("backspace", "esc"):
                        self._on_viewer_close()
                        return
                # CAMERA ouverte : elle n'a pas le focus clavier en jeu, donc on
                # lui route ses touches ici (avant tout le reste). R = pivoter,
                # Entree = photo, Retour arriere/Echap = fermer. NB : Espace
                # n'est PLUS une action (c'est la touche de SAUT de SC) : on la
                # laisse filer vers SC.
                if self._is_camera_active():
                    if norm in ("r", "enter"):
                        self._on_camera_key(norm)
                        return
                    if norm in ("backspace", "esc"):
                        self._on_camera_key("esc")
                        return
                # RETOUR ARRIERE = action "retour" du telephone (remplace
                # Echap, qui ouvrait le menu Options de Star Citizen). On
                # reutilise le token interne "esc" pour ne pas toucher les
                # handlers en aval. Dans un champ de saisie, on laisse le
                # QTextEdit effacer (ne pas detourner).
                if norm == "backspace":
                    if self._is_text_field():
                        return
                    norm = "esc"
                elif norm == "esc":
                    # Echap n'est plus le retour general. On ne la conserve
                    # que pour RESSORTIR du champ de saisie (la, l'overlay est
                    # au premier plan -> n'atteint pas SC). Hors champ : ignore.
                    if not self._is_text_field():
                        return
                # JEU actif : router TOUTES ses touches vers le jeu via un
                # evenement synthetique (cf. _on_game_key), SANS voler le
                # premier plan -> SC reste actif (vrai overlay). "esc" (=retour
                # arriere) n'est pas une touche de jeu : il file au handle_back.
                if norm in self._GAME_VKS and self._is_game_active():
                    self._on_game_key(norm)
                    return
                if norm in self._NAV_KEYS:
                    self._on_nav(norm)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try: _core._dbg_log(f"[PHONE NAV] on_press KO : {e}")
                    except Exception: pass

        # Sur Windows : filtre bas-niveau pour SUPPRIMER cote OS la touche de
        # RETOUR (Retour arriere) tant que le telephone est ouvert et qu'on
        # n'est PAS dans un champ de saisie, afin qu'elle n'atteigne pas Star
        # Citizen ni un jeu au focus Qt. On supprime aussi Echap (best-effort :
        # empeche le menu Options de SC ; NB SC en raw-input peut tout de meme
        # la recevoir, d'ou le passage a Retour arriere pour le "retour").
        # suppress_event() bloque la touche cote OS mais laisse _on_press
        # s'executer (-> navigation). Dans un champ, Retour arriere n'est PAS
        # supprimee : le QTextEdit doit pouvoir effacer.
        listener_kwargs = {"on_press": _on_press}
        try:
            import sys as _sys
            if _sys.platform.startswith("win"):
                _VK_ESCAPE = 0x1B
                _VK_BACK = 0x08
                _GAME_VK_SET = set(self._GAME_VKS.values())
                def _win32_filter(msg, data):
                    try:
                        vk = getattr(data, "vkCode", None)
                        lst = self._kb_listener
                        if lst is None:
                            return
                        if vk == _VK_ESCAPE:
                            lst.suppress_event()
                        elif vk == _VK_BACK and not self._is_text_field():
                            lst.suppress_event()
                        elif vk in _GAME_VK_SET and (self._is_game_active()
                                                     or self._is_camera_active()):
                            # Jeu OU camera actif : on AVALE ces touches cote OS
                            # (routees via _on_game_key / _on_camera_key) pour
                            # que SC ne les recoive pas. Espace (0x20) ne fait
                            # PAS partie du set -> il file toujours vers SC
                            # (touche de saut), Entree assurant la validation.
                            lst.suppress_event()
                    except Exception:
                        pass
                listener_kwargs["win32_event_filter"] = _win32_filter
        except Exception:
            pass

        try:
            self._kb_listener = kb.Listener(**listener_kwargs)
            self._kb_listener.daemon = True
            self._kb_listener.start()
            if _CORE_AVAILABLE:
                try: _core._dbg_log("[PHONE NAV] listener demarre (D-pad)")
                except Exception: pass
        except Exception as e:
            self._kb_listener = None
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE NAV] start KO : {e}")
                except Exception: pass

    def stop(self) -> None:
        if self._kb_listener is None:
            return
        try:
            self._kb_listener.stop()
        except Exception:
            pass
        self._kb_listener = None
        if _CORE_AVAILABLE:
            try: _core._dbg_log("[PHONE NAV] listener stoppe")
            except Exception: pass


class PhoneOverlayWindow(QWidget):
    """Overlay smartphone CircusPhone. Fenetre frameless topmost, parent
    MainWindow. D4 etape 1 : chassis + ecran annuaire (liste contacts).

    Apparition animee (montee depuis le bas, 400ms). Position finale aux
    3/4 droite de l'ecran. Taille adaptative a la resolution.

    Le parent (MainWindow) appelle :
      - show_animated()  : affiche avec l'animation de montee
      - hide_animated()  : (re)cache (toggle par re-appui du raccourci)
      - refresh_contacts(): reconstruit la liste depuis annuaire + joueurs
    Et ecoute :
      - sig_call(pseudo)    : l'utilisateur veut appeler ce contact
      - sig_message(pseudo) : l'utilisateur veut ouvrir la messagerie
      - sig_forget(pseudo)  : l'utilisateur veut oublier ce contact
    """

    sig_call    = Signal(str)
    sig_message = Signal(str)
    sig_forget  = Signal(str)
    # Signaux d'appel (ecrans incoming / outgoing / in_call).
    # Pas de payload : MainWindow connait deja l'etat d'appel courant.
    sig_accept_call   = Signal()    # decrocher (ecran incoming)
    sig_decline_call  = Signal()    # refuser   (ecran incoming)
    sig_hangup_call   = Signal()    # annuler   (outgoing) ou raccrocher (in_call)
    sig_mute_toggled  = Signal(bool)   # mute micro toggle (in_call)
    sig_speaker_toggled = Signal(bool) # haut-parleur toggle (in_call)
    # Signaux de la messagerie (D4 etape 3, ecran conversation).
    sig_send_message    = Signal(str, str)  # target, body : clic envoyer
    sig_back_contacts   = Signal()          # fleche retour : revenir a Contacts
    sig_send_image      = Signal(str, str)  # (pseudo, chemin) : envoyer un screenshot
    sig_draft_changed   = Signal(str, str)  # target, draft : texte en cours
    # Reglages profil (D5+) : ecran dedie dans l'overlay. L'engrenage du
    # header Contacts emet sig_settings_clicked, qui bascule sur l'ecran.
    # Tous les boutons internes (choisir, supprimer, zoom +/-, fleches
    # directionnelles, reset, retour) emettent leurs propres signaux que
    # MainWindow connecte au _profile_photos.
    sig_settings_clicked    = Signal()
    sig_settings_back       = Signal()        # fleche retour -> Contacts
    sig_settings_choose     = Signal()        # bouton "Choisir une photo"
    sig_settings_remove     = Signal()        # bouton "Supprimer"
    sig_settings_zoom_in    = Signal()        # bouton +
    sig_settings_zoom_out   = Signal()        # bouton -
    sig_settings_move       = Signal(int, int) # signe (dx, dy) : -1/0/+1
    sig_settings_recenter   = Signal()        # bouton centre du pad
    # Navigation clavier D-pad (thread pynput -> Qt). Emis depuis le thread
    # du listener, recu en main thread Qt (queued connection auto).
    sig_nav_key = Signal(str)   # 'up'|'down'|'left'|'right'|'enter'
    sig_game_key = Signal(str)  # touche de jeu -> injection synthetique au jeu
    sig_camera_key = Signal(str)  # touche camera -> action sur la fenetre photo
    sig_viewer_close = Signal()   # Retour -> fermer la photo plein ecran

    def __init__(self, main_window):
        # Fenetre INDEPENDANTE (parent=None) volontairement : si l'overlay
        # est enfant de la fenetre principale, forcer son premier plan (pour
        # capter le clavier d'un jeu) REACTIVE son proprietaire -> le client
        # remonte par-dessus Star Citizen. En top-level non-possede, seul
        # l'overlay passe au premier plan. On garde self._mw pour le reste.
        super().__init__(
            None,
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint | Qt.Tool,
        )
        self._mw = main_window
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("PhoneOverlay")

        # [D5] Callable injecte par MainWindow pour recuperer les bytes
        # JPEG d'un pair (ou None si pas en cache). Si None, on n'affiche
        # aucun avatar (spec : "vide" = rien, pas de placeholder).
        self._photo_provider = None
        # Pseudos actuellement affiches dans chaque ecran (None si l'ecran
        # n'est pas en train d'afficher quelqu'un). Sert a update_avatar_for
        # pour savoir s'il faut rafraichir tel ou tel avatar quand une
        # nouvelle photo arrive.
        self._current_outgoing_peer: str = ""
        self._current_incoming_peer: str = ""
        self._current_in_call_peer:  str = ""

        # --- Dimensions adaptatives selon la resolution de l'ecran ---
        # On vise une hauteur de telephone ~= 62% de la hauteur ecran,
        # bornee, et une largeur deduite du ratio du mockup (corps 200x440).
        screen = QGuiApplication.primaryScreen()
        try:
            geo = screen.availableGeometry()
            scr_w, scr_h = geo.width(), geo.height()
        except Exception:
            scr_w, scr_h = 1920, 1080
        body_h = int(scr_h * 0.62)
        body_h = max(420, min(760, body_h))         # bornes raisonnables
        body_w = int(body_h * (200.0 / 440.0))      # ratio du mockup
        self._body_w = body_w
        self._body_h = body_h

        # Geometrie interne (reprise des proportions du SVG mockup) :
        #   corps   : 200 x 440, rx 28
        #   ecran   : marge laterale 12, haut 56 (bandeau), bas 16
        sx = body_w / 200.0
        sy = body_h / 440.0
        self._radius      = int(28 * sx)
        self._banner_h    = int(56 * sy)            # hauteur du bandeau
        self._screen_x    = int(12 * sx)
        self._screen_y    = self._banner_h
        self._screen_w    = body_w - 2 * self._screen_x
        self._screen_h    = body_h - self._banner_h - int(16 * sy)
        self._screen_rad  = int(14 * sx)

        # La fenetre fait exactement la taille du corps du telephone.
        self.setFixedSize(body_w, body_h)

        self._anim: QPropertyAnimation | None = None
        self._visible_state = False   # True quand l'overlay est "ouvert"

        # --- Navigation clavier D-pad (ecran contacts uniquement) ---
        # _nav_index : index dans la liste des lignes navigables (contacts
        #   connectes uniquement, car eux seuls ont les actions phone/letter).
        # _nav_action : 0 = Appeler (phone), 1 = Message (letter).
        # _nav_rows : refs des _PhoneContactRow navigables, reconstruites a
        #   chaque refresh_contacts. Liste vide => rien a naviguer.
        self._nav_index = 0
        self._nav_action = 0
        self._nav_rows = []
        # Mode de l'ecran contacts selon l'icone d'accueil qui l'a ouvert :
        #   "full"    = ecran Appels (lignes avec tel + lettre)  [defaut]
        #   "message" = ecran Messagerie (ligne entiere -> conversation)
        self._contacts_mode = "full"
        self._last_contacts_args = None
        # Onglet Appels courant ("menu" | "historique"), cache de l'historique
        # d'appels recu de MainWindow, et lignes navigables de l'historique.
        self._appels_tab = "menu"
        self._call_history_cache = []
        self._history_rows = []
        # Navigation D-pad ecran conversation : 3 cibles cyclables.
        #   _convo_nav_index : 0 = fleche Retour, 1 = champ texte,
        #     2 = Trombone (screenshots), 3 = Envoyer
        #   _convo_in_field  : True quand on a donne le focus au champ pour
        #     taper (les fleches deplacent alors le curseur ; Echap ressort).
        self._convo_nav_index = 0
        self._convo_bubbles = []   # bulles navigables de la conversation
        self._convo_in_field = False
        self._nav_listener = _PhoneNavKeyListener(
            on_nav=lambda d: self.sig_nav_key.emit(d),
            is_text_field=lambda: self._convo_in_field,
            on_game_key=lambda k: self.sig_game_key.emit(k),
            is_game_active=self._is_game_active,
            on_camera_key=lambda k: self.sig_camera_key.emit(k),
            is_camera_active=self._is_camera_active,
            is_viewer_active=self._is_photo_viewer_active,
            on_viewer_close=lambda: self.sig_viewer_close.emit(),
        )
        self.sig_nav_key.connect(self._on_nav_key)
        self.sig_game_key.connect(self._on_game_key)
        self.sig_camera_key.connect(self._on_camera_key)
        self.sig_viewer_close.connect(self._close_photo_viewer)

        # [v0.3] Etat des apps CircusPhone : home + lazy-load + app courante.
        self._page_home = None        # PhoneHome (None = repli v0.2 sans home)
        self._current_app = None      # PhoneApp affichee (None = ecran natif)
        self._app_cache = {}          # APP_ID -> instance (lazy-load)
        # D'ou a-t-on ouvert l'ecran Reglages profil ? "contacts" (roue dentee
        # de l'en-tete Contacts) ou "settings" (app Parametres). Le retour de
        # cet ecran revient a l'origine.
        self._settings_origin = "contacts"
        # D'ou a-t-on ouvert l'app/jeu courant ? None = depuis le home racine
        # (le retour revient au home) ; "games" = depuis le dossier Jeux (le
        # retour revient DANS le dossier, facon iOS).
        self._app_origin = None
        # APP_ID -> classe de jeu, pour resoudre sig_open_app du dossier Jeux.
        # Rempli dans _build_phone_home depuis le registre PHONE_GAMES.
        self._games_by_id = {}
        self._phone_services = None   # PhoneServices, construit dans _build_screen
        self._photo_window = None     # CameraWindow (cas special, fenetre separee)
        self._photo_viewer = None     # PhotoViewerWindow (photo en grand, moniteur)

        self._build_screen()

    # ------------------------------------------------------------------
    # Construction de l'ecran (zone blanche) + son contenu
    # ------------------------------------------------------------------
    def _build_screen(self):
        """Construit le widget de l'ecran (zone blanche) et y place un
        QStackedWidget contenant les 4 ecrans possibles :
          - "contacts" : interface par defaut (annuaire)
          - "outgoing" : appel sortant en cours, ca sonne chez la cible
          - "incoming" : appel entrant, en sonnerie locale
          - "in_call" : appel decroche, conversation en cours
        Le passage d'un ecran a l'autre se fait via show_screen_*().
        Le chassis (corps noir + bandeau) est dessine dans paintEvent."""
        # Widget-ecran : positionne en absolu sur la zone blanche.
        self._screen = QWidget(self)
        self._screen.setGeometry(
            self._screen_x, self._screen_y,
            self._screen_w, self._screen_h,
        )
        self._screen.setObjectName("PhoneScreen")
        self._screen.setStyleSheet(
            f"QWidget#PhoneScreen {{ background:{_PHONE_SCREEN_BG}; "
            f"border-radius:{self._screen_rad}px; }}"
        )
        outer = QVBoxLayout(self._screen)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background:transparent;")
        outer.addWidget(self._stack)

        # Hauteur d'une ligne de contact (utilisee par refresh_contacts).
        self._row_h = max(30, int(self._screen_h * 0.082))

        # Build chacun des 6 ecrans (ordre = ordre d'index dans le stack).
        self._page_contacts = self._build_screen_contacts()
        self._page_outgoing = self._build_screen_outgoing()
        self._page_incoming = self._build_screen_incoming()
        self._page_in_call  = self._build_screen_in_call()
        self._page_convo    = self._build_screen_conversation()
        self._page_settings = self._build_screen_settings()
        self._page_shots    = self._build_screen_screenshots()
        self._stack.addWidget(self._page_contacts)   # idx 0
        self._stack.addWidget(self._page_outgoing)   # idx 1
        self._stack.addWidget(self._page_incoming)   # idx 2
        self._stack.addWidget(self._page_in_call)    # idx 3
        self._stack.addWidget(self._page_convo)      # idx 4
        self._stack.addWidget(self._page_settings)   # idx 5
        self._stack.addWidget(self._page_shots)      # idx 6

        # [v0.3] Ecran d'accueil (home) : grille d'apps sur fond d'ecran.
        # Ajoute APRES les ecrans natifs (le switch se fait par widget, donc
        # l'ordre d'index ne compte pas). Devient l'ecran par defaut.
        self._build_phone_home()
        if self._page_home is not None:
            self._stack.setCurrentWidget(self._page_home)
        else:
            # Repli : modules phone_apps absents -> comportement v0.2
            # (demarrage direct sur Contacts).
            self._stack.setCurrentWidget(self._page_contacts)

    # ------------------------------------------------------------------
    # [v0.3] Home (grille d'apps) + lazy-load + cycle de vie des apps
    # ------------------------------------------------------------------
    def _build_phone_home(self):
        """Construit l'ecran d'accueil + le bundle de services injecte aux
        apps. Tolerant : si les modules phone_apps / registre sont absents,
        laisse _page_home a None (repli v0.2, demarrage sur Contacts)."""
        try:
            from circusvoip_phone_apps import (
                PhoneHome, HomeEntry, PhoneServices, build_app_entries,
            )
            from circusvoip_phone_registry import PHONE_APPS
            try:
                from circusvoip_phone_registry import (
                    PHOTOS_APP, SETTINGS_APP,
                )
            except Exception:
                PHOTOS_APP = SETTINGS_APP = None
            try:
                from circusvoip_phone_registry import PHONE_GAMES
            except Exception:
                PHONE_GAMES = []
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE HOME] modules absents : {e}")
                except Exception: pass
            self._page_home = None
            return

        # Bundle de services : decouple les apps de MainWindow. photo_of est
        # une lambda (resolution differee : le provider peut etre injecte
        # APRES la construction du home).
        feed = getattr(self._mw, "_gamelog_feed", None)
        self._phone_services = PhoneServices(
            log=getattr(self._mw, "_on_log", None),
            send_ws=(_core._ws_send_safe if _CORE_AVAILABLE else None),
            gamelog=feed,
            photo_of=(lambda p: self._photo_provider(p)
                      if self._photo_provider else None),
        )
        # Pseudo local pousse APRES construction (setattr), pour rester
        # compatible avec un circusvoip_phone_apps.py qui n'aurait pas encore
        # le champ my_name (evite une TypeError qui casserait tout le tel).
        try:
            self._phone_services.my_name = (state.my_name if _CORE_AVAILABLE
                                            else None)
        except Exception:
            pass
        # Fournisseur de position live (OCR) pour les apps a proximite
        # physique (ex. tables de billard). Callable -> dict {zone,x,y,z}.
        try:
            self._phone_services.pos_provider = (
                (lambda: state.my_pos) if _CORE_AVAILABLE else None)
        except Exception:
            pass
        # Hook d'affichage d'une photo EN GRAND sur l'ecran de jeu (fenetre
        # separee), fourni a la galerie Photos. setattr defensif (compat avec
        # un circusvoip_phone_apps.py sans le champ view_photo).
        try:
            self._phone_services.view_photo = self._open_photo_viewer
        except Exception:
            pass

        try:
            self._games_by_id = {
                getattr(C, "APP_ID", ""): C for C in PHONE_GAMES
            }
        except Exception:
            self._games_by_id = {}

        # 2 icones ecrans natifs : Appels et Messagerie pointent sur l'ecran
        # contacts unifie, avec l'action D-pad par defaut pre-reglee.
        entries = [
            HomeEntry("calls", "Appels", "\u260E",
                      lambda: self._open_native_contacts(action=0)),
            HomeEntry("msg", "Messagerie", "\u2709",
                      lambda: self._open_native_contacts(action=1)),
        ]
        # Apps v0.3 (lazy-load via la fabrique _launch_app).
        entries += build_app_entries(
            PHONE_APPS, lambda C: (lambda: self._launch_app(C))
        )
        # Camera : cas special (fenetre separee couchee, hors stack/registre).
        entries.append(HomeEntry("photo", "Caméra", "\U0001F4F7",
                                 lambda: self._launch_photo()))
        # Photos : galerie des cliches (app normale du stack). On lit l'icone
        # (polaroids) et le libelle DEPUIS la classe, pas en dur.
        if PHOTOS_APP is not None:
            entries.append(HomeEntry(
                PHOTOS_APP.APP_ID, PHOTOS_APP.APP_NAME, PHOTOS_APP.APP_ICON,
                (lambda C: (lambda: self._launch_app(C)))(PHOTOS_APP)))
        # Parametres : TOUJOURS en dernier.
        if SETTINGS_APP is not None:
            entries.append(HomeEntry(
                SETTINGS_APP.APP_ID, SETTINGS_APP.APP_NAME,
                SETTINGS_APP.APP_ICON,
                (lambda C: (lambda: self._launch_app(C)))(SETTINGS_APP)))

        self._page_home = PhoneHome(
            self._screen_w, self._screen_h, self._screen_rad, entries,
            self._load_phone_wallpaper(),
        )
        self._stack.addWidget(self._page_home)

    def _default_phone_wallpaper_path(self):
        """Chemin du fond d'ecran PAR DEFAUT (image embarquee), ou None.
        'hugolisoir.png' est cherchee D'ABORD dans le dossier des fonds
        (circusvoip_phone_wallpapers/), puis a la racine du client (repli
        historique). Sinon le home retombe sur son degrade genere."""
        try:
            for p in (_BASE_DIR / "circusvoip_phone_wallpapers" / "hugolisoir.png",
                      _BASE_DIR / "hugolisoir.png"):
                if p.exists():
                    return str(p)
        except Exception:
            pass
        return None

    def _default_phone_wallpaper(self):
        """QPixmap du fond par defaut (hugolisoir.png) ou None (-> degrade)."""
        path = self._default_phone_wallpaper_path()
        if path:
            try:
                pm = _load_oriented_pixmap(path)
                if not pm.isNull():
                    return pm
            except Exception:
                pass
        return None

    def _load_phone_wallpaper(self):
        """Charge le fond d'ecran : clé config 'phone_wallpaper' si definie,
        sinon le fond PAR DEFAUT (hugolisoir.png), sinon None (degrade du
        home). Best-effort."""
        try:
            cfg = _core._load_client_cfg() if _CORE_AVAILABLE else {}
            path = (cfg or {}).get("phone_wallpaper")
            if path and Path(path).exists():
                pm = _load_oriented_pixmap(path)
                if not pm.isNull():
                    return pm
        except Exception:
            pass
        return self._default_phone_wallpaper()

    def _phone_wallpaper_dir(self):
        """Dossier des fonds d'ecran utilisateur (cree au besoin)."""
        d = _BASE_DIR / "circusvoip_phone_wallpapers"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    def _choose_phone_wallpaper(self):
        """[v0.3] Tuile « + » : importe une image perso. On la COPIE dans le
        dossier des fonds (pour qu'elle devienne une tuile permanente), on
        l'applique, et on demande a l'app Parametres de rescanner + cocher."""
        from PySide6.QtWidgets import QFileDialog
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "Ajouter un fond d'écran", "",
                "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        except Exception:
            return
        if not path:
            return
        pm = _load_oriented_pixmap(path)
        if pm.isNull():
            return
        dest = path
        try:
            import shutil
            wdir = self._phone_wallpaper_dir()
            src = Path(path)
            cand = wdir / src.name
            n = 1
            while cand.exists() and str(cand) != str(src):
                cand = wdir / (src.stem + f"_{n}" + src.suffix)
                n += 1
            if str(cand) != str(src):
                shutil.copyfile(path, cand)
            dest = str(cand)
        except Exception:
            dest = path
        self._store_and_apply_wallpaper(pm, key=dest, stored_path=dest)
        # Rescanner la grille de l'app Parametres + cocher le nouveau fond.
        try:
            app = self._current_app
            if app is not None and hasattr(app, "rescan"):
                app.rescan(select_key=dest)
        except Exception:
            pass

    def _apply_phone_wallpaper(self, pixmap, key="", path=""):
        """[v0.3] Applique un fond choisi dans l'app Parametres.
          - path != "" : image fichier -> on pointe directement dessus.
          - path == "" : fond propose genere -> on l'enregistre en PNG.
        'key' identifie le fond (nom de preset ou chemin) pour la coche."""
        if pixmap is None:
            return
        try:
            if pixmap.isNull():
                return
        except Exception:
            return
        stored = path
        if not stored:
            try:
                dest = _BASE_DIR / "circusvoip_phone_wallpaper.png"
                if pixmap.save(str(dest), "PNG"):
                    stored = str(dest)
            except Exception:
                stored = None
        if not stored:
            return
        self._store_and_apply_wallpaper(pixmap, key=key, stored_path=stored)

    def _reset_phone_wallpaper(self):
        """[v0.3] Tuile « Défaut » : retire le fond configure -> le home
        revient a son degrade par defaut."""
        try:
            if _CORE_AVAILABLE:
                cfg = _core._load_client_cfg() or {}
                cfg.pop("phone_wallpaper", None)
                cfg.pop("phone_wallpaper_key", None)
                _core._save_client_cfg(cfg)
        except Exception:
            pass
        try:
            if self._page_home is not None:
                self._page_home.set_wallpaper(self._default_phone_wallpaper())
                self._page_home.update()
        except Exception:
            pass
        self._propagate_wallpaper(self._default_phone_wallpaper())

    def _store_and_apply_wallpaper(self, pixmap, key="", stored_path=""):
        """Memorise le fond (chemin + cle pour la coche) et l'applique au home."""
        try:
            if _CORE_AVAILABLE and stored_path:
                cfg = _core._load_client_cfg() or {}
                cfg["phone_wallpaper"] = stored_path
                cfg["phone_wallpaper_key"] = key or stored_path
                _core._save_client_cfg(cfg)
        except Exception:
            pass
        try:
            if self._page_home is not None and pixmap is not None:
                self._page_home.set_wallpaper(pixmap)
                self._page_home.update()
        except Exception:
            pass
        self._propagate_wallpaper(pixmap)

    def _propagate_wallpaper(self, pixmap):
        """Applique le fond aux apps qui hebergent leur propre grille/fond
        (ex. dossier Jeux), pour que le changement soit visible partout, pas
        seulement sur l'accueil."""
        try:
            for inst in self._app_cache.values():
                fn = getattr(inst, "set_wallpaper", None)
                if callable(fn):
                    try:
                        fn(pixmap)
                        inst.update()
                    except Exception:
                        pass
        except Exception:
            pass

    def _open_native_contacts(self, action: int = 0):
        """Ouvre l'ecran contacts depuis le home.
          action 0 = icone Appels    -> mode "call" + onglets Menu/Historique
          action 1 = icone Messagerie -> mode "message" (ligne -> conversation)."""
        self._current_app = None
        is_msg = (action == 1)
        self._contacts_mode = "message" if is_msg else "call"
        self._nav_action = 0
        self._nav_index = 0
        try:
            self._contacts_title.setText("Messages" if is_msg else "Appels")
        except Exception:
            pass
        # Barre d'onglets uniquement en mode Appels.
        self._appels_tabbar.setVisible(not is_msg)
        if is_msg:
            self._history_scroll.setVisible(False)
            self._lbl_history_empty.setVisible(False)
            self._rebuild_contacts()
        else:
            self._appels_tab = "menu"
            self._rebuild_contacts()
            self._set_appels_tab("menu")
        self._stack.setCurrentWidget(self._page_contacts)
        try:
            self._apply_nav_highlight()
        except Exception:
            pass

    def _rebuild_contacts(self):
        """Reconstruit la liste des contacts avec les derniers arguments connus
        (utilise quand on bascule Appels <-> Messagerie sans nouvelle donnee)."""
        args = self._last_contacts_args
        if not args:
            return
        try:
            self.refresh_contacts(**args)
        except Exception:
            pass

    def _set_appels_tab(self, tab: str):
        """Bascule l'onglet Appels (Menu / Historique). On garde TOUJOURS une
        zone scrollable visible (stretch=1) pour que la barre d'onglets reste
        en haut (sinon le layout se recentre)."""
        self._appels_tab = "historique" if tab == "historique" else "menu"
        is_hist = (self._appels_tab == "historique")
        self._contacts_scroll.setVisible(not is_hist)
        self._history_scroll.setVisible(is_hist)
        self._lbl_history_empty.setVisible(False)   # message gere DANS le scroll
        if is_hist:
            self._lbl_empty.setVisible(False)
            self._rebuild_call_history()
            self._nav_rows = list(self._history_rows)
        else:
            self._rebuild_contacts()      # repose _nav_rows = contacts
        self._nav_index = 0
        self._update_tab_styles()
        try:
            self._apply_nav_highlight()
        except Exception:
            pass

    def _update_tab_styles(self):
        for btn, key in ((self._tab_menu_btn, "menu"),
                         (self._tab_hist_btn, "historique")):
            active = (self._appels_tab == key)
            color = _PHONE_DOT_ONLINE if active else "#9aa0a6"
            border = (f"border-bottom:2px solid {_PHONE_DOT_ONLINE};"
                      if active else "border-bottom:2px solid transparent;")
            btn.setStyleSheet(
                "QPushButton { border:none; padding:5px 0; font-size:10pt; "
                f"font-weight:700; background:transparent; color:{color}; "
                f"{border} }}")

    def refresh_call_history(self, history):
        """Recoit l'historique d'appels de MainWindow ; met a jour le cache et
        rafraichit l'onglet Historique s'il est affiche."""
        self._call_history_cache = list(history or [])
        if self._contacts_mode == "call" and self._appels_tab == "historique":
            self._rebuild_call_history()
            self._history_scroll.setVisible(True)
            self._lbl_history_empty.setVisible(False)
            self._nav_rows = list(self._history_rows)
            if self._nav_rows:
                self._nav_index = max(
                    0, min(self._nav_index, len(self._nav_rows) - 1))
            try:
                self._apply_nav_highlight()
            except Exception:
                pass

    def _rebuild_call_history(self):
        """(Re)construit les lignes de l'historique depuis le cache (plus
        recent en haut)."""
        while self._history_layout.count() > 1:
            item = self._history_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        rows = []
        for e in reversed(self._call_history_cache[-50:]):
            if not isinstance(e, dict):
                continue
            row = _CallHistoryRow(
                e.get("peer", ""), e.get("dir", "out"),
                e.get("outcome", ""), e.get("ts", 0.0), self._row_h)
            row.sig_call.connect(self.sig_call)
            self._history_layout.insertWidget(len(rows), row)
            rows.append(row)
        self._history_rows = rows
        if not rows:
            # Liste vide : message centre DANS la zone scrollable (garde la
            # zone visible -> barre d'onglets en haut).
            lbl = QLabel("Aucun appel\nrécent.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                "color:#9aa0a6; font-size:9pt; background:transparent; "
                "padding-top:24px;")
            self._history_layout.insertWidget(0, lbl)

    def _launch_app(self, AppClass):
        """Lance une app : instancie en lazy au 1er appel, l'ajoute au stack,
        l'affiche, appelle on_show. Pour un jeu (CAPTURES_KEYBOARD), force le
        premier plan Windows + focus pour que les frappes (fleches/WASD/P/C)
        arrivent a l'app et non a SC (meme mecanisme que le champ conversation)."""
        # Toute ouverture d'app via le home repart d'une origine "neutre" : le
        # retour ira au home. Seul _open_game_from_folder repositionne ensuite
        # _app_origin = "games" pour qu'un jeu lance DEPUIS le dossier y revienne.
        # (Sans ce reset, une valeur "games" residuelle renvoyait par erreur au
        # dossier Jeux en quittant une autre app comme les Reglages.)
        self._app_origin = None
        try:
            inst = self._app_cache.get(AppClass.APP_ID)
            if inst is None:
                inst = AppClass(self._screen_w, self._screen_h,
                                self._screen_rad, self._phone_services, self)
                self._app_cache[AppClass.APP_ID] = inst
                self._stack.addWidget(inst)
                # Si l'app heberge son propre fond (ex. dossier Jeux), on lui
                # pousse le fond courant des sa creation (sinon elle afficherait
                # le fond par defaut jusqu'au prochain changement).
                _swp = getattr(inst, "set_wallpaper", None)
                if callable(_swp):
                    try:
                        _swp(self._load_phone_wallpaper())
                    except Exception:
                        pass
                # Une app peut demander a revenir au home (ex. "Quitter" d'un
                # menu de jeu). Connexion une seule fois, a la creation.
                try:
                    inst.sig_request_home.connect(self._go_home)
                except Exception:
                    pass
                # App Parametres : delegue ses actions a l'overlay.
                if hasattr(inst, "sig_open_profile"):
                    try:
                        inst.sig_open_profile.connect(
                            self._open_profile_settings_from_app)
                    except Exception:
                        pass
                if hasattr(inst, "sig_open_wallpaper"):
                    try:
                        inst.sig_open_wallpaper.connect(self._choose_phone_wallpaper)
                    except Exception:
                        pass
                if hasattr(inst, "sig_apply_wallpaper"):
                    try:
                        inst.sig_apply_wallpaper.connect(self._apply_phone_wallpaper)
                    except Exception:
                        pass
                if hasattr(inst, "sig_reset_wallpaper"):
                    try:
                        inst.sig_reset_wallpaper.connect(self._reset_phone_wallpaper)
                    except Exception:
                        pass
                # Dossier Jeux : ouvre un jeu (sig_open_app) + recoit la liste
                # des jeux a afficher (set_games) et le fond courant.
                if hasattr(inst, "sig_open_app"):
                    try:
                        inst.sig_open_app.connect(self._open_game_from_folder)
                    except Exception:
                        pass
                if hasattr(inst, "set_games"):
                    try:
                        inst.set_games(list(self._games_by_id.values()))
                    except Exception:
                        pass
                    # Rendu continu avec le home : meme fond d'ecran.
                    try:
                        inst.set_wallpaper(self._load_phone_wallpaper())
                    except Exception:
                        pass
                # App Parametres : dossier des fonds + fond actuellement coche.
                if hasattr(inst, "set_wallpaper_dir"):
                    try:
                        inst.set_wallpaper_dir(str(self._phone_wallpaper_dir()))
                    except Exception:
                        pass
                if hasattr(inst, "set_default_preview"):
                    try:
                        inst.set_default_preview(
                            self._default_phone_wallpaper_path())
                    except Exception:
                        pass
                if hasattr(inst, "set_selected_key"):
                    try:
                        cfg = _core._load_client_cfg() if _CORE_AVAILABLE else {}
                        inst.set_selected_key((cfg or {}).get("phone_wallpaper_key", ""))
                    except Exception:
                        pass
            self._current_app = inst
            self._stack.setCurrentWidget(inst)
            # Rafraichit le pseudo local dans les services (au cas ou ils
            # auraient ete construits avant la connexion au serveur).
            try:
                if self._phone_services is not None and _CORE_AVAILABLE:
                    self._phone_services.my_name = state.my_name
            except Exception:
                pass
            if getattr(AppClass, "CAPTURES_KEYBOARD", False):
                # Plus de vol de premier plan : le jeu recoit ses touches via
                # le listener global (evenements synthetiques, cf. _on_game_key)
                # et ses touches sont supprimees cote OS. SC garde le focus
                # -> vrai overlay, on reste EN JEU. Rien a forcer ici.
                pass
            inst.on_show()
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE APP] launch KO : {e}")
                except Exception: pass

    def _arm_game_confirm_later(self, app, delay_ms: int = 300):
        """Re-arme la garde anti-fuite (_confirm_armed) d'un jeu apres un
        court delai. A l'ouverture via Entree, l'overlay vole le premier plan
        a SC ; le RELACHEMENT de cette Entree est alors souvent delivre a SC
        (qui avait encore le focus a cet instant) et n'atteint pas le jeu, si
        bien que son keyReleaseEvent ne re-arme jamais la garde -> Entree
        morte dans le menu. Ce timer garantit le re-armement independamment du
        focus. L'Entree d'OUVERTURE, elle, a deja ete consommee dans les
        millisecondes suivant le lancement (avant ce delai), donc elle reste
        ignoree comme voulu."""
        try:
            QTimer.singleShot(
                delay_ms,
                lambda a=app: setattr(a, "_confirm_armed", True),
            )
        except Exception:
            pass

    def _open_photo_viewer(self, path):
        """Affiche une photo EN GRAND sur l'ecran de jeu (fenetre separee,
        sans bordure ni fond, 80% de la hauteur, orientation d'origine). Le
        telephone reste visible (la galerie derriere). Retour arriere ferme
        (route par le listener clavier global -> _close_photo_viewer)."""
        try:
            from circusvoip_phone_camera import PhotoViewerWindow
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE VIEWER] module absent : {e}")
                except Exception: pass
            return
        try:
            # Fermer une eventuelle photo deja ouverte avant d'en montrer une.
            if self._photo_viewer is not None:
                try: self._photo_viewer.close()
                except Exception: pass
                self._photo_viewer = None
            win = PhotoViewerWindow(path)
            win._on_closed = self._on_photo_viewer_closed
            self._photo_viewer = win
            # Fenetre PASSIVE : on la montre sans voler le focus a SC.
            win.show()
            win.raise_()
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE VIEWER] open KO : {e}")
                except Exception: pass

    def _is_photo_viewer_active(self):
        w = self._photo_viewer
        try:
            return w is not None and w.isVisible()
        except Exception:
            return False

    def _close_photo_viewer(self):
        w = self._photo_viewer
        if w is None:
            return
        try:
            w.close_viewer()
        except Exception:
            try: w.close()
            except Exception: pass

    def _on_photo_viewer_closed(self):
        self._photo_viewer = None

    def _launch_photo(self):
        """[v0.3] Lance la camera (fenetre separee couchee a viseur evide,
        hors stack) et masque le telephone le temps de la prise de vue. La
        camera gere sa propre fermeture (Echap) ; on rouvre le telephone au
        raccourci habituel."""
        try:
            from circusvoip_phone_camera import CameraWindow
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE PHOTO] module absent : {e}")
                except Exception: pass
            return
        try:
            if self._photo_window is None:
                self._photo_window = CameraWindow(self._phone_services)
            # A la fermeture de la camera : reafficher le telephone.
            self._photo_window._on_closed = self._reshow_after_camera
            # Fenetre d'armement : l'Entree qui OUVRE la camera (maintenue /
            # auto-repetee) ne doit pas declencher de photo.
            self._camera_arm_ts = time.monotonic()
            # Masquer le CHASSIS du telephone SANS fermer l'overlay : le listener
            # clavier global doit RESTER ACTIF pour router les touches camera
            # (hide_animated() l'arreterait -> raccourcis morts). On ne touche
            # donc ni a _visible_state ni au listener : simple hide() de la
            # fenetre.
            if self._current_app is not None:
                try:
                    self._current_app.on_hide()
                except Exception:
                    pass
            self.hide()
            # Camera : fenetre PASSIVE (ne vole pas le focus a SC). On la montre
            # et on la remonte, sans l'activer. Les touches viennent du listener.
            self._photo_window.show()
            self._photo_window.raise_()
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE PHOTO] launch KO : {e}")
                except Exception: pass

    def _reshow_after_camera(self):
        """Reaffiche le telephone quand la camera se ferme. Le listener clavier
        global n'ayant jamais ete arrete, il n'y a rien a redemarrer."""
        try:
            self.show()
            self.raise_()
        except Exception:
            pass

    def _is_camera_active(self) -> bool:
        """True quand la fenetre camera est ouverte (thread listener). Sert a
        router R/Entree/Espace/Retour vers la camera plutot que vers SC/jeu."""
        try:
            return (self._photo_window is not None
                    and self._photo_window.isVisible())
        except Exception:
            return False

    def _on_camera_key(self, norm: str):
        """Touche camera recue via le listener global (thread -> main).
          r            -> pivoter
          enter/space  -> prendre la photo (sauf pendant la fenetre d'armement)
          esc          -> fermer (Retour arriere / Echap)."""
        win = self._photo_window
        if win is None or not win.isVisible():
            return
        try:
            if norm == "r":
                win._rotate()
            elif norm == "enter":
                # Ignore la touche d'ouverture (maintien / auto-repeat) pendant
                # ~0.5 s, puis chaque pression prend une photo.
                if time.monotonic() - getattr(self, "_camera_arm_ts", 0.0) < 0.5:
                    return
                win.take_photo()
            elif norm == "esc":
                win.close()
        except Exception:
            pass

    def _open_game_from_folder(self, app_id):
        """Ouvre un jeu demande par le dossier Jeux (sig_open_app). Resout
        l'APP_ID en classe via le registre, lance le jeu, puis memorise que
        l'origine est le dossier (le retour du jeu y reviendra)."""
        Cls = self._games_by_id.get(app_id)
        if Cls is None:
            return
        self._launch_app(Cls)
        # _launch_app a pu reinitialiser le contexte ; on marque l'origine
        # APRES, pour que le retour (_go_home) revienne dans le dossier.
        self._app_origin = "games"

    def _go_home(self):
        """Retour a l'accueil : met l'app courante en pause (on_hide). Si on
        venait du dossier Jeux ("games"), on revient DANS le dossier ; sinon
        on reaffiche le home du telephone."""
        if self._current_app is not None:
            try:
                self._current_app.on_hide()
            except Exception:
                pass
            self._current_app = None
        # Retour facon iOS : depuis un jeu ouvert via le dossier, revenir au
        # dossier Jeux plutot qu'au home. (Mettre False ci-dessous pour que
        # le retour aille TOUJOURS au home.)
        if self._app_origin == "games":
            self._app_origin = None
            folder = self._app_cache.get("games")
            if folder is not None:
                self._current_app = folder
                self._stack.setCurrentWidget(folder)
                try:
                    folder.on_show()
                except Exception:
                    pass
                return
        self._app_origin = None
        if self._page_home is not None:
            self._stack.setCurrentWidget(self._page_home)
        else:
            self._stack.setCurrentWidget(self._page_contacts)

    # ------------------------------------------------------------------
    # Construction des 4 ecrans
    # ------------------------------------------------------------------
    def _build_screen_contacts(self) -> QWidget:
        """Ecran par defaut : titre 'Contacts' + liste annuaire.
        D5 a venir : bouton engrenage en haut a droite (reglages profil)."""
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 10, 0, 10)
        v.setSpacing(0)

        # Header : titre centre + engrenage a droite (D5 reglages profil).
        # Layout horizontal a 3 zones : spacer-gauche, titre centre, engrenage-droite.
        # Le spacer gauche a la meme largeur que l'engrenage pour que le titre
        # reste reellement centre visuellement.
        header = QWidget()
        header.setStyleSheet("background:transparent;")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(0)
        gear_size = 18
        # Spacer gauche (meme largeur que l'engrenage pour centrer le titre)
        spacer_left = QWidget()
        spacer_left.setFixedSize(gear_size, gear_size)
        spacer_left.setStyleSheet("background:transparent;")
        h.addWidget(spacer_left)
        # Titre centre
        title = QLabel("Contacts")
        self._contacts_title = title
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color:{_PHONE_SCREEN_TXT}; font-size:12pt; font-weight:700; "
            "background:transparent; padding-bottom:6px;"
        )
        h.addWidget(title, stretch=1)
        # Engrenage cliquable (D5 : ouvre la popup Reglages profil)
        gear = _PhoneIconLabel("gear", gear_size, True, header)
        gear.sig_clicked.connect(self._open_profile_settings_from_contacts)
        h.addWidget(gear)
        v.addWidget(header)

        # Barre d'onglets (visible uniquement en mode Appels) : Menu | Historique.
        self._appels_tabbar = QWidget()
        self._appels_tabbar.setStyleSheet("background:transparent;")
        tb = QHBoxLayout(self._appels_tabbar)
        tb.setContentsMargins(10, 0, 10, 4)
        tb.setSpacing(6)
        self._tab_menu_btn = QPushButton("Menu")
        self._tab_hist_btn = QPushButton("Historique")
        for b in (self._tab_menu_btn, self._tab_hist_btn):
            b.setCursor(Qt.PointingHandCursor)
            b.setFlat(True)
            tb.addWidget(b, stretch=1)
        self._tab_menu_btn.clicked.connect(
            lambda: self._set_appels_tab("menu"))
        self._tab_hist_btn.clicked.connect(
            lambda: self._set_appels_tab("historique"))
        self._appels_tabbar.setVisible(False)
        v.addWidget(self._appels_tabbar)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#e3e5e8; background:#e3e5e8; max-height:1px;")
        v.addWidget(sep)

        # Zone scrollable de la liste.
        self._contacts_scroll = QScrollArea()
        self._contacts_scroll.setWidgetResizable(True)
        self._contacts_scroll.setFrameShape(QFrame.NoFrame)
        self._contacts_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self._contacts_scroll.setStyleSheet(
            "QScrollArea { background:transparent; }"
            "QScrollBar:vertical { width:6px; background:transparent; }"
            "QScrollBar::handle:vertical { background:#d0d3d7; "
            "  border-radius:3px; }"
        )
        self._contacts_host = QWidget()
        self._contacts_host.setStyleSheet("background:transparent;")
        self._contacts_layout = QVBoxLayout(self._contacts_host)
        self._contacts_layout.setContentsMargins(0, 4, 0, 4)
        self._contacts_layout.setSpacing(2)
        self._contacts_layout.addStretch(1)
        self._contacts_scroll.setWidget(self._contacts_host)
        v.addWidget(self._contacts_scroll, stretch=1)

        # Liste de l'historique des appels (onglet Historique) : meme style,
        # cachee par defaut (affichee quand on bascule sur l'onglet).
        self._history_scroll = QScrollArea()
        self._history_scroll.setWidgetResizable(True)
        self._history_scroll.setFrameShape(QFrame.NoFrame)
        self._history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._history_scroll.setStyleSheet(
            "QScrollArea { background:transparent; }"
            "QScrollBar:vertical { width:6px; background:transparent; }"
            "QScrollBar::handle:vertical { background:#d0d3d7; "
            "  border-radius:3px; }"
        )
        self._history_host = QWidget()
        self._history_host.setStyleSheet("background:transparent;")
        self._history_layout = QVBoxLayout(self._history_host)
        self._history_layout.setContentsMargins(0, 4, 0, 4)
        self._history_layout.setSpacing(2)
        self._history_layout.addStretch(1)
        self._history_scroll.setWidget(self._history_host)
        self._history_scroll.setVisible(False)
        v.addWidget(self._history_scroll, stretch=1)
        self._lbl_history_empty = QLabel("Aucun appel\nrécent.")
        self._lbl_history_empty.setAlignment(Qt.AlignCenter)
        self._lbl_history_empty.setStyleSheet(
            "color:#9aa0a6; font-size:9pt; background:transparent;")
        self._lbl_history_empty.setVisible(False)
        v.addWidget(self._lbl_history_empty)

        # Etat "liste vide" affiche quand l'annuaire n'a aucun contact.
        self._lbl_empty = QLabel(
            "Annuaire vide.\nLes joueurs croises\napparaitront ici."
        )
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet(
            "color:#9aa0a6; font-size:9pt; background:transparent;"
        )
        self._lbl_empty.setVisible(False)
        v.addWidget(self._lbl_empty)
        return page

    def _build_screen_outgoing(self) -> QWidget:
        """Ecran 'Appel sortant' : ca sonne chez la cible, on attend.
        Petit texte 'Appel en cours' + gros nom + bouton raccrocher rouge.
        Fond noir (cf spec utilisateur D4 finition) pour donner une
        signature visuelle distincte au mode appel."""
        page = QWidget()
        page.setObjectName("PhoneScreenCallOutgoing")
        page.setStyleSheet(
            "QWidget#PhoneScreenCallOutgoing { "
            f"background:{_PHONE_BODY_COLOR}; "
            f"border-radius:{self._screen_rad}px; }}"
        )
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 20, 0, 20)
        v.setSpacing(0)

        lbl_top = QLabel("Appel en cours")
        lbl_top.setAlignment(Qt.AlignCenter)
        lbl_top.setStyleSheet(
            "color:#c2c6cb; font-size:10pt; font-weight:500; "
            "background:transparent;"
        )
        v.addWidget(lbl_top)

        v.addStretch(1)
        # [D5] Avatar : visible seulement si on a la photo du correspondant.
        # Pour rester cliquable cote spec ("rien si pas de photo"), on cache
        # tout le widget via setVisible quand pas de photo.
        self._av_outgoing = _AvatarWidget(int(self._screen_w * 0.60), page)
        self._av_outgoing.setVisible(False)
        row_av = QHBoxLayout()
        row_av.addStretch(1)
        row_av.addWidget(self._av_outgoing)
        row_av.addStretch(1)
        v.addLayout(row_av)
        v.addSpacing(8)
        self._lbl_outgoing_name = QLabel("")
        self._lbl_outgoing_name.setAlignment(Qt.AlignCenter)
        self._lbl_outgoing_name.setStyleSheet(
            "color:#ffffff; font-size:18pt; font-weight:700; "
            "background:transparent;"
        )
        self._lbl_outgoing_name.setWordWrap(True)
        v.addWidget(self._lbl_outgoing_name)

        lbl_anim = QLabel("...")
        lbl_anim.setAlignment(Qt.AlignCenter)
        lbl_anim.setStyleSheet(
            "color:#888888; font-size:14pt; background:transparent;"
        )
        v.addWidget(lbl_anim)
        v.addStretch(2)

        # Bouton raccrocher rouge (annule l'appel sortant).
        h = QHBoxLayout()
        h.addStretch(1)
        btn = _PhoneCircleButton("phone_hang", _PHONE_BTN_HANGUP,
                                 self._action_btn_size())
        btn.sig_clicked.connect(self.sig_hangup_call)
        h.addWidget(btn)
        h.addStretch(1)
        v.addLayout(h)

        # Placeholder pour le raccourci (sera branche a l'etape raccourcis).
        self._lbl_outgoing_shortcut = QLabel("—")
        self._lbl_outgoing_shortcut.setAlignment(Qt.AlignCenter)
        self._lbl_outgoing_shortcut.setStyleSheet(
            "color:#9aa0a6; font-size:8pt; background:transparent;"
        )
        v.addWidget(self._lbl_outgoing_shortcut)
        return page

    def _build_screen_incoming(self) -> QWidget:
        """Ecran 'Appel entrant' : 'appel entrant' en haut, gros nom au
        milieu, 2 boutons en bas (vert decrocher / rouge refuser) avec
        leur raccourci affiche dessous (placeholder en attendant l'etape
        raccourcis). Fond noir (cf D4 finition)."""
        page = QWidget()
        page.setObjectName("PhoneScreenCallIncoming")
        page.setStyleSheet(
            "QWidget#PhoneScreenCallIncoming { "
            f"background:{_PHONE_BODY_COLOR}; "
            f"border-radius:{self._screen_rad}px; }}"
        )
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 20, 0, 20)
        v.setSpacing(0)

        lbl_top = QLabel("Appel entrant")
        lbl_top.setAlignment(Qt.AlignCenter)
        lbl_top.setStyleSheet(
            "color:#c2c6cb; font-size:10pt; font-weight:500; "
            "background:transparent;"
        )
        v.addWidget(lbl_top)

        v.addStretch(1)
        # [D5] Avatar : visible seulement si on a la photo de l'appelant.
        self._av_incoming = _AvatarWidget(int(self._screen_w * 0.60), page)
        self._av_incoming.setVisible(False)
        row_av_in = QHBoxLayout()
        row_av_in.addStretch(1)
        row_av_in.addWidget(self._av_incoming)
        row_av_in.addStretch(1)
        v.addLayout(row_av_in)
        v.addSpacing(8)
        self._lbl_incoming_name = QLabel("")
        self._lbl_incoming_name.setAlignment(Qt.AlignCenter)
        self._lbl_incoming_name.setStyleSheet(
            "color:#ffffff; font-size:18pt; font-weight:700; "
            "background:transparent;"
        )
        self._lbl_incoming_name.setWordWrap(True)
        v.addWidget(self._lbl_incoming_name)
        v.addStretch(2)

        # 2 boutons cote a cote : vert decrocher | rouge refuser.
        btn_sz = self._action_btn_size()
        h = QHBoxLayout()
        h.setSpacing(int(self._screen_w * 0.08))

        col_acc = QVBoxLayout()
        col_acc.setAlignment(Qt.AlignCenter)
        b_acc = _PhoneCircleButton("phone_acc", _PHONE_BTN_ACCEPT, btn_sz)
        b_acc.sig_clicked.connect(self.sig_accept_call)
        col_acc.addWidget(b_acc, alignment=Qt.AlignCenter)
        self._lbl_incoming_acc_shortcut = QLabel("—")
        self._lbl_incoming_acc_shortcut.setAlignment(Qt.AlignCenter)
        self._lbl_incoming_acc_shortcut.setStyleSheet(
            "color:#9aa0a6; font-size:8pt; background:transparent;"
        )
        col_acc.addWidget(self._lbl_incoming_acc_shortcut)

        col_dec = QVBoxLayout()
        col_dec.setAlignment(Qt.AlignCenter)
        b_dec = _PhoneCircleButton("phone_hang", _PHONE_BTN_HANGUP, btn_sz)
        b_dec.sig_clicked.connect(self.sig_decline_call)
        col_dec.addWidget(b_dec, alignment=Qt.AlignCenter)
        self._lbl_incoming_dec_shortcut = QLabel("—")
        self._lbl_incoming_dec_shortcut.setAlignment(Qt.AlignCenter)
        self._lbl_incoming_dec_shortcut.setStyleSheet(
            "color:#9aa0a6; font-size:8pt; background:transparent;"
        )
        col_dec.addWidget(self._lbl_incoming_dec_shortcut)

        h.addStretch(1)
        h.addLayout(col_acc)
        h.addLayout(col_dec)
        h.addStretch(1)
        v.addLayout(h)
        return page

    def _build_screen_in_call(self) -> QWidget:
        """Ecran 'En appel' : nom du correspondant + 3 boutons en bas
        (raccrocher / mute / haut-parleur). Mute et HP sont toggleables
        avec un style visuel actif/inactif (couleur du logo).
        Fond noir (cf D4 finition)."""
        page = QWidget()
        page.setObjectName("PhoneScreenCallInCall")
        page.setStyleSheet(
            "QWidget#PhoneScreenCallInCall { "
            f"background:{_PHONE_BODY_COLOR}; "
            f"border-radius:{self._screen_rad}px; }}"
        )
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 20, 0, 20)
        v.setSpacing(0)

        lbl_top = QLabel("En appel avec")
        lbl_top.setAlignment(Qt.AlignCenter)
        lbl_top.setStyleSheet(
            "color:#c2c6cb; font-size:10pt; font-weight:500; "
            "background:transparent;"
        )
        v.addWidget(lbl_top)

        v.addStretch(1)
        # [D5] Avatar : visible seulement si on a la photo du correspondant.
        self._av_in_call = _AvatarWidget(int(self._screen_w * 0.60), page)
        self._av_in_call.setVisible(False)
        row_av_ic = QHBoxLayout()
        row_av_ic.addStretch(1)
        row_av_ic.addWidget(self._av_in_call)
        row_av_ic.addStretch(1)
        v.addLayout(row_av_ic)
        v.addSpacing(8)
        self._lbl_in_call_name = QLabel("")
        self._lbl_in_call_name.setAlignment(Qt.AlignCenter)
        self._lbl_in_call_name.setStyleSheet(
            "color:#ffffff; font-size:18pt; font-weight:700; "
            "background:transparent;"
        )
        self._lbl_in_call_name.setWordWrap(True)
        v.addWidget(self._lbl_in_call_name)
        v.addStretch(2)

        # 3 boutons : mute | raccrocher | haut-parleur. Le raccrocher au
        # centre pour qu'il soit toujours dominant visuellement.
        btn_sz = self._action_btn_size()
        btn_sz_small = int(btn_sz * 0.82)
        h = QHBoxLayout()
        h.setSpacing(int(self._screen_w * 0.05))

        # Mute micro (toggleable). Logo gris quand inactif (micro ouvert),
        # rouge quand actif (micro coupe).
        col_mute = QVBoxLayout()
        col_mute.setAlignment(Qt.AlignCenter)
        self._btn_mute = _PhoneToggleButton(
            "mic", _PHONE_BTN_TOGGLE_OFF, _PHONE_BTN_TOGGLE_ON, btn_sz_small
        )
        self._btn_mute.sig_toggled.connect(self.sig_mute_toggled)
        col_mute.addWidget(self._btn_mute, alignment=Qt.AlignCenter)
        self._lbl_mute_shortcut = QLabel("—")
        self._lbl_mute_shortcut.setAlignment(Qt.AlignCenter)
        self._lbl_mute_shortcut.setStyleSheet(
            "color:#9aa0a6; font-size:8pt; background:transparent;"
        )
        col_mute.addWidget(self._lbl_mute_shortcut)

        # Raccrocher.
        col_hang = QVBoxLayout()
        col_hang.setAlignment(Qt.AlignCenter)
        b_hang = _PhoneCircleButton("phone_hang", _PHONE_BTN_HANGUP, btn_sz)
        b_hang.sig_clicked.connect(self.sig_hangup_call)
        col_hang.addWidget(b_hang, alignment=Qt.AlignCenter)
        self._lbl_hang_shortcut = QLabel("—")
        self._lbl_hang_shortcut.setAlignment(Qt.AlignCenter)
        self._lbl_hang_shortcut.setStyleSheet(
            "color:#9aa0a6; font-size:8pt; background:transparent;"
        )
        col_hang.addWidget(self._lbl_hang_shortcut)

        # Haut-parleur (toggleable). Gris inactif, blanc actif.
        col_sp = QVBoxLayout()
        col_sp.setAlignment(Qt.AlignCenter)
        self._btn_speaker = _PhoneToggleButton(
            "speaker", _PHONE_BTN_TOGGLE_OFF, _PHONE_BTN_TOGGLE_SPEAKER,
            btn_sz_small,
        )
        self._btn_speaker.sig_toggled.connect(self.sig_speaker_toggled)
        col_sp.addWidget(self._btn_speaker, alignment=Qt.AlignCenter)
        self._lbl_sp_shortcut = QLabel("—")
        self._lbl_sp_shortcut.setAlignment(Qt.AlignCenter)
        self._lbl_sp_shortcut.setStyleSheet(
            "color:#9aa0a6; font-size:8pt; background:transparent;"
        )
        col_sp.addWidget(self._lbl_sp_shortcut)

        h.addStretch(1)
        h.addLayout(col_mute)
        h.addLayout(col_hang)
        h.addLayout(col_sp)
        h.addStretch(1)
        v.addLayout(h)
        return page

    def _action_btn_size(self) -> int:
        """Taille standard d'un bouton d'action (raccrocher / decrocher).
        Calculee a partir de la largeur d'ecran pour rester adaptative."""
        return max(36, int(self._screen_w * 0.22))

    def _build_screen_conversation(self) -> QWidget:
        """Ecran de conversation privee : en haut une fleche retour + nom
        du contact, au milieu la liste des messages (bulles), en bas un
        champ de saisie multi-lignes + bouton envoyer. D4 etape 3."""
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        # --- Bandeau du haut : fleche retour + [avatar] + nom du correspondant ---
        top = QHBoxLayout()
        top.setSpacing(4)
        self._convo_back = _PhoneIconLabel(
            "back", max(16, int(self._row_h * 0.7)), True
        )
        self._convo_back.sig_clicked.connect(self.sig_back_contacts)
        top.addWidget(self._convo_back)
        # [D5] Avatar : visible seulement si on a la photo du correspondant.
        av_sz = max(20, int(self._row_h * 0.80))
        self._av_convo = _AvatarWidget(av_sz, page)
        self._av_convo.setVisible(False)
        top.addWidget(self._av_convo)
        self._convo_title = QLabel("")
        self._convo_title.setStyleSheet(
            f"color:{_PHONE_NAME_ONLINE}; font-size:11pt; font-weight:700; "
            "background:transparent;"
        )
        self._convo_title.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        top.addWidget(self._convo_title, stretch=1)
        v.addLayout(top)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#e3e5e8; background:#e3e5e8; max-height:1px;")
        v.addWidget(sep)

        # --- Zone scrollable des messages (bulles) ---
        self._convo_scroll = QScrollArea()
        self._convo_scroll.setWidgetResizable(True)
        self._convo_scroll.setFrameShape(QFrame.NoFrame)
        self._convo_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self._convo_scroll.setStyleSheet(
            "QScrollArea { background:transparent; }"
            "QScrollBar:vertical { width:6px; background:transparent; }"
            "QScrollBar::handle:vertical { background:#d0d3d7; "
            "  border-radius:3px; }"
        )
        self._convo_host = QWidget()
        self._convo_host.setStyleSheet("background:transparent;")
        self._convo_layout = QVBoxLayout(self._convo_host)
        self._convo_layout.setContentsMargins(2, 2, 2, 2)
        self._convo_layout.setSpacing(4)
        self._convo_layout.addStretch(1)   # pousse les bulles vers le bas
        self._convo_scroll.setWidget(self._convo_host)
        v.addWidget(self._convo_scroll, stretch=1)

        # Auto-scroll en bas robuste (ajout 25/05/2026 Kainan).
        # Avant : un QTimer.singleShot(0, ...) dans refresh_conversation
        # tentait de scroller a la fin, mais Qt n'avait pas encore mis a
        # jour scrollbar.maximum() au moment du timer (le layout pass
        # n'avait pas eu lieu), donc le scroll restait en haut.
        # Fix : on se branche sur le signal rangeChanged du scrollbar qui
        # se declenche APRES que Qt a calcule la nouvelle plage (= apres
        # le layout pass). Et on utilise un flag _convo_should_scroll_to_bottom
        # pour ne scroller que quand on le veut explicitement (ouverture
        # d'une conversation, reception d'un nouveau message), pas a
        # chaque resize de fenetre ni si l'utilisateur a scrolle manuellement
        # vers le haut.
        self._convo_should_scroll_to_bottom = False

        def _on_convo_range_changed(_mn=None, _mx=None):
            if self._convo_should_scroll_to_bottom:
                bar = self._convo_scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
                self._convo_should_scroll_to_bottom = False
        self._convo_scroll.verticalScrollBar().rangeChanged.connect(
            _on_convo_range_changed
        )

        # --- Zone de saisie + bouton envoyer ---
        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        # Le champ : QTextEdit multi-lignes, grandit avec le contenu.
        # On utilise un QTextEdit (pas un QLineEdit) pour le retour a la
        # ligne automatique et la possibilite de plusieurs lignes.
        self._convo_input = _PhoneMessageInput(max_chars=PHONE_MAX_BODY_LEN)
        self._convo_input.sig_submit.connect(self._on_convo_submit)
        self._convo_input.sig_changed.connect(self._on_convo_draft_changed)
        bottom.addWidget(self._convo_input, stretch=1)
        # Bouton piece jointe (trombone) : ouvre la page des screenshots.
        clip_sz = max(28, int(self._row_h * 0.85))
        self._btn_clip = _PhoneIconLabel("clip", clip_sz, True)
        self._btn_clip.sig_clicked.connect(self._open_screenshot_picker)
        bottom.addWidget(self._btn_clip, alignment=Qt.AlignBottom)
        # Bouton envoyer (fleche).
        send_sz = max(28, int(self._row_h * 0.85))
        self._btn_send = _PhoneIconLabel("send", send_sz, True)
        self._btn_send.sig_clicked.connect(self._on_convo_submit)
        bottom.addWidget(self._btn_send, alignment=Qt.AlignBottom)
        v.addLayout(bottom)

        # Pseudo du correspondant courant (mis a jour par show_screen_conversation).
        self._convo_pseudo = ""
        return page

    def _on_convo_submit(self):
        """Bouton envoyer (ou Entree) : recupere le texte du champ, le
        vide, emet sig_send_message au parent."""
        if not self._convo_pseudo:
            return
        body = self._convo_input.toPlainText().strip()
        if not body:
            return
        # Trim au cas ou (le widget limite deja a max_chars).
        if len(body) > PHONE_MAX_BODY_LEN:
            body = body[:PHONE_MAX_BODY_LEN]
        self.sig_send_message.emit(self._convo_pseudo, body)
        # Vide le champ apres envoi (l'envoi est synchrone : MainWindow
        # stockera et rappellera refresh_conversation pour afficher).
        self._convo_input.clear()
        # Signaler que le draft est maintenant vide.
        self.sig_draft_changed.emit(self._convo_pseudo, "")

    def _on_convo_draft_changed(self, text: str):
        """Le texte du champ a change : on relaie au parent pour qu'il
        sauvegarde le brouillon (case 'appel pendant redaction' de la
        spec)."""
        if not self._convo_pseudo:
            return
        self.sig_draft_changed.emit(self._convo_pseudo, text)

    def refresh_conversation(self, messages_items):
        """Reaffiche la liste des bulles. messages_items est une liste
        [(ts, body, is_me), ...] triee chronologiquement. Appelee a
        l'ouverture de l'ecran et a chaque envoi / reception qui concerne
        la conversation actuellement affichee."""
        # Vider les bulles existantes (tout sauf le stretch initial).
        # On utilise setParent(None) + deleteLater() : setParent(None)
        # detache immediatement le widget du layout (donc disparait du
        # rendu), deleteLater() programme la liberation memoire propre.
        while self._convo_layout.count() > 1:
            item = self._convo_layout.takeAt(1)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        # Armer l'auto-scroll en bas pour le prochain rangeChanged (qui
        # va se declencher des que les bulles ci-dessous seront ajoutees
        # et le layout recalcule). Le flag est consomme une seule fois
        # par _on_convo_range_changed et ne se redeclenche pas si
        # l'utilisateur scroll manuellement entre temps. Modif 25/05/2026.
        self._convo_should_scroll_to_bottom = True
        # Reinserer les bulles dans l'ordre. ts passe a la bulle pour
        # afficher le timestamp en bas (ajout 23/05/2026).
        self._convo_bubbles = []          # pour la navigation D-pad
        for ts, body, is_me in messages_items:
            bubble = _PhoneMessageBubble(body, is_me, self._screen_w, ts=ts,
                                         screen_h=self._screen_h)
            self._convo_layout.addWidget(bubble)
            self._convo_bubbles.append(bubble)
        # Selection par defaut : le champ de saisie (juste apres les bulles).
        self._convo_nav_index = len(self._convo_bubbles) + 1
        try:
            self._apply_convo_nav_highlight()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # [D5+] Ecran Reglages profil (page interne du telephone)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    #  Page "Screenshots" : choisir une capture a envoyer dans la
    #  conversation courante (ouverte par le trombone).
    # ------------------------------------------------------------------
    def _build_screen_screenshots(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(4)
        back = _PhoneIconLabel("back", max(16, int(self._row_h * 0.7)), True)
        back.sig_clicked.connect(self._close_screenshot_picker)
        top.addWidget(back)
        title = QLabel("Screenshots")
        title.setStyleSheet(
            f"color:{_PHONE_NAME_ONLINE}; font-size:11pt; font-weight:700; "
            "background:transparent;")
        title.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        top.addWidget(title, stretch=1)
        v.addLayout(top)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#e3e5e8; background:#e3e5e8; max-height:1px;")
        v.addWidget(sep)

        self._shots_scroll = QScrollArea()
        self._shots_scroll.setWidgetResizable(True)
        self._shots_scroll.setFrameShape(QFrame.NoFrame)
        self._shots_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._shots_scroll.setStyleSheet(
            "QScrollArea { background:transparent; }"
            "QScrollBar:vertical { width:6px; background:transparent; }"
            "QScrollBar::handle:vertical { background:#d0d3d7; "
            "  border-radius:3px; }")
        self._shots_host = QWidget()
        self._shots_host.setStyleSheet("background:transparent;")
        self._shots_grid = QVBoxLayout(self._shots_host)
        self._shots_grid.setContentsMargins(2, 4, 2, 4)
        self._shots_grid.setSpacing(6)
        self._shots_grid.addStretch(1)
        self._shots_scroll.setWidget(self._shots_host)
        v.addWidget(self._shots_scroll, stretch=1)

        self._lbl_shots_empty = QLabel(
            "Aucun screenshot.\nPrends une photo avec\nl'appareil photo !")
        self._lbl_shots_empty.setAlignment(Qt.AlignCenter)
        self._lbl_shots_empty.setStyleSheet(
            "color:#9aa0a6; font-size:9pt; background:transparent;")
        self._lbl_shots_empty.setVisible(False)
        v.addWidget(self._lbl_shots_empty)
        return page

    def _screenshots_dir(self) -> str:
        """Dossier des captures de l'appareil photo (meme regle que
        circusvoip_phone_camera._photos_dir, sans dependre du module)."""
        base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "screenshots")

    def _open_screenshot_picker(self):
        """Trombone : liste les captures (recentes d'abord) et affiche la
        page de selection. Retour possible vers la conversation."""
        self._populate_screenshots()
        self._stack.setCurrentWidget(self._page_shots)

    def _close_screenshot_picker(self):
        self._stack.setCurrentWidget(self._page_convo)
        try:
            self._apply_convo_nav_highlight()
        except Exception:
            pass

    def _populate_screenshots(self):
        while self._shots_grid.count() > 1:
            item = self._shots_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        files = []
        try:
            d = self._screenshots_dir()
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if fn.lower().endswith((".png", ".jpg", ".jpeg")):
                        full = os.path.join(d, fn)
                        try:
                            files.append((os.path.getmtime(full), full))
                        except Exception:
                            pass
        except Exception:
            pass
        files.sort(reverse=True)          # plus recentes d'abord
        files = files[:30]                # limite d'affichage
        self._lbl_shots_empty.setVisible(not files)
        idx = 0
        rows = []
        for _mt, full in files:
            row = _ScreenshotRow(full, self._row_h * 2.2)
            row.sig_pick.connect(self._on_screenshot_picked)
            self._shots_grid.insertWidget(idx, row)
            idx += 1
            rows.append(row)
        self._shots_rows = rows
        self._shots_nav_index = 0
        self._apply_shots_nav_highlight()

    def _apply_shots_nav_highlight(self):
        rows = getattr(self, "_shots_rows", [])
        on_page = (self._stack.currentWidget() is self._page_shots)
        for i, r in enumerate(rows):
            try:
                r.set_nav_highlight(on_page and i == self._shots_nav_index)
            except Exception:
                pass

    def _nav_shots(self, direction: str):
        """Navigation D-pad page screenshots : haut/bas = ligne, Entree =
        choisir (envoie), Echap = retour conversation."""
        rows = getattr(self, "_shots_rows", [])
        if direction == "esc":
            self._close_screenshot_picker()
            return
        if not rows:
            return
        if direction in ("up", "left"):
            self._shots_nav_index = (self._shots_nav_index - 1) % len(rows)
        elif direction in ("down", "right"):
            self._shots_nav_index = (self._shots_nav_index + 1) % len(rows)
        elif direction == "enter":
            row = rows[self._shots_nav_index]
            self._on_screenshot_picked(row.path())
            return
        self._apply_shots_nav_highlight()
        try:
            self._shots_scroll.ensureWidgetVisible(
                rows[self._shots_nav_index], 0, 8)
        except Exception:
            pass

    def _on_screenshot_picked(self, path: str):
        """Une capture a ete choisie : on demande l'envoi a MainWindow (qui
        gere le protocole image) puis on revient a la conversation."""
        pseudo = self._convo_pseudo
        self._close_screenshot_picker()
        if pseudo and path:
            try:
                self.sig_send_image.emit(pseudo, path)
            except Exception:
                pass

    def _build_screen_settings(self) -> QWidget:
        """Ecran Reglages profil : preview ronde + zoom +/- + pad
        directionnel + boutons Choisir / Supprimer. Tous les widgets
        emettent leurs signaux respectifs ; MainWindow les connecte au
        manager des photos. La preview est rafraichie via
        refresh_settings_preview() apres chaque action."""
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        # --- Bandeau du haut : fleche retour + titre ---
        top = QHBoxLayout()
        top.setSpacing(4)
        back_btn = _PhoneIconLabel(
            "back", max(16, int(self._row_h * 0.7)), True
        )
        back_btn.sig_clicked.connect(self.sig_settings_back.emit)
        top.addWidget(back_btn)
        title = QLabel("Reglages profil")
        title.setStyleSheet(
            f"color:{_PHONE_NAME_ONLINE}; font-size:11pt; font-weight:700; "
            "background:transparent;"
        )
        title.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        top.addWidget(title, stretch=1)
        v.addLayout(top)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#e3e5e8; background:#e3e5e8; max-height:1px;")
        v.addWidget(sep)

        # --- Preview ronde (taille adaptee a la largeur ecran) ---
        # Avatar ~ 50% de la largeur ecran -> bien visible mais laisse
        # de la place pour le pad directionnel a cote.
        prev_sz = max(80, int(self._screen_w * 0.50))
        self._settings_preview = _AvatarWidget(prev_sz, page)
        self._settings_no_photo = QLabel("(aucune photo)")
        self._settings_no_photo.setAlignment(Qt.AlignCenter)
        self._settings_no_photo.setFixedSize(prev_sz, prev_sz)
        self._settings_no_photo.setStyleSheet(
            "background:#2a2a2a; border:1px solid #444; "
            f"border-radius:{prev_sz // 2}px; color:#888; font-size:8pt;"
        )

        from PySide6.QtWidgets import QStackedLayout, QGridLayout
        prev_holder = QWidget()
        prev_holder.setFixedSize(prev_sz, prev_sz)
        stack = QStackedLayout(prev_holder)
        stack.setStackingMode(QStackedLayout.StackAll)
        stack.addWidget(self._settings_no_photo)
        stack.addWidget(self._settings_preview)

        # --- Pad directionnel a droite de la preview ---
        # Boutons compacts pour tenir dans l'ecran telephone.
        def _mk_pad_btn(text: str):
            b = QPushButton(text)
            b.setFixedSize(26, 26)
            b.setStyleSheet(
                "QPushButton { background:#2a2a2a; color:#fff; "
                "border:1px solid #444; border-radius:4px; font-size:9pt; }"
                "QPushButton:hover { background:#3a3a3a; }"
                "QPushButton:pressed { background:#1a1a1a; }"
            )
            return b
        self._settings_btn_up    = _mk_pad_btn("▲")
        self._settings_btn_left  = _mk_pad_btn("◄")
        self._settings_btn_reset = _mk_pad_btn("●")
        self._settings_btn_right = _mk_pad_btn("►")
        self._settings_btn_down  = _mk_pad_btn("▼")
        self._settings_btn_up.clicked.connect(
            lambda: self.sig_settings_move.emit(0, -1))
        self._settings_btn_down.clicked.connect(
            lambda: self.sig_settings_move.emit(0, +1))
        self._settings_btn_left.clicked.connect(
            lambda: self.sig_settings_move.emit(-1, 0))
        self._settings_btn_right.clicked.connect(
            lambda: self.sig_settings_move.emit(+1, 0))
        self._settings_btn_reset.clicked.connect(self.sig_settings_recenter.emit)

        pad_holder = QWidget()
        pad_holder.setFixedSize(26 * 3 + 6, 26 * 3 + 6)
        pad_grid = QGridLayout(pad_holder)
        pad_grid.setContentsMargins(0, 0, 0, 0)
        pad_grid.setHorizontalSpacing(3)
        pad_grid.setVerticalSpacing(3)
        pad_grid.addWidget(self._settings_btn_up,    0, 1)
        pad_grid.addWidget(self._settings_btn_left,  1, 0)
        pad_grid.addWidget(self._settings_btn_reset, 1, 1)
        pad_grid.addWidget(self._settings_btn_right, 1, 2)
        pad_grid.addWidget(self._settings_btn_down,  2, 1)

        row_prev = QHBoxLayout()
        row_prev.addStretch(1)
        row_prev.addWidget(prev_holder)
        row_prev.addSpacing(8)
        row_prev.addWidget(pad_holder, alignment=Qt.AlignVCenter)
        row_prev.addStretch(1)
        v.addLayout(row_prev)

        # --- Ligne zoom : - [Zoom XX%] + ---
        row_zoom = QHBoxLayout()
        row_zoom.addStretch(1)
        btn_zoom_minus = QPushButton("−")
        btn_zoom_minus.setFixedSize(32, 26)
        btn_zoom_minus.setStyleSheet(
            "QPushButton { background:#2a2a2a; color:#fff; "
            "border:1px solid #444; border-radius:4px; font-size:11pt; }"
            "QPushButton:hover { background:#3a3a3a; }"
        )
        btn_zoom_plus = QPushButton("+")
        btn_zoom_plus.setFixedSize(32, 26)
        btn_zoom_plus.setStyleSheet(btn_zoom_minus.styleSheet())
        btn_zoom_minus.clicked.connect(self.sig_settings_zoom_out.emit)
        btn_zoom_plus.clicked.connect(self.sig_settings_zoom_in.emit)
        self._settings_lbl_zoom = QLabel("Zoom : 100%")
        self._settings_lbl_zoom.setStyleSheet(
            "color:#c0c0c0; font-size:9pt; background:transparent;"
        )
        self._settings_lbl_zoom.setAlignment(Qt.AlignCenter)
        row_zoom.addWidget(btn_zoom_minus)
        row_zoom.addSpacing(8)
        row_zoom.addWidget(self._settings_lbl_zoom)
        row_zoom.addSpacing(8)
        row_zoom.addWidget(btn_zoom_plus)
        row_zoom.addStretch(1)
        v.addLayout(row_zoom)

        # --- Boutons Choisir / Supprimer ---
        row_btns = QHBoxLayout()
        btn_choose = QPushButton("Choisir...")
        btn_remove = QPushButton("Supprimer")
        for b in (btn_choose, btn_remove):
            b.setStyleSheet(
                "QPushButton { background:#2a2a2a; color:#fff; "
                "border:1px solid #444; border-radius:4px; "
                "padding:4px 8px; font-size:9pt; }"
                "QPushButton:hover { background:#3a3a3a; }"
            )
        btn_choose.clicked.connect(self.sig_settings_choose.emit)
        btn_remove.clicked.connect(self.sig_settings_remove.emit)
        row_btns.addWidget(btn_choose)
        row_btns.addWidget(btn_remove)
        v.addLayout(row_btns)

        v.addStretch(1)
        return page

    def refresh_settings_preview(self, jpeg_bytes, zoom_percent: int):
        """Met a jour la preview ronde et le label de zoom dans l'ecran
        Reglages. Appele par MainWindow apres chaque action utilisateur
        (choix de photo, zoom +/-, fleche, recentrer, suppression).
          jpeg_bytes    : bytes JPEG de la photo actuelle, ou None/b''
                          si aucune photo.
          zoom_percent  : valeur entre 40 et 200, affichee en pourcentage.
        """
        if not hasattr(self, "_settings_preview"):
            return  # ecran pas encore construit
        if jpeg_bytes:
            self._settings_preview.set_photo_bytes(jpeg_bytes)
            has = self._settings_preview.has_photo()
        else:
            self._settings_preview.set_photo_bytes(None)
            has = False
        self._settings_preview.setVisible(has)
        self._settings_no_photo.setVisible(not has)
        try:
            self._settings_lbl_zoom.setText(f"Zoom : {int(zoom_percent)}%")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # [D5] Avatars : injection du provider + helpers de rafraichissement
    # ------------------------------------------------------------------
    def set_photo_provider(self, fn):
        """Injecte un callable (pseudo)->bytes|None pour la recuperation
        des photos de pairs. MainWindow appelle ca juste apres avoir cree
        l'overlay. Si jamais appele, les avatars restent invisibles."""
        self._photo_provider = fn

    def _peer_photo_bytes(self, pseudo: str):
        """Retourne les bytes JPEG d'un pair s'ils sont en cache, sinon
        None. Demande aussi a MainWindow de declencher un request si la
        photo n'est pas connue. Best-effort, jamais bloquant."""
        if not pseudo or self._photo_provider is None:
            return None
        try:
            return self._photo_provider(pseudo)
        except Exception:
            return None

    def _apply_avatar(self, avatar_widget, pseudo: str):
        """Applique la photo d'un pseudo a un _AvatarWidget. Si la photo
        n'est pas en cache, l'avatar reste invisible (spec : pas de
        placeholder).

        Declenche TOUJOURS une request asynchrone, meme si on a deja la
        photo en cache : c'est ainsi qu'on detecte qu'un pair a change
        sa photo (le serveur compare le hash if-none-match et nous
        renvoie 'unchanged' si rien n'a bouge, sinon la nouvelle photo
        qui declenchera update_avatar_for via _on_profile_photo_response).
        Le cout d'une request est minime, et c'est anti-double-shot
        (un seul request en vol par pseudo)."""
        if avatar_widget is None:
            return
        b = self._peer_photo_bytes(pseudo) if pseudo else None
        if b:
            avatar_widget.set_photo_bytes(b)
            avatar_widget.setVisible(avatar_widget.has_photo())
        else:
            avatar_widget.set_photo_bytes(None)
            avatar_widget.setVisible(False)
        # Pousser la request asynchrone systematiquement (verifie aussi
        # la fraicheur du cache, pas seulement le remplir).
        if pseudo:
            try:
                mw = self._mw
                if mw is not None and hasattr(mw, "_profile_photos"):
                    mw._profile_photos.request_peer_photo(pseudo)
            except Exception:
                pass

    def update_avatar_for(self, pseudo: str):
        """[D5] Appele par MainWindow quand une nouvelle photo d'un pair
        est arrivee. Rafraichit tous les avatars actuellement affiches
        pour ce pseudo (contacts si visible, ecrans d'appel, conversation)."""
        if not pseudo:
            return
        # Conversation
        if getattr(self, "_convo_pseudo", "") == pseudo:
            self._apply_avatar(self._av_convo, pseudo)
        # Ecrans d'appel
        if self._current_outgoing_peer == pseudo:
            self._apply_avatar(self._av_outgoing, pseudo)
        if self._current_incoming_peer == pseudo:
            self._apply_avatar(self._av_incoming, pseudo)
        if self._current_in_call_peer == pseudo:
            self._apply_avatar(self._av_in_call, pseudo)
        # Ecran contacts : on doit reconstruire la ligne concernee. Pour
        # rester simple, on demande a MainWindow de refresh tous les
        # contacts (couteux mais rare).
        try:
            mw = self._mw
            if (mw is not None
                    and self._stack.currentWidget() is self._page_contacts):
                if hasattr(mw, "_phone_refresh_overlay_contacts"):
                    mw._phone_refresh_overlay_contacts()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # API publique : changement d'ecran
    # ------------------------------------------------------------------
    def show_screen_contacts(self):
        """Bascule sur l'ecran par defaut (annuaire)."""
        self._stack.setCurrentWidget(self._page_contacts)
        # Sortir proprement du mode frappe conversation et effacer sa
        # surbrillance, puis re-appliquer celle des contacts.
        self._convo_in_field = False
        self._apply_convo_nav_highlight()
        self._apply_nav_highlight()

    def show_screen_calls(self):
        """Affiche l'ecran APPELS (mode 'call' : onglets Menu/Historique).
        Utilise au retour d'un appel, pour ne jamais retomber sur l'ancien
        ecran 'full' (tel+lettre titre 'Contacts') quand l'utilisateur n'a pas
        ouvert Appels lui-meme."""
        self._open_native_contacts(0)

    def show_screen_home(self):
        """[v0.3] Bascule sur l'ecran d'accueil (home). Repli sur Contacts si
        le home n'existe pas (modules phone_apps absents)."""
        self._convo_in_field = False
        self._current_app = None
        if self._page_home is not None:
            self._stack.setCurrentWidget(self._page_home)
        else:
            self._stack.setCurrentWidget(self._page_contacts)

    def show_screen_outgoing(self, peer: str):
        """Bascule sur l'ecran appel sortant. peer = nom de la cible."""
        self._lbl_outgoing_name.setText(peer or "")
        self._current_outgoing_peer = peer or ""
        self._apply_avatar(self._av_outgoing, peer or "")
        self._stack.setCurrentWidget(self._page_outgoing)

    def show_screen_incoming(self, caller: str):
        """Bascule sur l'ecran appel entrant. caller = nom de l'appelant."""
        self._lbl_incoming_name.setText(caller or "")
        self._current_incoming_peer = caller or ""
        self._apply_avatar(self._av_incoming, caller or "")
        self._stack.setCurrentWidget(self._page_incoming)

    def show_screen_in_call(self, peer: str):
        """Bascule sur l'ecran en cours. peer = correspondant. Reset les
        toggles mute/HP a inactif a chaque nouvel appel."""
        self._lbl_in_call_name.setText(peer or "")
        self._current_in_call_peer = peer or ""
        self._apply_avatar(self._av_in_call, peer or "")
        # Reset toggles : nouveau appel = micro ouvert, HP coupe.
        self._btn_mute.set_active(False, emit=False)
        self._btn_speaker.set_active(False, emit=False)
        self._stack.setCurrentWidget(self._page_in_call)

    def show_screen_conversation(self, pseudo: str, items, draft: str = ""):
        """Bascule sur l'ecran conversation avec `pseudo`.
          items : liste [(ts, body, is_me), ...] triee chrono
          draft : brouillon a restaurer dans le champ (vide par defaut)
        Le widget memorise le pseudo en cours pour les signaux."""
        self._convo_pseudo = pseudo or ""
        self._convo_title.setText(pseudo or "")
        self._apply_avatar(self._av_convo, pseudo or "")
        # Restaure le brouillon SANS declencher sig_draft_changed (sinon
        # boucle : reload -> draft -> save -> reload). Le widget input
        # offre un setter silencieux.
        self._convo_input.set_text_silent(draft or "")
        self.refresh_conversation(items or [])
        self._stack.setCurrentWidget(self._page_convo)
        # Reset navigation D-pad : on demarre sur le champ texte (cible la
        # plus utile en arrivant), hors mode frappe. Surbrillance appliquee.
        self._convo_nav_index = len(self._convo_bubbles) + 1
        self._convo_in_field = False
        try:
            self._convo_input.clearFocus()
        except Exception:
            pass
        self._apply_convo_nav_highlight()
        # Effacer toute surbrillance residuelle de l'ecran contacts.
        self._apply_nav_highlight()

    def show_screen_settings(self):
        """[D5+] Bascule sur l'ecran Reglages profil. MainWindow doit
        appeler refresh_settings_preview() juste apres pour peupler
        l'affichage avec la photo et le zoom courants."""
        self._stack.setCurrentWidget(self._page_settings)

    def _open_profile_settings_from_contacts(self):
        """Roue dentee de l'en-tete Contacts : ouvre les Reglages profil ;
        le retour reviendra a Contacts."""
        self._settings_origin = "contacts"
        self.sig_settings_clicked.emit()

    def _open_profile_settings_from_app(self):
        """App Parametres -> 'Photo de profil' : ouvre le MEME ecran Reglages
        profil (preview rafraichie via le flux existant), mais le retour
        reviendra a l'app Parametres."""
        self._settings_origin = "settings"
        self.sig_settings_clicked.emit()

    def show_screen_settings_app(self):
        """Reaffiche l'app Parametres (retour depuis l'ecran photo de profil
        ouvert depuis elle). Repli sur le home si l'app n'est pas en cache."""
        inst = self._app_cache.get("settings")
        if inst is not None:
            self._current_app = inst
            self._stack.setCurrentWidget(inst)
            try:
                inst.on_show()
            except Exception:
                pass
        else:
            self.show_screen_home()

    def update_shortcut_labels(self, accept_key: str = "", decline_key: str = "",
                                mute_key: str = "", speaker_key: str = ""):
        """Met a jour l'affichage des raccourcis sous les boutons d'appel.
        Chaque parametre est la chaine du raccourci (forme canonique, ex:
        'ctrl+shift+m') ou vide. Si vide, le label affiche '—'. Sinon il
        affiche la forme presentable (Ctrl + Shift + M). MainWindow
        appelle cette methode au boot de l'overlay et a chaque fois qu'un
        raccourci telephone est modifie dans les Parametres."""
        def fmt(key):
            if not key:
                return "—"
            try:
                import circusvoip_core as _c
                return _c.format_hotkey_for_display(key) or "—"
            except Exception:
                return key
        a = fmt(accept_key)
        d = fmt(decline_key)
        m = fmt(mute_key)
        s = fmt(speaker_key)
        # Ecran appel sortant : seul le bouton raccrocher (rouge) est la,
        # donc le raccourci affiche est celui de "refuser/raccrocher".
        if hasattr(self, "_lbl_outgoing_shortcut"):
            self._lbl_outgoing_shortcut.setText(d)
        # Ecran appel entrant : 2 boutons (decrocher + refuser).
        if hasattr(self, "_lbl_incoming_acc_shortcut"):
            self._lbl_incoming_acc_shortcut.setText(a)
        if hasattr(self, "_lbl_incoming_dec_shortcut"):
            self._lbl_incoming_dec_shortcut.setText(d)
        # Ecran en cours : mute, raccrocher, haut-parleur.
        if hasattr(self, "_lbl_mute_shortcut"):
            self._lbl_mute_shortcut.setText(m)
        if hasattr(self, "_lbl_hang_shortcut"):
            self._lbl_hang_shortcut.setText(d)
        if hasattr(self, "_lbl_sp_shortcut"):
            self._lbl_sp_shortcut.setText(s)

    # ------------------------------------------------------------------
    # Rafraichissement de la liste des contacts
    # ------------------------------------------------------------------
    def refresh_contacts(self, annuaire: dict, online_names,
                         my_name: str = "", unread_set=None,
                         photo_provider=None, last_msg_ts_map=None):
        """Reconstruit la liste des contacts a partir de l'annuaire et de
        l'ensemble des joueurs actuellement en ligne.
          annuaire     : dict {"contacts": {pseudo: {...}}}
          online_names : iterable des pseudos connectes maintenant
          my_name      : mon pseudo, exclu de la liste
          unread_set   : iterable des pseudos qui ont des MP non lus (D4
                         etape 3). L'enveloppe de ces contacts affiche
                         un badge rouge.
          photo_provider : callable optionnel (pseudo) -> bytes|None.
                         [D5] Fournit les bytes JPEG d'un pair s'ils
                         sont en cache. Si absent ou None retourne, la
                         ligne ne montre pas d'avatar (spec : vide).
          last_msg_ts_map : dict optionnel {pseudo: float ts}. Pour chaque
                         contact qui a une conversation active, le ts du
                         dernier message echange (sent OU received, le
                         plus recent). Les pseudos absents du dict ou
                         avec ts <= 0 sont consideres "sans conversation".
                         Ajout 24/05/2026 Kainan.
        Tri (24/05/2026) : 2 groupes (connectes en haut, deconnectes en
        bas). DANS chaque groupe, contacts avec une conversation tries
        par ts du dernier message (recent en haut), puis contacts sans
        conversation tries alphabetiquement. Style messagerie type
        WhatsApp/Telegram, mais en conservant la separation connecte /
        deconnecte (pastille verte/grise reste lisible)."""
        online = set(online_names or [])
        unread = set(unread_set or [])
        ts_map = last_msg_ts_map or {}
        contacts = (annuaire or {}).get("contacts", {})
        # Memorise les derniers arguments pour pouvoir reconstruire la liste
        # sans nouvelle donnee (ex. quand on bascule Appels <-> Messagerie).
        self._last_contacts_args = dict(
            annuaire=annuaire, online_names=online, my_name=my_name,
            unread_set=unread, photo_provider=photo_provider,
            last_msg_ts_map=ts_map,
        )

        # Vider les lignes existantes (tout sauf le stretch final).
        while self._contacts_layout.count() > 1:
            item = self._contacts_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # Construire les 2 groupes, en excluant mon propre pseudo.
        names = [n for n in contacts.keys() if n and n != my_name]

        def _sort_group(pseudos):
            """Tri intra-groupe : convos par ts du dernier message (recent
            en haut), puis sans-convo par ordre alpha."""
            with_convo = []
            without_convo = []
            for p in pseudos:
                ts = float(ts_map.get(p, 0.0) or 0.0)
                if ts > 0:
                    with_convo.append((ts, p))
                else:
                    without_convo.append(p)
            # ts desc (plus recent en haut), tie-break alpha
            with_convo.sort(key=lambda x: (-x[0], x[1].lower()))
            without_convo.sort(key=str.lower)
            return [p for _, p in with_convo] + without_convo

        connected = _sort_group([n for n in names if n in online])
        disconnected = _sort_group([n for n in names if n not in online])

        on_hist = (self._contacts_mode == "call"
                   and self._appels_tab == "historique")
        if not connected and not disconnected:
            if not on_hist:
                self._contacts_scroll.setVisible(False)
                self._lbl_empty.setVisible(True)
            return
        if not on_hist:
            self._contacts_scroll.setVisible(True)
            self._lbl_empty.setVisible(False)

        def _photo(p):
            if photo_provider is None:
                return None
            try:
                return photo_provider(p)
            except Exception:
                return None

        mode = self._contacts_mode
        idx = 0
        nav_rows = []
        for pseudo in connected:
            row = _PhoneContactRow(
                pseudo, True, self._row_h, unread=(pseudo in unread),
                photo_bytes=_photo(pseudo), mode=mode,
            )
            row.sig_call.connect(self.sig_call)
            row.sig_message.connect(self.sig_message)
            self._contacts_layout.insertWidget(idx, row)
            idx += 1
            nav_rows.append(row)
        for pseudo in disconnected:
            row = _PhoneContactRow(
                pseudo, False, self._row_h, unread=(pseudo in unread),
                photo_bytes=_photo(pseudo), mode=mode,
            )
            if mode == "message":
                # Messagerie : la ligne (meme hors-ligne) ouvre la conversation
                # et est navigable au D-pad.
                row.sig_message.connect(self.sig_message)
                nav_rows.append(row)
            else:
                row.sig_forget.connect(self.sig_forget)
            self._contacts_layout.insertWidget(idx, row)
            idx += 1

        # Reconstruire l'etat de navigation D-pad : seules les lignes
        # connectees (avec phone+letter) sont navigables. On clampe l'index
        # courant et on re-applique la surbrillance.
        # Sur l'onglet Historique, la navigation porte sur les lignes
        # d'historique : on ne remplace PAS _nav_rows par les contacts (mais on
        # a quand meme reconstruit les lignes pour le retour sur l'onglet Menu).
        if not on_hist:
            self._nav_rows = nav_rows
            if not nav_rows:
                self._nav_index = 0
            else:
                self._nav_index = max(0, min(self._nav_index, len(nav_rows) - 1))
            self._nav_action = (0 if self._nav_action not in (0, 1)
                                else self._nav_action)
        self._apply_nav_highlight()

    # ------------------------------------------------------------------
    # Navigation clavier D-pad (ecran contacts : Appeler / Message)
    # ------------------------------------------------------------------
    def _apply_nav_highlight(self):
        """Re-applique la surbrillance sur toutes les lignes navigables
        selon _nav_index / _nav_action. Ne fait rien hors ecran contacts
        (la surbrillance reste posee mais invisible tant qu'on n'est pas
        sur cet ecran ; on l'efface quand meme pour rester propre)."""
        on_contacts = (self._stack.currentWidget() is self._page_contacts)
        for i, row in enumerate(self._nav_rows):
            try:
                sel = on_contacts and (i == self._nav_index)
                row.set_nav_highlight(sel, self._nav_action)
            except Exception:
                pass

    @Slot(str)
    def _is_game_active(self) -> bool:
        """True quand l'app courante est un jeu qui capture le clavier. Sert
        au listener pour router les touches vers le jeu (event synthetique) et
        les supprimer cote OS, SANS voler le premier plan (SC reste actif)."""
        app = self._current_app
        return app is not None and getattr(app, "CAPTURES_KEYBOARD", False)

    # Correspondance touche normalisee -> (Qt.Key, texte) pour l'injection.
    _GAME_KEY_QT = {
        "up":    (Qt.Key_Up, ""),    "down":  (Qt.Key_Down, ""),
        "left":  (Qt.Key_Left, ""),  "right": (Qt.Key_Right, ""),
        "enter": (Qt.Key_Return, "\r"),
        "p": (Qt.Key_P, "p"), "r": (Qt.Key_R, "r"),
        "f": (Qt.Key_F, "f"), "t": (Qt.Key_T, "t"),
        "v": (Qt.Key_V, "v"),
    }

    def _on_game_key(self, norm: str):
        """Slot main-thread : injecte une touche de jeu dans l'app courante via
        un evenement clavier synthetique (KeyPress + KeyRelease). Le jeu la
        traite par son keyPressEvent habituel, sans que l'overlay ait le focus
        clavier Windows -> SC garde le premier plan (vrai overlay)."""
        app = self._current_app
        if app is None or not getattr(app, "CAPTURES_KEYBOARD", False):
            return
        qt = self._GAME_KEY_QT.get(norm)
        if qt is None:
            return
        keycode, text = qt
        try:
            from PySide6.QtGui import QKeyEvent
            from PySide6.QtCore import QEvent
            QApplication.postEvent(
                app, QKeyEvent(QEvent.KeyPress, keycode, Qt.NoModifier, text))
            QApplication.postEvent(
                app, QKeyEvent(QEvent.KeyRelease, keycode, Qt.NoModifier, text))
        except Exception:
            pass

    def _on_nav_key(self, direction: str):
        """Slot main-thread : route une touche D-pad selon l'ecran courant.
        Ecran contacts -> _nav_contacts ; ecran conversation -> _nav_convo.
        Les autres ecrans ne sont pas navigables au clavier (scope actuel)."""
        try:
            cur = self._stack.currentWidget()
            # [v0.3] Home : grille d'apps.
            if self._page_home is not None and cur is self._page_home:
                if direction != "esc":
                    self._page_home.handle_nav(direction)
                    # Si 'enter' vient de lancer un jeu capture-clavier, cette
                    # meme Entree va fuiter dans son keyPressEvent (focus Qt)
                    # et validerait son 1er bouton de menu : on desarme la
                    # validation, qui se rearmera au relachement de la touche.
                    if direction == "enter" and self._current_app is not None \
                            and getattr(self._current_app,
                                        "CAPTURES_KEYBOARD", False):
                        try:
                            self._current_app._confirm_armed = False
                        except Exception:
                            pass
                        self._arm_game_confirm_later(self._current_app)
                return
            # [v0.3] App CircusPhone affichee.
            if self._current_app is not None and cur is self._current_app:
                if direction == "esc":
                    # L'app peut consommer le retour (ex. revenir a son menu
                    # interne) ; sinon on revient au home du telephone.
                    consumed = False
                    try:
                        consumed = bool(self._current_app.handle_back())
                    except Exception:
                        consumed = False
                    if not consumed:
                        self._go_home()
                    return
                # Jeu (capture clavier brute) : ne PAS router le D-pad ; le
                # jeu recoit ses touches via keyPressEvent natif (focus Qt).
                if not getattr(self._current_app, "CAPTURES_KEYBOARD", False):
                    prev = self._current_app
                    try:
                        self._current_app.handle_nav(direction)
                    except Exception:
                        pass
                    # Cas dossier Jeux : 'enter' a pu lancer un jeu capture-
                    # clavier (sig_open_app -> _open_game_from_folder). Comme
                    # depuis le home, cette Entree va fuiter dans le menu du
                    # jeu : on desarme sa validation (rearmee au relachement).
                    if direction == "enter" \
                            and self._current_app is not None \
                            and self._current_app is not prev \
                            and getattr(self._current_app,
                                        "CAPTURES_KEYBOARD", False):
                        try:
                            self._current_app._confirm_armed = False
                        except Exception:
                            pass
                        self._arm_game_confirm_later(self._current_app)
                return
            if cur is self._page_contacts:
                if direction == "esc":
                    self._go_home()
                    return
                self._nav_contacts(direction)
            elif cur is self._page_convo:
                self._nav_convo(direction)
            elif cur is self._page_shots:
                self._nav_shots(direction)
            elif cur is self._page_settings:
                # Ecran photo de profil : Echap = meme retour que la fleche
                # (revient a l'origine : app Parametres ou Contacts).
                if direction == "esc":
                    self.sig_settings_back.emit()
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE NAV] route KO : {e}")
                except Exception: pass

    def _nav_contacts(self, direction: str):
        """Navigation D-pad ecran contacts.
          up/down  : change de ligne (contact connecte)
          left/right : bascule l'action Appeler <-> Message
          enter    : declenche l'action sur la ligne courante
        Ignore si rien a naviguer."""
        try:
            # Mode Appels : gauche/droite basculent l'onglet Menu/Historique
            # (independamment du contenu de la liste courante).
            if self._contacts_mode == "call" and direction in ("left", "right"):
                self._set_appels_tab(
                    "menu" if direction == "left" else "historique")
                return
            n = len(self._nav_rows)
            if n == 0:
                return
            if direction == "up":
                self._nav_index = (self._nav_index - 1) % n
                self._ensure_nav_visible()
            elif direction == "down":
                self._nav_index = (self._nav_index + 1) % n
                self._ensure_nav_visible()
            elif direction == "enter":
                row = self._nav_rows[self._nav_index]
                pseudo = row.pseudo()
                if self._contacts_mode == "message":
                    self.sig_message.emit(pseudo)   # ouvre la conversation
                else:                                # mode "call"
                    self.sig_call.emit(pseudo)       # Menu/Historique -> appel
                return  # l'action peut changer d'ecran : pas de re-highlight
            self._apply_nav_highlight()
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE NAV] contacts KO : {e}")
                except Exception: pass

    def _nav_convo(self, direction: str):
        """Navigation D-pad ecran conversation. 4 cibles : Retour (0),
        champ texte (1), Trombone/screenshots (2), Envoyer (3).
        Hors champ :
          left/up    : cible precedente
          right/down : cible suivante
          enter      : Retour -> back ; Envoyer -> submit ; Champ -> focus
                       (entre dans le champ pour taper)
        Dans le champ (_convo_in_field=True) :
          esc        : ressort du champ, rend la navigation
          (le reste est gere par le QTextEdit : frappe, Entree=submit via
           keyPressEvent du champ, fleches=curseur). On n'intercepte pas."""
        try:
            # Si on est dans le champ pour taper : seul Echap nous interesse,
            # pour ressortir. Tout le reste appartient au QTextEdit.
            if self._convo_in_field:
                if direction == "esc":
                    self._convo_in_field = False
                    try:
                        self._convo_input.clearFocus()
                    except Exception:
                        pass
                    self._apply_convo_nav_highlight()
                return

            nb = len(getattr(self, "_convo_bubbles", []))
            total = nb + 4      # bulles (0..nb-1) puis 4 actions (nb..nb+3)
            if direction in ("left", "up"):
                self._convo_nav_index = (self._convo_nav_index - 1) % total
            elif direction in ("right", "down"):
                self._convo_nav_index = (self._convo_nav_index + 1) % total
            elif direction == "esc":
                # Hors champ : Retour arriere quitte la conversation (comme
                # sur toutes les autres pages).
                self.sig_back_contacts.emit()
                return
            elif direction == "enter":
                idx = self._convo_nav_index
                if idx < nb:
                    # Bulle selectionnee : si c'est une image, l'ouvrir EN
                    # GRAND sur l'ecran de jeu (meme visionneuse que la
                    # galerie). Sinon (texte), rien.
                    try:
                        p = self._convo_bubbles[idx].image_path()
                    except Exception:
                        p = None
                    if p:
                        viewer = getattr(self, "_open_photo_viewer", None)
                        if callable(viewer):
                            try:
                                viewer(p)
                            except Exception:
                                pass
                    return
                a = idx - nb        # 0=Retour, 1=champ, 2=Trombone, 3=Envoyer
                if a == 0:
                    self.sig_back_contacts.emit()
                    return   # changement d'ecran
                elif a == 2:
                    # Trombone : ouvrir la page des screenshots.
                    self._open_screenshot_picker()
                    return   # changement d'ecran
                elif a == 3:
                    self._on_convo_submit()
                    # reste sur la conversation, le champ est vide : on
                    # garde la selection sur Envoyer.
                elif a == 1:
                    # Entrer dans le champ pour taper. La fenetre overlay a
                    # le flag Qt.Tool : un setFocus() seul ne suffit pas a
                    # lui donner le focus clavier (le champ ne s'active pas).
                    # Il faut d'abord ACTIVER la fenetre (activateWindow +
                    # raise_) pour qu'elle devienne la fenetre active, puis
                    # donner le focus au champ. C'est ce que fait Windows
                    # automatiquement quand on clique dans le champ ; ici on
                    # le declenche programmatiquement.
                    self._convo_in_field = True
                    try:
                        # Forcer l'overlay au premier plan Windows (focus
                        # clavier systeme) pour que les frappes arrivent au
                        # champ et non a SC. Puis focus Qt sur le champ.
                        self._win32_force_foreground()
                        self.activateWindow()
                        self.raise_()
                        self._convo_input.setFocus(Qt.OtherFocusReason)
                        c = self._convo_input.textCursor()
                        c.movePosition(QTextCursor.End)
                        self._convo_input.setTextCursor(c)
                    except Exception:
                        pass
            elif direction == "esc":
                # Hors champ, retour (Retour arriere) : on revient aux
                # contacts (raccourci pratique). Optionnel mais coherent.
                self.sig_back_contacts.emit()
                return
            self._apply_convo_nav_highlight()
            self._ensure_convo_nav_visible()
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE NAV] convo KO : {e}")
                except Exception: pass

    def _ensure_convo_nav_visible(self):
        """Fait defiler la conversation pour garder la bulle selectionnee
        visible (quand la selection est sur un message, pas sur une action)."""
        try:
            nb = len(getattr(self, "_convo_bubbles", []))
            i = self._convo_nav_index
            if 0 <= i < nb:
                self._convo_scroll.ensureWidgetVisible(self._convo_bubbles[i],
                                                       0, 12)
        except Exception:
            pass

    def _win32_force_foreground(self, target=None):
        """Force une fenetre a devenir la fenetre active Windows (focus clavier
        systeme), afin qu'elle recoive reellement les frappes meme quand Star
        Citizen est au premier plan. `target` = widget a mettre au premier plan
        (defaut : l'overlay lui-meme ; pour la camera, on passe la fenetre
        photo, qui est une fenetre separee).

        SetForegroundWindow seul echoue souvent sur Windows moderne a cause
        de la restriction anti-vol-de-focus : seul le thread du processus
        deja au premier plan peut donner le focus librement. Le contournement
        standard : on attache temporairement notre thread d'entree a celui de
        la fenetre actuellement au premier plan (AttachThreadInput), ce qui
        nous autorise a appeler SetForegroundWindow, puis on detache.

        Approche A (cf. discussion) : SC perd le focus le temps de taper, ce
        qui est acceptable car le joueur ne se deplace pas en ecrivant.
        Retourne True si la sequence a pu s'executer, False sinon (non-Windows,
        hwnd invalide, exception)."""
        try:
            import sys as _sys
            if not _sys.platform.startswith("win"):
                return False
            import ctypes
            from ctypes import wintypes
            win = target if target is not None else self
            hwnd = int(win.winId())
            if hwnd == 0:
                return False
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # Fenetre actuellement au premier plan (typiquement SC).
            fg = user32.GetForegroundWindow()

            # Thread proprietaire de la fenetre fg, et notre thread courant.
            fg_tid = user32.GetWindowThreadProcessId(fg, None)
            cur_tid = kernel32.GetCurrentThreadId()

            SW_SHOW = 5
            attached = False
            if fg_tid and fg_tid != cur_tid:
                # Attache nos files d'entree -> autorise SetForegroundWindow.
                attached = bool(
                    user32.AttachThreadInput(cur_tid, fg_tid, True)
                )
            try:
                user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOW)
                user32.BringWindowToTop(ctypes.c_void_p(hwnd))
                user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
                user32.SetActiveWindow(ctypes.c_void_p(hwnd))
                user32.SetFocus(ctypes.c_void_p(hwnd))
            finally:
                if attached:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
            if _CORE_AVAILABLE:
                try: _core._dbg_log("[PHONE NAV] force foreground (champ)")
                except Exception: pass
            return True
        except Exception as e:
            if _CORE_AVAILABLE:
                try: _core._dbg_log(f"[PHONE NAV] force foreground KO : {e}")
                except Exception: pass
            return False

    def _apply_convo_nav_highlight(self):
        """Applique la surbrillance sur la cible courante de l'ecran
        conversation (Retour / champ / Envoyer). Si on est entre dans le
        champ pour taper (_convo_in_field), on efface les surbrillances de
        navigation (le focus Qt natif du champ prend le relais visuel)."""
        on_convo = (self._stack.currentWidget() is self._page_convo)
        sel = (-1 if (self._convo_in_field or not on_convo)
               else self._convo_nav_index)
        bubbles = getattr(self, "_convo_bubbles", [])
        nb = len(bubbles)
        # Surbrillance des bulles (indices 0..nb-1).
        for i, b in enumerate(bubbles):
            try:
                b.set_nav_selected(sel == i)
            except Exception:
                pass
        # Surbrillance des actions (indices nb..nb+3 -> 0..3).
        a = (sel - nb) if sel >= nb else -1
        try:
            self._convo_back.set_nav_selected(a == 0)
        except Exception:
            pass
        try:
            self._convo_input.set_nav_selected(a == 1)
        except Exception:
            pass
        try:
            self._btn_clip.set_nav_selected(a == 2)
        except Exception:
            pass
        try:
            self._btn_send.set_nav_selected(a == 3)
        except Exception:
            pass

    def _ensure_nav_visible(self):
        """Scroll l'aire de contacts pour que la ligne selectionnee reste
        visible (le scroll est dans self._contacts_scroll)."""
        try:
            if 0 <= self._nav_index < len(self._nav_rows):
                row = self._nav_rows[self._nav_index]
                self._contacts_scroll.ensureWidgetVisible(row)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Dessin du chassis (corps noir + bandeau "CircusPhone")
    # ------------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self._body_w, self._body_h

        # Corps du telephone : rectangle arrondi noir.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_PHONE_BODY_COLOR))
        p.drawRoundedRect(0, 0, w, h, self._radius, self._radius)

        # Boutons lateraux (decoratifs, repris du mockup).
        p.setBrush(QColor(_PHONE_BTN_COLOR))
        bw = max(2, int(w * 0.015))
        # Gauche : 3 boutons
        p.drawRoundedRect(0, int(h * 0.25), bw, int(h * 0.05), 1, 1)
        p.drawRoundedRect(0, int(h * 0.33), bw, int(h * 0.09), 1, 1)
        p.drawRoundedRect(0, int(h * 0.44), bw, int(h * 0.09), 1, 1)
        # Droite : 1 bouton
        p.drawRoundedRect(w - bw, int(h * 0.36), bw, int(h * 0.14), 1, 1)

        # Bandeau "CircusPhone" centre dans la zone haute.
        cx = w / 2
        cy = self._banner_h / 2 + int(self._banner_h * 0.08)
        # "Circus" (petit, gris) + "Phone" (grand, blanc) cote a cote.
        f_small = QFont("sans-serif")
        f_small.setPixelSize(max(10, int(self._banner_h * 0.26)))
        f_small.setWeight(QFont.Medium)
        f_big = QFont("sans-serif")
        f_big.setPixelSize(max(14, int(self._banner_h * 0.40)))
        f_big.setWeight(QFont.DemiBold)
        # Mesurer pour centrer l'ensemble.
        from PySide6.QtGui import QFontMetrics
        fm_s = QFontMetrics(f_small)
        fm_b = QFontMetrics(f_big)
        w_circus = fm_s.horizontalAdvance("Circus")
        w_phone  = fm_b.horizontalAdvance("Phone")
        total = w_circus + w_phone
        x0 = cx - total / 2
        baseline = cy + fm_b.ascent() / 2
        p.setFont(f_small)
        p.setPen(QColor(_PHONE_BANNER_GREY))
        p.drawText(int(x0), int(baseline), "Circus")
        p.setFont(f_big)
        p.setPen(QColor(_PHONE_BANNER_WHITE))
        p.drawText(int(x0 + w_circus), int(baseline), "Phone")
        p.end()

    # ------------------------------------------------------------------
    # Positionnement + animation
    # ------------------------------------------------------------------
    def _final_pos(self) -> QPoint:
        """Position finale de l'overlay : a 85% horizontal de l'ecran,
        le bas du telephone a 3 pixels du bord bas de la zone utile
        (availableGeometry exclut deja la barre des taches Windows,
        donc 3px du "bord visible" reel).

        Note : avant v0.2.0 dev, c'etait 75% mais retours utilisateurs
        sur 1080p et 2K ultrawide trouvaient le telephone trop a gauche.
        85% donne une marge a droite ~ 1/3 a 1/2 de la largeur du tel."""
        screen = QGuiApplication.primaryScreen()
        try:
            geo = screen.availableGeometry()
        except Exception:
            return QPoint(100, 100)
        # 85% horizontal : le centre du telephone est a 85% de la largeur.
        cx = geo.x() + int(geo.width() * 0.85)
        x = cx - self._body_w // 2
        # Verticalement : colle en bas avec une marge de 3px.
        margin_bottom = 3
        y = geo.y() + geo.height() - self._body_h - margin_bottom
        # Garde-fou : si le telephone est plus haut que l'ecran (cas
        # tres improbable), on aligne sur le haut.
        x = max(geo.x(), min(x, geo.x() + geo.width() - self._body_w))
        y = max(geo.y(), y)
        return QPoint(x, y)

    def show_animated(self):
        """Affiche l'overlay avec l'animation de montee depuis le bas
        (400ms, easing doux). Si deja visible, ne fait rien."""
        if self._visible_state:
            return
        self._visible_state = True
        final = self._final_pos()
        screen = QGuiApplication.primaryScreen()
        try:
            geo = screen.availableGeometry()
            start_y = geo.y() + geo.height()    # juste sous le bord bas
        except Exception:
            start_y = final.y() + self._body_h
        start = QPoint(final.x(), start_y)
        self.move(start)
        self.show()
        self.raise_()
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(400)
        self._anim.setStartValue(start)
        self._anim.setEndValue(final)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()
        # Navigation clavier : on (re)part de la 1ere ligne, action Appeler,
        # et on demarre le listener D-pad (arrete a la fermeture).
        self._nav_index = 0
        self._nav_action = 0
        self._apply_nav_highlight()
        try:
            self._nav_listener.start()
        except Exception:
            pass

    def hide_animated(self):
        """Cache l'overlay. Pour D4 etape 1 : descente symetrique vers le
        bas puis hide(). Toggle par re-appui du raccourci d'ouverture."""
        if not self._visible_state:
            return
        self._visible_state = False
        # [v0.3] Mettre l'app courante en pause a la fermeture du telephone
        # (sinon un jeu continuerait de ticker fenetre cachee : son QTimer
        # n'est pas arrete par le simple hide de la fenetre parente).
        if self._current_app is not None:
            try:
                self._current_app.on_hide()
            except Exception:
                pass
        # Arreter le listener D-pad : on ne reserve plus les fleches une
        # fois le telephone ferme.
        try:
            self._nav_listener.stop()
        except Exception:
            pass
        final = self.pos()
        screen = QGuiApplication.primaryScreen()
        try:
            geo = screen.availableGeometry()
            end_y = geo.y() + geo.height()
        except Exception:
            end_y = final.y() + self._body_h
        end = QPoint(final.x(), end_y)
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(300)
        self._anim.setStartValue(final)
        self._anim.setEndValue(end)
        self._anim.setEasingCurve(QEasingCurve.InCubic)
        self._anim.finished.connect(self.hide)
        self._anim.start()

    def is_open(self) -> bool:
        """True si l'overlay est actuellement ouvert (ou en cours
        d'animation d'ouverture)."""
        return self._visible_state


class _GameLogFeed(QObject):
    """[v0.3] Fan-out thread-safe des lignes du Game.log vers des abonnes
    (les apps CircusPhone, ex : Portefeuille). Le tail tourne dans un thread
    daemon (c2-gamelog-tail-smart), donc on passe par un signal Qt : les
    slots des abonnes (qui vivent sur le thread Qt) sont appeles en
    connexion *queued*, donc thread-safe pour toucher l'UI. Evite a chaque
    app d'ouvrir son propre tail concurrent sur le meme fichier.

    Contrat consomme par les apps (cf. circusvoip_phone_apps.PhoneServices) :
      subscribe(cb)/unsubscribe(cb) ou cb(line: str), et current_path().
    """
    sig_line = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = ""

    def subscribe(self, callback):
        # AutoConnection : depuis le thread tail vers un slot du thread Qt,
        # Qt route en queued automatiquement.
        self.sig_line.connect(callback)

    def unsubscribe(self, callback):
        try:
            self.sig_line.disconnect(callback)
        except (TypeError, RuntimeError):
            pass

    def emit_line(self, line: str):
        self.sig_line.emit(line)

    def set_path(self, path: str):
        self._path = path or ""

    def current_path(self) -> str:
        return self._path


class MainWindow(QMainWindow):
    # Signal interne pour declencher run_connect dans le worker thread
    _sig_start_connect = Signal(str, str, str)
    # Signal interne pour faire passer un evenement hotkey du thread
    # pynput au thread Qt main (un signal Qt utilise auto une
    # QueuedConnection en cross-thread, ce qui est thread-safe ;
    # contrairement a QTimer.singleShot qui exige un thread Qt).
    _sig_hotkey = Signal(str)  # nom du hotkey (ex: "mute_mic", "mute_all"...)

    # v0.2 alpha 055 : signal emis quand la machine d'etat clavier du masque
    # DisplayInfo change d'etat (mobiglass/menu options ouvert ou ferme).
    # Permet un rafraichissement immediat du masque sans attendre le tick
    # 500ms du timer. Emis depuis le thread pynput, recu en main Qt thread.
    _sig_mask_state_changed = Signal()

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

        # Soundboard (v0.2 alpha 029). Cache des samples audio charges
        # depuis les fichiers .wav locaux (a cote du script). Chaque
        # entree : sound_id -> np.ndarray float32 mono 48kHz pret a etre
        # mixe par AudioIO.play_soundboard().
        # Le cache est rempli au boot par _load_soundboard_sounds() pour
        # eviter de relire le disque a chaque clic (latence + I/O).
        # Si un fichier manque, l'entree est absente et le son sera
        # silencieusement ignore (avec log).
        self._soundboard_cache: dict = {}
        # Fenetre flottante du soundboard (creee a la demande au 1er clic
        # du bouton "Soundboard").
        self._soundboard_window = None

        # CircusPhone (D4) : overlay smartphone + annuaire local.
        # _phone_overlay : instance PhoneOverlayWindow, creee a la demande
        #                  a la 1re ouverture.
        # _phone_annuaire : dict {"contacts": {...}} charge du fichier au
        #                   boot, enrichi a chaque reception de la liste
        #                   des joueurs, sauve apres chaque changement.
        self._phone_overlay = None
        # [v0.3] Feed Game.log partage : le tail (c2-gamelog-tail-smart) y
        # pousse chaque ligne, les apps CircusPhone s'y abonnent (Portefeuille).
        self._gamelog_feed = _GameLogFeed()
        self._phone_annuaire = _phone_load_annuaire()
        # CircusPhone (D4 etape 3) : conversations privees + brouillons,
        # charges du fichier au boot. Tout passe par les fonctions
        # _phone_load_messages / _phone_save_messages / _phone_append_*.
        self._phone_messages = _phone_load_messages()

        # [D5] Manager photos profil (locale + cache des pairs). Singleton
        # cote MainWindow. Cree apres le chargement annuaire/messages pour
        # que les disques soient prets.
        self._profile_photos = _ProfilePhotoManager(self)

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

        # Titre dynamique : lit _VERSION_STRING qui vient de circusvoip_version.json
        # (format "0.1.2 alpha 035"). Avant, la version etait hardcodee en
        # "0.1" et ne refletait jamais la version reelle.
        self.setWindowTitle(f"CircusVOIP Client — {_VERSION_STRING}")
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

        # Charge les sons du soundboard en cache. Cette etape lit les
        # .wav du dossier du script et les decode en numpy float32 ;
        # delaiee apres _build_ui pour que les eventuels logs soient
        # rendus dans la console du client. Non bloquant : si un
        # fichier manque, c'est juste logue.
        self._load_soundboard_sounds()

        # QTimer pour suivre l'etat de lecture du soundboard.
        # Interroge audio_io.is_soundboard_playing() toutes les 100ms et
        # met a jour la fenetre soundboard (grise les boutons pendant
        # la lecture). Si la fenetre n'est pas creee ou pas visible, on
        # ne fait rien (le timer reste actif mais sans effet UI).
        self._soundboard_state_timer = QTimer(self)
        self._soundboard_state_timer.setInterval(100)
        self._soundboard_state_timer.timeout.connect(
            self._on_soundboard_state_timer
        )
        self._soundboard_state_timer.start()

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

        # v0.2 feature 3 : timer de check du masque DisplayInfo.
        # Tourne en permanence des le boot. Le toggle on/off se fait via
        # la case dans Parametres (self._cfg["displayinfo_mask_enabled"]).
        # Si la case n'est pas cochee, le timer tick mais ne fait rien
        # (cout negligeable, ~1 hashmap lookup + 1 comparaison de float).
        self._displayinfo_mask = None  # cree au premier show, cache sinon
        # v0.2 alpha 058 : fenetre source pour OBS (cf. case
        # "Activer la source OBS du masque" dans Parametres).
        # Cree au premier show si la case est cochee, detruite sinon.
        self._displayinfo_mask_obs = None

        # v0.2 alpha 060 : service partage du worker masque.
        # Centralise la gestion du QThread + _DisplayInfoMaskWorker, et
        # mutualise entre la fenetre ecran et la fenetre OBS (qui peuvent
        # tourner independamment l'une de l'autre desormais).
        # Pas de cout au boot : le worker n'est demarre que quand un
        # consommateur s'attache via service.attach().
        self._displayinfo_mask_service = _DisplayInfoMaskWorkerService(self)

        self._displayinfo_mask_timer = QTimer(self)
        self._displayinfo_mask_timer.setInterval(DISPLAYINFO_MASK_CHECK_MS)
        self._displayinfo_mask_timer.timeout.connect(
            self._update_displayinfo_mask
        )
        self._displayinfo_mask_timer.start()

        # v0.2 alpha 055 : machine d'etat clavier qui detecte la mobiglass
        # (F1/F2/F11) et le menu options (Echap) pour cacher le masque
        # quand le HUD DisplayInfo n'est plus visible. Le callback rebondit
        # sur le thread Qt main via un signal pour appeler
        # _update_displayinfo_mask immediatement, sans attendre le tick
        # de 500ms.
        try:
            self._sig_mask_state_changed.connect(self._update_displayinfo_mask)
        except Exception:
            pass
        self._mask_key_tracker = _DisplayInfoMaskKeyTracker(
            on_state_changed=lambda: self._sig_mask_state_changed.emit()
        )
        try:
            self._mask_key_tracker.start()
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK KEYS] start KO au boot : {e}"
                    )
                except Exception:
                    pass

        # Check des mises a jour en arriere-plan : REACTIVE en v0.2.0 dev
        # pour les tests joueurs. Le serveur d'update tourne sur le VPS
        # port 8080. Le worker interroge en background 2s apres le boot,
        # signale via _sig_update_available si une MAJ est dispo, et le
        # bouton "Verifier les MAJ" (page Parametres) passe en orange.
        # Pour redesactiver : recommenter le bloc ci-dessous.
        threading.Thread(
            target=self._update_check_worker,
            daemon=True,
            name="c2-update-check",
        ).start()

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
        # Etat d'appel CircusPhone (anciennement init par _build_page_phone
        # qui a ete supprimee : la page de debug n'a plus de raison d'etre
        # car le vrai overlay CircusPhone D4 est utilise en prod). Ces 3
        # attributs sont consommes par _phone_set_state, _phone_refresh_ui
        # et tous les _phone_do_* qui restent indispensables au CircusPhone.
        #   _phone_state : "idle" | "ringing_out" | "ringing_in" | "in_call"
        self._phone_state   = "idle"
        self._phone_call_id = None
        self._phone_peer    = None
        # Historique des appels (persistant) + suivi des appels en cours pour
        # determiner l'issue (repondu / manque / refuse).
        #   entree : {"peer":str, "dir":"in"|"out", "outcome":str, "ts":float}
        self._call_history = self._load_call_history()
        self._call_track = {}   # call_id -> {peer, dir, ts, answered}
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
        self.btn_mute_prox = QPushButton("MUTE AUDIO PROXIMITE")
        self.btn_mute_prox.setCheckable(True)
        self.btn_mute_prox.setMinimumHeight(32)
        self.btn_mute_prox.clicked.connect(self._on_mute_prox_toggled)
        v_mutes.addWidget(self.btn_mute_prox)

        # MUTE radio
        self.btn_mute_radio = QPushButton("MUTE AUDIO RADIO")
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

        # Section SOUNDBOARD (v0.2 alpha 029). Separee de la section
        # OVERLAY : conceptuellement c'est une fonctionnalite differente
        # (overlay = positionnement des elements HUD, soundboard = sons
        # vocaux RP). Une section propre rend ca plus lisible et permet
        # d'ajouter plus tard d'autres controles (volume dedie, raccourcis).
        self.gb_soundboard = QGroupBox("SOUNDBOARD")
        v_soundboard = QVBoxLayout(self.gb_soundboard)
        v_soundboard.setSpacing(6)
        # Bouton "Soundboard" qui toggle la fenetre flottante des sons.
        # Re-clic referme. Pas checkable pour rester simple (l'etat
        # ouvert/ferme est gere par la fenetre elle-meme, pas par le bouton).
        self.btn_soundboard = QPushButton("Soundboard")
        self.btn_soundboard.setMinimumHeight(32)
        self.btn_soundboard.setStyleSheet(
            "padding: 6px 12px;"
        )
        self.btn_soundboard.clicked.connect(self._on_soundboard_button_clicked)
        v_soundboard.addWidget(self.btn_soundboard)
        v_left.addWidget(self.gb_soundboard)
        # v0.2 alpha 035 : section cachee par defaut au boot. Sera
        # affichee uniquement quand le serveur push my_profile avec
        # soundboard_allowed=True. Si pas connecte ou pas de perm,
        # l'utilisateur ne voit pas la section du tout (= regle Q3).
        self.gb_soundboard.setVisible(False)

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
        # Label "Aucun autre joueur en ligne" affiche quand on est connecte
        # mais qu'aucun autre joueur n'est dans _player_cards. Cache par
        # defaut (visible=False) : devient visible via
        # _refresh_no_other_players_label appele depuis _on_player_joined,
        # _on_player_left, _on_players_reset et les events connect/disconnect.
        # Style : gris italique centre, discret.
        # Ajout 25/05/2026 Kainan.
        self.lbl_no_other_players = QLabel("Aucun autre joueur en ligne")
        self.lbl_no_other_players.setAlignment(Qt.AlignCenter)
        self.lbl_no_other_players.setStyleSheet(
            f"color: {THEME_MUTED}; font-style: italic; "
            "padding: 20px; font-size: 10pt;"
        )
        self.lbl_no_other_players.setVisible(False)
        self._players_layout.addWidget(self.lbl_no_other_players)
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
        self.lbl_mute_prox_key  = _make_key_row(v_radio, "Mute audio proximite :",   "mute_prox")
        self.lbl_mute_radio_key = _make_key_row(v_radio, "Mute audio radio :",       "mute_radio")
        self.lbl_mute_all_key   = _make_key_row(v_radio, "Mute tout :",              "mute_all")
        self.lbl_prox_short_key = _make_key_row(v_radio, "Proximite 30m / 5m :",     "prox_short")
        self.lbl_cycle_ch_key   = _make_key_row(v_radio, "Cycle canal radio :",      "cycle_channel")

        v_left.addWidget(gb_radio)

        # ── CircusPhone (D4 etape 4) : raccourcis du telephone ──
        gb_phone = QGroupBox("CircusPhone")
        gb_phone.setStyleSheet(
            "QGroupBox { font-weight: bold; padding-top: 14px; }"
        )
        v_phone = QVBoxLayout(gb_phone)
        v_phone.setSpacing(6)
        self.lbl_phone_open_key = _make_key_row(
            v_phone, "Ouvrir / Fermer le telephone :", "phone_open"
        )
        self.lbl_phone_accept_key  = _make_key_row(
            v_phone, "Decrocher :", "phone_accept"
        )
        self.lbl_phone_decline_key = _make_key_row(
            v_phone, "Refuser / Raccrocher :", "phone_decline"
        )
        self.lbl_phone_mute_key    = _make_key_row(
            v_phone, "Mute micro :", "phone_mute"
        )
        self.lbl_phone_speaker_key = _make_key_row(
            v_phone, "Haut-parleur :", "phone_speaker"
        )
        v_left.addWidget(gb_phone)

        # Initialiser l'affichage des touches depuis state
        self._refresh_radio_key_labels()

        # Section Mise a jour : REACTIVEE en v0.2.0 dev pour les tests joueurs.
        # Le QPushButton est instancie, stylise et ajoute au layout.
        # Pour redesactiver (release publique sans MAJ visible) : recommenter
        # les deux v_upd.addWidget / v_left.addWidget en bas du bloc.
        gb_upd = QGroupBox("Mise a jour")
        gb_upd.setStyleSheet("QGroupBox { font-weight: bold; padding-top: 14px; }")
        v_upd = QVBoxLayout(gb_upd)
        v_upd.setSpacing(6)
        self.btn_check_update = QPushButton("Verifier les MAJ")
        self.btn_check_update.setMinimumHeight(28)
        self._set_update_button_style(False)
        self.btn_check_update.clicked.connect(self._on_check_update_clicked)
        v_upd.addWidget(self.btn_check_update)
        v_left.addWidget(gb_upd)

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

        # Separateur visuel avant le masque DisplayInfo : sujet different
        # (rendu graphique a l'ecran, vs choix CPU/GPU pour le moteur OCR).
        sep_ocr = QFrame()
        sep_ocr.setFrameShape(QFrame.HLine)
        sep_ocr.setFrameShadow(QFrame.Sunken)
        sep_ocr.setStyleSheet("color: #444;")
        v_ocr.addWidget(sep_ocr)

        # Case masque DisplayInfo (v0.2, feature 3). Coche -> un rectangle
        # noir opaque s'affiche sur la zone DisplayInfo SC quand l'OCR
        # lit une position fraiche. Voir DisplayInfoMaskWindow.
        self.cb_displayinfo_mask = QCheckBox(
            "Masquer la zone DisplayInfo (HUD)"
        )
        mask_enabled = bool(self._cfg.get("displayinfo_mask_enabled", False))
        self.cb_displayinfo_mask.setChecked(mask_enabled)
        self.cb_displayinfo_mask.setToolTip(
            "Affiche un rectangle noir opaque par-dessus la zone "
            "DisplayInfo de Star Citizen (le HUD qui affiche le nom "
            "de la zone / planete / station). Utile pour les streamers "
            "qui veulent eviter de leak leur position, ou si le HUD "
            "vous gene visuellement.\n\n"
            "Le masque ne s'affiche que quand l'OCR lit une position "
            "fraiche (vous etes en jeu, pas sur le bureau ou dans un "
            "menu)."
        )
        self.cb_displayinfo_mask.toggled.connect(
            self._on_displayinfo_mask_toggled
        )
        v_ocr.addWidget(self.cb_displayinfo_mask)

        self.lbl_displayinfo_mask_info = QLabel(
            "Le masque s'affiche automatiquement sur l'ecran ou tourne "
            "l'OCR, et disparait si la position OCR n'est plus mise a "
            "jour (alt-tab, menu, ecran de chargement)."
        )
        self.lbl_displayinfo_mask_info.setStyleSheet(
            "color: #888; font-size: 9pt;"
        )
        self.lbl_displayinfo_mask_info.setWordWrap(True)
        v_ocr.addWidget(self.lbl_displayinfo_mask_info)

        # v0.2 alpha 058 : case "Activer la source OBS du masque". Quand
        # cochee, une fenetre offscreen invisible a l'ecran est creee et
        # affiche le meme masque que la fenetre ecran. Le streamer peut
        # alors l'ajouter dans OBS comme source "Window Capture" pour
        # diffuser le masque a ses viewers sans avoir le masque a l'ecran
        # (les deux modes coexistent, ils sont independants).
        self.cb_displayinfo_mask_obs = QCheckBox(
            "Activer la source OBS du masque (pour streamers)"
        )
        mask_obs_enabled = bool(
            self._cfg.get("displayinfo_mask_obs_enabled", False)
        )
        self.cb_displayinfo_mask_obs.setChecked(mask_obs_enabled)
        self.cb_displayinfo_mask_obs.setToolTip(
            "Cree une fenetre cachee, hors ecran, qui affiche le meme "
            "masque que celui visible a l'ecran. Cette fenetre peut etre "
            "ajoutee dans OBS comme source 'Window Capture' (en mode "
            "Windows 10 Graphics Capture).\n\n"
            "Titre de la fenetre dans OBS : "
            f"\"{DisplayInfoMaskWindowOBS.OBS_WINDOW_TITLE}\".\n\n"
            "Necessite que le masque DisplayInfo soit aussi active "
            "(la fenetre OBS reutilise le rendu du masque ecran)."
        )
        self.cb_displayinfo_mask_obs.toggled.connect(
            self._on_displayinfo_mask_obs_toggled
        )
        v_ocr.addWidget(self.cb_displayinfo_mask_obs)

        self.lbl_displayinfo_mask_obs_info = QLabel(
            "Dans OBS : Ajouter une source > Capture de fenetre > Choisir "
            f"\"{DisplayInfoMaskWindowOBS.OBS_WINDOW_TITLE}\" > Mode de "
            "capture \"Windows 10\". Positionner cette source par-dessus "
            "votre Game Capture sur la zone du HUD."
        )
        self.lbl_displayinfo_mask_obs_info.setStyleSheet(
            "color: #888; font-size: 9pt;"
        )
        self.lbl_displayinfo_mask_obs_info.setWordWrap(True)
        v_ocr.addWidget(self.lbl_displayinfo_mask_obs_info)

        # Cases de selection de la frequence du masque (v0.2 alpha 027).
        # Comportement : une seule case peut etre cochee a la fois
        # (gere manuellement, pas un QButtonGroup, parce que ce sont
        # des QCheckBox et pas des QRadioButton).
        lbl_mask_fps = QLabel("Frequence du masque :")
        lbl_mask_fps.setStyleSheet("color: #ccc; padding-top: 6px;")
        v_ocr.addWidget(lbl_mask_fps)
        # Conteneur horizontal pour les 5 cases
        h_fps = QHBoxLayout()
        h_fps.setSpacing(8)
        # Recupere la frequence courante depuis la config (defaut 5).
        # v0.2.0 dev : defaut abaisse de 60 a 5 FPS pour reduire la charge
        # CPU sur les PC modestes. L'utilisateur peut toujours choisir
        # 10/20/30/60 dans l'UI selon ses besoins.
        current_fps = int(self._cfg.get("displayinfo_mask_fps", 5))
        self.cb_mask_fps = {}  # fps_val -> QCheckBox
        # v0.2 alpha 052 : 120 FPS retire (le pipeline plafonne ~50 FPS
        # effectifs cote calcul, donc demander 120 ne change rien).
        for fps_val in (5, 10, 20, 30, 60):
            cb = QCheckBox(f"{fps_val} FPS")
            cb.setChecked(fps_val == current_fps)
            # Lambda capture fps_val correctement (sinon late-binding).
            cb.toggled.connect(
                lambda checked, v=fps_val: self._on_mask_fps_toggled(v, checked)
            )
            self.cb_mask_fps[fps_val] = cb
            h_fps.addWidget(cb)
        h_fps.addStretch()
        v_ocr.addLayout(h_fps)

        # Dimensions du masque (v0.2 alpha 045) : hauteur (en multiples
        # de la hauteur de la zone OCR, defaut 19 lignes) et largeur (en
        # multiplicateur de la largeur de la zone OCR, defaut 1.0).
        # Permet d'ajuster a la volee sans modifier le code.
        h_mask_dim = QHBoxLayout()
        h_mask_dim.setSpacing(8)
        lbl_h = QLabel("Hauteur (× zone OCR) :")
        lbl_h.setStyleSheet("color: #ccc;")
        h_mask_dim.addWidget(lbl_h)
        self.spn_mask_height = QSpinBox()
        self.spn_mask_height.setRange(1, 50)
        self.spn_mask_height.setSingleStep(1)
        self.spn_mask_height.setValue(
            int(self._cfg.get("displayinfo_mask_height_factor", 19))
        )
        self.spn_mask_height.setFixedWidth(80)
        self.spn_mask_height.valueChanged.connect(
            self._on_mask_height_changed
        )
        h_mask_dim.addWidget(self.spn_mask_height)
        h_mask_dim.addSpacing(20)
        lbl_w = QLabel("Largeur (× zone OCR) :")
        lbl_w.setStyleSheet("color: #ccc;")
        h_mask_dim.addWidget(lbl_w)
        self.spn_mask_width = QDoubleSpinBox()
        self.spn_mask_width.setRange(0.1, 5.0)
        self.spn_mask_width.setSingleStep(0.1)
        self.spn_mask_width.setDecimals(2)
        self.spn_mask_width.setValue(
            float(self._cfg.get("displayinfo_mask_width_factor", 1.0))
        )
        self.spn_mask_width.setFixedWidth(80)
        self.spn_mask_width.valueChanged.connect(
            self._on_mask_width_changed
        )
        h_mask_dim.addWidget(self.spn_mask_width)
        h_mask_dim.addStretch()
        v_ocr.addLayout(h_mask_dim)

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

    def _phone_log(self, msg: str):
        """Stub : anciennement loggue dans txt_phone_log de la page Phone
        Debug supprimee. On garde la methode car elle est invoquee par tous
        les _phone_do_* (decroche, refuse, raccroche, etc.) qui restent
        indispensables au vrai CircusPhone. Aucune action visible : si tu
        veux retrouver ces logs, ils sont aussi dans le log debug global
        via les exceptions et events serveur."""
        pass

    def _phone_refresh_ui(self):
        """Stub : anciennement mettait a jour les widgets de la page Phone
        Debug (lbl_phone_state, btn_phone_call, etc.) qui a ete supprimee.
        On garde la methode car _phone_set_state l'appelle systematiquement,
        mais elle est devenue un no-op. Le vrai overlay CircusPhone gere sa
        propre UI via _phone_refresh_overlay_buttons et _phone_set_state."""
        pass

    def _phone_set_state(self, new_state: str, peer=None, call_id=None):
        """Centralise les mutations d'etat d'appel + refresh UI.
        Synchronise aussi l'etat d'appel cote core (state.phone_in_call /
        state.phone_peer) : c'est ce que lisent _on_audio_captured (pour
        emettre la voix avec le flag 0x03) et _audio_ws_loop (pour ne
        jouer que les trames 0x03 venant du correspondant). D3."""
        # D4b : retenir si on quitte un appel actif avec HP ON, pour
        # envoyer une derniere liste vide au serveur (cleanup explicite,
        # meme si le serveur fait deja le cleanup sur phone_call_ended).
        was_hp_active = bool(getattr(self, "_phone_speaker_on", False)) and \
                        self._phone_state == "in_call"

        self._phone_state   = new_state
        self._phone_peer    = peer
        self._phone_call_id = call_id
        self._phone_refresh_ui()
        # --- Sync core (D3 : audio bidirectionnel) ---
        # in_call uniquement quand l'appel est reellement decroche ;
        # pendant la sonnerie (ringing_out / ringing_in) on n'emet pas
        # encore de voix telephone.
        if _CORE_AVAILABLE:
            try:
                in_call = (new_state == "in_call")
                state.phone_in_call = in_call
                state.phone_peer    = peer if in_call else None
                # D4b : exposer le call_id au core pour qu'il puisse
                # envoyer phone_speaker_state depuis la boucle OCR.
                state.phone_call_id = call_id if in_call else None
                # Cas limite : si la touche radio etait DEJA enfoncee au
                # moment ou l'appel demarre, le press n'a pas ete bloque
                # (il a eu lieu avant l'appel) et radio_active est reste
                # True. Le release sera ignore pendant l'appel -> il
                # resterait True a jamais. On force donc l'arret de la
                # radio a l'entree en appel.
                if in_call:
                    state.radio_active = False
                    state.profile_radio_active = False
                # D4b : sortie d'appel -> reset complet HP. Le serveur
                # sait deja nettoyer via phone_call_ended mais on envoie
                # une derniere liste vide pour eviter tout coin de race
                # (et pour reset proprement les flags locaux).
                if not in_call:
                    self._phone_speaker_on = False
                    if was_hp_active:
                        # Etat avait HP ON juste avant la transition.
                        # Forcer l'envoi vide. state.phone_hp_active est
                        # mis a False en interne par _phone_hp_send_state
                        # quand phone_in_call=False.
                        state.phone_hp_active = False
                        try:
                            _core._phone_hp_send_state(force=True)
                        except Exception:
                            pass
                    state.phone_hp_active = False
                    state.phone_hp_last_neighbors_sent = set()
            except Exception:
                pass

    # --- Actions utilisateur (boutons de la page) ---

    def _phone_do_accept(self):
        """Bouton DECROCHER : envoie phone_call_accept."""
        if self._phone_state != "ringing_in":
            self._phone_log("[IGNORE] Aucun appel entrant a decrocher.")
            return
        cid = self._phone_call_id
        try:
            ok = _core._ws_send_safe({
                "type": "phone_call_accept", "call_id": cid,
            })
        except Exception as e:
            ok = False
            self._phone_log(f"[ERREUR] _ws_send_safe : {e}")
        if ok:
            self._phone_log(f"→ phone_call_accept (call_id={cid})")
        else:
            self._phone_log("[ERREUR] Envoi phone_call_accept echoue.")

    def _phone_do_decline(self):
        """Bouton REFUSER : envoie phone_call_decline."""
        if self._phone_state != "ringing_in":
            self._phone_log("[IGNORE] Aucun appel entrant a refuser.")
            return
        cid = self._phone_call_id
        try:
            ok = _core._ws_send_safe({
                "type": "phone_call_decline", "call_id": cid,
            })
        except Exception as e:
            ok = False
            self._phone_log(f"[ERREUR] _ws_send_safe : {e}")
        if ok:
            self._phone_log(f"→ phone_call_decline (call_id={cid})")
            # Couper la sonnerie immediatement + retour repos (le serveur
            # ne renotifie pas celui qui refuse).
            self._phone_stop_ring()
            self._call_track_finalize(cid, "declined")
            self._phone_set_state("idle")
            # Bascule l'overlay sur l'ecran Contacts (le serveur ne nous
            # renotifie pas, donc c'est ici qu'on fait le retour visuel).
            self._phone_back_to_contacts()
        else:
            self._phone_log("[ERREUR] Envoi phone_call_decline echoue.")

    def _phone_do_hangup(self):
        """Bouton RACCROCHER : annule un appel sortant ou raccroche un
        appel en cours (meme message WS dans les deux cas)."""
        if self._phone_state not in ("ringing_out", "in_call"):
            self._phone_log("[IGNORE] Aucun appel a raccrocher.")
            return
        cid = self._phone_call_id
        try:
            ok = _core._ws_send_safe({
                "type": "phone_call_hangup", "call_id": cid,
            })
        except Exception as e:
            ok = False
            self._phone_log(f"[ERREUR] _ws_send_safe : {e}")
        if ok:
            self._phone_log(f"→ phone_call_hangup (call_id={cid})")
            # Couper le bip d'appel si on annulait une sonnerie sortante +
            # retour repos (le serveur ne renotifie pas le raccrocheur).
            self._phone_stop_ring()
            self._call_track_finalize(cid, "missed")
            self._phone_set_state("idle")
            # Bascule l'overlay sur l'ecran Contacts (le serveur ne nous
            # renotifie pas, donc c'est ici qu'on fait le retour visuel).
            self._phone_back_to_contacts()
        else:
            self._phone_log("[ERREUR] Envoi phone_call_hangup echoue.")

    # --- Slots : reception des signaux du NetWorker (thread Qt) ---

    # ---- Historique des appels (onglet Appels > Historique) --------------
    def _load_call_history(self):
        """Charge l'historique d'appels persiste (max 50 entrees)."""
        try:
            if _CORE_AVAILABLE:
                cfg = _core._load_client_cfg() or {}
                h = cfg.get("call_history")
                if isinstance(h, list):
                    return [e for e in h if isinstance(e, dict)][-50:]
        except Exception:
            pass
        return []

    def _save_call_history(self):
        try:
            if _CORE_AVAILABLE:
                cfg = _core._load_client_cfg() or {}
                cfg["call_history"] = self._call_history[-50:]
                _core._save_client_cfg(cfg)
        except Exception:
            pass

    def _record_call(self, peer, direction, outcome):
        """Ajoute une entree a l'historique, persiste, et rafraichit l'onglet
        Historique si l'overlay l'affiche.
          direction : "in" (entrant) | "out" (sortant)
          outcome   : "answered" | "missed" | "declined" | "busy" | "offline"."""
        if not peer:
            return
        try:
            self._call_history.append({
                "peer": peer, "dir": direction,
                "outcome": outcome, "ts": time.time(),
            })
            self._call_history = self._call_history[-50:]
            self._save_call_history()
            ov = self._phone_overlay
            if ov is not None and hasattr(ov, "refresh_call_history"):
                try:
                    ov.refresh_call_history(self._call_history)
                except Exception:
                    pass
        except Exception:
            pass

    def _call_track_start(self, call_id, peer, direction):
        """Memorise un appel qui demarre (pour determiner l'issue a la fin)."""
        if call_id:
            self._call_track[call_id] = {
                "peer": peer, "dir": direction,
                "ts": time.time(), "answered": False,
            }

    def _call_track_finalize(self, call_id, default_outcome):
        """Cloture un appel suivi : 'answered' s'il a ete decroche, sinon
        l'issue par defaut fournie. Enregistre l'entree d'historique."""
        info = self._call_track.pop(call_id, None) if call_id else None
        if info is None:
            return
        outcome = "answered" if info.get("answered") else default_outcome
        self._record_call(info.get("peer"), info.get("dir", "out"), outcome)

    @Slot(str, str)
    def _on_phone_ringing(self, call_id: str, target: str):
        self._phone_set_state("ringing_out", peer=target, call_id=call_id)
        self._call_track_start(call_id, target, "out")
        self._phone_log(f"← phone_call_ringing : ça sonne chez {target} "
                        f"(call_id={call_id})")
        # Appelant : on lance le bip d'appel en boucle.
        self._phone_play_ring("dial")
        # D4 etape 2 : bascule l'overlay sur l'ecran 'Appel sortant' s'il
        # est ouvert. Sinon : pas d'ouverture automatique (telephone "dans
        # la poche") - juste le bip d'appel audible.
        ov = self._phone_overlay
        if ov is not None and ov.is_open():
            ov.show_screen_outgoing(target or "")

    @Slot(str, str)
    def _on_phone_incoming(self, call_id: str, caller: str):
        self._phone_set_state("ringing_in", peer=caller, call_id=call_id)
        self._call_track_start(call_id, caller, "in")
        self._phone_log(f"← phone_call_incoming : {caller} vous appelle "
                        f"(call_id={call_id})")
        # Destinataire : on lance la sonnerie en boucle.
        self._phone_play_ring("ring")
        # D4 etape 2 : telephone "dans la poche". On NE bascule PAS
        # automatiquement la page debug et on NE force PAS l'ouverture
        # de l'overlay. Seul son : la sonnerie. Si l'overlay est deja
        # ouvert, il bascule sur l'ecran appel entrant (priorite
        # d'affichage de la spec).
        ov = self._phone_overlay
        if ov is not None and ov.is_open():
            ov.show_screen_incoming(caller or "")

    @Slot(str, str, str)
    def _on_phone_accepted(self, call_id: str, caller: str, callee: str):
        # Defense : on ignore les messages qui concerneraient un autre
        # appel que celui qu'on connait. Cas tordu mais possible
        # (message tardif apres reco rapide, ou serveur incoherent).
        if call_id and self._phone_call_id and call_id != self._phone_call_id:
            self._phone_log(
                f"← phone_call_accepted IGNORE (call_id={call_id} "
                f"!= courant={self._phone_call_id})"
            )
            return
        # L'autre partie = celle qui n'est pas nous.
        my = state.my_name if _CORE_AVAILABLE else None
        peer = callee if caller == my else caller
        self._phone_set_state("in_call", peer=peer, call_id=call_id)
        if call_id in self._call_track:
            self._call_track[call_id]["answered"] = True
        self._phone_log(f"← phone_call_accepted : en appel avec {peer} "
                        f"(call_id={call_id})")
        # Appel decroche : la sonnerie / le bip s'arrete des deux cotes.
        self._phone_stop_ring()
        # D4 etape 2 : bascule sur l'ecran 'En appel' si l'overlay est
        # ouvert.
        ov = self._phone_overlay
        if ov is not None and ov.is_open():
            ov.show_screen_in_call(peer or "")

    @Slot(str)
    def _on_phone_declined(self, call_id: str):
        if call_id and self._phone_call_id and call_id != self._phone_call_id:
            self._phone_log(
                f"← phone_call_declined IGNORE (call_id={call_id} "
                f"!= courant={self._phone_call_id})"
            )
            return
        self._phone_log(f"← phone_call_declined : appel refuse "
                        f"(call_id={call_id})")
        # Appelant : la cible a refuse, on coupe le bip d'appel.
        self._phone_stop_ring()
        self._call_track_finalize(call_id, "declined")
        self._phone_set_state("idle")
        self._phone_back_to_contacts()

    @Slot(str, str)
    def _on_phone_busy(self, target: str, cause: str):
        label = "hors ligne" if cause == "offline" else "deja en appel"
        self._phone_log(f"← phone_call_busy : {target} est {label}")
        # Par securite (aucun son ne devrait tourner a ce stade, mais
        # idempotent).
        self._phone_stop_ring()
        # Retire toute piste d'appel sortant vers cette cible AVANT d'enregistrer
        # (sinon un 'ended' ulterieur du serveur la finaliserait une 2e fois).
        for _cid, _info in list(self._call_track.items()):
            if _info.get("dir") == "out" and _info.get("peer") == target:
                self._call_track.pop(_cid, None)
        self._record_call(target, "out",
                          "offline" if cause == "offline" else "busy")
        self._phone_set_state("idle")
        self._phone_back_to_contacts()

    @Slot(str, str, str)
    def _on_phone_missed(self, call_id: str, caller: str, callee: str):
        if call_id and self._phone_call_id and call_id != self._phone_call_id:
            self._phone_log(
                f"← phone_call_missed IGNORE (call_id={call_id} "
                f"!= courant={self._phone_call_id})"
            )
            return
        my = state.my_name if _CORE_AVAILABLE else None
        if caller == my:
            self._phone_log(f"← phone_call_missed : appel non abouti "
                            f"(call_id={call_id})")
        else:
            self._phone_log(f"← phone_call_missed : appel manque de "
                            f"{caller} (call_id={call_id})")
        # Timeout 45s : on coupe la sonnerie / le bip des deux cotes.
        self._phone_stop_ring()
        self._call_track_finalize(call_id, "missed")
        self._phone_set_state("idle")
        self._phone_back_to_contacts()

    @Slot(str, str)
    def _on_phone_ended(self, call_id: str, reason: str):
        if call_id and self._phone_call_id and call_id != self._phone_call_id:
            self._phone_log(
                f"← phone_call_ended IGNORE (call_id={call_id} "
                f"!= courant={self._phone_call_id})"
            )
            return
        reason_txt = {
            "hangup":          "l'autre partie a raccroche",
            "peer_disconnect": "l'autre partie s'est deconnectee",
            "peer_timeout":    "l'autre partie a timeout",
        }.get(reason, reason)
        self._phone_log(f"← phone_call_ended : {reason_txt} "
                        f"(call_id={call_id})")
        # Fin d'appel : on coupe tout son en cours.
        self._phone_stop_ring()
        self._call_track_finalize(call_id, "missed")
        self._phone_set_state("idle")
        self._phone_back_to_contacts()

    def _phone_back_to_contacts(self):
        """Retour a l'ecran Contacts apres un appel termine. No-op si
        l'overlay n'existe pas ou n'est pas ouvert (auquel cas il n'a
        rien a basculer)."""
        ov = self._phone_overlay
        if ov is not None and ov.is_open():
            # Ecran APPELS (mode call) : un appel releve du contexte Appels.
            # Evite de retomber sur l'ancien ecran 'full' titre 'Contacts' si
            # l'utilisateur n'avait pas ouvert Appels avant l'appel.
            if hasattr(ov, "show_screen_calls"):
                ov.show_screen_calls()
            else:
                ov.show_screen_contacts()

    def _phone_on_disconnect(self):
        """Appele quand la connexion serveur tombe : tout appel en cours
        est de facto termine (le serveur l'a deja purge). Retour repos."""
        if getattr(self, "_phone_state", "idle") != "idle":
            self._phone_set_state("idle")
            self._phone_log("[NET] Deconnecte : appel termine.")
        # Couper la sonnerie si elle tournait encore.
        self._phone_stop_ring()

    # --- Sonnerie telephone (Feature 4, D2) ---

    def _phone_play_ring(self, kind: str):
        """Demarre la sonnerie telephone en boucle via audio_io.
          kind = "ring" -> sonnerie destinataire (appel entrant)
          kind = "dial" -> bip d'appel appelant (en attente de reponse)
        No-op silencieux si audio_io n'est pas disponible (audio pas
        demarre = pas connecte) : le cycle d'appel D1 continue de
        fonctionner sans le son."""
        audio = getattr(state, "audio_io", None) if _CORE_AVAILABLE else None
        if audio is None:
            return
        try:
            audio.play_phone_ring(kind)
        except Exception as e:
            self._phone_log(f"[AUDIO] play_phone_ring KO : {e}")

    def _phone_stop_ring(self):
        """Coupe la sonnerie telephone immediatement (coupure nette).
        Idempotent : sans danger si rien ne sonne ou si audio_io est
        absent. Appele sur chaque transition d'appel."""
        audio = getattr(state, "audio_io", None) if _CORE_AVAILABLE else None
        if audio is None:
            return
        try:
            audio.stop_phone_ring()
        except Exception as e:
            self._phone_log(f"[AUDIO] stop_phone_ring KO : {e}")

    # ------------------------------------------------------------------
    # CircusPhone (D4) : overlay smartphone + annuaire
    # ------------------------------------------------------------------
    def _phone_ensure_overlay(self):
        """Cree l'overlay smartphone s'il n'existe pas encore (lazy init,
        comme la fenetre soundboard). Branche ses signaux. Retourne
        l'instance (ou None si la creation echoue)."""
        if self._phone_overlay is not None:
            return self._phone_overlay
        try:
            ov = PhoneOverlayWindow(self)
            # Actions depuis l'ecran Contacts.
            ov.sig_call.connect(self._on_phone_overlay_call)
            ov.sig_message.connect(self._on_phone_overlay_message)
            ov.sig_forget.connect(self._on_phone_overlay_forget)
            # Actions depuis les ecrans d'appel (D4 etape 2).
            ov.sig_accept_call.connect(self._phone_do_accept)
            ov.sig_decline_call.connect(self._phone_do_decline)
            ov.sig_hangup_call.connect(self._phone_do_hangup)
            ov.sig_mute_toggled.connect(self._on_phone_overlay_mute)
            ov.sig_speaker_toggled.connect(self._on_phone_overlay_speaker)
            # Actions depuis l'ecran Conversation (D4 etape 3).
            ov.sig_send_message.connect(self._on_phone_overlay_send_message)
            ov.sig_back_contacts.connect(self._on_phone_overlay_back_contacts)
            ov.sig_send_image.connect(self._on_phone_send_image)
            ov.sig_draft_changed.connect(self._on_phone_overlay_draft_changed)
            # [D5+] Reglages profil : page interne du telephone. Engrenage
            # bascule vers l'ecran Reglages. Chaque widget de l'ecran emet
            # son signal, MainWindow relaye au manager des photos.
            ov.sig_settings_clicked.connect(self._on_phone_overlay_settings)
            ov.sig_settings_back.connect(self._on_phone_overlay_settings_back)
            ov.sig_settings_choose.connect(self._on_phone_overlay_settings_choose)
            ov.sig_settings_remove.connect(self._on_phone_overlay_settings_remove)
            ov.sig_settings_zoom_in.connect(self._on_phone_overlay_settings_zoom_in)
            ov.sig_settings_zoom_out.connect(self._on_phone_overlay_settings_zoom_out)
            ov.sig_settings_move.connect(self._on_phone_overlay_settings_move)
            ov.sig_settings_recenter.connect(self._on_phone_overlay_settings_recenter)
            # [D5] Injecte le provider de photos pour l'affichage d'avatars
            # dans l'overlay. Le manager retourne les bytes JPEG d'un pair
            # si en cache, None sinon. L'overlay decide quoi faire (afficher
            # ou rien selon la spec : pas de placeholder).
            try:
                ov.set_photo_provider(
                    lambda pseudo: self._profile_photos.get_peer_photo_bytes(
                        pseudo
                    )
                )
            except Exception:
                pass
            self._phone_overlay = ov
            # CircusPhone (D4 etape 4) : initialiser l'affichage des
            # raccourcis sous les boutons d'appel a partir de state.
            self._phone_refresh_overlay_shortcuts()
        except Exception as e:
            self._on_log(f"[PHONE] Echec creation overlay : {e}")
            self._phone_overlay = None
        return self._phone_overlay

    def _phone_refresh_overlay_shortcuts(self):
        """Met a jour les labels de raccourci sous les boutons de l'overlay
        a partir des touches actuellement configurees dans state. Appele
        a la creation de l'overlay et a chaque modification d'un raccourci
        telephone dans les Parametres. No-op si l'overlay n'existe pas."""
        ov = self._phone_overlay
        if ov is None or not hasattr(ov, "update_shortcut_labels"):
            return
        try:
            ov.update_shortcut_labels(
                accept_key  = getattr(state, "phone_accept_key", None) or "",
                decline_key = getattr(state, "phone_decline_key", None) or "",
                mute_key    = getattr(state, "phone_mute_key", None) or "",
                speaker_key = getattr(state, "phone_speaker_key", None) or "",
            )
        except Exception as e:
            self._on_log(f"[PHONE] refresh shortcuts KO : {e}")

    def _phone_toggle_overlay(self):
        """Ouvre l'overlay smartphone, ou le referme s'il est deja ouvert
        (toggle par re-appui, comme prevu dans la spec). A l'ouverture, la
        liste des contacts est rafraichie depuis l'annuaire courant, et
        l'overlay s'ouvre sur l'ecran adapte a l'etat d'appel courant
        (Contacts par defaut, ou Appel entrant / sortant / en cours si
        un appel est en cours)."""
        ov = self._phone_ensure_overlay()
        if ov is None:
            return
        # Si la CAMERA est ouverte, le raccourci telephone la FERME (elle
        # reaffiche le telephone via son callback), plutot que de basculer
        # l'overlay masque derriere : sinon on arreterait le listener global
        # et les raccourcis camera deviendraient inutilisables.
        pw = getattr(ov, "_photo_window", None)
        if pw is not None and pw.isVisible():
            pw.close()
            return
        if ov.is_open():
            ov.hide_animated()
        else:
            self._phone_refresh_overlay_contacts()
            # Choix de l'ecran initial selon l'etat d'appel.
            st = getattr(self, "_phone_state", "idle")
            peer = getattr(self, "_phone_peer", None) or ""
            if st == "ringing_out":
                ov.show_screen_outgoing(peer)
            elif st == "ringing_in":
                ov.show_screen_incoming(peer)
            elif st == "in_call":
                ov.show_screen_in_call(peer)
            else:
                ov.show_screen_home()
            ov.show_animated()

    def _phone_refresh_overlay_contacts(self):
        """Rafraichit la liste des contacts affichee dans l'overlay a
        partir de l'annuaire et des joueurs actuellement connectes.
        No-op si l'overlay n'existe pas encore (rien a rafraichir)."""
        ov = self._phone_overlay
        if ov is None:
            return
        try:
            online = set()
            if _CORE_AVAILABLE:
                online = set(state.players.keys())
            my_name = state.my_name if _CORE_AVAILABLE else ""
            # D4 etape 3 : ensemble des contacts avec MP non lus
            # -> badge rouge sur leur enveloppe.
            # Calcul du timestamp du dernier message (sent OU received) par
            # contact pour le tri par recence (24/05/2026). Les contacts
            # sans convo n'apparaissent pas dans ce dict -> tri alpha en
            # fin de liste de leur groupe (connecte / deconnecte).
            unread_set = set()
            last_msg_ts_map = {}
            convos = self._phone_messages.get("conversations", {})
            for pseudo, c in convos.items():
                if not isinstance(c, dict):
                    continue
                if int(c.get("unread", 0)) > 0:
                    unread_set.add(pseudo)
                # Dernier ts de la convo = max sur sent + received.
                latest = 0.0
                for m in c.get("sent", []):
                    try:
                        ts = float(m.get("ts", 0.0))
                        if ts > latest:
                            latest = ts
                    except Exception:
                        pass
                for m in c.get("received", []):
                    try:
                        ts = float(m.get("ts", 0.0))
                        if ts > latest:
                            latest = ts
                    except Exception:
                        pass
                if latest > 0:
                    last_msg_ts_map[pseudo] = latest
            ov.refresh_contacts(
                self._phone_annuaire, online, my_name,
                unread_set=unread_set,
                # [D5] Provider de photos : bytes JPEG en cache ou None.
                # Aussi, declenche une request asynchrone pour les pseudos
                # qu'on n'a jamais vus (pour qu'ils s'affichent au refresh
                # suivant).
                photo_provider=self._photo_provider_for_contacts,
                last_msg_ts_map=last_msg_ts_map,
            )
            if hasattr(ov, "refresh_call_history"):
                ov.refresh_call_history(self._call_history)
        except Exception as e:
            self._on_log(f"[PHONE] refresh contacts KO : {e}")

    def _photo_provider_for_contacts(self, pseudo: str):
        """[D5] Wrapper du provider du manager qui declenche TOUJOURS une
        request asynchrone, meme si on a deja la photo en cache. C'est
        le seul moyen de detecter qu'un pair a change sa photo : on envoie
        notre hash en if-none-match, le serveur repond 'unchanged' si rien
        n'a bouge, ou 'ok' avec la nouvelle photo si elle a change. Le
        cout d'une request est minime (un petit message JSON), et la
        request est anti-double-shot (un seul en vol par pseudo)."""
        try:
            b = self._profile_photos.get_peer_photo_bytes(pseudo)
        except Exception:
            b = None
        # Toujours demander, meme si on a deja une photo. Si le hash sur
        # serveur est le meme, on recoit 'unchanged' et c'est tout. Si
        # different, on recoit la nouvelle photo et update_avatar_for
        # se chargera du refresh.
        try:
            self._profile_photos.request_peer_photo(pseudo)
        except Exception:
            pass
        return b

    def _phone_annuaire_enrich(self, pseudos):
        """Enrichit l'annuaire avec une liste de pseudos vus connectes,
        sauvegarde si quelque chose a change, et rafraichit l'overlay si
        celui-ci est ouvert. Appele a la reception de la liste des joueurs
        (welcome) et a chaque join."""
        try:
            my_name = state.my_name if _CORE_AVAILABLE else ""
            changed = _phone_enrich_annuaire(
                self._phone_annuaire, pseudos, my_name
            )
            if changed:
                _phone_save_annuaire(self._phone_annuaire)
            # Refresh live : meme si l'annuaire n'a pas change (pseudo
            # deja connu), le statut connecte/deconnecte a pu bouger.
            self._phone_refresh_overlay_contacts()
        except Exception as e:
            self._on_log(f"[PHONE] enrich annuaire KO : {e}")

    @Slot(str)
    def _on_phone_overlay_call(self, pseudo: str):
        """Clic sur l'icone telephone d'un contact connecte : lance un
        appel. Reutilise le chemin existant (phone_call_request) ; en D4
        etape 1 on passe par le meme mecanisme que la page debug."""
        if not pseudo:
            return
        if self._phone_state != "idle":
            self._phone_log(f"[IGNORE] Deja en appel (clic sur {pseudo}).")
            return
        if not (_CORE_AVAILABLE and state.connected):
            self._phone_log("[IGNORE] Pas connecte au serveur.")
            return
        try:
            ok = _core._ws_send_safe({
                "type": "phone_call_request", "target": pseudo,
            })
        except Exception as e:
            ok = False
            self._phone_log(f"[ERREUR] _ws_send_safe : {e}")
        if ok:
            self._phone_log(f"→ phone_call_request (target={pseudo}) [overlay]")
        else:
            self._phone_log("[ERREUR] Envoi phone_call_request echoue.")

    @Slot(str)
    def _on_phone_overlay_message(self, pseudo: str):
        """Clic sur l'icone lettre d'un contact : ouvre l'ecran conversation
        avec ce contact. Marque comme lu (vide le compteur unread) et
        rafraichit la liste affichee."""
        if not pseudo:
            return
        ov = self._phone_overlay
        if ov is None:
            return
        try:
            # Marquer comme lu + sauvegarder si changement.
            if _phone_mark_read(self._phone_messages, pseudo):
                _phone_save_messages(self._phone_messages)
            # Construire la liste chronologique + recuperer le brouillon.
            items = _phone_merge_messages(self._phone_messages, pseudo)
            convo = self._phone_messages.get("conversations", {}).get(
                pseudo, {}
            )
            draft = convo.get("draft", "") if isinstance(convo, dict) else ""
            ov.show_screen_conversation(pseudo, items, draft)
            # Rafraichir la liste Contacts (badge unread efface).
            self._phone_refresh_overlay_contacts()
        except Exception as e:
            self._on_log(f"[PHONE] open conversation KO : {e}")

    @Slot(str, str)
    def _on_phone_overlay_send_message(self, target: str, body: str):
        """Envoi d'un MP texte depuis l'ecran conversation. Stockage local
        immediat + envoi WS au serveur (qui le routera au destinataire).
        Si l'envoi WS echoue (deconnexion), le message reste stocke local
        comme envoye (la spec ne prevoit pas de retry / dead letter)."""
        if not target or not body:
            return
        body = body[:PHONE_MAX_BODY_LEN]
        # 1) Envoi WS au serveur.
        try:
            ok = _core._ws_send_safe({
                "type":   "phone_message_send",
                "target": target,
                "body":   body,
            })
        except Exception as e:
            ok = False
            self._on_log(f"[PHONE-MSG] _ws_send_safe KO : {e}")
        if not ok:
            self._phone_log("[PHONE-MSG] Envoi echoue (non connecte ?)")
            # On stocke quand meme localement : le message a ete redige,
            # autant que l'utilisateur le voie dans son historique.
        # 2) Stockage local (envoye).
        try:
            ts = time.time()
            _phone_append_sent(self._phone_messages, target, body, ts)
            # Vider le brouillon en meme temps (la spec : "envoyer" = clear).
            _phone_set_draft(self._phone_messages, target, "")
            _phone_save_messages(self._phone_messages)
        except Exception as e:
            self._on_log(f"[PHONE-MSG] stockage local KO : {e}")
        # 3) Si l'overlay affiche cette conversation, refresh.
        ov = self._phone_overlay
        if ov is not None and getattr(ov, "_convo_pseudo", "") == target:
            try:
                items = _phone_merge_messages(self._phone_messages, target)
                ov.refresh_conversation(items)
            except Exception:
                pass
        # 4) Refresh Contacts : le ts du dernier message a change donc le tri
        #    par recence (24/05/2026) doit se reorganiser. En pratique l'ecran
        #    contacts n'est pas visible au moment d'un envoi (l'utilisateur est
        #    dans l'ecran convo), mais on rafraichit quand meme pour que la
        #    liste soit a jour des le prochain affichage.
        self._phone_refresh_overlay_contacts()

    @Slot()
    def _on_phone_send_image(self, pseudo: str, path: str):
        """Envoi d'un screenshot dans la conversation : charge l'image, la
        compresse (JPEG, <=720px, sous la limite de frame WS), l'envoie en
        base64 au serveur qui la route, et la stocke localement en bulle
        image ("[img]<fichier>" dans l'historique)."""
        if not pseudo or not path:
            return
        try:
            import base64
            from PySide6.QtCore import QBuffer, QIODevice
            img = QImage(path)
            if img.isNull():
                self._phone_log(f"[IMG] illisible : {path}")
                return
            # Qualite d'envoi : on privilegie une resolution/qualite ELEVEE
            # (l'image doit rester nette meme ouverte en plein ecran sur le
            # moniteur), en ne baissant que ce qu'il faut pour tenir dans le
            # budget de transmission (PHONE_MAX_IMG_B64). On essaie d'abord la
            # pleine largeur (plafonnee a 1600 px) a bonne qualite, puis on
            # degrade progressivement qualite PUIS resolution si trop lourd.
            _MAX_W = 1600
            if img.width() > _MAX_W:
                img = img.scaledToWidth(_MAX_W, Qt.SmoothTransformation)
            raw = b""
            _fits = False
            _widths = [w for w in (img.width(), 1280, 1024, 800)
                       if w <= img.width()]
            # dedoublonner en gardant l'ordre
            _seen_w = set(); _widths = [w for w in _widths
                                        if not (w in _seen_w or _seen_w.add(w))]
            for _w in _widths:
                im2 = (img if _w == img.width()
                       else img.scaledToWidth(_w, Qt.SmoothTransformation))
                for quality in (85, 72, 58, 45, 32):
                    buf = QBuffer()
                    buf.open(QIODevice.WriteOnly)
                    im2.save(buf, "JPEG", quality)
                    raw = bytes(buf.data())
                    buf.close()
                    if len(raw) * 4 // 3 <= PHONE_MAX_IMG_B64:
                        _fits = True
                        break
                if _fits:
                    break
            b64 = base64.b64encode(raw).decode("ascii")
            if len(b64) > PHONE_MAX_IMG_B64:
                self._phone_log(f"[IMG] trop lourde meme compressee "
                                f"({len(b64)} b64) : abandon")
                return
            # 1) Envoi WS.
            try:
                ok = _core._ws_send_safe({
                    "type": "phone_image_send",
                    "target": pseudo,
                    "data": b64,
                })
            except Exception as e:
                ok = False
                self._on_log(f"[IMG] _ws_send_safe KO : {e}")
            if not ok:
                self._phone_log("[IMG] Envoi echoue (non connecte ?)")
            # 2) Cache local + historique (meme si l'envoi a echoue, pour
            #    que l'utilisateur voie sa bulle).
            ts = time.time()
            fname = f"{int(ts * 1000)}_{pseudo}_out.jpg"
            try:
                PHONE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                (PHONE_IMAGES_DIR / fname).write_bytes(raw)
            except Exception as e:
                self._phone_log(f"[IMG] cache local KO : {e}")
            _phone_append_sent(self._phone_messages, pseudo,
                               PHONE_IMG_PREFIX + fname, ts)
            _phone_save_messages(self._phone_messages)
            self._phone_log(f"[IMG] envoyee -> {pseudo} "
                            f"({len(raw)} o jpeg, {len(b64)} b64)")
            # 3) Refresh la conversation affichee.
            ov = self._phone_overlay
            if ov is not None and getattr(ov, "_convo_pseudo", "") == pseudo:
                try:
                    items = _phone_merge_messages(self._phone_messages, pseudo)
                    ov.refresh_conversation(items)
                except Exception:
                    pass
        except Exception as e:
            self._phone_log(f"[IMG] envoi KO : {e}")

    def _on_phone_image_received(self, sender: str, b64: str, ts: float):
        """Reception d'une image (base64) : decode, stocke dans le cache
        local, puis reutilise TOUT le flux texte (annuaire, non-lus, notif,
        refresh) via _on_phone_message_received avec un body '[img]<fichier>'."""
        if not sender or not b64:
            return
        try:
            import base64
            raw = base64.b64decode(b64.encode("ascii"), validate=True)
        except Exception:
            self._phone_log(f"[IMG] base64 invalide de {sender} : ignore")
            return
        fname = f"{int((ts or time.time()) * 1000)}_{sender}_in.jpg"
        try:
            PHONE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            (PHONE_IMAGES_DIR / fname).write_bytes(raw)
        except Exception as e:
            self._phone_log(f"[IMG] cache reception KO : {e}")
            return
        self._on_phone_message_received(sender, PHONE_IMG_PREFIX + fname, ts)

    def _on_phone_overlay_back_contacts(self):
        """Fleche retour depuis l'ecran conversation : sauvegarde le
        brouillon courant + retour a l'ecran Contacts."""
        ov = self._phone_overlay
        if ov is None:
            return
        # Sauvegarde du draft (au cas ou le dernier sig_draft_changed
        # n'aurait pas eu le temps d'arriver).
        pseudo = getattr(ov, "_convo_pseudo", "")
        if pseudo:
            try:
                draft = ov._convo_input.toPlainText()
                _phone_set_draft(self._phone_messages, pseudo, draft)
                _phone_save_messages(self._phone_messages)
            except Exception:
                pass
        # Refresh Contacts (au cas ou des MP soient arrives pendant qu'on
        # etait sur la conversation) + bascule.
        self._phone_refresh_overlay_contacts()
        ov.show_screen_contacts()

    @Slot(str, str)
    def _on_phone_overlay_draft_changed(self, pseudo: str, draft: str):
        """Le texte du champ a change : on sauvegarde le brouillon (cas
        'appel pendant redaction' de la spec). On debounce avec un timer
        pour ne pas faire un I/O fichier a chaque touche."""
        if not pseudo:
            return
        try:
            _phone_set_draft(self._phone_messages, pseudo, draft)
        except Exception:
            return
        # Debounce : reporte la sauvegarde reelle de 1s, et re-arme le
        # timer a chaque frappe. Si l'utilisateur arrete de taper 1s,
        # on ecrit.
        if not hasattr(self, "_phone_draft_save_timer"):
            self._phone_draft_save_timer = QTimer(self)
            self._phone_draft_save_timer.setSingleShot(True)
            self._phone_draft_save_timer.timeout.connect(
                lambda: _phone_save_messages(self._phone_messages)
            )
        self._phone_draft_save_timer.start(1000)

    @Slot(dict)
    def _on_phone_server_msg(self, data: dict):
        """Route un message multijoueur (mp_*) recu du serveur vers l'app du
        telephone actuellement affichee (qui expose handle_server_msg)."""
        ov = getattr(self, "_phone_overlay", None)
        if ov is None:
            return
        app = getattr(ov, "_current_app", None)
        if app is not None and hasattr(app, "handle_server_msg"):
            try:
                app.handle_server_msg(data)
            except Exception:
                pass

    @Slot(str, str, float)
    def _on_phone_message_received(self, sender: str, body: str, ts: float):
        """Reception d'un MP texte depuis le serveur. Stocke localement,
        joue la notification audio, met a jour la pastille rouge sur
        l'enveloppe du contact, et rafraichit la conversation si elle
        est actuellement affichee."""
        if not sender or not body:
            return
        try:
            # 1) Stockage local (recu + increment unread).
            _phone_append_received(self._phone_messages, sender, body, ts)
            # Si l'expediteur n'est pas encore dans l'annuaire (cas rare :
            # joueur deja connu sur le serveur mais croise pour la 1re fois
            # via un MP), on l'ajoute aussi pour qu'il apparaisse dans la
            # liste. _phone_enrich_annuaire est idempotent.
            try:
                my_name = state.my_name if _CORE_AVAILABLE else ""
                if _phone_enrich_annuaire(
                    self._phone_annuaire, [sender], my_name
                ):
                    _phone_save_annuaire(self._phone_annuaire)
            except Exception:
                pass
            _phone_save_messages(self._phone_messages)
            self._phone_log(
                f"← phone_message_received de {sender} ({len(body)} char)"
            )

            # 2) Determiner si la conversation avec ce sender est deja
            #    ouverte a l'ecran. Si oui, pas besoin de jouer la notif
            #    (l'utilisateur voit le message arriver en direct dans les
            #    bulles, jouer un son serait redondant et bruyant).
            ov = self._phone_overlay
            current_convo_open = False
            if ov is not None:
                current_convo_open = (
                    ov.is_open()
                    and ov._stack.currentWidget() is ov._page_convo
                    and getattr(ov, "_convo_pseudo", "") == sender
                )

            # 3) Notification sonore (son 1s, slider 'Sonnerie tel.').
            #    Skip si :
            #    - la convo avec ce sender est deja ouverte (l'utilisateur
            #      voit le message arriver en direct), OU
            #    - ce sender a deja des MP non lus avant celui-ci (la notif
            #      a deja sonne, evite le spam si plusieurs MP arrivent en
            #      rafale d'un meme contact). Le compteur unread vient d'etre
            #      incremente par _phone_append_received ; il vaut donc 1
            #      pour le premier MP de la salve, 2+ pour les suivants.
            convo_state = self._phone_messages.get(sender, {})
            unread_count = int(convo_state.get("unread", 0))
            already_notified = unread_count > 1

            if not current_convo_open and not already_notified:
                audio = getattr(state, "audio_io", None) if _CORE_AVAILABLE \
                        else None
                if audio is not None:
                    try:
                        audio.play_phone_notif()
                    except Exception as e:
                        self._phone_log(f"[AUDIO] play_phone_notif KO : {e}")

            # 4) Refresh UI : pastille rouge sur l'enveloppe + conversation
            #    si elle est ouverte avec ce contact.
            if ov is not None:
                if current_convo_open:
                    if _phone_mark_read(self._phone_messages, sender):
                        _phone_save_messages(self._phone_messages)
                    try:
                        items = _phone_merge_messages(
                            self._phone_messages, sender
                        )
                        ov.refresh_conversation(items)
                    except Exception:
                        pass
                # Refresh Contacts : badge unread + presence eventuelle
                # du nouvel expediteur dans l'annuaire.
                self._phone_refresh_overlay_contacts()
        except Exception as e:
            self._on_log(f"[PHONE-MSG] reception KO : {e}")

    @Slot(str, str, str, str)
    def _on_profile_photo_response(self, target: str, status: str,
                                   new_hash: str, data_b64: str):
        """[D5] Reponse a une demande de photo de profil. Si la photo est
        mise a jour, on rafraichit les emplacements UI qui pourraient
        afficher cette photo (header MP, header appel, item contacts)."""
        try:
            changed = self._profile_photos.handle_response(
                target, status, new_hash, data_b64
            )
        except Exception as e:
            self._on_log(f"[PROFILE] reponse KO : {e}")
            return
        if not changed:
            return
        # Refresh des emplacements potentiellement concernes par 'target'.
        ov = self._phone_overlay
        if ov is None:
            return
        try:
            ov.update_avatar_for(target)
        except Exception:
            pass

    @Slot(str)
    def _on_phone_overlay_forget(self, pseudo: str):
        """Clic sur la croix 'oublier' d'un contact deconnecte : le retire
        de l'annuaire (fichier + affichage)."""
        if not pseudo:
            return
        try:
            removed = _phone_forget_contact(self._phone_annuaire, pseudo)
            if removed:
                _phone_save_annuaire(self._phone_annuaire)
                self._phone_log(f"[ANNUAIRE] Contact oublie : {pseudo}")
            self._phone_refresh_overlay_contacts()
        except Exception as e:
            self._on_log(f"[PHONE] forget contact KO : {e}")

    @Slot(bool)
    def _on_phone_overlay_mute(self, active: bool):
        """Toggle mute micro depuis l'ecran 'En appel'. Coupe / restaure
        la transmission du micro. Reutilise l'API audio existante : on
        force le multiplicateur 'mon micro' a 0 ou 1.
        active=True  -> micro coupe (transmission a 0)
        active=False -> micro ouvert (transmission a 1)"""
        self._phone_log(
            f"[PHONE] Mute micro : {'ON' if active else 'OFF'}"
        )
        audio = getattr(state, "audio_io", None) if _CORE_AVAILABLE else None
        if audio is None:
            return
        try:
            # set_capture_muted est l'API exposee par audio_io pour
            # couper la capture cote envoi (sans toucher au gate ni
            # au PTT). Si l'API n'existe pas sur cette version d'audio_io,
            # on log et on continue (echec gracieux).
            if hasattr(audio, "set_capture_muted"):
                audio.set_capture_muted(bool(active))
            else:
                self._phone_log(
                    "[PHONE] audio_io.set_capture_muted absent "
                    "(toggle visuel uniquement)"
                )
        except Exception as e:
            self._phone_log(f"[PHONE] mute toggle KO : {e}")

    @Slot(bool)
    def _on_phone_overlay_speaker(self, active: bool):
        """Toggle haut-parleur depuis l'ecran 'En appel'. D4b : envoi de
        l'etat HP au serveur avec la liste actuelle des voisins ≤5m.
        Le serveur diffusera les autorisations 'HP active' aux voisins
        et 'neighbors_update' au peer."""
        self._phone_log(
            f"[PHONE] Haut-parleur : {'ON' if active else 'OFF'}"
        )
        # On garde la valeur locale pour cohérence UI / boutons.
        self._phone_speaker_on = bool(active)
        # Sync core + envoi serveur (D4b)
        if _CORE_AVAILABLE:
            try:
                state.phone_hp_active = bool(active)
                # Force=True : envoi immediat sans throttle (action utilisateur).
                # Si active=True : envoie la liste actuelle des voisins ≤5m.
                # Si active=False : envoie une liste vide (cleanup serveur).
                ok = _core._phone_hp_send_state(force=True)
                if not ok:
                    self._phone_log(
                        "[PHONE] HP : envoi phone_speaker_state echoue "
                        "(pas connecte ou pas en appel)"
                    )
            except Exception as e:
                self._phone_log(f"[PHONE] HP : erreur sync core : {e}")

    def _on_phone_overlay_settings(self):
        """[D5+] Bascule l'overlay vers l'ecran Reglages profil (page
        interne du telephone). MainWindow refresh la preview a partir
        du manager des photos."""
        ov = self._phone_overlay
        if ov is None:
            return
        try:
            ov.show_screen_settings()
            self._phone_refresh_settings_preview()
        except Exception as e:
            self._on_log(f"[PHONE] settings show KO : {e}")

    def _phone_refresh_settings_preview(self):
        """Recupere les bytes JPEG actuels + zoom du manager, et pousse
        a l'overlay pour rafraichir l'ecran Reglages. Appele apres
        chaque action utilisateur sur cet ecran."""
        ov = self._phone_overlay
        if ov is None:
            return
        b = None
        try:
            if self._profile_photos.has_local_photo():
                with open(PHONE_PROFILE_PHOTO_FILE, "rb") as f:
                    b = f.read()
        except Exception:
            b = None
        try:
            z = self._profile_photos.get_zoom()
        except Exception:
            z = 1.0
        try:
            ov.refresh_settings_preview(b, int(round(z * 100)))
        except Exception:
            pass

    def _on_phone_overlay_settings_back(self):
        """Fleche retour de l'ecran Reglages profil : revient a l'origine
        (app Parametres si on y venait de la, sinon Contacts)."""
        ov = self._phone_overlay
        if ov is None:
            return
        try:
            if getattr(ov, "_settings_origin", "contacts") == "settings":
                ov.show_screen_settings_app()
            else:
                ov.show_screen_contacts()
                self._phone_refresh_overlay_contacts()
        except Exception:
            pass

    def _on_phone_overlay_settings_choose(self):
        """Bouton 'Choisir...' : ouvre un QFileDialog, applique la photo."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(
            self, "Choisir une photo",
            "",
            "Images (*.jpg *.jpeg *.png *.webp *.bmp)",
        )
        if not path:
            return
        ok, msg = self._profile_photos.set_local_photo_from_file(path)
        if not ok:
            QMessageBox.warning(self, "Photo de profil", msg)
        else:
            self._phone_log("[PROFILE] Nouvelle photo enregistree.")
        self._phone_refresh_settings_preview()

    def _on_phone_overlay_settings_remove(self):
        """Bouton 'Supprimer' : demande confirmation, puis efface."""
        from PySide6.QtWidgets import QMessageBox
        if not self._profile_photos.has_local_photo():
            return
        ret = QMessageBox.question(
            self, "Supprimer la photo",
            "Supprimer votre photo de profil ?\n\n"
            "Note : la photo deja partagee avec d'autres joueurs reste "
            "visible chez eux jusqu'a ce que vous en choisissiez une "
            "nouvelle.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self._profile_photos.clear_local_photo()
        self._phone_log("[PROFILE] Photo locale supprimee.")
        self._phone_refresh_settings_preview()

    def _on_phone_overlay_settings_zoom_in(self):
        """Bouton '+' : zoom IN = delta zoom negatif (factor diminue)."""
        from PySide6.QtWidgets import QMessageBox
        ok, msg = self._profile_photos.adjust_zoom(-PHONE_PROFILE_ZOOM_STEP)
        if not ok and "source" in msg.lower():
            QMessageBox.information(self, "Zoom",
                "Choisissez d'abord une photo avant de zoomer.")
        self._phone_refresh_settings_preview()

    def _on_phone_overlay_settings_zoom_out(self):
        """Bouton '-' : zoom OUT = delta zoom positif (factor augmente)."""
        from PySide6.QtWidgets import QMessageBox
        ok, msg = self._profile_photos.adjust_zoom(+PHONE_PROFILE_ZOOM_STEP)
        if not ok and "source" in msg.lower():
            QMessageBox.information(self, "Zoom",
                "Choisissez d'abord une photo avant de zoomer.")
        self._phone_refresh_settings_preview()

    def _on_phone_overlay_settings_move(self, dx_sign: int, dy_sign: int):
        """Flèches directionnelles : decale le cadrage selon le pas
        conseille par le manager (proportionnel au zoom)."""
        from PySide6.QtWidgets import QMessageBox
        step = self._profile_photos.get_offset_step()
        ok, msg = self._profile_photos.adjust_offset(
            dx_sign * step, dy_sign * step
        )
        if not ok and "source" in msg.lower():
            QMessageBox.information(self, "Cadrage",
                "Choisissez d'abord une photo avant de cadrer.")
        self._phone_refresh_settings_preview()

    def _on_phone_overlay_settings_recenter(self):
        """Bouton central du pad : recentre (offset 0,0)."""
        from PySide6.QtWidgets import QMessageBox
        ok, msg = self._profile_photos.reset_offset()
        if not ok and "source" in msg.lower():
            QMessageBox.information(self, "Cadrage",
                "Choisissez d'abord une photo avant de cadrer.")
        self._phone_refresh_settings_preview()

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

    # ------------------------------------------------------------
    # Log audio RX detaille (diagnostic crackling, ajout 02/06/2026)
    # ------------------------------------------------------------

    @Slot(bool)
    def _on_audio_rx_log_toggled(self, checked: bool):
        """Toggle de la case 'Activer le log audio detaille'.

        Activation a chaud : pas de redemarrage client necessaire.
        - Sauve l'etat dans _cfg pour persistance entre sessions.
        - Appelle state.audio_io.set_audio_rx_log_enabled() qui ouvre
          ou ferme le fichier CSV dans circusvoip_debug/audio_rx/.
        - Met a jour le label d'info sous la case.

        Le module circusvoip_audio_rx_logger est autonome : si l'init
        echoue (disque plein, perms), on remet la case decochee et on
        previent l'utilisateur via le log debug.
        """
        self._cfg["audio_rx_log_enabled"] = bool(checked)
        _save_cfg(self._cfg)

        # Recuperer audio_io depuis le core (instance partagee)
        audio_io = None
        if _CORE_AVAILABLE:
            try:
                audio_io = _core.state.audio_io
            except Exception:
                audio_io = None

        if audio_io is None:
            self._on_log(
                "[AUDIO RX LOG] audio_io non disponible, toggle ignore"
            )
            self._refresh_audio_rx_log_info()
            return

        # Pseudo (necessaire pour le nom de fichier)
        try:
            pseudo = _core.state.player_name or "Joueur"
        except Exception:
            pseudo = "Joueur"

        # Dossier debug (ou ecrire le sous-dossier audio_rx/)
        debug_dir = None
        if _CORE_AVAILABLE:
            try:
                debug_dir = _core._DEBUG_DIR
            except Exception:
                debug_dir = None
        if debug_dir is None:
            from pathlib import Path
            debug_dir = Path("circusvoip_debug")

        # Appel a chaud (le module logger gere thread-safe l'ouverture
        # ou fermeture du fichier CSV).
        try:
            ok = audio_io.set_audio_rx_log_enabled(
                bool(checked), pseudo=pseudo, debug_dir=debug_dir
            )
            if checked and not ok:
                self._on_log(
                    "[AUDIO RX LOG] Echec activation : "
                    "verifier perms / disque dispo / module present"
                )
            elif checked and ok:
                self._on_log(
                    f"[AUDIO RX LOG] Active. Fichier dans "
                    f"{debug_dir / 'audio_rx'}/"
                )
            elif not checked:
                self._on_log("[AUDIO RX LOG] Desactive")
        except Exception as e:
            self._on_log(f"[AUDIO RX LOG] Erreur toggle : {e}")

        self._refresh_audio_rx_log_info()

    def _refresh_audio_rx_log_info(self):
        """Met a jour le texte d'info sous la case 'log audio detaille'."""
        if not hasattr(self, "lbl_audio_rx_log_info"):
            return
        if self.cb_audio_rx_log.isChecked():
            self.lbl_audio_rx_log_info.setText(
                "Actif : trames audio recues + callbacks sounddevice + "
                "stats 30s sont enregistres dans un CSV separe "
                "(circusvoip_debug/audio_rx/). Volume eleve : "
                "~80-160 MB par heure selon le nombre de senders. "
                "Desactiver des que le diagnostic est fait."
            )
            self.lbl_audio_rx_log_info.setStyleSheet(
                "color: #ffaa44; font-size: 9pt;"
            )
        else:
            self.lbl_audio_rx_log_info.setText(
                "Desactive : pas d'enregistrement detaille. Les logs "
                "habituels [AUDIO STATS] toutes les 30s restent actifs "
                "dans le log debug principal."
            )
            self.lbl_audio_rx_log_info.setStyleSheet(
                "color: #888; font-size: 9pt;"
            )

    # ------------------------------------------------------------
    # Masque DisplayInfo (v0.2, feature 3)
    # ------------------------------------------------------------
    # Voir la classe DisplayInfoMaskWindow et la constante
    # DISPLAYINFO_MASK_REF_4K plus haut dans le fichier.

    @Slot(bool)
    def _on_displayinfo_mask_toggled(self, checked: bool):
        """Toggle de la case 'Masquer la zone DisplayInfo'. Sauve dans
        _cfg et applique immediatement (montre ou cache le mask, selon
        l'etat OCR courant)."""
        self._cfg["displayinfo_mask_enabled"] = bool(checked)
        _save_cfg(self._cfg)
        self._on_log(f"[MASK] DisplayInfo mask = {checked}")
        # Application immediate via la routine de tick (qui decide
        # show/hide selon toutes les conditions).
        self._update_displayinfo_mask()

    @Slot(bool)
    def _on_displayinfo_mask_obs_toggled(self, checked: bool):
        """Toggle de la case 'Activer la source OBS du masque'
        (v0.2 alpha 058). Sauve dans _cfg et applique immediatement.

        Quand active : une fenetre offscreen est creee a cote de la
        fenetre masque ecran ; elle se branche sur le signal d'image du
        meme worker et OBS peut la capturer comme source de fenetre.
        Quand desactivee : la fenetre offscreen est detruite.

        Pas d'effet visible a l'ecran (la fenetre est positionnee hors
        ecran)."""
        self._cfg["displayinfo_mask_obs_enabled"] = bool(checked)
        _save_cfg(self._cfg)
        self._on_log(f"[MASK OBS] source OBS = {checked}")
        # Application immediate via la routine de tick.
        self._update_displayinfo_mask()

    @Slot(int, bool)
    def _on_mask_fps_toggled(self, fps_val: int, checked: bool):
        """Handler des cases FPS du masque (5/10/20/30/60).
        Comportement radio : decocher les autres si on coche celle-ci.
        Si on tente de decocher la case actuellement cochee, on re-coche
        immediatement (au moins une case doit etre selectionnee)."""
        # Eviter la recursion : pendant qu'on programme setChecked(False)
        # sur les autres, le signal toggled se redeclenche -> on garde
        # un flag.
        if getattr(self, "_mask_fps_updating", False):
            return
        if not checked:
            # Tentative de decocher la case courante : interdit, on
            # re-coche silencieusement.
            self._mask_fps_updating = True
            try:
                cb = self.cb_mask_fps.get(fps_val)
                if cb is not None:
                    cb.setChecked(True)
            finally:
                self._mask_fps_updating = False
            return
        # Une nouvelle case est cochee : on decoche toutes les autres.
        self._mask_fps_updating = True
        try:
            for v, cb in self.cb_mask_fps.items():
                if v != fps_val:
                    cb.setChecked(False)
        finally:
            self._mask_fps_updating = False
        # Sauve dans la config et applique a chaud.
        self._cfg["displayinfo_mask_fps"] = int(fps_val)
        _save_cfg(self._cfg)
        self._on_log(f"[MASK] Frequence masque = {fps_val} FPS")
        # Notifie le masque existant (s'il tourne) que sa frequence change.
        try:
            mask = getattr(self, "_displayinfo_mask", None)
            if mask is not None:
                mask.update_fps(int(fps_val))
        except Exception as e:
            self._on_log(f"[MASK] update_fps KO : {e}")

    @Slot(int)
    def _on_mask_height_changed(self, value: int):
        """Handler du QSpinBox hauteur du masque (v0.2 alpha 045).
        Multiplicateur de la hauteur de la zone OCR (defaut 19 lignes
        du HUD DisplayInfo). Sauve dans la config et applique a chaud
        au masque actif si present."""
        try:
            self._cfg["displayinfo_mask_height_factor"] = int(value)
            _save_cfg(self._cfg)
            self._on_log(f"[MASK] Hauteur masque = {value} (x zone OCR)")
            # Applique au masque actif s'il existe : on appelle
            # _apply_geometry() qui relira la config et recalculera.
            mask = getattr(self, "_displayinfo_mask", None)
            if mask is not None:
                try:
                    mask._apply_geometry()
                except Exception as e:
                    self._on_log(f"[MASK] _apply_geometry KO : {e}")
        except Exception as e:
            self._on_log(f"[MASK] _on_mask_height_changed KO : {e}")

    @Slot(float)
    def _on_mask_width_changed(self, value: float):
        """Handler du QDoubleSpinBox largeur du masque (v0.2 alpha 045).
        Multiplicateur de la largeur de la zone OCR (defaut 1.0). Sauve
        dans la config et applique a chaud au masque actif si present."""
        try:
            self._cfg["displayinfo_mask_width_factor"] = float(value)
            _save_cfg(self._cfg)
            self._on_log(f"[MASK] Largeur masque = {value:.2f} (x zone OCR)")
            mask = getattr(self, "_displayinfo_mask", None)
            if mask is not None:
                try:
                    mask._apply_geometry()
                except Exception as e:
                    self._on_log(f"[MASK] _apply_geometry KO : {e}")
        except Exception as e:
            self._on_log(f"[MASK] _on_mask_width_changed KO : {e}")

    def _resolve_ocr_screen(self):
        """Retourne le QScreen qui contient la zone OCR (state.zone_coords),
        ou None si aucune zone n'est definie ou aucun ecran ne contient
        le centre de la zone.

        zone_coords est en pixels PHYSIQUES (le client a active
        SetProcessDpiAwareness(2)). QGuiApplication.screenAt() accepte
        des coordonnees logiques, donc on convertit phys -> logique
        en cherchant l'ecran qui contient la position et en divisant
        par son devicePixelRatio."""
        z = getattr(state, "zone_coords", None)
        if not isinstance(z, dict):
            return None
        try:
            cx_phys = int(z.get("left", 0)) + int(z.get("width", 0)) // 2
            cy_phys = int(z.get("top",  0)) + int(z.get("height", 0)) // 2
        except (TypeError, ValueError):
            return None
        # Parcourir les ecrans, trouver celui dont la geometrie physique
        # contient le centre. Pour la conversion, on multiplie la
        # geometrie logique par le DPR.
        try:
            screens = QGuiApplication.screens()
        except Exception:
            return None
        for sc in screens:
            try:
                geo = sc.geometry()
                dpr = sc.devicePixelRatio() or 1.0
                # Geometrie physique de l'ecran
                px_left   = int(round(geo.x()      * dpr))
                px_top    = int(round(geo.y()      * dpr))
                px_right  = int(round((geo.x() + geo.width())  * dpr))
                px_bottom = int(round((geo.y() + geo.height()) * dpr))
                if (px_left <= cx_phys < px_right
                        and px_top <= cy_phys < px_bottom):
                    return sc
            except Exception:
                continue
        return None

    def _update_displayinfo_mask(self):
        """Decide montrer/cacher le mask DisplayInfo (fenetre ecran et/ou
        fenetre source OBS) selon :
          1. La case 'Activer le masque DisplayInfo' est cochee (ecran).
          2. La case 'Activer la source OBS du masque' est cochee (OBS).
          3. state.zone_coords est definie (on connait l'ecran cible).
          4. state.my_pos_ts est recent (< DISPLAYINFO_MASK_STALE_S sec).
          5. state.mask_force_hidden est False : la machine d'etat clavier
             n'a pas detecte de mobiglass / menu options ouvert.

        v0.2 alpha 060 : les conditions 3-5 sont communes aux deux
        fenetres. Les conditions 1-2 sont independantes. Le worker
        partage (self._displayinfo_mask_service) est demarre automatique-
        ment des qu'au moins une des deux fenetres s'y attache, et arrete
        quand la derniere se detache.

        Appele :
          - immediatement quand une case est toggle (handlers)
          - immediatement quand la machine d'etat clavier change d'etat
            (signal _sig_mask_state_changed)
          - periodiquement par _displayinfo_mask_timer (tous les 500ms)
        """
        try:
            existing     = getattr(self, "_displayinfo_mask", None)
            obs_existing = getattr(self, "_displayinfo_mask_obs", None)

            # ---- Conditions communes (peuvent invalider les deux) ----
            cond_screen_cfg = bool(
                self._cfg.get("displayinfo_mask_enabled", False)
            )
            cond_obs_cfg = bool(
                self._cfg.get("displayinfo_mask_obs_enabled", False)
            )

            # Au moins une des deux doit etre cochee pour eviter le
            # calcul des conditions communes.
            any_enabled = cond_screen_cfg or cond_obs_cfg

            cond_common = True
            if any_enabled and not DEBUG_MASK_BYPASS_OCR_CHECK:
                # Verifier la fraicheur de l'OCR (sauf flag debug bypass)
                pos_ts = getattr(state, "my_pos_ts", 0.0) or 0.0
                if state.my_pos is None or pos_ts <= 0.0:
                    cond_common = False
                else:
                    age = time.monotonic() - pos_ts
                    if age > DISPLAYINFO_MASK_STALE_S:
                        cond_common = False

            # Consulter la machine d'etat clavier (mobiglass / menu).
            tracker = getattr(self, "_mask_key_tracker", None)
            if tracker is not None and cond_common and any_enabled:
                try:
                    tracker.check_position_change(getattr(state, "my_pos", None))
                except Exception:
                    pass
            if getattr(state, "mask_force_hidden", False):
                cond_common = False

            # ---- Resolution de l'ecran cible (utilise par les deux) ----
            screen = None
            if any_enabled and cond_common:
                screen = self._resolve_ocr_screen()
                if screen is None:
                    if DEBUG_MASK_BYPASS_OCR_CHECK:
                        try:
                            screen = QGuiApplication.primaryScreen()
                        except Exception:
                            screen = None
                    if screen is None:
                        cond_common = False

            # ---- Booleens finaux : fenetre par fenetre ----
            want_show_screen = cond_screen_cfg and cond_common
            want_show_obs    = cond_obs_cfg    and cond_common

            # ---- Fenetre ECRAN ----
            # v0.2 alpha 060 : la fenetre ecran est instanciee des qu'au
            # moins une des deux fenetres (ecran ou OBS) est demandee.
            # Raison : c'est la fenetre ecran qui sait calculer la region
            # de capture (via _apply_geometry qui prend en compte zone OCR,
            # DPR, et facteurs config) et la pousser au service. Si seule
            # la fenetre OBS etait demandee, on n'aurait personne pour
            # pousser la region -> worker dort.
            # Si want_show_screen est False, on cree quand meme la fenetre
            # mais on ne la show() pas : elle reste invisible a l'utilisateur,
            # ne s'attache pas au service (donc pas de larsen), mais peut
            # quand meme calculer et pousser la region.
            need_screen_window = want_show_screen or want_show_obs

            if need_screen_window:
                if existing is None:
                    # Defaut 5 FPS pour limiter la conso CPU (cf. note sur
                    # la 1ere occurrence ligne ~10692).
                    cfg_fps = int(self._cfg.get("displayinfo_mask_fps", 5))
                    existing = DisplayInfoMaskWindow(
                        screen,
                        fps=cfg_fps,
                        service=self._displayinfo_mask_service,
                    )
                    self._displayinfo_mask = existing
                else:
                    existing.update_for_screen(screen)

                # show() ou hide() selon want_show_screen.
                if want_show_screen:
                    if not existing.isVisible():
                        existing.show()
                else:
                    # Mode "fenetre ecran fantome" : on a besoin de la fenetre
                    # pour pousser la region au service (cas fenetre OBS
                    # seule), mais l'utilisateur ne doit rien voir.
                    if existing.isVisible():
                        existing.hide()
                    # Note : on N'appelle PAS _stop_worker() ici : la
                    # fenetre ecran reste "attachee" au service tant qu'elle
                    # existe, ce qui maintient le worker en vie. C'est OK
                    # parce que la fenetre OBS est attachee aussi, donc le
                    # worker doit tourner de toute facon. Si l'utilisateur
                    # decoche les deux cases, want_show_obs deviendra False
                    # aussi et on tombera dans la branche else ci-dessous,
                    # qui detruira la fenetre proprement.
            else:
                if existing is not None:
                    try:
                        existing.close()
                    except Exception:
                        pass
                    try:
                        existing.deleteLater()
                    except Exception:
                        pass
                    self._displayinfo_mask = None

            # ---- Fenetre source OBS ----
            # v0.2 alpha 060 : la fenetre OBS est independante de la fenetre
            # ecran. Elle s'attache au meme service partage que la fenetre
            # ecran, et reciproquement le service auto-demarre le worker
            # si elle est seule attachee.
            if want_show_obs:
                if obs_existing is None:
                    obs_existing = DisplayInfoMaskWindowOBS(
                        screen,
                        service=self._displayinfo_mask_service,
                    )
                    # Si la fenetre ecran existe, on s'aligne sur sa
                    # geometrie pour avoir la meme taille de capture.
                    # Sinon on fait un best-effort depuis l'ecran cible.
                    try:
                        if self._displayinfo_mask is not None:
                            ref_geo = self._displayinfo_mask.geometry()
                            obs_existing._refresh_geometry(ref_geometry=ref_geo)
                    except Exception:
                        pass
                    self._displayinfo_mask_obs = obs_existing
                    if _CORE_AVAILABLE:
                        try:
                            _core._dbg_log(
                                "[MASK OBS] fenetre OBS creee "
                                f"(titre : '{DisplayInfoMaskWindowOBS.OBS_WINDOW_TITLE}')"
                            )
                        except Exception:
                            pass
                else:
                    # Re-application geometrie si la fenetre ecran existe.
                    try:
                        obs_existing._screen = screen
                        if self._displayinfo_mask is not None:
                            ref_geo = self._displayinfo_mask.geometry()
                            obs_existing._refresh_geometry(ref_geometry=ref_geo)
                    except Exception:
                        pass
                if not obs_existing.isVisible():
                    obs_existing.show()
            else:
                if obs_existing is not None:
                    try:
                        obs_existing.close()
                    except Exception:
                        pass
                    try:
                        obs_existing.deleteLater()
                    except Exception:
                        pass
                    self._displayinfo_mask_obs = None
                    if _CORE_AVAILABLE:
                        try:
                            _core._dbg_log("[MASK OBS] fenetre OBS detruite")
                        except Exception:
                            pass
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(
                        f"[MASK] _update_displayinfo_mask KO : {e}"
                    )
                except Exception:
                    pass

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
            # CircusPhone (D4 etape 4)
            ("lbl_phone_open_key",    "phone_open_key"),
            ("lbl_phone_accept_key",  "phone_accept_key"),
            ("lbl_phone_decline_key", "phone_decline_key"),
            ("lbl_phone_mute_key",    "phone_mute_key"),
            ("lbl_phone_speaker_key", "phone_speaker_key"),
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

    # CircusPhone (D4 etape 4) : 5 callbacks hotkey, thread pynput -> Qt
    # via _sig_hotkey (queued connection). Le slot _on_hotkey_dispatch
    # appelle ensuite la bonne action dans le thread main Qt.
    def _on_hotkey_phone_open(self):
        try: _core._dbg_log("[HOTKEY] phone_open (thread pynput)")
        except Exception: pass
        self._sig_hotkey.emit("phone_open")

    def _on_hotkey_phone_accept(self):
        self._sig_hotkey.emit("phone_accept")

    def _on_hotkey_phone_decline(self):
        self._sig_hotkey.emit("phone_decline")

    def _on_hotkey_phone_mute(self):
        self._sig_hotkey.emit("phone_mute")

    def _on_hotkey_phone_speaker(self):
        self._sig_hotkey.emit("phone_speaker")

    # Actions executees dans le thread Qt apres dispatch.
    def _do_phone_open(self):
        """Raccourci 'ouvrir telephone' : actif partout. Toggle l'overlay."""
        try:
            self._phone_toggle_overlay()
        except Exception as e:
            self._on_log(f"[PHONE] hotkey open KO : {e}")

    def _do_phone_accept(self):
        """Raccourci 'decrocher' : actif uniquement en appel entrant."""
        try:
            if getattr(self, "_phone_state", "idle") == "ringing_in":
                self._phone_do_accept()
        except Exception as e:
            self._on_log(f"[PHONE] hotkey accept KO : {e}")

    def _do_phone_decline(self):
        """Raccourci 'refuser/raccrocher' : refuse en sonnerie entrante,
        raccroche pendant un appel sortant ou en cours. Pareil que le
        bouton rouge des ecrans correspondants."""
        try:
            st = getattr(self, "_phone_state", "idle")
            if st == "ringing_in":
                self._phone_do_decline()
            elif st in ("ringing_out", "in_call"):
                self._phone_do_hangup()
        except Exception as e:
            self._on_log(f"[PHONE] hotkey decline/hangup KO : {e}")

    def _do_phone_mute(self):
        """Raccourci 'mute micro' : actif uniquement pendant un appel."""
        try:
            if getattr(self, "_phone_state", "idle") != "in_call":
                return
            ov = self._phone_overlay
            if ov is None:
                return
            # Toggle visuel + audio (le set_active emet sig_mute_toggled,
            # qui appelle audio_io.set_capture_muted via _on_phone_overlay_mute).
            new_state = not ov._btn_mute.is_active()
            ov._btn_mute.set_active(new_state, emit=True)
        except Exception as e:
            self._on_log(f"[PHONE] hotkey mute KO : {e}")

    def _do_phone_speaker(self):
        """Raccourci 'haut-parleur' : actif uniquement pendant un appel.
        Pour l'instant, toggle visuel + log (le routage audio reel viendra
        avec D4b)."""
        try:
            if getattr(self, "_phone_state", "idle") != "in_call":
                return
            ov = self._phone_overlay
            if ov is None:
                return
            new_state = not ov._btn_speaker.is_active()
            ov._btn_speaker.set_active(new_state, emit=True)
        except Exception as e:
            self._on_log(f"[PHONE] hotkey speaker KO : {e}")

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
            # CircusPhone (D4 etape 4)
            "phone_open":    self._do_phone_open,
            "phone_accept":  self._do_phone_accept,
            "phone_decline": self._do_phone_decline,
            "phone_mute":    self._do_phone_mute,
            "phone_speaker": self._do_phone_speaker,
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
            "mute_prox":     ("Mute audio proximite",  "mute_prox_key",       "mute_prox_key"),
            "mute_radio":    ("Mute audio radio",      "mute_radio_key",      "mute_radio_key"),
            "mute_all":      ("Mute tout",              "mute_all_key",        "mute_all_key"),
            "prox_short":    ("Proximite 30m / 5m",    "proximity_short_key", "proximity_short_key"),
            "cycle_channel": ("Cycle canal radio",     "cycle_channel_key",   "cycle_channel_key"),
            # CircusPhone (D4 etape 4)
            "phone_open":    ("Ouvrir / Fermer telephone", "phone_open_key",   "phone_open_key"),
            "phone_accept":  ("Decrocher",                 "phone_accept_key", "phone_accept_key"),
            "phone_decline": ("Refuser / Raccrocher",      "phone_decline_key","phone_decline_key"),
            "phone_mute":    ("Mute micro (telephone)",    "phone_mute_key",   "phone_mute_key"),
            "phone_speaker": ("Haut-parleur",              "phone_speaker_key","phone_speaker_key"),
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
            # CircusPhone (D4 etape 4) : si le raccourci modifie concerne
            # le telephone, mettre a jour aussi les labels sous les boutons
            # de l'overlay.
            if cfg_key.startswith("phone_"):
                self._phone_refresh_overlay_shortcuts()

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

        # ----- Sliders volume (v0.2) ----------------------------------
        # 3 sliders 0..200 % (defaut 100 %) qui controlent les sons
        # generes par le client :
        #   - Bip radio   : son PTT (press + release). Cable immediatement
        #                   sur audio_io.set_radio_beep_volume().
        #   - Soundboard  : reserve a la feature 1 (sons fixes embarques).
        #                   Le setter audio_io existe mais aucun son joue
        #                   tant que la feature n'est pas branchee -> le
        #                   slider est fonctionnel mais inaudible pour
        #                   l'instant.
        #   - Sonnerie    : reserve a la feature 4 (telephone). Memes
        #                   conditions que Soundboard.
        # Les 3 sliders persistent dans _cfg sous radio_beep_volume,
        # soundboard_volume, phone_ring_volume (sauve au closeEvent comme
        # mic_gain et gate_threshold_x2).
        #
        # Separateur visuel : un QFrame fin pour bien marquer la rupture
        # avec les controles micro au-dessus.
        sep_vol = QFrame()
        sep_vol.setFrameShape(QFrame.HLine)
        sep_vol.setFrameShadow(QFrame.Sunken)
        sep_vol.setStyleSheet("color: #444;")
        v.addWidget(sep_vol)

        def _add_volume_slider(label_text: str, cfg_key: str,
                               default: int, on_change_attr: str,
                               val_label_attr: str, slider_attr: str):
            """Helper : construit une ligne <label> <slider 0..200> <valeur>
            et la branche sur self.<on_change_attr>. Le slider est expose
            sous self.<slider_attr> et le label valeur sous
            self.<val_label_attr>. La valeur initiale vient de
            self._cfg[cfg_key] (clampee a 0..200) ou de `default`."""
            h = QHBoxLayout()
            h.setSpacing(8)
            lbl = QLabel(label_text)
            # Largeur FIXE (pas minimum) pour que les 3 sliders volume
            # demarrent tous a la meme abscisse. _audio_lbl_w=50 est un
            # minimum qui laissait "Soundboard :" / "Sonnerie tel. :"
            # s'etendre a leur largeur naturelle alors que "Bip radio :"
            # restait plus court -> sliders desalignes. 95px couvre le
            # label le plus long.
            lbl.setFixedWidth(95)
            h.addWidget(lbl)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 200)
            # Lecture config avec clamp 0..200 et fallback default.
            try:
                v_init = int(self._cfg.get(cfg_key, default))
            except (TypeError, ValueError):
                v_init = default
            v_init = max(0, min(200, v_init))
            sl.setValue(v_init)
            sl.setTickPosition(QSlider.TicksBelow)
            sl.setTickInterval(50)
            sl.valueChanged.connect(getattr(self, on_change_attr))
            h.addWidget(sl, stretch=1)
            lbl_val = QLabel(f"{v_init}%")
            lbl_val.setMinimumWidth(45)
            h.addWidget(lbl_val)
            v.addLayout(h)
            setattr(self, slider_attr, sl)
            setattr(self, val_label_attr, lbl_val)

        _add_volume_slider(
            "Bip radio :",
            "radio_beep_volume",
            100,
            "_on_radio_beep_volume_changed",
            "lbl_radio_beep_vol",
            "sl_radio_beep_vol",
        )
        _add_volume_slider(
            "Soundboard :",
            "soundboard_volume",
            100,
            "_on_soundboard_volume_changed",
            "lbl_soundboard_vol",
            "sl_soundboard_vol",
        )
        _add_volume_slider(
            "Sonnerie tel. :",
            "phone_ring_volume",
            100,
            "_on_phone_ring_volume_changed",
            "lbl_phone_ring_vol",
            "sl_phone_ring_vol",
        )

        # ──────────────────────────────────────────────────────────────
        # Diagnostic crackling : log audio RX detaille (ajout 02/06/2026)
        # ──────────────────────────────────────────────────────────────
        # Active un log CSV separe (circusvoip_debug/audio_rx/) qui trace
        # chaque trame audio recue + chaque callback sounddevice + des
        # stats agregees 30s. Volume eleve (~80-160 MB/h) donc desactive
        # par defaut. A activer ponctuellement pour diagnostiquer un
        # probleme de crackling/pop.
        sep_audio_diag = QFrame()
        sep_audio_diag.setFrameShape(QFrame.HLine)
        sep_audio_diag.setFrameShadow(QFrame.Sunken)
        sep_audio_diag.setStyleSheet("color: #444;")
        v.addWidget(sep_audio_diag)

        self.cb_audio_rx_log = QCheckBox(
            "Activer le log audio detaille (diagnostic crackling)"
        )
        audio_rx_log_enabled = bool(
            self._cfg.get("audio_rx_log_enabled", False)
        )
        self.cb_audio_rx_log.setChecked(audio_rx_log_enabled)
        self.cb_audio_rx_log.setToolTip(
            "Enregistre dans un fichier CSV separe "
            "(circusvoip_debug/audio_rx/) chaque trame audio recue, "
            "chaque callback sounddevice, et des stats agregees toutes "
            "les 30s. Permet de diagnostiquer un probleme de crackling "
            "ou de pop audio.\n\n"
            "ATTENTION : volume eleve (~80-160 MB/h). Ne laisser actif "
            "que le temps du diagnostic, puis decocher.\n\n"
            "Activation a chaud : pas besoin de redemarrer le client."
        )
        self.cb_audio_rx_log.toggled.connect(self._on_audio_rx_log_toggled)
        v.addWidget(self.cb_audio_rx_log)

        self.lbl_audio_rx_log_info = QLabel("")
        self.lbl_audio_rx_log_info.setStyleSheet(
            "color: #888; font-size: 9pt;"
        )
        self.lbl_audio_rx_log_info.setWordWrap(True)
        self._refresh_audio_rx_log_info()
        v.addWidget(self.lbl_audio_rx_log_info)

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
                # Volumes v0.2 : appliquer les 3 sliders (bip radio,
                # soundboard, sonnerie telephone). Slider 0..200 % ->
                # ratio 0.0..2.0. Si les sliders ne sont pas encore
                # construits (cas theorique : audio cree avant l'UI),
                # on saute silencieusement.
                if hasattr(self, "sl_radio_beep_vol"):
                    state.audio_io.set_radio_beep_volume(
                        self.sl_radio_beep_vol.value() / 100.0
                    )
                if hasattr(self, "sl_soundboard_vol"):
                    state.audio_io.set_soundboard_volume(
                        self.sl_soundboard_vol.value() / 100.0
                    )
                if hasattr(self, "sl_phone_ring_vol"):
                    state.audio_io.set_phone_ring_volume(
                        self.sl_phone_ring_vol.value() / 100.0
                    )
                # Reactivation du log audio RX detaille si l'utilisateur
                # l'avait coche dans une session precedente (audit
                # 02/06/2026). La case est deja dans son etat coche grace
                # a _build_audio_panel + audio_rx_log_enabled lu de _cfg ;
                # on declenche maintenant le handler pour reellement
                # ouvrir le fichier CSV.
                if hasattr(self, "cb_audio_rx_log") and self.cb_audio_rx_log.isChecked():
                    try:
                        self._on_audio_rx_log_toggled(True)
                    except Exception as e:
                        self._on_log(
                            f"[AUDIO RX LOG] Echec reactivation au boot : {e}"
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

    # ------------------------------------------------------------
    # Handlers sliders volume (v0.2)
    # ------------------------------------------------------------
    # Chaque slider est en 0..200 (% du volume nominal). On convertit
    # en ratio float (slider/100.0, donc 0.0..2.0) avant de l'envoyer a
    # audio_io. Pas de save immediat : la valeur est ecrite dans _cfg et
    # persistera au closeEvent (cf. _on_gain_changed pour le rationale).

    @Slot(int)
    def _on_radio_beep_volume_changed(self, value: int):
        """Slider 'Bip radio' : volume du bip PTT local."""
        if hasattr(self, "lbl_radio_beep_vol"):
            self.lbl_radio_beep_vol.setText(f"{value}%")
        if state.audio_io is not None:
            try:
                state.audio_io.set_radio_beep_volume(value / 100.0)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[AUDIO] set_radio_beep_volume KO : {e}"
                        )
                    except Exception:
                        pass
        self._cfg["radio_beep_volume"] = value

    @Slot(int)
    def _on_soundboard_volume_changed(self, value: int):
        """Slider 'Soundboard' : volume des sons soundboard (feature 1
        v0.2, pas encore branchee). Le setter audio_io existe mais aucun
        son audible tant que la feature n'est pas implementee."""
        if hasattr(self, "lbl_soundboard_vol"):
            self.lbl_soundboard_vol.setText(f"{value}%")
        if state.audio_io is not None:
            try:
                state.audio_io.set_soundboard_volume(value / 100.0)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[AUDIO] set_soundboard_volume KO : {e}"
                        )
                    except Exception:
                        pass
        self._cfg["soundboard_volume"] = value

    @Slot(int)
    def _on_phone_ring_volume_changed(self, value: int):
        """Slider 'Sonnerie tel.' : volume de la sonnerie d'appel entrant
        et du bip d'appel sortant (CircusPhone, branche en D2). Couvrira
        aussi la notif MP en D4. Applique via audio_io.set_phone_ring_volume."""
        if hasattr(self, "lbl_phone_ring_vol"):
            self.lbl_phone_ring_vol.setText(f"{value}%")
        if state.audio_io is not None:
            try:
                state.audio_io.set_phone_ring_volume(value / 100.0)
            except Exception as e:
                if _CORE_AVAILABLE:
                    try:
                        _core._dbg_log(
                            f"[AUDIO] set_phone_ring_volume KO : {e}"
                        )
                    except Exception:
                        pass
        self._cfg["phone_ring_volume"] = value

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
        # v0.2 alpha 029/031 : signal soundboard depuis le thread reseau.
        # QueuedConnection (par defaut cross-thread) -> _play_soundboard_local
        # tourne dans le thread Qt main, peut appeler audio_io.play_soundboard
        # et accede au cache de sons sans souci de thread-safety.
        self._worker.sig_soundboard_play.connect(self._play_soundboard_local)
        # v0.2 alpha 035 : signal de changement de permission profil
        # (push serveur). Le slot adapte l'UI (cache/montre soundboard).
        self._worker.sig_my_perm_changed.connect(self._on_my_perm_changed)
        # CircusPhone (Feature 4, D1) : signaux du cycle de vie d'appel.
        # Cross-thread (NetWorker -> thread Qt main) en QueuedConnection
        # par defaut : les slots _on_phone_* touchent l'UI sans souci.
        self._worker.sig_phone_ringing.connect(self._on_phone_ringing)
        self._worker.sig_phone_incoming.connect(self._on_phone_incoming)
        self._worker.sig_phone_accepted.connect(self._on_phone_accepted)
        self._worker.sig_phone_declined.connect(self._on_phone_declined)
        self._worker.sig_phone_busy.connect(self._on_phone_busy)
        self._worker.sig_phone_missed.connect(self._on_phone_missed)
        self._worker.sig_phone_ended.connect(self._on_phone_ended)
        self._worker.sig_phone_server_msg.connect(self._on_phone_server_msg)
        # CircusPhone (D4 etape 3) : reception d'un MP texte.
        self._worker.sig_phone_message_received.connect(
            self._on_phone_message_received
        )
        self._worker.sig_phone_image_received.connect(
            self._on_phone_image_received
        )
        # [D5] Reponse a une demande de photo de profil.
        self._worker.sig_profile_photo_response.connect(
            self._on_profile_photo_response
        )

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
            # [D5] Si la photo locale n'est pas synchronisee avec le
            # serveur (changement hors-ligne ou jamais uploadee), on la
            # repousse maintenant. Best-effort, no-op si pas de photo.
            # Delai 500ms pour laisser le welcome se traiter d'abord
            # (state.my_name doit etre prete, et le serveur a fini
            # d'envoyer la liste joueurs).
            try:
                QTimer.singleShot(
                    500, lambda: self._profile_photos.try_upload_local()
                )
            except Exception:
                pass
            # Connexion etablie : afficher tout de suite "Aucun autre joueur
            # en ligne" si on est seul (le welcome avec la liste viendra
            # rapidement, _on_players_reset rafraichira aussi).
            self._refresh_no_other_players_label()
        else:
            txt = "Deconnecte"
            if message:
                txt = f"Deconnecte ({message})"
            self.lbl_status.setText(txt)
            self._set_status_style(False)
            self.btn_toggle.setText("CONNECTER")
            # CircusPhone (D1) : connexion perdue -> tout appel en cours
            # est termine cote serveur, on remet l'etat phone au repos.
            try:
                self._phone_on_disconnect()
            except Exception:
                pass
            # Vider les cards joueurs
            for name in list(self._player_cards.keys()):
                card = self._player_cards.pop(name)
                self._players_layout.removeWidget(card)
                card.deleteLater()
            # Cards videes + plus connecte -> cacher le label "aucun".
            self._refresh_no_other_players_label()
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

    def _refresh_no_other_players_label(self):
        """Affiche / masque le label 'Aucun autre joueur en ligne'.
        Visible UNIQUEMENT si on est connecte au serveur ET qu'il n'y a
        aucune card joueur. Hors connexion la zone reste vide (pas de
        message trompeur). Ajout 25/05/2026 Kainan."""
        try:
            if not hasattr(self, "lbl_no_other_players"):
                return
            is_connected = bool(_CORE_AVAILABLE and state.connected)
            no_others = (len(self._player_cards) == 0)
            self.lbl_no_other_players.setVisible(is_connected and no_others)
        except Exception:
            pass

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
        # CircusPhone (D4) : un joueur rejoint -> enrichir l'annuaire
        # (ajout ou maj last_seen) + refresh live de l'overlay. _on_players_reset
        # appelle aussi _on_player_joined en boucle puis enrichit en masse ;
        # ce double enrichissement est sans effet de bord (idempotent).
        self._phone_annuaire_enrich([name])
        # Au moins 1 joueur dans la liste -> cacher le label "aucun".
        self._refresh_no_other_players_label()

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
        # v0.2 (optim perf) : liberer aussi les structures cote audio_io
        # (queue + volume + multiplier + flags is_radio/is_phone/force_radio,
        # ainsi que l'etat du filtre radio qui contient un buffer reverb
        # 1440 floats par joueur). Avant ce fix, remove_user existait mais
        # n'etait jamais appelee -> fuite memoire permanente quand des
        # joueurs rejoignent puis quittent sur une session longue.
        try:
            if state.audio_io is not None:
                state.audio_io.remove_user(name)
        except Exception as e:
            if _CORE_AVAILABLE:
                try:
                    _core._dbg_log(f"[AUDIO] remove_user({name}) KO : {e}")
                except Exception:
                    pass
        # CircusPhone (D4) : un joueur quitte -> son statut passe a
        # "deconnecte" dans l'overlay. On ne touche pas a l'annuaire
        # (le contact reste enregistre), juste un refresh d'affichage.
        self._phone_refresh_overlay_contacts()
        # Si c'etait le dernier joueur autre -> afficher le label "aucun".
        self._refresh_no_other_players_label()

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
        # CircusPhone (D4) : enrichir l'annuaire avec tous les joueurs vus
        # connectes dans ce welcome, puis rafraichir l'overlay.
        self._phone_annuaire_enrich(names)
        # Welcome avec liste vide ou non-vide -> refresh du label "aucun".
        # (Chaque _on_player_joined le fait deja en boucle, mais on appelle
        # une derniere fois au cas ou la boucle a ete vide -> label visible.)
        self._refresh_no_other_players_label()

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

    # ------------------------------------------------------------
    # Soundboard (v0.2 alpha 029)
    # ------------------------------------------------------------
    # Architecture :
    #   1. Au boot, _load_soundboard_sounds() lit les .wav locaux et les
    #      cache en numpy float32 mono 48kHz dans self._soundboard_cache.
    #   2. Clic sur un bouton du soundboard : envoie un message WS
    #      soundboard_play au serveur.
    #   3. Le serveur broadcast au canal vocal courant.
    #   4. Tous les clients du canal (y compris l'emetteur) recoivent
    #      le message et appellent _play_soundboard_local(sound_id).
    #   5. _play_soundboard_local fait jouer le sample cache via
    #      AudioIO.play_soundboard(samples).
    #
    # Le slider "Son A" agit sur le facteur de volume cote
    # AudioIO._soundboard_volume_factor (applique au mix).

    # Mapping sound_id -> nom de fichier wav (sans extension du dossier).
    # Pour ajouter un son : creer le wav a cote, ajouter l'entree ici.
    SOUNDBOARD_FILES = {
        "alarme": "alarm.wav",
    }

    def _load_soundboard_sounds(self):
        """Charge tous les .wav du soundboard en memoire (float32 mono 48kHz).
        Appele au boot par __init__ (apres que tous les modules sont
        importes). Les fichiers absents sont logues mais non bloquants
        (le clic sur ce son sera silencieusement ignore avec un log)."""
        try:
            import wave
            import numpy as np_local
        except Exception as e:
            self._on_log(f"[SOUNDBOARD] Import wave/numpy KO : {e}")
            return
        base_dir = Path(__file__).resolve().parent
        sounds_dir = base_dir / "sounds"
        for sound_id, fname in self.SOUNDBOARD_FILES.items():
            # v0.2.0 dev : les fichiers audio sont regroupes dans sounds/
            # (cf. sonneries telephone dial/ring/notif dans le meme dossier).
            # Avant, les .wav de la soundboard etaient a la racine de app/.
            # Migration : deplacer alarm.wav dans app/sounds/.
            # (Anciennement nomme ENVP_Alarms-0160-event.wav.)
            path = sounds_dir / fname
            if not path.exists():
                self._on_log(
                    f"[SOUNDBOARD] Fichier absent : {path}. Son '{sound_id}' "
                    "indisponible (les autres clients ne pourront pas le "
                    "jouer non plus s'ils n'ont pas le fichier)."
                )
                continue
            try:
                with wave.open(str(path), "rb") as wf:
                    n_ch     = wf.getnchannels()
                    samp_w   = wf.getsampwidth()
                    rate     = wf.getframerate()
                    n_frames = wf.getnframes()
                    raw      = wf.readframes(n_frames)
                # Conversion en np.float32 normalisee a [-1, 1].
                # samp_w = 2 (16 bits, le plus courant pour les wav).
                if samp_w == 2:
                    arr = np_local.frombuffer(raw, dtype=np_local.int16).astype(np_local.float32) / 32768.0
                elif samp_w == 1:
                    # 8 bits unsigned : [-128, 127] apres centrage.
                    arr = (np_local.frombuffer(raw, dtype=np_local.uint8).astype(np_local.float32) - 128.0) / 128.0
                elif samp_w == 4:
                    # 32 bits int.
                    arr = np_local.frombuffer(raw, dtype=np_local.int32).astype(np_local.float32) / 2147483648.0
                else:
                    self._on_log(
                        f"[SOUNDBOARD] Largeur sample non supportee "
                        f"({samp_w} octets) pour '{sound_id}'. Ignore."
                    )
                    continue
                # Stereo -> mono (moyenne des canaux).
                if n_ch == 2:
                    arr = arr.reshape(-1, 2).mean(axis=1)
                elif n_ch != 1:
                    # 5.1, 7.1, etc. : on prend la 1ere voie.
                    arr = arr.reshape(-1, n_ch)[:, 0]
                # Resample si necessaire vers 48 kHz (= SAMPLE_RATE).
                # Resample lineaire simple ; pour de la qualite "vraie"
                # on utiliserait scipy.signal.resample mais c'est lourd.
                # Le lineaire suffit pour des sons courts du soundboard.
                target_rate = 48000
                if rate != target_rate:
                    n_in  = len(arr)
                    n_out = int(round(n_in * target_rate / rate))
                    if n_out > 0:
                        xp = np_local.arange(n_in)
                        x  = np_local.linspace(0, n_in - 1, n_out)
                        arr = np_local.interp(x, xp, arr).astype(np_local.float32)
                self._soundboard_cache[sound_id] = arr
                duration_s = len(arr) / target_rate
                self._on_log(
                    f"[SOUNDBOARD] Charge '{sound_id}' depuis {fname} "
                    f"({duration_s:.2f}s a {target_rate}Hz, "
                    f"{rate}Hz orig, {n_ch}ch)"
                )
            except Exception as e:
                self._on_log(
                    f"[SOUNDBOARD] Lecture KO pour '{sound_id}' ({fname}) : {e}"
                )

    @Slot(str, str)
    def _play_soundboard_local(self, sound_id: str, sender: str = ""):
        """Joue un son du soundboard sur la sortie locale via AudioIO.
        Appele :
          - par le handler du bouton du soundboard (= moi qui declenche).
          - par le dispatch WS soundboard_play (= un autre joueur ou moi
            recu en echo via le serveur, via sig_soundboard_play emit
            depuis NetWorker)."""
        if not isinstance(sound_id, str) or not sound_id:
            return
        samples = self._soundboard_cache.get(sound_id)
        if samples is None:
            self._on_log(
                f"[SOUNDBOARD] Son '{sound_id}' non charge (fichier "
                f"absent ?). Lecture ignoree."
            )
            return
        audio = getattr(state, "audio_io", None)
        if audio is None:
            self._on_log(
                "[SOUNDBOARD] audio_io indisponible (audio pas demarre ?). "
                "Lecture ignoree."
            )
            return
        try:
            audio.play_soundboard(samples)
            who = sender or "(local)"
            self._on_log(f"[SOUNDBOARD] Play '{sound_id}' par {who}")
        except Exception as e:
            self._on_log(f"[SOUNDBOARD] play_soundboard KO : {e}")

    @Slot()
    def _on_soundboard_state_timer(self):
        """Tic du timer 100ms : interroge audio_io pour savoir si un son
        joue actuellement, et met a jour la fenetre soundboard (grise
        les boutons pendant la lecture). Si la fenetre n'existe pas
        encore ou n'est pas visible, on ignore (pas de calcul inutile)."""
        sw = self._soundboard_window
        if sw is None:
            return
        audio = getattr(state, "audio_io", None) if _CORE_AVAILABLE else None
        if audio is None:
            playing = False
        else:
            try:
                playing = audio.is_soundboard_playing()
            except Exception:
                playing = False
        try:
            sw.set_playing_state(playing)
        except Exception:
            pass

    @Slot(str, bool)
    def _on_my_perm_changed(self, perm_key: str, value: bool):
        """Slot appele quand le serveur push une nouvelle permission sur
        mon profil. Met a jour l'UI : actuellement, gere la section
        soundboard (visible ssi soundboard_allowed=True).

        v0.2 alpha 035. Si plus tard d'autres permissions apparaissent
        (phone_allowed, etc.), on ajoutera des elif ici."""
        try:
            if perm_key == "soundboard_allowed":
                # Affiche/cache toute la section SOUNDBOARD du panneau
                # gauche. Si la fenetre flottante etait ouverte au moment
                # de la revocation, on la ferme aussi (sinon les boutons
                # restent visibles independamment de la section).
                if hasattr(self, "gb_soundboard"):
                    self.gb_soundboard.setVisible(bool(value))
                if not value and self._soundboard_window is not None:
                    try:
                        self._soundboard_window.hide()
                    except Exception:
                        pass
        except Exception as e:
            self._on_log(f"[PERM] _on_my_perm_changed KO : {e}")

    @Slot()
    def _on_soundboard_button_clicked(self):
        """Toggle de la fenetre flottante du soundboard. Au 1er clic,
        cree la fenetre. Clics suivants : show/hide selon etat courant.

        v0.2 alpha 037 : avec Qt.Popup, un clic en dehors de la popup
        la ferme automatiquement. Si l'utilisateur clique sur le bouton
        Soundboard alors que la popup est visible, le clic ferme la
        popup -> puis le bouton recoit le clic -> on rouvre. Pas le
        comportement voulu. On debounce : si la popup s'est fermee dans
        les 200ms qui precedent, on ignore ce clic (= la popup s'est
        deja fermee toute seule, l'utilisateur a juste cliquer pour
        fermer, pas pour rouvrir)."""
        now = time.monotonic()
        last_hide = getattr(self, "_soundboard_window_last_hide_ts", 0.0)
        if now - last_hide < 0.2:
            # Popup vient de se fermer toute seule (Qt.Popup), pas la
            # peine de rouvrir.
            return
        if self._soundboard_window is None:
            self._soundboard_window = SoundboardWindow(parent=self)
            # Hook sur le hide pour memoriser le ts de fermeture (sert
            # au debounce du bouton). On wrap hide pour ajouter notre
            # logique. closeEvent pourrait aussi marcher mais hide est
            # plus direct (Qt.Popup appelle hide quand on clique
            # ailleurs).
            orig_hide_event = self._soundboard_window.hideEvent
            def _patched_hide_event(event, _orig=orig_hide_event):
                try:
                    self._soundboard_window_last_hide_ts = time.monotonic()
                except Exception:
                    pass
                return _orig(event)
            self._soundboard_window.hideEvent = _patched_hide_event
        if self._soundboard_window.isVisible():
            self._soundboard_window.hide()
        else:
            # Positionner la fenetre juste sous le bouton "Soundboard"
            # pour effet de panneau deroulant.
            try:
                btn = self.btn_soundboard
                pt_btn = btn.mapToGlobal(QPoint(0, btn.height()))
                self._soundboard_window.move(pt_btn)
            except Exception:
                pass
            self._soundboard_window.show()
            self._soundboard_window.raise_()
            self._soundboard_window.activateWindow()

    def _on_soundboard_sound_clicked(self, sound_id: str):
        """Handler des boutons dans SoundboardWindow. Envoie le message
        WS soundboard_play au serveur. Le serveur broadcast au canal,
        et on recevra l'echo qui declenchera la lecture locale (cf.
        dispatch soundboard_play dans _handle_ws_message)."""
        if not _CORE_AVAILABLE:
            self._on_log("[SOUNDBOARD] Module core indispo, envoi ignore.")
            return
        try:
            ok = _core._ws_send_safe({
                "type":     "soundboard_play",
                "sound_id": sound_id,
            })
            if ok:
                self._on_log(f"[SOUNDBOARD] Envoye '{sound_id}' au serveur")
            else:
                self._on_log(
                    "[SOUNDBOARD] WS non connectee, envoi ignore. "
                    "Connecte-toi a un serveur d'abord."
                )
        except Exception as e:
            self._on_log(f"[SOUNDBOARD] _ws_send_safe KO : {e}")

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
            # v0.2 : invalider aussi le timestamp pour que le masque
            # DisplayInfo se cache immediatement (sinon il resterait
            # visible pendant DISPLAYINFO_MASK_STALE_S secondes apres
            # la fermeture de SC, ce qui est confus pour l'utilisateur).
            state.my_pos_ts = 0.0
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
            # CircusPhone (D4 etape 4) : 5 raccourcis telephone.
            state.phone_open_key     = _canon(core_cfg.get("phone_open_key"))
            state.phone_accept_key   = _canon(core_cfg.get("phone_accept_key"))
            state.phone_decline_key  = _canon(core_cfg.get("phone_decline_key"))
            state.phone_mute_key     = _canon(core_cfg.get("phone_mute_key"))
            state.phone_speaker_key  = _canon(core_cfg.get("phone_speaker_key"))
            state.rp_mode              = bool(core_cfg.get("rp_mode", False))
            self._on_log(
                f"[CONFIG] Chargee : radio_key={state.radio_key!r} "
                f"profile_key={state.profile_radio_key!r} "
                f"rp_mode={state.rp_mode}"
            )
        except Exception as e:
            self._on_log(f"[CONFIG] Erreur chargement : {e}")

        # Brancher les callbacks de toggle (mute mic/prox/radio/all,
        # cycle canal, prox short, profile radio PTT, CircusPhone D4).
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
                # CircusPhone (D4 etape 4) : 5 raccourcis telephone.
                on_phone_open    = self._on_hotkey_phone_open,
                on_phone_accept  = self._on_hotkey_phone_accept,
                on_phone_decline = self._on_hotkey_phone_decline,
                on_phone_mute    = self._on_hotkey_phone_mute,
                on_phone_speaker = self._on_hotkey_phone_speaker,
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

        # FLAG DEBUG (v0.2 alpha 016) : si False, on NE DEMARRE PAS l'OCR.
        # Permet de tester si le masque clignote a cause d'une interaction
        # OCR <-> masque. Effets de bord : state.my_pos reste a None, les
        # overlays proximite ne marchent pas, mais on s'en fiche pour le
        # debug du masque.
        if DEBUG_ENABLE_OCR:
            _spawn_ocr_thread()

            # Thread watchdog OCR : detecte les freezes silencieux de la
            # boucle OCR (segfault torch/CUDA, deadlock GPU, etc.). Si l'OCR
            # ne tick plus depuis 15s, log un warning. Si plus de 30s, demande
            # au client de respawner le thread OCR via le callback.
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
        else:
            # Mode debug : OCR coupe entierement. Pas de watchdog non plus.
            try:
                if _CORE_AVAILABLE:
                    _core._dbg_log(
                        "[DEBUG] OCR desactive (DEBUG_ENABLE_OCR=False). "
                        "Mode debug masque uniquement."
                    )
            except Exception:
                pass

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
                # [v0.3] Exposer le chemin courant aux abonnes du feed
                # (current_path()), utile au rattrapage one-shot du Wallet.
                try:
                    self._gamelog_feed.set_path(path)
                except Exception:
                    pass
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

            # [v0.3] Fan-out vers les abonnes du feed (apps CircusPhone).
            # Emis depuis ce thread daemon ; les slots Qt sont appeles en
            # queued (thread-safe). Best-effort : ne jamais casser le tail.
            try:
                self._gamelog_feed.emit_line(line)
            except Exception:
                pass

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
        # 0a. Confirmation utilisateur. Si on est dans une fermeture
        # automatique (relance pour MAJ, crash recovery, ...), on bypass :
        # un flag self._skip_close_confirm est mis par le caller dans ces
        # cas-la. Sinon, popup "Quitter CircusVOIP ?" et on annule la
        # fermeture si l'utilisateur clique sur Annuler.
        # NB : on utilise addButton pour forcer les libellés français
        # ("Quitter" / "Annuler") au lieu des "Yes"/"No" par defaut de Qt
        # (qui ne sont traduits que si on charge un QTranslator global).
        if not getattr(self, "_skip_close_confirm", False):
            try:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Question)
                box.setWindowTitle("Quitter CircusVOIP ?")
                box.setText("Voulez-vous vraiment fermer CircusVOIP ?")
                btn_quit = box.addButton("Quitter",
                                         QMessageBox.AcceptRole)
                btn_cancel = box.addButton("Annuler",
                                           QMessageBox.RejectRole)
                box.setDefaultButton(btn_cancel)
                box.exec()
                if box.clickedButton() is not btn_quit:
                    event.ignore()
                    return
            except Exception as e:
                # En cas de souci avec la popup (ex. Qt non dispo dans
                # certains contextes de shutdown), on continue la fermeture
                # plutot que de bloquer l'app.
                print(f"[CLOSE] Popup confirmation KO : {e}",
                      file=sys.stderr)

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
                            "phone_open_key",
                            "phone_accept_key",
                            "phone_decline_key",
                            "phone_mute_key",
                            "phone_speaker_key",
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
            # v0.2 : sauvegarder les 3 nouveaux sliders de volume.
            # Redondant avec les handlers (qui ecrivent deja dans _cfg a
            # chaque move) mais coherent avec mic_gain/gate ci-dessus.
            if hasattr(self, "sl_radio_beep_vol"):
                self._cfg["radio_beep_volume"] = self.sl_radio_beep_vol.value()
            if hasattr(self, "sl_soundboard_vol"):
                self._cfg["soundboard_volume"] = self.sl_soundboard_vol.value()
            if hasattr(self, "sl_phone_ring_vol"):
                self._cfg["phone_ring_volume"] = self.sl_phone_ring_vol.value()
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
        # v0.2 : fermer le masque DisplayInfo et stopper son timer
        try:
            if hasattr(self, "_displayinfo_mask_timer"):
                self._displayinfo_mask_timer.stop()
            mask = getattr(self, "_displayinfo_mask", None)
            if mask is not None:
                try:
                    # close() declenche closeEvent qui detach du service.
                    mask.close()
                    mask.deleteLater()
                except Exception:
                    pass
                self._displayinfo_mask = None
            # v0.2 alpha 058 : fermer aussi la fenetre source OBS.
            mask_obs = getattr(self, "_displayinfo_mask_obs", None)
            if mask_obs is not None:
                try:
                    # close() declenche closeEvent qui detach du service.
                    mask_obs.close()
                    mask_obs.deleteLater()
                except Exception:
                    pass
                self._displayinfo_mask_obs = None
            # v0.2 alpha 060 : shutdown du service partage (arrete le
            # worker s'il tourne encore, detache tout consommateur restant).
            svc = getattr(self, "_displayinfo_mask_service", None)
            if svc is not None:
                try:
                    svc.shutdown()
                except Exception:
                    pass
        except Exception:
            pass
        # v0.2 alpha 055 : stopper le listener clavier de la machine d'etat
        # masque (F1/F2/F11/Echap).
        try:
            tracker = getattr(self, "_mask_key_tracker", None)
            if tracker is not None:
                tracker.stop()
                self._mask_key_tracker = None
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
