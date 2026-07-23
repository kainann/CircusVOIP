# -*- coding: utf-8 -*-
"""
circusvoip_phone_valakkar
=========================

Application « Valakkar » du CircusPhone (v0.3), au contrat `PhoneApp`.
Valakkar = le ver des sables de Pyro (ex-prototype « Snake »). Jeu à grille
adaptative, rendu par SKINS de sprites avec repli couleurs.

Skin par défaut « dune » (Star Citizen) :
  - ver      = Valakkar (tête + segments annelés, rotés selon la direction)
  - proie    = véhicule Tumbril (détouré du fond noir)
  - fond     = surface de la planète MonoX

Premiere app JEU portée : elle valide le chemin clavier des jeux
(CAPTURES_KEYBOARD=True : l'overlay route les touches brutes vers
keyPressEvent) et le cycle de vie (on_hide met le QTimer en pause pour que
le jeu ne tourne pas en fond).

Touches :
    Flèches / WASD / ZQSD    se déplacer
    Entrée          démarrer / rejouer
    P                        pause
    V                        changer de skin (dune <-> classic)

Les sprites sont dans le dossier  skins/<nom>/  à côté de ce script :
    worm_head.png  worm_body.png  food_vehicle.png  background.jpg
Si un fichier manque, le skin retombe proprement sur le rendu couleurs.

Dépendance : circusvoip_phone_apps (PhoneApp/PhoneServices). Le bloc
__main__ est un HARNAIS DE TEST VISUEL supprimable (monte la ValakkarApp
dans un châssis CircusPhone, glisser pour déplacer, Échap pour quitter).
"""

from __future__ import annotations

import os
import random
import sys
from collections import deque

from PySide6.QtCore import Qt, QTimer, QRectF, QEvent
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont, QFontMetrics, QKeyEvent,
    QGuiApplication, QPainterPath, QPixmap,
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


import datetime
try:
    import circusvoip_core as _core
    _CORE_OK = True
except Exception:                       # pragma: no cover
    _core = None
    _CORE_OK = False


# ======================================================================
# Palette (rendu « classic » de repli)
# ======================================================================
_BG          = "#0d1117"
_GRID        = "#161b22"
_BORDER      = "#30363d"
_SNAKE_HEAD  = "#3fb950"
_SNAKE_BODY  = "#2ea043"
_FOOD        = "#f85149"
_TEXT_MUTED  = "#6e7681"
_ACCENT      = "#58a6ff"
_OVERLAY_BG  = "#0d1117cc"

_PHONE_BODY_COLOR   = "#1a1a1a"
_PHONE_BTN_COLOR    = "#0a0a0a"
_PHONE_BANNER_GREY  = "#888888"
_PHONE_BANNER_WHITE = "#ffffff"

# ======================================================================
# Paramètres de jeu
# ======================================================================
_CELL_TARGET_PX    = 18
_START_INTERVAL_MS = 160
_MIN_INTERVAL_MS    = 70
_SPEEDUP_PER_FOOD  = 6

_DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

_SKIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skins")


def _load_pix(folder, name):
    """Charge un QPixmap depuis skins/<folder>/<name>, ou None si absent."""
    path = os.path.join(_SKIN_DIR, folder, name)
    if os.path.exists(path):
        pm = QPixmap(path)
        if not pm.isNull():
            return pm
    return None


class Skin:
    """Conteneur de sprites pour un skin. Les pixmaps None déclenchent le
    repli couleurs pour l'élément concerné."""

    def __init__(self, name, folder=None):
        self.name = name
        if folder:
            self.head = _load_pix(folder, "worm_head.png")
            self.body = _load_pix(folder, "worm_body.png")
            self.food = _load_pix(folder, "food_vehicle.png")
            self.background = _load_pix(folder, "background.jpg")
        else:
            self.head = self.body = self.food = self.background = None

    @property
    def textured(self):
        return self.background is not None or self.body is not None


