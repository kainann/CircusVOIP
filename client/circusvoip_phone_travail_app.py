#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[TRAVAIL 10/08/2026] App « Travail » du CircusPhone.

Tableau d'annonces entre joueurs : on publie une mission en indiquant le
METIER RECHERCHE, et ceux qui exercent ce metier la voient apparaitre.

--- Le metier d'une mission est celui qu'on CHERCHE ---

Point le plus contre-intuitif du modele : un mineur publie une mission
`mercenaire` quand il cherche une escorte. C'est ce qui fait tourner le
jeu de role -- chacun a besoin des autres. Les regles vivent dans
circusvoip_phone_travail.py ; ce fichier ne fait que les montrer.

--- Le serveur decide, le client dessine ---

Cet ecran ne stocke rien et ne tranche rien. Chaque action envoie une
INTENTION (travail_publier, travail_prendre...) et attend le travail_etat
que le serveur renvoie ; c'est lui seul qui redessine la page.

C'est ce qui evite qu'a deux joueurs prenant la meme mission, chaque
ecran donne raison a son proprietaire -- et que l'un des deux se deplace
pour rien. Le prix est un aller-retour reseau avant que l'ecran ne
change : imperceptible sur un clic, alors que l'incoherence, elle, ne se
rattrape pas.

Les validations de saisie restent faites en local AVANT l'envoi, pour que
les fautes de frappe soient signalees sans aller-retour. Le serveur les
refait toutes : c'est lui qui fait autorite.

--- Trois onglets, un bandeau ---

  Missions      : ce qu'on me propose, filtre sur MES metiers.
  Mes missions  : ce que j'ai publie.
  Metiers       : mes metiers (2 max) + notifications.

Le bandeau de mission en cours est HORS des onglets, visible en
permanence. C'est un ETAT, pas une liste -- un joueur a une mission ou
n'en a pas -- et lui donner une quatrieme page l'aurait rendu invisible
la plupart du temps.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRect, Qt,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget,
)

from circusvoip_phone_apps import PhoneApp
import circusvoip_phone_travail as T

# Palette : reprise a l'identique de l'app Appels. Redefinir une teinte
# ici ferait deriver les deux ecrans a la premiere retouche.
_BG     = "#ffffff"
_TXT    = "#1a1a1a"
_MUTED  = "#9aa0a6"
_ACCENT = "#2f6fed"
_VERT   = "#3fb950"
_ROUGE  = "#e5484d"
_SEP    = "#e6e8eb"
_GRISE  = "#f1f3f4"
# [NAV 11/08/2026] Couleur du halo de selection.
#
# Noir et non bleu : le bleu d'accent est aussi la couleur de REMPLISSAGE
# des boutons principaux ("Nouvelle mission", "Publier"). Un contour bleu
# sur un fond bleu est invisible -- precisement sur les boutons ou le
# curseur a le plus de chances de se trouver.
#
# Le noir tranche sur les trois fonds du fichier : blanc des cartes, gris
# des zones inertes, bleu des boutons pleins.
_HALO   = "#000000"

# ---------------------------------------------------------------------
#  Stockage local (provisoire)
# ---------------------------------------------------------------------

class _Etat:
    """Miroir LOCAL de ce que le serveur vient d'envoyer.

    [TRAVAIL 10/08/2026] Ne contient AUCUNE regle et ne decide de rien :
    il stocke le dernier travail_etat recu, un point c'est tout.

    C'est la difference avec le depot local qu'il remplace. Le client ne
    retire jamais une mission de sa propre liste, ne coche jamais un
    metier de lui-meme : il envoie une intention, et il redessine ce que
    le serveur lui repond. Sans cette discipline, deux joueurs qui
    prennent la meme mission verraient chacun leur ecran leur donner
    raison -- et l'un des deux se deplacerait pour rien.

    Le prix est un aller-retour reseau avant que l'ecran ne change. Sur
    un clic de bouton, c'est imperceptible ; l'incoherence, elle, ne se
    rattrape pas.
    """

    def __init__(self):
        self.metiers: list[str] = []
        self.notifs: bool = True
        self.missions: list[dict] = []   # visibles, filtrees par le serveur
        self.miennes: list[dict] = []
        self.mission_en_cours = None
        self.pret = False                # a-t-on deja recu une reponse ?

    def appliquer(self, d: dict):
        self.metiers = list(d.get("metiers") or [])
        self.notifs = bool(d.get("notifs", True))
        self.missions = list(d.get("missions") or [])
        self.miennes = list(d.get("miennes") or [])
        self.mission_en_cours = d.get("en_cours")
        self.pret = True


def _envoyer(payload: dict) -> bool:
    """Envoie une trame au serveur. False si la connexion est absente.

    Passe par le coeur plutot que par un socket propre a l'app : c'est
    lui qui gere la reconnexion et la file d'envoi.
    """
    try:
        import circusvoip_core as _core
        return bool(_core._ws_send_safe(payload))
    except Exception:
        return False


# ---------------------------------------------------------------------
#  Fragments d'interface
# ---------------------------------------------------------------------

# [SAISIE 11/08/2026] Style des champs.
#
# Sans style explicite, le texte SAISI heritait du gris du theme et
# devenait aussi pale que le texte d'invite : on ne distinguait plus ce
# qu'on venait de taper de ce qui n'etait qu'une suggestion. Le texte
# tape est en noir franc, l'invite reste grise -- c'est la seule
# difference qui compte a l'ecran.
_STYLE_CHAMP = (
    f"QLineEdit,QTextEdit{{color:{_TXT};background:{_BG};"
    f"border:1px solid {_SEP};border-radius:10px;padding:6px 8px;"
    f"font-size:9pt;}}"
    f"QLineEdit::placeholder,QTextEdit::placeholder{{color:{_MUTED};}}"
)


