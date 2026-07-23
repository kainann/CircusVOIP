# -*- coding: utf-8 -*-
"""
circusvoip_phone_billard
========================

Application « Billard » du CircusPhone (v0.3), au contrat `PhoneApp`.
Mini-jeu de billard (table vue de dessus, viser / doser / tirer) jouable au
clavier.

Jeu : capture clavier brute (CAPTURES_KEYBOARD=True) et boucle physique
continue (timer 16 ms). on_hide arrête la simulation ; on_show la RELANCE si
les billes étaient en mouvement (sinon, cacher pendant un tir figerait la
table jusqu'au prochain coup).

    Gauche/Droite   viser (rotation de la canne)
    Haut/Bas        doser la puissance
    Espace / Entrée tirer
    R               replacer les billes

Note : le prototype d'origine était un « billard caché » qui n'apparaissait
qu'à proximité d'un billard en jeu (classe `ProximityGate`, conservée plus
bas mais NON utilisée par l'app intégrée — une app du téléphone s'ouvre
depuis le menu, pas par proximité). La gate pourra resservir si on veut un
jour ce déclenchement contextuel.

Dépendance : circusvoip_phone_apps (PhoneApp/PhoneServices). Le bloc
__main__ est un HARNAIS DE TEST VISUEL supprimable.
"""

from __future__ import annotations

import math
import sys

from PySide6.QtCore import Qt, QTimer, QRectF, QEvent, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QKeyEvent,
    QGuiApplication, QPainterPath, QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QWidget

from circusvoip_phone_apps import PhoneApp, PhoneServices
# [build 61] Icone vectorielle (fin des emoji : rendu identique sur tous
# les PC). Import defensif : un vieux circusvoip_phone_apps sans fabrique
# ne doit pas faire tomber l'app du registre -> repli glyphe.
try:
    from circusvoip_phone_apps import LazyPhoneIcon as _LazyPhoneIcon
except Exception:
    _LazyPhoneIcon = None

try:
    import circusvoip_phone_mp as mp
except Exception:
    mp = None


# ======================================================================
# Palette
# ======================================================================
_BG          = "#0d1117"
_PANEL       = "#161b22"
_BORDER      = "#30363d"
_TEXT        = "#c9d1d9"
_MUTED       = "#6e7681"
_ACCENT      = "#3fb950"
_GOLD        = "#d4a72c"
_CHIP        = "#58a6ff"
_RED         = "#f85149"

_FELT        = "#0f5132"   # tapis vert billard
_FELT_LIGHT  = "#147a47"
_RAIL        = "#5a3a1a"   # bandes bois
_RAIL_LIGHT  = "#7a5026"
_POCKET      = "#0a0a0a"

_PHONE_BODY_COLOR   = "#1a1a1a"
_PHONE_BTN_COLOR    = "#0a0a0a"
_PHONE_BANNER_GREY  = "#888888"
_PHONE_BANNER_WHITE = "#ffffff"


# ======================================================================
# Porte de proximité — branchera l'OCR de CircusVoIP plus tard
# ======================================================================
class ProximityGate:
    """Décide si le jeu de billard doit être visible, selon la proximité du
    joueur à un billard connu.

    En production : CircusVoIP fournit la position du joueur (via son OCR)
    et la liste des billards. On appelle update_position() à chaque lecture
    OCR ; is_near() reflète alors la proximité.

    En test : always_open=True force is_near() à True (jeu toujours visible).

    Le format des positions/billards reprend celui de circusvoip_sc_ocr :
        {"zone": str, "x": float, "y": float, "z": float}
    La distance est euclidienne 3D, comme scocr.distance().
    """

    def __init__(self, tables=None, radius=8.0, always_open=False,
                 on_change=None):
        self.tables = list(tables or [])     # liste de billards connus
        self.radius = float(radius)          # mètres : seuil de proximité
        self.always_open = bool(always_open)
        self._near = bool(always_open)
        self._on_change = on_change          # callback(bool) au changement
        self._last_pos = None

    @staticmethod
    def _distance(a, b):
        return math.sqrt((a["x"] - b["x"]) ** 2
                         + (a["y"] - b["y"]) ** 2
                         + (a["z"] - b["z"]) ** 2)

    def nearest(self, pos):
        """Retourne (table, distance) du billard le plus proche, ou (None, inf).
        Si une zone est renseignée des deux côtés, elle doit correspondre."""
        best, best_d = None, float("inf")
        for t in self.tables:
            if t.get("zone") and pos.get("zone") and t["zone"] != pos["zone"]:
                continue
            d = self._distance(pos, t)
            if d < best_d:
                best, best_d = t, d
        return best, best_d

    def update_position(self, pos):
        """À appeler depuis le callback OCR de CircusVoIP. Met à jour l'état
        de proximité et déclenche on_change si l'état bascule."""
        self._last_pos = pos
        if self.always_open:
            return self._near
        _, d = self.nearest(pos)
        near = d <= self.radius
        if near != self._near:
            self._near = near
            if self._on_change:
                self._on_change(near)
        return self._near

    def set_always_open(self, value):
        self.always_open = bool(value)
        new = True if value else self._near
        if new != self._near:
            self._near = new
            if self._on_change:
                self._on_change(new)

    def is_near(self):
        return self._near


# ----------------------------------------------------------------------
#  Visibilite de l'app : l'icone Billard n'apparait dans le telephone que
#  lorsqu'on est PRES d'un vrai billard en jeu. On matche la position OCR
#  du joueur sur une liste de tables connues : MEME zone (Pyro : zones
#  distinctes, coords parfois identiques) OU, a defaut de zone discriminante
#  (Stanton : tout est 'ObjectContainer_Commercial'), MEME zone + coords
#  proches. Rayon genereux (taille de salle) car la position OCR est celle
#  du joueur, pas pile la table.
# ----------------------------------------------------------------------
_TABLE_RADIUS = 4.0   # rayon en m. Sur Stanton la zone est generique
                      # (ObjectContainer_Commercial) et les coords sont
                      # LOCALES, donc un rayon trop large declencherait l'icone
                      # dans des couloirs d'autres stations.

