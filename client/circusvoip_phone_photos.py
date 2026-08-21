"""CircusPhone - App "Photos" : galerie a deux onglets.

  - "Mes Photos"     : cliches pris par la Camera (screenshots/circusphoto_*)
  - "Photos Recues"  : images recues en messagerie (circusphone_images/*_in.jpg)

[PHOTOS 02/08/2026] Ajout de l'onglet "Photos Recues".

Pourquoi cet onglet
-------------------
Une image recue est ecrite dans circusphone_images/ et referencee par une
bulle "[img]<fichier>" du fil de messagerie. Or le fil est plafonne
(PHONE_MAX_MESSAGES) : quand la bulle sort du fil, le fichier reste sur le
disque mais PLUS RIEN n'y donne acces. Cet onglet est la seule porte
d'entree permanente vers ces images.

Ne sont listes que les "_in.jpg" (recus). Les "_out.jpg" sont des copies
JPEG re-encodees de cliches deja presents dans screenshots/ : les afficher
ferait doublon avec l'onglet "Mes Photos".

Affichage
---------
Grille de DEUX colonnes, sans libelle sous les vignettes (decision du
02/08). La navigation reprend la mecanique de l'ecran Appels : l'index -1
designe la barre d'onglets, ou l'on remonte depuis la premiere ligne.

App NON-jeu (CAPTURES_KEYBOARD = False) : navigation via handle_nav/handle_back.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QPen
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QGridLayout, QScrollArea,
    QFrame, QStackedWidget, QSizePolicy,
)

from circusvoip_phone_apps import PhoneApp
# Barre d'onglets partagee avec l'ecran Appels : on la REUTILISE plutot que
# d'en redefinir une, pour que les deux ecrans ne divergent pas et pour
# heriter du WA_StyledBackground qu'elle pose (sans lui, la surbrillance de
# la barre ne se peint pas).
# /!\ circusvoip_phone_annuaire DOIT figurer dans RELEASE_FILES.
from circusvoip_phone_annuaire import _Onglets


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


def _images_recues_dir() -> str:
    """Cache des images de messagerie : ./circusphone_images.

    Meme dossier que PHONE_IMAGES_DIR cote client (les modules sont livres
    a plat, cote a cote). On le recalcule ici au lieu de l'importer : ca
    evite une dependance de plus vers circusvoip_client.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "circusphone_images")


def _list_recues() -> list:
    """Images RECUES, plus recentes d'abord.

    Nommage pose a la reception : "<ts_ms>_<expediteur>_in.jpg". On trie sur
    l'horodatage du NOM (pose par l'emetteur, stable) plutot que sur la date
    du fichier, qui bouge a la moindre copie de dossier. Repli sur mtime si
    le prefixe n'est pas lisible.
    """
    d = _images_recues_dir()
    out = []
    try:
        for name in os.listdir(d):
            low = name.lower()
            if not low.endswith("_in.jpg"):
                continue
            full = os.path.join(d, name)
            try:
                ts = float(name.split("_", 1)[0])
            except Exception:
                try:
                    ts = os.path.getmtime(full) * 1000.0
                except Exception:
                    ts = 0.0
            out.append((ts, full))
    except Exception:
        pass
    out.sort(reverse=True)
    return [f for _ts, f in out]


