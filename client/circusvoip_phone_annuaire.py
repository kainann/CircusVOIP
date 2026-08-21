#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[CONTACTS 31/07/2026] Apps « Appels » et « Contacts » du CircusPhone.

Remplace l'ecran Appels historique, qui listait les JOUEURS CONNECTES du
serveur et permettait de les appeler par pseudo. Ce fonctionnement
contredisait la decision du 30/07 : l'annuaire est reserve a
l'administration, un joueur ne doit pas savoir qui est en ligne ni
comment le joindre sans qu'on lui ait donne son numero.

--- Deux apps, deux roles ---

  Appels    : CLAVIER de composition + HISTORIQUE.
              C'est ici qu'on appelle un numero qu'on vient de recevoir
              de vive voix, sans l'avoir enregistre. Sans clavier, un
              numero donne a l'oral serait inutilisable.

  Contacts  : carnet LOCAL + ajout.
              Uniquement ce que le joueur a saisi lui-meme.

--- Le nom n'est stocke nulle part ---

Historique et messages ne retiennent que le NUMERO ; le nom est
substitue a l'affichage via repertoire.afficher(). Ajouter un contact
renomme donc retroactivement toutes les lignes de ce numero, et le
supprimer les fait redevenir des numeros. Aucune mise a jour a propager.

--- Ce qu'on ne peut pas savoir ---

Pas de pastille de presence : elle revelerait qui est connecte a partir
d'un simple numero. Un appel vers un joueur hors ligne -- ou vers un
numero qui n'existe pas -- sonne dans le vide jusqu'a expiration. Les
deux cas sont indistinguables, ce qui rend le balayage de numeros
inutile.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea,
    QVBoxLayout, QWidget,
)

from circusvoip_phone_apps import PhoneApp

try:
    from circusvoip_phone_contacts import ContactError, normalise_numero
except Exception:  # repli si le module n'est pas encore deploye
    class ContactError(Exception):
        pass

    def normalise_numero(saisie):
        """Repli minimal. [CORRECTIF 19/08/2026]

        Le repli ne definissait QUE ContactError : normalise_numero
        restait indefini, et toute saisie de numero levait NameError --
        _verifier() a chaque frappe, puis _demarrer() a la validation.
        L'ecran paraissait mort sans qu'aucun message n'apparaisse.
        """
        txt = "".join(str(saisie or "").split())
        if not txt.isdigit():
            raise ContactError("Numéro invalide.")
        return txt

# [GROUPES 19/08/2026] Regles PARTAGEES avec le serveur. Le repli garde
# l'app utilisable si le module n'est pas encore deploye : la liste des
# conversations continue de fonctionner, seuls les groupes disparaissent.
# Sans lui, un deploiement partiel casserait TOUTE la messagerie.
try:
    import circusvoip_phone_groupes as _GRP
except Exception as _e_grp:
    _GRP = None
    # [GROUPES 19/08/2026] Repli BRUYANT (§5 ter du PROJET.md) : sans ce
    # message, l'absence du module se traduirait par un bouton « Nouveau
    # groupe » manquant, ce qui ressemble a un choix de conception et non
    # a une erreur de deploiement.
    import sys as _sys_grp
    try:
        print(f"[GROUPES] Module de regles absent ({_e_grp!r}) : bouton "
              f"« Nouveau groupe » masque, groupes indisponibles.",
              file=_sys_grp.stderr, flush=True)
    except Exception:
        pass

    def normalise_numero(x):
        s = "".join(c for c in str(x or "") if c.isdigit())
        if len(s) != 6 or not s.startswith("42"):
            raise ContactError("Numéro invalide.")
        return s

# Palette, alignee sur l'ecran telephone existant (fond blanc).
_BG      = "#ffffff"
_TXT     = "#1a1a1a"
_MUTED   = "#9aa0a6"
_ACCENT  = "#2f6fed"
# [HISTORIQUE 10/08/2026] Sens de l'appel. Verts et rouges choisis dans la
# meme famille que le bouton APPELER pour ne pas introduire une 3e teinte.
_SORTANT = "#1e8e3e"   # fleche haute : j'ai appele
_ENTRANT = "#d93025"   # fleche basse : on m'a appele
_VERT    = "#3fb950"
_ROUGE   = "#e5484d"
_SEP     = "#e6e8eb"


class _Avatar(QWidget):
    """Photo ronde, ou initiale si aucune photo n'est disponible.

    On affiche une photo meme pour un numero absent du carnet : c'est
    voulu. Le RP protege le NOM, pas le visage -- croiser un inconnu et
    voir sa tete sans savoir qui il est est exactement la situation
    qu'on veut reproduire.
    """

    def __init__(self, taille: int, parent=None):
        super().__init__(parent)
        self._t = int(taille)
        self._pix: QPixmap | None = None
        self._lettre = "?"
        self.setFixedSize(self._t, self._t)

    def definir(self, jpeg: bytes | None, etiquette: str):
        self._lettre = (etiquette or "?").strip()[:1].upper() or "?"
        self._pix = None
        if jpeg:
            pm = QPixmap()
            if pm.loadFromData(jpeg, "JPEG") or pm.loadFromData(jpeg):
                self._pix = pm.scaled(self._t, self._t, Qt.KeepAspectRatioByExpanding,
                                      Qt.SmoothTransformation)
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        chemin = QPainterPath()
        chemin.addEllipse(0, 0, self._t, self._t)
        p.setClipPath(chemin)
        if self._pix is not None:
            x = (self._t - self._pix.width()) // 2
            y = (self._t - self._pix.height()) // 2
            p.drawPixmap(x, y, self._pix)
        else:
            p.fillRect(0, 0, self._t, self._t, QColor("#d7dade"))
            p.setPen(QColor(_MUTED))
            f = p.font()
            f.setPointSize(max(8, int(self._t * 0.42)))
            f.setBold(True)
            p.setFont(f)
            p.drawText(0, 0, self._t, self._t, Qt.AlignCenter, self._lettre)
        p.end()


class _BoutonAppel(QWidget):
    """Bouton rond vert avec le combine telephonique VECTORIEL.

    [02/08/2026] Reprend le trace exact de `_PhoneCircleButton` des
    ecrans d'appel (bouton "decrocher"). Le glyphe Unicode utilise
    auparavant (\u260e) est rendu differemment selon la police installee
    et ressemblait a tout sauf a un telephone. Un trace vectoriel est
    identique partout et coherent avec le reste du telephone.
    """

    sig_clicked = Signal()

    def __init__(self, taille: int, parent=None):
        super().__init__(parent)
        self._sz = int(taille)
        self._nav_sel = False
        self.setFixedSize(self._sz, self._sz)
        self.setCursor(Qt.PointingHandCursor)

    def set_nav_selected(self, on: bool):
        on = bool(on)
        if on != self._nav_sel:
            self._nav_sel = on
            self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.sig_clicked.emit()
        ev.accept()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = self._sz
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_VERT))
        p.drawEllipse(0, 0, s, s)

        # Le combine est trace dans un viewport 80x80, mais la forme
        # occupe x=12..80 et y=12..74 : son centre est en (46, 43) et non
        # (40, 40). D'ou le decalage, sinon l'icone parait de travers.
        ech = (s * 0.72) / 80.0
        ox = s / 2.0 - 46 * ech
        oy = s / 2.0 - 43 * ech

        def P(x, y):
            return ox + x * ech, oy + y * ech

        chemin = QPainterPath()
        x, y = P(18, 18); chemin.moveTo(x, y)
        cx, cy = P(18, 12); ex, ey = P(24, 12); chemin.quadTo(cx, cy, ex, ey)
        ex, ey = P(32, 12); chemin.lineTo(ex, ey)
        cx, cy = P(38, 12); ex, ey = P(38, 18); chemin.quadTo(cx, cy, ex, ey)
        ex, ey = P(38, 24); chemin.lineTo(ex, ey)
        cx, cy = P(38, 30); ex, ey = P(34, 32); chemin.quadTo(cx, cy, ex, ey)
        cx, cy = P(32, 33); ex, ey = P(32, 36); chemin.quadTo(cx, cy, ex, ey)
        cx, cy = P(32, 44); ex, ey = P(40, 52); chemin.quadTo(cx, cy, ex, ey)
        cx, cy = P(48, 60); ex, ey = P(56, 60); chemin.quadTo(cx, cy, ex, ey)
        cx, cy = P(59, 60); ex, ey = P(60, 58); chemin.quadTo(cx, cy, ex, ey)
        cx, cy = P(62, 54); ex, ey = P(68, 54); chemin.quadTo(cx, cy, ex, ey)
        ex, ey = P(74, 54); chemin.lineTo(ex, ey)
        cx, cy = P(80, 54); ex, ey = P(80, 60); chemin.quadTo(cx, cy, ex, ey)
        ex, ey = P(80, 68); chemin.lineTo(ex, ey)
        cx, cy = P(80, 74); ex, ey = P(74, 74); chemin.quadTo(cx, cy, ex, ey)
        cx, cy = P(50, 74); ex, ey = P(30, 54); chemin.quadTo(cx, cy, ex, ey)
        cx, cy = P(18, 38); ex, ey = P(18, 28); chemin.quadTo(cx, cy, ex, ey)
        chemin.closeSubpath()
        p.setBrush(QColor("#ffffff"))
        p.drawPath(chemin)

        if self._nav_sel:
            # Anneau sombre : dit quelle action Entree declenchera.
            stylo = QPen(QColor(_TXT))
            stylo.setWidth(2)
            p.setPen(stylo)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(1, 1, s - 2, s - 2)
        p.end()


class _BoutonAction(QPushButton):
    """Bouton d'action d'une ligne, avec halo de selection clavier.

    Reprend `set_nav_selected` des icones v0.3 : la ligne surlignee ne
    suffit pas quand elle porte PLUSIEURS actions -- il faut voir
    laquelle est visee avant d'appuyer sur Entree.
    """

    def __init__(self, texte: str, style_base: str, parent=None):
        super().__init__(texte, parent)
        self._style_base = style_base
        self._nav_sel = False
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(style_base)

    def set_nav_selected(self, on: bool):
        on = bool(on)
        if on == self._nav_sel:
            return
        self._nav_sel = on
        # Halo : contour sombre epais, visible aussi bien sur le bouton
        # vert que sur le bouton clair.
        self.setStyleSheet(
            self._style_base.replace("border:none;", f"border:2px solid {_TXT};")
                            .replace(f"border:1px solid {_ACCENT};",
                                     f"border:2px solid {_TXT};")
            if on else self._style_base)


