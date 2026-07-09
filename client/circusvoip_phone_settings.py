# -*- coding: utf-8 -*-
"""
circusvoip_phone_settings
=========================

Application « Paramètres » du CircusPhone (v0.3), au contrat `PhoneApp`.
App NON-jeu (CAPTURES_KEYBOARD=False) : l'overlay route le D-pad via
handle_nav. Icone du home : roue dentee (glyphe « ⚙ »).

Deux ecrans (self._state) :
  - "list"      : liste des reglages -> "Photo de profil" / "Fond d'écran".
  - "wallpaper" : GRILLE de fonds. Tuiles, dans l'ordre :
        1) les images du DOSSIER utilisateur (circusvoip_phone_wallpapers/),
        2) quelques fonds proposes, generes par code (degrades),
        3) une tuile « + » pour importer une image perso.
      Le fond actuellement applique porte une petite COCHE. Apres selection
      on RESTE sur la grille.

Formats d'images acceptes pour le dossier : PNG, JPG/JPEG, WEBP, BMP.
Conseil : portrait (l'ecran est vertical), au moins ~600 px de haut.

Actions DELEGUEES a l'overlay (via signaux) :
  - sig_open_profile   -> ecran photo de profil existant.
  - sig_open_wallpaper -> import d'une image perso (tuile « + »).
  - sig_apply_wallpaper(pixmap, key, path) -> applique un fond :
        path="" => fond propose genere (l'overlay le sauve en PNG) ;
        path!="" => image fichier (l'overlay pointe directement dessus).
        key = identifiant du fond (nom de preset ou chemin) pour la coche.

L'overlay pousse a l'app : set_wallpaper_dir(dir), set_selected_key(key),
et rescan(select_key) apres un import.

Le bloc __main__ est un HARNAIS DE TEST VISUEL supprimable.
"""

import sys
import math
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QEvent, Signal
from PySide6.QtGui import (
    QPainter, QColor, QFont, QPainterPath, QPen, QPixmap,
    QLinearGradient, QRadialGradient, QBrush, QImageReader,
)
from PySide6.QtWidgets import QApplication, QWidget

from circusvoip_phone_apps import PhoneApp, PhoneServices


def _load_oriented_pixmap(path):
    """Charge une image en APPLIQUANT son orientation EXIF (sinon les photos
    portrait stockees en paysage + tag EXIF s'affichent couchees)."""
    try:
        rd = QImageReader(str(path))
        rd.setAutoTransform(True)
        img = rd.read()
        if not img.isNull():
            return QPixmap.fromImage(img)
    except Exception:
        pass
    return QPixmap(str(path))

# --- palette ---
_BG     = "#0d1117"
_PANEL  = "#161b22"
_BORDER = "#30363d"
_TEXT   = "#c9d1d9"
_MUTED  = "#6e7681"
_ACCENT = "#2f6fed"

# Fond par defaut (degrade genere) = le repli du home quand aucun fond n'est
# configure. Doit matcher HOME_BG_DEFAULT_* de circusvoip_phone_apps.
_DEFAULT_TOP = "#16263f"
_DEFAULT_BOT = "#0a0d14"
_DEFAULT_KEY = "__default__"

# Fonds proposes generes par code : (nom, couleur haut, couleur bas).
_PRESETS = [
    ("Sombre",    "#0d1117", "#161b22"),
    ("Nuit",      "#0a1a2f", "#14375e"),
    ("Nébuleuse", "#1a0a2f", "#4a1e6e"),
    ("Forêt",     "#0a1f12", "#14492a"),
    ("Pyro",      "#2b1206", "#7a3d18"),
]
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
_GRID_COLS = 2