class _PhotoCell(QFrame):
    """Une vignette de la grille. Pas de libelle : l'image seule."""

    def __init__(self, path: str, cell_w: int, cell_h: int, parent=None):
        super().__init__(parent)
        self._path = path
        self.setObjectName("PhotoCell")
        # Sans WA_StyledBackground, le cadre de selection ne se peint pas sur
        # un QFrame stylise par feuille de style.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(cell_w, cell_h)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(0)
        thumb = QLabel()
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background:#000000; border-radius:6px;")
        lay.addWidget(thumb, 1)
        pix = QPixmap(path)
        if not pix.isNull():
            pix = pix.scaled(QSize(cell_w - 8, cell_h - 8),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
            thumb.setPixmap(pix)
        self.set_nav_selected(False)

    def path(self) -> str:
        return self._path

    def set_nav_selected(self, sel: bool):
        self.setStyleSheet(
            "QFrame#PhotoCell { background:%s; border:2px solid %s; "
            "border-radius:8px; }"
            % (("rgba(47,111,237,0.14)", "#2f6fed") if sel
               else ("transparent", "transparent")))


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
        self._cells = []
        self._paths = []
        # -1 = curseur sur la BARRE D'ONGLETS, >=0 = index dans la grille.
        # Meme convention que _nav_historique de l'ecran Appels.
        self._nav_index = 0
        self._tab = 0                # 0 = Mes Photos, 1 = Photos Recues
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
        self._onglets = _Onglets(["Mes Photos", "Photos Reçues"],
                                 self._changer_onglet)
        lv.addWidget(self._onglets)
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
        self._lbl_empty = QLabel("Aucune photo.")
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet("color:#9aa0a6; font-size:11pt; "
                                      "background:transparent; padding:40px;")
        self._list_layout.addWidget(self._lbl_empty)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self._grid.setSpacing(6)
        self._list_layout.addWidget(self._grid_host)
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
    NB_COLS = 2

    def on_show(self):
        """(Re)construit la galerie a chaque ouverture (de nouvelles photos
        ont pu etre prises entre-temps). Revient toujours a la liste, et
        TOUJOURS sur l'onglet "Mes Photos" (decision du 02/08) : l'app n'a
        pas d'etat a retenir d'une ouverture a l'autre."""
        self._view_path = None
        self._stack.setCurrentWidget(self._page_list)
        if self._onglets.courant() != 0:
            self._onglets.selectionner(0)   # declenche _changer_onglet
        else:
            self._tab = 0
            self._rebuild()

    def _changer_onglet(self, i: int):
        """Callback de la barre. Le curseur RESTE sur la barre (index -1).

        C'est le comportement de _basculer_onglet cote Appels : reposer le
        curseur dans le contenu empechait d'enchainer gauche/droite pour
        revenir a l'onglet precedent.
        """
        self._tab = int(i)
        self._nav_index = -1
        self._rebuild(garder_curseur=True)

    def _list_courante(self) -> list:
        return _list_recues() if self._tab == 1 else _list_photos()

    def _rebuild(self, garder_curseur: bool = False):
        # Vider les anciennes cellules.
        for c in self._cells:
            c.setParent(None)
            c.deleteLater()
        self._cells = []
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)

        self._paths = self._list_courante()
        vide = not self._paths
        self._lbl_empty.setText("Aucune photo reçue." if self._tab == 1
                                else "Aucune photo.")
        self._lbl_empty.setVisible(vide)
        self._grid_host.setVisible(not vide)

        # Cellule carree, deux par ligne, marges et espacement deduits.
        dispo = max(80, self._screen_w - 16 - self._grid.spacing())
        cell_w = int(dispo / self.NB_COLS)
        cell_h = int(cell_w * 0.75)
        for i, path in enumerate(self._paths):
            cell = _PhotoCell(path, cell_w, cell_h)
            self._grid.addWidget(cell, i // self.NB_COLS, i % self.NB_COLS)
            self._cells.append(cell)

        if not garder_curseur:
            self._nav_index = 0 if self._paths else -1
        if self._nav_index >= len(self._paths):
            self._nav_index = len(self._paths) - 1 if self._paths else -1
        self._apply_highlight()

    def _apply_highlight(self):
        on_list = (self._stack.currentWidget() is self._page_list)
        sur_barre = (self._nav_index < 0)
        self._onglets.set_nav_highlight(on_list and sur_barre)
        for i, c in enumerate(self._cells):
            c.set_nav_selected(on_list and not sur_barre
                               and i == self._nav_index)
        if on_list and not sur_barre and self._cells:
            try:
                self._scroll.ensureWidgetVisible(self._cells[self._nav_index],
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
        """Grille a NB_COLS colonnes + barre d'onglets a l'index -1.

        haut  : remonte d'une ligne ; depuis la 1re ligne -> barre d'onglets
        bas   : descend d'une ligne ; depuis la barre -> 1re vignette.
                Depuis l'avant-derniere ligne quand la derniere est
                incomplete, on atterrit sur la DERNIERE vignette existante
                plutot que dans le vide (cas du nombre impair de photos).
        g/d   : sur la barre -> change d'onglet ; dans la grille -> change
                de colonne. Volontairement SANS bouclage : aller a gauche
                depuis la colonne de gauche ne fait rien, sinon un simple
                deplacement horizontal changerait d'ecran par surprise.
        """
        if self._stack.currentWidget() is self._page_view:
            # En vue plein ecran : seul Retour ferme (gere par handle_back).
            return True

        n = len(self._cells)
        cols = self.NB_COLS

        if direction == "up":
            if self._nav_index < 0:
                return True                      # deja sur la barre
            self._nav_index = (self._nav_index - cols
                               if self._nav_index >= cols else -1)
        elif direction == "down":
            if self._nav_index < 0:
                self._nav_index = 0 if n else -1
            elif self._nav_index + cols < n:
                self._nav_index += cols
            elif self._nav_index < n - 1:
                self._nav_index = n - 1          # derniere ligne incomplete
        elif direction in ("left", "right"):
            if self._nav_index < 0:
                cible = 0 if direction == "left" else 1
                if cible != self._onglets.courant():
                    self._onglets.selectionner(cible)   # -> _changer_onglet
                return True
            if direction == "left" and self._nav_index % cols > 0:
                self._nav_index -= 1
            elif (direction == "right" and self._nav_index % cols < cols - 1
                    and self._nav_index + 1 < n):
                self._nav_index += 1
        elif direction == "enter":
            if self._nav_index < 0:
                # Entree sur la barre : on descend dans la grille.
                self._nav_index = 0 if n else -1
                self._apply_highlight()
                return True
            if not (0 <= self._nav_index < n):
                return True
            path = self._cells[self._nav_index].path()
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

        self._apply_highlight()
        return True

    def handle_back(self) -> bool:
        # Depuis la vue plein ecran -> revenir a la liste (consomme).
        if self._stack.currentWidget() is self._page_view:
            self._view_path = None
            self._stack.setCurrentWidget(self._page_list)
            self._apply_highlight()
            return True
        return False    # depuis la liste -> l'overlay revient au home