BILLIARD_TABLES = [
    {"name": "Ruin Station",  "zone": "rs_int_p6leo_ruinstation", "x": 5,   "y": 91,  "z": 8},
    {"name": "Orbituary",     "zone": "rs_int_p3leo",  "x": 47,  "y": 63,  "z": -4},
    {"name": "StarLight",     "zone": "rs_int_p3l1",   "x": 47,  "y": 63,  "z": -4},
    {"name": "Checkmate",     "zone": "rs_int_p2l4",   "x": 47,  "y": 63,  "z": -4},
    {"name": "PatchCity",     "zone": "rs_int_p2l3",   "x": 47,  "y": 63,  "z": -4},
    {"name": "Nyx Gateway",   "zone": "rs_comm_pyro-nyx_jp",  "x": -18, "y": -62, "z": 8},
    {"name": "Pyro Gateway",  "zone": "rs_comm_nyx-pyro_jp1", "x": -18, "y": -62, "z": 8},
    {"name": "Stanton-Magnus Gateway", "zone": "rs_comm_stan-magnus_jp1", "x": -18, "y": -62, "z": 8},
    {"name": "Port Tressler", "zone": "ObjectContainer_Commercial", "x": 20,  "y": 45,  "z": -5},
    {"name": "MIC L1",        "zone": "ObjectContainer_Commercial", "x": 30,  "y": -50, "z": 7},
    {"name": "Everus Harbor", "zone": "ObjectContainer_Commercial", "x": 15,  "y": 14,  "z": 2},
    {"name": "Lorville",      "zone": "lorville_l19_int", "x": 106, "y": 134, "z": 30},
    {"name": "HUR L5",        "zone": "ObjectContainer_Commercial", "x": 58,  "y": -21, "z": 1},
    {"name": "CRU L5",        "zone": "ObjectContainer_Commercial", "x": 43.05, "y": 23.03, "z": 4.15},
    # --- Vaisseaux ---
    # Anvil Carrack : releve Kainan 02/07/2026 ("ANVL_Carrack_<cid>" ->
    # zone canonique OCR "anvl_carrack"). Les coords sont LOCALES au
    # vaisseau : une seule entree couvre donc TOUS les Carracks (amenagement
    # interieur identique), meme en vol. Le cid varie par vaisseau mais
    # n'est pas utilise ici (zone + coords suffisent).
    {"name": "Carrack",       "zone": "anvl_carrack", "x": 6.71, "y": 19.27, "z": -5.56},
]


_DBG_LAST = [0.0]


def _debug_billiard_gap(services, pos):
    """Quand on est dans la ZONE d'un billard connu mais hors rayon, logue la
    position courante et la distance a la table (throttle 3 s) -> sert a
    calibrer les coords. Silencieux si la zone n'est celle d'aucun billard."""
    import time
    z = pos.get("zone")
    cands = [t for t in BILLIARD_TABLES if t.get("zone") == z]
    if not cands:
        return
    try:
        px, py, pz = float(pos["x"]), float(pos["y"]), float(pos["z"])
    except (KeyError, TypeError, ValueError):
        return
    best = min(cands, key=lambda t: (px - t["x"]) ** 2 + (py - t["y"]) ** 2
               + (pz - t["z"]) ** 2)
    d = ((px - best["x"]) ** 2 + (py - best["y"]) ** 2
         + (pz - best["z"]) ** 2) ** 0.5
    now = time.time()
    if now - _DBG_LAST[0] < 3.0:
        return
    _DBG_LAST[0] = now
    log = getattr(services, "log", None)
    if callable(log):
        try:
            log("[BILLARD] zone=%s pos=(%.0f,%.0f,%.0f) -> %s a %.1f m "
                "(rayon %.1f m)" % (z, px, py, pz, best["name"], d,
                                    _TABLE_RADIUS))
        except Exception:
            pass


def _near_billiard(pos) -> bool:
    """True si la position OCR du joueur est a portee (<= _TABLE_RADIUS) d'un
    billard connu, ET dans la meme zone que lui. Coords requises pour TOUTES les
    tables (une zone comme 'rs_int_p6leo_ruinstation' couvre toute la station,
    donc la zone seule ne suffit pas a localiser le billard)."""
    if not isinstance(pos, dict):
        return False
    z = pos.get("zone")
    try:
        px, py, pz = float(pos["x"]), float(pos["y"]), float(pos["z"])
    except (KeyError, TypeError, ValueError):
        return False
    for t in BILLIARD_TABLES:
        tz = t.get("zone")
        if tz is not None and z is not None and tz != z:
            continue
        dx, dy, dz = px - t["x"], py - t["y"], pz - t["z"]
        if (dx * dx + dy * dy + dz * dz) ** 0.5 <= _TABLE_RADIUS:
            return True
    return False


# ======================================================================
# Jeu de billard (maquette) — vue de dessus
# ======================================================================
class _Ball:
    __slots__ = ("x", "y", "vx", "vy", "color", "potted", "kind")

    def __init__(self, x, y, color, kind="obj"):
        self.x, self.y = x, y
        self.vx, self.vy = 0.0, 0.0
        self.color = color
        self.potted = False
        self.kind = kind    # "cue" | "yellow" | "red" | "black"


