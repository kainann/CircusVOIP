# -*- coding: utf-8 -*-
"""
circusvoip_phone_solvsterra
===========================

Application « Sol VS Terra » du CircusPhone (v0.3), au contrat `PhoneApp`.
Sol VS Terra = bataille navale (ex-prototype « bataille navale ») revisitée
en affrontement de flottes Star Citizen, jouable au clavier contre une IA.

Déroulé :
  1. PLACEMENT — pose de tes navires sur la grille du bas (fantôme vert =
     valide, rouge = invalide).
  2. COMBAT — deux grilles 10x10 : flotte ENNEMIE en haut (cachée, ta
     cible), TA flotte en bas (tu y vois les tirs de l'IA).

Jeu : capture clavier brute (CAPTURES_KEYBOARD=True). Tour par tour avec un
timer IA en attente — on_hide arrête ce timer ; on_show le RELANCE s'il
était en attente (sinon, cacher pendant le tour de l'IA bloquerait la
partie). Pas de réseau : l'adversaire est une IA locale.

Touches — PLACEMENT :
    Flèches directionnelles   déplacer le navire courant
    R                        pivoter (horizontal / vertical)
    Entrée / clic   poser le navire
    Retour arrière           annuler le dernier navire
    F                        remplir le reste automatiquement
Touches — COMBAT :
    Flèches directionnelles   déplacer le viseur (grille du haut)
    Entrée / clic   tirer
    C                        changer de skin

Dépendance : circusvoip_phone_apps (PhoneApp/PhoneServices). Le bloc
__main__ est un HARNAIS DE TEST VISUEL supprimable.
"""

from __future__ import annotations

import os
import random
import sys
import time

from PySide6.QtCore import Qt, QTimer, QRectF, QEvent, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont, QFontMetrics, QKeyEvent, QGuiApplication,
    QPainterPath, QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from circusvoip_phone_apps import PhoneApp, PhoneServices
try:
    import circusvoip_phone_mp as mp
except Exception:               # module absent -> multijoueur desactive
    mp = None


# ======================================================================
# Palette (thème CircusVoIP)
# ======================================================================
_BG         = "#0d1117"
_WATER      = "#11233a"
_WATER_2    = "#0d1b2e"
_GRIDLINE   = "#1f3b58"
_SHIP       = "#8b949e"
_SHIP_EDGE  = "#c9d1d9"
_HIT        = "#f85149"
_SUNK       = "#7a1f1f"
_MISS       = "#6e7681"
_CURSOR     = "#58a6ff"
_TEXT       = "#c9d1d9"
_MUTED      = "#6e7681"
_ACCENT     = "#3fb950"
_OVERLAY_BG = "#0d1117d0"

_PHONE_BODY_COLOR   = "#1a1a1a"
_PHONE_BTN_COLOR    = "#0a0a0a"
_PHONE_BANNER_GREY  = "#888888"
_PHONE_BANNER_WHITE = "#ffffff"

_N = 10
_FLEET = [("Porte-vaisseaux", 5), ("Cuirassé", 4),
          ("Croiseur", 3), ("Sous-marin", 3), ("Destroyer", 2)]
_AI_DELAY_MS = 550

_DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


# ======================================================================
# Skins
# ======================================================================
_DEFAULT_COLORS = {
    "bg": _BG, "water": _WATER, "water2": _WATER_2, "grid": _GRIDLINE,
    "ship": _SHIP, "ship_edge": _SHIP_EDGE, "hit": _HIT, "sunk": _SUNK,
    "miss": _MISS, "cursor": _CURSOR, "text": _TEXT, "accent": _ACCENT,
    "muted": _MUTED, "overlay": _OVERLAY_BG, "label_foe": _HIT,
}
_SONAR = {  # phosphore vert
    "bg": "#001207", "water": "#02180c", "water2": "#041f0f", "grid": "#13662e",
    "ship": "#1d7a35", "ship_edge": "#39ff14", "hit": "#ff4d4d", "sunk": "#5a1414",
    "miss": "#2f6b3f", "cursor": "#39ff14", "text": "#9affb0", "accent": "#39ff14",
    "muted": "#3f7a52", "overlay": "#001207d8", "label_foe": "#ff6b6b",
}
_RADAR = {  # holographique bleu
    "bg": "#03101f", "water": "#08233f", "water2": "#0a2a4a", "grid": "#1f6fd0",
    "ship": "#2f6fed", "ship_edge": "#9ecbff", "hit": "#ff6b5b", "sunk": "#5a1414",
    "miss": "#3a5a80", "cursor": "#ffd479", "text": "#cfe6ff", "accent": "#58a6ff",
    "muted": "#5a7aa0", "overlay": "#03101fd8", "label_foe": "#ff8a7a",
}

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSET_BASES = [_SCRIPT_DIR, os.getcwd()]


def _load_pix(folder, names):
    """Cherche le 1er fichier existant parmi 'names' à plusieurs endroits.
    Retourne (QPixmap|None, liste des chemins essayés)."""
    tried = []
    for base in _ASSET_BASES:
        for nm in names:
            for rel in (os.path.join("skins", folder, nm),
                        os.path.join(folder, nm), nm):
                path = os.path.join(base, rel)
                tried.append(path)
                if os.path.exists(path):
                    pm = QPixmap(path)
                    if not pm.isNull():
                        return pm, path
    return None, tried


class Skin:
    """Palette de couleurs + sprites optionnels (fond + navire)."""

    def __init__(self, name, colors=None, folder=None):
        self.name = name
        self.folder = folder
        self.c = dict(_DEFAULT_COLORS)
        if colors:
            self.c.update(colors)
        self.background = None
        self.ship = None
        self.ship_by_size = {}
        self.missing = []
        self._tried = {}
        if folder:
            self.background, info = _load_pix(folder, ("background.jpg",
                                                       "background.png"))
            if self.background is None:
                self.missing.append("background.jpg"); self._tried["background"] = info
            self.ship, info = _load_pix(folder, ("ship.png", "ship.jpg"))
            if self.ship is None:
                self.missing.append("ship.png"); self._tried["ship"] = info
            # Sprites optionnels par taille de navire : ship_5.png ... ship_2.png
            for size in (5, 4, 3, 2):
                pm, info = _load_pix(folder, (f"ship_{size}.png", f"ship_{size}.jpg"))
                if pm is not None:
                    self.ship_by_size[size] = pm
                else:
                    self._tried[f"ship_{size}"] = info

    def ship_for(self, size):
        """Sprite à utiliser pour un navire de cette taille (spécifique sinon
        générique sinon None -> rendu couleur)."""
        return self.ship_by_size.get(size, self.ship)

    @property
    def has_images(self):
        return (self.background is not None or self.ship is not None
                or bool(self.ship_by_size))

    def diagnostic(self):
        lines = [f"[skin {self.name}] dossier recherché : skins/{self.folder}/"]
        # Sprites par taille (ce que tu as fourni)
        for size in (5, 4, 3, 2):
            if size in self.ship_by_size:
                lines.append(f"   OK       ship_{size}.png")
            else:
                lines.append(f"   MANQUANT ship_{size}.png")
                for pth in self._tried.get(f"ship_{size}", [])[:2]:
                    lines.append(f"            essayé : {pth}")
        # Fond (optionnel)
        if self.background is not None:
            lines.append("   OK       background.jpg")
        else:
            lines.append("   (option) background.jpg  absent")
        return "\n".join(lines)


def _random_fleet():
    """Flotte aléatoire sans chevauchement (contacts permis)."""
    ships, occupied = [], set()
    for name, size in _FLEET:
        while True:
            horiz = random.random() < 0.5
            if horiz:
                x = random.randint(0, _N - size)
                y = random.randint(0, _N - 1)
                cells = [(x + i, y) for i in range(size)]
            else:
                x = random.randint(0, _N - 1)
                y = random.randint(0, _N - size)
                cells = [(x, y + i) for i in range(size)]
            if any(c in occupied for c in cells):
                continue
            occupied.update(cells)
            ships.append({"name": name, "size": size,
                          "cells": cells, "hits": set()})
            break
    return ships


def _ship_at(ships, cell):
    for s in ships:
        if cell in s["cells"]:
            return s
    return None


