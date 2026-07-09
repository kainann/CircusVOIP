# -*- coding: utf-8 -*-
"""
circusvoip_phone_camera
=======================

Application « Caméra » du CircusPhone (v0.3). CAS SPECIAL : contrairement aux
autres apps, ce n'est PAS un ecran du QStackedWidget. C'est une FENETRE
SEPAREE, couchee (rotation 90°), agrandie (~2/3 de l'ecran), avec le centre
de l'ecran reellement EVIDE (trou decoupe via setMask) pour servir de
viseur : on voit le jeu a travers et on clique a travers. Le declencheur
capture la zone du viseur et l'enregistre en PNG dans ./screenshots/.

Pourquoi une fenetre separee et pas un ecran du stack : le viseur exige un
trou transparent + clic-a-travers sur toute la fenetre, incompatible avec la
zone ecran (opaque, blanche, deplacable) du chassis portrait. Le home la
lance donc comme une fenetre a part (cf. _launch_photo dans l'overlay), et
masque le telephone le temps de la prise de vue.

Integration : la classe n'herite PAS de PhoneApp (ce n'est pas un widget de
stack). Elle expose juste APP_ID / APP_NAME / APP_ICON pour que l'overlay
construise son icone de home. L'eventFilter applicatif (capture Echap /
declencheur) est installe a l'affichage et RETIRE a la fermeture, pour ne pas
avaler les touches une fois la camera fermee.

    Entrée (ou clic sur le déclencheur) : prendre la photo
    R : pivoter le téléphone (paysage <-> portrait)
    Retour arrière (ou Échap) : fermer la caméra
    HUD fixe et centré (plus déplaçable).

Note : sur écran haute densité (mise à l'échelle Windows ≠ 100 %), la zone
capturée peut être légèrement décalée ; à calibrer.

Dépendance : PySide6 uniquement. Le bloc __main__ est un harnais supprimable.
"""

from __future__ import annotations

import os
import sys
import datetime

from PySide6.QtCore import Qt, QTimer, QRectF, QRect, QEvent, QPoint, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont, QFontMetrics, QGuiApplication,
    QPainterPath, QRegion, QTransform, QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget, QLabel


# --- Palette du CircusPhone (identique aux autres applis) ---
_PHONE_BODY_COLOR    = "#1a1a1a"
_PHONE_BTN_COLOR     = "#0a0a0a"
_PHONE_BANNER_GREY   = "#888888"
_PHONE_BANNER_WHITE  = "#ffffff"
_TEXT        = "#c9d1d9"
_MUTED       = "#6e7681"
_ACCENT      = "#3fb950"
_RED         = "#f85149"
_FRAME_LINE  = "#ffffff"


def _photos_dir():
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "screenshots")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(d, exist_ok=True)
    return d