class _LigneNav(QWidget):
    """Ligne selectionnable d'une liste (contact ou appel).

    [AUDIT 02/08/2026] Reprend le motif des lignes du CircusPhone v0.3
    (_PhoneContactRow, _CallHistoryRow) :

      - `WA_StyledBackground` est INDISPENSABLE. Sans cet attribut, un
        QWidget simple NE PEINT PAS le "background" defini en
        stylesheet : la selection se deplacait bien, mais restait
        totalement invisible.
      - `set_nav_highlight(selected, action)` : fond vert translucide sur
        la ligne, PLUS un halo sur l'action visee -- comme le fait
        _PhoneContactRow avec ses icones telephone/lettre.
      - clic souris sur la ligne entiere.

    Les ACTIONS sont declarees par la ligne elle-meme : gauche/droite
    circule entre elles. Une ligne peut n'en avoir qu'une (contact deja
    connu : seulement appeler), auquel cas gauche/droite ne fait rien.
    """

    sig_clic = Signal(str)

    def __init__(self, cle: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._cle = str(cle or "")
        self._actions: list = []          # [(bouton, callable)]
        self.setCursor(Qt.PointingHandCursor)

    def cle(self) -> str:
        return self._cle

    def ajouter_action(self, bouton, fonction):
        self._actions.append((bouton, fonction))

    def nb_actions(self) -> int:
        return len(self._actions)

    def declencher(self, index: int):
        """Execute l'action visee. Index borne : une ligne a moins
        d'actions qu'une autre, et l'index courant est partage."""
        if not self._actions:
            return
        i = max(0, min(index, len(self._actions) - 1))
        try:
            self._actions[i][1]()
        except Exception:
            pass

    def set_nav_highlight(self, selected: bool, action: int = 0):
        self.setStyleSheet(
            "background:rgba(63,185,80,0.22); border-radius:8px;"
            if selected else "background:transparent;")
        for i, (b, _fn) in enumerate(self._actions):
            try:
                b.set_nav_selected(selected and i == action)
            except Exception:
                pass

    def mousePressEvent(self, ev):
        try:
            self.sig_clic.emit(self._cle)
        except Exception:
            pass
        ev.accept()


class _Onglets(QWidget):
    """Barre d'onglets simple, calquee sur celle de l'ecran Appels
    historique (Menu / Historique) pour ne pas depayser."""

    def __init__(self, libelles: list[str], on_change, parent=None):
        super().__init__(parent)
        # Sans cet attribut, la surbrillance de la barre ne peindrait pas.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._on_change = on_change
        self._btns: list[QPushButton] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(0)
        for i, lib in enumerate(libelles):
            b = QPushButton(lib)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=i: self.selectionner(k))
            lay.addWidget(b, stretch=1)
            self._btns.append(b)
        self._courant = 0
        self._peindre()

    def selectionner(self, i: int):
        if i == self._courant:
            return
        self._courant = i
        self._peindre()
        self._on_change(i)

    def courant(self) -> int:
        return self._courant

    def set_nav_highlight(self, selected: bool):
        """Surbrillance de la barre quand le curseur y remonte.

        Cadre bleu fin plutot qu'un fond vert : le vert marque la LIGNE
        selectionnee dans les listes, et le reprendre ici rendait les
        deux niveaux de selection indistinguables. Le cadre delimite en
        plus clairement la zone, ce qu'un simple fond ne faisait pas.
        """
        # 2 px + fond bleute : a 1 px et sans fond, le cadre se perdait
        # sur le blanc de l'ecran, d'autant que la barre est deja soulignee
        # par le trait vert de l'onglet actif.
        self.setStyleSheet(
            f"background:rgba(47,111,237,0.10);"
            f"border:2px solid {_ACCENT};border-radius:8px;"
            if selected else
            "background:transparent;border:2px solid transparent;")

    def _peindre(self):
        for i, b in enumerate(self._btns):
            actif = (i == self._courant)
            b.setStyleSheet(
                f"QPushButton{{background:transparent;border:none;"
                f"padding:6px 2px;font-size:9pt;"
                f"font-weight:{'700' if actif else '400'};"
                f"color:{_VERT if actif else _MUTED};"
                f"border-bottom:2px solid {_VERT if actif else _SEP};}}")


def _zone_defilante() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    """Zone scrollable prete a l'emploi, fond blanc, sans bordure."""
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sc.setStyleSheet(f"QScrollArea{{background:{_BG};border:none;}}")
    hote = QWidget()
    hote.setStyleSheet(f"background:{_BG};")
    lay = QVBoxLayout(hote)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addStretch(1)
    sc.setWidget(hote)
    return sc, hote, lay


def _vider(lay: QVBoxLayout):
    """Retire tout sauf l'etirement final."""
    while lay.count() > 1:
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.deleteLater()


def _titre(texte: str) -> QLabel:
    lbl = QLabel(texte)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(
        f"color:{_TXT};font-size:11pt;font-weight:700;padding:6px 0;")
    return lbl


def _message_vide(texte: str) -> QLabel:
    lbl = QLabel(texte)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{_MUTED};font-size:9pt;padding:24px 12px;")
    return lbl


# ======================================================================
#  Appels : clavier + historique
# ======================================================================

