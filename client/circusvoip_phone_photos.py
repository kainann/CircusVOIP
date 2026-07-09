"""CircusPhone - App "Photos" : galerie des cliches pris par la Camera.

Lit le dossier des photos (screenshots/circusphoto_*.png), affiche une liste
de vignettes navigable au D-pad, et permet d'ouvrir une photo en plein ecran.
App NON-jeu (CAPTURES_KEYBOARD = False) : navigation via handle_nav/handle_back.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QPen
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QScrollArea, QFrame,
    QStackedWidget, QSizePolicy,
)

from circusvoip_phone_apps import PhoneApp


def _make_app_icon(size: int = 128):
    """Dessine une petite PILE DE POLAROIDS en QPixmap (icone d'app). Repli
    sur le glyphe 🖼 si Qt n'est pas encore initialise (pas de QApplication)."""
    try:
        pm = QPixmap(size, size)
        pm.fill(QColor("#1b2230"))          # fond de tuile sombre
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        cx, cy = size / 2.0, size / 2.0
        cw, ch = size * 0.46, size * 0.54
        # (angle, dx, dy, couleur de la "photo") ; ordre = arriere -> avant.
        cards = [
            (-17.0, -0.10 * size,  0.02 * size, "#4aa8e0"),   # bleu, arriere G
            (14.0,   0.10 * size,  0.05 * size, "#5fce7d"),   # vert, arriere D
            (-3.0,   0.0,         -0.02 * size, "#f2b34d"),   # orange, avant
        ]
        for ang, dx, dy, col in cards:
            p.save()
            p.translate(cx + dx, cy + dy)
            p.rotate(ang)
            card = QRectF(-cw / 2, -ch / 2, cw, ch)
            # ombre portee
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 70))
            p.drawRoundedRect(card.translated(size * 0.02, size * 0.025),
                              size * 0.04, size * 0.04)
            # cadre blanc du polaroid
            p.setBrush(QColor("#ffffff"))
            p.setPen(QPen(QColor("#d8dbe0"), max(1.0, size * 0.008)))
            p.drawRoundedRect(card, size * 0.035, size * 0.035)
            # zone "photo" : marge basse plus large (signature polaroid)
            m = cw * 0.12
            photo = QRectF(card.left() + m, card.top() + m,
                           cw - 2 * m, ch - 2 * m - ch * 0.20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(col))
            p.drawRoundedRect(photo, size * 0.02, size * 0.02)
            p.restore()
        p.end()
        if not pm.isNull():
            return pm
    except Exception:
        pass
    return "\U0001F5BC"


def _photos_dir() -> str:
    """Meme dossier que la Camera : ./screenshots (a cote du module)."""
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "screenshots")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(os.getcwd(), "screenshots")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    return d


def _list_photos() -> list:
    """Chemins des photos, plus recentes d'abord (tri par date de fichier)."""
    d = _photos_dir()
    out = []
    try:
        for name in os.listdir(d):
            low = name.lower()
            if low.startswith("circusphoto_") and low.endswith((".png", ".jpg",
                                                                 ".jpeg")):
                full = os.path.join(d, name)
                try:
                    mt = os.path.getmtime(full)
                except Exception:
                    mt = 0.0
                out.append((mt, full))
    except Exception:
        pass
    out.sort(reverse=True)
    return [p for _mt, p in out]