class BilliardApp(PhoneApp):
    """Billard du CircusPhone : table vue de dessus, viser/doser/tirer.
    Jeu a boucle physique continue ; on_hide arrete la simulation, on_show
    la relance si les billes etaient en mouvement."""

    APP_ID = "billard"
    APP_NAME = "Billard"
    APP_ICON = (_LazyPhoneIcon("billard", "\U0001F3B1")
                if _LazyPhoneIcon is not None else "\U0001F3B1")
    CAPTURES_KEYBOARD = True         # jeu : l'overlay route les touches brutes

    BALL_R = 0.018          # rayon bille en fraction de la largeur de table
    FRICTION = 0.985
    STOP_EPS = 0.0008

    @staticmethod
    def is_available(services) -> bool:
        """Visibilite de l'icone : l'app n'apparait QUE si le joueur est PRES
        d'un billard en jeu (position OCR sur une table connue). Tant que la
        position n'est pas connue (attente OCR / pas connecte), on CACHE l'app :
        on ne peut pas confirmer qu'on est a un billard. Seul cas degrade en
        visible : aucun fournisseur de position (vieux client sans la feature)."""
        prov = getattr(services, "pos_provider", None)
        if not callable(prov):
            return True            # pas de systeme de position -> compat
        try:
            pos = prov()
        except Exception:
            return False           # erreur -> on ne peut pas confirmer -> cache
        if not pos:
            return False           # position pas encore connue (attente OCR)
        near = _near_billiard(pos)
        if not near:
            _debug_billiard_gap(services, pos)
        return near

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.resize(int(screen_w), int(screen_h))
        self.setFocusPolicy(Qt.StrongFocus)
        # Attribut lu par paintEvent pour clipper aux coins arrondis.
        self._screen_radius = int(screen_radius)

        # Coordonnées du jeu en repère normalisé : table dans [0..1]x[0..2]
        # (table 2x plus haute que large, façon vue portrait du téléphone).
        self._aim = -math.pi / 2     # direction de visée (vers le haut)
        self._power = 0.5            # 0..1
        self._balls = []
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._tick)
        # Pause : retient si la simulation tournait quand on a cache l'app.
        self._paused = False
        self._anim_was_active = False
        self._reset_balls()

        # Menu d'accueil + ecran multijoueur. Le billard est multijoueur
        # UNIQUEMENT (pas de solo) : "Jouer" mene directement a creer /
        # rejoindre une partie. menu | mp | rules | playing.
        self._menu_items = ["Jouer", "Règles", "Quitter"]
        self._menu_index = 0
        self._menu_rects = []
        self._mp_items = ["Créer une partie", "Rejoindre une partie"]
        self._mp_index = 0
        self._mp_rects = []
        # --- Multijoueur (proximite, comme le Poker : joueurs a <=30 m). ---
        self._mp = None
        self._mp_list = []
        self._mp_list_idx = 0
        self._mp_roster = []
        self._mp_host = None
        self._mp_notice = ""
        # --- Partie reseau (physique deterministe -> lockstep ; le tireur
        #     fait autorite sur l'etat final pour corriger toute derive). ---
        self._is_net = False
        self._net_seat = None
        self._net_turn = 0          # siege dont c'est le tour (0 commence)
        self._net_score = [0, 0]    # billes empochees par siege
        self._net_over = False
        # BLACKBALL : couleur de chaque siege ("yellow"/"red", None = table
        # ouverte), gagnant (siege) et cause de fin. Le tireur fait autorite.
        self._net_colors = [None, None]
        self._net_winner = None
        self._net_end_why = ""
        self._net_pots_before = {}  # {kind: nb empoche} avant le tir courant
        # menu | mp | mp_browse | mp_lobby | rules | playing
        self._state = "menu"

    # --- Cycle de vie (contrat PhoneApp) ------------------------------
    def on_show(self):
        """Devient l'ecran courant : on revient TOUJOURS au menu (ouvrir le
        jeu depuis la liste d'apps doit afficher son menu). Stoppe la
        simulation et prend le focus clavier."""
        self._paused = False
        self._anim.stop()
        self._anim_was_active = False
        if self._mp is not None and getattr(self._mp, "lobby_id", None):
            try:
                self._mp.leave()
            except Exception:
                pass
        self._is_net = False
        self._net_over = False
        self._mp_roster = []
        self._mp_host = None
        self._mp_notice = ""
        self._state = "menu"
        self._menu_index = 0
        self.setFocus()
        self.update()

    def handle_back(self) -> bool:
        """Retour (Echap) : Regles/Multijoueur -> menu ; partie -> menu ;
        menu -> non consomme (l'overlay revient au home)."""
        if self._state in ("rules", "mp"):
            self._state = "menu"
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
        if self._state == "playing":
            self._anim.stop()
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

    def _menu_activate(self):
        """Active le bouton selectionne du menu d'accueil. Pas de solo : Jouer
        mene directement a l'ecran creer/rejoindre."""
        idx = self._menu_index
        if idx == 0:            # Jouer -> creer / rejoindre (multijoueur)
            self._mp_index = 0
            self._state = "mp"
            self.update()
        elif idx == 1:          # Règles
            self._state = "rules"
            self.update()
        elif idx == 2:          # Quitter -> home du telephone
            try:
                self.sig_request_home.emit()
            except Exception:
                pass

    def _mp_get(self):
        """Session multijoueur de proximite (joueurs a <=30 m)."""
        if mp is None:
            return None
        if self._mp is not None:
            return self._mp
        send_ws = getattr(self.services, "send_ws", None)
        my_name = getattr(self.services, "my_name", None) or "Toi"
        self._mp = mp.MpLobby("billard", mp.SCOPE_PROXIMITY, send_ws, my_name, self)
        self._mp.sig_list.connect(self._on_mp_list)
        self._mp.sig_lobby.connect(self._on_mp_lobby)
        self._mp.sig_started.connect(self._on_mp_started)
        self._mp.sig_game.connect(self._on_mp_game)
        self._mp.sig_error.connect(self._on_mp_error)
        self._mp.sig_closed.connect(self._on_mp_closed)
        return self._mp

    def _mp_activate(self):
        """Creer / rejoindre une partie de proximite (joueurs au meme billard).
        La proximite est validee cote serveur (meme lieu + rayon serre)."""
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

    def handle_server_msg(self, data) -> bool:
        if self._mp is None:
            return False
        try:
            return bool(self._mp.handle_server_msg(data))
        except Exception:
            return False

    # --- Signaux MpLobby ----------------------------------------------
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
        order = payload.get("order") or []
        my_name = getattr(self.services, "my_name", None)
        try:
            self._net_seat = order.index(my_name) if my_name else 0
        except ValueError:
            self._net_seat = 0
        self._is_net = True
        self._net_turn = 0            # siege 0 commence
        self._net_score = [0, 0]
        self._net_over = False
        self._net_colors = [None, None]
        self._net_winner = None
        self._net_end_why = ""
        self._reset_balls()           # rateau fixe -> identique chez tous
        self._state = "playing"
        self.update()

    def _on_mp_error(self, code, msg):
        self._mp_notice = msg or code
        self.update()

    def _on_mp_closed(self, reason):
        self._mp_notice = ("Partie fermée : l'adversaire est parti."
                           if reason == "host_left" else "Partie fermée.")
        self._mp_roster = []
        self._mp_host = None
        self._is_net = False
        self._anim.stop()
        self._state = "mp"
        self.update()

    # --- Helpers reseau ------------------------------------------------
    def _object_potted(self):
        return sum(1 for b in self._balls
                   if b is not self._cue and b.potted)

    def _net_my_turn(self):
        return self._is_net and self._net_turn == self._net_seat \
            and not self._net_over

    def _net_shoot(self):
        """Tir local en reseau : relaie le coup (visee+puissance) puis simule.
        On memorise le nombre de billes deja empochees pour, a l'arret,
        decider si on garde la main."""
        if not self._net_my_turn() or self._balls_moving():
            return
        # Snapshot AVANT tir, par type, pour categoriser ce qui tombe.
        self._net_pots_before = {
            k: sum(1 for b in self._balls if b.kind == k and b.potted)
            for k in ("yellow", "red", "black")
        }
        if self._mp is not None:
            try:
                self._mp.send_game({"k": "shoot", "aim": self._aim,
                                    "power": self._power})
            except Exception:
                pass
        self.shoot()

    def _on_mp_game(self, ev):
        """Coup distant relaye. 'shoot' = l'adversaire tire (on rejoue la meme
        physique) ; 'rest' = etat final FAISANT AUTORITE renvoye par le tireur
        (on s'y aligne pour annuler toute derive flottante)."""
        if not self._is_net:
            return
        payload = (ev or {}).get("payload") or {}
        k = payload.get("k")
        if k == "shoot":
            self._aim = float(payload.get("aim", self._aim))
            self._power = float(payload.get("power", self._power))
            self._net_pots_before = self._object_potted()
            self.shoot()             # rejoue le tir adverse (animation locale)
        elif k == "rest":
            balls = payload.get("balls") or []
            for b, st in zip(self._balls, balls):
                b.x, b.y = float(st[0]), float(st[1])
                b.potted = bool(st[2])
                b.vx = b.vy = 0.0
            self._anim.stop()
            self._net_turn = int(payload.get("turn", self._net_turn))
            sc = payload.get("score")
            if isinstance(sc, list) and len(sc) == 2:
                self._net_score = [int(sc[0]), int(sc[1])]
            co = payload.get("colors")
            if isinstance(co, list) and len(co) == 2:
                self._net_colors = [co[0], co[1]]
            self._net_winner = payload.get("winner", self._net_winner)
            self._net_end_why = payload.get("why", self._net_end_why) or ""
            self._net_over = bool(payload.get("over"))
            self.update()

    def _net_resolve_rest(self):
        """A l'arret des billes, cote TIREUR : applique les REGLES BLACKBALL
        (7 jaunes / 7 rouges / noire) puis diffuse l'etat final qui fait
        autorite.
          - Table ouverte : la 1re couleur (majoritaire) empochee sur un coup
            SANS faute attribue cette couleur au tireur, l'autre a l'adversaire.
          - Empocher >=1 boule de SA couleur (sans faute) : on REJOUE.
          - Blanche empochee : FAUTE -> blanche replacee, main a l'adversaire
            (les boules tombees restent tombees, pas d'attribution).
          - Noire empochee : VICTOIRE si toutes ses couleurs etaient rentrees
            et pas de blanche dans le meme coup ; sinon DEFAITE immediate."""
        shooter = self._net_turn
        before = self._net_pots_before or {}
        pot_y = (sum(1 for b in self._balls if b.kind == "yellow" and b.potted)
                 - before.get("yellow", 0))
        pot_r = (sum(1 for b in self._balls if b.kind == "red" and b.potted)
                 - before.get("red", 0))
        black_pot = any(b.kind == "black" and b.potted for b in self._balls)
        cue_pot = self._cue.potted
        mycol = self._net_colors[shooter]

        if black_pot:
            # Fin de partie immediate, gagnee ou perdue.
            self._net_over = True
            legit = (mycol is not None
                     and self._remaining(mycol) == 0
                     and not cue_pot)
            if legit:
                self._net_winner = shooter
                self._net_end_why = "noire empochée"
            else:
                self._net_winner = 1 - shooter
                self._net_end_why = ("noire + blanche" if cue_pot
                                     else "noire trop tôt")
        elif cue_pot:
            # Faute : blanche replacee, main a l'adversaire.
            self._cue.potted = False
            self._cue.x, self._cue.y = 0.5, 1.5
            self._cue.vx = self._cue.vy = 0.0
            self._net_turn = 1 - shooter
        else:
            if mycol is None and (pot_y or pot_r):
                # Attribution des couleurs (table ouverte).
                mycol = "yellow" if pot_y >= pot_r else "red"
                self._net_colors[shooter] = mycol
                self._net_colors[1 - shooter] = ("red" if mycol == "yellow"
                                                 else "yellow")
            mine_potted = pot_y if mycol == "yellow" else (
                pot_r if mycol == "red" else 0)
            if mine_potted <= 0:
                self._net_turn = 1 - shooter   # rien de sa couleur : main passee
        # Score affiche = boules empochees de SA couleur (7 = pret pour la noire).
        for seat in (0, 1):
            c = self._net_colors[seat]
            self._net_score[seat] = (7 - self._remaining(c)) if c else 0

        if self._mp is not None:
            try:
                self._mp.send_game({
                    "k": "rest",
                    "balls": [[b.x, b.y, b.potted] for b in self._balls],
                    "turn": self._net_turn,
                    "score": list(self._net_score),
                    "colors": list(self._net_colors),
                    "winner": self._net_winner,
                    "why": self._net_end_why,
                    "over": self._net_over,
                })
            except Exception:
                pass
        self.update()

    def on_hide(self):
        """Quitte l'ecran : fige la table en arretant la boucle physique.
        Idempotent (overlay + hideEvent)."""
        if self._paused:
            return
        self._paused = True
        self._anim_was_active = self._anim.isActive()
        self._anim.stop()

    # ---- mise en place ----
    def _reset_balls(self):
        self._anim.stop()
        self._balls = []
        # bille blanche en bas
        cue = _Ball(0.5, 1.5, "#f2efe6", "cue")
        self._balls.append(cue)
        self._cue = cue
        # Triangle BLACKBALL (billard anglais) : 7 JAUNES + 7 ROUGES + la
        # NOIRE au centre de la 3e rangee. Ratelier FIXE (identique chez les
        # deux joueurs -> lockstep deterministe, et l'index de chaque boule
        # suffit a connaitre son type cote receveur).
        Y, R, K = "yellow", "red", "black"
        pattern = [
            [R],
            [Y, R],
            [R, K, Y],
            [Y, R, Y, R],
            [R, Y, R, Y, Y],
        ]
        cols = {"yellow": "#f2c744", "red": "#f85149", "black": "#101317"}
        apex_x, apex_y = 0.5, 0.55
        spacing = self.BALL_R * 2.05
        for row, kinds in enumerate(pattern):
            for col, kind in enumerate(kinds):
                bx = apex_x + (col - row / 2.0) * spacing
                by = apex_y - row * spacing * 0.9
                self._balls.append(_Ball(bx, by, cols[kind], kind))
        self._aim = -math.pi / 2
        self._power = 0.5
        self.update()

    def _remaining(self, kind):
        """Boules de ce type encore sur la table."""
        return sum(1 for b in self._balls if b.kind == kind and not b.potted)

    def _balls_moving(self):
        return any(abs(b.vx) > self.STOP_EPS or abs(b.vy) > self.STOP_EPS
                   for b in self._balls if not b.potted)

    # ---- tir ----
    def _effective_velocity(self, aim, speed):
        """Vitesse réellement appliquée : si la blanche est collée à une bande
        et qu'on vise dans cette bande, on annule la composante entrante (la
        bille glisse le long du mur au lieu de rebondir aussitôt)."""
        r = self.BALL_R
        vx = math.cos(aim) * speed
        vy = math.sin(aim) * speed
        if self._cue.x <= r + 1e-6 and vx < 0:      # bande gauche
            vx = 0.0
        elif self._cue.x >= 1.0 - r - 1e-6 and vx > 0:   # bande droite
            vx = 0.0
        if self._cue.y <= r + 1e-6 and vy < 0:      # bande haute
            vy = 0.0
        elif self._cue.y >= 2.0 - r - 1e-6 and vy > 0:   # bande basse
            vy = 0.0
        return vx, vy

    def shoot(self):
        if self._balls_moving() or self._cue.potted:
            return
        speed = 0.004 + self._power * 0.05
        vx, vy = self._effective_velocity(self._aim, speed)
        if vx == 0.0 and vy == 0.0:
            return                          # visée pile dans le mur : pas de tir
        self._cue.vx = vx
        self._cue.vy = vy
        self._anim.start()

    def _tick(self):
        r = self.BALL_R
        W, H = 1.0, 2.0
        for b in self._balls:
            if b.potted:
                continue
            b.x += b.vx
            b.y += b.vy
            b.vx *= self.FRICTION
            b.vy *= self.FRICTION
            # rebonds sur les bandes
            if b.x < r:
                b.x = r; b.vx = -b.vx
            elif b.x > W - r:
                b.x = W - r; b.vx = -b.vx
            if b.y < r:
                b.y = r; b.vy = -b.vy
            elif b.y > H - r:
                b.y = H - r; b.vy = -b.vy
            if abs(b.vx) < self.STOP_EPS:
                b.vx = 0.0
            if abs(b.vy) < self.STOP_EPS:
                b.vy = 0.0
        self._collide()
        self._check_pockets()
        if not self._balls_moving():
            self._anim.stop()
            if self._cue.potted:
                # remet la blanche en jeu à un emplacement LIBRE (évite tout
                # chevauchement figé avec une autre bille).
                self._cue.potted = False
                self._cue.vx = self._cue.vy = 0.0
                self._cue.x, self._cue.y = self._free_spot(0.5, 1.5)
            if self._is_net and not self._net_over \
                    and self._net_turn == self._net_seat:
                # Cote tireur uniquement : on tranche la main et on diffuse
                # l'etat final (autorite). Le receveur s'alignera dessus.
                self._net_resolve_rest()
        self.update()

    def _free_spot(self, x, y):
        """Trouve une position proche de (x, y), dans les limites et sans
        chevauchement avec une autre bille. Cherche en spirale croissante."""
        r = self.BALL_R
        lo, hi = r * 1.05, 1.0 - r * 1.05          # bornes en X (largeur 1)
        loy, hiy = r * 1.05, 2.0 - r * 1.05        # bornes en Y (hauteur 2)

        def clamp(px, py):
            return (min(hi, max(lo, px)), min(hiy, max(loy, py)))

        def is_free(px, py):
            for b in self._balls:
                if b is self._cue or b.potted:
                    continue
                if math.hypot(b.x - px, b.y - py) < 2 * r * 1.05:
                    return False
            return True

        px, py = clamp(x, y)
        if is_free(px, py):
            return px, py
        step = 2 * r * 1.1
        for ring in range(1, 40):
            for ang in range(0, 360, 30):
                a = math.radians(ang)
                cx, cy = clamp(x + math.cos(a) * step * ring,
                               y + math.sin(a) * step * ring)
                if is_free(cx, cy):
                    return cx, cy
        return px, py            # rien trouvé (très improbable) : au moins dans les bornes

    def _collide(self):
        r = self.BALL_R
        live = [b for b in self._balls if not b.potted]
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                a, b = live[i], live[j]
                dx, dy = b.x - a.x, b.y - a.y
                dist = math.hypot(dx, dy)
                if dist > 0 and dist < 2 * r:
                    nx, ny = dx / dist, dy / dist
                    # séparation
                    overlap = 2 * r - dist
                    a.x -= nx * overlap / 2; a.y -= ny * overlap / 2
                    b.x += nx * overlap / 2; b.y += ny * overlap / 2
                    # échange des composantes normales (collision élastique 1D)
                    avn = a.vx * nx + a.vy * ny
                    bvn = b.vx * nx + b.vy * ny
                    da = bvn - avn
                    a.vx += da * nx; a.vy += da * ny
                    b.vx -= da * nx; b.vy -= da * ny

    def _check_pockets(self):
        # 6 poches : 4 coins + 2 milieux des grands côtés
        pockets = [(0, 0), (1, 0), (0, 2), (1, 2), (0, 1), (1, 1)]
        pr = self.BALL_R * 1.6
        for b in self._balls:
            if b.potted:
                continue
            for (px, py) in pockets:
                if math.hypot(b.x - px, b.y - py) < pr:
                    b.potted = True
                    b.vx = b.vy = 0.0
                    break

    # ---- entrées ----
    def keyPressEvent(self, event: QKeyEvent):
        k = event.key()
        confirm = (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter)

        # Menu d'accueil : navigation verticale + validation (gardee contre
        # la touche d'ouverture qui fuit).
        if self._state == "menu":
            if k in (Qt.Key_Up,):
                self._menu_index = (self._menu_index - 1) % len(self._menu_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._menu_index = (self._menu_index + 1) % len(self._menu_items)
                self.update()
            elif k in confirm:
                if self._confirm_armed:
                    self._menu_activate()
            return
        # Ecran multijoueur : creer / rejoindre.
        if self._state == "mp":
            if k in (Qt.Key_Up,):
                self._mp_index = (self._mp_index - 1) % len(self._mp_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._mp_index = (self._mp_index + 1) % len(self._mp_items)
                self.update()
            elif k in confirm:
                self._mp_activate()
            elif k == Qt.Key_Backspace:
                self._state = "menu"
                self.update()
            return
        if self._state == "mp_browse":
            n = len(self._mp_list)
            if k in (Qt.Key_Up,) and n:
                self._mp_list_idx = (self._mp_list_idx - 1) % n; self.update()
            elif k in (Qt.Key_Down,) and n:
                self._mp_list_idx = (self._mp_list_idx + 1) % n; self.update()
            elif k == Qt.Key_R:
                self._mp_notice = ""
                if self._mp:
                    self._mp.refresh_list()
            elif k in confirm and n:
                lid = self._mp_list[self._mp_list_idx].get("lobby_id")
                if lid and self._mp:
                    self._mp_notice = ""
                    self._mp.join(lid)
            return
        if self._state == "mp_lobby":
            if k in confirm:
                if self._mp:
                    self._mp.toggle_ready()
            return
        # Ecran Regles : toute validation revient au menu.
        if self._state == "rules":
            if k in confirm or k == Qt.Key_Backspace:
                self._state = "menu"
                self.update()
            return

        # --- En partie ---
        # En reseau : on ne peut viser/tirer que pendant SON tour ; R (reset)
        # est desactive (casserait la synchro).
        if self._is_net and not self._net_my_turn():
            return
        if k in (Qt.Key_Left,):
            self._aim -= 0.04
            self.update()
        elif k in (Qt.Key_Right,):
            self._aim += 0.04
            self.update()
        elif k in (Qt.Key_Up,):
            self._power = min(1.0, self._power + 0.05)
            self.update()
        elif k in (Qt.Key_Down,):
            self._power = max(0.05, self._power - 0.05)
            self.update()
        elif k in confirm:
            if self._confirm_armed:
                if self._is_net:
                    self._net_shoot()
                else:
                    self.shoot()
        elif k == Qt.Key_R:
            if not self._is_net:
                self._reset_balls()

    def mousePressEvent(self, event):
        """Clic souris sur les ecrans a boutons (menu / multijoueur), ou
        retour depuis les Regles. En partie : non gere (jeu au clavier)."""
        if event.button() != Qt.LeftButton:
            return
        if self._state == "rules":
            self._state = "menu"
            self.update()
            return
        if self._state == "menu":
            pos = event.position()
            for i, r in enumerate(self._menu_rects):
                if r.contains(pos):
                    self._menu_index = i
                    self.update()
                    self._menu_activate()
                    return
            return
        if self._state == "mp":
            pos = event.position()
            for i, r in enumerate(self._mp_rects):
                if r.contains(pos):
                    self._mp_index = i
                    self.update()
                    self._mp_activate()
                    return
            return

    def hideEvent(self, event):
        # Filet de securite : pause via on_hide si l'app est cachee sans
        # passer par l'overlay (ex. un appel entrant bascule l'ecran).
        self.on_hide()
        super().hideEvent(event)

    # ---- rendu ----
    def _table_rect(self):
        w, h = self.width(), self.height()
        margin = w * 0.06
        tw = w - 2 * margin
        th = tw * 2.0
        if th > h - 2 * margin:
            th = h - 2 * margin
            tw = th / 2.0
        tx = (w - tw) / 2
        ty = (h - th) / 2
        return QRectF(tx, ty, tw, th)

    def _draw_button_screen(self, p, title, items, sel_index):
        """Ecran a boutons verticaux (titre + liste) sur fond sombre. Retourne
        les QRectF des boutons (pour la souris). Le fond _BG est deja peint."""
        w, h = self.width(), self.height()
        p.setPen(QColor(_ACCENT))
        ft = QFont("Consolas", max(15, w // 9))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.08, w, h * 0.22),
                   int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                   title)
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
                p.setBrush(QColor(_ACCENT))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor(_BG))
            else:
                p.setBrush(QColor(_PANEL))
                p.setPen(QPen(QColor(_BORDER), 1.5))
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor(_TEXT))
            p.setFont(fb)
            p.drawText(r, Qt.AlignCenter, label)
        return rects

    def _draw_menu(self, p):
        """Menu d'accueil : 'Billard' + 4 boutons."""
        self._menu_rects = self._draw_button_screen(
            p, "Billard", self._menu_items, self._menu_index)

    def _draw_mp(self, p):
        """Ecran multijoueur (apres 'Jouer') : creer / rejoindre une partie."""
        self._mp_rects = self._draw_button_screen(
            p, "Multijoueur", self._mp_items, self._mp_index)
        w, h = self.width(), self.height()
        p.setPen(QColor(_GOLD if self._mp_notice else _MUTED))
        p.setFont(QFont("Consolas", max(8, w // 26)))
        p.drawText(QRectF(w * 0.05, h * 0.85, w * 0.90, h * 0.12),
                   int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                   self._mp_notice or "Joueurs au même billard que toi")

    def _draw_mp_browse(self, p):
        w, h = self.width(), self.height()
        p.setPen(QColor(_ACCENT))
        ft = QFont("Consolas", max(14, w // 10)); ft.setBold(True); p.setFont(ft)
        p.drawText(QRectF(0, h * 0.06, w, h * 0.12), Qt.AlignCenter, "Rejoindre")
        if not self._mp_list:
            p.setPen(QColor(_MUTED)); p.setFont(QFont("Consolas", max(9, w // 20)))
            p.drawText(QRectF(w * 0.08, h * 0.32, w * 0.84, h * 0.40),
                       int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap),
                       "Aucune partie à proximité.\n\n[R] rafraîchir")
        else:
            ry = h * 0.22; rh = max(42.0, h * 0.145); gap = rh * 0.22
            for i, lo in enumerate(self._mp_list):
                r = QRectF(w * 0.07, ry + i * (rh + gap), w * 0.86, rh)
                sel = (i == self._mp_list_idx)
                p.setBrush(QColor(_ACCENT) if sel else QColor(_PANEL))
                p.setPen(Qt.NoPen if sel else QPen(QColor(_BORDER), 1.5))
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
                p.setPen(QColor(_BG) if sel else QColor(_TEXT))
                title = _fit("Partie de %s" % lo.get("host", "?"),
                             max(10, w // 16), 9, True)
                p.drawText(QRectF(r.x() + 10, r.y(), avail, rh * 0.58),
                           int(Qt.AlignLeft | Qt.AlignVCenter), title)
                p.setPen(QColor(_BG) if sel else QColor(_MUTED))
                sub = _fit("%s/%s joueurs" % (lo.get("count", 1),
                                              lo.get("max", 2)),
                           max(8, w // 24), 8, False)
                p.drawText(QRectF(r.x() + 10, r.y() + rh * 0.52,
                                  avail, rh * 0.44),
                           int(Qt.AlignLeft | Qt.AlignVCenter), sub)
        p.setPen(QColor(_MUTED)); p.setFont(QFont("Consolas", max(7, w // 28)))
        p.drawText(QRectF(0, h * 0.89, w, h * 0.06), Qt.AlignCenter,
                   "Haut/Bas · [Espace] rejoindre · Retour : annuler")

    def _draw_mp_lobby(self, p):
        w, h = self.width(), self.height()
        my_name = getattr(self.services, "my_name", None) or "Toi"
        p.setPen(QColor(_GOLD))
        ft = QFont("Consolas", max(14, w // 10)); ft.setBold(True); p.setFont(ft)
        p.drawText(QRectF(0, h * 0.05, w, h * 0.11), Qt.AlignCenter, "Salon")
        roster = self._mp_roster or []
        nr = sum(1 for m in roster if m.get("ready"))
        p.setPen(QColor(_MUTED)); p.setFont(QFont("Consolas", max(8, w // 24)))
        p.drawText(QRectF(0, h * 0.16, w, h * 0.05), Qt.AlignCenter,
                   ("%d joueur(s) · %d prêt(s)" % (len(roster), nr))
                   if roster else "Connexion au salon…")
        ry = h * 0.25; rh = max(30.0, h * 0.11); gap = rh * 0.22
        fn = QFont("Consolas", max(9, w // 18)); fn.setBold(True)
        ftag = QFont("Consolas", max(8, w // 26))
        for i, mmb in enumerate(roster[:2]):
            r = QRectF(w * 0.07, ry + i * (rh + gap), w * 0.86, rh)
            ready = bool(mmb.get("ready"))
            p.setBrush(QColor(_PANEL))
            p.setPen(QPen(QColor(_ACCENT if ready else _BORDER), 2 if ready else 1.4))
            p.drawRoundedRect(r, rh * 0.24, rh * 0.24)
            dot = rh * 0.34
            p.setBrush(QColor(_ACCENT if ready else _MUTED)); p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(r.x() + 10, r.y() + (rh - dot) / 2, dot, dot))
            name = mmb.get("name", "?")
            tags = []
            if name == self._mp_host:
                tags.append("hôte")
            if name == my_name:
                tags.append("toi")
            p.setFont(fn); p.setPen(QColor(_TEXT))
            p.drawText(QRectF(r.x() + 12 + dot, r.y(), r.width() * 0.6, rh),
                       int(Qt.AlignLeft | Qt.AlignVCenter),
                       name + (("  (%s)" % ", ".join(tags)) if tags else ""))
            p.setFont(ftag); p.setPen(QColor(_ACCENT if ready else _MUTED))
            p.drawText(QRectF(r.x(), r.y(), r.width() - 12, rh),
                       int(Qt.AlignRight | Qt.AlignVCenter),
                       "PRÊT" if ready else "en attente")
        me = next((m for m in roster if m.get("name") == my_name), None)
        ir = bool(me and me.get("ready"))
        p.setPen(QColor(_TEXT)); fc = QFont("Consolas", max(9, w // 18))
        fc.setBold(True); p.setFont(fc)
        p.drawText(QRectF(0, h * 0.84, w, h * 0.07), Qt.AlignCenter,
                   "[Espace] pas prêt" if ir else "[Espace] je suis prêt")
        p.setPen(QColor(_MUTED)); p.setFont(QFont("Consolas", max(7, w // 30)))
        p.drawText(QRectF(0, h * 0.92, w, h * 0.05), Qt.AlignCenter,
                   "Lancement auto quand tous prêts · Retour : quitter")

    def _draw_rules(self, p):
        """Ecran Regles : billard a deux couleurs de boules (style 8-ball)."""
        w, h = self.width(), self.height()
        p.setPen(QColor(_ACCENT))
        ft = QFont("Consolas", max(14, w // 10))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.07, w, h * 0.14),
                   Qt.AlignCenter, "Règles")
        p.setPen(QColor(_TEXT))
        fr = QFont("Consolas", max(9, w // 22))
        p.setFont(fr)
        txt = ("Chaque joueur joue une couleur (jaunes ou rouges) : la "
               "première empochée est la tienne. Empoche tes 7 boules puis "
               "la NOIRE pour gagner (trop tôt = défaite !). Blanche "
               "empochée : faute, main à l'adversaire. Gauche/Droite pour "
               "viser, Haut/Bas pour doser, Espace pour tirer.")
        p.drawText(QRectF(w * 0.08, h * 0.24, w * 0.84, h * 0.56),
                   int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap), txt)
        p.setPen(QColor(_MUTED))
        fh = QFont("Consolas", max(8, w // 26))
        p.setFont(fh)
        p.drawText(QRectF(0, h * 0.87, w, h * 0.10),
                   Qt.AlignCenter, "Échap ou clic : retour")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._screen_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()),
                                self._screen_radius, self._screen_radius)
            p.setClipPath(path)
        p.fillRect(self.rect(), QColor(_BG))

        if self._state in ("menu", "mp", "rules", "mp_browse", "mp_lobby"):
            if self._state == "menu":
                self._draw_menu(p)
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

        tr = self._table_rect()
        # bandes (cadre bois)
        rail = tr.adjusted(-tr.width() * 0.06, -tr.width() * 0.06,
                           tr.width() * 0.06, tr.width() * 0.06)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_RAIL))
        p.drawRoundedRect(rail, tr.width() * 0.06, tr.width() * 0.06)
        p.setBrush(QColor(_RAIL_LIGHT))
        p.drawRoundedRect(rail.adjusted(2, 2, -2, -2),
                          tr.width() * 0.05, tr.width() * 0.05)
        # tapis
        p.setBrush(QColor(_FELT))
        p.drawRoundedRect(tr, tr.width() * 0.02, tr.width() * 0.02)

        def to_px(bx, by):
            return (tr.x() + bx * tr.width(), tr.y() + by / 2.0 * tr.height())

        # poches
        pockets = [(0, 0), (1, 0), (0, 2), (1, 2), (0, 1), (1, 1)]
        prad = self.BALL_R * 1.6 * tr.width()
        p.setBrush(QColor(_POCKET))
        p.setPen(Qt.NoPen)
        for (px, py) in pockets:
            cx, cy = to_px(px, py)
            p.drawEllipse(QPointF(cx, cy), prad, prad)

        # billes
        br = self.BALL_R * tr.width()
        for b in self._balls:
            if b.potted:
                continue
            cx, cy = to_px(b.x, b.y)
            grad = QRadialGradient(cx - br * 0.3, cy - br * 0.3, br * 1.3)
            grad.setColorAt(0, QColor(b.color).lighter(135))
            grad.setColorAt(1, QColor(b.color))
            p.setBrush(QBrush(grad))
            # La NOIRE recoit un lisere clair pour rester lisible sur le tapis
            # sombre (et ne pas se confondre avec les poches).
            if b.kind == "black":
                p.setPen(QPen(QColor(200, 205, 212, 170), 1.4))
            else:
                p.setPen(QPen(QColor(0, 0, 0, 70), 1))
            p.drawEllipse(QPointF(cx, cy), br, br)

        # ligne de visée + jauge de puissance (si rien ne bouge)
        if not self._balls_moving() and not self._cue.potted:
            cx, cy = to_px(self._cue.x, self._cue.y)
            length = tr.width() * (0.25 + self._power * 0.55)
            # direction EFFECTIVE (annule la composante entrant dans une bande)
            evx, evy = self._effective_velocity(self._aim, 1.0)
            blocked = (evx == 0.0 and evy == 0.0)
            if blocked:
                dx, dy = math.cos(self._aim), math.sin(self._aim)   # indicatif
            else:
                norm = math.hypot(evx, evy)
                dx, dy = evx / norm, evy / norm
            ex = cx + dx * length
            ey = cy + dy * length
            col = QColor(_RED) if blocked else QColor(255, 255, 255, 180)
            pen = QPen(col, 2, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(cx, cy), QPointF(ex, ey))
            p.setBrush(QColor(_RED) if blocked else QColor(_GOLD))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(ex, ey), 3, 3)

        # HUD reseau : a qui de jouer + couleurs blackball + fin de partie.
        if self._is_net:
            w, h = self.width(), self.height()
            me, opp = self._net_seat, 1 - self._net_seat
            _fr = {"yellow": "jaunes", "red": "rouges"}
            if self._net_over:
                win = (self._net_winner == me)
                msg = "VICTOIRE" if win else "DÉFAITE"
                if self._net_end_why:
                    msg += f" — {self._net_end_why}"
                col = _ACCENT if win else _RED
            elif self._net_my_turn():
                mc = self._net_colors[me]
                msg = "À toi de jouer"
                if mc:
                    msg += f" ({_fr[mc]})"
                elif True:
                    msg += " (table ouverte)"
                col = _ACCENT
            else:
                msg, col = "Tour de l'adversaire", _MUTED
            p.setPen(QColor(col))
            p.setFont(QFont("Consolas", max(9, w // 22)))
            p.drawText(QRectF(0, h * 0.012, w, h * 0.045), Qt.AlignCenter, msg)
            p.setPen(QColor(_MUTED))
            p.setFont(QFont("Consolas", max(7, w // 28)))
            mc, oc = self._net_colors[me], self._net_colors[opp]
            if mc and oc:
                sub = (f"toi ({_fr[mc]}) : {self._remaining(mc)} rest."
                       f"  ·  adv : {self._remaining(oc)} rest.")
                if self._remaining(mc) == 0 and not self._net_over:
                    sub = "vise la NOIRE !  ·  " + sub.split("·")[1].strip()
            else:
                sub = "table ouverte : première couleur empochée = la tienne"
            p.drawText(QRectF(0, h * 0.058, w, h * 0.03), Qt.AlignCenter, sub)

        # HUD puissance
        self._draw_power(p, tr)
        p.end()

    def _draw_power(self, p, tr):
        w = self.width()
        bx = tr.x()
        by = tr.bottom() + tr.width() * 0.10
        bw = tr.width()
        bh = max(6.0, w * 0.025)
        if by + bh > self.height():
            by = self.height() - bh - 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_BORDER))
        p.drawRoundedRect(QRectF(bx, by, bw, bh), bh / 2, bh / 2)
        col = _ACCENT if self._power < 0.6 else (_GOLD if self._power < 0.85 else _RED)
        p.setBrush(QColor(col))
        p.drawRoundedRect(QRectF(bx, by, bw * self._power, bh), bh / 2, bh / 2)
        p.setPen(QColor(_MUTED))
        f = QFont("Consolas", max(7, int(w * 0.030)))
        p.setFont(f)
        p.drawText(QRectF(bx, by - bh * 2.2, bw, bh * 2),
                   Qt.AlignLeft | Qt.AlignVCenter, "puissance")


# ======================================================================
# Châssis CircusPhone (avec porte de proximité)
# ======================================================================
class _Harness(QWidget):
    """HARNAIS DE TEST VISUEL (supprimable) : châssis CircusPhone qui monte
    la BilliardApp (sans la gate de proximité, sans objet dans le menu)."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("CircusPhone — Billard (harnais)")
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
        self.game = BilliardApp(self._screen_w, self._screen_h,
                                self._screen_rad, PhoneServices(), parent=self)
        self.game.move(self._screen_x, self._screen_y)
        self._drag_offset = None
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                # Comme dans le client : en jeu/menu interne -> retour ; au
                # menu -> on ferme (retour au home du telephone).
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
