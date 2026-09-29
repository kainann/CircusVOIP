#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[ANNONCES 21/09/2026] App « Annonces » du CircusPhone.

Petites annonces PUBLIQUES entre joueurs : recruter pour un corps
(secouristes, securite), annoncer un evenement, vendre ou chercher un
objet. Tout joueur connecte voit toutes les annonces.

Les regles vivent dans circusvoip_phone_annonces.py (partage avec le
serveur) ; ce fichier ne fait que les montrer.

--- Le serveur decide, le client dessine ---

Meme discipline que l'app Travail : chaque action envoie une INTENTION
(annonces_publier, annonces_retirer) et l'ecran ne change qu'a l'arrivee
de annonces_etat. Rien n'est ajoute ni retire localement.

--- Pas de signal dedie cote client ---

Les trames annonces_* suivent le chemin deja ouvert aux jeux
multijoueurs : circusvoip_client les route vers l'app COURANTE, qui les
recoit par handle_server_msg(). Une app fermee ne recoit donc rien --
et n'en a pas besoin : on_show() redemande la liste a chaque ouverture,
et il n'y a ni badge ni son (decision du 21/09/2026 : sur un tableau
public, sonner a chaque annonce reveillerait tout le serveur pour la
vente d'un casque).

--- La saisie n'est JAMAIS effacee par un autre joueur ---

Le tableau etant public, une publication de n'importe qui pousse un
nouvel etat a tous les joueurs qui ont l'app ouverte. L'app Travail
reconstruit sa page a chaque etat recu, formulaire compris, et efface ce
qui etait en train d'etre tape (point encore ouvert sur Travail). Ici :
  - une POUSSEE (reponse=False) recue pendant la saisie est MEMORISEE
    sans redessiner ; l'ecran se met a jour a la sortie du formulaire ;
  - quand l'ecran se redessine quand meme (erreur de publication), le
    texte deja tape est recopie dans le nouveau champ.

--- Deux onglets ---

  Annonces      : tout le tableau, de la plus recente a la plus ancienne.
  Mes annonces  : les miennes, le bouton de publication, le retrait.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QTextEdit,
    QVBoxLayout, QWidget,
)

from circusvoip_phone_apps import PhoneApp
import circusvoip_phone_annonces as A

# ---------------------------------------------------------------------
#  Icone
# ---------------------------------------------------------------------
#
# Porte-voix dessine pour l'occasion (formes simples, pas un emoji
# existant). Injecte dans la table de circusvoip_phone_apps plutot
# qu'ajoute a ce fichier-la : ainsi make_phone_icon("annonces") -- que
# l'overlay appelle pour toutes les icones -- le dessine avec la meme
# tuile que les autres, sans avoir a redeployer circusvoip_phone_apps.
_SVG_ANNONCES = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
    '<path fill="#F4900C" d="M4 13h6l14-8v26l-14-8H4c-1.1 0-2-.9-2-2v-6'
    'c0-1.1.9-2 2-2z"/>'
    '<path fill="#FFCC4D" d="M24 5c1.7 0 3 5.8 3 13s-1.3 13-3 13V5z"/>'
    '<path fill="#66757F" d="M7 23h5l2 8c.2.9-.5 2-1.5 2h-1.3c-.7 0-1.3'
    '-.5-1.5-1.2L7 23z"/>'
    '<path fill="none" stroke="#CCD6DD" stroke-width="2" '
    'stroke-linecap="round" d="M30 12c1.3 1.6 2 3.7 2 6s-.7 4.4-2 6"/>'
    '</svg>')

try:
    from circusvoip_phone_apps import _EMOJI_SVG as _TABLE_ICONES
    _TABLE_ICONES.setdefault("annonces", _SVG_ANNONCES)
except Exception:
    pass

try:
    from circusvoip_phone_apps import LazyPhoneIcon as _LazyPhoneIcon
except Exception:
    _LazyPhoneIcon = None

# Palette : reprise a l'identique de l'app Travail. Redefinir une teinte
# ici ferait deriver les deux ecrans a la premiere retouche.
_BG     = "#ffffff"
_TXT    = "#1a1a1a"
_MUTED  = "#9aa0a6"
_ACCENT = "#2f6fed"
_VERT   = "#3fb950"
_ROUGE  = "#e5484d"
_OR     = "#b7791f"
_SEP    = "#e6e8eb"
_GRISE  = "#f1f3f4"
_HALO   = "#000000"   # noir : un halo bleu disparait sur un bouton bleu