class _PhotoThumbRow(QFrame):
    """Une ligne de la galerie : vignette + nom de fichier."""

    def __init__(self, path: str, thumb_h: int, parent=None):
        super().__init__(parent)
        self._path = path
        self.setObjectName("PhotoThumbRow")
        self.setStyleSheet(
            "QFrame#PhotoThumbRow { background:transparent; "
            "border-bottom:1px solid #eceef0; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(12)

        thumb = QLabel()
        thumb.setFixedSize(int(thumb_h * 1.6), thumb_h)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background:#000000; border-radius:6px;")
        pix = QPixmap(path)
        if not pix.isNull():
            pix = pix.scaled(thumb.size(), Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
            thumb.setPixmap(pix)
        h.addWidget(thumb, 0)

        lbl = QLabel(os.path.basename(path))
        lbl.setStyleSheet("color:#1a1a1a; font-size:10pt; "
                          "background:transparent;")
        lbl.setWordWrap(True)
        h.addWidget(lbl, 1)

    def path(self) -> str:
        return self._path

    def set_nav_selected(self, sel: bool):
        self.setStyleSheet(
            "QFrame#PhotoThumbRow { background:%s; border-radius:8px; "
            "border-bottom:1px solid #eceef0; }"
            % ("rgba(47,111,237,0.14)" if sel else "transparent"))


class _LazyPolaroidIcon:
    """Descripteur : construit l'icone polaroids au PREMIER acces a
    Class.APP_ICON (le home la lit au rendu, quand Qt est pret) puis la met
    en cache sur la classe. Evite le dessin a l'import (QApplication absente)."""

    def __get__(self, obj, objtype=None):
        cls = objtype or type(obj)
        if getattr(cls, "_icon_cache", None) is None:
            cls._icon_cache = _make_app_icon()
        return cls._icon_cache


class PhotosApp(PhoneApp):
    APP_ID = "photos"
    APP_NAME = "Photos"
    # APP_ICON est un DESCRIPTEUR : l'icone polaroids est dessinee au moment
    # du rendu (Qt pret), pas a l'import, puis mise en cache. Repli glyphe si
    # le dessin echoue (gere dans _make_app_icon).
    _icon_cache = None
    APP_ICON = _LazyPolaroidIcon()

    def __init__(self, screen_w, screen_h, screen_radius, services,
                 parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self._rows = []
        self._nav_index = 0
        self._view_path = None       # photo affichee en plein ecran (ou None)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        # --- Page LISTE ---
        self._page_list = QWidget()
        self._page_list.setStyleSheet("background:#ffffff;")
        lv = QVBoxLayout(self._page_list)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        title = QLabel("Photos")
        title.setStyleSheet("color:#1a1a1a; font-size:15pt; font-weight:bold; "
                            "background:transparent; padding:14px 16px 8px 16px;")
        lv.addWidget(title)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#eceef0; border:none;")
        lv.addWidget(sep)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }")
        self._host = QWidget()
        self._list_layout = QVBoxLayout(self._host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._lbl_empty = QLabel("Aucune photo.\nUtilise la Caméra pour en "
                                 "prendre.")
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet("color:#9aa0a6; font-size:11pt; "
                                      "background:transparent; padding:40px;")
        self._list_layout.addWidget(self._lbl_empty)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._host)
        lv.addWidget(self._scroll, 1)
        self._stack.addWidget(self._page_list)

        # --- Page VUE (plein ecran) ---
        self._page_view = QWidget()
        self._page_view.setStyleSheet("background:#000000;")
        vv = QVBoxLayout(self._page_view)
        vv.setContentsMargins(0, 0, 0, 0)
        self._view_label = QLabel()
        self._view_label.setAlignment(Qt.AlignCenter)
        self._view_label.setStyleSheet("background:#000000;")
        self._view_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        vv.addWidget(self._view_label, 1)
        hint = QLabel("Retour : galerie")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color:#c9d1d9; font-size:9pt; "
                           "background:#000000; padding:6px;")
        vv.addWidget(hint, 0)
        self._stack.addWidget(self._page_view)

    # --- cycle de vie ---
    def on_show(self):
        """(Re)construit la galerie a chaque ouverture (de nouvelles photos
        ont pu etre prises entre-temps). Revient toujours a la liste."""
        self._view_path = None
        self._stack.setCurrentWidget(self._page_list)
        self._rebuild()

    def _rebuild(self):
        # Vider les anciennes lignes.
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        paths = _list_photos()
        self._lbl_empty.setVisible(not paths)
        thumb_h = max(48, int(self._screen_h * 0.13))
        for i, p in enumerate(paths):
            row = _PhotoThumbRow(p, thumb_h)
            self._list_layout.insertWidget(i, row)
            self._rows.append(row)
        self._nav_index = 0
        self._apply_highlight()

    def _apply_highlight(self):
        on_list = (self._stack.currentWidget() is self._page_list)
        for i, r in enumerate(self._rows):
            r.set_nav_selected(on_list and i == self._nav_index)
        if on_list and self._rows:
            try:
                self._scroll.ensureWidgetVisible(self._rows[self._nav_index],
                                                 0, 8)
            except Exception:
                pass

    def _open_view(self, path):
        self._view_path = path
        pix = QPixmap(path)
        if not pix.isNull():
            # Photo affichee a ~80% de la hauteur de l'ecran (max), largeur
            # limitee a l'ecran, en conservant les proportions.
            target = QSize(max(1, self._screen_w),
                           max(1, int(self._screen_h * 0.80)))
            self._view_label.setPixmap(
                pix.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self._view_label.setText("Image indisponible")
            self._view_label.setStyleSheet("color:#9aa0a6; background:#000000;")
        self._stack.setCurrentWidget(self._page_view)

    # --- navigation D-pad ---
    def handle_nav(self, direction: str) -> bool:
        if self._stack.currentWidget() is self._page_view:
            # En vue plein ecran : seul Retour ferme (gere par handle_back).
            return True
        if not self._rows:
            return True
        if direction in ("up", "left"):
            self._nav_index = (self._nav_index - 1) % len(self._rows)
            self._apply_highlight()
        elif direction in ("down", "right"):
            self._nav_index = (self._nav_index + 1) % len(self._rows)
            self._apply_highlight()
        elif direction == "enter":
            path = self._rows[self._nav_index].path()
            # Priorite : afficher EN GRAND sur l'ecran de jeu (fenetre separee)
            # si l'overlay a fourni le hook. Sinon, repli : vue interne au
            # telephone.
            viewer = getattr(self.services, "view_photo", None)
            if callable(viewer):
                try:
                    viewer(path)
                    return True
                except Exception:
                    pass
            self._open_view(path)
        return True

    def handle_back(self) -> bool:
        # Depuis la vue plein ecran -> revenir a la liste (consomme).
        if self._stack.currentWidget() is self._page_view:
            self._view_path = None
            self._stack.setCurrentWidget(self._page_list)
            self._apply_highlight()
            return True
        return False    # depuis la liste -> l'overlay revient au home