class AppelsApp(PhoneApp):

    APP_ID   = "appels"
    APP_NAME = "Appels"
    APP_ICON = "\u260e"

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self._saisie = ""
        self._dans_champ = False
        self._lignes_hist: list[QWidget] = []
        self._numeros_hist: list[str] = []
        self._cible = 0        # 0 = champ numero, 1 = bouton Appeler
        self._idx_hist = 0
        self._action = 0
        self._nav_shown = True
        self.setStyleSheet(f"background:{_BG};")

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)
        v.addWidget(_titre("Appels"))

        self._onglets = _Onglets(["Clavier", "Historique"], self._changer_onglet)
        v.addWidget(self._onglets)

        # --- page clavier ---
        self._page_clavier = QWidget()
        vc = QVBoxLayout(self._page_clavier)
        vc.setContentsMargins(0, 8, 0, 0)
        vc.setSpacing(6)

        # Vrai QLineEdit et non un QLabel : c'est lui qui recevra le
        # focus clavier quand on entre dans le champ, et Qt gerera la
        # frappe (chiffres, retour arriere, curseur) sans qu'on ait a
        # intercepter la moindre touche.
        self._ed_num = QLineEdit()
        self._ed_num.setAlignment(Qt.AlignCenter)
        self._ed_num.setMinimumHeight(38)
        self._ed_num.setMaxLength(6)
        self._ed_num.setPlaceholderText("42••••")
        self._ed_num.textChanged.connect(self._sur_saisie)
        self._ed_num.returnPressed.connect(self._valider_saisie)
        vc.addWidget(self._ed_num)

        # Nom du contact si le numero compose est connu. Ne remplace pas
        # les chiffres : pendant la composition on montre ce qui est
        # tape, c'est un clavier.
        self._apercu = QLabel("")
        self._apercu.setAlignment(Qt.AlignCenter)
        self._apercu.setStyleSheet(f"color:{_ACCENT};font-size:9pt;")
        vc.addWidget(self._apercu)

        self._erreur = QLabel("")
        self._erreur.setAlignment(Qt.AlignCenter)
        self._erreur.setWordWrap(True)
        self._erreur.setStyleSheet(f"color:{_ROUGE};font-size:8pt;")
        vc.addWidget(self._erreur)

        # Les touches sont memorisees par caractere : taper au clavier
        # physique doit ALLUMER la touche correspondante a l'ecran, pour
        # que le pave reste credible comme un vrai telephone.
        self._touches: dict[str, QPushButton] = {}
        grille = QVBoxLayout()
        grille.setSpacing(4)
        for rangee in (("1", "2", "3"), ("4", "5", "6"),
                       ("7", "8", "9"), ("", "0", "\u232b")):
            h = QHBoxLayout()
            h.setSpacing(4)
            for touche in rangee:
                if not touche:
                    h.addStretch(1)
                    continue
                b = self._touche(touche)
                self._touches[touche] = b
                h.addWidget(b, stretch=1)
            grille.addLayout(h)
        vc.addLayout(grille)

        self._btn_appeler = QPushButton("Appeler")
        self._btn_appeler.setCursor(Qt.PointingHandCursor)
        self._btn_appeler.setStyleSheet(
            f"QPushButton{{background:{_VERT};color:white;border:none;"
            f"border-radius:6px;padding:8px;font-size:10pt;font-weight:700;}}"
            f"QPushButton:disabled{{background:#c9ccd1;}}")
        self._btn_appeler.clicked.connect(self._appeler)
        vc.addWidget(self._btn_appeler)
        vc.addStretch(1)
        v.addWidget(self._page_clavier, stretch=1)

        # --- page historique ---
        self._sc_hist, _, self._lay_hist = _zone_defilante()
        self._sc_hist.setVisible(False)
        v.addWidget(self._sc_hist, stretch=1)

        self._rafraichir_saisie()

    # --- clavier ---

    def _touche(self, texte: str) -> QPushButton:
        b = QPushButton(texte)
        b.setCursor(Qt.PointingHandCursor)
        b.setMinimumHeight(30)
        b.setStyleSheet(
            f"QPushButton{{background:#f2f3f5;color:{_TXT};border:none;"
            f"border-radius:6px;font-size:13pt;}}"
            f"QPushButton:pressed{{background:#e2e4e8;}}")
        if texte == "\u232b":
            b.clicked.connect(self._effacer)
        else:
            b.clicked.connect(lambda _=False, c=texte: self._taper(c))
        return b

    def _taper(self, chiffre: str):
        """Touche de la grille cliquee a la souris."""
        if len(self._saisie) >= 6:
            return
        self._ed_num.setText(self._saisie + chiffre)

    def _effacer(self):
        self._ed_num.setText(self._saisie[:-1])

    def _sur_saisie(self, texte: str):
        """Le champ fait foi : la grille et le clavier physique y ecrivent
        tous les deux, ce qui evite deux sources de verite."""
        propre = "".join(c for c in (texte or "") if c.isdigit())[:6]
        if propre != texte:
            self._ed_num.setText(propre)
            return
        avant = self._saisie
        self._saisie = propre
        # Surbrillance de la touche correspondante : sans ce retour, le
        # pave a l'ecran paraitrait decoratif quand on tape au clavier.
        # Deduite du texte et non de la touche pressee, donc valable quel
        # que soit le clavier et sans intercepter la moindre frappe.
        if len(propre) > len(avant) and propre:
            self._eclairer(propre[-1])
        elif len(propre) < len(avant):
            self._eclairer("\u232b")
        self._rafraichir_saisie()

    def _eclairer(self, caractere: str):
        """Allume brievement une touche de la grille."""
        b = self._touches.get(caractere)
        if b is None:
            return
        b.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:white;border:none;"
            f"border-radius:6px;font-size:13pt;}}")
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(120, lambda bb=b: bb.setStyleSheet(
                f"QPushButton{{background:#f2f3f5;color:{_TXT};border:none;"
                f"border-radius:6px;font-size:13pt;}}"
                f"QPushButton:pressed{{background:#e2e4e8;}}"))
        except Exception:
            pass

    def _valider_saisie(self):
        if self._btn_appeler.isEnabled():
            self._appeler()

    def _rafraichir_saisie(self):
        self._erreur.setText("")
        rep = getattr(self.services, "repertoire", None)
        nom = None
        if rep is not None and len(self._saisie) == 6:
            try:
                nom = rep.nom_pour(self._saisie)
            except Exception:
                nom = None
        self._apercu.setText(nom or "")
        # Le bouton ne s'active que sur un numero plausible : 6 chiffres
        # commencant par 42. Inutile de laisser lancer un appel voue a
        # sonner dans le vide sur une faute de frappe evidente.
        valide = len(self._saisie) == 6 and self._saisie.startswith("42")
        self._btn_appeler.setEnabled(valide)

    def _appeler(self):
        try:
            num = normalise_numero(self._saisie)
        except ContactError as e:
            self._erreur.setText(str(e))
            return
        fn = getattr(self.services, "appeler", None)
        if fn is None:
            self._erreur.setText("Téléphone indisponible.")
            return
        try:
            fn(num)
        except Exception as e:
            self._erreur.setText(f"Échec : {e}")
            return
        self._ed_num.clear()

    # --- historique ---

    def _changer_onglet(self, i: int):
        clavier = (i == 0)
        self._page_clavier.setVisible(clavier)
        self._sc_hist.setVisible(not clavier)
        if not clavier:
            self._construire_historique()

    def _construire_historique(self):
        _vider(self._lay_hist)
        self._lignes_hist = []
        fn = getattr(self.services, "historique", None)
        entrees = []
        if fn is not None:
            try:
                entrees = list(fn() or [])
            except Exception:
                entrees = []
        if not entrees:
            self._numeros_hist = []
            # [CORRECTIF 19/08/2026] Meme correctif que la liste de
            # contacts vide : se poser sur la barre d'onglets et
            # REPEINDRE. La branche sortait par return, donc la barre
            # n'etait pas allumee et l'ecran paraissait mort.
            #
            # _numeros_hist est vide aussi : sans ca, il gardait les
            # numeros du dernier historique construit, alors que
            # _lignes_hist etait remis a zero -- les deux listes ne
            # correspondaient plus.
            self._idx_hist = -1
            self._action = 0
            self._nav_shown = True
            self._peindre_historique()
            return
        self._numeros_hist = []
        for e in entrees[:40]:
            w = self._ligne_historique(e)
            self._lay_hist.insertWidget(self._lay_hist.count() - 1, w)
            self._lignes_hist.append(w)
            self._numeros_hist.append(str(e.get("numero", "") or ""))
        self._idx_hist = 0
        self._action = 0
        self._nav_shown = True
        self._peindre_historique()

    def _ligne_historique(self, e: dict) -> QWidget:
        num = str(e.get("numero", "") or "?")
        rep = getattr(self.services, "repertoire", None)
        connu = False
        etiquette = num
        if rep is not None:
            try:
                nom = rep.nom_pour(num)
                connu = nom is not None
                etiquette = nom or num
            except Exception:
                pass

        ligne = _LigneNav(num)
        ligne.sig_clic.connect(self._appeler_numero)
        h = QHBoxLayout(ligne)
        h.setContentsMargins(6, 5, 6, 5)
        h.setSpacing(8)

        av = _Avatar(30)
        photo = None
        fn_photo = getattr(self.services, "photo_par_numero", None)
        if fn_photo is not None:
            try:
                photo = fn_photo(num)
            except Exception:
                photo = None
        av.definir(photo, etiquette)
        h.addWidget(av)

        colonne = QVBoxLayout()
        colonne.setSpacing(0)

        # [HISTORIQUE 10/08/2026] Fleche de sens devant le nom.
        #
        # Le sens figurait deja en toutes lettres dans le sous-titre
        # ("Sortant · manqué · 11:03"), mais il fallait le LIRE. Sur une
        # liste de dix appels, l'oeil cherche un motif, pas un mot. La
        # fleche se repere sans lecture ; le texte reste dessous pour
        # ceux qui distinguent mal les couleurs -- l'information n'est
        # donc portee par la couleur SEULE nulle part.
        #
        # Vers le HAUT et vert = je suis parti vers l'exterieur.
        # Vers le BAS et rouge = quelque chose est arrive vers moi.
        _sortant = (e.get("sens") == "out")
        ligne_titre = QHBoxLayout()
        ligne_titre.setSpacing(4)
        ligne_titre.setContentsMargins(0, 0, 0, 0)
        fleche = QLabel("\u2191" if _sortant else "\u2193")
        fleche.setStyleSheet(
            f"color:{_SORTANT if _sortant else _ENTRANT};"
            f"font-size:13pt;font-weight:bold;background:transparent;")
        fleche.setToolTip("Appel sortant" if _sortant else "Appel entrant")
        ligne_titre.addWidget(fleche)
        lbl = QLabel(etiquette)
        lbl.setStyleSheet(f"color:{_TXT};font-size:10pt;background:transparent;")
        ligne_titre.addWidget(lbl, stretch=1)
        colonne.addLayout(ligne_titre)
        sous = QLabel(self._sous_titre(e))
        sous.setStyleSheet(f"color:{_MUTED};font-size:8pt;background:transparent;")
        colonne.addWidget(sous)
        h.addLayout(colonne, stretch=1)

        # Bouton APPELER, toujours present : rappeler depuis l'historique
        # est l'action la plus frequente.
        b_app = _BoutonAppel(28)
        b_app.sig_clicked.connect(lambda n=num: self._appeler_numero(n))
        h.addWidget(b_app)
        ligne.ajouter_action(b_app, lambda n=num: self._appeler_numero(n))

        # "Ajouter" seulement si le numero n'est pas deja au carnet :
        # proposer d'ajouter un contact connu n'aurait pas de sens. La
        # ligne a donc 1 ou 2 actions selon le cas, et gauche/droite
        # circule entre celles qui existent.
        if not connu:
            b_add = _BoutonAction(
                "Ajouter",
                f"QPushButton{{background:{_BG};color:{_ACCENT};"
                f"border:1px solid {_ACCENT};border-radius:10px;"
                f"padding:3px 10px;font-size:8pt;}}")
            b_add.clicked.connect(lambda _=False, n=num: self._ajouter(n))
            h.addWidget(b_add)
            ligne.ajouter_action(b_add, lambda n=num: self._ajouter(n))

        return ligne

    def _appeler_numero(self, numero: str):
        fn = getattr(self.services, "appeler", None)
        if fn is not None and numero:
            try:
                fn(numero)
            except Exception:
                pass

    @staticmethod
    def _sous_titre(e: dict) -> str:
        sens = "Sortant" if e.get("sens") == "out" else "Entrant"
        issue = {
            "answered": "",
            "missed": " · manqué",
            "declined": " · refusé",
            "busy": " · occupé",
        }.get(str(e.get("issue", "")), "")
        quand = ""
        ts = e.get("ts")
        if ts:
            try:
                import time
                t = time.localtime(float(ts))
                maintenant = time.localtime()
                hm = time.strftime("%H:%M", t)
                quand = (hm if (t.tm_year, t.tm_yday)
                         == (maintenant.tm_year, maintenant.tm_yday)
                         else time.strftime("%d/%m ", t) + hm)
            except Exception:
                quand = ""
        return f"{sens}{issue}" + (f" · {quand}" if quand else "")

    def _ajouter(self, numero: str):
        fn = getattr(self.services, "ouvrir_ajout_contact", None)
        if fn is None:
            return
        try:
            fn(numero)
        except Exception:
            pass

    # --- navigation D-pad ---
    #
    # Modele repris de l'ecran conversation existant (_nav_convo) plutot
    # que reinvente : Entree sur un champ ENTRE dedans (focus Qt reel,
    # apres forcage de la fenetre au premier plan), Qt gere la frappe, et
    # Echap ressort. Aucune interception manuelle de touches -- l'essai
    # precedent, qui routait les chiffres a la main, echouait des qu'on
    # changeait de disposition clavier et ne permettait pas de taper des
    # lettres.
    #
    # Cibles : 0 = champ numero, 1 = bouton Appeler.

    def dans_champ(self) -> bool:
        """True quand un champ a le focus : le D-pad doit alors se taire."""
        return bool(self._dans_champ)

    def champ_courant_vide(self) -> bool:
        """True si le champ ou l'on se trouve est vide.

        Lu par le listener pour decider si le retour arriere efface un
        caractere ou ressort du champ.
        """
        if not self._dans_champ:
            return False
        try:
            return not self._ed_num.text()
        except Exception:
            return False

    def handle_nav(self, direction: str) -> bool:
        if self._dans_champ:
            # Meme regle que dans Contacts : Entree valide (et lance
            # l'appel si le numero est complet), Retour ne ressort que si
            # le champ est VIDE -- sinon il efface un chiffre, ce qui est
            # son role naturel dans un champ de saisie.
            if direction == "enter":
                self._sortir_du_champ()
                if self._btn_appeler.isEnabled():
                    self._appeler()
                else:
                    self._cible = 1
                    self._peindre_cible()
            elif direction == "esc" and not self._ed_num.text():
                self._sortir_du_champ()
            return True
        if self._onglets.courant() == 1:
            return self._nav_historique(direction)
        # Onglet Clavier. Cibles, de haut en bas :
        #   -1 = barre d'onglets, 0 = champ numero, 1 = bouton Appeler.
        # Les onglets se rejoignent en remontant, comme sur l'historique.
        if direction == "up":
            self._cible = max(-1, self._cible - 1)
            self._peindre_cible()
            return True
        if direction == "down":
            self._cible = min(1, self._cible + 1)
            self._peindre_cible()
            return True
        if direction in ("left", "right"):
            if self._cible < 0:
                self._basculer_onglet(0 if direction == "left" else 1)
            return True
        if direction == "enter":
            if self._cible < 0:
                self._basculer_onglet(1)
            elif self._cible == 0:
                self._entrer_dans_champ()
            elif self._btn_appeler.isEnabled():
                self._appeler()
            return True
        return False

    def _entrer_dans_champ(self):
        self._dans_champ = True
        self._ed_num.setVisible(True)
        self._ed_num.setText(self._saisie)
        ov = self.window()
        fn = getattr(ov, "entrer_dans_champ", None)
        if fn is not None:
            fn(self._ed_num)
        else:
            try:
                self._ed_num.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass
        self._peindre_cible()

    def _sortir_du_champ(self):
        self._dans_champ = False
        try:
            self._ed_num.clearFocus()
        except Exception:
            pass
        self._peindre_cible()

    def _nav_historique(self, direction: str) -> bool:
        """Navigation de l'onglet Historique.

        _idx_hist == -1 designe la BARRE D'ONGLETS. On y accede en
        remontant au-dela de la premiere ligne : les onglets sont
        visuellement au-dessus de la liste, la navigation suit donc la
        meme geometrie. C'est aussi le seul moyen de revenir au clavier
        sans quitter l'app.

        gauche/droite : change d'ACTION sur la ligne (appeler / ajouter)
        quand on est sur une ligne, change d'ONGLET quand on est sur la
        barre.
        """
        # La selection est visible des l'ouverture : pas de "revelation au
        # 1er appui", qui obligeait a appuyer deux fois pour bouger et
        # laissait l'ecran sans repere visuel a l'arrivee.
        if direction == "up":
            if self._idx_hist <= 0:
                self._idx_hist = -1             # remonte sur les onglets
            else:
                self._idx_hist -= 1
            self._action = 0
        elif direction == "down":
            if self._idx_hist < len(self._lignes_hist) - 1:
                self._idx_hist += 1
            self._action = 0
        elif direction in ("left", "right"):
            if self._idx_hist < 0:
                self._basculer_onglet(0 if direction == "left" else 1)
                return True
            n = self._nb_actions(self._idx_hist)
            if n > 1:
                self._action = ((self._action + (1 if direction == "right" else -1))
                                % n)
        elif direction == "enter":
            if self._idx_hist < 0:
                self._basculer_onglet(0)
                return True
            self._nav_shown = True
            if 0 <= self._idx_hist < len(self._lignes_hist):
                self._lignes_hist[self._idx_hist].declencher(self._action)
            return True
        self._ensure_hist_visible()
        self._peindre_historique()
        return True

    def _basculer_onglet(self, i: int):
        """Change d'onglet en LAISSANT le curseur sur la barre.

        Sans ca, passer d'Historique a Clavier reposait le curseur dans
        le champ numero : la fleche droite n'avait alors plus d'effet et
        on ne pouvait plus revenir a Historique au clavier. Le curseur
        doit rester la ou il est visuellement -- sur la barre -- tant
        qu'on n'a pas redescendu.
        """
        self._onglets.selectionner(i)
        self._nav_shown = True
        self._cible = -1        # onglet Clavier : sur la barre
        self._idx_hist = -1     # onglet Historique : sur la barre
        self._action = 0
        self._peindre_cible()
        self._peindre_historique()

    def _nb_actions(self, i: int) -> int:
        try:
            return self._lignes_hist[i].nb_actions()
        except Exception:
            return 1

    def _peindre_cible(self):
        """Liseré sur la cible : sans reperage visuel, la navigation au
        clavier serait aveugle."""
        actif = self._dans_champ
        try:
            self._onglets.set_nav_highlight(self._cible < 0)
        except Exception:
            pass
        self._ed_num.setStyleSheet(
            f"QLineEdit{{background:{_BG};color:{_TXT};font-size:20pt;"
            f"font-weight:600;letter-spacing:2px;padding:2px;"
            f"border:2px solid "
            f"{_VERT if actif else (_ACCENT if self._cible == 0 else 'transparent')};"
            f"border-radius:6px;}}")
        self._btn_appeler.setStyleSheet(
            f"QPushButton{{background:{_VERT};color:white;"
            f"border:{'2px solid ' + _ACCENT if self._cible == 1 else 'none'};"
            f"border-radius:6px;padding:8px;font-size:10pt;font-weight:700;}}"
            f"QPushButton:disabled{{background:#c9ccd1;}}")

    def _peindre_historique(self):
        """[AUDIT 02/08/2026] Passe par set_nav_highlight comme les listes
        v0.3, et non par un setStyleSheet direct : la couleur est celle du
        reste du telephone, et la ligne peint bien son fond
        (WA_StyledBackground, cf _LigneNav)."""
        for i, w in enumerate(self._lignes_hist):
            try:
                w.set_nav_highlight(
                    self._nav_shown and i == self._idx_hist, self._action)
            except Exception:
                pass
        try:
            self._onglets.set_nav_highlight(
                self._nav_shown and self._idx_hist < 0)
        except Exception:
            pass

    def _ensure_hist_visible(self):
        """Fait defiler pour garder la ligne selectionnee a l'ecran.
        Sans ca, la selection sort du cadre des la 6e ligne."""
        try:
            if 0 <= self._idx_hist < len(self._lignes_hist):
                self._sc_hist.ensureWidgetVisible(
                    self._lignes_hist[self._idx_hist])
        except Exception:
            pass

    def handle_back(self) -> bool:
        if self._dans_champ:
            self._sortir_du_champ()
            return True
        if self._onglets.courant() == 1:
            self._onglets.selectionner(0)
            self._cible = 0
            self._peindre_cible()
            return True
        return False

    # --- cycle de vie ---

    def on_show(self):
        if self._onglets.courant() == 1:
            self._construire_historique()
        else:
            self._rafraichir_saisie()
        self._peindre_cible()