def _is_sunk(ship):
    return set(ship["cells"]) <= ship["hits"]


def _fleet_dead(ships):
    return all(_is_sunk(s) for s in ships)


class SolVsTerraApp(PhoneApp):
    """Sol VS Terra — bataille navale (ex-« bataille navale ») contre une IA,
    avec phase de placement manuel. Premiere app JEU tour-par-tour : capture
    clavier brute, et gestion fine de la pause (le timer IA en attente est
    relance au retour pour ne pas bloquer la partie)."""

    APP_ID = "solvsterra"
    APP_NAME = "Sol VS Terra"
    APP_ICON = "\u2694"              # ⚔ (placeholder ; sprite flotte plus tard)
    CAPTURES_KEYBOARD = True         # jeu : l'overlay route les touches brutes

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.resize(int(screen_w), int(screen_h))
        self.setFocusPolicy(Qt.StrongFocus)
        # Attribut lu par paintEvent pour clipper aux coins arrondis.
        self._screen_radius = int(screen_radius)
        self._N = _N

        self._ai_timer = QTimer(self)
        self._ai_timer.setSingleShot(True)
        self._ai_timer.timeout.connect(self._ai_fire)
        # Pause : retient si le timer IA etait en attente quand on a cache
        # l'app, pour le relancer au retour (sinon partie bloquee).
        self._paused = False
        self._ai_timer_was_active = False

        self._last_layout = None

        # Skins : palettes (toujours dispo) + skin image "naval" si présent.
        self._skins = [Skin("classic"), Skin("sonar", colors=_SONAR),
                       Skin("radar", colors=_RADAR)]
        naval = Skin("solvsterra", colors=_RADAR, folder="solvsterra")
        print("=" * 60)
        print(naval.diagnostic())
        if naval.has_images:
            self._skins.append(naval)
            print("[OK] Skin 'naval' chargé -> touche V pour l'afficher.")
        else:
            print("[!] Skin 'naval' introuvable : la touche V ne propose")
            print("    que classic / sonar / radar.")
            print("    Range les sprites ainsi, à côté du script :")
            print("       skins/naval/ship_5.png  (Bengal)")
            print("       skins/naval/ship_4.png  (Kraken)")
            print("       skins/naval/ship_3.png  (Idris)")
            print("       skins/naval/ship_2.png  (Perseus)")
        print("=" * 60)
        # Démarre directement sur le skin naval s'il est chargé.
        self._skin_idx = next((i for i, s in enumerate(self._skins)
                               if s.name == "solvsterra"), 0)

        self._reset()

        # Menu d'accueil (etat "menu") : boutons verticaux par-dessus le decor
        # du jeu. Etat initial = menu (et non placement). _reset() ci-dessus a
        # prepare une partie ; on l'ecrase juste apres par l'etat menu.
        self._menu_items = ["Jouer", "Règles", "Quitter"]
        self._menu_index = 0
        self._menu_rects = []
        # Ecran de choix du mode (apres "Jouer") : Solo (local vs IA) /
        # Online (multijoueur, via la future couche reseau partagee).
        self._mode_items = ["Solo", "Multijoueur"]
        self._mode_index = 0
        self._mode_rects = []
        # Ecran multijoueur (apres avoir choisi "Online") : creer / rejoindre
        # une partie. Actions non cablees pour l'instant (pas de reseau code).
        self._mp_items = ["Créer une partie", "Rejoindre une partie"]
        self._mp_index = 0
        self._mp_rects = []
        # --- Multijoueur (lobby reseau partage). SolVsTerra = TOUT le serveur. ---
        self._mp = None
        self._mp_list = []
        self._mp_list_idx = 0
        self._mp_roster = []
        self._mp_host = None
        self._mp_notice = ""
        # --- Partie en reseau (bataille navale : flottes secretes) ---
        self._is_net = False
        self._net_seat = None       # 0 ou 1 (ordre serveur) ; seat 0 tire en 1er
        self._net_ready = False     # ma flotte est posee et confirmee
        self._net_peer_ready = False
        self._net_result = None     # "win" | "lose"
        self._foe_hits = set()      # cases TOUCHEES sur la grille ennemie (reseau)
        self._foe_sunk = []         # navires ADVERSES coules (cells connues
                                    # via sunk_cells) -> sprites grille ennemie
        # Watchdog d'inactivite : si on attend le tir adverse (etat "enemy")
        # depuis trop longtemps (AFK / crash), on affiche une porte de sortie.
        # Pas de forfait force : l'utilisateur quitte avec Retour.
        self._NET_OPP_TIMEOUT = 45.0
        self._net_wait_state = None
        self._net_wait_since = 0.0
        self._net_watchdog = QTimer(self)
        self._net_watchdog.setInterval(1000)
        self._net_watchdog.timeout.connect(self._net_watchdog_tick)
        # menu|mode|mp|mp_browse|mp_lobby|rules|placement|ready|wait|player|enemy|over
        self._state = "menu"

    # --- Cycle de vie (contrat PhoneApp) ------------------------------
    def on_show(self):
        """Devient l'ecran courant : on revient TOUJOURS au menu d'accueil
        (ouvrir le jeu depuis la liste d'apps doit afficher son menu, pas
        reprendre un etat precedent). Prend le focus clavier."""
        self._paused = False
        self._ai_timer.stop()
        self._net_watchdog.stop()
        self._ai_timer_was_active = False
        # Si un salon/partie reseau etait actif, le quitter proprement.
        if self._mp is not None and getattr(self._mp, "lobby_id", None):
            try:
                self._mp.leave()
            except Exception:
                pass
        self._is_net = False
        self._net_ready = False
        self._net_peer_ready = False
        self._net_result = None
        self._mp_roster = []
        self._mp_host = None
        self._mp_notice = ""
        self._state = "menu"
        self._menu_index = 0
        self.setFocus()
        self.update()

    def on_hide(self):
        """Quitte l'ecran : met la partie en pause en arretant le timer IA.
        Idempotent (peut etre appele par l'overlay ET par hideEvent)."""
        if self._paused:
            return
        self._paused = True
        self._ai_timer_was_active = self._ai_timer.isActive()
        self._ai_timer.stop()
        self._net_watchdog.stop()

    @property
    def skin(self):
        return self._skins[self._skin_idx]

    def cycle_skin(self):
        self._skin_idx = (self._skin_idx + 1) % len(self._skins)
        self.update()

    def handle_back(self) -> bool:
        """Retour (Echap) : ecran Regles -> menu ; partie en cours -> menu ;
        menu -> non consomme (l'overlay revient au home du telephone)."""
        if self._state in ("rules", "mode"):
            self._state = "menu"
            self.update()
            return True
        if self._state == "mp":
            self._state = "mode"        # retour au choix Solo / Online
            self.update()
            return True
        if self._state == "mp_browse":
            self._mp_notice = ""
            self._state = "mp"
            self.update()
            return True
        if self._state == "mp_lobby":
            if self._mp:
                try:
                    self._mp.leave()
                except Exception:
                    pass
            self._mp_roster = []
            self._mp_host = None
            self._mp_notice = ""
            self._state = "mp"
            self.update()
            return True
        if self._state in ("placement", "ready", "wait", "player",
                           "enemy", "over"):
            self._ai_timer.stop()
            self._net_watchdog.stop()
            if self._is_net and self._mp:
                try:
                    self._mp.leave()
                except Exception:
                    pass
            self._is_net = False
            self._state = "menu"
            self._menu_index = 0
            self.update()
            return True
        return False

    def _mp_get(self):
        """Session multijoueur partagee. SolVsTerra = scope SERVEUR (tous les
        joueurs connectes). Retourne None si le module MP est absent."""
        if mp is None:
            return None
        if self._mp is not None:
            return self._mp
        send_ws = getattr(self.services, "send_ws", None)
        my_name = getattr(self.services, "my_name", None) or "Toi"
        self._mp = mp.MpLobby("solvsterra", mp.SCOPE_SERVER, send_ws, my_name, self)
        self._mp.sig_list.connect(self._on_mp_list)
        self._mp.sig_lobby.connect(self._on_mp_lobby)
        self._mp.sig_started.connect(self._on_mp_started)
        self._mp.sig_game.connect(self._on_mp_game)
        self._mp.sig_error.connect(self._on_mp_error)
        self._mp.sig_closed.connect(self._on_mp_closed)
        return self._mp

    def _mp_activate(self):
        """Creer / rejoindre une partie en ligne (tout le serveur)."""
        m = self._mp_get()
        if m is None:
            self._mp_notice = "Multijoueur indisponible (module manquant)."
            self.update()
            return
        self._mp_notice = ""
        if self._mp_index == 0:          # Créer
            self._mp_roster = [{"name": m.my_name, "ready": False}]
            self._mp_host = m.my_name
            m.create({})
            self._state = "mp_lobby"
        else:                            # Rejoindre
            self._mp_list = []
            self._mp_list_idx = 0
            m.refresh_list()
            self._state = "mp_browse"
        self.update()

    # --- Signaux MpLobby (serveur -> UI) ------------------------------
    def _on_mp_list(self, lobbies):
        self._mp_list = list(lobbies or [])
        if self._mp_list_idx >= len(self._mp_list):
            self._mp_list_idx = max(0, len(self._mp_list) - 1)
        if self._state == "mp_browse":
            self.update()

    def _on_mp_lobby(self, snap):
        self._mp_roster = list(snap.get("members") or [])
        self._mp_host = snap.get("host")
        if self._state in ("mp", "mp_browse"):
            self._state = "mp_lobby"
        self.update()

    def _on_mp_started(self, payload):
        self._start_network_match(payload)

    def _on_mp_error(self, code, msg):
        self._mp_notice = msg or code
        self.update()

    def _on_mp_closed(self, reason):
        self._mp_notice = ("Partie fermée : l'adversaire est parti."
                           if reason == "host_left" else "Partie fermée.")
        self._mp_roster = []
        self._mp_host = None
        self._is_net = False
        self._ai_timer.stop()
        self._net_watchdog.stop()
        self._state = "mp"
        self.update()

    def _start_network_match(self, payload):
        """Demarre une partie reseau : on passe au placement (chaque joueur
        pose sa flotte). Le 1er a tirer = siege 0 (ordre serveur)."""
        order = payload.get("order") or []
        my_name = getattr(self.services, "my_name", None)
        try:
            self._net_seat = order.index(my_name) if my_name else 0
        except ValueError:
            self._net_seat = 0
        self._ai_timer.stop()
        self._reset()                 # remet en placement, flottes vides
        self._is_net = True
        self._net_ready = False
        self._net_peer_ready = False
        self._net_result = None
        self._foe_hits = set()
        self._foe_ships = []          # flotte adverse SECRETE (inconnue ici)
        self._state = "placement"
        self._msg = "Place ta flotte."
        self._foe_sunk = []
        self._net_wait_state = None
        self._net_wait_since = time.monotonic()
        self._net_watchdog.start()
        self.update()

    def _net_watchdog_tick(self):
        """Rafraichit l'affichage tant qu'on ATTEND l'adversaire (etat 'enemy'
        en combat, ou 'wait' avant combat) et (re)arme le compteur a chaque
        changement d'etat, pour la banniere d'inactivite."""
        if not self._is_net:
            return
        waiting = self._state in ("enemy", "wait")
        if self._state != self._net_wait_state:
            self._net_wait_state = self._state
            self._net_wait_since = time.monotonic()
        if waiting:
            self.update()

    def _net_opp_idle_secs(self):
        """Depuis combien de temps on attend l'adversaire sans interruption
        (0 si ce n'est pas le cas)."""
        if not self._is_net or self._state not in ("enemy", "wait"):
            return 0.0
        return time.monotonic() - self._net_wait_since

    def _menu_activate(self):
        """Active le bouton selectionne du menu d'accueil."""
        idx = self._menu_index
        if idx == 0:            # Jouer -> ecran de choix du mode (Solo/Online)
            self._mode_index = 0
            self._state = "mode"
            self.update()
        elif idx == 1:          # Règles -> ecran d'explications
            self._state = "rules"
            self.update()
        elif idx == 2:          # Quitter -> revient au home du telephone
            try:
                self.sig_request_home.emit()
            except Exception:
                pass

    def _mode_activate(self):
        """Active le mode choisi (Solo / Online)."""
        if self._mode_index == 0:       # Solo -> nouvelle partie locale (IA)
            self._is_net = False
            self._reset()               # repasse en etat "placement"
            self.update()
        else:                           # Online -> ecran creer/rejoindre
            self._mp_index = 0
            self._state = "mp"
            self.update()

    def handle_server_msg(self, data) -> bool:
        """Route un message serveur mp_* vers la session multijoueur."""
        if self._mp is None:
            return False
        try:
            return bool(self._mp.handle_server_msg(data))
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _reset(self):
        self._foe_ships = _random_fleet()
        self._own_ships = []
        self._foe_shots = set()
        self._own_shots = set()
        self._ai_queue = []
        self._ai_hits = []
        self._cx = self._cy = _N // 2
        # Placement
        self._place_idx = 0
        self._place_horiz = True
        self._place_x = 0
        self._place_y = 0
        self._state = "placement"   # placement|ready|player|enemy|over
        self._msg = ""
        self._clamp_anchor()

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------
    def _current_ship(self):
        if self._place_idx < len(_FLEET):
            return _FLEET[self._place_idx]
        return None

    def _placed_cells(self):
        return {c for s in self._own_ships for c in s["cells"]}

    def _clamp_anchor(self):
        ship = self._current_ship()
        if not ship:
            return
        size = ship[1]
        if self._place_horiz:
            self._place_x = max(0, min(_N - size, self._place_x))
            self._place_y = max(0, min(_N - 1, self._place_y))
        else:
            self._place_x = max(0, min(_N - 1, self._place_x))
            self._place_y = max(0, min(_N - size, self._place_y))

    def _ghost_cells(self):
        ship = self._current_ship()
        if not ship:
            return []
        size = ship[1]
        if self._place_horiz:
            return [(self._place_x + i, self._place_y) for i in range(size)]
        return [(self._place_x, self._place_y + i) for i in range(size)]

    def _ghost_valid(self, cells):
        occupied = self._placed_cells()
        return all(0 <= x < _N and 0 <= y < _N and (x, y) not in occupied
                   for (x, y) in cells)

    def move_place(self, name):
        if self._state != "placement" or name not in _DIRS:
            return
        dx, dy = _DIRS[name]
        self._place_x += dx
        self._place_y += dy
        self._clamp_anchor()
        self.update()

    def rotate_place(self):
        if self._state != "placement":
            return
        self._place_horiz = not self._place_horiz
        self._clamp_anchor()
        self.update()

    def place_current(self):
        if self._state != "placement":
            return
        cells = self._ghost_cells()
        if not cells or not self._ghost_valid(cells):
            self._msg = "Position invalide."
            self.update()
            return
        name, size = self._current_ship()
        self._own_ships.append({"name": name, "size": size,
                                "cells": list(cells), "hits": set()})
        self._place_idx += 1
        if self._place_idx >= len(_FLEET):
            self._state = "ready"
        else:
            self._place_x = self._place_y = 0
            self._clamp_anchor()
        self.update()

    def undo_place(self):
        if self._own_ships and self._state in ("placement", "ready"):
            self._own_ships.pop()
            self._place_idx = len(self._own_ships)
            self._state = "placement"
            self._place_x = self._place_y = 0
            self._place_horiz = True
            self._clamp_anchor()
            self.update()

    def autofill(self):
        if self._state != "placement":
            return
        occupied = set(self._placed_cells())
        for name, size in _FLEET[self._place_idx:]:
            for _ in range(2000):
                horiz = random.random() < 0.5
                if horiz:
                    x = random.randint(0, _N - size)
                    y = random.randint(0, _N - 1)
                    cells = [(x + i, y) for i in range(size)]
                else:
                    x = random.randint(0, _N - 1)
                    y = random.randint(0, _N - size)
                    cells = [(x, y + i) for i in range(size)]
                if any(c in occupied for c in cells):
                    continue
                occupied.update(cells)
                self._own_ships.append({"name": name, "size": size,
                                        "cells": cells, "hits": set()})
                break
        self._place_idx = len(_FLEET)
        self._state = "ready"
        self.update()

    def begin_battle(self):
        if self._state != "ready":
            return
        if self._is_net:
            if not self._net_ready:
                self._net_ready = True
                if self._mp is not None:
                    try:
                        self._mp.send_game({"k": "ready"})
                    except Exception:
                        pass
            self._try_start_net_combat()
            return
        self._state = "player"
        self._msg = "À toi de tirer."
        self.update()

    def _try_start_net_combat(self):
        """Demarre le combat quand les DEUX flottes sont posees. Le siege 0
        (ordre serveur) tire en premier."""
        if not (self._net_ready and self._net_peer_ready):
            self._state = "wait"
            self._msg = "En attente de l'adversaire…"
            self.update()
            return
        if self._net_seat == 0:
            self._state = "player"
            self._msg = "À toi de tirer."
        else:
            self._state = "enemy"
            self._msg = "Tour de l'adversaire…"
        self.update()

    def _on_mp_game(self, ev):
        """Events de partie reseau (bataille navale). 3 types :
        - ready  : l'adversaire a pose sa flotte.
        - fire   : l'adversaire tire sur MA grille -> je calcule et renvoie result.
        - result : reponse a MON tir -> j'actualise la grille ennemie."""
        if not self._is_net:
            return
        payload = (ev or {}).get("payload") or {}
        k = payload.get("k")
        if k == "ready":
            self._net_peer_ready = True
            if self._net_ready:
                self._try_start_net_combat()
            elif self._state in ("placement", "ready", "wait"):
                self._msg = "Adversaire prêt — pose ta flotte."
                self.update()
            return
        if k == "fire":
            x, y = int(payload.get("x", -1)), int(payload.get("y", -1))
            cell = (x, y)
            if not (0 <= x < _N and 0 <= y < _N) or cell in self._own_shots:
                return
            self._own_shots.add(cell)
            ship = _ship_at(self._own_ships, cell)
            hit = ship is not None
            sunk_name = None
            if hit:
                ship["hits"].add(cell)
                if _is_sunk(ship):
                    sunk_name = ship["name"]
            dead = _fleet_dead(self._own_ships)
            # Cellules du navire coule : envoyees au tireur pour qu'il puisse
            # dessiner le SPRITE du navire sur sa grille ennemie (sinon il ne
            # connait que des croix). Listes JSON -> retuple a la reception.
            sunk_cells = (list(map(list, ship["cells"]))
                          if (hit and ship is not None and _is_sunk(ship))
                          else None)
            if self._mp is not None:
                try:
                    self._mp.send_game({"k": "result", "x": x, "y": y,
                                        "hit": hit, "sunk": sunk_name,
                                        "sunk_cells": sunk_cells,
                                        "sunk_size": (ship["size"]
                                                      if sunk_cells else None),
                                        "dead": dead})
                except Exception:
                    pass
            if dead:
                self._net_result = "lose"
                self._state = "over"
                self._net_watchdog.stop()
                self._msg = "DÉFAITE — ta flotte est coulée."
            else:
                self._state = "player"
                self._msg = "À toi de tirer."
            self.update()
            return
        if k == "result":
            x, y = int(payload.get("x", -1)), int(payload.get("y", -1))
            cell = (x, y)
            self._foe_shots.add(cell)
            if payload.get("hit"):
                self._foe_hits.add(cell)
                self._msg = ("Coulé : %s !" % payload.get("sunk")
                             if payload.get("sunk") else "Touché !")
                sc = payload.get("sunk_cells")
                if sc:
                    try:
                        cells = [tuple(map(int, c)) for c in sc]
                        self._foe_sunk.append({
                            "name": payload.get("sunk") or "?",
                            "size": int(payload.get("sunk_size")
                                        or len(cells)),
                            "cells": cells,
                            "hits": set(cells),   # tout touche -> _is_sunk OK
                        })
                    except Exception:
                        pass
            else:
                self._msg = "À l'eau."
            if payload.get("dead"):
                self._net_result = "win"
                self._state = "over"
                self._net_watchdog.stop()
                self._msg = "VICTOIRE — flotte ennemie coulée !"
            else:
                self._state = "enemy"
                self._msg = "Tour de l'adversaire…"
            self.update()
            return

    # ------------------------------------------------------------------
    # Combat
    # ------------------------------------------------------------------
    def move_cursor(self, name):
        if self._state != "player" or name not in _DIRS:
            return
        dx, dy = _DIRS[name]
        self._cx = max(0, min(_N - 1, self._cx + dx))
        self._cy = max(0, min(_N - 1, self._cy + dy))
        self.update()

    def fire_here(self):
        if self._state != "player":
            return
        cell = (self._cx, self._cy)
        if cell in self._foe_shots:
            return
        if self._is_net:
            # Flotte adverse SECRETE : on envoie le tir et on attend le
            # resultat (touche/coule/flotte coulee) renvoye par l'adversaire.
            if self._mp is not None:
                try:
                    self._mp.send_game({"k": "fire", "x": cell[0], "y": cell[1]})
                except Exception:
                    pass
            self._msg = "Tir envoyé…"
            self._state = "enemy"
            self.update()
            return
        self._foe_shots.add(cell)
        ship = _ship_at(self._foe_ships, cell)
        if ship:
            ship["hits"].add(cell)
            self._msg = (f"Coulé : {ship['name']} !" if _is_sunk(ship)
                         else "Touché !")
        else:
            self._msg = "À l'eau."
        if _fleet_dead(self._foe_ships):
            self._state = "over"
            self._msg = "VICTOIRE — flotte ennemie coulée !"
            self.update()
            return
        self._state = "enemy"
        self.update()
        self._ai_timer.start(_AI_DELAY_MS)

    def _ai_choose(self):
        while self._ai_queue:
            c = self._ai_queue.pop()
            if c not in self._own_shots:
                return c
        cands = [(x, y) for x in range(_N) for y in range(_N)
                 if (x, y) not in self._own_shots and (x + y) % 2 == 0]
        if not cands:
            cands = [(x, y) for x in range(_N) for y in range(_N)
                     if (x, y) not in self._own_shots]
        return random.choice(cands)

    def _ai_register(self, cell, ship):
        if ship is None:
            return
        if _is_sunk(ship):
            self._ai_hits, self._ai_queue = [], []
            return
        self._ai_hits.append(cell)
        hits = self._ai_hits
        q = []
        if len(hits) >= 2:
            xs = {h[0] for h in hits}
            ys = {h[1] for h in hits}
            if len(xs) == 1:
                x = hits[0][0]
                ys_s = sorted(h[1] for h in hits)
                for y in (ys_s[0] - 1, ys_s[-1] + 1):
                    if 0 <= y < _N and (x, y) not in self._own_shots:
                        q.append((x, y))
            elif len(ys) == 1:
                y = hits[0][1]
                xs_s = sorted(h[0] for h in hits)
                for x in (xs_s[0] - 1, xs_s[-1] + 1):
                    if 0 <= x < _N and (x, y) not in self._own_shots:
                        q.append((x, y))
        else:
            x, y = hits[0]
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < _N and 0 <= ny < _N and (nx, ny) not in self._own_shots:
                    q.append((nx, ny))
        self._ai_queue = q

    def _ai_fire(self):
        if self._is_net:        # pas d'IA en reseau : c'est un vrai adversaire
            return
        if self._state != "enemy":
            return
        cell = self._ai_choose()
        self._own_shots.add(cell)
        ship = _ship_at(self._own_ships, cell)
        if ship:
            ship["hits"].add(cell)
            self._ai_register(cell, ship)
            self._msg = (f"L'ennemi coule ton {ship['name']} !"
                         if _is_sunk(ship) else "L'ennemi te touche !")
        else:
            self._msg = "L'ennemi tire à l'eau."
        if _fleet_dead(self._own_ships):
            self._state = "over"
            self._msg = "DÉFAITE — ta flotte est coulée."
            self.update()
            return
        self._state = "player"
        self.update()

    # ------------------------------------------------------------------
    # Entrées
    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent):
        k = event.key()
        st = self._state
        fire_keys = (Qt.Key_Return, Qt.Key_Enter)
        # Etat "menu" : navigation verticale + validation.
        if st == "menu":
            if k in (Qt.Key_Up,):
                self._menu_index = (self._menu_index - 1) % len(self._menu_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._menu_index = (self._menu_index + 1) % len(self._menu_items)
                self.update()
            elif k in fire_keys:
                if self._confirm_armed:
                    self._menu_activate()
            else:
                super().keyPressEvent(event)
            return
        # Etat "rules" : toute validation revient au menu.
        if st == "rules":
            if k in fire_keys or k == Qt.Key_Backspace:
                self._state = "menu"
                self.update()
            else:
                super().keyPressEvent(event)
            return
        # Etat "mode" : choix Solo / Online.
        if st == "mode":
            if k in (Qt.Key_Up,):
                self._mode_index = (self._mode_index - 1) % len(self._mode_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._mode_index = (self._mode_index + 1) % len(self._mode_items)
                self.update()
            elif k in fire_keys:
                self._mode_activate()
            elif k == Qt.Key_Backspace:
                self._state = "menu"
                self.update()
            else:
                super().keyPressEvent(event)
            return
        # Etat "mp" : creer / rejoindre une partie en ligne.
        if st == "mp":
            if k in (Qt.Key_Up,):
                self._mp_index = (self._mp_index - 1) % len(self._mp_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._mp_index = (self._mp_index + 1) % len(self._mp_items)
                self.update()
            elif k in fire_keys:
                self._mp_activate()
            elif k == Qt.Key_Backspace:
                self._state = "mode"
                self.update()
            else:
                super().keyPressEvent(event)
            return
        if st == "mp_browse":
            n = len(self._mp_list)
            if k in (Qt.Key_Up,) and n:
                self._mp_list_idx = (self._mp_list_idx - 1) % n; self.update()
            elif k in (Qt.Key_Down,) and n:
                self._mp_list_idx = (self._mp_list_idx + 1) % n; self.update()
            elif k == Qt.Key_R:
                self._mp_notice = ""
                if self._mp:
                    self._mp.refresh_list()
            elif k in fire_keys and n:
                lid = self._mp_list[self._mp_list_idx].get("lobby_id")
                if lid and self._mp:
                    self._mp_notice = ""
                    self._mp.join(lid)
            return
        if st == "mp_lobby":
            if k in fire_keys:
                if self._mp:
                    self._mp.toggle_ready()
            return
        if k == Qt.Key_V:
            # V (et non C : C = "s'allonger" chez certains joueurs SC).
            self.cycle_skin()
            return
        if st == "placement":
            if k in (Qt.Key_Up,):
                self.move_place("up")
            elif k in (Qt.Key_Down,):
                self.move_place("down")
            elif k in (Qt.Key_Left,):
                self.move_place("left")
            elif k in (Qt.Key_Right,):
                self.move_place("right")
            elif k in fire_keys:
                self.place_current()
            elif k == Qt.Key_R:
                self.rotate_place()
            elif k == Qt.Key_Backspace:
                self.undo_place()
            elif k == Qt.Key_F:
                self.autofill()
        elif st == "ready":
            if k in fire_keys:
                self.begin_battle()
            elif k == Qt.Key_Backspace:
                self.undo_place()
        elif st == "player":
            if k in (Qt.Key_Up,):
                self.move_cursor("up")
            elif k in (Qt.Key_Down,):
                self.move_cursor("down")
            elif k in (Qt.Key_Left,):
                self.move_cursor("left")
            elif k in (Qt.Key_Right,):
                self.move_cursor("right")
            elif k in fire_keys:
                self.fire_here()
        elif st == "over":
            if k in fire_keys and not self._is_net:
                self._reset()
                self.update()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        st = self._state
        if st == "rules":
            self._state = "menu"
            self.update()
            return
        if st == "mode":
            pos = e.position()
            for i, r in enumerate(self._mode_rects):
                if r.contains(pos):
                    self._mode_index = i
                    self.update()
                    self._mode_activate()
                    return
            return
        if st == "mp":
            pos = e.position()
            for i, r in enumerate(self._mp_rects):
                if r.contains(pos):
                    self._mp_index = i
                    self.update()
                    self._mp_activate()
                    return
            return
        if st == "menu":
            pos = e.position()
            for i, r in enumerate(self._menu_rects):
                if r.contains(pos):
                    self._menu_index = i
                    self.update()
                    self._menu_activate()
                    return
            return
        if st == "over":
            self._reset()
            self.update()
            return
        if st == "ready":
            self.begin_battle()
            return
        if not self._last_layout:
            return
        p = e.position()
        if st == "placement":
            gx, gy, cell = self._last_layout["own"]
            x = int((p.x() - gx) // cell)
            y = int((p.y() - gy) // cell)
            if 0 <= x < _N and 0 <= y < _N:
                self._place_x, self._place_y = x, y
                self._clamp_anchor()
                self.place_current()
        elif st == "player":
            gx, gy, cell = self._last_layout["foe"]
            x = int((p.x() - gx) // cell)
            y = int((p.y() - gy) // cell)
            if 0 <= x < _N and 0 <= y < _N:
                self._cx, self._cy = x, y
                self.fire_here()

    def hideEvent(self, event):
        # Filet de securite : si on cache l'app sans passer par l'overlay
        # (ex. un appel entrant bascule l'ecran), on pause via on_hide.
        self.on_hide()
        super().hideEvent(event)

    # ------------------------------------------------------------------
    # Rendu
    # ------------------------------------------------------------------
    def _layout(self):
        w, h = self.width(), self.height()
        pad = max(3, int(w * 0.03))
        lbl = max(11, int(h * 0.022))
        status = max(16, int(h * 0.05))
        gap = max(4, int(h * 0.012))
        fixed = pad + lbl + gap + status + gap + lbl + gap + pad
        grid_h = (h - fixed) / 2
        cell = int(min(grid_h / _N, (w - 2 * pad) / _N))
        cell = max(8, cell)
        grid_px = cell * _N
        gx = (w - grid_px) // 2
        y = pad
        foe_lbl_y = y + lbl
        foe_gy = foe_lbl_y + gap
        status_y = foe_gy + grid_px + gap
        own_lbl_y = status_y + status + lbl
        own_gy = own_lbl_y + gap
        return {
            "cell": cell, "gx": gx, "lbl": lbl, "status": status,
            "foe_lbl_y": foe_lbl_y, "foe": (gx, foe_gy, cell),
            "status_y": status_y,
            "own_lbl_y": own_lbl_y, "own": (gx, own_gy, cell),
        }

    def _status_text(self):
        if self._state == "placement":
            s = self._current_ship()
            if s:
                return f"{s[0]} ({s[1]})  ·  R pivoter  ·  F auto"
            return ""
        if self._state == "ready":
            return "Flotte prête."
        return self._msg

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self._screen_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()),
                                self._screen_radius, self._screen_radius)
            p.setClipPath(path)
        c = self.skin.c
        if self.skin.background is not None:
            bg = self.skin.background.scaled(
                self.width(), self.height(),
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap((self.width() - bg.width()) // 2,
                         (self.height() - bg.height()) // 2, bg)
            p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        else:
            p.fillRect(self.rect(), QColor(c["bg"]))

        # Menu / Mode / Regles : par-dessus le decor du jeu (fond deja peint),
        # sans dessiner les grilles ni les vaisseaux.
        if self._state in ("menu", "rules", "mode", "mp",
                           "mp_browse", "mp_lobby"):
            if self._state == "menu":
                self._draw_menu(p)
            elif self._state == "mode":
                self._draw_mode(p)
            elif self._state == "mp":
                self._draw_mp(p)
            elif self._state == "mp_browse":
                self._draw_mp_browse(p)
            elif self._state == "mp_lobby":
                self._draw_mp_lobby(p)
            else:
                self._draw_rules(p)
            p.end()
            return

        lay = self._layout()
        self._last_layout = lay
        cell = lay["cell"]

        flbl = QFont("Consolas", max(7, int(lay["lbl"] * 0.62)))
        flbl.setBold(True)
        p.setFont(flbl)
        p.setPen(QColor(c["label_foe"]))
        p.drawText(lay["gx"], lay["foe_lbl_y"] - 2, "FLOTTE ENNEMIE")
        # Nom du skin (HUD) aligné à droite : preuve visible que C agit.
        fhud = QFont("Consolas", max(6, int(lay["lbl"] * 0.52)))
        p.setFont(fhud)
        p.setPen(QColor(c["muted"]))
        hud = f"skin: {self.skin.name}"
        fm = QFontMetrics(fhud)
        p.drawText(lay["gx"] + cell * _N - fm.horizontalAdvance(hud),
                   lay["foe_lbl_y"] - 2, hud)
        p.setFont(flbl)
        p.setPen(QColor(c["accent"]))
        own_label = "DÉPLOIE TA FLOTTE" if self._state == "placement" else "TA FLOTTE"
        p.drawText(lay["gx"], lay["own_lbl_y"] - 2, own_label)

        gx, foe_gy, _ = lay["foe"]
        self._draw_board(p, gx, foe_gy, cell,
                         (self._foe_sunk if self._is_net
                          else self._foe_ships),
                         self._foe_shots, reveal=False,
                         cursor=(self._cx, self._cy) if self._state == "player" else None,
                         hit_cells=self._foe_hits if self._is_net else None)
        gx, own_gy, _ = lay["own"]
        self._draw_board(p, gx, own_gy, cell, self._own_ships, self._own_shots,
                         reveal=True, cursor=None)
        if self._state == "placement":
            cells = self._ghost_cells()
            self._draw_ghost(p, gx, own_gy, cell, cells, self._ghost_valid(cells))

        # Statut
        rect = QRectF(lay["gx"], lay["status_y"], cell * _N, lay["status"])
        if self._state == "placement":
            s = self._current_ship()
            top = QRectF(rect.x(), rect.y(), rect.width(), rect.height() * 0.55)
            bot = QRectF(rect.x(), rect.y() + rect.height() * 0.45,
                         rect.width(), rect.height() * 0.55)
            fn = QFont("Consolas", max(7, int(lay["status"] * 0.40)))
            fn.setBold(True)
            p.setFont(fn)
            p.setPen(QColor(c["accent"]))
            p.drawText(top, Qt.AlignCenter, f"{s[0]} ({s[1]})" if s else "")
            fc2 = QFont("Consolas", max(6, int(lay["status"] * 0.30)))
            p.setFont(fc2)
            p.setPen(QColor(c["muted"]))
            p.drawText(bot, Qt.AlignCenter, "R pivoter   F auto   ⌫ annuler")
        else:
            fs = QFont("Consolas", max(7, int(lay["status"] * 0.38)))
            fs.setBold(True)
            p.setFont(fs)
            p.setPen(QColor(c["text"]))
            p.drawText(rect, Qt.AlignCenter, self._status_text())

        # Porte de sortie si on attend le tir adverse en combat ("enemy").
        # (Le cas "wait" est gere via le sous-titre de son overlay, plus bas,
        #  car un overlay recouvre cette zone.)
        if (self._state == "enemy"
                and self._net_opp_idle_secs() >= self._NET_OPP_TIMEOUT):
            fw = QFont("Consolas", max(7, int(lay["status"] * 0.30)))
            p.setFont(fw)
            p.setPen(QColor("#f85149"))
            wr = QRectF(rect.x(), rect.y() + rect.height() * 0.62,
                        rect.width(), rect.height() * 0.4)
            p.drawText(wr, Qt.AlignCenter,
                       "Adversaire inactif — Retour pour quitter")

        if self._state == "ready":
            self._draw_overlay(p, "FLOTTE\nPRÊTE",
                               "Entrée : combat\nRetour arrière : modifier")
        elif self._state == "wait":
            sub = "L'adversaire pose sa flotte…"
            if self._net_opp_idle_secs() >= self._NET_OPP_TIMEOUT:
                sub += "\nInactif — Retour pour quitter"
            self._draw_overlay(p, "EN ATTENTE", sub)
        elif self._state == "over":
            if self._is_net:
                win = (self._net_result == "win")
            else:
                win = _fleet_dead(self._foe_ships)
            self._draw_overlay(p, "VICTOIRE" if win else "DÉFAITE",
                               "Entrée : rejouer" if not self._is_net
                               else "Retour arrière : quitter")
        p.end()

    def _draw_board(self, p, gx, gy, cell, ships, shots, reveal, cursor,
                    hit_cells=None):
        # hit_cells (reseau) : ensemble des cases TOUCHEES connues quand on ne
        # possede pas la flotte adverse (ships=[]). Sinon on deduit via les
        # navires locaux (_ship_at).
        c = self.skin.c
        wa = 170 if self.skin.background is not None else 255
        for y in range(_N):
            for x in range(_N):
                base = c["water"] if (x + y) % 2 == 0 else c["water2"]
                col = QColor(base)
                col.setAlpha(wa)
                p.fillRect(gx + x * cell, gy + y * cell, cell, cell, col)
        p.setPen(QPen(QColor(c["grid"]), 1))
        for i in range(_N + 1):
            p.drawLine(gx + i * cell, gy, gx + i * cell, gy + _N * cell)
            p.drawLine(gx, gy + i * cell, gx + _N * cell, gy + i * cell)

        for s in ships:
            sunk = _is_sunk(s)
            if not (reveal or sunk):
                continue
            cells = s["cells"]
            sprite = self.skin.ship_for(s["size"])
            if sprite is not None:
                xs = [cc[0] for cc in cells]
                ys = [cc[1] for cc in cells]
                horiz = (max(xs) - min(xs)) >= (max(ys) - min(ys))
                inset = max(1.0, cell * 0.06)
                rect = QRectF(gx + min(xs) * cell + inset,
                              gy + min(ys) * cell + inset,
                              (max(xs) - min(xs) + 1) * cell - 2 * inset,
                              (max(ys) - min(ys) + 1) * cell - 2 * inset)
                self._draw_ship_sprite(p, sprite, rect, horiz)
                if sunk:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(248, 81, 73, 120))
                    p.drawRoundedRect(rect, cell * 0.18, cell * 0.18)
            else:
                for (x, y) in cells:
                    rx, ry = gx + x * cell, gy + y * cell
                    inset = max(1.0, cell * 0.12)
                    rr = QRectF(rx + inset, ry + inset,
                                cell - 2 * inset, cell - 2 * inset)
                    p.setPen(QPen(QColor(c["sunk"] if sunk else c["ship_edge"]), 1))
                    p.setBrush(QColor(c["sunk"] if sunk else c["ship"]))
                    p.drawRoundedRect(rr, cell * 0.18, cell * 0.18)

        for (x, y) in shots:
            rx, ry = gx + x * cell, gy + y * cell
            cxp, cyp = rx + cell / 2, ry + cell / 2
            is_hit = ((x, y) in hit_cells) if hit_cells is not None \
                else (_ship_at(ships, (x, y)) is not None)
            if is_hit:
                m = cell * 0.26
                p.setPen(QPen(QColor(c["hit"]), max(2, int(cell * 0.12))))
                p.drawLine(QPointF(cxp - m, cyp - m), QPointF(cxp + m, cyp + m))
                p.drawLine(QPointF(cxp - m, cyp + m), QPointF(cxp + m, cyp - m))
            else:
                r = cell * 0.12
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(c["miss"]))
                p.drawEllipse(QRectF(cxp - r, cyp - r, 2 * r, 2 * r))

        if cursor is not None:
            x, y = cursor
            p.setPen(QPen(QColor(c["cursor"]), max(2, int(cell * 0.12))))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(gx + x * cell + 1, gy + y * cell + 1,
                              cell - 2, cell - 2))

    def _draw_ship_sprite(self, p, pix, rect, horiz):
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if horiz:
            p.drawPixmap(rect, pix, QRectF(pix.rect()))
        else:
            p.translate(rect.center())
            p.rotate(90)
            tr = QRectF(-rect.height() / 2, -rect.width() / 2,
                        rect.height(), rect.width())
            p.drawPixmap(tr, pix, QRectF(pix.rect()))
        p.restore()

    def _draw_ghost(self, p, gx, gy, cell, cells, valid):
        incell = [c for c in cells if 0 <= c[0] < _N and 0 <= c[1] < _N]
        if not incell:
            return
        sprite = self.skin.ship_for(len(cells)) if valid else None
        if sprite is not None:
            xs = [c[0] for c in cells]
            ys = [c[1] for c in cells]
            horiz = (max(xs) - min(xs)) >= (max(ys) - min(ys))
            inset = max(1.0, cell * 0.06)
            rect = QRectF(gx + min(xs) * cell + inset, gy + min(ys) * cell + inset,
                          (max(xs) - min(xs) + 1) * cell - 2 * inset,
                          (max(ys) - min(ys) + 1) * cell - 2 * inset)
            p.setOpacity(0.9)
            self._draw_ship_sprite(p, sprite, rect, horiz)
            p.setOpacity(1.0)
            p.setPen(QPen(QColor(self.skin.c["accent"]), max(1, int(cell * 0.10))))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, cell * 0.18, cell * 0.18)
        else:
            fill = QColor(63, 185, 80, 150) if valid else QColor(248, 81, 73, 150)
            edge = QColor(self.skin.c["accent"]) if valid else QColor(self.skin.c["hit"])
            for (x, y) in incell:
                rx, ry = gx + x * cell, gy + y * cell
                inset = max(1.0, cell * 0.12)
                rr = QRectF(rx + inset, ry + inset, cell - 2 * inset, cell - 2 * inset)
                p.setPen(QPen(edge, max(1, int(cell * 0.08))))
                p.setBrush(fill)
                p.drawRoundedRect(rr, cell * 0.18, cell * 0.18)

    def _draw_button_screen(self, p, title, items, sel_index):
        """Dessine un ecran a boutons verticaux (titre + liste) par-dessus le
        decor du jeu. Retourne la liste des QRectF des boutons (pour la souris).
        Mutualise par le menu et l'ecran de choix du mode."""
        c = self.skin.c
        accent = c.get("accent", "#3fb950")
        text = c.get("text", "#d8d8d8")
        border = c.get("muted", "#6e7681")
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 130))

        # Titre.
        p.setPen(QColor(accent))
        ft = QFont("Consolas", max(15, w // 10))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.08, w, h * 0.22),
                   int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                   title)

        # Boutons.
        rects = []
        bw = w * 0.80
        bh = max(32.0, h * 0.10)
        gap = bh * 0.38
        x = (w - bw) / 2.0
        y0 = h * 0.40
        fb = QFont("Consolas", max(9, w // 18))
        fb.setBold(True)
        rad = bh * 0.28
        for i, label in enumerate(items):
            r = QRectF(x, y0 + i * (bh + gap), bw, bh)
            rects.append(r)
            if i == sel_index:
                p.setBrush(QColor(accent))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor("#0d1117"))
            else:
                p.setBrush(QColor(0, 0, 0, 150))
                p.setPen(QPen(QColor(border), 1.5))
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor(text))
            p.setFont(fb)
            p.drawText(r, Qt.AlignCenter, label)
        return rects

    def _draw_menu(self, p):
        """Menu d'accueil (etat menu) : 'Sol VS Terra' + 4 boutons."""
        self._menu_rects = self._draw_button_screen(
            p, "Sol VS Terra", self._menu_items, self._menu_index)

    def _draw_mode(self, p):
        """Ecran de choix du mode (apres 'Jouer') : Solo / Online."""
        self._mode_rects = self._draw_button_screen(
            p, "Mode de jeu", self._mode_items, self._mode_index)

    def _draw_mp(self, p):
        """Ecran multijoueur (apres 'Online') : creer / rejoindre."""
        self._mp_rects = self._draw_button_screen(
            p, "Multijoueur", self._mp_items, self._mp_index)
        c = self.skin.c
        w, h = self.width(), self.height()
        p.setPen(QColor(c.get("accent", "#3fb950") if self._mp_notice
                        else c.get("muted", "#6e7681")))
        p.setFont(QFont("Consolas", max(8, w // 26)))
        p.drawText(QRectF(w * 0.05, h * 0.85, w * 0.90, h * 0.12),
                   int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                   self._mp_notice or "Partie ouverte à tout le serveur")

    def _draw_mp_browse(self, p):
        """Liste des parties ouvertes (Rejoindre)."""
        c = self.skin.c
        accent = c.get("accent", "#3fb950"); muted = c.get("muted", "#6e7681")
        text = c.get("text", "#c9d1d9"); border = c.get("border", "#30363d")
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))
        p.setPen(QColor(accent))
        ft = QFont("Consolas", max(13, w // 11)); ft.setBold(True); p.setFont(ft)
        p.drawText(QRectF(0, h * 0.06, w, h * 0.12), Qt.AlignCenter, "Rejoindre")
        if not self._mp_list:
            p.setPen(QColor(muted))
            p.setFont(QFont("Consolas", max(9, w // 20)))
            p.drawText(QRectF(w * 0.08, h * 0.32, w * 0.84, h * 0.40),
                       int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap),
                       "Aucune partie ouverte.\n\n[R] rafraîchir")
        else:
            ry = h * 0.22; rh = max(42.0, h * 0.145); gap = rh * 0.22
            for i, lo in enumerate(self._mp_list):
                r = QRectF(w * 0.07, ry + i * (rh + gap), w * 0.86, rh)
                sel = (i == self._mp_list_idx)
                p.setBrush(QColor(accent) if sel else QColor(0, 0, 0, 150))
                p.setPen(Qt.NoPen if sel else QPen(QColor(border), 1.5))
                p.drawRoundedRect(r, rh * 0.20, rh * 0.20)
                avail = r.width() - 20

                def _fit(txt, base_pt, min_pt, bold):
                    pt = base_pt
                    f = QFont("Consolas", pt); f.setBold(bold); p.setFont(f)
                    while pt > min_pt and \
                            p.fontMetrics().horizontalAdvance(txt) > avail:
                        pt -= 1
                        f = QFont("Consolas", pt); f.setBold(bold)
                        p.setFont(f)
                    if p.fontMetrics().horizontalAdvance(txt) > avail:
                        return p.fontMetrics().elidedText(
                            txt, Qt.ElideRight, int(avail))
                    return txt

                # Titre adaptatif : pseudo long lisible en entier.
                p.setPen(QColor("#0d1117") if sel else QColor(text))
                title = _fit("Partie de %s" % lo.get("host", "?"),
                             max(10, w // 16), 9, True)
                p.drawText(QRectF(r.x() + 10, r.y(), avail, rh * 0.58),
                           int(Qt.AlignLeft | Qt.AlignVCenter), title)
                p.setPen(QColor("#0d1117") if sel else QColor(muted))
                sub = _fit("%s/%s joueurs" % (lo.get("count", 1),
                                              lo.get("max", 2)),
                           max(8, w // 24), 8, False)
                p.drawText(QRectF(r.x() + 10, r.y() + rh * 0.52,
                                  avail, rh * 0.44),
                           int(Qt.AlignLeft | Qt.AlignVCenter), sub)
        p.setPen(QColor(muted))
        p.setFont(QFont("Consolas", max(7, w // 28)))
        p.drawText(QRectF(0, h * 0.89, w, h * 0.06), Qt.AlignCenter,
                   "Haut/Bas · [Entrée] rejoindre · Retour : annuler")

    def _draw_mp_lobby(self, p):
        """Salon d'attente : roster + Pret. Lancement auto quand tous prets."""
        c = self.skin.c
        accent = c.get("accent", "#3fb950"); muted = c.get("muted", "#6e7681")
        text = c.get("text", "#c9d1d9"); border = c.get("border", "#30363d")
        gold = c.get("cursor", "#d4a72c")
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))
        p.setPen(QColor(gold))
        ft = QFont("Consolas", max(13, w // 11)); ft.setBold(True); p.setFont(ft)
        p.drawText(QRectF(0, h * 0.05, w, h * 0.11), Qt.AlignCenter, "Salon")
        my_name = getattr(self.services, "my_name", None) or "Toi"
        roster = self._mp_roster or []
        nr = sum(1 for m in roster if m.get("ready"))
        p.setPen(QColor(muted)); p.setFont(QFont("Consolas", max(8, w // 24)))
        p.drawText(QRectF(0, h * 0.16, w, h * 0.05), Qt.AlignCenter,
                   ("%d joueur(s) · %d prêt(s)" % (len(roster), nr))
                   if roster else "Connexion au salon…")
        ry = h * 0.25; rh = max(30.0, h * 0.11); gap = rh * 0.22
        fn = QFont("Consolas", max(9, w // 18)); fn.setBold(True)
        ftag = QFont("Consolas", max(8, w // 26))
        for i, mmb in enumerate(roster[:4]):
            r = QRectF(w * 0.07, ry + i * (rh + gap), w * 0.86, rh)
            ready = bool(mmb.get("ready"))
            p.setBrush(QColor(0, 0, 0, 150))
            p.setPen(QPen(QColor(accent if ready else border), 2 if ready else 1.4))
            p.drawRoundedRect(r, rh * 0.24, rh * 0.24)
            dot = rh * 0.34
            p.setBrush(QColor(accent if ready else muted)); p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(r.x() + 10, r.y() + (rh - dot) / 2, dot, dot))
            name = mmb.get("name", "?")
            tags = []
            if name == self._mp_host:
                tags.append("hôte")
            if name == my_name:
                tags.append("toi")
            p.setFont(fn); p.setPen(QColor(text))
            p.drawText(QRectF(r.x() + 12 + dot, r.y(), r.width() * 0.6, rh),
                       int(Qt.AlignLeft | Qt.AlignVCenter),
                       name + (("  (%s)" % ", ".join(tags)) if tags else ""))
            p.setFont(ftag); p.setPen(QColor(accent if ready else muted))
            p.drawText(QRectF(r.x(), r.y(), r.width() - 12, rh),
                       int(Qt.AlignRight | Qt.AlignVCenter),
                       "PRÊT" if ready else "en attente")
        if self._mp_notice:
            p.setPen(QColor(gold)); p.setFont(QFont("Consolas", max(8, w // 26)))
            p.drawText(QRectF(w * 0.05, h * 0.76, w * 0.90, h * 0.07),
                       int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                       self._mp_notice)
        me = next((m for m in roster if m.get("name") == my_name), None)
        ir = bool(me and me.get("ready"))
        p.setPen(QColor(text)); fc = QFont("Consolas", max(9, w // 18))
        fc.setBold(True); p.setFont(fc)
        p.drawText(QRectF(0, h * 0.84, w, h * 0.07), Qt.AlignCenter,
                   "[Entrée] pas prêt" if ir else "[Entrée] je suis prêt")
        p.setPen(QColor(muted)); p.setFont(QFont("Consolas", max(7, w // 30)))
        p.drawText(QRectF(0, h * 0.92, w, h * 0.05), Qt.AlignCenter,
                   "Lancement auto quand tous prêts · Retour : quitter")

    def _draw_rules(self, p):
        """Ecran Regles : par-dessus le decor du jeu, titre + explication."""
        c = self.skin.c
        accent = c.get("accent", "#3fb950")
        muted = c.get("muted", "#6e7681")
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))

        p.setPen(QColor(accent))
        ft = QFont("Consolas", max(14, w // 10))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.08, w, h * 0.16),
                   Qt.AlignCenter, "Règles")

        p.setPen(QColor("#e8e8e8"))
        fr = QFont("Consolas", max(9, w // 20))
        p.setFont(fr)
        txt = ("Déploie ta flotte sur ta grille (R pour pivoter), puis vise "
               "la grille ennemie avec les flèches et tire pour détruire tous "
               "ses vaisseaux avant qu'elle ne détruise les tiens.")
        p.drawText(QRectF(w * 0.10, h * 0.28, w * 0.80, h * 0.50),
                   int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap), txt)

        p.setPen(QColor(muted))
        fh = QFont("Consolas", max(8, w // 26))
        p.setFont(fh)
        p.drawText(QRectF(0, h * 0.87, w, h * 0.10),
                   Qt.AlignCenter, "Échap ou clic : retour")

    def _draw_overlay(self, p, title, subtitle):
        c = self.skin.c
        p.fillRect(self.rect(), QColor(c["overlay"]))
        p.setPen(QColor(c["cursor"]))
        ft = QFont("Consolas", max(13, self.width() // 11))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(self.rect().adjusted(0, -self.height() // 8, 0, 0),
                   Qt.AlignCenter, title)
        p.setPen(QColor(c["text"]))
        fsub = QFont("Consolas", max(8, self.width() // 26))
        p.setFont(fsub)
        p.drawText(self.rect().adjusted(0, self.height() // 7, 0, 0),
                   Qt.AlignCenter, subtitle)

class _Harness(QWidget):
    """HARNAIS DE TEST VISUEL (supprimable) : châssis CircusPhone qui monte
    la SolVsTerraApp. Géométrie adaptative identique au client."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("CircusPhone — Sol VS Terra (harnais)")

        screen = QGuiApplication.primaryScreen()
        try:
            scr_h = screen.availableGeometry().height()
        except Exception:
            scr_h = 1080
        body_h = max(420, min(760, int(scr_h * 0.62)))
        body_w = int(body_h * (200.0 / 440.0))
        sx, sy = body_w / 200.0, body_h / 440.0
        self._body_w, self._body_h = body_w, body_h
        self._radius     = int(28 * sx)
        self._banner_h   = int(56 * sy)
        self._screen_x   = int(12 * sx)
        self._screen_y   = self._banner_h
        self._screen_w   = body_w - 2 * self._screen_x
        self._screen_h   = body_h - self._banner_h - int(16 * sy)
        self._screen_rad = int(14 * sx)
        self.setFixedSize(body_w, body_h)
        self.setFocusPolicy(Qt.StrongFocus)

        self.game = SolVsTerraApp(self._screen_w, self._screen_h,
                                  self._screen_rad, PhoneServices(), parent=self)
        self.game.move(self._screen_x, self._screen_y)
        self._drag_offset = None

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                # Comme dans le client : en jeu/regles -> retour menu ; au menu
                # -> on ferme (equivalent du retour au home du telephone).
                if not self.game.handle_back():
                    self.game.on_hide()
                    self.close()
            else:
                self.game.keyPressEvent(event)
            return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = (
                e.globalPosition().toPoint() - self.frameGeometry().topLeft())
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_offset = None

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self._body_w, self._body_h
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_PHONE_BODY_COLOR))
        p.drawRoundedRect(0, 0, w, h, self._radius, self._radius)
        p.setBrush(QColor(_PHONE_BTN_COLOR))
        bw = max(2, int(w * 0.015))
        p.drawRoundedRect(0, int(h * 0.25), bw, int(h * 0.05), 1, 1)
        p.drawRoundedRect(0, int(h * 0.33), bw, int(h * 0.09), 1, 1)
        p.drawRoundedRect(0, int(h * 0.44), bw, int(h * 0.09), 1, 1)
        p.drawRoundedRect(w - bw, int(h * 0.36), bw, int(h * 0.14), 1, 1)
        cx = w / 2
        cy = self._banner_h / 2 + int(self._banner_h * 0.08)
        f_small = QFont("sans-serif")
        f_small.setPixelSize(max(10, int(self._banner_h * 0.26)))
        f_small.setWeight(QFont.Medium)
        f_big = QFont("sans-serif")
        f_big.setPixelSize(max(14, int(self._banner_h * 0.40)))
        f_big.setWeight(QFont.DemiBold)
        fm_s, fm_b = QFontMetrics(f_small), QFontMetrics(f_big)
        wc = fm_s.horizontalAdvance("Circus")
        wp = fm_b.horizontalAdvance("Phone")
        x0 = cx - (wc + wp) / 2
        baseline = cy + fm_b.ascent() / 2
        p.setFont(f_small)
        p.setPen(QColor(_PHONE_BANNER_GREY))
        p.drawText(int(x0), int(baseline), "Circus")
        p.setFont(f_big)
        p.setPen(QColor(_PHONE_BANNER_WHITE))
        p.drawText(int(x0 + wc), int(baseline), "Phone")
        p.end()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = _Harness()
    win.show()
    sys.exit(app.exec())