class CameraWindow(QWidget):
    """Caméra du CircusPhone : fenêtre couchée à viseur évidé. Lancée comme
    fenêtre séparée par le home (pas un écran du stack)."""

    # Metadonnees pour l'icone du home (la classe n'herite pas de PhoneApp).
    APP_ID = "photo"
    APP_NAME = "Photo"
    APP_ICON = "\U0001F4F7"          # 📷

    def __init__(self, services=None):
        # Memes flags que l'overlay telephone : fenetre outil, toujours au
        # dessus, et surtout WA_ShowWithoutActivating -> elle NE VOLE PAS le
        # focus a Star Citizen (sinon SC passe en arriere-plan). Les touches
        # sont captees par le listener clavier GLOBAL du client.
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._on_closed = None        # callback fixe par le client (reaffiche
                                      # le telephone quand la camera se ferme)
        self._flash_on = False        # flash blanc sur le viseur a la capture
        self._flash_alpha = 0
        self.setWindowTitle("CircusPhone — Photo")
        self.setMouseTracking(True)
        self.services = services
        self._evfilter_installed = False
        # Anti-declenchement : la touche Entree qui OUVRE la camera depuis le
        # home ne doit pas prendre de photo. Le declencheur clavier reste
        # DESARME tant qu'une touche declencheur n'a pas ete relachee (cf.
        # eventFilter / KeyRelease) ; ainsi le maintien ou l'auto-repetition
        # de la touche d'ouverture n'enclenche jamais de photo.
        self._shutter_armed = False
        # Vrai pendant la capture (hide -> grab -> show) pour ne pas confondre
        # le re-affichage post-capture avec une vraie (re)ouverture (qui, elle,
        # redesarme le declencheur).
        self._capturing = False

        # Orientation courante du chassis : "landscape" (couche) ou "portrait"
        # (debout). Bascule avec la touche R.
        self._orientation = "landscape"
        self._pressed_shutter = False
        self._status = ""
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._clear_status)

        # Calcule toute la geometrie (taille, viseur, chassis, masque) selon
        # l'orientation, et centre la fenetre. HUD FIXE : pas de deplacement.
        self._setup_geometry()

    # ---- geometrie selon l'orientation (paysage / portrait) ----
    def _setup_geometry(self):
        screen = QGuiApplication.primaryScreen()
        try:
            geo = screen.availableGeometry()
            sw, sh = geo.width(), geo.height()
        except Exception:
            sw, sh = 1280, 800

        # Dimensions de base du chassis en PORTRAIT (corps 200x440, repris des
        # autres applis), dimensionne pour occuper ~2/3 de l'ecran une fois
        # couche.
        target_w = sw * 0.66
        target_h = sh * 0.66
        body_h = int(min(target_w, target_h * (440.0 / 200.0)))
        body_h = max(560, body_h)
        # Le cote LONG (body_h) devient la HAUTEUR en PORTRAIT et la LARGEUR en
        # PAYSAGE : il doit tenir dans la PLUS PETITE dimension de l'ecran pour
        # que les DEUX orientations rentrent (sinon le portrait deborde).
        body_h = min(body_h, int(0.92 * min(sw, sh)))
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

        if self._orientation == "landscape":
            # Couche (rotation -90°) : fenetre body_h (large) x body_w (haut).
            # Le viseur (trou) occupe la droite, la bande "CircusPhone" la
            # gauche. (px,py) portrait -> (py, body_w - px) couche.
            self._w, self._h = body_h, body_w
            sx0, sy0 = self._screen_x, self._screen_y
            sw0, sh0 = self._screen_w, self._screen_h
            x1 = sy0
            y1 = self._body_w - (sx0 + sw0)
            self._hole = QRectF(x1, y1, sh0, sw0)
        else:
            # Debout (portrait) : fenetre body_w x body_h. Bandeau en haut,
            # viseur = zone ecran, declencheur en bas.
            self._w, self._h = body_w, body_h
            self._hole = QRectF(self._screen_x, self._screen_y,
                                self._screen_w, self._screen_h)

        self.setFixedSize(self._w, self._h)
        self._chassis_pix = self._render_chassis()
        self._apply_mask()
        self._center_on_screen()
        self.update()

    def _center_on_screen(self):
        try:
            geo = QGuiApplication.primaryScreen().availableGeometry()
            self.move(geo.x() + (geo.width() - self._w) // 2,
                      geo.y() + (geo.height() - self._h) // 2)
        except Exception:
            pass

    def _rotate(self):
        """Touche R : bascule paysage <-> portrait et recalcule tout."""
        self._orientation = ("portrait" if self._orientation == "landscape"
                             else "landscape")
        self._setup_geometry()

    # ---- chassis : dessine en portrait, pivote -90° si paysage ----
    def _render_chassis(self):
        # 1) dessine le téléphone en PORTRAIT (exactement comme les autres applis)
        portrait = QPixmap(self._body_w, self._body_h)
        portrait.fill(Qt.transparent)
        p = QPainter(portrait)
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
        p.end()
        # 2) En PAYSAGE : rotation -90° (telephone couche, bande large a
        #    GAUCHE). En PORTRAIT : on garde le chassis tel quel (bandeau haut).
        if self._orientation == "landscape":
            return portrait.transformed(QTransform().rotate(-90),
                                        Qt.SmoothTransformation)
        return portrait

    # ---- masque : silhouette du châssis MOINS le trou, PLUS le déclencheur ----
    def _apply_mask(self, include_hole=False, capture=False):
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self._w, self._h),
                            self._radius, self._radius)
        outer = QRegion(path.toFillPolygon(QTransform()).toPolygon())
        if include_hole:
            # Flash : le viseur devient peignable (blanc) le temps du flash.
            self.setMask(outer)
            return
        hole = QRegion(self._hole.toRect())
        mask = outer.subtracted(hole)
        if capture:
            # Capture : on RETIRE aussi le disque du declencheur du masque ->
            # toute la zone viseur est transparente (la scene passe a travers),
            # donc AUCUN artefact (rond/anneau) ne peut apparaitre sur la photo,
            # quel que soit le timing de repaint.
            self.setMask(mask)
            return
        # Le déclencheur : on réintègre tout le disque (bord propre). Le vide
        # entre le rond et l'anneau ne sera PAS découpé : il sera peint en noir
        # translucide (la scène reste visible, juste assombrie), ce qui évite
        # les bords crénelés du masque.
        c = self._shutter_center()
        rad, _ring_w, _dot = self._shutter_metrics()
        r = rad + 2
        disc = QRegion(int(round(c.x() - r)), int(round(c.y() - r)),
                       int(round(2 * r)), int(round(2 * r)), QRegion.Ellipse)
        self.setMask(mask.united(disc))

    # ---- déclencheur : dans le viseur (zone écran), à DROITE, centré en
    #      hauteur. Le disque est réintégré au masque pour rester cliquable. ----
    def _shutter_radius(self):
        return max(14.0, min(self._hole.width(), self._hole.height()) * 0.075)

    def _shutter_metrics(self):
        """Dimensions du déclencheur, partagées par le masque et le dessin :
        (rayon extérieur, épaisseur de l'anneau, rayon du rond central)."""
        rad = self._shutter_radius()
        ring_w = max(2.0, rad * 0.16)
        dot = rad * 0.55
        return rad, ring_w, dot

    def _shutter_center(self):
        rad = self._shutter_radius()
        pad = rad * 0.9
        if self._orientation == "landscape":
            cx = self._hole.right() - pad - rad
            cy = self._hole.center().y()
        else:
            cx = self._hole.center().x()
            cy = self._hole.bottom() - pad - rad
        return QPointF(cx, cy)

    def _hit_shutter(self, pos):
        c = self._shutter_center()
        return (pos.x() - c.x()) ** 2 + (pos.y() - c.y()) ** 2 <= \
            self._shutter_radius() ** 2

    # ---- capture ----
    def take_photo(self):
        tl = self.mapToGlobal(self._hole.topLeft().toPoint())
        geo = QRect(tl.x(), tl.y(),
                    int(self._hole.width()), int(self._hole.height()))
        # On NE masque PLUS la fenetre. On retire du masque le disque du
        # declencheur (mode capture) -> la zone viseur est entierement
        # transparente : ni rond, ni anneau, ni corps sombre sur la photo,
        # independamment du timing de repaint.
        self._capturing = True
        self._apply_mask(capture=True)
        self.update()
        QApplication.processEvents()
        QTimer.singleShot(90, lambda: self._grab(geo))

    def _grab(self, geo):
        path = ""
        try:
            screen = QGuiApplication.primaryScreen()
            pix = screen.grabWindow(0, geo.x(), geo.y(),
                                    geo.width(), geo.height())
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(_photos_dir(), f"circusphoto_{ts}.png")
            if not pix.save(path, "PNG"):
                path = ""
        except Exception as exc:                       # pragma: no cover
            print("Capture impossible :", exc)
            path = ""
        self._capturing = False
        if path:
            print("Photo enregistrée :", path)
            self._status = "✓ Enregistré"
        else:
            self._status = "Échec capture"
        # Statut affiche ~3 s (en bas), puis disparait.
        self._status_timer.start(3000)
        # Flash blanc sur le viseur : retour visuel de la prise.
        self._start_flash()

    def _start_flash(self):
        self._flash_on = True
        self._flash_alpha = 235
        self._apply_mask(include_hole=True)   # viseur peignable
        self.update()
        QTimer.singleShot(70, self._flash_fade)

    def _flash_fade(self):
        self._flash_alpha = 110
        self.update()
        QTimer.singleShot(70, self._end_flash)

    def _end_flash(self):
        self._flash_on = False
        self._flash_alpha = 0
        self._apply_mask()                    # re-evide le viseur (scene visible)
        self.update()

    def _clear_status(self):
        self._status = ""
        self.update()

    # ---- entrées ----
    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.KeyPress:
            k = event.key()
            if k in (Qt.Key_Escape, Qt.Key_Backspace):
                # Retour arriere (ou Echap) : fermer l'appareil photo.
                self.close()
            elif k == Qt.Key_R:
                # Pivoter le telephone (paysage <-> portrait). On ignore
                # l'auto-repeat pour ne pas enchainer les rotations.
                if not event.isAutoRepeat():
                    self._rotate()
            elif k in (Qt.Key_Return, Qt.Key_Enter):
                # On ne declenche QUE si le clavier est arme. Il ne s'arme
                # qu'apres relachement de la touche (cf. KeyRelease) : ainsi
                # l'Entree qui a OUVERT la camera (maintenue/auto-repetee)
                # n'enclenche jamais de photo. On ignore aussi l'auto-repeat
                # pour eviter une rafale en maintenant la touche.
                if self._shutter_armed and not event.isAutoRepeat():
                    self.take_photo()
            return True
        if et == QEvent.KeyRelease:
            k = event.key()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                # Le relachement (notamment celui de la touche d'ouverture)
                # arme le declencheur pour la prochaine pression deliberee.
                self._shutter_armed = True
                return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        # Fenetre passive : on la remonte au-dessus mais on NE l'active PAS
        # (WA_ShowWithoutActivating) -> SC garde le focus. Les touches viennent
        # du listener global du client.
        self.raise_()
        # Remet le masque nominal : si la camera a ete fermee PENDANT le
        # flash (masque "plein") ou une capture (masque sans disque), la
        # reouverture repart d'un viseur correct.
        self._flash_on = False
        self._apply_mask()
        if self._capturing:
            self._capturing = False
        else:
            self._shutter_armed = False

    def closeEvent(self, event):
        # Prevenir le client pour qu'il reaffiche le telephone (le listener
        # global n'a jamais ete arrete).
        cb = getattr(self, "_on_closed", None)
        if callable(cb):
            try:
                cb()
            except Exception:
                pass
        super().closeEvent(event)

    def mousePressEvent(self, e):
        # HUD FIXE : plus de deplacement. Seul le clic sur le declencheur agit.
        if e.button() == Qt.LeftButton and self._hit_shutter(e.position()):
            self._pressed_shutter = True
            self.update()
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._pressed_shutter:
            fire = self._hit_shutter(e.position())
            self._pressed_shutter = False
            self.update()
            if fire:
                self.take_photo()

    # ==================================================================
    # Rendu : on pose le châssis pivoté, puis les guides + le déclencheur
    # ==================================================================
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # châssis CircusPhone (le trou sera évidé par le masque)
        p.drawPixmap(0, 0, self._chassis_pix)
        # Pendant la capture : on NE dessine PAS les éléments DANS le viseur
        # (cadre + déclencheur) pour qu'ils n'apparaissent pas sur la photo.
        if not self._capturing:
            self._draw_frame_guides(p)
        # nom "CircusPhone" (bande gauche, vertical en paysage)
        self._draw_banner(p)
        # indications d'usage : barre horizontale en haut du viseur
        self._draw_hint_bar(p)
        # statut de capture ("✓ Enregistré") : barre du bas
        self._draw_status_bar(p)
        # Toujours appele : l'effacement du disque (transparence) doit avoir
        # lieu meme en capture (sinon rond sombre). L'anneau/rond, lui, est
        # saute en capture (cf. _draw_white_shutter).
        self._draw_white_shutter(p)
        # flash blanc sur le viseur (retour visuel de la prise)
        if self._flash_on:
            p.fillRect(self._hole, QColor(255, 255, 255, self._flash_alpha))
        p.end()

    def _draw_white_shutter(self, p):
        c = self._shutter_center()
        rad, ring_w, dot = self._shutter_metrics()
        pressed = self._pressed_shutter

        # 1) Efface la zone du disque (retire le corps sombre opaque du
        #    téléphone qui y était peint) -> redevient transparent.
        p.save()
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.setPen(Qt.NoPen)
        p.setBrush(Qt.black)
        p.drawEllipse(c, rad + 2, rad + 2)
        p.restore()

        # Pendant la capture : le disque a ete efface (donc transparent = scene
        # visible) mais on NE dessine PAS l'anneau/le rond -> le declencheur
        # n'apparait pas sur la photo, et il n'y a plus de rond sombre.
        if self._capturing:
            return

        # 2) Fond noir LÉGER (translucide) à l'intérieur du cercle : la scène
        #    reste visible, juste assombrie. Pas de contour extérieur.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))
        p.drawEllipse(c, rad - ring_w / 2, rad - ring_w / 2)

        # 3) Anneau blanc
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(_PHONE_BANNER_WHITE), ring_w))
        p.drawEllipse(c, rad - ring_w / 2, rad - ring_w / 2)

        # 4) Rond blanc central (un peu plus petit à l'appui)
        r_dot = dot * (1.0 if not pressed else 0.82)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff" if not pressed else "#c9d1d9"))
        p.drawEllipse(c, r_dot, r_dot)

    def _draw_frame_guides(self, p):
        r = self._hole
        p.setPen(QPen(QColor(_FRAME_LINE), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r.adjusted(-1, -1, 0, 0))
        L = max(10.0, min(r.width(), r.height()) * 0.06)
        p.setPen(QPen(QColor(_FRAME_LINE), 2))
        corners = [
            (r.left(), r.top(), 1, 1), (r.right(), r.top(), -1, 1),
            (r.left(), r.bottom(), 1, -1), (r.right(), r.bottom(), -1, -1),
        ]
        off = 3
        for (cx, cy, sgx, sgy) in corners:
            ox, oy = cx - sgx * off, cy - sgy * off
            p.drawLine(QPointF(ox, oy), QPointF(ox + sgx * L, oy))
            p.drawLine(QPointF(ox, oy), QPointF(ox, oy + sgy * L))

    def _fit_font(self, text, max_w, base_px, min_px, bold):
        """Plus grande police (>= min_px) telle que 'text' tienne dans max_w."""
        f = QFont("sans-serif")
        f.setWeight(QFont.DemiBold if bold else QFont.Medium)
        px = max(min_px, int(base_px))
        for _ in range(60):
            f.setPixelSize(px)
            if QFontMetrics(f).horizontalAdvance(text) <= max_w or px <= min_px:
                break
            px -= 1
        return f

    def _draw_banner(self, p):
        """Nom 'CircusPhone' dans la bande large : VERTICAL en paysage
        (inchange), horizontal en portrait. (Le STATUT de capture est desormais
        affiche en bas, cf. _draw_status_bar.)"""
        vertical = (self._orientation == "landscape")
        if vertical:
            band_long, band_short = float(self._h), self._hole.left()
            cx, cy = band_short / 2.0, self._h / 2.0
        else:
            band_long, band_short = float(self._w), self._hole.top()
            cx, cy = self._w / 2.0, band_short / 2.0
        if band_short <= 1 or band_long <= 1:
            return
        p.save()
        p.translate(cx, cy)
        if vertical:
            p.rotate(-90)
        avail = band_long * 0.8

        def _baseline(fm, y_c):
            return y_c + fm.ascent() / 2.0 - fm.descent() / 2.0

        # Nom "CircusPhone" (deux tons), centre dans la bande.
        size_t = max(9, int(band_short * 0.34))
        f_c = QFont("sans-serif"); f_c.setWeight(QFont.Medium)
        f_p = QFont("sans-serif"); f_p.setWeight(QFont.DemiBold)
        for _ in range(24):
            f_c.setPixelSize(size_t); f_p.setPixelSize(int(size_t * 1.4))
            wc = QFontMetrics(f_c).horizontalAdvance("Circus")
            wp = QFontMetrics(f_p).horizontalAdvance("Phone")
            if wc + wp <= avail or size_t <= 9:
                break
            size_t -= 1
        fm_p = QFontMetrics(f_p)
        yb = _baseline(fm_p, 0)
        x0 = -(wc + wp) / 2.0
        p.setFont(f_c); p.setPen(QColor(_PHONE_BANNER_GREY))
        p.drawText(QPointF(x0, yb), "Circus")
        p.setFont(f_p); p.setPen(QColor(_PHONE_BANNER_WHITE))
        p.drawText(QPointF(x0 + wc, yb), "Phone")
        p.restore()

    def _draw_status_bar(self, p):
        """Statut de capture ('✓ Enregistré' en vert / erreur en rouge) dans le
        bandeau EN BAS, sous le viseur. Affiche ~3 s apres la prise."""
        if not self._status:
            return
        r = self._hole
        bot_h = self._h - r.bottom()      # bandeau sous le viseur
        if bot_h < 10:
            return
        f = self._fit_font(self._status, r.width() * 0.9, bot_h * 0.6, 8, True)
        fm = QFontMetrics(f)
        p.setFont(f)
        p.setPen(QColor(_ACCENT if self._status.startswith("✓") else _RED))
        x = r.left() + (r.width() - fm.horizontalAdvance(self._status)) / 2.0
        y = r.bottom() + (bot_h + fm.ascent() - fm.descent()) / 2.0
        p.drawText(QPointF(x, y), self._status)

    def _draw_hint_bar(self, p):
        """Indications d'usage HORIZONTALES dans le bandeau AU-DESSUS du viseur
        (sur le corps du chassis, SANS fond) : la zone photo reste propre.
        En portrait, 'CircusPhone' occupe deja ce bandeau : on cantonne les
        indications a une bande en HAUT pour ne pas le chevaucher."""
        r = self._hole
        top_h = r.top()          # hauteur du bandeau au-dessus du viseur
        if top_h < 10:
            return
        zone = top_h if self._orientation == "landscape" else min(top_h, 46.0)
        hints = "R pivoter    ·    Entrée photo    ·    Retour fermer"
        f = self._fit_font(hints, r.width() * 0.94, zone * 0.62, 8, False)
        fm = QFontMetrics(f)
        p.setFont(f)
        p.setPen(QColor(_PHONE_BANNER_WHITE))
        x = r.left() + (r.width() - fm.horizontalAdvance(hints)) / 2.0
        y = (zone + fm.ascent() - fm.descent()) / 2.0
        p.drawText(QPointF(x, y), hints)


class PhotoViewerWindow(QWidget):
    """Affiche UNE photo en grand sur l'ecran de JEU (le moniteur), pas dans
    le CircusPhone. Fenetre SANS bordure ni fond (taille exacte de l'image),
    a ~80% de la hauteur de l'ecran, orientation d'origine CONSERVEE (une photo
    horizontale reste horizontale, une verticale reste verticale). Passive :
    ne vole pas le focus a Star Citizen. Fermeture par Retour arriere (routee
    par le listener clavier global du client)."""

    HEIGHT_RATIO = 0.80        # 80% de la hauteur de l'ecran

    def __init__(self, path: str, parent=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        # Fond transparent + pas de bordure : on ne voit QUE la photo.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("CircusPhone — Photo")
        self._on_closed = None

        self._label = QLabel(self)
        self._label.setStyleSheet("background:transparent;")
        self._label.setAttribute(Qt.WA_TranslucentBackground, True)

        self._pix = QPixmap(path)
        self._setup_geometry()

    def _setup_geometry(self):
        try:
            geo = QGuiApplication.primaryScreen().availableGeometry()
            sw, sh = geo.width(), geo.height()
            ox, oy = geo.x(), geo.y()
        except Exception:
            sw, sh, ox, oy = 1280, 800, 0, 0

        if self._pix.isNull():
            # Image illisible : petite fenetre transparente, rien a montrer.
            self.resize(10, 10)
            self.move(ox + sw // 2, oy + sh // 2)
            return

        # Hauteur cible = 80% de l'ecran ; largeur PROPORTIONNELLE (on conserve
        # l'orientation d'origine, aucune rotation). Ne pas depasser la largeur
        # de l'ecran pour les photos tres larges.
        target_h = int(sh * self.HEIGHT_RATIO)
        scaled = self._pix.scaledToHeight(target_h, Qt.SmoothTransformation)
        if scaled.width() > sw:
            scaled = self._pix.scaledToWidth(int(sw * 0.98),
                                             Qt.SmoothTransformation)
        self._scaled = scaled
        w, h = scaled.width(), scaled.height()
        self._label.setPixmap(scaled)
        self._label.setGeometry(0, 0, w, h)
        # La fenetre fait EXACTEMENT la taille de la photo -> pas de marge noire.
        self.resize(w, h)
        self.move(ox + (sw - w) // 2, oy + (sh - h) // 2)

    def close_viewer(self):
        cb = self._on_closed
        try:
            self.close()
        finally:
            if callable(cb):
                try:
                    cb()
                except Exception:
                    pass

    def closeEvent(self, ev):
        super().closeEvent(ev)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = CameraWindow()
    win.show()
    sys.exit(app.exec())