def _halo(widget, base, selected):
    """Applique (ou retire) le halo de selection clavier sur un widget.

    [NAV 11/08/2026] Le halo vise le CADRE EXTERIEUR, jamais le contenu.

    Une feuille de style sans selecteur s'applique au widget ET a toute
    sa descendance : la bordure de selection se retrouvait dessinee
    autour de chaque label et de chaque bouton de la carte, qui
    ressemblait alors a un formulaire. On passe donc par un selecteur
    d'objet -- "#carte" -- qui ne designe que le widget nomme.

    Trois formes de feuille cohabitent dans ce fichier :
      - selecteur d'objet     : les cartes, qui ont des enfants ;
      - "QPushButton{...}"    : les boutons, sans enfants ;
      - proprietes nues       : les widgets simples.
    Le halo suit la forme de la base, sinon il ne s'applique pas : un
    bloc et une propriete nue n'ont pas la meme specificite.
    """
    if not selected:
        widget.setStyleSheet(base)
        return
    nom = widget.objectName()
    if nom:
        cible = f"#{nom}"
    elif "{" in base:
        cible = "QPushButton"
    else:
        widget.setStyleSheet(base + f"border:2px solid {_HALO};")
        return
    widget.setStyleSheet(
        base + f"\n{cible}{{border:2px solid {_HALO};}}")


def _titre(texte):
    lbl = QLabel(texte)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(
        f"color:{_TXT};font-size:11pt;font-weight:700;padding:6px 0;")
    return lbl


def _vide(texte):
    lbl = QLabel(texte)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{_MUTED};font-size:9pt;padding:24px 12px;")
    return lbl