_STYLE_CHAMP = (
    f"QLineEdit,QTextEdit{{color:{_TXT};background:{_BG};"
    f"border:1px solid {_SEP};border-radius:10px;padding:6px 8px;"
    f"font-size:9pt;}}"
    f"QLineEdit::placeholder,QTextEdit::placeholder{{color:{_MUTED};}}"
)

# Couleur de pastille par categorie : c'est sur elle que l'oeil balaye
# la liste, elle doit se reperer sans lecture.
_COUL_CAT = {
    A.CAT_RECRUTEMENT: _ACCENT,
    A.CAT_EVENEMENT:   "#8250df",
    A.CAT_VENTE:       _VERT,
}


def _envoyer(payload: dict) -> bool:
    """Envoie une trame au serveur par le coeur. False si hors ligne."""
    try:
        import circusvoip_core as _core
        return bool(_core._ws_send_safe(payload))
    except Exception:
        return False


def _journal(texte: str):
    """Trace dans le journal debug du client, best-effort.

    Le client avale les exceptions de handle_server_msg : sans cette
    trace, une erreur de rendu laisserait un ecran fige sans aucun
    indice.
    """
    try:
        import circusvoip_core as _core
        _core._dbg_log(f"[ANNONCES] {texte}")
    except Exception:
        pass


# ---------------------------------------------------------------------
#  Fragments d'interface (copies de Travail, pas importes : une erreur
#  dans l'app Travail ne doit pas emporter celle-ci)
# ---------------------------------------------------------------------

def _halo(widget, base, selected):
    """Halo de selection sur le CADRE du widget, jamais sur ses enfants.

    Cf. circusvoip_phone_travail_app._halo pour le detail des trois
    formes de feuille de style.
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
    widget.setStyleSheet(base + f"\n{cible}{{border:2px solid {_HALO};}}")


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


def _bouton(texte, couleur=_ACCENT, plein=False, danger=False):
    b = QPushButton(texte)
    b.setCursor(Qt.PointingHandCursor)
    coul = _ROUGE if danger else couleur
    if plein:
        b.setStyleSheet(
            f"QPushButton{{background:{coul};color:#ffffff;border:none;"
            f"border-radius:10px;padding:8px;font-size:9pt;"
            f"font-weight:600;}}")
    else:
        b.setStyleSheet(
            f"QPushButton{{border:1px solid {coul};color:{coul};"
            f"background:transparent;border-radius:8px;"
            f"padding:4px 12px;font-size:9pt;}}")
    return b


class _Onglets(QWidget):
    """Barre d'onglets, meme rendu que Travail / Appels / Contacts."""

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