class SettingsApp(PhoneApp):
    """Parametres du CircusPhone : photo de profil + fond d'ecran (grille)."""

    APP_ID = "settings"
    APP_NAME = "Paramètres"
    APP_ICON = "\u2699"
    CAPTURES_KEYBOARD = False

    sig_open_profile   = Signal()              # ecran photo de profil
    sig_open_wallpaper = Signal()              # import image perso (tuile « + »)
    sig_apply_wallpaper = Signal(object, str, str)  # (pixmap, key, path)
    sig_reset_wallpaper = Signal()             # revenir au fond par defaut

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.resize(int(screen_w), int(screen_h))
        self._screen_radius = int(screen_radius)
        self._state = "list"
        self._items = ["Photo de profil", "Fond d'écran"]
        self._index = 0
        self._rects = []
        # Grille de fonds.
        self._wallpaper_dir = Path(__file__).resolve().parent / "circusvoip_phone_wallpapers"
        self._tiles = []
        self._wp_index = 0
        self._wp_rects = []
        self._wp_scroll = 0.0             # defilement vertical de la grille (px)
        self._selected_key = ""           # fond actuellement applique (coche)
        self._file_thumbs = {}            # path -> QPixmap (cache)
        self._default_preview_path = None # image du fond par defaut (tuile)

    # --- API poussee par l'overlay ------------------------------------
    def set_wallpaper_dir(self, path):
        try:
            self._wallpaper_dir = Path(path)
        except Exception:
            pass

    def set_selected_key(self, key):
        # Aucun fond configure -> la tuile « Défaut » est cochee.
        self._selected_key = key or _DEFAULT_KEY
        self.update()

    def set_default_preview(self, path):
        """Chemin de l'image du fond PAR DEFAUT (hugolisoir.png) pour la
        miniature de la tuile « Défaut » ; None -> degrade."""
        self._default_preview_path = path or None
        self._tiles = []   # forcer un rebuild a la prochaine ouverture
        self.update()

    def rescan(self, select_key=None):
        """Reconstruit les tuiles depuis le dossier ; selectionne select_key."""
        self._rebuild_tiles()
        if select_key is not None:
            self._selected_key = select_key
            idx = next((j for j, t in enumerate(self._tiles)
                        if t.get("key") == select_key), None)
            if idx is not None:
                self._wp_index = idx
        self._clamp_index()
        self._ensure_wp_visible()
        self.update()

    # --- Cycle de vie -------------------------------------------------
    def on_show(self):
        self._state = "list"
        self._index = 0
        self.update()

    def on_hide(self):
        pass

    # --- Tuiles -------------------------------------------------------
    def _rebuild_tiles(self):
        tiles = []
        # Fond par defaut (toujours en tete). Affiche l'image hugolisoir.png
        # si disponible, sinon un degrade.
        dtile = {"kind": "default", "key": _DEFAULT_KEY,
                 "spec": ("Défaut", _DEFAULT_TOP, _DEFAULT_BOT),
                 "name": "Défaut"}
        if self._default_preview_path:
            dp = str(self._default_preview_path)
            dtile["path"] = dp
            if dp not in self._file_thumbs:
                pm = _load_oriented_pixmap(dp)
                if not pm.isNull():
                    self._file_thumbs[dp] = pm
        tiles.append(dtile)
        try:
            d = self._wallpaper_dir
            if d and Path(d).is_dir():
                for f in sorted(Path(d).iterdir()):
                    if f.suffix.lower() in _IMG_EXTS:
                        key = str(f)
                        tiles.append({"kind": "file", "key": key,
                                      "path": key, "name": f.stem})
                        if key not in self._file_thumbs:
                            pm = _load_oriented_pixmap(key)
                            if not pm.isNull():
                                self._file_thumbs[key] = pm
        except Exception:
            pass
        for spec in _PRESETS:
            tiles.append({"kind": "preset", "key": spec[0],
                          "spec": spec, "name": spec[0]})
        tiles.append({"kind": "add", "key": None})
        self._tiles = tiles

    def _clamp_index(self):
        n = len(self._tiles)
        if n == 0:
            self._wp_index = 0
        else:
            self._wp_index = max(0, min(self._wp_index, n - 1))

    # --- Geometrie / defilement de la grille --------------------------
    def _wp_geom(self):
        """Parametres de mise en page de la grille (constants pour une taille
        d'ecran donnee)."""
        w, h = self.width(), self.height()
        margin = w * 0.07
        gap = w * 0.05
        tile_w = (w - 2 * margin - (_GRID_COLS - 1) * gap) / _GRID_COLS
        tile_h = tile_w * 1.25
        area_top = h * 0.155
        area_bottom = h * 0.92
        rad = min(tile_w, tile_h) * 0.14
        return margin, gap, tile_w, tile_h, area_top, area_bottom, rad

    def _wp_content_h(self):
        n = len(self._tiles)
        if n == 0:
            return 0.0
        _, gap, _, tile_h, _, _, _ = self._wp_geom()
        rows = (n + _GRID_COLS - 1) // _GRID_COLS
        return rows * (tile_h + gap) - gap

    def _wp_max_scroll(self):
        _, _, _, _, area_top, area_bottom, _ = self._wp_geom()
        visible = area_bottom - area_top
        return max(0.0, self._wp_content_h() - visible)

    def _ensure_wp_visible(self):
        """Ajuste le defilement pour que la tuile selectionnee soit visible."""
        _, gap, _, tile_h, area_top, area_bottom, _ = self._wp_geom()
        row = self._wp_index // _GRID_COLS
        top = row * (tile_h + gap)            # y dans le contenu (0 = 1ere rangee)
        bot = top + tile_h
        view_h = area_bottom - area_top
        if top - self._wp_scroll < 0:
            self._wp_scroll = top
        elif bot - self._wp_scroll > view_h:
            self._wp_scroll = bot - view_h
        self._wp_scroll = max(0.0, min(self._wp_scroll, self._wp_max_scroll()))

    def wheelEvent(self, event):
        if self._state != "wallpaper":
            return
        try:
            dy = event.angleDelta().y()
        except Exception:
            dy = 0
        if dy:
            self._wp_scroll = max(0.0, min(self._wp_scroll - dy * 0.5,
                                           self._wp_max_scroll()))
            self.update()

    # --- Navigation ---------------------------------------------------
    def handle_nav(self, direction: str) -> bool:
        if self._state == "list":
            if direction == "up":
                self._index = (self._index - 1) % len(self._items)
                self.update(); return True
            if direction == "down":
                self._index = (self._index + 1) % len(self._items)
                self.update(); return True
            if direction == "enter":
                self._activate_list(); return True
            return False
        # state == "wallpaper"
        n = len(self._tiles)
        if n == 0:
            return True
        i = self._wp_index
        col = i % _GRID_COLS
        moved = False
        if direction == "left":
            if col > 0:
                self._wp_index = i - 1; moved = True
        elif direction == "right":
            if col < _GRID_COLS - 1 and i + 1 < n:
                self._wp_index = i + 1; moved = True
        elif direction == "up":
            if i - _GRID_COLS >= 0:
                self._wp_index = i - _GRID_COLS; moved = True
        elif direction == "down":
            target = i + _GRID_COLS
            if target < n:
                self._wp_index = target; moved = True
            else:
                # Pas de tuile juste en dessous (colonne de droite + derniere
                # rangee partielle) : aller a la derniere tuile s'il reste une
                # rangee en dessous (ex. le « + » seul a gauche).
                next_row_start = ((i // _GRID_COLS) + 1) * _GRID_COLS
                if next_row_start < n:
                    self._wp_index = n - 1; moved = True
        elif direction == "enter":
            self._activate_wallpaper(); return True
        else:
            return False
        if moved:
            self._ensure_wp_visible()
            self.update()
        return True

    def handle_back(self) -> bool:
        if self._state == "wallpaper":
            self._state = "list"
            self.update()
            return True
        return False

    def mousePressEvent(self, event):
        pos = event.position()
        if self._state == "list":
            for i, r in enumerate(self._rects):
                if r.contains(pos):
                    self._index = i; self.update(); self._activate_list(); return
        else:
            _, _, _, _, area_top, area_bottom, _ = self._wp_geom()
            if pos.y() < area_top or pos.y() > area_bottom:
                super().mousePressEvent(event); return
            for i, r in enumerate(self._wp_rects):
                if r.contains(pos):
                    self._wp_index = i; self._ensure_wp_visible()
                    self.update(); self._activate_wallpaper(); return
        super().mousePressEvent(event)

    def _activate_list(self):
        if self._index == 0:
            try: self.sig_open_profile.emit()
            except Exception: pass
        elif self._index == 1:
            self._rebuild_tiles()
            idx = next((j for j, t in enumerate(self._tiles)
                        if t.get("key") == self._selected_key), 0)
            self._wp_index = idx
            self._wp_scroll = 0.0
            self._ensure_wp_visible()
            self._state = "wallpaper"
            self.update()

    def _activate_wallpaper(self):
        if not self._tiles:
            return
        t = self._tiles[self._wp_index]
        kind = t.get("kind")
        if kind == "add":
            try: self.sig_open_wallpaper.emit()
            except Exception: pass
            return
        if kind == "default":
            self._selected_key = _DEFAULT_KEY
            try: self.sig_reset_wallpaper.emit()
            except Exception: pass
            self.update()
            return
        if kind == "preset":
            pm = self._make_preset(t["spec"], self.width(), self.height())
            self._selected_key = t["key"]
            try: self.sig_apply_wallpaper.emit(pm, t["key"], "")
            except Exception: pass
        elif kind == "file":
            pm = self._file_thumbs.get(t["key"])
            if pm is None or pm.isNull():
                pm = _load_oriented_pixmap(t["path"])
            self._selected_key = t["key"]
            try: self.sig_apply_wallpaper.emit(pm, t["key"], t["path"])
            except Exception: pass
        self.update()   # on RESTE sur la grille ; la coche se met a jour

    # --- Generation d'un fond propose ---------------------------------
    def _make_preset(self, spec, w, h):
        name, top, bot = spec
        w = max(2, int(w)); h = max(2, int(h))
        pm = QPixmap(w, h)
        pm.fill(QColor(top))
        pr = QPainter(pm)
        pr.setRenderHint(QPainter.Antialiasing, True)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(top)); g.setColorAt(1.0, QColor(bot))
        pr.fillRect(0, 0, w, h, QBrush(g))
        rg = QRadialGradient(w * 0.5, h * 0.30, max(w, h) * 0.65)
        rg.setColorAt(0.0, QColor(255, 255, 255, 30))
        rg.setColorAt(1.0, QColor(255, 255, 255, 0))
        pr.fillRect(0, 0, w, h, QBrush(rg))
        pr.end()
        return pm

    # --- Rendu --------------------------------------------------------
    def _draw_gear(self, p, cx, cy, r):
        teeth = 9; inner = r * 0.80
        path = QPainterPath()
        for i in range(teeth * 2):
            ang = math.pi * i / teeth
            rr = r if (i % 2 == 0) else inner
            x = cx + rr * math.cos(ang); y = cy + rr * math.sin(ang)
            (path.moveTo if i == 0 else path.lineTo)(x, y)
        path.closeSubpath()
        p.setPen(Qt.NoPen); p.setBrush(QColor(_ACCENT)); p.drawPath(path)
        hole = r * 0.34
        p.setBrush(QColor(_BG))
        p.drawEllipse(QRectF(cx - hole, cy - hole, 2 * hole, 2 * hole))

    def _draw_check(self, p, cx, cy, r):
        """Petite pastille avec une coche (fond actuellement applique)."""
        p.setPen(Qt.NoPen); p.setBrush(QColor(_ACCENT))
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        pen = QPen(QColor("#ffffff"), max(2.0, r * 0.32))
        pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(cx - r * 0.45, cy + r * 0.05)
        path.lineTo(cx - r * 0.10, cy + r * 0.40)
        path.lineTo(cx + r * 0.50, cy - r * 0.35)
        p.drawPath(path)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self._screen_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()),
                                self._screen_radius, self._screen_radius)
            p.setClipPath(path)
        p.fillRect(self.rect(), QColor(_BG))
        if self._state == "list":
            self._draw_list(p)
        else:
            self._draw_wallpaper(p)
        p.end()

    def _draw_list(self, p):
        w, h = self.width(), self.height()
        p.setPen(QColor(_TEXT))
        ft = QFont("Consolas", max(14, w // 11)); ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.07, w, h * 0.12), Qt.AlignCenter, "Paramètres")
        self._draw_gear(p, w * 0.5, h * 0.18, max(7, w // 22))
        self._rects = []
        bw = w * 0.82; bh = max(34.0, h * 0.11); gap = bh * 0.34
        x = (w - bw) / 2.0; y0 = h * 0.34
        fb = QFont("Consolas", max(9, w // 19)); fb.setBold(True)
        rad = bh * 0.26
        for i, label in enumerate(self._items):
            r = QRectF(x, y0 + i * (bh + gap), bw, bh)
            self._rects.append(r)
            if i == self._index:
                p.setBrush(QColor(_ACCENT)); p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, rad, rad); p.setPen(QColor("#ffffff"))
            else:
                p.setBrush(QColor(_PANEL)); p.setPen(QPen(QColor(_BORDER), 1.5))
                p.drawRoundedRect(r, rad, rad); p.setPen(QColor(_TEXT))
            p.setFont(fb); p.drawText(r, Qt.AlignCenter, label)

    def _draw_wallpaper(self, p):
        w, h = self.width(), self.height()
        p.setPen(QColor(_TEXT))
        ft = QFont("Consolas", max(13, w // 12)); ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.05, w, h * 0.10), Qt.AlignCenter, "Fond d'écran")

        if not self._tiles:
            self._rebuild_tiles()
        n = len(self._tiles)
        margin, gap, tile_w, tile_h, area_top, area_bottom, rad = self._wp_geom()
        # Re-clamp le defilement (le contenu a pu changer).
        self._wp_scroll = max(0.0, min(self._wp_scroll, self._wp_max_scroll()))
        flabel = QFont("Consolas", max(7, w // 30)); flabel.setBold(True)
        self._wp_rects = []

        # Zone defilante : on clippe pour ne pas peindre sur le titre / pied.
        p.save()
        p.setClipRect(QRectF(0, area_top, w, area_bottom - area_top),
                      Qt.IntersectClip)
        for i in range(n):
            t = self._tiles[i]
            row = i // _GRID_COLS; col = i % _GRID_COLS
            x = margin + col * (tile_w + gap)
            y = area_top + row * (tile_h + gap) - self._wp_scroll
            r = QRectF(x, y, tile_w, tile_h)
            self._wp_rects.append(r)
            # Hors zone visible : inutile de dessiner.
            if r.bottom() < area_top or r.top() > area_bottom:
                continue
            nav = (i == self._wp_index)
            cur = (t.get("key") == self._selected_key and t.get("kind") != "add")
            tile_path = QPainterPath(); tile_path.addRoundedRect(r, rad, rad)
            kind = t.get("kind")
            if kind == "add":
                p.setBrush(QColor(_PANEL))
                pen = QPen(QColor(_ACCENT if nav else _MUTED), 3 if nav else 2)
                pen.setStyle(Qt.DashLine); p.setPen(pen)
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QPen(QColor(_ACCENT if nav else _TEXT),
                              max(3, tile_w * 0.04)))
                cx = r.center().x(); cy = r.center().y() - r.height() * 0.06
                arm = tile_w * 0.18
                p.drawLine(int(cx - arm), int(cy), int(cx + arm), int(cy))
                p.drawLine(int(cx), int(cy - arm), int(cx), int(cy + arm))
                p.setPen(QColor(_MUTED)); p.setFont(flabel)
                p.drawText(QRectF(r.left(), r.bottom() - r.height() * 0.24,
                                  r.width(), r.height() * 0.22),
                           Qt.AlignCenter, "Ajouter")
                continue
            # Tuile image (fichier / defaut avec image) ou degrade (preset /
            # defaut sans image).
            tpath = t.get("path")
            thumb = self._file_thumbs.get(tpath) if tpath else None
            p.save(); p.setClipPath(tile_path)
            if thumb is not None and not thumb.isNull():
                scaled = thumb.scaled(int(tile_w), int(tile_h),
                                      Qt.KeepAspectRatioByExpanding,
                                      Qt.SmoothTransformation)
                sx = (scaled.width() - tile_w) / 2.0
                sy = (scaled.height() - tile_h) / 2.0
                p.drawPixmap(r, scaled, QRectF(sx, sy, tile_w, tile_h))
            elif t.get("spec"):
                _, top, bot = t["spec"]
                g = QLinearGradient(r.left(), r.top(), r.left(), r.bottom())
                g.setColorAt(0.0, QColor(top)); g.setColorAt(1.0, QColor(bot))
                p.fillRect(r, QBrush(g))
                rg = QRadialGradient(r.center().x(), r.top() + r.height() * 0.3,
                                     max(r.width(), r.height()) * 0.7)
                rg.setColorAt(0.0, QColor(255, 255, 255, 26))
                rg.setColorAt(1.0, QColor(255, 255, 255, 0))
                p.fillRect(r, QBrush(rg))
            else:
                p.fillRect(r, QColor(_PANEL))
            self._draw_tile_label(p, r, t.get("name", ""), flabel)
            p.restore()
            self._draw_tile_border(p, r, rad, nav)
            if cur:
                self._draw_check(p, r.right() - tile_w * 0.16,
                                 r.top() + tile_w * 0.16, tile_w * 0.13)
        p.restore()

        # Barre de defilement indicative.
        ms = self._wp_max_scroll()
        if ms > 0:
            view_h = area_bottom - area_top
            total = view_h + ms
            thumb_h = max(18.0, view_h * view_h / total)
            thumb_y = area_top + (view_h - thumb_h) * (self._wp_scroll / ms)
            bar_w = max(2.5, w * 0.012)
            bar_x = w - margin * 0.5
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(_BORDER))
            p.drawRoundedRect(QRectF(bar_x, area_top, bar_w, view_h),
                              bar_w / 2, bar_w / 2)
            p.setBrush(QColor(_ACCENT))
            p.drawRoundedRect(QRectF(bar_x, thumb_y, bar_w, thumb_h),
                              bar_w / 2, bar_w / 2)

        p.setPen(QColor(_MUTED))
        fh = QFont("Consolas", max(8, w // 28)); p.setFont(fh)
        p.drawText(QRectF(0, h * 0.94, w, h * 0.05), Qt.AlignCenter, "Échap : retour")

    def _draw_tile_label(self, p, r, name, font):
        p.setPen(Qt.NoPen); p.setBrush(QColor(0, 0, 0, 120))
        band = r.height() * 0.22
        p.drawRect(QRectF(r.left(), r.bottom() - band, r.width(), band))
        p.setPen(QColor("#ffffff")); p.setFont(font)
        p.drawText(QRectF(r.left(), r.bottom() - band, r.width(), band),
                   Qt.AlignCenter, name)

    def _draw_tile_border(self, p, r, rad, nav):
        p.setPen(QPen(QColor(_ACCENT if nav else _BORDER), 3 if nav else 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, rad, rad)


# ===================================================================
#  HARNAIS DE TEST VISUEL (supprimable)
# ===================================================================
class _Harness(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("CircusPhone — Paramètres (harnais)")
        body_w, body_h = 300, 620
        self._radius = 22
        self.setFixedSize(body_w, body_h)
        self.app = SettingsApp(body_w - 24, body_h - 70, 14,
                               PhoneServices(), parent=self)
        self.app.move(12, 56)
        self.app.sig_open_profile.connect(lambda: print("[h] photo de profil"))
        self.app.sig_open_wallpaper.connect(lambda: print("[h] + import"))
        self.app.sig_apply_wallpaper.connect(
            lambda pm, key, path: print("[h] apply", key, path, pm.size()))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            k = event.key()
            if k == Qt.Key_Escape:
                if not self.app.handle_back(): self.close()
            elif k in (Qt.Key_Up, Qt.Key_Z, Qt.Key_W): self.app.handle_nav("up")
            elif k in (Qt.Key_Down, Qt.Key_S): self.app.handle_nav("down")
            elif k in (Qt.Key_Left, Qt.Key_Q, Qt.Key_A): self.app.handle_nav("left")
            elif k in (Qt.Key_Right, Qt.Key_D): self.app.handle_nav("right")
            elif k in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter): self.app.handle_nav("enter")
            return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        a = QApplication.instance()
        if a is not None: a.installEventFilter(self)

    def paintEvent(self, ev):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QColor("#1a1a1a")); p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), self._radius, self._radius); p.end()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = _Harness(); win.show()
    sys.exit(app.exec())