# Tête « dune » : dans la source le museau pointe vers la GAUCHE. On rote
# en conséquence (degrés horaires, repère écran y vers le bas).
_HEAD_ANGLE = {"left": 0, "up": 90, "right": 180, "down": 270}
# Corps : axe horizontal dans la source -> 0° à l'horizontale, 90° vertical.
_BODY_ANGLE = {"left": 0, "right": 0, "up": 90, "down": 90}


class ValakkarApp(PhoneApp):
    """Valakkar — le ver des sables de Pyro (ex-« Snake »). Jeu autonome a
    grille adaptative, rendu par skin (sprites) avec repli couleurs.
    Premiere app JEU du CircusPhone : capture clavier brute
    (CAPTURES_KEYBOARD=True) et mise en pause a la sortie d'ecran (on_hide)
    pour ne pas laisser le QTimer tourner en fond."""

    APP_ID = "valakkar"
    APP_NAME = "Valakkar"
    APP_ICON = (_LazyPhoneIcon("valakkar", "\U0001F40D")
                if _LazyPhoneIcon is not None else "\U0001F40D")
    CAPTURES_KEYBOARD = True         # jeu : l'overlay route les touches brutes

    def __init__(self, screen_w, screen_h, screen_radius, services,
                 parent=None, cell_target: int = _CELL_TARGET_PX):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.resize(int(screen_w), int(screen_h))
        self.setFocusPolicy(Qt.StrongFocus)
        # Attribut lu par paintEvent pour clipper aux coins arrondis de
        # l'ecran (PhoneApp expose self._screen_rad ; on garde ce nom-ci
        # pour ne pas toucher au code de rendu repris du proto).
        self._screen_radius = int(screen_radius)

        self._cols = max(8, int(screen_w) // cell_target)
        self._rows = max(8, int(screen_h) // cell_target)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self._snake: deque = deque()
        self._dir = "right"
        self._next_dir = "right"
        self._food = (0, 0)
        self._score = 0
        self._interval = _START_INTERVAL_MS
        self._state = "title"

        # Menu d'accueil (etat "title") : boutons verticaux.
        self._menu_items = ["Jouer", "Règles", "Tableau des scores", "Quitter"]
        self._menu_index = 0
        self._menu_rects = []   # QRectF par bouton, calcules au paint (souris)

        # Meilleurs scores (persistants) : liste [{"score":int,"date":str}]
        # triee decroissante, max 10. _last_best = la derniere partie a battu
        # le record (affiche sur l'ecran Game Over).
        self._scores = self._load_scores()
        self._last_best = False
        # Classement SERVEUR partage (tous joueurs) : rempli par hs_list.
        #   None = pas encore recu (en attente / hors-ligne), [] = vide.
        self._shared_scores = None

        # Skins disponibles. "dune" (Valakkar / surface de MonoX) si les
        # assets existent, sinon repli "classic".
        self._skins = [Skin("Valakkar", folder="Valakkar"), Skin("classic")]
        self._skin_idx = 0 if self._skins[0].textured else 1
        # Diagnostic (console) : dit exactement ce qui charge ou pas.
        try:
            sk = self._skins[0]
            print(f"[VALAKKAR SKIN] dossier: "
                  f"{os.path.join(_SKIN_DIR, 'Valakkar')}")
            for attr, fname in (("head", "worm_head.png"),
                                ("body", "worm_body.png"),
                                ("food", "food_vehicle.png"),
                                ("background", "background.jpg")):
                pm = getattr(sk, attr, None)
                path = os.path.join(_SKIN_DIR, "Valakkar", fname)
                if pm is not None:
                    print(f"[VALAKKAR SKIN]   {fname}: OK")
                elif not os.path.exists(path):
                    print(f"[VALAKKAR SKIN]   {fname}: INTROUVABLE ({path})")
                else:
                    print(f"[VALAKKAR SKIN]   {fname}: present mais ILLISIBLE")
            print(f"[VALAKKAR SKIN] demarrage sur: "
                  f"{self._skins[self._skin_idx].name}")
        except Exception:
            pass
        self._reset()

    # --- Cycle de vie (contrat PhoneApp) ------------------------------
    def on_show(self):
        """Devient l'ecran courant : on revient TOUJOURS au menu d'accueil
        (ouvrir le jeu depuis la liste d'apps doit afficher son menu, pas
        reprendre un etat precedent). Prend le focus clavier."""
        self._timer.stop()
        self._state = "title"
        self._menu_index = 0
        # Rattrapage : soumet le meilleur score local au serveur (utile si un
        # record a ete fait hors ligne ; sans effet sinon).
        self._hs_sync_local_best()
        self.setFocus()
        self.update()

    def on_hide(self):
        """Quitte l'ecran : met la partie en PAUSE. C'est le point qui
        garantit que le jeu ne tourne pas en fond (sinon le QTimer
        continuerait de ticker meme telephone ferme)."""
        if self._state == "playing":
            self.toggle_pause()

    # --- API publique -------------------------------------------------
    def set_direction(self, name: str):
        if name in _DIRS and self._state == "playing":
            self._next_dir = name

    def start_or_restart(self):
        if self._state in ("title", "over"):
            self._reset()
            self._state = "playing"
            self._interval = _START_INTERVAL_MS
            self._timer.start(self._interval)
            self.update()

    def toggle_pause(self):
        if self._state == "playing":
            self._state = "paused"
            self._timer.stop()
        elif self._state == "paused":
            self._state = "playing"
            self._timer.start(self._interval)
        self.update()

    def cycle_skin(self):
        self._skin_idx = (self._skin_idx + 1) % len(self._skins)
        self.update()

    @property
    def skin(self) -> Skin:
        return self._skins[self._skin_idx]

    @property
    def score(self) -> int:
        return self._score

    # --- Logique ------------------------------------------------------
    def _reset(self):
        cx, cy = self._cols // 2, self._rows // 2
        self._snake = deque([(cx, cy), (cx - 1, cy), (cx - 2, cy)])
        self._dir = "right"
        self._next_dir = "right"
        self._score = 0
        self._spawn_food()

    def _spawn_food(self):
        occupied = set(self._snake)
        free = [(c, r) for c in range(self._cols) for r in range(self._rows)
                if (c, r) not in occupied]
        if not free:
            self._state = "over"
            self._timer.stop()
            return
        self._food = random.choice(free)

    def _tick(self):
        if self._state != "playing":
            return
        if self._next_dir != _OPPOSITE.get(self._dir):
            self._dir = self._next_dir

        dc, dr = _DIRS[self._dir]
        hc, hr = self._snake[0]
        new_head = (hc + dc, hr + dr)

        nc, nr = new_head
        if nc < 0 or nc >= self._cols or nr < 0 or nr >= self._rows:
            self._game_over()
            return

        will_eat = (new_head == self._food)
        body = self._snake if will_eat else list(self._snake)[:-1]
        if new_head in body:
            self._game_over()
            return

        self._snake.appendleft(new_head)
        if will_eat:
            self._score += 1
            self._interval = max(
                _MIN_INTERVAL_MS,
                _START_INTERVAL_MS - self._score * _SPEEDUP_PER_FOOD,
            )
            self._timer.start(self._interval)
            self._spawn_food()
        else:
            self._snake.pop()
        self.update()

    def _game_over(self):
        self._state = "over"
        self._timer.stop()
        self._record_score(self._score)
        self.update()

    # --- Meilleurs scores (persistants) -------------------------------
    def _load_scores(self):
        try:
            if _CORE_OK:
                cfg = _core._load_client_cfg() or {}
                raw = cfg.get("valakkar_scores")
                if isinstance(raw, list):
                    out = [{"score": int(e["score"]),
                            "date": str(e.get("date", ""))}
                           for e in raw
                           if isinstance(e, dict) and "score" in e]
                    out.sort(key=lambda e: e["score"], reverse=True)
                    return out[:10]
        except Exception:
            pass
        return []

    def _save_scores(self):
        try:
            if _CORE_OK:
                cfg = _core._load_client_cfg() or {}
                cfg["valakkar_scores"] = self._scores[:10]
                _core._save_client_cfg(cfg)
        except Exception:
            pass

    def _record_score(self, score):
        """Ajoute le score de la partie au tableau (top 10) et le persiste.
        Met _last_best a True si c'est un nouveau record. Envoie aussi le
        score au SERVEUR (classement partage entre joueurs)."""
        self._last_best = False
        if not score or score <= 0:
            return
        prev_best = self._scores[0]["score"] if self._scores else 0
        self._scores.append({
            "score": int(score),
            "date": datetime.datetime.now().strftime("%d/%m/%y"),
        })
        self._scores.sort(key=lambda e: e["score"], reverse=True)
        self._scores = self._scores[:10]
        self._last_best = (int(score) > prev_best)
        self._save_scores()
        self._hs_send({"type": "hs_submit", "game": "valakkar",
                       "score": int(score)})

    def _hs_send(self, msg) -> bool:
        """Envoie un message hs_* au serveur (best-effort, silencieux si
        hors-ligne ou services absents)."""
        try:
            fn = getattr(self.services, "send_ws", None)
            if callable(fn):
                return bool(fn(msg))
        except Exception:
            pass
        return False

    def _hs_request(self):
        """Demande le classement partage au serveur (reponse : hs_list)."""
        self._hs_send({"type": "hs_get", "game": "valakkar"})

    def _hs_sync_local_best(self):
        """Soumet le MEILLEUR score local au serveur. Rattrape les records
        faits HORS LIGNE : le serveur ne garde que le meilleur par joueur,
        donc re-soumettre est sans risque (ignore si pas mieux). Appele a
        l'ouverture de l'app et de l'ecran des scores (si connecte)."""
        try:
            if self._scores:
                best = int(self._scores[0].get("score", 0))
                if best > 0:
                    self._hs_send({"type": "hs_submit", "game": "valakkar",
                                   "score": best})
        except Exception:
            pass

    def handle_server_msg(self, data) -> bool:
        """Recoit les messages serveur routes par l'overlay (hs_*)."""
        try:
            if (isinstance(data, dict) and data.get("type") == "hs_list"
                    and data.get("game") == "valakkar"):
                raw = data.get("scores") or []
                self._shared_scores = [
                    {"name": str(e.get("name", "?")),
                     "score": int(e.get("score", 0)),
                     "date": str(e.get("date", ""))}
                    for e in raw if isinstance(e, dict)]
                if self._state == "scores":
                    self.update()
                return True
        except Exception:
            pass
        return False

    # --- Clavier ------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        # Etat "rules"/"scores" = ecrans d'info : toute validation revient au menu.
        if self._state in ("rules", "scores"):
            if key in (Qt.Key_Return, Qt.Key_Enter,
                       Qt.Key_Backspace, Qt.Key_Escape):
                self._state = "title"
                self.update()
            else:
                super().keyPressEvent(event)
            return
        # Etat "title" = menu d'accueil : navigation verticale + validation.
        if self._state == "title":
            if key in (Qt.Key_Up, Qt.Key_Z, Qt.Key_W):
                self._menu_index = (self._menu_index - 1) % len(self._menu_items)
                self.update()
            elif key in (Qt.Key_Down, Qt.Key_S):
                self._menu_index = (self._menu_index + 1) % len(self._menu_items)
                self.update()
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                if self._confirm_armed:
                    self._menu_activate()
            else:
                super().keyPressEvent(event)
            return
        if key in (Qt.Key_Up, Qt.Key_Z, Qt.Key_W):
            self.set_direction("up")
        elif key in (Qt.Key_Down, Qt.Key_S):
            self.set_direction("down")
        elif key in (Qt.Key_Left, Qt.Key_Q, Qt.Key_A):
            self.set_direction("left")
        elif key in (Qt.Key_Right, Qt.Key_D):
            self.set_direction("right")
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.start_or_restart()
        elif key == Qt.Key_P:
            self.toggle_pause()
        elif key == Qt.Key_V:
            # V et non C : C = "s'allonger" chez certains joueurs SC.
            self.cycle_skin()
        else:
            super().keyPressEvent(event)

    def _menu_activate(self):
        """Active le bouton selectionne du menu d'accueil."""
        idx = self._menu_index
        if idx == 0:            # Jouer
            self.start_or_restart()
        elif idx == 1:          # Règles -> ecran d'explications
            self._state = "rules"
            self.update()
        elif idx == 2:          # Tableau des scores
            self._state = "scores"
            self._hs_sync_local_best()   # rattrape un record fait hors ligne
            self._hs_request()  # classement partage (reponse asynchrone)
            self.update()
        elif idx == 3:          # Quitter -> revient au home du telephone
            try:
                self.sig_request_home.emit()
            except Exception:
                pass

    def handle_back(self) -> bool:
        """Retour (Echap) : ecran Regles -> menu ; partie (playing/paused/over)
        -> menu ; menu (title) -> non consomme (l'overlay revient au home)."""
        if self._state in ("rules", "scores"):
            self._state = "title"
            self.update()
            return True
        if self._state in ("playing", "paused", "over"):
            self._timer.stop()
            self._state = "title"
            self._menu_index = 0
            self.update()
            return True
        return False

    def mousePressEvent(self, event):
        """Clic souris : sur un bouton du menu, ou retour depuis les Regles."""
        if self._state in ("rules", "scores"):
            self._state = "title"
            self.update()
            return
        if self._state == "title":
            pos = event.position()
            for i, r in enumerate(self._menu_rects):
                if r.contains(pos):
                    self._menu_index = i
                    self.update()
                    self._menu_activate()
                    return
        super().mousePressEvent(event)

    def hideEvent(self, event):
        if self._state == "playing":
            self.toggle_pause()
        super().hideEvent(event)

    # --- Rendu --------------------------------------------------------
    def _seg_dir(self, i):
        """Direction (vecteur) d'un segment vers la tête, pour l'orienter."""
        if i == 0:
            return _DIRS[self._dir]
        ax, ay = self._snake[i - 1]
        bx, by = self._snake[i]
        return (ax - bx, ay - by)

    @staticmethod
    def _vec_to_dir(v):
        for name, vec in _DIRS.items():
            if vec == v:
                return name
        return "right"

    def _draw_sprite(self, p, pix, cx, cy, size, angle):
        """Dessine un pixmap centré sur (cx,cy), mis à l'échelle pour tenir
        dans size×size en gardant le ratio, roté de 'angle' degrés."""
        p.save()
        p.translate(cx, cy)
        p.rotate(angle)
        pw, ph = pix.width(), pix.height()
        scale = size / max(pw, ph)
        w, h = pw * scale, ph * scale
        p.drawPixmap(QRectF(-w / 2, -h / 2, w, h), pix, QRectF(pix.rect()))
        p.restore()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        if self._screen_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()),
                                self._screen_radius, self._screen_radius)
            p.setClipPath(path)

        skin = self.skin
        w, h = self.width(), self.height()
        cell = min(w // self._cols, h // self._rows)
        board_w, board_h = cell * self._cols, cell * self._rows
        ox = (w - board_w) // 2
        oy = (h - board_h) // 2

        # --- Fond ---
        if skin.background is not None:
            # cover : on remplit tout le widget en gardant le ratio
            bg = skin.background.scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap((w - bg.width()) // 2, (h - bg.height()) // 2, bg)
            # voile sombre pour faire ressortir le ver (terrain rougeâtre,
            # couleur proche de celle du ver : on assombrit nettement le sol)
            p.fillRect(self.rect(), QColor(0, 0, 0, 150))
        else:
            p.fillRect(self.rect(), QColor(_BG))
            p.setPen(QPen(QColor(_GRID), 1))
            for c in range(1, self._cols):
                p.drawLine(ox + c * cell, oy, ox + c * cell, oy + board_h)
            for r in range(1, self._rows):
                p.drawLine(ox, oy + r * cell, ox + board_w, oy + r * cell)
            p.setPen(QPen(QColor(_BORDER), 2))
            p.drawRect(ox, oy, board_w, board_h)

        def center(cell_pos):
            c, r = cell_pos
            return ox + c * cell + cell / 2, oy + r * cell + cell / 2

        # --- Elements de jeu (nourriture / serpent / score) ---
        # En etat "title"/"rules" (menu/regles), on ne dessine QUE le fond du
        # terrain : pas de serpent/nourriture/score, pour un ecran propre.
        if self._state not in ("title", "rules"):
            # --- Nourriture ---
            if skin.food is not None:
                cxp, cyp = center(self._food)
                self._draw_sprite(p, skin.food, cxp, cyp, cell * 1.7, 0)
            else:
                self._draw_cell(p, self._food, _FOOD, ox, oy, cell, cell * 0.18)

            # --- Serpent ---
            if skin.body is not None or skin.head is not None:
                n = len(self._snake)
                # Ombre par segment, qui RETRECIT vers la queue : des perles
                # distinctes et fuselees (et non un tube lisse uniforme).
                for i, seg in enumerate(self._snake):
                    cxp, cyp = center(seg)
                    t = i / max(1, n - 1)            # 0 tete -> 1 queue
                    rad = cell * (0.44 - 0.16 * t)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(0, 0, 0, 90))
                    p.drawEllipse(QRectF(cxp - rad, cyp - rad, 2 * rad, 2 * rad))
                for i, seg in enumerate(self._snake):
                    cxp, cyp = center(seg)
                    d = self._vec_to_dir(self._seg_dir(i))
                    t = i / max(1, n - 1)
                    if i == 0 and skin.head is not None:
                        # Tete discrete (pas de dome large proeminent).
                        self._draw_sprite(p, skin.head, cxp, cyp,
                                          cell * 1.12, _HEAD_ANGLE[d])
                    elif skin.body is not None:
                        # Corps fusele : large derriere la tete -> fin a la
                        # queue (silhouette de ver, pas de colonne uniforme).
                        scale = 1.10 - 0.55 * t
                        self._draw_sprite(p, skin.body, cxp, cyp,
                                          cell * scale, _BODY_ANGLE[d])
                    else:
                        col = _SNAKE_HEAD if i == 0 else _SNAKE_BODY
                        self._draw_cell(p, seg, col, ox, oy, cell, 1.0)
            else:
                for i, seg in enumerate(self._snake):
                    col = _SNAKE_HEAD if i == 0 else _SNAKE_BODY
                    self._draw_cell(p, seg, col, ox, oy, cell, 1.0)

            # --- Score ---
            p.setPen(QColor("#ffffff") if skin.textured else QColor(_ACCENT))
            f = QFont("Consolas", max(9, cell // 2))
            f.setBold(True)
            p.setFont(f)
            p.drawText(ox + 6, oy + cell + 2, str(self._score))

        # --- Overlays d'état ---
        if self._state == "title":
            self._draw_menu(p)
        elif self._state == "rules":
            self._draw_rules(p)
        elif self._state == "scores":
            self._draw_scores(p)
        elif self._state == "paused":
            self._draw_overlay(p, "Pause", "Entrée ou P : reprendre")
        elif self._state == "over":
            won = len(self._snake) >= self._cols * self._rows
            sub = f"Score : {self._score}"
            if self._last_best and self._score > 0:
                sub += "  ·  Nouveau record !"
            sub += "\nEntrée : rejouer"
            self._draw_overlay(p, "Gagné !" if won else "Game Over", sub)
        p.end()

    def _draw_menu(self, p):
        """Menu d'accueil (etat title) : titre 'Valakkar' en haut, puis les
        3 boutons verticaux (Jouer / Tableau des scores / Quitter). Le bouton
        selectionne est mis en surbrillance. Les rectangles sont memorises
        pour le clic souris."""
        w, h = self.width(), self.height()
        # Voile translucide : on laisse voir le FOND DU JEU (terrain dune /
        # grille deja peint par paintEvent) tout en gardant titre et boutons
        # lisibles. Pas d'aplat opaque.
        p.fillRect(self.rect(), QColor(0, 0, 0, 130))

        # Titre.
        p.setPen(QColor(_SNAKE_HEAD))
        ft = QFont("Consolas", max(16, w // 8))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.08, w, h * 0.24),
                   Qt.AlignCenter, "Valakkar")

        # Boutons.
        self._menu_rects = []
        n = len(self._menu_items)
        bw = w * 0.78
        bh = max(34.0, h * 0.105)
        gap = bh * 0.40
        x = (w - bw) / 2.0
        y0 = h * 0.42
        fb = QFont("Consolas", max(10, w // 17))
        fb.setBold(True)
        rad = bh * 0.28
        for i, label in enumerate(self._menu_items):
            r = QRectF(x, y0 + i * (bh + gap), bw, bh)
            self._menu_rects.append(r)
            selected = (i == self._menu_index)
            if selected:
                p.setBrush(QColor(_SNAKE_HEAD))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor(_BG))
            else:
                p.setBrush(QColor(_GRID))
                p.setPen(QPen(QColor(_BORDER), 1.5))
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor("#d8d8d8"))
            p.setFont(fb)
            p.drawText(r, Qt.AlignCenter, label)

    def _draw_rules(self, p):
        """Ecran Regles : par-dessus le decor du jeu (voile translucide),
        titre + explication, et un rappel pour revenir."""
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))

        # Titre.
        p.setPen(QColor(_SNAKE_HEAD))
        ft = QFont("Consolas", max(15, w // 9))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.08, w, h * 0.18),
                   Qt.AlignCenter, "Règles")

        # Explication (texte enroule).
        p.setPen(QColor("#e8e8e8"))
        fr = QFont("Consolas", max(10, w // 18))
        p.setFont(fr)
        txt = ("Utilise les flèches directionnelles pour contrôler le "
               "Valakkar et l'aider à manger les cyclones.")
        p.drawText(QRectF(w * 0.10, h * 0.30, w * 0.80, h * 0.45),
                   int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap), txt)

        # Rappel retour.
        p.setPen(QColor(_TEXT_MUTED))
        fh = QFont("Consolas", max(8, w // 24))
        p.setFont(fh)
        p.drawText(QRectF(0, h * 0.86, w, h * 0.10),
                   Qt.AlignCenter, "Échap ou clic : retour")

    def _draw_scores(self, p):
        """Ecran Tableau des scores : classement SERVEUR partage entre joueurs
        (rang · pseudo · score). Si le serveur n'a pas (encore) repondu, on
        montre les scores LOCAUX en repli, avec une mention."""
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(0, 0, 0, 165))

        # Titre (adaptatif : reduit la police pour tenir dans la largeur).
        p.setPen(QColor(_SNAKE_HEAD))
        pt = max(13, w // 11)
        ft = QFont("Consolas", pt); ft.setBold(True); p.setFont(ft)
        while pt > 9 and \
                p.fontMetrics().horizontalAdvance("Meilleurs scores") > w * 0.94:
            pt -= 1
            ft = QFont("Consolas", pt); ft.setBold(True); p.setFont(ft)
        p.drawText(QRectF(0, h * 0.06, w, h * 0.16),
                   Qt.AlignCenter, "Meilleurs scores")

        shared = self._shared_scores
        rows, my = [], ""
        try:
            my = str(getattr(self.services, "my_name", "") or "")
        except Exception:
            pass
        if shared:                      # classement serveur (nom + score)
            rows = [(e.get("name", "?"), e.get("score", 0)) for e in shared]
            note = ""
        elif shared is not None:        # serveur a repondu : vide
            rows, note = [], ""
        else:                           # pas (encore) de reponse : repli local
            rows = [(my or "moi", e.get("score", 0)) for e in self._scores]
            note = "hors ligne : scores locaux"

        if not rows:
            p.setPen(QColor(_TEXT_MUTED))
            fr = QFont("Consolas", max(10, w // 17))
            p.setFont(fr)
            p.drawText(QRectF(0, h * 0.40, w, h * 0.20),
                       int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap),
                       "Aucun score pour l'instant.\nJoue une partie !")
        else:
            y = h * 0.24
            rowh = h * 0.060
            fr = QFont("Consolas", max(9, w // 19))
            p.setFont(fr)
            fm = p.fontMetrics()
            for i, (nom, sc) in enumerate(rows[:10]):
                top = (i == 0)
                mine = (my and nom == my)
                # Rang.
                p.setPen(QColor(_ACCENT) if top else QColor("#d8d8d8"))
                p.drawText(QRectF(w * 0.08, y, w * 0.10, rowh),
                           int(Qt.AlignLeft | Qt.AlignVCenter), f"{i + 1}.")
                # Pseudo (le mien en vert), elide si trop long.
                p.setPen(QColor(_SNAKE_HEAD) if mine
                         else (QColor(_ACCENT) if top else QColor("#d8d8d8")))
                nm = fm.elidedText(str(nom), Qt.ElideRight, int(w * 0.48))
                p.drawText(QRectF(w * 0.18, y, w * 0.48, rowh),
                           int(Qt.AlignLeft | Qt.AlignVCenter), nm)
                # Score.
                p.setPen(QColor(_ACCENT) if top else QColor("#d8d8d8"))
                p.drawText(QRectF(w * 0.66, y, w * 0.26, rowh),
                           int(Qt.AlignRight | Qt.AlignVCenter), str(sc))
                y += rowh
            if note:
                p.setPen(QColor(_TEXT_MUTED))
                fn = QFont("Consolas", max(8, w // 26))
                p.setFont(fn)
                p.drawText(QRectF(0, y + rowh * 0.2, w, rowh),
                           Qt.AlignCenter, note)

        # Rappel retour.
        p.setPen(QColor(_TEXT_MUTED))
        fh = QFont("Consolas", max(8, w // 24))
        p.setFont(fh)
        p.drawText(QRectF(0, h * 0.88, w, h * 0.10),
                   Qt.AlignCenter, "Échap ou clic : retour")

    def _draw_cell(self, p, cell_pos, color, ox, oy, cell, inset):
        c, r = cell_pos
        rectf = QRectF(ox + c * cell + inset, oy + r * cell + inset,
                       cell - 2 * inset, cell - 2 * inset)
        radius = max(2.0, cell * 0.22)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(rectf, radius, radius)

    def _draw_overlay(self, p, title, subtitle):
        p.fillRect(self.rect(), QColor(_OVERLAY_BG))
        p.setPen(QColor(_SNAKE_HEAD))
        ft = QFont("Consolas", max(15, self.width() // 9))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(self.rect().adjusted(0, -self.height() // 10, 0, 0),
                   Qt.AlignCenter, title)
        p.setPen(QColor("#d8d8d8"))
        fs = QFont("Consolas", max(8, self.width() // 24))
        p.setFont(fs)
        p.drawText(self.rect().adjusted(0, self.height() // 8, 0, 0),
                   Qt.AlignCenter, subtitle)


class _Harness(QWidget):
    """HARNAIS DE TEST VISUEL (supprimable) : châssis CircusPhone qui monte
    la ValakkarApp, pour l'œilleter hors intégration. Géométrie adaptative
    identique au client."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("CircusPhone — Valakkar (harnais)")

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

        self.game = ValakkarApp(self._screen_w, self._screen_h,
                                self._screen_rad, PhoneServices(), parent=self)
        self.game.move(self._screen_x, self._screen_y)
        self.game.setFocusPolicy(Qt.NoFocus)
        self._drag_offset = None

        # Capture clavier robuste : un filtre au niveau de l'application
        # route TOUTES les touches vers le jeu, peu importe quel widget a
        # le focus interne. Évite le cas "fenêtre frameless qui ne reçoit
        # pas les touches" sous Windows.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                # Comme dans le client : en jeu -> retour au menu ; au menu ->
                # on ferme (equivalent du retour au home du telephone).
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
        w_circus = fm_s.horizontalAdvance("Circus")
        w_phone = fm_b.horizontalAdvance("Phone")
        x0 = cx - (w_circus + w_phone) / 2
        baseline = cy + fm_b.ascent() / 2
        p.setFont(f_small)
        p.setPen(QColor(_PHONE_BANNER_GREY))
        p.drawText(int(x0), int(baseline), "Circus")
        p.setFont(f_big)
        p.setPen(QColor(_PHONE_BANNER_WHITE))
        p.drawText(int(x0 + w_circus), int(baseline), "Phone")
        p.end()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = _Harness()
    win.show()
    sys.exit(app.exec())