class _Carte(QFrame):
    """Une annonce. Les boutons sont exposes dans `self.boutons` pour que
    l'app les inscrive au parcours du D-pad (cf. Travail, 03/09/2026 :
    un bouton que seule la souris atteint n'existe pas pour le joueur)."""

    def __init__(self, annonce, contact_affiche, actions, bandeau=None,
                 bandeau_couleur=None, parent=None):
        super().__init__(parent)
        self.setObjectName("carte")
        self.setStyleSheet(
            f"#carte{{background:{_BG};"
            f"border:1px solid {_SEP};border-radius:10px;}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        haut = QHBoxLayout()
        haut.setSpacing(6)
        cat = annonce.get("categorie")
        coul = _COUL_CAT.get(cat, _ACCENT)
        p = QLabel(A.libelle_categorie(cat))
        p.setStyleSheet(
            f"color:{coul};font-size:8pt;font-weight:600;"
            f"background:{_GRISE};border-radius:8px;padding:2px 8px;")
        haut.addWidget(p)
        off = str(annonce.get("officiel") or "")
        if off:
            # Badge calcule par le SERVEUR : le client ne peut pas se
            # l'attribuer, il ne fait que l'afficher.
            #
            # SOUS la pastille et non a cote : « Chef de la sécurité » a
            # cote de « Recrutement » depasse la largeur d'un ecran de
            # telephone en 2K, et la zone se mettait a defiler a
            # l'horizontale.
            b = QLabel(f"★ {off}")
            b.setStyleSheet(
                f"color:{_OR};font-size:8pt;font-weight:700;"
                f"background:rgba(183,121,31,0.12);"
                f"border-radius:8px;padding:2px 8px;")
            haut.addStretch(1)
            v.addLayout(haut)
            haut = QHBoxLayout()
            haut.setSpacing(6)
            haut.addWidget(b)
        haut.addStretch(1)
        # L'age n'est PAS sur cette ligne : avec la pastille et le badge
        # « Chef de la sécurité », elle depassait la largeur de l'ecran et
        # la zone defilait a l'horizontale -- les cartes apparaissaient
        # coupees a gauche. Il rejoint la ligne du contact, qui passe a
        # la ligne si besoin.
        v.addLayout(haut)

        d = QLabel(str(annonce.get("description") or ""))
        d.setWordWrap(True)
        d.setTextFormat(Qt.PlainText)   # texte d'un autre joueur : jamais
        # interprete comme du HTML (liens, images, mise en page forcee).
        d.setStyleSheet(f"color:{_TXT};font-size:9pt;")
        v.addWidget(d)

        meta = QLabel(f"{contact_affiche}  ·  "
                      f"{A.age_texte(annonce.get('cree_le'))}")
        meta.setTextFormat(Qt.PlainText)
        meta.setWordWrap(True)
        meta.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        v.addWidget(meta)

        if bandeau:
            bl = QLabel(bandeau)
            bl.setWordWrap(True)
            bl.setStyleSheet(
                f"color:{bandeau_couleur or _ROUGE};font-size:8pt;"
                f"font-weight:600;")
            v.addWidget(bl)

        self.boutons = []
        if actions:
            barre = QHBoxLayout()
            barre.setSpacing(6)
            barre.addStretch(1)
            for libelle, fonction, danger in actions:
                bt = _bouton(libelle, danger=danger)
                bt.clicked.connect(lambda _=False, f=fonction: f())
                self.boutons.append((bt, fonction))
                barre.addWidget(bt)
            v.addLayout(barre)


class _BoutonChoix(QPushButton):
    """Bouton de categorie du formulaire : bleu plein si choisi."""

    def __init__(self, categorie, parent=None):
        super().__init__(A.libelle_categorie(categorie), parent)
        self.categorie = categorie
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(36)
        self.peindre(False)

    def peindre(self, choisi):
        if choisi:
            style = (f"background:{_ACCENT};color:#ffffff;"
                     f"border:1px solid {_ACCENT};font-weight:700;")
        else:
            style = f"background:{_BG};color:{_TXT};border:1px solid {_SEP};"
        self.setStyleSheet(
            f"QPushButton{{{style}border-radius:10px;"
            f"font-size:9pt;padding:6px;}}")


# ---------------------------------------------------------------------
#  L'application
# ---------------------------------------------------------------------

class AnnoncesApp(PhoneApp):

    APP_ID   = "annonces"
    APP_NAME = "Annonces"
    APP_ICON = (_LazyPhoneIcon("annonces", "\U0001F4E2")
                if _LazyPhoneIcon is not None else "\U0001F4E2")

    ONGLET_TOUTES  = 0
    ONGLET_MIENNES = 1

    _COLONNES = 2   # largeur des grilles (categories, paires de boutons)

    def __init__(self, screen_w, screen_h, screen_radius, services,
                 parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.setStyleSheet(f"background:{_BG};")
        self._annonces: list[dict] = []
        self._pret = False
        self._erreur = ""
        self._creation = False
        # Etat du formulaire, conserve HORS des widgets : ceux-ci sont
        # detruits a chaque _rafraichir().
        self._brouillon = ""
        self._sel_cat = A.CATEGORIES[0]
        self._avec_numero = True
        # Action envoyee dont on attend la reponse ("publier", "retirer"),
        # "" sinon. Sert a reconnaitre la reponse a UNE publication --
        # et donc a savoir quand refermer le formulaire.
        self._attente = ""
        # Un etat pousse par un autre joueur pendant la saisie n'est pas
        # dessine tout de suite ; ce drapeau dit qu'il faudra le faire.
        self._a_redessiner = False
        self._confirm_retrait = ""
        # -1 = barre d'onglets, 0..n-1 = cibles de la page.
        self._cible = -1
        # [(widget, style_base, action, champ, grille, a_montrer)]
        self._nav = []
        self._dans_champ_ = False
        self._ed_desc = None

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)
        v.addWidget(_titre("Annonces"))
        self._onglets = _Onglets(["Annonces", "Mes annonces"],
                                 self._changer_onglet)
        v.addWidget(self._onglets)

        self._zone = QScrollArea()
        self._zone.setWidgetResizable(True)
        self._zone.setFrameShape(QFrame.NoFrame)
        self._zone.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self._zone, stretch=1)

        self._rafraichir()

    # -- services --

    def _nom_ou_numero(self, numero):
        """Nom du contact si on l'a enregistre, numero sinon -- via le
        point d'entree UNIQUE du repertoire local."""
        num = str(numero or "")
        rep = getattr(self.services, "repertoire", None)
        if rep is None or not num:
            return num
        try:
            return rep.afficher(num)
        except Exception:
            return num

    def _connu(self, numero):
        rep = getattr(self.services, "repertoire", None)
        try:
            return bool(rep and rep.contient(str(numero or "")))
        except Exception:
            return False

    def _appeler(self, numero):
        fn = getattr(self.services, "appeler", None)
        if callable(fn) and numero:
            try:
                fn(str(numero))
            except Exception:
                pass

    def _ajouter_contact(self, numero):
        fn = getattr(self.services, "ouvrir_ajout_contact", None)
        if callable(fn) and numero:
            try:
                fn(str(numero))
            except Exception:
                pass

    # -- cycle de vie --

    def on_show(self):
        # Une confirmation ou un formulaire laisses ouverts ne survivent
        # pas a la fermeture : les retrouver sans s'en souvenir invite a
        # valider par reflexe. Le BROUILLON, lui, est garde -- un appel
        # entrant ne doit pas faire perdre une annonce a moitie ecrite.
        self._creation = False
        self._confirm_retrait = ""
        self._attente = ""
        self._erreur = ""
        self._dans_champ_ = False
        self._rafraichir()
        _envoyer({"type": "annonces_liste"})

    def on_hide(self):
        self._memoriser_brouillon()
        self._dans_champ_ = False

    # -- reception --

    def handle_server_msg(self, data):
        """Trames annonces_* routees par le client vers l'app courante."""
        try:
            if (data or {}).get("type") == "annonces_etat":
                self.appliquer_etat(data)
        except Exception as e:
            _journal(f"etat KO : {e!r}")

    def appliquer_etat(self, data):
        d = data or {}
        self._annonces = list(d.get("annonces") or [])
        self._pret = True
        reponse = bool(d.get("reponse", True))
        if not reponse:
            # Poussee causee par un autre joueur. Pendant la saisie, on
            # memorise sans toucher a l'ecran : reconstruire le
            # formulaire ferait perdre le focus du champ en pleine
            # frappe.
            if self._creation or self._dans_champ_:
                self._a_redessiner = True
                return
            self._rafraichir()
            return

        err = str(d.get("erreur") or "")
        attente, self._attente = self._attente, ""
        self._erreur = err
        if not err and attente == "publier":
            # Publication acceptee : formulaire referme, brouillon vide.
            # Le champ est oublie AVANT : _rafraichir() recopie sinon son
            # texte dans le brouillon qu'on vient de vider.
            self._creation = False
            self._ed_desc = None
            self._brouillon = ""
            self._avec_numero = True
            self._dans_champ_ = False
            self._cible = 0
        self._rafraichir()

    # -- actions --

    def _demander(self, type_, attente="", **champs):
        # Pose AVANT l'envoi : la reponse arrive par un signal Qt mis en
        # file, donc apres cette methode -- mais rien ne coute de ne pas
        # dependre de cet ordre.
        self._attente = attente
        if not _envoyer({"type": type_, **champs}):
            self._attente = ""
            self._erreur = ("Serveur injoignable : reconnectez-vous pour "
                            "publier ou retirer une annonce.")
            self._rafraichir()

    def _publier(self):
        self._memoriser_brouillon()
        try:
            A.valide_categorie(self._sel_cat)
            texte = A.valide_description(self._brouillon)
        except A.AnnonceError as e:
            self._erreur = str(e)
            self._rafraichir()
            return
        self._demander("annonces_publier", attente="publier",
                       categorie=self._sel_cat, description=texte,
                       avec_numero=self._avec_numero)

    def _demander_retrait(self, a):
        self._confirm_retrait = str(a.get("id") or "")
        self._rafraichir()

    def _annuler_retrait(self):
        self._confirm_retrait = ""
        self._rafraichir()

    def _confirmer_retrait(self, a):
        # Rien n'est retire localement : l'annonce disparait quand le
        # serveur a confirme.
        self._confirm_retrait = ""
        self._demander("annonces_retirer", attente="retirer",
                       id=a.get("id"))

    def _ouvrir_formulaire(self):
        self._creation = True
        self._erreur = ""
        self._cible = 0
        self._rafraichir()

    def _fermer_formulaire(self):
        """Annuler (bouton ou Retour) : referme ET JETTE la saisie.

        [21/09/2026] La premiere version gardait le brouillon, et
        « Nouvelle annonce » rouvrait l'annonce abandonnee. Constate au
        premier test : Annuler veut dire annuler. Le formulaire repart
        vierge, categorie et case numero comprises.

        Le brouillon ne survit plus qu'aux sorties NON voulues : appel
        entrant, fermeture du telephone, changement d'onglet. Le bouton
        devient alors « Reprendre mon annonce ».

        Le champ est oublie AVANT de vider le brouillon : _rafraichir()
        recopierait sinon son texte dans le brouillon qu'on vient de
        vider.
        """
        self._ed_desc = None
        self._brouillon = ""
        self._sel_cat = A.CATEGORIES[0]
        self._avec_numero = True
        self._creation = False
        self._erreur = ""
        self._dans_champ_ = False
        self._cible = 0
        self._rafraichir()

    def _choisir_cat(self, cat):
        self._sel_cat = cat
        for b in getattr(self, "_btns_cat", []):
            b.peindre(b.categorie == cat)
        # peindre() a reecrit les styles : on resynchronise les bases du
        # halo SANS reconstruire la page (ce qui recreerait le champ).
        self._nav = [(w, w.styleSheet() if w in self._btns_cat else base,
                      a, c, g, m) for (w, base, a, c, g, m) in self._nav]
        self._peindre_cible()

    def _basculer_numero(self):
        self._avec_numero = not self._avec_numero
        self._peindre_case_numero()
        # Meme resynchronisation que _choisir_cat.
        b = self._btn_numero
        self._nav = [(w, w.styleSheet() if w is b else base, a, c, g, m)
                     for (w, base, a, c, g, m) in self._nav]
        self._peindre_cible()

    def _memoriser_brouillon(self):
        w = self._ed_desc
        if w is None:
            return
        try:
            self._brouillon = w.toPlainText()
        except RuntimeError:
            # Widget deja detruit par Qt : on garde le dernier connu.
            pass

    # -- navigation --

    def _changer_onglet(self, _i):
        self._memoriser_brouillon()
        self._creation = False
        self._confirm_retrait = ""
        self._erreur = ""
        self._cible = -1
        self._rafraichir()

    def _nav_add(self, widget, action=None, champ=None, grille=None,
                 a_montrer=None):
        """Inscrit une cible du D-pad, dans l'ordre de l'ecran.

        `a_montrer` : widget a garder visible quand la cible est un
        bouton DANS une carte -- sinon le defilement s'arrete au bouton
        et le texte de l'annonce reste hors de l'ecran.
        """
        self._nav.append((widget, widget.styleSheet(), action, champ,
                          grille, a_montrer))

    def _peindre_cible(self):
        for i, (w, base, _a, _c, _g, _m) in enumerate(self._nav):
            try:
                _halo(w, base, i == self._cible)
            except Exception:
                pass
        try:
            self._onglets.set_nav_highlight(self._cible == -1)
        except Exception:
            pass
        self._voir_cible()

    def _voir_cible(self):
        if not (0 <= self._cible < len(self._nav)):
            return
        w, _b, _a, _c, _g, m = self._nav[self._cible]
        try:
            if m is not None:
                self._zone.ensureWidgetVisible(m, 0, 10)
            self._zone.ensureWidgetVisible(w, 0, 40)
        except Exception:
            pass

    def _grille_de(self, i):
        if 0 <= i < len(self._nav):
            return self._nav[i][4]
        return None

    def _bornes_grille(self, i):
        g = self._grille_de(i)
        deb = fin = i
        while deb - 1 >= 0 and self._grille_de(deb - 1) == g:
            deb -= 1
        while fin + 1 < len(self._nav) and self._grille_de(fin + 1) == g:
            fin += 1
        return deb, fin

    def _descendre(self):
        i = self._cible
        g = self._grille_de(i)
        if g is not None:
            _deb, fin = self._bornes_grille(i)
            j = i + self._COLONNES
            self._cible = j if j <= fin else min(len(self._nav) - 1, fin + 1)
        else:
            self._cible = min(len(self._nav) - 1, i + 1)
        self._peindre_cible()

    def _monter(self):
        i = self._cible
        g = self._grille_de(i)
        if g is not None:
            deb, _fin = self._bornes_grille(i)
            j = i - self._COLONNES
            if j >= deb:
                self._cible = j
            else:
                # Sortie par le haut : on atterrit sur la PREMIERE cible
                # de la rangee du dessus (ou sur la grille precedente),
                # pas sur la derniere -- sinon, depuis une paire de
                # boutons, Haut sautait sur le bouton de droite de la
                # carte precedente.
                k = deb - 1
                if k >= 0 and self._grille_de(k) is not None:
                    d2, f2 = self._bornes_grille(k)
                    k = d2 + ((f2 - d2) // self._COLONNES) * self._COLONNES
                self._cible = max(-1, k)
        else:
            k = i - 1
            if k >= 0 and self._grille_de(k) is not None:
                d2, f2 = self._bornes_grille(k)
                k = d2 + ((f2 - d2) // self._COLONNES) * self._COLONNES
            self._cible = max(-1, k)
        self._peindre_cible()

    def _lateral(self, pas):
        i = self._cible
        deb, fin = self._bornes_grille(i)
        rang_deb = deb + ((i - deb) // self._COLONNES) * self._COLONNES
        rang_fin = min(fin, rang_deb + self._COLONNES - 1)
        j = i + pas
        if rang_deb <= j <= rang_fin:
            self._cible = j
            self._peindre_cible()

    def handle_nav(self, direction):
        if self._dans_champ_:
            if direction == "enter":
                self._sortir_du_champ()
                self._descendre()
            return True
        if direction == "up":
            self._monter()
            return True
        if direction == "down":
            if self._cible < 0 and not self._nav:
                return True
            self._descendre()
            return True
        if direction in ("left", "right"):
            pas = 1 if direction == "right" else -1
            if self._cible < 0:
                cur = self._onglets.courant()
                self._onglets.selectionner(
                    (cur + pas) % len(self._onglets._btns))
            elif self._grille_de(self._cible) is not None:
                self._lateral(pas)
            return True
        if direction == "enter":
            if self._cible < 0:
                self._descendre()
                return True
            if 0 <= self._cible < len(self._nav):
                _w, _b, action, champ, _g, _m = self._nav[self._cible]
                if champ is not None:
                    self._entrer_dans_champ()
                elif action is not None:
                    action()
            return True
        return False

    def handle_back(self):
        """Retour : champ, puis confirmation, puis formulaire, puis l'app.

        Le test `_dans_champ_` vient EN PREMIER : l'overlay route "esc"
        vers handle_back() et non vers handle_nav() (CIRCUSVOIP_PROJET.md
        5 ter). Sans lui, Retour arriere sur un champ vide refermerait le
        formulaire.
        """
        if self._dans_champ_:
            self._sortir_du_champ()
            self._peindre_cible()
            return True
        if self._confirm_retrait:
            self._annuler_retrait()
            return True
        if self._creation:
            self._fermer_formulaire()
            return True
        return False

    # -- contrat clavier (CIRCUSVOIP_PROJET.md 5 ter) --

    def dans_champ(self) -> bool:
        return self._dans_champ_

    def champ_courant_vide(self) -> bool:
        w = self._widget_champ()
        if w is None:
            return True
        try:
            return not w.toPlainText().strip()
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
        self._memoriser_brouillon()
        try:
            w = self._widget_champ()
            if w is not None:
                w.clearFocus()
            self.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    def eventFilter(self, obj, ev):
        """Suit le focus REEL du champ (clic souris compris).

        Sans ca, un clic dans le champ laisse `_dans_champ_` a faux, le
        filtre win32 supprime Retour arriere et le renvoie en "esc" --
        qui fermerait le formulaire. Cf. Travail, correctif du 01/09.
        """
        try:
            t = ev.type()
            if t in (QEvent.FocusIn, QEvent.FocusOut):
                index = -1
                for i, entree in enumerate(self._nav):
                    if entree[3] is obj:
                        index = i
                        break
                if index >= 0:
                    if t == QEvent.FocusIn:
                        self._dans_champ_ = True
                        self._cible = index
                    else:
                        # Pas de redessin ICI meme si une poussee attend :
                        # on est dans le filtre d'evenements du champ, et
                        # _rafraichir() le detruirait sous nos pieds. La
                        # poussee en attente est dessinee a la sortie du
                        # formulaire, qui redessine de toute facon.
                        self._dans_champ_ = False
                        self._memoriser_brouillon()
                    self._peindre_cible()
        except Exception:
            pass
        return super().eventFilter(obj, ev)

    # -- rendu --

    def _rafraichir(self):
        # Le texte tape est recopie AVANT que setWidget() ne detruise le
        # champ : c'est ce qui empeche une erreur de publication (ou tout
        # autre redessin) d'effacer la saisie.
        self._memoriser_brouillon()
        self._ed_desc = None
        self._note_numero = None
        self._a_redessiner = False
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

        if self._onglets.courant() == self.ONGLET_TOUTES:
            self._peindre_toutes(v)
        elif self._creation:
            self._peindre_formulaire(v)
        else:
            self._peindre_miennes(v)

        v.addStretch(1)
        self._zone.setWidget(page)
        if self._cible >= len(self._nav):
            self._cible = len(self._nav) - 1 if self._nav else -1
        self._peindre_cible()

    def _contact_texte(self, a):
        num = a.get("contact")
        if a.get("mien"):
            return ("Votre annonce  ·  numéro affiché" if num
                    else "Votre annonce  ·  sans numéro")
        if num:
            return f"Contact : {self._nom_ou_numero(num)}"
        return "Sans contact"

    def _inscrire_carte(self, carte, annonce, grille_id, action_seule=None):
        """Inscrit une carte au parcours : ses boutons s'il y en a (en
        rangee, gauche/droite pour passer de l'un a l'autre), la carte
        elle-meme sinon -- pour qu'on puisse la LIRE en descendant."""
        if carte.boutons:
            for b, f in carte.boutons:
                self._nav_add(b, f, grille=grille_id, a_montrer=carte)
        else:
            self._nav_add(carte, action_seule)

    def _peindre_toutes(self, v):
        if not self._pret:
            # "Aucune annonce" et "pas encore recu" sont deux choses
            # differentes : les confondre ferait croire a un tableau vide.
            v.addWidget(_vide("Chargement…"))
            return
        if not self._annonces:
            v.addWidget(_vide(
                "Aucune annonce pour le moment.\n"
                "Publiez la vôtre depuis « Mes annonces »."))
            return
        peut_appeler = callable(getattr(self.services, "appeler", None))
        for a in self._annonces:
            actions = []
            num = a.get("contact")
            if num and not a.get("mien") and peut_appeler:
                actions.append(("Appeler",
                                (lambda n=num: self._appeler(n)), False))
                if not self._connu(num):
                    actions.append(
                        ("Ajouter",
                         (lambda n=num: self._ajouter_contact(n)), False))
            carte = _Carte(a, self._contact_texte(a), actions)
            v.addWidget(carte)
            self._inscrire_carte(carte, a, f"a:{a.get('id')}")

    def _peindre_miennes(self, v):
        b = _bouton("+  Nouvelle annonce", plein=True)
        b.setMinimumHeight(36)
        if self._brouillon.strip():
            b.setText("+  Reprendre mon annonce")
        b.clicked.connect(self._ouvrir_formulaire)
        v.addWidget(b)
        self._nav_add(b, self._ouvrir_formulaire)

        miennes = [a for a in self._annonces if a.get("mien")]
        info = QLabel(f"{len(miennes)} / {A.ANNONCES_PAR_AUTEUR_MAX} "
                      f"annonces  ·  durée de vie 7 jours")
        info.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        v.addWidget(info)

        if not self._pret:
            v.addWidget(_vide("Chargement…"))
            return
        if not miennes:
            v.addWidget(_vide("Vous n'avez publié aucune annonce."))
            return
        for a in miennes:
            aid = str(a.get("id") or "")
            if aid == self._confirm_retrait:
                # Annuler EN PREMIER : une Entree tapee trop vite sur une
                # action irreversible ne doit pas la declencher.
                actions = [
                    ("Annuler", self._annuler_retrait, False),
                    ("Confirmer le retrait",
                     (lambda aa=a: self._confirmer_retrait(aa)), True),
                ]
                bandeau, coul = "Retirer cette annonce ?", _ROUGE
            else:
                actions = [("Retirer",
                            (lambda aa=a: self._demander_retrait(aa)), True)]
                bandeau = A.echeance_texte(a.get("cree_le")) or None
                coul = _ROUGE
            carte = _Carte(a, self._contact_texte(a), actions,
                           bandeau=bandeau, bandeau_couleur=coul)
            v.addWidget(carte)
            self._inscrire_carte(carte, a, f"m:{aid}")

    def _peindre_formulaire(self, v):
        lbl = QLabel("Catégorie")
        lbl.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        v.addWidget(lbl)
        self._btns_cat = []
        cats = list(A.CATEGORIES)
        for i in range(0, len(cats), self._COLONNES):
            h = QHBoxLayout()
            h.setSpacing(6)
            rangee = cats[i:i + self._COLONNES]
            for cat in rangee:
                b = _BoutonChoix(cat)
                b.peindre(cat == self._sel_cat)
                b.clicked.connect(lambda _=False, c=cat: self._choisir_cat(c))
                h.addWidget(b, stretch=1)
                self._btns_cat.append(b)
                self._nav_add(b, (lambda c=cat: self._choisir_cat(c)),
                              grille="cat")
            if len(rangee) < self._COLONNES:
                # Garde la largeur de colonne sur la derniere rangee :
                # sans ce vide, « Vente / Achat » s'etirerait sur toute
                # la ligne et ne ressemblerait plus a un choix parmi trois.
                h.addStretch(1)
            v.addLayout(h)

        self._ed_desc = QTextEdit()
        self._ed_desc.setPlaceholderText(
            "Votre annonce : qui vous cherchez, ce que vous proposez, "
            "où et quand…")
        self._ed_desc.setAcceptRichText(False)
        self._ed_desc.setFixedHeight(110)
        self._ed_desc.setObjectName("champ")
        self._ed_desc.setStyleSheet(_STYLE_CHAMP)
        if self._brouillon:
            self._ed_desc.setPlainText(self._brouillon)
        v.addWidget(self._ed_desc)
        self._nav_add(self._ed_desc, champ=self._ed_desc)
        self._ed_desc.installEventFilter(self)

        self._compteur = QLabel()
        self._compteur.setAlignment(Qt.AlignRight)
        self._ed_desc.textChanged.connect(self._maj_compteur)
        v.addWidget(self._compteur)
        self._maj_compteur()

        self._btn_numero = QPushButton()
        self._btn_numero.setCursor(Qt.PointingHandCursor)
        self._btn_numero.setMinimumHeight(36)
        self._peindre_case_numero()
        self._btn_numero.clicked.connect(self._basculer_numero)
        v.addWidget(self._btn_numero)
        self._nav_add(self._btn_numero, self._basculer_numero)

        note = QLabel("Votre numéro sera visible par tous les joueurs du "
                      "serveur." if self._avec_numero else
                      "Personne ne pourra vous contacter depuis l'annonce.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{_MUTED};font-size:8pt;")
        self._note_numero = note
        v.addWidget(note)

        h = QHBoxLayout()
        h.setSpacing(6)
        annuler = QPushButton("Annuler")
        annuler.setCursor(Qt.PointingHandCursor)
        annuler.setStyleSheet(
            f"QPushButton{{border:1px solid {_SEP};color:{_MUTED};"
            f"background:transparent;border-radius:10px;padding:8px;"
            f"font-size:9pt;}}")
        annuler.clicked.connect(self._fermer_formulaire)
        h.addWidget(annuler, stretch=1)
        self._nav_add(annuler, self._fermer_formulaire, grille="actions")
        publier = _bouton("Publier", plein=True)
        publier.clicked.connect(self._publier)
        h.addWidget(publier, stretch=1)
        self._nav_add(publier, self._publier, grille="actions")
        v.addLayout(h)

    def _maj_compteur(self):
        try:
            n = len(self._ed_desc.toPlainText().strip())
        except Exception:
            return
        trop = n > A.DESCRIPTION_MAX_LEN
        self._compteur.setText(f"{n} / {A.DESCRIPTION_MAX_LEN}")
        self._compteur.setStyleSheet(
            f"color:{_ROUGE if trop else _MUTED};font-size:8pt;")

    def _peindre_case_numero(self):
        on = self._avec_numero
        # Case dessinee au caractere, comme les notifications de Travail :
        # une cible de 36 px se vise sans souris fine.
        self._btn_numero.setText(
            ("☑  " if on else "☐  ")
            + "Inclure mon numéro de téléphone")
        self._btn_numero.setStyleSheet(
            f"QPushButton{{text-align:left;padding:8px 10px;"
            f"border:1px solid {_VERT if on else _SEP};"
            f"color:{_TXT if on else _MUTED};"
            f"background:{'rgba(63,185,80,0.10)' if on else _BG};"
            f"border-radius:10px;font-size:9pt;}}")
        note = getattr(self, "_note_numero", None)
        if note is not None:
            try:
                note.setText(
                    "Votre numéro sera visible par tous les joueurs du "
                    "serveur." if on else
                    "Personne ne pourra vous contacter depuis l'annonce.")
            except RuntimeError:
                pass