# ======================================================================
#  Contacts : carnet local + ajout
# ======================================================================

class ContactsApp(PhoneApp):

    APP_ID   = "contacts"
    APP_NAME = "Contacts"
    APP_ICON = "\U0001f4c7"

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.setStyleSheet(f"background:{_BG};")
        self._lignes: list[QWidget] = []
        self._numeros: list[str] = []
        self._idx = 0
        self._action = 0
        self._cible = 0
        self._dans_champ = False
        self._nav_shown = True

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)
        v.addWidget(_titre("Contacts"))

        self._onglets = _Onglets(["Mes contacts", "Ajouter"],
                                 self._changer_onglet)
        v.addWidget(self._onglets)

        self._sc_liste, _, self._lay_liste = _zone_defilante()
        v.addWidget(self._sc_liste, stretch=1)

        # --- page ajout ---
        self._page_ajout = QWidget()
        va = QVBoxLayout(self._page_ajout)
        va.setContentsMargins(4, 14, 4, 0)
        va.setSpacing(6)

        va.addWidget(self._etiquette("Nom"))
        self._ed_nom = QLineEdit()
        self._ed_nom.setMaxLength(20)
        self._ed_nom.setStyleSheet(self._style_champ())
        self._ed_nom.textChanged.connect(self._verifier_ajout)
        va.addWidget(self._ed_nom)

        va.addSpacing(8)
        va.addWidget(self._etiquette("Numéro de téléphone"))
        self._ed_num = QLineEdit()
        self._ed_num.setMaxLength(9)      # tolere les espaces de saisie
        self._ed_num.setPlaceholderText("42••••")
        self._ed_num.setStyleSheet(self._style_champ())
        self._ed_num.textChanged.connect(self._verifier_ajout)
        va.addWidget(self._ed_num)

        self._msg_ajout = QLabel("")
        self._msg_ajout.setWordWrap(True)
        self._msg_ajout.setStyleSheet(f"color:{_ROUGE};font-size:8pt;")
        va.addWidget(self._msg_ajout)

        va.addStretch(1)
        self._btn_ajouter = QPushButton("Ajouter contact")
        self._btn_ajouter.setCursor(Qt.PointingHandCursor)
        self._btn_ajouter.setEnabled(False)
        self._btn_ajouter.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:white;border:none;"
            f"border-radius:6px;padding:9px;font-size:10pt;font-weight:700;}}"
            f"QPushButton:disabled{{background:#c9ccd1;}}")
        self._btn_ajouter.clicked.connect(self._ajouter)
        va.addWidget(self._btn_ajouter)
        self._page_ajout.setVisible(False)
        v.addWidget(self._page_ajout, stretch=1)

        self._construire_liste()

    @staticmethod
    def _etiquette(texte: str) -> QLabel:
        lbl = QLabel(texte)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color:{_TXT};font-size:10pt;")
        return lbl

    @staticmethod
    def _style_champ() -> str:
        return (f"QLineEdit{{background:{_BG};color:{_TXT};"
                f"border:1px solid #c9ccd1;border-radius:4px;"
                f"padding:6px;font-size:10pt;}}")

    # --- liste ---

    def _changer_onglet(self, i: int):
        liste = (i == 0)
        self._sc_liste.setVisible(liste)
        self._page_ajout.setVisible(not liste)
        if liste:
            self._construire_liste()

    def _construire_liste(self):
        _vider(self._lay_liste)
        rep = getattr(self.services, "repertoire", None)
        entrees = []
        if rep is not None:
            try:
                entrees = rep.liste()
            except Exception:
                entrees = []
        if not entrees:
            self._lignes = []
            self._numeros = []
            self._lay_liste.insertWidget(0, _message_vide(
                "Aucun contact.\n\nAjoutez un numéro depuis l'onglet "
                "« Ajouter », ou depuis l'historique des appels."))
            # [CORRECTIF 19/08/2026] Liste vide : se poser sur la BARRE
            # D'ONGLETS (_idx == -1) au lieu du premier contact.
            #
            # _idx valait 0, qui designe une ligne inexistante : rien ne
            # s'allumait, et Entree ne faisait rien. Il fallait deviner
            # qu'il faut appuyer sur HAUT pour atteindre les onglets --
            # or c'est justement l'ecran ou le joueur n'a encore rien et
            # doit aller vers « Ajouter ».
            #
            # Le _peindre_liste() manquait aussi : la branche sortait par
            # return, donc meme la barre ne se repeignait pas.
            self._idx = -1
            self._peindre_liste()
            return
        self._lignes = []
        self._numeros = []
        for num, nom in entrees:
            w = self._ligne(num, nom)
            self._lay_liste.insertWidget(self._lay_liste.count() - 1, w)
            self._lignes.append(w)
            self._numeros.append(num)
        self._idx = min(self._idx, max(0, len(self._lignes) - 1))
        self._peindre_liste()

    def _ligne(self, numero: str, nom: str) -> QWidget:
        ligne = _LigneNav(numero)
        ligne.sig_clic.connect(self._appeler)
        h = QHBoxLayout(ligne)
        h.setContentsMargins(6, 5, 6, 5)
        h.setSpacing(8)

        av = _Avatar(30)
        photo = None
        fn = getattr(self.services, "photo_par_numero", None)
        if fn is not None:
            try:
                photo = fn(numero)
            except Exception:
                photo = None
        av.definir(photo, nom)
        h.addWidget(av)

        colonne = QVBoxLayout()
        colonne.setSpacing(0)
        l1 = QLabel(nom)
        l1.setStyleSheet(
            f"color:{_TXT};font-size:10pt;font-weight:600;background:transparent;")
        colonne.addWidget(l1)
        l2 = QLabel(numero)
        l2.setStyleSheet(f"color:{_MUTED};font-size:8pt;background:transparent;")
        colonne.addWidget(l2)
        h.addLayout(colonne, stretch=1)

        b_app = _BoutonAppel(28)
        b_app.sig_clicked.connect(lambda n=numero: self._appeler(n))
        h.addWidget(b_app)
        ligne.ajouter_action(b_app, lambda n=numero: self._appeler(n))

        b_sup = _BoutonAction(
            "\u00d7",
            f"QPushButton{{background:transparent;color:{_ROUGE};border:none;"
            f"font-size:13pt;font-weight:700;}}")
        b_sup.setFixedSize(QSize(24, 24))
        b_sup.clicked.connect(lambda _=False, n=numero: self._supprimer(n))
        h.addWidget(b_sup)
        ligne.ajouter_action(b_sup, lambda n=numero: self._supprimer(n))

        return ligne

    def _appeler(self, numero: str):
        fn = getattr(self.services, "appeler", None)
        if fn is not None:
            try:
                fn(numero)
            except Exception:
                pass

    def _supprimer(self, numero: str):
        rep = getattr(self.services, "repertoire", None)
        if rep is None:
            return
        try:
            rep.supprimer(numero)
        except Exception:
            return
        self._construire_liste()

    # --- ajout ---

    def _verifier_ajout(self):
        self._msg_ajout.setText("")
        nom = self._ed_nom.text().strip()
        try:
            normalise_numero(self._ed_num.text())
            num_ok = True
        except ContactError:
            num_ok = False
        self._btn_ajouter.setEnabled(bool(nom) and num_ok)

    def _ajouter(self):
        rep = getattr(self.services, "repertoire", None)
        if rep is None:
            self._msg_ajout.setText("Répertoire indisponible.")
            return
        try:
            rep.ajouter(self._ed_nom.text(), self._ed_num.text())
        except ContactError as e:
            self._msg_ajout.setText(str(e))
            return
        except Exception as e:
            self._msg_ajout.setText(f"Échec : {e}")
            return
        self._ed_nom.clear()
        self._ed_num.clear()
        self._msg_ajout.setText("")
        # Retour a la liste : l'ajout est termine, montrer le resultat
        # vaut mieux que laisser un formulaire vide.
        self._onglets.selectionner(0)

    def prefixer_numero(self, numero: str):
        """Ouvre l'onglet d'ajout avec le numero pre-rempli.

        Appele par le bouton « Ajouter » de l'historique : le joueur n'a
        plus qu'a saisir le nom.
        """
        self._ed_num.setText(str(numero or ""))
        self._ed_nom.clear()
        self._msg_ajout.setText("")
        self._onglets.selectionner(1)
        self._verifier_ajout()
        try:
            self._ed_nom.setFocus()
        except Exception:
            pass

    # --- navigation D-pad ---
    #
    # Meme modele que l'app Appels et que l'ecran conversation : Entree
    # sur un champ y ENTRE (focus Qt reel), Qt gere la frappe -- lettres
    # comprises, ce qui rend le champ Nom utilisable --, Echap ressort.
    #
    # Onglet liste : haut/bas parcourt, Entree appelle.
    # Onglet ajout : 0 = Nom, 1 = Numero, 2 = bouton Ajouter.

    def dans_champ(self) -> bool:
        return bool(self._dans_champ)

    def champ_courant_vide(self) -> bool:
        if not self._dans_champ:
            return False
        try:
            w = self._ed_nom if self._cible == 0 else self._ed_num
            return not w.text()
        except Exception:
            return False

    def handle_nav(self, direction: str) -> bool:
        if self._dans_champ:
            # [CONTACTS 31/07/2026] Dans un champ :
            #   Entree        -> valide et passe au champ suivant.
            #   Retour, champ VIDE -> ressort du champ.
            #   Retour, champ REMPLI -> laisse effacer un caractere.
            # Sans cette regle, sortir d'un champ rempli mangeait la
            # derniere lettre du nom qu'on venait de taper : Retour est
            # a la fois la touche "retour" du telephone et la touche
            # d'effacement du champ.
            if direction == "enter":
                self._valider_champ()
            elif direction == "esc" and not self._texte_cible():
                self._sortir_du_champ()
            return True
        if self._onglets.courant() == 0:
            return self._nav_liste(direction)
        return self._nav_ajout(direction)

    def _texte_cible(self) -> str:
        w = self._ed_nom if self._cible == 0 else self._ed_num
        try:
            return w.text()
        except Exception:
            return ""

    def _valider_champ(self):
        """Entree dans un champ : enchaine sur la cible suivante.

        Nom -> Numero -> bouton Ajouter. Sur le bouton, Entree ajoute
        directement : on ne repasse donc jamais par Retour pour remplir
        un contact.
        """
        if self._cible == 0:
            self._cible = 1
            self._entrer_dans_champ()
        else:
            # Depuis le champ Numero : on sort du mode saisie et on se
            # pose sur le bouton Ajouter. _sortir_du_champ APRES avoir
            # change de cible, sinon _dans_champ resterait vrai et
            # l'Entree suivante repasserait par _valider_champ au lieu
            # d'ajouter le contact.
            self._cible = 2
            self._sortir_du_champ()

    def _entrer_dans_champ(self):
        widget = self._ed_nom if self._cible == 0 else self._ed_num
        self._dans_champ = True
        ov = self.window()
        fn = getattr(ov, "entrer_dans_champ", None)
        if fn is not None:
            fn(widget)
        else:
            try:
                widget.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass
        self._peindre_cible()

    def _sortir_du_champ(self):
        self._dans_champ = False
        for w in (self._ed_nom, self._ed_num):
            try:
                w.clearFocus()
            except Exception:
                pass
        self._peindre_cible()

    def _nav_liste(self, direction: str) -> bool:
        """_idx == -1 designe la barre d'onglets, atteinte en remontant
        au-dela du premier contact. gauche/droite change d'ACTION sur la
        ligne (appeler / supprimer), ou d'ONGLET sur la barre."""
        if direction == "up":
            if self._idx <= 0:
                self._idx = -1
            else:
                self._idx -= 1
            self._action = 0
        elif direction == "down":
            if self._idx < len(self._lignes) - 1:
                self._idx += 1
            self._action = 0
        elif direction in ("left", "right"):
            if self._idx < 0:
                self._basculer_onglet(0 if direction == "left" else 1)
                return True
            n = 1
            try:
                n = self._lignes[self._idx].nb_actions()
            except Exception:
                pass
            if n > 1:
                self._action = ((self._action + (1 if direction == "right" else -1))
                                % n)
        elif direction == "enter":
            if self._idx < 0:
                self._basculer_onglet(1)
                return True
            self._nav_shown = True
            if 0 <= self._idx < len(self._lignes):
                self._lignes[self._idx].declencher(self._action)
            return True
        self._ensure_liste_visible()
        self._peindre_liste()
        return True

    def _nav_ajout(self, direction: str) -> bool:
        # -1 = barre d'onglets, 0 = Nom, 1 = Numero, 2 = bouton Ajouter.
        if direction == "up":
            self._cible = max(-1, self._cible - 1)
            self._peindre_cible()
        elif direction == "down":
            self._cible = min(2, self._cible + 1)
            self._peindre_cible()
        elif direction in ("left", "right"):
            if self._cible < 0:
                self._basculer_onglet(0 if direction == "left" else 1)
        elif direction == "enter":
            if self._cible < 0:
                self._basculer_onglet(0)
            elif self._cible in (0, 1):
                self._entrer_dans_champ()
            elif self._btn_ajouter.isEnabled():
                self._ajouter()
        return True

    def _basculer_onglet(self, i: int):
        """Change d'onglet en laissant le curseur sur la barre (cf. la
        meme methode dans AppelsApp)."""
        self._onglets.selectionner(i)
        self._nav_shown = True
        self._cible = -1
        self._idx = -1
        self._action = 0
        self._peindre_cible()
        self._peindre_liste()

    def _peindre_cible(self):
        actif = self._dans_champ
        try:
            self._onglets.set_nav_highlight(self._cible < 0)
        except Exception:
            pass
        for i, w in enumerate((self._ed_nom, self._ed_num)):
            selectionne = (self._cible == i)
            couleur = (_VERT if (actif and selectionne)
                       else (_ACCENT if selectionne else "#c9ccd1"))
            epaisseur = 2 if selectionne else 1
            w.setStyleSheet(
                f"QLineEdit{{background:{_BG};color:{_TXT};"
                f"border:{epaisseur}px solid {couleur};"
                f"border-radius:4px;padding:6px;font-size:10pt;}}")
        self._btn_ajouter.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:white;"
            f"border:{'2px solid ' + _TXT if self._cible == 2 else 'none'};"
            f"border-radius:6px;padding:9px;font-size:10pt;font-weight:700;}}"
            f"QPushButton:disabled{{background:#c9ccd1;}}")

    def _peindre_liste(self):
        for i, w in enumerate(self._lignes):
            try:
                w.set_nav_highlight(
                    self._nav_shown and i == self._idx, self._action)
            except Exception:
                pass
        try:
            self._onglets.set_nav_highlight(self._nav_shown and self._idx < 0)
        except Exception:
            pass

    def _ensure_liste_visible(self):
        try:
            if 0 <= self._idx < len(self._lignes):
                self._sc_liste.ensureWidgetVisible(self._lignes[self._idx])
        except Exception:
            pass

    def handle_back(self) -> bool:
        if self._dans_champ:
            self._sortir_du_champ()
            return True
        if self._onglets.courant() == 1:
            self._onglets.selectionner(0)
            return True
        return False

    def on_show(self):
        if self._onglets.courant() == 0:
            self._construire_liste()
        self._peindre_cible()