class _Onglets(QWidget):
    """Barre d'onglets, calquee sur celle des apps Appels et Contacts."""

    def __init__(self, libelles, on_change, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._on_change = on_change
        self._btns = []
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

    def selectionner(self, i):
        if i == self._courant:
            return
        self._courant = i
        self._peindre()
        self._on_change(i)

    def courant(self):
        return self._courant

    def set_nav_highlight(self, selected):
        """Surbrillance de la barre quand le curseur y remonte.

        Cadre bleu fin plutot qu'un fond plein : le halo marque la CIBLE
        dans le contenu, et reprendre le meme rendu ici rendrait les deux
        niveaux de selection indistinguables.
        """
        if selected:
            self.setStyleSheet(
                f"background:rgba(47,111,237,0.10);"
                f"border:2px solid {_ACCENT};border-radius:8px;")
        else:
            self.setStyleSheet("")

    def _peindre(self):
        for i, b in enumerate(self._btns):
            actif = (i == self._courant)
            b.setStyleSheet(
                f"QPushButton{{border:none;background:transparent;"
                f"color:{_ACCENT if actif else _MUTED};"
                f"font-size:9pt;font-weight:{700 if actif else 500};"
                f"padding:6px 2px;"
                f"border-bottom:2px solid "
                f"{_ACCENT if actif else 'transparent'};}}")


class _Bandeau(QFrame):
    """Mission en cours, visible quel que soit l'onglet.

    Toujours present, meme vide : un bandeau qui apparait et disparait
    ferait sauter le contenu de 40 px a chaque changement d'etat, et le
    joueur ne saurait pas ou regarder.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        # Nomme : le halo de selection doit pouvoir le viser, comme les
        # cartes et les champs.
        self.setObjectName("bandeau")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(6)
        # [NAV 11/08/2026] Le bandeau n'a PLUS de bouton.
        #
        # Il est hors de la zone de defilement, donc hors du parcours du
        # D-pad : ses boutons etaient decoratifs, impossibles a atteindre
        # au clavier. Les y enregistrer aurait demande des index negatifs
        # (le bandeau est au-DESSUS des onglets) et rendu la navigation
        # illisible.
        #
        # Les actions vivent donc en tete du CONTENU, ou elles sont
        # naturellement dans le parcours. Le bandeau garde son role :
        # rappeler l'etat, quel que soit l'onglet.
        self._base = ""
        self._lbl = QLabel("Aucune mission en cours")
        self._lbl.setWordWrap(True)
        lay.addWidget(self._lbl, stretch=1)
        self.montrer(None)

    def styleSheet_base(self):
        """Style courant SANS halo.

        Le bandeau se repeint a chaque montrer() -- vide ou en cours --
        donc memoriser son style une fois pour toutes, comme on le fait
        pour les cartes, donnerait un halo pose sur une base perimee.
        """
        return self._base

    def montrer(self, mission):
        if mission is None:
            self._lbl.setText("Aucune mission en cours")
            self._lbl.setStyleSheet(f"color:{_MUTED};font-size:9pt;")
            self._base = f"#bandeau{{background:{_GRISE};border-radius:10px;}}"
            self.setStyleSheet(self._base)
            return
        self._lbl.setText(
            f"{mission.get('titre', '')}  ·  "
            f"{T.libelle_metier(mission.get('metier'))}")
        self._lbl.setStyleSheet(
            f"color:{_TXT};font-size:9pt;font-weight:600;")
        self._base = (f"#bandeau{{background:rgba(63,185,80,0.12);"
                      f"border:1px solid {_VERT};border-radius:10px;}}")
        self.setStyleSheet(self._base)


class _Carte(QFrame):
    """Une annonce dans une liste."""

    def __init__(self, mission, actions, parent=None):
        super().__init__(parent)
        # Nomme pour que le halo ne vise QUE ce cadre : sans selecteur,
        # la bordure de selection descendrait sur tous les enfants.
        self.setObjectName("carte")
        # Selecteur d'OBJET SEUL, sans le nom de type : le halo ajoute
        # ensuite "#carte{...}" et deux regles ne se departagent que si
        # elles ont la MEME specificite. "QFrame#carte" l'emporterait sur
        # "#carte", et la bordure de selection ne s'afficherait jamais.
        self.setStyleSheet(
            f"#carte{{background:{_BG};"
            f"border:1px solid {_SEP};border-radius:10px;}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(3)

        haut = QHBoxLayout()
        haut.setSpacing(6)
        t = QLabel(mission.get("titre", ""))
        t.setWordWrap(True)
        t.setStyleSheet(f"color:{_TXT};font-size:10pt;font-weight:600;")
        haut.addWidget(t, stretch=1)
        # Le metier est une PASTILLE et non une ligne de texte : c'est le
        # critere sur lequel l'oeil balaye une liste d'annonces, il doit
        # se reperer sans lecture.
        p = QLabel(T.libelle_metier(mission.get("metier")))
        p.setStyleSheet(
            f"color:{_ACCENT};font-size:8pt;font-weight:600;"
            f"background:rgba(47,111,237,0.10);"
            f"border-radius:8px;padding:2px 8px;")
        haut.addWidget(p)
        v.addLayout(haut)

        desc = (mission.get("description") or "").strip()
        if desc:
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet(f"color:{_TXT};font-size:9pt;")
            v.addWidget(d)

        bas = QHBoxLayout()
        bas.setSpacing(6)
        pay = QLabel(T.paiement_texte(mission.get("paiement")))
        pay.setStyleSheet(f"color:{_VERT};font-size:9pt;font-weight:600;")
        bas.addWidget(pay)
        bas.addStretch(1)
        meta = QLabel(f"{mission.get('auteur', '')}  ·  "
                      f"{T.age_texte(mission.get('cree_le'))}")
        meta.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        bas.addWidget(meta)
        v.addLayout(bas)

        if actions:
            barre = QHBoxLayout()
            barre.setSpacing(6)
            barre.addStretch(1)
            for libelle, fonction, danger in actions:
                b = QPushButton(libelle)
                b.setCursor(Qt.PointingHandCursor)
                coul = _ROUGE if danger else _ACCENT
                b.setStyleSheet(
                    f"QPushButton{{border:1px solid {coul};color:{coul};"
                    f"background:transparent;border-radius:8px;"
                    f"padding:4px 12px;font-size:9pt;}}")
                b.clicked.connect(lambda _=False, f=fonction: f())
                barre.addWidget(b)
            v.addLayout(barre)


class _BoutonMetier(QPushButton):
    """Bouton de la grille des metiers : vert si choisi, grise si la
    limite de deux est atteinte."""

    def __init__(self, metier, parent=None):
        super().__init__(T.libelle_metier(metier), parent)
        self.metier = metier
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(38)
        self.peindre(False, False)

    def peindre(self, choisi, bloque):
        if choisi:
            style = (f"background:{_VERT};color:#ffffff;"
                     f"border:1px solid {_VERT};font-weight:700;")
        elif bloque:
            # Grise SEULEMENT quand deux metiers sont deja pris : avant,
            # tout est cliquable. Le grisage indique une LIMITE atteinte,
            # pas un choix unique.
            style = (f"background:{_GRISE};color:{_MUTED};"
                     f"border:1px solid {_SEP};")
        else:
            style = (f"background:{_BG};color:{_TXT};"
                     f"border:1px solid {_SEP};")
        self.setStyleSheet(
            f"QPushButton{{{style}border-radius:10px;"
            f"font-size:9pt;padding:6px;}}")


# ---------------------------------------------------------------------
#  L'application
# ---------------------------------------------------------------------

class TravailApp(PhoneApp):

    APP_ID   = "travail"
    APP_NAME = "Travail"
    APP_ICON = "\U0001F6E0"

    ONGLET_MISSIONS = 0
    ONGLET_MIENNES  = 1
    ONGLET_METIERS  = 2

    def __init__(self, screen_w, screen_h, screen_radius, services,
                 parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.setStyleSheet(f"background:{_BG};")
        self._etat = _Etat()
        self._creation = False   # l'onglet Mes missions montre le formulaire
        self._erreur = ""
        # [NAV 10/08/2026] Curseur de selection, sur le modele des apps
        # Appels et Contacts : -1 designe la BARRE D'ONGLETS, 0..n-1 les
        # elements de la page courante. Les onglets etant visuellement
        # au-dessus du contenu, on les atteint en remontant -- la
        # navigation suit la geometrie de l'ecran.
        # [BANDEAU 11/08/2026] Index de cible : -2 = BANDEAU, -1 = barre
        # d'onglets, 0..n = contenu. L'ordre suit la geometrie de l'ecran,
        # de haut en bas -- remonter depuis les onglets atteint le
        # bandeau, qui est juste au-dessus.
        #
        # -2 n'existe que s'il y a une mission en cours : un bandeau
        # "aucune mission" n'a rien a deployer, le selectionner ne ferait
        # que rallonger le parcours.
        self._cible = -1
        self._deploye = False    # le bandeau couvre l'onglet actif
        self._nav = []           # [(widget, style_base, action)]
        self._dans_champ_ = False

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)
        v.addWidget(_titre("Missions"))

        self._bandeau = _Bandeau()
        v.addWidget(self._bandeau)

        self._onglets = _Onglets(
            ["Missions", "Mes missions", "Métiers"], self._changer_onglet)
        v.addWidget(self._onglets)

        # [PANNEAU 11/08/2026] Panneau de la mission en cours.
        #
        # Enfant DIRECT de l'app, hors de tout layout : c'est ce qui lui
        # permet de RECOUVRIR les onglets et la liste en se deployant. Un
        # widget place dans le layout pousserait le contenu vers le bas au
        # lieu de passer par-dessus, et l'ecran sauterait.
        #
        # Sa hauteur est animee de 0 a sa taille utile ; le mouvement dit
        # d'ou vient le panneau -- du bandeau, juste au-dessus -- ce
        # qu'une apparition instantanee ne dirait pas.
        self._panneau = QFrame(self)
        self._panneau.setObjectName("panneau")
        self._panneau.setStyleSheet(
            f"#panneau{{background:{_BG};border:1px solid {_VERT};"
            f"border-radius:12px;}}")
        self._panneau.hide()
        self._pan_lay = QVBoxLayout(self._panneau)
        self._pan_lay.setContentsMargins(12, 10, 12, 10)
        self._pan_lay.setSpacing(5)
        self._anim = QPropertyAnimation(self._panneau, b"geometry", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._zone = QScrollArea()
        self._zone.setWidgetResizable(True)
        self._zone.setFrameShape(QFrame.NoFrame)
        self._zone.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self._zone, stretch=1)

        self._rafraichir()

    # -- identite --

    def _mon_numero(self):
        """Mon numero, via les services du telephone.

        Sans lui on ne peut ni publier ni prendre : c'est l'identite dans
        toute l'app. Le repli sur une chaine vide fait echouer les
        validations avec un message clair plutot que de planter.
        """
        for attr in ("mon_numero", "my_numero", "numero"):
            val = getattr(self.services, attr, None)
            if callable(val):
                try:
                    val = val()
                except Exception:
                    val = None
            if val:
                return str(val)
        return ""

    # -- navigation --

    def _changer_onglet(self, _i):
        self._creation = False
        self._erreur = ""
        # Le curseur reste sur la barre : on vient de changer d'onglet,
        # on est encore en train de choisir ou aller. Le descendre
        # d'office dans la nouvelle page ferait perdre le fil.
        self._cible = -1
        self._rafraichir()

    # ------------------------------------------------------------------
    #  Navigation D-pad
    # ------------------------------------------------------------------
    #
    # [NAV 10/08/2026] Reprise du modele des apps Appels et Contacts, et
    # non d'un defilement de liste comme le Portefeuille.
    #
    # La difference tient a ce qu'il y a sur l'ecran : le Portefeuille
    # affiche des lignes qu'on ne fait que LIRE, donc faire glisser la vue
    # suffit. Ici chaque carte porte un bouton -- Prendre, Retirer,
    # Terminer -- et chaque champ se remplit. Sans curseur, ces actions
    # sont hors de portee au clavier, or le CircusPhone se pilote au
    # clavier : la souris appartient a Star Citizen.
    #
    # Les cibles sont reconstruites a chaque _rafraichir(), dans l'ordre
    # d'apparition a l'ecran. La cible -1 est la barre d'onglets.

    def _nav_add(self, widget, action=None, champ=None, grille=None):
        """Enregistre un widget comme cible du D-pad, dans l'ordre.

        `grille` identifie une zone a DEUX COLONNES (les boutons de
        metier). Dans une telle zone, le D-pad se deplace en deux
        dimensions : gauche/droite changent de colonne, haut/bas de
        rangee. Sans ca, atteindre le bouton de droite demandait de
        descendre -- ce qui ne correspond a rien de ce qu'on voit, la
        grille etant lue en lignes.
        """
        self._nav.append((widget, widget.styleSheet(), action, champ,
                          grille))

    def _peindre_cible(self):
        for i, (w, base, _a, _c, _g) in enumerate(self._nav):
            try:
                _halo(w, base, i == self._cible)
            except Exception:
                pass
        try:
            self._onglets.set_nav_highlight(self._cible == -1)
        except Exception:
            pass
        try:
            _halo(self._bandeau, self._bandeau.styleSheet_base(),
                  self._cible == -2)
        except Exception:
            pass
        self._voir_cible()

    def _voir_cible(self):
        """Fait defiler pour que la cible reste visible.

        Sans ca, le curseur descend hors de l'ecran et le joueur navigue
        a l'aveugle -- il voit une liste immobile et croit que rien ne
        repond.
        """
        if not (0 <= self._cible < len(self._nav)):
            return
        try:
            self._zone.ensureWidgetVisible(self._nav[self._cible][0], 0, 40)
        except Exception:
            pass

    def dans_champ(self) -> bool:
        """True quand un champ de saisie a le focus clavier.

        Interroge par _PhoneNavKeyListener : sans cette methode, l'app est
        traitee comme "jamais dans un champ", les fleches sont volees au
        curseur de saisie et le retour arriere quitte l'app au lieu
        d'effacer un caractere.
        """
        return self._dans_champ_

    def champ_courant_vide(self) -> bool:
        """True si le champ focalise est vide.

        Champ NON vide : le retour arriere efface. Champ vide : il
        ressort. Sans cette regle, sortir d'un champ rempli mange la
        derniere lettre qu'on vient de taper.
        """
        w = self._widget_champ()
        if w is None:
            return True
        try:
            if hasattr(w, "toPlainText"):
                return not w.toPlainText().strip()
            return not w.text().strip()
        except Exception:
            return True

    def _widget_champ(self):
        if not (0 <= self._cible < len(self._nav)):
            return None
        return self._nav[self._cible][3]

    def _entrer_dans_champ(self):
        w = self._widget_champ()
        if w is None:
            return
        self._dans_champ_ = True
        # L'overlay porte le flag Qt.Tool : un setFocus() seul ne donne
        # PAS le focus clavier systeme, les frappes partiraient dans Star
        # Citizen. entrer_dans_champ() force la fenetre au premier plan,
        # comme le fait l'ecran conversation.
        ov = self.window()
        fn = getattr(ov, "entrer_dans_champ", None)
        if fn is not None:
            fn(w)
        else:
            try:
                w.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass

    def _sortir_du_champ(self):
        self._dans_champ_ = False
        try:
            w = self._widget_champ()
            if w is not None:
                w.clearFocus()
            self.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    def handle_nav(self, direction):
        if self._dans_champ_:
            # Dans un champ, seules deux touches nous concernent ; le
            # reste appartient au QLineEdit (frappe, curseur, effacement).
            if direction == "enter":
                self._sortir_du_champ()
                self._descendre()
            elif direction == "esc" and self.champ_courant_vide():
                self._sortir_du_champ()
            return True

        n = len(self._nav)
        if direction == "up":
            self._monter()
            return True
        if direction == "down":
            self._descendre()
            return True
        if direction in ("left", "right"):
            pas = 1 if direction == "right" else -1
            if self._cible == -2:
                # Sur le bandeau, gauche/droite ne font rien : il n'y a
                # qu'un bandeau, et changer d'onglet depuis ici ferait
                # perdre de vue ce qu'on etait en train de consulter.
                return True
            if self._cible < 0:
                # Depuis la barre : on change d'onglet.
                cur = self._onglets.courant()
                self._onglets.selectionner(
                    (cur + pas) % len(self._onglets._btns))
            elif self._grille_de(self._cible) is not None:
                self._lateral(pas)
            # Hors grille, gauche/droite ne font rien : elles
            # emporteraient le joueur hors de la page qu'il lit.
            return True
        if direction == "enter":
            if self._cible == -2:
                # Deploie ou replie le bandeau : le detail de la mission
                # en cours vient COUVRIR l'onglet actif. Il n'a pas
                # d'onglet a lui -- ce n'est pas une destination, c'est un
                # coup d'oeil sur ce qu'on fait deja.
                self._deploye = not self._deploye
                self._cible = -2
                self._rafraichir()
                return True
            if self._cible < 0:
                # Depuis la barre : on descend dans la page.
                self._descendre()
                return True
            if 0 <= self._cible < n:
                _w, _b, action, champ, _g = self._nav[self._cible]
                if champ is not None:
                    self._entrer_dans_champ()
                elif action is not None:
                    action()
            return True
        return False

    _COLONNES = 2   # largeur des grilles de metiers

    def _grille_de(self, i):
        """Identifiant de grille de la cible i, ou None."""
        if 0 <= i < len(self._nav):
            return self._nav[i][4]
        return None

    def _bornes_grille(self, i):
        """(debut, fin) inclusifs de la grille contenant i."""
        g = self._grille_de(i)
        deb = fin = i
        while deb - 1 >= 0 and self._grille_de(deb - 1) == g:
            deb -= 1
        while fin + 1 < len(self._nav) and self._grille_de(fin + 1) == g:
            fin += 1
        return deb, fin

    def _descendre(self):
        i = self._cible
        if self._deploye and i < 0:
            # La barre d'onglets est sous le panneau : la traverser
            # ferait un arret sur un element qu'on ne voit pas.
            self._cible = 0 if self._nav else -2
            self._peindre_cible()
            return
        g = self._grille_de(i)
        if g is not None:
            deb, fin = self._bornes_grille(i)
            j = i + self._COLONNES
            # Depasser la derniere rangee sort de la grille par le bas,
            # plutot que de rester bloque dessus.
            self._cible = j if j <= fin else min(len(self._nav) - 1, fin + 1)
        else:
            self._cible = min(len(self._nav) - 1, i + 1)
        self._peindre_cible()

    def _mini(self):
        """Index le plus haut atteignable : -2 si un bandeau est
        deployable, -1 sinon."""
        return -2 if self._etat.mission_en_cours is not None else -1

    def _monter(self):
        i = self._cible
        if self._deploye and i <= 0:
            self._cible = -2
            self._peindre_cible()
            return
        g = self._grille_de(i)
        if g is not None:
            deb, _fin = self._bornes_grille(i)
            j = i - self._COLONNES
            self._cible = j if j >= deb else max(self._mini(), deb - 1)
        else:
            self._cible = max(self._mini(), i - 1)
        self._peindre_cible()

    def _lateral(self, pas):
        """Gauche/droite DANS une grille : change de colonne.

        Borne a la rangee courante : deborder ferait sauter le curseur
        d'une ligne a l'autre, ce qu'aucun tableau ne fait.
        """
        i = self._cible
        deb, fin = self._bornes_grille(i)
        rang_deb = deb + ((i - deb) // self._COLONNES) * self._COLONNES
        rang_fin = min(fin, rang_deb + self._COLONNES - 1)
        j = i + pas
        if rang_deb <= j <= rang_fin:
            self._cible = j
            self._peindre_cible()

    def handle_back(self):
        """Retour : ferme d'abord le formulaire, puis quitte l'app."""
        if self._deploye:
            self._deploye = False
            self._cible = -2
            self._rafraichir()
            return True
        if self._creation:
            self._creation = False
            self._erreur = ""
            self._rafraichir()
            return True
        return False

    def on_show(self):
        # Les missions et l'age affiche changent hors de l'app : on
        # reconstruit a chaque ouverture plutot que de garder un ecran
        # fige sur l'etat d'il y a une heure.
        # L'etat vient du SERVEUR : on le redemande a chaque ouverture
        # plutot que de reafficher celui d'il y a une heure. Les missions
        # des autres joueurs ont bouge entre-temps.
        self._creation = False
        self._rafraichir()
        _envoyer({"type": "travail_liste"})

    # -- actions --

    def _demander(self, type_, **champs):
        """Envoie une intention au serveur et attend sa reponse.

        Le client NE MODIFIE RIEN de lui-meme. C'est ce qui garantit que
        deux joueurs prenant la meme mission ne voient pas chacun un
        ecran leur donnant raison : seul le serveur tranche, et l'ecran
        se redessine a l'arrivee de travail_etat.
        """
        if not _envoyer({"type": type_, **champs}):
            self._erreur = ("Serveur injoignable : reconnectez-vous pour "
                            "publier ou prendre une mission.")
            self._rafraichir()

    def appliquer_etat(self, data):
        """Recoit travail_etat du serveur et redessine."""
        self._etat.appliquer(data)
        err = (data or {}).get("erreur") or ""
        self._erreur = err
        if not err and self._creation:
            # Publication acceptee : on referme le formulaire. Le garder
            # ouvert laisserait croire que rien ne s'est passe.
            self._creation = False
        self._rafraichir()

    def _basculer_metier(self, metier):
        actuels = list(self._etat.metiers)
        if metier in actuels:
            actuels.remove(metier)
        else:
            if len(actuels) >= T.METIERS_MAX:
                self._erreur = (
                    f"{T.METIERS_MAX} métiers maximum. Décochez-en un "
                    f"d'abord.")
                self._rafraichir()
                return
            actuels.append(metier)
        # Validation locale AVANT l'envoi : elle evite un aller-retour
        # pour une erreur que le client voit seul. Le serveur la refait --
        # c'est lui qui fait autorite, un client bricole ne doit pas
        # pouvoir cocher huit metiers.
        try:
            propres = T.valide_metiers_joueur(actuels)
            self._erreur = ""
        except T.TravailError as e:
            self._erreur = str(e)
            self._rafraichir()
            return
        self._demander("travail_metiers", metiers=propres,
                       notifs=self._etat.notifs)

    def _publier(self):
        # Validation locale d'abord : elle rend l'erreur de saisie
        # immediate, sans aller-retour. Le serveur revalide tout -- le
        # plafond de missions ouvertes, lui, ne peut etre verifie que par
        # lui, puisqu'il seul connait toutes les annonces.
        try:
            T.valide_metier(self._sel_metier)
            T.valide_titre(self._ed_titre.text())
            T.valide_paiement(self._ed_paiement.text())
            T.valide_description(self._ed_desc.toPlainText())
        except T.TravailError as e:
            self._erreur = str(e)
            self._rafraichir()
            return
        self._demander("travail_publier",
                       metier=self._sel_metier,
                       titre=self._ed_titre.text(),
                       paiement=self._ed_paiement.text(),
                       description=self._ed_desc.toPlainText())

    # -- rendu --

    def _rafraichir(self):
        # Les cibles sont reconstruites a chaque rendu : les widgets
        # precedents viennent d'etre detruits par setWidget(), garder
        # leurs references ferait planter le halo au tour suivant.
        self._nav = []
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(2, 6, 2, 6)
        v.setSpacing(6)

        if self._erreur:
            e = QLabel(self._erreur)
            e.setWordWrap(True)
            e.setStyleSheet(
                f"color:{_ROUGE};font-size:9pt;"
                f"background:rgba(229,72,77,0.10);"
                f"border-radius:8px;padding:6px 10px;")
            v.addWidget(e)

        m_cours = self._etat.mission_en_cours
        if m_cours is None:
            # Plus de mission : un panneau deploye n'aurait plus de sujet.
            self._deploye = False

        onglet = self._onglets.courant()
        # `pret` : a-t-on deja recu une reponse du serveur ?
        #
        # [TRAVAIL 11/08/2026] Sans cette condition, la bascule se
        # declenchait a la CONSTRUCTION de l'ecran -- avant que
        # travail_etat n'arrive, donc sur un etat vide. Un joueur ayant
        # deja ses metiers ouvrait quand meme sur l'onglet Metiers, et
        # rien ne l'y ramenait ensuite. On ne decide pas sur un etat
        # qu'on n'a pas encore.
        if self._etat.pret and not self._etat.metiers \
                and onglet != self.ONGLET_METIERS:
            # Sans metier, l'onglet Missions serait vide sans dire
            # pourquoi. On bascule vers le choix : c'est la premiere
            # chose a faire, et la seule qui debloque le reste.
            #
            # [NAV 10/08/2026] Bascule SILENCIEUSE, sans passer par
            # selectionner() : celui-ci appelle _changer_onglet, qui
            # rappelle _rafraichir -- donc une RECURSION en plein rendu.
            # La passe interne remplissait self._nav de ses widgets, la
            # passe externe ajoutait les siens a la meme liste, puis
            # setWidget() detruisait ceux de la passe interne. Le curseur
            # pointait alors sur des widgets morts : le halo echouait en
            # silence et la selection disparaissait des qu'on appuyait
            # sur Bas.
            self._onglets._courant = self.ONGLET_METIERS
            self._onglets._peindre()
            onglet = self.ONGLET_METIERS

        if onglet == self.ONGLET_MISSIONS:
            self._peindre_missions(v)
        elif onglet == self.ONGLET_MIENNES:
            self._peindre_miennes(v)
        else:
            self._peindre_metiers(v)

        v.addStretch(1)
        self._zone.setWidget(page)
        self._bandeau.montrer(self._etat.mission_en_cours)
        # [NAV 11/08/2026] Panneau ouvert : on REMET LE PARCOURS A ZERO
        # avant de le construire. Les cibles de la page sont dessous, donc
        # invisibles : les laisser dans le parcours faisait descendre le
        # curseur sur des cartes cachees, et le joueur ne voyait plus rien
        # bouger. Seul ce qui est visible doit etre atteignable.
        if self._deploye and self._etat.mission_en_cours is not None:
            self._nav = []
        self._maj_panneau()
        # Le curseur remonte a la barre d'onglets si la page s'est
        # raccourcie sous lui (mission prise, formulaire ferme) : le
        # laisser pointer dans le vide bloquerait toute navigation.
        if self._cible >= len(self._nav):
            self._cible = self._mini()
        self._peindre_cible()

    def resizeEvent(self, ev):
        """Le panneau flotte hors layout : personne ne le redimensionne
        a notre place. Sans ca, il garderait la largeur qu'il avait a
        l'ouverture et deborderait de l'ecran."""
        super().resizeEvent(ev)
        if self._panneau.isVisible():
            g = self._panneau.geometry()
            self._panneau.setGeometry(
                self._geo_panneau(True, g.height()))

    def _debrancher_fin(self):
        """Detache le masquage differe de l'animation.

        [ANIM 11/08/2026] Un drapeau plutot qu'un disconnect() aveugle :
        appeler disconnect() sur un signal sans connexion emet un
        RuntimeWarning a chaque ouverture du panneau. Le try/except ne
        l'attrapait pas -- Qt n'y leve pas d'exception, il ecrit sur la
        sortie d'erreur. Du bruit permanent dans le log, et c'est
        exactement ce qui finit par masquer un vrai avertissement.
        """
        if getattr(self, "_fin_branchee", False):
            try:
                self._anim.finished.disconnect(self._panneau.hide)
            except (TypeError, RuntimeError):
                pass
            self._fin_branchee = False

    def _geo_panneau(self, ouvert, hauteur):
        """Rectangle du panneau : juste sous le bandeau, meme largeur.

        Ferme, il a une hauteur nulle -- il est donc invisible sans avoir
        a le deplacer hors de l'ecran, et l'animation part exactement du
        bord bas du bandeau.
        """
        b = self._bandeau.geometry()
        y = b.y() + b.height()
        return QRect(b.x(), y, b.width(), hauteur if ouvert else 0)

    def _maj_panneau(self):
        """Construit, ouvre ou referme le panneau selon l'etat."""
        m = self._etat.mission_en_cours
        if not self._deploye or m is None:
            if self._panneau.isVisible():
                self._anim.stop()
                self._anim.setStartValue(self._panneau.geometry())
                self._anim.setEndValue(self._geo_panneau(False, 0))
                # Masquer a la FIN, pas au depart : sinon le panneau
                # disparait d'un coup et l'animation ne se voit pas.
                self._debrancher_fin()
                self._anim.finished.connect(self._panneau.hide)
                self._fin_branchee = True
                self._anim.start()
            return

        while self._pan_lay.count():
            it = self._pan_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
        self._peindre_en_cours(self._pan_lay, m)

        larg = self._bandeau.width()
        h = self._panneau.sizeHint().height()
        if h <= 0:
            h = 200
        h = min(h, max(120, self.height() - self._bandeau.y()
                       - self._bandeau.height() - 12))
        self._panneau.raise_()
        if not self._panneau.isVisible():
            self._panneau.setGeometry(self._geo_panneau(False, 0))
            self._panneau.show()
        self._anim.stop()
        self._debrancher_fin()
        self._anim.setStartValue(self._panneau.geometry())
        self._anim.setEndValue(self._geo_panneau(True, h))
        self._anim.start()

    def _peindre_en_cours(self, v, m):
        """Detail de la mission en cours, deploye par-dessus l'onglet.

        [BANDEAU 11/08/2026] Ce n'est pas un quatrieme onglet : la mission
        en cours est un ETAT, pas une destination. On y jette un coup
        d'oeil et on revient d'ou on venait -- d'ou le retour arriere qui
        replie plutot que de quitter l'app.
        """
        titre = QLabel(m.get("titre", ""))
        titre.setWordWrap(True)
        titre.setStyleSheet(
            f"color:{_TXT};font-size:11pt;font-weight:700;"
            f"background:transparent;border:none;")
        v.addWidget(titre)

        h = QHBoxLayout()
        h.setSpacing(6)
        p = QLabel(T.libelle_metier(m.get("metier")))
        p.setObjectName("pastille")
        p.setStyleSheet(
            f"color:{_ACCENT};font-size:8pt;font-weight:600;"
            f"background:rgba(47,111,237,0.10);"
            f"border-radius:8px;padding:2px 8px;")
        h.addWidget(p)
        pay = QLabel(T.paiement_texte(m.get("paiement")))
        pay.setStyleSheet(f"color:{_VERT};font-size:9pt;font-weight:600;")
        h.addWidget(pay)
        h.addStretch(1)
        v.addLayout(h)

        desc = (m.get("description") or "").strip()
        if desc:
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet(f"color:{_TXT};font-size:9pt;")
            v.addWidget(d)

        # Le numero de l'auteur est l'information la plus utile de cet
        # ecran : c'est lui qu'on appelle pour dire qu'on arrive.
        contact = QLabel(f"Contact : {m.get('auteur', '')}"
                         f"   ·   Prise {T.age_texte(m.get('pris_le'))}")
        contact.setWordWrap(True)
        contact.setStyleSheet(f"color:{_MUTED};font-size:9pt;")
        v.addWidget(contact)

        # [TRAVAIL 11/08/2026] Un seul bouton : ABANDONNER.
        #
        # Terminer appartient a l'auteur, pas a l'executant -- celui-ci
        # signerait sa propre livraison. L'executant garde en revanche le
        # droit de rendre la mission a tout moment, ce qui l'empeche
        # d'etre bloque par un auteur qui ne revient pas.
        b = QPushButton("Abandonner la mission")
        b.setCursor(Qt.PointingHandCursor)
        b.setMinimumHeight(34)
        b.setStyleSheet(
            f"QPushButton{{border:1px solid {_ROUGE};color:{_ROUGE};"
            f"background:transparent;border-radius:10px;"
            f"padding:6px;font-size:9pt;font-weight:600;}}")
        act = (lambda mm=m: self._demander("travail_abandonner",
                                           id=mm.get("id")))
        b.clicked.connect(lambda _=False, f=act: f())
        v.addWidget(b)
        self._nav_add(b, act)

        note = QLabel("Seul l'auteur peut marquer la mission terminée.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        v.addWidget(note)

    def _peindre_missions(self, v):
        moi = self._mon_numero()
        en_cours = self._etat.mission_en_cours
        liste = self._etat.missions
        if not self._etat.pret:
            # Tant que le serveur n'a pas repondu, on ne pretend pas que
            # la liste est vide : "aucune mission" et "pas encore recu"
            # sont deux choses differentes, et les confondre ferait
            # croire a un tableau desert alors qu'il se remplit.
            v.addWidget(_vide("Chargement…"))
            return
        if not liste:
            v.addWidget(_vide(
                "Aucune mission pour vos métiers.\n"
                "Les annonces publiées par d'autres joueurs "
                "apparaîtront ici."))
            return
        for m in liste:
            actions = []
            action_nav = None
            if m.get("auteur") == moi:
                # Sa propre annonce, reconnaissable sans avoir a lire le
                # numero : sinon on essaie de la prendre et on se prend
                # un refus.
                actions.append(("La vôtre", lambda: None, False))
            elif en_cours is None:
                action_nav = (lambda mm=m: self._demander(
                    "travail_prendre", id=mm.get("id")))
                actions.append(("Prendre", action_nav, False))
            carte = _Carte(m, actions)
            v.addWidget(carte)
            # La CARTE entiere est la cible, pas son bouton : c'est elle
            # qu'on lit, et la surligner montre de quelle annonce on
            # parle. Entree declenche son action principale.
            self._nav_add(carte, action_nav)

    def _peindre_miennes(self, v):
        if self._creation:
            self._peindre_formulaire(v)
            return

        moi = self._mon_numero()
        b = QPushButton("+  Nouvelle mission")
        b.setCursor(Qt.PointingHandCursor)
        b.setMinimumHeight(36)
        b.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:#ffffff;border:none;"
            f"border-radius:10px;font-size:9pt;font-weight:600;}}")
        b.clicked.connect(self._ouvrir_formulaire)
        v.addWidget(b)
        self._nav_add(b, self._ouvrir_formulaire)

        liste = self._etat.miennes
        if not liste:
            v.addWidget(_vide("Vous n'avez publié aucune mission."))
            return
        for m in liste:
            actions = []
            action_nav = None
            if m.get("etat") == T.ETAT_OUVERTE:
                action_nav = (lambda mm=m: self._demander(
                    "travail_retirer", id=mm.get("id")))
                actions.append(("Retirer", action_nav, True))
            else:
                # Prise : on ne peut plus retirer, seulement terminer.
                # Le numero de l'executant est affiche pour pouvoir
                # l'appeler -- c'est tout l'interet de l'annonce.
                pris = QLabel(f"Prise par {m.get('executant', '')}")
                pris.setStyleSheet(f"color:{_VERT};font-size:8pt;")
                v.addWidget(pris)
                action_nav = (lambda mm=m: self._demander(
                    "travail_clore", id=mm.get("id")))
                actions.append(("Terminer", action_nav, False))
            carte = _Carte(m, actions)
            v.addWidget(carte)
            self._nav_add(carte, action_nav)

    def _ouvrir_formulaire(self):
        self._creation = True
        self._sel_metier = T.METIERS[0]
        self._erreur = ""
        self._rafraichir()

    def _peindre_formulaire(self, v):
        lbl = QLabel("Métier recherché")
        lbl.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        v.addWidget(lbl)

        # Grille 2 colonnes, comme l'ecran de choix : le joueur retrouve
        # la meme disposition, donc il n'a pas a la reapprendre.
        self._btns_form = []
        for i in range(0, len(T.METIERS), 2):
            h = QHBoxLayout()
            h.setSpacing(6)
            for metier in T.METIERS[i:i + 2]:
                b = _BoutonMetier(metier)
                b.peindre(metier == self._sel_metier, False)
                b.clicked.connect(
                    lambda _=False, mm=metier: self._choisir_cible(mm))
                h.addWidget(b, stretch=1)
                self._btns_form.append(b)
                self._nav_add(
                    b, (lambda mm=metier: self._choisir_cible(mm)),
                    grille="cible")
            v.addLayout(h)

        self._ed_titre = QLineEdit()
        self._ed_titre.setPlaceholderText("Titre de la mission")
        self._ed_titre.setMaxLength(T.TITRE_MAX_LEN)
        self._ed_titre.setMinimumHeight(34)
        # Nomme pour que le halo le vise : sans nom d'objet, _halo
        # retombe sur "QPushButton", qui ne designe pas un champ
        # de saisie -- la selection y devenait invisible.
        self._ed_titre.setObjectName("champ")
        self._ed_titre.setStyleSheet(_STYLE_CHAMP)
        v.addWidget(self._ed_titre)
        self._nav_add(self._ed_titre, champ=self._ed_titre)

        self._ed_paiement = QLineEdit()
        # Texte libre : "part du butin", "à discuter", "500k + carburant".
        # Un champ numerique obligerait a mentir dans la description a
        # chaque fois que le paiement n'est pas une somme.
        self._ed_paiement.setPlaceholderText("Paiement (texte libre)")
        self._ed_paiement.setMaxLength(T.PAIEMENT_MAX_LEN)
        self._ed_paiement.setMinimumHeight(34)
        self._ed_paiement.setObjectName("champ")
        self._ed_paiement.setStyleSheet(_STYLE_CHAMP)
        v.addWidget(self._ed_paiement)
        self._nav_add(self._ed_paiement, champ=self._ed_paiement)

        self._ed_desc = QTextEdit()
        self._ed_desc.setPlaceholderText("Description (facultative)")
        self._ed_desc.setFixedHeight(90)
        self._ed_desc.setObjectName("champ")
        self._ed_desc.setStyleSheet(_STYLE_CHAMP)
        v.addWidget(self._ed_desc)
        self._nav_add(self._ed_desc, champ=self._ed_desc)

        h = QHBoxLayout()
        h.setSpacing(6)
        annuler = QPushButton("Annuler")
        annuler.setCursor(Qt.PointingHandCursor)
        annuler.setStyleSheet(
            f"QPushButton{{border:1px solid {_SEP};color:{_MUTED};"
            f"background:transparent;border-radius:10px;padding:8px;"
            f"font-size:9pt;}}")
        annuler.clicked.connect(self.handle_back)
        h.addWidget(annuler, stretch=1)
        # Rangee de deux : gauche/droite passe de l'un a l'autre, et Bas
        # sort du formulaire d'un coup. Sans grille, ces deux boutons
        # cote a cote se parcouraient VERTICALEMENT -- le curseur
        # descendait pour aller a droite, ce qui ne correspond a rien de
        # ce qu'on voit.
        self._nav_add(annuler, self.handle_back, grille="actions")
        publier = QPushButton("Publier")
        publier.setCursor(Qt.PointingHandCursor)
        publier.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};color:#ffffff;border:none;"
            f"border-radius:10px;padding:8px;font-size:9pt;"
            f"font-weight:600;}}")
        publier.clicked.connect(self._publier)
        h.addWidget(publier, stretch=1)
        self._nav_add(publier, self._publier, grille="actions")
        v.addLayout(h)

    def _choisir_cible(self, metier):
        self._sel_metier = metier
        for b in self._btns_form:
            b.peindre(b.metier == metier, False)
        # peindre() vient de reecrire les styles : les bases memorisees
        # dans _nav sont perimees et le halo a saute. On les resynchronise
        # sans reconstruire la page -- reconstruire ici viderait les
        # champs de saisie deja remplis.
        self._nav = [(w, w.styleSheet() if w in self._btns_form else base,
                      a, c, g)
                     for (w, base, a, c, g) in self._nav]
        self._peindre_cible()

    def _peindre_metiers(self, v):
        choisis = self._etat.metiers
        bloque = len(choisis) >= T.METIERS_MAX

        lbl = QLabel(f"Vos métiers  ({len(choisis)}/{T.METIERS_MAX})")
        lbl.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        v.addWidget(lbl)

        for i in range(0, len(T.METIERS), 2):
            h = QHBoxLayout()
            h.setSpacing(6)
            for metier in T.METIERS[i:i + 2]:
                b = _BoutonMetier(metier)
                b.peindre(metier in choisis,
                          bloque and metier not in choisis)
                b.clicked.connect(
                    lambda _=False, mm=metier: self._basculer_metier(mm))
                h.addWidget(b, stretch=1)
                self._nav_add(
                    b, (lambda mm=metier: self._basculer_metier(mm)),
                    grille="metiers")
            v.addLayout(h)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{_SEP};")
        v.addWidget(sep)

        self._btn_notif = QPushButton()
        self._btn_notif.setCursor(Qt.PointingHandCursor)
        self._btn_notif.setMinimumHeight(36)
        self._peindre_notif()
        self._btn_notif.clicked.connect(self._basculer_notif)
        v.addWidget(self._btn_notif)
        self._nav_add(self._btn_notif, self._basculer_notif)

    def _peindre_notif(self):
        on = self._etat.notifs
        # Case a cocher dessinee au caractere plutot qu'un QCheckBox :
        # l'ecran du telephone n'a pas de curseur souris fin, et une
        # cible de 36 px de haut se clique sans viser.
        self._btn_notif.setText(
            ("\u2611  " if on else "\u2610  ")
            + "Me notifier des nouvelles missions")
        self._btn_notif.setStyleSheet(
            f"QPushButton{{text-align:left;padding:8px 10px;"
            f"border:1px solid {_VERT if on else _SEP};"
            f"color:{_TXT if on else _MUTED};"
            f"background:{'rgba(63,185,80,0.10)' if on else _BG};"
            f"border-radius:10px;font-size:9pt;}}")

    def _basculer_notif(self):
        self._demander("travail_metiers", metiers=self._etat.metiers,
                       notifs=not self._etat.notifs)
        return
        # [NAV 10/08/2026] _rafraichir() et non _peindre_notif() : ce
        # dernier reecrit la feuille de style du bouton, donc EFFACE le
        # halo du curseur. Le curseur etait toujours la, il ne se voyait
        # plus -- et le joueur croyait avoir perdu la main.
        #
        # C'est le piege de tout widget qui se repeint lui-meme alors
        # qu'un halo est pose dessus : le style de selection et le style
        # d'etat vivent au meme endroit. Passer par le rendu complet
        # reconstruit les deux dans le bon ordre.
        self._rafraichir()