# ======================================================================
#  Messagerie : liste des conversations + nouvelle conversation
# ======================================================================

class MessagerieApp(PhoneApp):
    """Liste des CONVERSATIONS, et rien d'autre.

    [CONTACTS 02/08/2026] Remplace l'ecran natif en mode "message", qui
    listait les joueurs connectes du serveur : savoir qui est en ligne
    n'a pas a etre accessible aux joueurs (decision du 30/07). On ne voit
    donc ici que les gens avec qui on a REELLEMENT echange, plus un
    bouton pour amorcer une conversation a partir d'un numero.

    L'ecran de CONVERSATION lui-meme (bulles, envoi d'images, brouillon,
    visionneuse) n'est pas reecrit : cette app se contente de l'ouvrir
    via services.ouvrir_conversation. C'est plusieurs centaines de lignes
    qui fonctionnent, et les refaire reintroduirait les defauts qu'on
    vient de corriger ailleurs.

    Pas de roue dentee : les reglages de profil ont leur propre app.

    --- [GROUPES 19/08/2026] Les groupes vivent dans la MEME liste ---

    Un groupe n'a pas d'ecran a lui : sa conversation est rangee sous la
    cle "G:<id>", dans le meme dictionnaire que les conversations
    directes, et s'ouvre par le meme ecran. C'est ce qui permet de ne
    rien reecrire -- bulles, brouillon et defilement fonctionnent deja.

    Ce que cette app attend de `services`, en plus de l'existant :

      groupes()                  -> [{"id","nom","membres"}, ...]
      creer_groupe(nom, membres) -> None (envoi asynchrone au serveur)

    Les deux sont optionnels : absents, le bouton « Nouveau groupe »
    n'apparait pas et rien d'autre ne change.
    """

    APP_ID   = "messagerie"
    APP_NAME = "Messagerie"
    APP_ICON = "\u2709"

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.setStyleSheet(f"background:{_BG};")
        self._lignes: list = []
        self._numeros: list[str] = []
        self._idx = 0
        # Toujours vrai : la selection est visible des l'ouverture.
        self._nav_shown = True
        self._sur_bouton = True     # curseur sur la barre de boutons
        self._cible_form = 0        # dans le formulaire : combo/champ/bouton
        self._liste_ouverte = False
        self._page_nouveau = False
        self._dans_champ = False
        # [GROUPES 19/08/2026] Il y a maintenant DEUX boutons en tete :
        # _sur_bouton dit qu'on est dans la barre, _idx_bouton dit lequel.
        # 0 = Nouvelle conversation, 1 = Nouveau groupe.
        self._idx_bouton = 0
        self._page_groupe = False
        self._cible_grp = 0          # 0 = nom, 1 = contacts, 2 = Creer
        self._grp_choisis: list[str] = []
        self._grp_idx = 0
        self._grp_lignes: list = []
        self._grp_numeros: list[str] = []
        # Action visee SUR la ligne courante (gauche/droite). Seules les
        # lignes de groupe en ont une : la croix « quitter ».
        self._action = 0
        # Confirmation en cours : identifiant du groupe, ou "".
        self._confirm_gid = ""

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(6)
        v.addWidget(_titre("Messagerie"))

        # Section 1 : amorcer une conversation.
        self._btn_nouveau = QPushButton("Nouvelle conversation")
        self._btn_nouveau.setCursor(Qt.PointingHandCursor)
        self._btn_nouveau.clicked.connect(self._ouvrir_nouveau)
        v.addWidget(self._btn_nouveau)

        # [GROUPES 19/08/2026] Affiche seulement si le module de regles et
        # le service sont la. Un bouton menant a un ecran incapable de
        # creer quoi que ce soit est pire qu'un bouton absent : le joueur
        # croit a une panne et recommence.
        self._btn_groupe = QPushButton("Nouveau groupe")
        self._btn_groupe.setCursor(Qt.PointingHandCursor)
        self._btn_groupe.clicked.connect(self._ouvrir_groupe)
        # [GROUPES 19/08/2026] L'etat est tenu dans un ATTRIBUT, pas lu
        # par isVisible(). Un QWidget dont le parent n'est pas encore
        # affiche rend isVisible() == False meme apres setVisible(True) :
        # la navigation aurait compte un seul bouton, et « Nouveau
        # groupe » aurait ete inatteignable au clavier -- exactement le
        # defaut des croix de retrait corrige au build 73.
        self._grp_dispo = self._groupes_actifs()
        self._btn_groupe.setVisible(self._grp_dispo)
        v.addWidget(self._btn_groupe)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{_SEP};")
        v.addWidget(sep)

        # Section 2 : les conversations.
        self._sc_liste, _, self._lay_liste = _zone_defilante()
        v.addWidget(self._sc_liste, stretch=1)

        # Page "nouvelle conversation" : un numero, c'est tout.
        self._page_form = QWidget()
        vf = QVBoxLayout(self._page_form)
        vf.setContentsMargins(4, 14, 4, 0)
        vf.setSpacing(6)
        # Deux facons d'amorcer une conversation : choisir un contact
        # deja enregistre, ou taper un numero qu'on vient de recevoir.
        # Les deux coexistent -- le carnet ne contient pas forcement la
        # personne a qui on veut ecrire.
        lbl_c = QLabel("Choisir un contact")
        lbl_c.setAlignment(Qt.AlignCenter)
        lbl_c.setStyleSheet(f"color:{_TXT};font-size:10pt;")
        vf.addWidget(lbl_c)
        self._cmb = QComboBox()
        self._cmb.currentIndexChanged.connect(self._sur_contact_choisi)
        self._cmb.activated.connect(self._sur_contact_valide)
        vf.addWidget(self._cmb)

        vf.addSpacing(6)
        lbl = QLabel("ou saisir un numéro")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color:{_MUTED};font-size:9pt;")
        vf.addWidget(lbl)
        self._ed_num = QLineEdit()
        self._ed_num.setAlignment(Qt.AlignCenter)
        self._ed_num.setMaxLength(6)
        self._ed_num.setPlaceholderText("42••••")
        self._ed_num.textChanged.connect(self._verifier)
        vf.addWidget(self._ed_num)
        self._msg = QLabel("")
        self._msg.setWordWrap(True)
        self._msg.setStyleSheet(f"color:{_ROUGE};font-size:8pt;")
        vf.addWidget(self._msg)
        vf.addStretch(1)
        self._btn_demarrer = QPushButton("Démarrer")
        self._btn_demarrer.setCursor(Qt.PointingHandCursor)
        self._btn_demarrer.setEnabled(False)
        self._btn_demarrer.clicked.connect(self._demarrer)
        vf.addWidget(self._btn_demarrer)
        self._page_form.setVisible(False)
        v.addWidget(self._page_form, stretch=1)

        # --- [GROUPES 19/08/2026] Page "nouveau groupe" ---
        #
        # Les membres se prennent dans le CARNET, pas au clavier. Un
        # groupe se compose de gens qu'on connait deja ; taper cinq
        # numeros de memoire serait une source d'erreur sans contrepartie,
        # et une faute de frappe ne serait jamais rattrapable puisque la
        # composition est FIGEE a la creation.
        self._page_grp = QWidget()
        vg = QVBoxLayout(self._page_grp)
        vg.setContentsMargins(4, 10, 4, 0)
        vg.setSpacing(5)
        lbl_n = QLabel("Nom du groupe")
        lbl_n.setAlignment(Qt.AlignCenter)
        lbl_n.setStyleSheet(f"color:{_TXT};font-size:10pt;")
        vg.addWidget(lbl_n)
        self._ed_nom = QLineEdit()
        self._ed_nom.setAlignment(Qt.AlignCenter)
        self._ed_nom.setMaxLength(
            _GRP.NOM_MAX_LEN if _GRP is not None else 40)
        # Placeholder NEUTRE : un exemple concret ("Les amis") oriente
        # l'usage vers un seul type de groupe, alors que rien dans le
        # code ne le restreint.
        self._ed_nom.setPlaceholderText("...")
        self._ed_nom.textChanged.connect(self._verifier_grp)
        vg.addWidget(self._ed_nom)

        self._lbl_membres = QLabel("Membres")
        self._lbl_membres.setAlignment(Qt.AlignCenter)
        self._lbl_membres.setStyleSheet(f"color:{_MUTED};font-size:9pt;")
        vg.addWidget(self._lbl_membres)
        self._sc_grp, _, self._lay_grp = _zone_defilante()
        vg.addWidget(self._sc_grp, stretch=1)

        self._msg_grp = QLabel("")
        self._msg_grp.setWordWrap(True)
        self._msg_grp.setStyleSheet(f"color:{_ROUGE};font-size:8pt;")
        vg.addWidget(self._msg_grp)
        self._btn_creer = QPushButton("Créer")
        self._btn_creer.setCursor(Qt.PointingHandCursor)
        self._btn_creer.setEnabled(False)
        self._btn_creer.clicked.connect(self._creer_groupe)
        vg.addWidget(self._btn_creer)
        self._page_grp.setVisible(False)
        v.addWidget(self._page_grp, stretch=1)

        # --- [GROUPES 19/08/2026] Confirmation de sortie ---
        #
        # Une PAGE, pas une boite de dialogue : l'overlay porte le flag
        # Qt.Tool, et une fenetre modale par-dessus lui ne recevrait pas
        # le clavier -- elle serait inutilisable au D-pad.
        #
        # Le NOM du groupe est repete dans la question : la croix fait 24
        # pixels et deux groupes peuvent porter le meme nom. Confirmer
        # sans savoir lequel on quitte serait pire que pas de
        # confirmation du tout.
        self._page_conf = QWidget()
        vc = QVBoxLayout(self._page_conf)
        vc.setContentsMargins(10, 20, 10, 10)
        vc.setSpacing(10)
        vc.addStretch(1)
        self._lbl_conf = QLabel("")
        self._lbl_conf.setWordWrap(True)
        self._lbl_conf.setAlignment(Qt.AlignCenter)
        self._lbl_conf.setStyleSheet(f"color:{_TXT};font-size:11pt;")
        vc.addWidget(self._lbl_conf)
        _lbl_av = QLabel("Les autres membres en seront informés. Vous ne "
                         "pourrez pas revenir : un groupe ne se rejoint "
                         "pas.")
        _lbl_av.setWordWrap(True)
        _lbl_av.setAlignment(Qt.AlignCenter)
        _lbl_av.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        vc.addWidget(_lbl_av)
        self._btn_conf_oui = QPushButton("Quitter le groupe")
        self._btn_conf_oui.setCursor(Qt.PointingHandCursor)
        self._btn_conf_oui.clicked.connect(self._confirmer_sortie)
        vc.addWidget(self._btn_conf_oui)
        self._btn_conf_non = QPushButton("Annuler")
        self._btn_conf_non.setCursor(Qt.PointingHandCursor)
        self._btn_conf_non.clicked.connect(self._annuler_sortie)
        vc.addWidget(self._btn_conf_non)
        vc.addStretch(1)
        self._page_conf.setVisible(False)
        v.addWidget(self._page_conf, stretch=1)

        self._peindre_boutons()
        self._construire_liste()

    # --- [GROUPES 19/08/2026] groupes ---

    def _groupes_actifs(self) -> bool:
        """Vrai si les groupes sont utilisables de bout en bout.

        Exige le module de REGLES et le service de creation : l'un sans
        l'autre donne un ecran qui valide sans pouvoir envoyer, ou qui
        envoie sans avoir valide.
        """
        return (_GRP is not None
                and getattr(self.services, "creer_groupe", None) is not None)

    def _groupes(self) -> dict:
        """Groupes du joueur, indexes par CLE de conversation.

        Rend un dict plutot qu'une liste : la liste des conversations le
        consulte une fois par ligne, et une recherche lineaire par ligne
        ferait du quadratique sur une liste deja longue.
        """
        fn = getattr(self.services, "groupes", None)
        if fn is None or _GRP is None:
            return {}
        try:
            return {_GRP.cle_conversation(g.get("id")): g
                    for g in (fn() or []) if g.get("id")}
        except Exception:
            return {}

    # --- liste ---

    def _construire_liste(self):
        _vider(self._lay_liste)
        self._lignes = []
        self._numeros = []
        fn = getattr(self.services, "conversations", None)
        entrees = []
        if fn is not None:
            try:
                entrees = list(fn() or [])
            except Exception:
                entrees = []
        if not entrees:
            self._lay_liste.insertWidget(0, _message_vide(
                "Aucune conversation.\n\nUtilisez « Nouvelle conversation » "
                "pour écrire à un numéro."))
            return
        groupes = self._groupes()
        for e in entrees:
            num = str(e.get("numero", "") or "")
            if not num:
                continue
            w = self._ligne(num, bool(e.get("non_lu")), groupes.get(num))
            self._lay_liste.insertWidget(self._lay_liste.count() - 1, w)
            self._lignes.append(w)
            self._numeros.append(num)
        self._idx = min(self._idx, max(0, len(self._lignes) - 1))
        self._peindre_liste()

    def _ligne(self, numero: str, non_lu: bool, groupe=None) -> QWidget:
        """Ligne de conversation. `groupe` la transforme en ligne de groupe.

        Une conversation de groupe a pour cle "G:<id>", qui n'est pas un
        numero : l'afficher telle quelle donnerait « G:4f2a... » dans la
        liste. Quand le groupe est CONNU, on montre son nom ; quand il ne
        l'est pas -- groupe quitte, ou etat pas encore recu du serveur --
        on montre un libelle neutre plutot que la cle brute.
        """
        est_grp = _GRP is not None and _GRP.est_cle_groupe(numero)
        rep = getattr(self.services, "repertoire", None)
        if est_grp:
            etiquette = (groupe or {}).get("nom") or "Groupe"
        else:
            etiquette = numero
            if rep is not None:
                try:
                    etiquette = rep.afficher(numero)
                except Exception:
                    pass

        ligne = _LigneNav(numero)
        ligne.sig_clic.connect(self._ouvrir_conversation)
        h = QHBoxLayout(ligne)
        h.setContentsMargins(6, 5, 6, 5)
        h.setSpacing(8)

        av = _Avatar(30)
        photo = None
        if not est_grp:
            # Un groupe n'a pas de photo : photo_par_numero() recevrait
            # "G:4f2a..." et chercherait un joueur de ce numero. L'avatar
            # tombe donc sur son initiale, ici celle du nom du groupe.
            fn = getattr(self.services, "photo_par_numero", None)
            if fn is not None:
                try:
                    photo = fn(numero)
                except Exception:
                    photo = None
        av.definir(photo, etiquette)
        h.addWidget(av)

        if est_grp:
            # Deux lignes : le nom, puis les membres. Sans le second, deux
            # groupes homonymes seraient indistinguables -- et rien
            # n'empeche un joueur d'en creer deux.
            bloc = QWidget()
            bloc.setStyleSheet("background:transparent;")
            vb = QVBoxLayout(bloc)
            vb.setContentsMargins(0, 0, 0, 0)
            vb.setSpacing(0)
            lbl = QLabel(etiquette)
            lbl.setStyleSheet(
                f"color:{_TXT};font-size:10pt;background:transparent;"
                f"font-weight:{'700' if non_lu else '400'};")
            vb.addWidget(lbl)
            sous = self._resume_groupe(groupe)
            if sous:
                lbl2 = QLabel(sous)
                lbl2.setStyleSheet(
                    f"color:{_MUTED};font-size:8pt;background:transparent;")
                vb.addWidget(lbl2)
            h.addWidget(bloc, stretch=1)
            # [GROUPES 19/08/2026] Croix de sortie. Declaree comme ACTION
            # de la ligne, pas seulement posee dedans : c'est le mecanisme
            # qui la rend atteignable au clavier par gauche/droite. Une
            # croix ajoutee sans ajouter_action() serait cliquable a la
            # souris et invisible au D-pad -- le defaut corrige au build
            # 73, qu'il ne faut pas reintroduire.
            if groupe is not None:
                # 32 px et 18 pt : a 24/13 (la taille des croix de
                # Contacts) le glyphe etait illisible sur une ligne de
                # groupe, plus haute a cause du sous-titre -- il passait
                # pour une tache rouge. Le fond gris clair lui donne une
                # surface franche a viser a la souris.
                b_out = _BoutonAction(
                    "\u2715",
                    f"QPushButton{{background:#f2f3f5;color:{_ROUGE};"
                    f"border:none;border-radius:16px;font-size:14pt;"
                    f"font-weight:700;}}")
                b_out.setFixedSize(QSize(32, 32))
                gid = str(groupe.get("id") or "")
                b_out.clicked.connect(
                    lambda _=False, g=gid: self._demander_sortie(g))
                h.addWidget(b_out)
                ligne.ajouter_action(
                    b_out, lambda g=gid: self._demander_sortie(g))
        else:
            lbl = QLabel(etiquette)
            lbl.setStyleSheet(
                f"color:{_TXT};font-size:10pt;background:transparent;"
                f"font-weight:{'700' if non_lu else '400'};")
            h.addWidget(lbl, stretch=1)

        if non_lu:
            # Pastille de non-lu, comme sur l'ecran natif.
            pastille = QLabel()
            pastille.setFixedSize(QSize(9, 9))
            pastille.setStyleSheet(
                f"background:{_ROUGE};border-radius:4px;")
            h.addWidget(pastille)

        return ligne

    def _peindre_liste(self):
        for i, w in enumerate(self._lignes):
            try:
                # [GROUPES 19/08/2026] L'action visee est transmise pour
                # que l'anneau apparaisse SUR la croix. Sans elle, la
                # croix serait selectionnable sans que rien ne le montre
                # -- l'utilisateur appuierait sur Entree en croyant
                # ouvrir la conversation.
                # -1 quand _action vaut 0 : on vise la CONVERSATION, donc
                # aucune action ne doit porter le halo. Passer 0 designait
                # la premiere action -- la croix apparaissait encadree
                # alors qu'Entree ouvrait la conversation, ce qui rendait
                # la selection incomprehensible.
                w.set_nav_highlight(
                    self._nav_shown and not self._sur_bouton and i == self._idx,
                    self._action - 1)
            except Exception:
                pass

    def _peindre_boutons(self):
        sur_barre = (self._sur_bouton and not self._page_nouveau
                     and not self._page_groupe)
        vise = sur_barre and self._idx_bouton == 0
        self._btn_nouveau.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:white;"
            f"border:{'2px solid ' + _TXT if vise else 'none'};"
            f"border-radius:6px;padding:8px;font-size:10pt;font-weight:700;}}")
        # [GROUPES 19/08/2026] Second bouton, en teinte plus sobre : la
        # creation de groupe est l'action secondaire de cet ecran.
        vise_g = sur_barre and self._idx_bouton == 1
        self._btn_groupe.setStyleSheet(
            f"QPushButton{{background:{_BG};color:{_ACCENT};"
            f"border:2px solid {_TXT if vise_g else _ACCENT};"
            f"border-radius:6px;padding:7px;font-size:10pt;font-weight:700;}}")
        actif_nom = self._dans_champ and self._page_groupe
        cg = self._cible_grp
        self._ed_nom.setStyleSheet(
            f"QLineEdit{{background:{_BG};color:{_TXT};font-size:11pt;"
            f"padding:6px;border:2px solid "
            f"{_VERT if actif_nom else (_ACCENT if cg == 0 else '#c9ccd1')};"
            f"border-radius:4px;}}")
        self._btn_creer.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:white;"
            f"border:{'2px solid ' + _TXT if cg == 2 else 'none'};"
            f"border-radius:6px;padding:9px;font-size:10pt;font-weight:700;}}"
            f"QPushButton:disabled{{background:#c9ccd1;}}")
        actif = self._dans_champ
        cf = self._cible_form
        # La fleche du combo est fournie explicitement : le theme global
        # du client stylise QComboBox::drop-down, ce qui SUPPRIME le rendu
        # natif de la fleche si aucune image n'est donnee (correctif du
        # build 65). On garde donc le sous-controle par defaut ici.
        self._cmb.setStyleSheet(
            f"QComboBox{{background:{_BG};color:{_TXT};font-size:10pt;"
            f"padding:5px;border:2px solid "
            f"{_ACCENT if cf == 0 else '#c9ccd1'};border-radius:4px;}}")
        self._ed_num.setStyleSheet(
            f"QLineEdit{{background:{_BG};color:{_TXT};font-size:14pt;"
            f"padding:6px;border:2px solid "
            f"{_VERT if actif else (_ACCENT if cf == 1 else '#c9ccd1')};"
            f"border-radius:4px;}}")
        self._btn_demarrer.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:white;"
            f"border:{'2px solid ' + _TXT if cf == 2 else 'none'};"
            f"border-radius:6px;padding:9px;font-size:10pt;font-weight:700;}}"
            f"QPushButton:disabled{{background:#c9ccd1;}}")

    def _ensure_visible(self):
        try:
            if 0 <= self._idx < len(self._lignes):
                self._sc_liste.ensureWidgetVisible(self._lignes[self._idx])
        except Exception:
            pass

    # --- actions ---

    def _ouvrir_conversation(self, numero: str):
        fn = getattr(self.services, "ouvrir_conversation", None)
        if fn is not None and numero:
            try:
                fn(numero)
            except Exception:
                pass

    def _remplir_contacts(self):
        """Alimente la liste deroulante depuis le carnet LOCAL."""
        self._cmb.blockSignals(True)
        self._cmb.clear()
        self._cmb.addItem("—", "")
        rep = getattr(self.services, "repertoire", None)
        if rep is not None:
            try:
                for num, nom in rep.liste():
                    self._cmb.addItem(f"{nom}  ({num})", num)
            except Exception:
                pass
        self._cmb.setCurrentIndex(0)
        self._cmb.blockSignals(False)

    def _ouvrir_liste(self):
        """Deroule la liste des contacts.

        Le popup Qt gere lui-meme haut/bas et Entree, a condition d'avoir
        le focus clavier : la fenetre overlay porte le flag Qt.Tool, un
        showPopup() seul ne suffirait donc pas -- il faut d'abord la
        forcer au premier plan, comme pour les champs de saisie.

        Tant que la liste est ouverte, dans_champ() renvoie True : le
        D-pad se tait et laisse Qt naviguer.
        """
        self._liste_ouverte = True
        ov = self.window()
        fn = getattr(ov, "entrer_dans_champ", None)
        if fn is not None:
            fn(self._cmb)
        else:
            try:
                self._cmb.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass
        try:
            self._cmb.showPopup()
        except Exception:
            self._liste_ouverte = False
        self._peindre_boutons()

    def _sur_contact_valide(self, _i=0):
        """Un contact vient d'etre choisi DANS la liste deroulante.

        `activated` n'est emis que sur une selection de l'utilisateur, pas
        sur un changement programmatique : c'est donc le bon signal pour
        savoir que la liste s'est refermee.
        """
        self._liste_ouverte = False
        if self._cmb.currentData():
            self._cible_form = 2       # on vise Demarrer
        self._peindre_boutons()

    def _sur_contact_choisi(self, _i=0):
        """Choisir un contact remplit le champ numero : les deux entrees
        aboutissent au meme endroit, et on voit quel numero part."""
        num = self._cmb.currentData()
        if num:
            self._ed_num.setText(str(num))

    def _ouvrir_nouveau(self):
        self._page_nouveau = True
        self._cible_form = 0
        self._sc_liste.setVisible(False)
        self._page_form.setVisible(True)
        self._liste_ouverte = False
        self._ed_num.clear()
        self._msg.setText("")
        self._remplir_contacts()
        self._peindre_boutons()

    def _fermer_nouveau(self):
        self._page_nouveau = False
        self._dans_champ = False
        self._page_form.setVisible(False)
        self._sc_liste.setVisible(True)
        self._peindre_boutons()
        self._construire_liste()

    def _verifier(self, _txt=""):
        self._msg.setText("")
        try:
            normalise_numero(self._ed_num.text())
            self._btn_demarrer.setEnabled(True)
        except ContactError:
            self._btn_demarrer.setEnabled(False)

    def _demarrer(self):
        try:
            num = normalise_numero(self._ed_num.text())
        except ContactError as e:
            self._msg.setText(str(e))
            return
        self._fermer_nouveau()
        self._ouvrir_conversation(num)

    # --- [GROUPES 19/08/2026] page "nouveau groupe" ---

    def _resume_groupe(self, groupe) -> str:
        """Sous-titre d'une ligne de groupe : les membres, ou leur nombre.

        Les noms viennent du carnet LOCAL, seul endroit ou une
        substitution est possible : le serveur n'envoie jamais de pseudo.
        Un membre inconnu reste donc son numero, ce qui est voulu.

        Au-dela de trois membres on bascule sur un simple compte : la
        ligne ne peut pas s'allonger, et une enumeration tronquee au
        milieu d'un numero se lit plus mal qu'un nombre.
        """
        if not groupe or _GRP is None:
            return ""
        membres = list(groupe.get("membres") or [])
        moi = self._mon_numero()
        autres = [m for m in membres if m != moi]
        if not autres:
            return "Vous seul"
        if len(autres) > 3:
            return f"{len(autres)} membres"
        rep = getattr(self.services, "repertoire", None)
        fn = None
        if rep is not None:
            fn = getattr(rep, "nom_pour", None)
        return _GRP.resume_membres(
            {"membres": autres}, fn, None)

    def _mon_numero(self) -> str:
        """Numero du joueur, ou "" s'il n'est pas connu.

        Sert a s'exclure du resume des membres : se lire soi-meme dans la
        liste des participants n'apprend rien et mange la place.
        """
        fn = getattr(self.services, "mon_numero", None)
        if fn is None:
            return ""
        try:
            return str(fn() or "")
        except Exception:
            return ""

    def _ouvrir_groupe(self):
        """Ouvre la page de creation. Sans contact, on n'y entre pas."""
        if not self._groupes_actifs():
            return
        self._page_groupe = True
        self._cible_grp = 0
        self._grp_choisis = []
        self._grp_idx = 0
        self._dans_champ = False
        self._ed_nom.clear()
        self._msg_grp.setText("")
        self._sc_liste.setVisible(False)
        self._page_grp.setVisible(True)
        self._construire_grp()
        self._verifier_grp()
        self._peindre_boutons()

    def _fermer_groupe(self):
        self._page_groupe = False
        self._dans_champ = False
        self._page_grp.setVisible(False)
        self._sc_liste.setVisible(True)
        self._peindre_boutons()
        self._construire_liste()

    def _construire_grp(self):
        """Liste des contacts cochables.

        Le carnet LOCAL est la seule source : on ne compose un groupe
        qu'avec des gens qu'on a deja enregistres. Taper des numeros de
        memoire serait une source d'erreur sans rattrapage possible,
        puisque la composition est FIGEE des la creation.
        """
        _vider(self._lay_grp)
        self._grp_lignes = []
        self._grp_numeros = []
        entrees = []
        rep = getattr(self.services, "repertoire", None)
        if rep is not None:
            try:
                entrees = list(rep.liste() or [])
            except Exception:
                entrees = []
        if not entrees:
            self._lay_grp.insertWidget(0, _message_vide(
                "Aucun contact.\n\nEnregistrez d'abord des contacts pour "
                "pouvoir composer un groupe."))
            return
        for num, nom in entrees:
            num = str(num)
            w = self._ligne_grp(num, nom)
            self._lay_grp.insertWidget(self._lay_grp.count() - 1, w)
            self._grp_lignes.append(w)
            self._grp_numeros.append(num)
        self._grp_idx = min(self._grp_idx, max(0, len(self._grp_lignes) - 1))
        self._peindre_grp()

    def _ligne_grp(self, numero: str, nom: str) -> QWidget:
        ligne = _LigneNav(numero)
        ligne.sig_clic.connect(self._basculer_membre)
        h = QHBoxLayout(ligne)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(8)
        coche = QLabel("\u2611" if numero in self._grp_choisis else "\u2610")
        coche.setStyleSheet(
            f"color:{_VERT if numero in self._grp_choisis else _MUTED};"
            f"font-size:13pt;background:transparent;")
        h.addWidget(coche)
        lbl = QLabel(f"{nom}  ({numero})")
        lbl.setStyleSheet(
            f"color:{_TXT};font-size:10pt;background:transparent;")
        h.addWidget(lbl, stretch=1)
        return ligne

    def _basculer_membre(self, numero: str):
        """Coche ou decoche un contact.

        Le plafond est verifie ICI et pas seulement a l'envoi : laisser
        cocher douze personnes pour refuser ensuite oblige a deviner
        lesquelles retirer.
        """
        numero = str(numero)
        if numero in self._grp_choisis:
            self._grp_choisis.remove(numero)
        else:
            maxi = (_GRP.MEMBRES_MAX - 1) if _GRP is not None else 9
            if len(self._grp_choisis) >= maxi:
                self._msg_grp.setText(
                    f"{maxi} membres maximum (vous compris, "
                    f"cela fait {maxi + 1}).")
                return
            self._grp_choisis.append(numero)
        self._msg_grp.setText("")
        self._construire_grp()
        self._verifier_grp()

    def _peindre_grp(self):
        vise = (self._page_groupe and self._cible_grp == 1
                and not self._dans_champ)
        for i, w in enumerate(self._grp_lignes):
            try:
                w.set_nav_highlight(vise and i == self._grp_idx)
            except Exception:
                pass

    def _ensure_grp_visible(self):
        try:
            if 0 <= self._grp_idx < len(self._grp_lignes):
                self._sc_grp.ensureWidgetVisible(
                    self._grp_lignes[self._grp_idx])
        except Exception:
            pass

    def _verifier_grp(self, _txt=""):
        """Active « Créer » quand la saisie est valide.

        La validation vient du module de REGLES, pas d'une copie locale :
        une seconde definition du nom valide divergerait de celle du
        serveur, et le joueur verrait « Créer » actif pour se faire
        refuser ensuite.
        """
        if _GRP is None:
            self._btn_creer.setEnabled(False)
            return
        nom_ok = bool(_GRP.valide_nom(self._ed_nom.text()))
        assez = len(self._grp_choisis) >= (_GRP.MEMBRES_MIN - 1)
        n = len(self._grp_choisis)
        self._lbl_membres.setText(
            "Membres" if not n else f"Membres ({n} choisi{'s' if n > 1 else ''})")
        self._btn_creer.setEnabled(bool(nom_ok and assez))
        self._peindre_boutons()

    def _creer_groupe(self):
        """Envoie la demande au serveur, puis referme.

        L'ecran ne se met PAS a jour de lui-meme : le groupe n'existe que
        quand le serveur l'a cree et renvoye l'etat. Afficher le groupe
        tout de suite le ferait apparaitre puis disparaitre en cas de
        refus -- exactement le genre de faux positif qu'on evite ailleurs.
        """
        if _GRP is None:
            return
        nom = _GRP.valide_nom(self._ed_nom.text())
        if not nom:
            self._msg_grp.setText(
                f"Nom invalide ({_GRP.NOM_MAX_LEN} caractères maximum).")
            return
        if len(self._grp_choisis) < (_GRP.MEMBRES_MIN - 1):
            self._msg_grp.setText("Choisissez au moins un membre.")
            return
        fn = getattr(self.services, "creer_groupe", None)
        if fn is None:
            return
        try:
            fn(nom, list(self._grp_choisis))
        except Exception:
            self._msg_grp.setText("Envoi impossible.")
            return
        self._fermer_groupe()

    # --- [GROUPES 19/08/2026] quitter un groupe ---

    def _nb_actions(self, i: int) -> int:
        """Nombre d'actions portees par la ligne i de la LISTE.

        Zero pour une conversation directe, un pour un groupe (la croix).
        C'est ce compte qui borne la navigation gauche/droite.
        """
        try:
            return self._lignes[i].nb_actions()
        except Exception:
            return 0

    def _demander_sortie(self, gid: str):
        """Ouvre la confirmation pour ce groupe.

        Le depart est definitif et VISIBLE DES AUTRES -- ils recoivent
        une annonce -- donc il ne peut pas partir d'un clic accidentel
        sur une croix de 24 pixels.
        """
        gid = str(gid or "")
        if not gid or _GRP is None:
            return
        nom = "ce groupe"
        try:
            for cand in (getattr(self.services, "groupes", lambda: [])() or []):
                if str(cand.get("id")) == gid:
                    nom = cand.get("nom") or nom
                    break
        except Exception:
            pass
        self._confirm_gid = gid
        # Cadrage par defaut sur « Annuler » : sur une action
        # irreversible, une touche Entree tapee trop vite ne doit pas la
        # declencher.
        self._conf_idx = 1
        self._lbl_conf.setText(f"Quitter « {nom} » ?")
        self._sc_liste.setVisible(False)
        self._page_conf.setVisible(True)
        self._peindre_conf()

    def _annuler_sortie(self):
        """Referme la confirmation sans rien envoyer."""
        self._confirm_gid = ""
        self._page_conf.setVisible(False)
        self._sc_liste.setVisible(True)
        self._peindre_boutons()

    def _confirmer_sortie(self):
        """Envoie la demande. Rien n'est retire localement.

        Le groupe disparait quand le SERVEUR a confirme et renvoye
        l'etat : le retirer tout de suite le ferait disparaitre puis
        reapparaitre si l'envoi echouait.
        """
        gid = self._confirm_gid
        self._annuler_sortie()
        if not gid:
            return
        try:
            import circusvoip_core as _core_grp
            _core_grp._ws_send_safe({"type": "groupe_quitter", "id": gid})
        except Exception:
            pass

    def _peindre_conf(self):
        vise = getattr(self, "_conf_idx", 1)
        self._btn_conf_oui.setStyleSheet(
            f"QPushButton{{background:{_ROUGE};color:white;"
            f"border:{'2px solid ' + _TXT if vise == 0 else 'none'};"
            f"border-radius:6px;padding:9px;font-size:10pt;font-weight:700;}}")
        self._btn_conf_non.setStyleSheet(
            f"QPushButton{{background:{_BG};color:{_TXT};"
            f"border:2px solid {_TXT if vise == 1 else '#c9ccd1'};"
            f"border-radius:6px;padding:9px;font-size:10pt;}}")

    # --- navigation D-pad ---

    def dans_champ(self) -> bool:
        """Inclut la liste deroulante ouverte : Qt y gere haut/bas et
        Entree, le D-pad ne doit pas s'en meler."""
        return bool(self._dans_champ or self._liste_ouverte)

    def champ_courant_vide(self) -> bool:
        if self._liste_ouverte:
            # Retour arriere pendant que la liste est ouverte : on la
            # referme plutot que d'effacer quoi que ce soit.
            return True
        if not self._dans_champ:
            return False
        try:
            # [GROUPES 19/08/2026] Deux champs de saisie possibles selon
            # la page. Interroger _ed_num pendant qu'on tape le nom d'un
            # groupe rendrait "vide" a tort, et le retour arriere
            # quitterait le champ au lieu d'effacer une lettre.
            champ = self._ed_nom if self._page_groupe else self._ed_num
            return not champ.text()
        except Exception:
            return False

    def handle_nav(self, direction: str) -> bool:
        # [GROUPES 19/08/2026] La confirmation de sortie capte TOUT, et en
        # premier : tant qu'elle est ouverte, aucune autre cible ne doit
        # repondre, sinon une touche agirait sur la liste cachee derriere
        # -- l'utilisateur deplacerait une selection qu'il ne voit pas.
        if self._confirm_gid:
            if direction in ("up", "down"):
                self._conf_idx = 0 if direction == "up" else 1
                self._peindre_conf()
            elif direction == "enter":
                if self._conf_idx == 0:
                    self._confirmer_sortie()
                else:
                    self._annuler_sortie()
            elif direction == "esc":
                self._annuler_sortie()
            return True

        if self._liste_ouverte:
            if direction == "esc":
                try:
                    self._cmb.hidePopup()
                except Exception:
                    pass
                self._liste_ouverte = False
                self._peindre_boutons()
            return True
        if self._dans_champ:
            # [GROUPES 19/08/2026] Le champ du nom de groupe se comporte
            # differemment de celui du numero : valider n'envoie RIEN, ca
            # descend seulement vers la liste des membres. Creer avec zero
            # membre est impossible, et Entree qui echoue silencieusement
            # ferait croire a une panne.
            if self._page_groupe:
                if direction == "enter":
                    self._dans_champ = False
                    self._cible_grp = 1
                    self._peindre_boutons()
                    self._peindre_grp()
                elif direction == "esc" and not self._ed_nom.text():
                    self._dans_champ = False
                    self._peindre_boutons()
                return True
            if direction == "enter":
                self._dans_champ = False
                self._cible_form = 2
                self._peindre_boutons()
                if self._btn_demarrer.isEnabled():
                    self._demarrer()
            elif direction == "esc" and not self._ed_num.text():
                self._dans_champ = False
                self._peindre_boutons()
            return True

        if self._page_groupe:
            # Cibles : 0 = champ nom, 1 = liste des contacts, 2 = Creer.
            if direction == "up":
                if self._cible_grp == 1 and self._grp_idx > 0:
                    self._grp_idx -= 1          # on remonte DANS la liste
                    self._ensure_grp_visible()
                else:
                    self._cible_grp = max(0, self._cible_grp - 1)
            elif direction == "down":
                if (self._cible_grp == 1
                        and self._grp_idx < len(self._grp_lignes) - 1):
                    self._grp_idx += 1
                    self._ensure_grp_visible()
                else:
                    self._cible_grp = min(2, self._cible_grp + 1)
                    if self._cible_grp == 1:
                        self._grp_idx = 0
            elif direction == "enter":
                if self._cible_grp == 0:
                    self._entrer_dans_champ_nom()
                elif self._cible_grp == 1:
                    if 0 <= self._grp_idx < len(self._grp_numeros):
                        self._basculer_membre(self._grp_numeros[self._grp_idx])
                elif self._btn_creer.isEnabled():
                    self._creer_groupe()
                    return True
            elif direction == "esc":
                self._fermer_groupe()
                return True
            self._peindre_boutons()
            self._peindre_grp()
            return True

        if self._page_nouveau:
            # Cibles : 0 = liste des contacts, 1 = champ numero,
            # 2 = bouton Demarrer.
            if direction == "up":
                self._cible_form = max(0, self._cible_form - 1)
            elif direction == "down":
                self._cible_form = min(2, self._cible_form + 1)
            elif direction in ("left", "right") and self._cible_form == 0:
                # Gauche/droite fait defiler les contacts SANS ouvrir la
                # popup Qt : une liste deroulante ouverte capterait le
                # clavier et sortirait du cadre du telephone.
                n = self._cmb.count()
                if n > 1:
                    i = self._cmb.currentIndex()
                    self._cmb.setCurrentIndex(
                        (i + (1 if direction == "right" else -1)) % n)
            elif direction == "enter":
                if self._cible_form == 0:
                    # Entree OUVRE la liste deroulante, comme on
                    # l'attend d'un tel controle. Les deux versions
                    # precedentes ne faisaient que deplacer le curseur :
                    # a l'ecran, la touche paraissait morte.
                    self._ouvrir_liste()
                elif self._cible_form == 1:
                    self._entrer_dans_champ()
                elif self._btn_demarrer.isEnabled():
                    self._demarrer()
            self._peindre_boutons()
            return True

        # Liste : le bouton "Nouvelle conversation" est AU-DESSUS, on le
        # rejoint en remontant -- meme geometrie que la barre d'onglets
        # d'Appels et Contacts.
        # Pas de "revelation au 1er appui" ici, contrairement aux listes
        # d'Appels et Contacts : le curseur est DEJA visible a l'ouverture,
        # sur le bouton "Nouvelle conversation". Ajouter une revelation
        # obligeait a appuyer deux fois sur bas pour atteindre la premiere
        # conversation.
        # [GROUPES 19/08/2026] La barre d'en-tete compte maintenant DEUX
        # boutons quand les groupes sont actifs. Haut/bas circule entre
        # eux puis descend dans la liste ; gauche/droite ne sert pas ici,
        # les deux boutons etant empiles verticalement.
        nb_boutons = 2 if self._grp_dispo else 1
        # Changer de ligne remet la visee sur la conversation : garder la
        # croix visee en descendant ferait quitter un groupe qu'on ne
        # faisait que traverser.
        if direction in ("up", "down"):
            self._action = 0
        if direction == "up":
            if not self._sur_bouton:
                if self._idx <= 0:
                    self._sur_bouton = True
                    self._idx_bouton = nb_boutons - 1   # le plus proche
                else:
                    self._idx -= 1
            elif self._idx_bouton > 0:
                self._idx_bouton -= 1
        elif direction == "down":
            if self._sur_bouton:
                if self._idx_bouton < nb_boutons - 1:
                    self._idx_bouton += 1
                elif self._lignes:
                    self._sur_bouton = False
                    self._idx = 0
            elif self._idx < len(self._lignes) - 1:
                self._idx += 1
        elif direction in ("left", "right"):
            # [GROUPES 19/08/2026] Circule entre l'ouverture de la
            # conversation (action 0, implicite) et les actions de la
            # ligne -- aujourd'hui la seule croix « quitter », presente
            # sur les groupes uniquement. C'est ce parcours qui rend la
            # croix atteignable au clavier ; posee sans lui, elle ne
            # serait cliquable qu'a la souris.
            if not self._sur_bouton:
                nb = self._nb_actions(self._idx)
                if nb:
                    pas = 1 if direction == "right" else -1
                    self._action = max(0, min(nb, self._action + pas))
        elif direction == "enter":
            if self._sur_bouton:
                if self._idx_bouton == 1 and nb_boutons > 1:
                    self._ouvrir_groupe()
                else:
                    self._ouvrir_nouveau()
                return True
            if 0 <= self._idx < len(self._numeros):
                # _action == 0 : ouvrir la conversation. Au-dela : les
                # actions de la ligne, decalees de 1.
                if self._action > 0 and self._nb_actions(self._idx):
                    self._lignes[self._idx].declencher(self._action - 1)
                else:
                    self._ouvrir_conversation(self._numeros[self._idx])
            return True
        self._ensure_visible()
        self._peindre_boutons()
        self._peindre_liste()
        return True

    def _entrer_dans_champ(self):
        self._dans_champ = True
        ov = self.window()
        fn = getattr(ov, "entrer_dans_champ", None)
        if fn is not None:
            fn(self._ed_num)
        else:
            try:
                self._ed_num.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass
        self._peindre_boutons()

    def _entrer_dans_champ_nom(self):
        """[GROUPES 19/08/2026] Meme mecanique que _entrer_dans_champ.

        Le passage par la fenetre est INDISPENSABLE : l'overlay porte le
        flag Qt.Tool, un setFocus() seul ne lui donnerait pas le clavier
        et le champ resterait muet sous les touches.
        """
        self._dans_champ = True
        ov = self.window()
        fn = getattr(ov, "entrer_dans_champ", None)
        if fn is not None:
            fn(self._ed_nom)
        else:
            try:
                self._ed_nom.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass
        self._peindre_boutons()

    def handle_back(self) -> bool:
        if self._dans_champ:
            self._dans_champ = False
            self._peindre_boutons()
            return True
        if self._confirm_gid:
            self._annuler_sortie()
            return True
        if self._page_groupe:
            self._fermer_groupe()
            return True
        if self._page_nouveau:
            self._fermer_nouveau()
            return True
        return False

    def on_show(self):
        # [GROUPES 19/08/2026] La visibilite du bouton est reevaluee a
        # CHAQUE ouverture : les services peuvent apparaitre apres la
        # construction de l'app, notamment si le joueur ouvre le
        # telephone avant que la connexion au serveur soit etablie.
        try:
            self._grp_dispo = self._groupes_actifs()
            self._btn_groupe.setVisible(self._grp_dispo)
            if not self._grp_dispo:
                # Le bouton a disparu alors que le curseur etait dessus :
                # sans ce recalage, Entree ne ferait plus rien.
                self._idx_bouton = 0
        except Exception:
            pass
        # [GROUPES 19/08/2026] Reclamer la liste a CHAQUE ouverture, comme
        # l'app Travail le fait pour ses missions. Sans cette demande, un
        # joueur ajoute a un groupe pendant qu'il etait deconnecte ne le
        # verrait jamais : la poussee spontanee du serveur ne touche que
        # les joueurs connectes au moment de la creation.
        if self._grp_dispo:
            try:
                import circusvoip_core as _core_grp
                _core_grp._ws_send_safe({"type": "groupe_liste"})
            except Exception:
                pass
        if not (self._page_nouveau or self._page_groupe
                or self._confirm_gid):
            self._construire_liste()
        self._peindre_boutons()
