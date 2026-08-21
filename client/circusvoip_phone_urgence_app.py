#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[URGENCE 15/08/2026] App « Urgence » du CircusPhone.

Trois onglets, dont deux n'existent que pour les detenteurs d'un role :

    Urgence    declencher une demande, et la suivre (tout le monde)
    Demandes   liste des signaux, puis suivi de celui qu'on a pris
    Service    prise de service, collegues, et equipe pour un chef

Un joueur ordinaire n'en voit qu'un : la barre d'onglets n'apparait
qu'avec un role, une barre a un seul onglet etant du decor qui coute de
la place.

Les regles vivent dans circusvoip_phone_urgence.py ; ce fichier ne fait
que les montrer.

--- L'etat vient du serveur, toujours ---

L'app n'ecrit rien de sa propre autorite. Elle envoie une intention --
urgence_creer, urgence_prendre -- et redessine a l'arrivee de
urgence_etat.

Deux raisons. D'abord, deux secouristes qui prennent la meme demande ne
doivent pas voir chacun un ecran lui donnant raison : seul le serveur
tranche. Ensuite, l'etat change SANS action locale -- la victime
abandonne, le signal expire, un collegue pointe -- et un ecran pilote
par les clics resterait sur une situation qui n'existe plus.

--- Deux exigences pour la distance ---

La LISTE autorise le repli sur les coordonnees systeme malgre la
presence d'un astre, mais n'affiche que des tranches : sur place,
proche, loin, autre systeme. Elles derivent avec l'orbite, donc elles
sont fausses -- mais pour choisir une demande, savoir si c'est loin
suffit.

Le SUIVI garde la regle stricte : un nombre seulement dans un
referentiel commun. Un chiffre credible mais faux enverrait le
secouriste ailleurs.

--- La capture ne bloque pas l'interface ---

Elle prend 4 a 9 secondes (deux passes EasyOCR par essai). Dans le
thread UI, le telephone se figerait -- et on ne distinguerait pas un
OCR lent d'un plantage. Elle tourne dans un QThread PARENTE : sans
parent Qt, l'objet C++ peut etre detruit pendant que le thread tourne,
ce qui abandonne le processus sans aucune exception Python.

--- Ce qui reste a faire ---

Rien de temporaire dans ce fichier. Les chefs se designent depuis la
console d'admin, dont les commandes n'existent pas encore : tant qu'il
n'y a pas de premier chef, personne ne peut distribuer de role, donc
personne n'est en service, donc toute demande est refusee.
"""

from __future__ import annotations

import os
import time
import traceback

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from circusvoip_phone_apps import PhoneApp
import circusvoip_phone_urgence as U

# Palette : reprise a l'identique de l'app Travail. Redefinir une teinte
# ici ferait deriver les ecrans a la premiere retouche.
_BG     = "#ffffff"
_TXT    = "#1a1a1a"
_MUTED  = "#9aa0a6"
_ACCENT = "#2f6fed"
_VERT   = "#3fb950"
_ROUGE  = "#e5484d"
_ORANGE = "#d98324"
_SEP    = "#e6e8eb"
_GRISE  = "#f1f3f4"
_HALO   = "#000000"


def _titre(txt):
    lbl = QLabel(txt)
    lbl.setStyleSheet(
        f"color:{_TXT};font-size:15px;font-weight:600;padding:2px 2px 4px;")
    return lbl


def _petit(txt, couleur=_MUTED):
    lbl = QLabel(txt)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{couleur};font-size:10px;")
    return lbl


# ---------------------------------------------------------------------
#  Zone de capture
# ---------------------------------------------------------------------

def _zone_ocr():
    """Zone d'UNE ligne, resolue comme _init_zone_ocr() du client.

    L'ordre compte, et il a coute une matinee le 12/08 : une zone
    calibree a la main n'a aucune raison de coincider avec
    l'heuristique. Chez l'utilisateur de reference, auto_ocr_zone()
    renvoyait une bande sur le moniteur principal alors que SC tournait
    sur le second -- rien n'etait lisible, et la conclusion tiree de
    cette lecture vide etait fausse.
    """
    try:
        import circusvoip_core as core
        z = (core._load_client_cfg() or {}).get("zone_coords")
        if isinstance(z, dict) and "left" in z and "width" in z:
            z = dict(z)
            if "gamma" not in z:
                import circusvoip_sc_ocr as ocr
                z["gamma"] = ocr.auto_ocr_zone().get("gamma", 0.5)
            return z, "config"
    except Exception:
        pass
    try:
        import circusvoip_sc_ocr as ocr
        return ocr.auto_ocr_zone(), "auto"
    except Exception:
        return None, "aucune"


# ---------------------------------------------------------------------
#  Journal des declenchements
# ---------------------------------------------------------------------
#
# Chaque declenchement laisse deux fichiers horodates : l'IMAGE de la
# zone capturee et un COMPTE RENDU texte.
#
# L'image est la piece decisive. Quand une lecture est refusee ou qu'un
# signe parait faux, le journal OCR seul ne permet pas de trancher entre
# "l'ecran affichait ca" et "l'OCR s'est trompe" -- il faut avoir vu les
# pixels. Or ils ne sont pas reproductibles : le jeu a bouge, la planete
# a tourne, la session a change. Sans capture immediate, la question
# reste ouverte pour toujours.
#
# C'est exactement ce qui a coute deux diagnostics d'une demi-journee le
# 06/08 sur le micro : l'information existait, personne ne l'avait
# gardee.

def _dossier_logs():
    """Dossier des journaux d'urgence, cree au besoin.

    A cote du client plutot que dans %TEMP% : ces fichiers sont faits
    pour etre relus et envoyes, pas pour etre nettoyes par le systeme.
    """
    base = None
    try:
        import circusvoip_core as core
        base = str(getattr(core, "_BASE_DIR", "") or "")
    except Exception:
        base = ""
    if not base:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "logs_urgence")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return None
    return d


def _horodatage():
    return time.strftime("%Y-%m-%d_%H-%M-%S")


# ---------------------------------------------------------------------
#  Capture hors du thread UI
# ---------------------------------------------------------------------

class _Captureur(QThread):
    """Lit la hierarchie et rend le resultat au thread UI.

    Le journal OCR est capte pendant la lecture : quand une capture est
    refusee, c'est lui qui dit sur quoi les deux lectures ont diverge.
    Sans lui, un refus serait indiscernable d'une panne.
    """

    fini = Signal(object, str, str, bool)
    # (hierarchie, journal, chemin image, lecture simple ?)

    def __init__(self, zone, lignes=8, essais=3, dossier=None, horo=None,
                 double_lecture=True, parent=None):
        # [15/08/2026] PARENT Qt obligatoire.
        #
        # Sans parent, le QThread n'appartient a personne cote C++ : si
        # l'app le lache -- reassignation de self._captureur, passage du
        # ramasse-miettes Python -- l'objet C++ est detruit alors que le
        # thread tourne encore. Qt abandonne alors le processus avec
        # "QThread: Destroyed while thread is still running", SANS
        # exception Python : le client se ferme, et rien n'apparait dans
        # le journal.
        #
        # C'est la signature exacte de la fermeture observee le 15/08 a
        # l'ouverture du telephone.
        super().__init__(parent)
        self._zone = dict(zone or {})
        self._lignes = int(lignes)
        self._essais = int(essais)
        self._double = bool(double_lecture)
        self._dossier = dossier
        self._horo = horo or _horodatage()

    def _sauver_image(self, ocr, lignes):
        """Enregistre la zone capturee, AVANT la lecture.

        Avant, et non apres : l'image doit montrer ce que l'OCR va lire,
        pas ce que l'ecran affiche une fois les essais termines -- entre
        les deux, jusqu'a 20 secondes ont pu passer et le jeu a bouge.
        """
        if not self._dossier:
            return ""
        try:
            import cv2 as _cv2
            haute = dict(self._zone)
            haute["height"] = int(self._zone.get("height", 29)) * self._lignes
            img = ocr.capture_region(haute)
            chemin = os.path.join(self._dossier, f"urgence_{self._horo}.png")
            _cv2.imwrite(chemin, img)
            lignes.append(f"[URGENCE] image de la zone : {chemin}")
            return chemin
        except Exception as e:
            lignes.append(f"[URGENCE] capture image KO : {e!r}")
            return ""

    def run(self):
        lignes = []
        hier = None
        chemin_img = ""
        simple = not self._double
        t0 = time.time()
        try:
            import circusvoip_sc_ocr as ocr
        except Exception:
            self.fini.emit(None, "Module OCR indisponible.", "", False)
            return
        ancien = getattr(ocr, "_logger", None)
        try:
            ocr.set_logger(lambda m: lignes.append(str(m)))
        except Exception:
            pass
        try:
            chemin_img = self._sauver_image(ocr, lignes)
            hier = ocr.capture_hierarchy(self._zone, lines=self._lignes,
                                         attempts=self._essais,
                                         double_lecture=self._double)
            if hier is None and self._double:
                # [13/08/2026] Repli sur une lecture unique.
                #
                # La double lecture suppose un joueur immobile : deux
                # captures a 5 m pres. Or un blesse bouge encore -- il
                # fuit, il rampe, il pilote. En vol, 233 m separent deux
                # lectures espacees de 3 s : le declenchement echouait a
                # tous les coups, au moment ou il compte le plus.
                #
                # Un signal approximatif vaut mieux que pas de signal :
                # sans lui, personne ne vient. La position est marquee
                # comme relevee en deplacement, et l'ecran le dit.
                lignes.append("[URGENCE] double lecture refusee "
                              "(joueur en mouvement ?) - repli sur une "
                              "lecture unique")
                hier = ocr.capture_hierarchy(self._zone, lines=self._lignes,
                                             attempts=self._essais,
                                             double_lecture=False)
                simple = hier is not None
        except Exception:
            lignes.append(traceback.format_exc())
        finally:
            try:
                if ancien is not None:
                    ocr.set_logger(ancien)
            except Exception:
                pass
        lignes.append(f"[URGENCE] lecture terminee en "
                      f"{time.time() - t0:.1f} s")
        self.fini.emit(hier, "\n".join(lignes), chemin_img, simple)


# ---------------------------------------------------------------------
#  Champ de description
# ---------------------------------------------------------------------

class _ChampNumero(QLineEdit):
    """Champ d'une ligne dont Entree est AVALEE.

    [18/08/2026] Meme raison que _ChampDescription : une fois le focus
    clavier obtenu, les frappes vont directement au widget. Entree y
    declencherait returnPressed et, surtout, l'overlay route la meme
    touche vers handle_nav() -- qui sort du champ. Sans cette classe, le
    champ numero n'avait AUCUN traitement, et le retour arriere sortait
    de la saisie au lieu d'effacer : impossible de corriger une faute de
    frappe.

    Le retour arriere n'est pas traite ici non plus : l'overlay le route
    vers handle_back(), qui ne sort que si le champ est VIDE. Le traiter
    des deux cotes ferait sortir a la premiere correction.
    """

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            return
        super().keyPressEvent(ev)


class _ChampDescription(QPlainTextEdit):
    """Champ multi-lignes dont Entree SORT au lieu d'aller a la ligne.

    [13/08/2026] Une fois le focus clavier obtenu, les frappes vont
    directement au widget : l'overlay ne les voit plus passer. Entree
    inserait donc un retour a la ligne, et il devenait impossible de
    sortir du champ autrement qu'a la souris -- que l'overlay ne route
    pas forcement.

    Un retour a la ligne n'a de toute facon pas de sens ici : 120
    caracteres, lus d'un coup d'oeil par un secouriste.
    """

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            # AVALEE, sans rien emettre : l'overlay route la meme touche
            # vers handle_nav(), et c'est LUI qui sort du champ. Sortir
            # ici aussi faisait sortir puis rentrer aussitot -- la cible
            # etant restee sur le champ, on n'en sortait jamais.
            #
            # On l'avale quand meme, sinon un retour a la ligne serait
            # insere au passage. Il n'a pas de sens ici : 120 caracteres,
            # lus d'un coup d'oeil par un secouriste.
            return
        # Echap et retour arriere ne sont pas traites ici non plus :
        # l'overlay les route vers handle_back(), qui ne sort que si le
        # champ est vide. Les traiter des deux cotes faisait sortir a la
        # premiere correction de frappe.
        super().keyPressEvent(ev)


# ---------------------------------------------------------------------
#  Bouton de declenchement
# ---------------------------------------------------------------------

class _BoutonUrgence(QPushButton):
    """Gros bouton plein. La taille EST la fonction : on le cherche du
    regard dans une situation ou on n'a pas le temps de lire."""

    def __init__(self, libelle, couleur, parent=None):
        super().__init__(libelle, parent)
        self._couleur = couleur
        # 64 px : on doit pouvoir viser sans regarder. C'est la seule
        # commande de l'app qui soit urgente au sens propre.
        self.setMinimumHeight(64)
        self.setCursor(Qt.PointingHandCursor)
        self.style_base = (
            f"QPushButton{{background:{couleur};color:#ffffff;"
            f"border:none;border-radius:14px;font-size:15px;"
            f"font-weight:600;}}"
            f"QPushButton:disabled{{background:{_GRISE};color:{_MUTED};}}")
        self.setStyleSheet(self.style_base)


# ---------------------------------------------------------------------
#  Etat serveur
# ---------------------------------------------------------------------

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


class _EtatUrgence:
    """Miroir LOCAL de ce que le serveur vient d'envoyer.

    [URGENCE 15/08/2026] L'app ne decide RIEN. Elle envoie une intention
    -- urgence_creer, urgence_prendre -- et redessine a l'arrivee de
    urgence_etat.

    C'est ce qui garantit qu'un secouriste ne voie pas un ecran lui
    donnant raison pendant qu'un autre voit le contraire : seul le
    serveur tranche. Et c'est ce qui permet a l'ecran de refleter des
    changements que personne n'a demandes ici -- la victime abandonne, le
    signal expire, un collegue prend son service.
    """

    def __init__(self):
        self.pret = False
        self.role = None
        self.chef = False
        self.en_service = False
        self.ma_demande = None
        self.signaux = []
        self.collegues = []
        self.equipe = []

    def appliquer(self, d: dict):
        d = d or {}
        self.role = d.get("role")
        self.chef = bool(d.get("chef"))
        self.en_service = bool(d.get("en_service"))
        self.ma_demande = d.get("ma_demande")
        self.signaux = list(d.get("signaux") or [])
        self.collegues = list(d.get("collegues") or [])
        self.equipe = list(d.get("equipe") or [])
        self.pret = True


# ---------------------------------------------------------------------
#  App
# ---------------------------------------------------------------------

class UrgenceApp(PhoneApp):

    APP_ID   = "urgence"
    APP_NAME = "Urgence"
    APP_ICON = "\U0001F198"   # SOS

    # Etapes de la page principale.
    _ETAPE_CHOIX    = 0
    _ETAPE_CONFIRME = 1
    _ETAPE_DEMANDE  = 2
    _ETAPE_EXPIREE  = 3

    # Onglets. Un joueur sans role ne voit que le premier : la barre
    # n'apparait qu'a partir de deux onglets utiles.
    _PAGE_DEMANDE = 0
    _PAGE_LISTE   = 1
    _PAGE_ADMIN   = 2

    def __init__(self, screen_w, screen_h, screen_radius, services,
                 parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.setStyleSheet(f"background:{_BG};")

        self._captureur = None
        self._type_en_cours = None
        self._type_confirme = None
        self._action = None          # "creer" ou "actualiser"
        self._derniere_position = None
        self._derniere_duree = None
        self._horo = None
        self._dossier_log = _dossier_logs()
        self._etat = _EtatUrgence()

        self._cible = 0
        self._nav = []
        self._dans_champ = False
        self._champ_actif = None
        self._ma_position = None
        self._a_retirer = None
        self._retour_confirmation = None
        self._pour_liste = False
        self._zone, self._origine_zone = _zone_ocr()

        self._chrono = QTimer(self)
        self._chrono.setInterval(100)
        self._chrono.timeout.connect(self._tic_capture)
        self._t0 = None

        # Bat une fois par seconde tant qu'une demande vit : c'est ce qui
        # fait apparaitre "les secours arrivent" sans que le joueur ait a
        # rouvrir l'app.
        # Mesure secouriste : coup unique replanifie APRES chaque mesure.
        # Un minuteur periodique empilerait les captures des que le delai
        # descend a 10 s et qu'une lecture prend 9.
        self._minuteur = QTimer(self)
        self._minuteur.setSingleShot(True)
        self._minuteur.timeout.connect(self._lancer_mesure)
        self._decompte = QTimer(self)
        self._decompte.setInterval(250)
        self._decompte.timeout.connect(self._tic_decompte)
        self._echeance = None
        self._derniere_distance = None
        self._derniere_mesure = None

        self._pouls = QTimer(self)
        self._pouls.setInterval(1000)
        self._pouls.timeout.connect(self._battement)

        racine = QVBoxLayout(self)
        racine.setContentsMargins(0, 0, 0, 0)
        racine.setSpacing(0)

        self._barre = QWidget()
        hb = QHBoxLayout(self._barre)
        hb.setContentsMargins(8, 6, 8, 0)
        hb.setSpacing(4)
        self._onglets = []
        for i, libelle in enumerate(("Urgence", "Demandes", "Service")):
            b = QPushButton(libelle)
            b.setCheckable(True)
            b.setMinimumHeight(26)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=i: self._aller_page(k))
            hb.addWidget(b)
            self._onglets.append(b)
        racine.addWidget(self._barre)
        self._barre.setVisible(False)

        self._pages = QStackedWidget()
        racine.addWidget(self._pages, 1)

        self._etapes = QStackedWidget()
        self._etapes.addWidget(self._etape_choix())
        self._etapes.addWidget(self._etape_confirmation())
        self._etapes.addWidget(self._etape_demande())
        self._etapes.addWidget(self._etape_expiree())
        self._pages.addWidget(self._etapes)
        self._pages.addWidget(self._page_demandes())
        self._pages.addWidget(self._page_admin())

        self._aller_etape(self._ETAPE_CHOIX)
        self._aller_page(self._PAGE_DEMANDE)
        self._rendre()

    # -----------------------------------------------------------------
    #  Page principale : trois etapes
    # -----------------------------------------------------------------

    # -- 1. choix -------------------------------------------------------

    def _etape_choix(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        entete = QHBoxLayout()
        entete.setSpacing(4)
        t = QLabel("Description")
        t.setStyleSheet(
            f"color:{_TXT};font-size:13px;font-weight:600;")
        entete.addWidget(t)
        opt = QLabel("optionnel")
        opt.setStyleSheet(f"color:{_MUTED};font-size:10px;")
        entete.addWidget(opt)
        entete.addStretch(1)
        v.addLayout(entete)

        # Champ multi-lignes sur la moitie haute. Facultatif, et c'est
        # ecrit : quelqu'un en danger appuiera d'abord et n'ecrira
        # jamais. Un champ qui aurait l'air obligatoire ferait perdre des
        # secondes exactement quand elles comptent.
        self._texte = _ChampDescription()
        self._texte.setPlaceholderText(
            f"Ce qui vous arrive, où vous êtes… "
            f"({U.TEXTE_MAX_LEN} caractères max)")
        self._texte_base = (
            f"QPlainTextEdit{{background:{_GRISE};color:{_TXT};"
            f"border:none;border-radius:10px;padding:6px;font-size:11px;}}")
        self._texte.setStyleSheet(self._texte_base)
        self._texte.textChanged.connect(self._borner_texte)
        # Hauteur FIXE, environ un tiers de l'ecran. Un champ extensible
        # repoussait les boutons contre le bas et les collait l'un a
        # l'autre : dans l'urgence, on vise sans regarder, et deux
        # boutons adjacents s'appuient l'un pour l'autre.
        self._texte.setFixedHeight(max(120, int(self._screen_h * 0.33)))
        v.addWidget(self._texte)

        self._compteur = QLabel("")
        self._compteur.setAlignment(Qt.AlignRight)
        self._compteur.setStyleSheet(f"color:{_MUTED};font-size:9px;")
        v.addWidget(self._compteur)

        # Le vide au-dessus des boutons n'est pas decoratif : il separe
        # ce qu'on remplit de ce qu'on declenche.
        v.addStretch(3)

        self._btn_med = _BoutonUrgence("Urgence médicale", _ROUGE)
        self._btn_med.clicked.connect(
            lambda: self._demander(U.TYPE_MEDICAL))
        v.addWidget(self._btn_med)

        v.addSpacing(18)

        self._btn_sec = _BoutonUrgence("Urgence sécurité", _ACCENT)
        self._btn_sec.clicked.connect(
            lambda: self._demander(U.TYPE_SECURITE))
        v.addWidget(self._btn_sec)

        v.addStretch(2)

        self._erreur = QLabel("")
        self._erreur.setWordWrap(True)
        self._erreur.setAlignment(Qt.AlignCenter)
        self._erreur.setStyleSheet(f"color:{_ROUGE};font-size:11px;")
        self._erreur.setVisible(False)
        v.addWidget(self._erreur)

        return w

    def _borner_texte(self):
        """Coupe a la longueur maximale et affiche le reste.

        QPlainTextEdit n'a pas de setMaxLength. Tronquer en silence
        ferait disparaitre des caracteres sous les doigts ; le compteur
        rend la limite visible avant qu'on la touche.
        """
        t = self._texte.toPlainText()
        if len(t) > U.TEXTE_MAX_LEN:
            curseur = self._texte.textCursor()
            pos = curseur.position()
            self._texte.blockSignals(True)
            self._texte.setPlainText(t[:U.TEXTE_MAX_LEN])
            curseur.setPosition(min(pos, U.TEXTE_MAX_LEN))
            self._texte.setTextCursor(curseur)
            self._texte.blockSignals(False)
            t = t[:U.TEXTE_MAX_LEN]
        reste = U.TEXTE_MAX_LEN - len(t)
        self._compteur.setText(f"{reste} caractères restants" if t else "")

    # -- 2. confirmation ------------------------------------------------

    def _etape_confirmation(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)
        v.addStretch(1)

        self._conf_texte = QLabel("")
        self._conf_texte.setWordWrap(True)
        self._conf_texte.setAlignment(Qt.AlignCenter)
        self._conf_texte.setStyleSheet(
            f"color:{_TXT};font-size:14px;font-weight:600;")
        v.addWidget(self._conf_texte)

        # Un seul appui, gros bouton, rien a remplir. La confirmation est
        # un dernier point de sortie contre le declenchement accidentel,
        # pas un formulaire -- elle est franchie par quelqu'un qui n'a
        # peut-etre que quelques secondes.
        self._btn_conf = _BoutonUrgence("Confirmer", _ROUGE)
        self._btn_conf.clicked.connect(self._confirmer)
        v.addWidget(self._btn_conf)

        self._btn_annul = QPushButton("Annuler")
        self._btn_annul.setMinimumHeight(36)
        self._btn_annul.style_base = (
            f"QPushButton{{background:{_GRISE};color:{_TXT};border:none;"
            f"border-radius:10px;font-size:12px;}}")
        self._btn_annul.setStyleSheet(self._btn_annul.style_base)
        self._btn_annul.clicked.connect(self._annuler_confirmation)
        v.addWidget(self._btn_annul)
        v.addStretch(1)
        return w

    # -- 3. demande en cours --------------------------------------------

    def _etape_demande(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        self._d_type = QLabel("")
        self._d_type.setStyleSheet(
            f"color:{_TXT};font-size:15px;font-weight:600;")
        v.addWidget(self._d_type)

        self._d_phrase = QLabel("")
        self._d_phrase.setWordWrap(True)
        self._d_phrase.setStyleSheet(f"color:{_TXT};font-size:11px;")
        v.addWidget(self._d_phrase)

        self._d_desc = QLabel("")
        self._d_desc.setWordWrap(True)
        self._d_desc.setStyleSheet(
            f"color:{_MUTED};font-size:11px;font-style:italic;")
        v.addWidget(self._d_desc)

        self._d_age = QLabel("")
        self._d_age.setStyleSheet(f"color:{_MUTED};font-size:10px;")
        v.addWidget(self._d_age)

        self._btn_maj = QPushButton("Actualiser ma position")
        self._btn_maj.setMinimumHeight(34)
        self._btn_maj.style_base = (
            f"QPushButton{{background:{_GRISE};color:{_TXT};border:none;"
            f"border-radius:10px;font-size:12px;}}"
            f"QPushButton:disabled{{color:{_MUTED};}}")
        self._btn_maj.setStyleSheet(self._btn_maj.style_base)
        self._btn_maj.clicked.connect(self._actualiser_position)
        v.addWidget(self._btn_maj)

        v.addStretch(1)

        # L'etat en bas, en gros. C'est la seule chose que la victime
        # regarde en boucle.
        self._d_etat = QLabel("")
        self._d_etat.setWordWrap(True)
        self._d_etat.setAlignment(Qt.AlignCenter)
        self._d_etat.setStyleSheet(
            f"color:{_ORANGE};font-size:14px;font-weight:600;padding:6px;")
        v.addWidget(self._d_etat)

        self._btn_abandon = QPushButton("Abandonner ma demande")
        self._btn_abandon.setMinimumHeight(36)
        self._btn_abandon.style_base = (
            f"QPushButton{{background:{_GRISE};color:{_ROUGE};border:none;"
            f"border-radius:10px;font-size:12px;}}")
        self._btn_abandon.setStyleSheet(self._btn_abandon.style_base)
        self._btn_abandon.clicked.connect(self._abandonner)
        v.addWidget(self._btn_abandon)
        return w

    # -- 4. expiration ---------------------------------------------------

    def _etape_expiree(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12)
        v.addStretch(1)
        lbl = QLabel("Demande expirée")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"color:{_TXT};font-size:15px;font-weight:600;")
        v.addWidget(lbl)
        self._btn_ok = QPushButton("OK")
        self._btn_ok.setMinimumHeight(40)
        self._btn_ok.style_base = (
            f"QPushButton{{background:{_ACCENT};color:#ffffff;border:none;"
            f"border-radius:12px;font-size:13px;font-weight:600;}}")
        self._btn_ok.setStyleSheet(self._btn_ok.style_base)
        self._btn_ok.clicked.connect(self._accuser_expiration)
        v.addWidget(self._btn_ok)
        v.addStretch(1)
        return w

    # -----------------------------------------------------------------
    #  Onglet Demandes  (liste, puis suivi)
    # -----------------------------------------------------------------
    #
    # [URGENCE 15/08/2026] Deux ecrans dans un seul onglet, et non deux
    # onglets : on ne suit qu'UNE demande a la fois -- on ne vole pas
    # vers deux endroits. Des qu'une demande est prise, l'onglet devient
    # l'ecran de suivi ; la liste revient quand on relache ou termine.
    #
    # Consequence voulue : plus besoin de distinguer "ma" demande dans la
    # liste, puisqu'on ne la voit plus. Le contour vert signale seulement
    # que quelqu'un d'autre s'en occupe deja -- ce qui n'interdit rien,
    # la prise est multiple.

    def _page_demandes(self):
        page = QWidget()
        self._vues_dem = QStackedWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._vues_dem)
        self._vues_dem.addWidget(self._vue_liste())
        self._vues_dem.addWidget(self._vue_suivi())
        return page

    def _vue_liste(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        self._l_titre = QLabel("Demandes")
        self._l_titre.setStyleSheet(
            f"color:{_TXT};font-size:14px;font-weight:600;")
        v.addWidget(self._l_titre)

        self._l_vide = QLabel("Aucune demande en cours.")
        self._l_vide.setWordWrap(True)
        self._l_vide.setStyleSheet(f"color:{_MUTED};font-size:11px;")
        v.addWidget(self._l_vide)

        zone = QScrollArea()
        zone.setWidgetResizable(True)
        zone.setFrameShape(QFrame.NoFrame)
        zone.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        interieur = QWidget()
        self._l_liste = QVBoxLayout(interieur)
        self._l_liste.setContentsMargins(0, 0, 0, 0)
        self._l_liste.setSpacing(6)
        self._l_liste.addStretch(1)
        zone.setWidget(interieur)
        v.addWidget(zone, 1)

        self._btn_situer = QPushButton("Situer les demandes")
        self._btn_situer.setMinimumHeight(32)
        self._btn_situer.setToolTip(
            "Lit votre position une fois pour classer les demandes par "
            "proximité. Environ 6 secondes.")
        self._btn_situer.style_base = (
            f"QPushButton{{background:{_GRISE};color:{_TXT};border:none;"
            f"border-radius:10px;font-size:11px;}}"
            f"QPushButton:disabled{{color:{_MUTED};}}")
        self._btn_situer.setStyleSheet(self._btn_situer.style_base)
        self._btn_situer.clicked.connect(self._situer)
        v.addWidget(self._btn_situer)
        return w

    def _vue_suivi(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        self._s_type = QLabel("")
        self._s_type.setStyleSheet(
            f"color:{_TXT};font-size:14px;font-weight:600;")
        v.addWidget(self._s_type)

        self._s_phrase = QLabel("")
        self._s_phrase.setWordWrap(True)
        self._s_phrase.setStyleSheet(f"color:{_TXT};font-size:12px;")
        v.addWidget(self._s_phrase)

        self._s_technique = QLabel("")
        self._s_technique.setWordWrap(True)
        self._s_technique.setStyleSheet(
            f"color:{_MUTED};font-family:Consolas,monospace;font-size:9px;")
        v.addWidget(self._s_technique)

        self._s_message = QLabel("")
        self._s_message.setWordWrap(True)
        self._s_message.setStyleSheet(
            f"color:{_TXT};font-size:11px;font-style:italic;")
        v.addWidget(self._s_message)

        self._s_age = QLabel("")
        self._s_age.setStyleSheet(f"color:{_MUTED};font-size:10px;")
        v.addWidget(self._s_age)

        v.addStretch(1)

        self._s_distance = QLabel("—")
        self._s_distance.setAlignment(Qt.AlignCenter)
        self._s_distance.setStyleSheet(
            f"color:{_TXT};font-size:26px;font-weight:600;")
        v.addWidget(self._s_distance)

        self._s_tendance = QLabel("")
        self._s_tendance.setAlignment(Qt.AlignCenter)
        self._s_tendance.setStyleSheet(f"color:{_MUTED};font-size:11px;")
        v.addWidget(self._s_tendance)

        v.addStretch(1)

        self._btn_terminer = QPushButton("Terminer l'intervention")
        self._btn_terminer.setMinimumHeight(38)
        self._btn_terminer.style_base = (
            f"QPushButton{{background:{_VERT};color:#ffffff;border:none;"
            f"border-radius:10px;font-size:12px;font-weight:600;}}")
        self._btn_terminer.setStyleSheet(self._btn_terminer.style_base)
        self._btn_terminer.clicked.connect(
            lambda: self._agir("urgence_terminer"))
        v.addWidget(self._btn_terminer)

        self._btn_relacher = QPushButton("Abandonner l'intervention")
        self._btn_relacher.setMinimumHeight(34)
        self._btn_relacher.style_base = (
            f"QPushButton{{background:{_GRISE};color:{_ROUGE};border:none;"
            f"border-radius:10px;font-size:11px;}}")
        self._btn_relacher.setStyleSheet(self._btn_relacher.style_base)
        self._btn_relacher.clicked.connect(
            lambda: self._agir("urgence_relacher"))
        v.addWidget(self._btn_relacher)
        return w

    def _agir(self, type_, **champs):
        """Envoie une intention portant sur la demande suivie."""
        sig = self._signal_suivi()
        if sig is None:
            return
        _envoyer({"type": type_, "id": sig.get("id"), **champs})

    def _signal_suivi(self):
        for sig in self._etat.signaux:
            if sig.get("mien"):
                return sig
        return None

    # -- liste ---------------------------------------------------------

    def _situer(self):
        """Lit ma position UNE fois, pour classer la liste.

        Une seule mesure suffit pour toutes les demandes : c'est ma
        position qui coute six secondes, pas la comparaison. Ponctuelle,
        et non repetee -- le rafraichissement continu ne demarre qu'apres
        la prise en charge.
        """
        self._lancer_mesure(pour_liste=True)

    def _rendre_liste(self):
        while self._l_liste.count() > 1:
            item = self._l_liste.takeAt(0)
            wgt = item.widget()
            if wgt is not None:
                wgt.deleteLater()

        signaux = list(self._etat.signaux)
        self._l_vide.setVisible(not signaux)
        self._l_titre.setText(f"Demandes ({len(signaux)})"
                              if signaux else "Demandes")
        # Tri par proximite : les demandes situables d'abord, dans
        # l'ordre. Sans position lue, on garde l'ordre chronologique --
        # a defaut de proximite, l'anciennete est le meilleur critere.
        signaux.sort(key=lambda s: (self._rang_proximite(s),
                                    s.get("cree_le") or 0.0))
        for i, sig in enumerate(signaux):
            self._l_liste.insertWidget(i, self._ligne_demande(sig))

    def _proximite(self, sig):
        """Tranche de proximite d'une demande, ou None si inconnue.

        [URGENCE 15/08/2026] Ici -- et ICI SEULEMENT -- le repli sur les
        coordonnees systeme est autorise malgre la presence d'un astre.
        Elles derivent avec l'orbite, donc elles sont fausses ; mais pour
        CHOISIR une demande on n'a pas besoin d'une distance juste, on a
        besoin de savoir si c'est loin. Une erreur de 2 000 km sur un
        ecart de 12 millions ne change aucune decision.
        
        L'ecran de suivi, lui, garde la regle stricte : un nombre
        seulement dans un referentiel commun. Deux usages, deux
        exigences.
        """
        moi = self._ma_position
        pos = sig.get("position")
        if not moi or not pos:
            return None
        det = U.distance_detail(pos, moi)
        if det.get("distance") is not None and det.get("fiable"):
            return ("Sur place", _VERT)
        # Repli systeme, assume et approximatif.
        a = (pos or {}).get("system")
        b = (moi or {}).get("system")
        if not a or not b:
            return ("Position inconnue", _MUTED)
        try:
            d = ((float(a["x"]) - float(b["x"])) ** 2
                 + (float(a["y"]) - float(b["y"])) ** 2
                 + (float(a["z"]) - float(b["z"])) ** 2) ** 0.5
        except Exception:
            return None
        if d > 1e11:
            # Deux ordres de grandeur au-dessus de la taille d'un
            # systeme : les reperes ne sont pas les memes.
            return ("Autre système", _ROUGE)
        if d < 1_000_000:
            return ("Proche", _ACCENT)
        return ("Loin", _MUTED)

    def _rang_proximite(self, sig):
        p = self._proximite(sig)
        if p is None:
            return 9
        return {"Sur place": 0, "Proche": 1, "Loin": 2,
                "Autre système": 3}.get(p[0], 8)

    def _ligne_demande(self, sig):
        w = QFrame()
        pris = bool(sig.get("pris"))
        w.setStyleSheet(
            f"QFrame{{background:{_GRISE};border-radius:10px;"
            f"border:{'2px solid ' + _VERT if pris else 'none'};}}")
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(2)

        pos = sig.get("position")
        titre = QLabel(U.phrase_position(pos) if pos
                       else "Position non lue.")
        titre.setWordWrap(True)
        titre.setStyleSheet(
            f"color:{_TXT};font-size:11px;font-weight:600;border:none;")
        v.addWidget(titre)

        txt = (sig.get("texte") or "").strip()
        if txt:
            msg = QLabel(f"« {txt} »")
            msg.setWordWrap(True)
            msg.setStyleSheet(
                f"color:{_TXT};font-size:10px;font-style:italic;"
                f"border:none;")
            v.addWidget(msg)

        bas = QHBoxLayout()
        bas.setSpacing(6)
        age = QLabel(U.age_texte(sig.get("cree_le")))
        age.setStyleSheet(f"color:{_MUTED};font-size:9px;border:none;")
        bas.addWidget(age)
        prox = self._proximite(sig)
        if prox:
            lbl = QLabel(prox[0])
            lbl.setStyleSheet(
                f"color:{prox[1]};font-size:9px;font-weight:600;"
                f"border:none;")
            bas.addWidget(lbl)
        bas.addStretch(1)
        btn = QPushButton("Prendre")
        btn.setMinimumHeight(26)
        btn.style_base = (
            f"QPushButton{{background:{_ACCENT};color:#ffffff;border:none;"
            f"border-radius:8px;font-size:10px;padding:2px 10px;}}")
        btn.setStyleSheet(btn.style_base)
        btn.clicked.connect(
            lambda _=False, i=sig.get("id"):
            _envoyer({"type": "urgence_prendre", "id": i}))
        bas.addWidget(btn)
        v.addLayout(bas)
        w._btn_prendre = btn
        return w

    # -- suivi ---------------------------------------------------------

    def _rendre_suivi(self, sig):
        self._s_type.setText(U.libelle_type(sig.get("type")))
        pos = sig.get("position")
        self._s_phrase.setText(U.phrase_position(pos) if pos
                               else "Position non lue.")
        tech = U.phrase_technique(pos) if pos else ""
        self._s_technique.setText(tech)
        self._s_technique.setVisible(bool(tech))
        txt = (sig.get("texte") or "").strip()
        self._s_message.setText(f"« {txt} »" if txt else "")
        self._s_message.setVisible(bool(txt))
        self._s_age.setText(
            f"Signal émis {U.age_texte(sig.get('cree_le'))}")
        if pos and pos.get("lecture_simple"):
            self._s_age.setText(
                self._s_age.text() + " — " + U.MESSAGE_MOUVEMENT)

    # -----------------------------------------------------------------
    #  Onglet Administratif
    # -----------------------------------------------------------------

    def _page_admin(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        # Prise de service. Absente chez le chef : il est de service des
        # sa connexion -- il est le filet de securite du dispositif, et sa
        # seule echappatoire est de se deconnecter.
        self._btn_service = QPushButton("Prendre mon service")
        self._btn_service.setMinimumHeight(44)
        self._btn_service.style_base = (
            f"QPushButton{{background:{_ACCENT};color:#ffffff;border:none;"
            f"border-radius:12px;font-size:13px;font-weight:600;}}")
        self._btn_service.setStyleSheet(self._btn_service.style_base)
        self._btn_service.clicked.connect(self._basculer_service)
        v.addWidget(self._btn_service)

        # Zone du chef : saisie d'un numero pour attribuer SON role.
        self._bloc_chef = QWidget()
        hc = QHBoxLayout(self._bloc_chef)
        hc.setContentsMargins(0, 0, 0, 0)
        hc.setSpacing(4)
        self._champ_numero = _ChampNumero()
        self._champ_numero.setPlaceholderText("Numéro à recruter")
        self._champ_numero.setMaxLength(6)
        self._champ_numero_base = (
            f"QLineEdit{{background:{_GRISE};color:{_TXT};border:none;"
            f"border-radius:8px;padding:6px;font-size:11px;}}")
        self._champ_numero.setStyleSheet(self._champ_numero_base)
        hc.addWidget(self._champ_numero, 1)
        self._btn_recruter = QPushButton("Ajouter")
        self._btn_recruter.setMinimumHeight(30)
        self._btn_recruter.style_base = (
            f"QPushButton{{background:{_ACCENT};color:#ffffff;border:none;"
            f"border-radius:8px;font-size:11px;padding:2px 12px;}}")
        self._btn_recruter.setStyleSheet(self._btn_recruter.style_base)
        self._btn_recruter.clicked.connect(self._recruter)
        hc.addWidget(self._btn_recruter)
        v.addWidget(self._bloc_chef)

        self._a_titre = QLabel("")
        self._a_titre.setStyleSheet(
            f"color:{_TXT};font-size:13px;font-weight:600;")
        v.addWidget(self._a_titre)

        zone = QScrollArea()
        zone.setWidgetResizable(True)
        zone.setFrameShape(QFrame.NoFrame)
        zone.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        interieur = QWidget()
        self._a_liste = QVBoxLayout(interieur)
        self._a_liste.setContentsMargins(0, 0, 0, 0)
        self._a_liste.setSpacing(4)
        self._a_liste.addStretch(1)
        zone.setWidget(interieur)
        v.addWidget(zone, 1)

        self._a_erreur = QLabel("")
        self._a_erreur.setWordWrap(True)
        self._a_erreur.setStyleSheet(f"color:{_ROUGE};font-size:10px;")
        self._a_erreur.setVisible(False)
        v.addWidget(self._a_erreur)
        return page

    def _basculer_service(self):
        _envoyer({"type": "urgence_service",
                  "actif": not self._etat.en_service})

    def _recruter(self):
        num = self._champ_numero.text().strip()
        if not num:
            return
        _envoyer({"type": "urgence_attribuer", "numero": num})
        self._champ_numero.clear()

    def _retirer(self, numero):
        """Retire un role. Passe par un ecran de confirmation.

        Un clic malencontreux retirerait quelqu'un de l'equipe, et il
        faudrait redemander son numero pour le remettre.
        """
        self._a_retirer = str(numero)
        self._conf_texte.setText(
            f"Retirer le rôle à {self._nom_affiche(numero)} ?")
        self._btn_conf.setText("Retirer")
        self._retour_confirmation = self._PAGE_ADMIN
        self._aller_etape(self._ETAPE_CONFIRME)
        # [18/08/2026] L'ecran de confirmation vit dans la pile de
        # l'onglet URGENCE. Sans ce changement de page, on basculait une
        # pile qu'on ne regarde pas : la croix semblait morte, alors
        # qu'elle avait bien fait son travail.
        self._aller_page(self._PAGE_DEMANDE)

    def _rendre_admin(self):
        e = self._etat
        chef = e.chef
        self._bloc_chef.setVisible(bool(chef))
        self._btn_service.setVisible(not chef)
        if not chef:
            if e.en_service:
                self._btn_service.setText("Quitter mon service")
                self._btn_service.style_base = (
                    f"QPushButton{{background:{_GRISE};color:{_TXT};"
                    f"border:none;border-radius:12px;font-size:13px;"
                    f"font-weight:600;}}")
            else:
                self._btn_service.setText("Prendre mon service")
                self._btn_service.style_base = (
                    f"QPushButton{{background:{_ACCENT};color:#ffffff;"
                    f"border:none;border-radius:12px;font-size:13px;"
                    f"font-weight:600;}}")
            self._btn_service.setStyleSheet(self._btn_service.style_base)

        while self._a_liste.count() > 1:
            item = self._a_liste.takeAt(0)
            wgt = item.widget()
            if wgt is not None:
                wgt.deleteLater()

        if chef:
            # Le chef voit TOUTE son equipe, en service ou non : sinon il
            # ne peut ni retirer quelqu'un, ni savoir qui il a deja nomme.
            membres = e.equipe
            dispo = sum(1 for m in membres if m.get("en_service"))
            self._a_titre.setText(
                f"{U.libelle_role(e.role)}s — {dispo} en service")
        else:
            # Un detenteur ordinaire ne voit que ceux qui ont pointe :
            # l'etat de connexion des autres n'est pas son affaire.
            membres = [{"numero": n, "chef": False, "en_service": True}
                       for n in e.collegues]
            self._a_titre.setText(f"{len(membres)} collègue(s) en service")

        for i, m in enumerate(membres):
            self._a_liste.insertWidget(i, self._ligne_membre(m, chef))
        if self._pages.currentIndex() == self._PAGE_ADMIN:
            self._construire_nav()

    def _ligne_membre(self, membre, chef):
        w = QFrame()
        w.setStyleSheet(f"QFrame{{background:{_GRISE};border-radius:8px;}}")
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(8)

        num = str(membre.get("numero") or "")
        nom = self._nom_affiche(num)
        lbl = QLabel(nom + ("  ★" if membre.get("chef") else ""))
        lbl.setStyleSheet(f"color:{_TXT};font-size:13px;border:none;")
        h.addWidget(lbl)

        if membre.get("en_service"):
            pt = QLabel("en service")
            pt.setStyleSheet(
                f"color:{_VERT};font-size:11px;border:none;")
            h.addWidget(pt)
        h.addStretch(1)

        # La croix n'apparait que pour le chef, et jamais sur sa propre
        # ligne : il se retirerait un role que personne ne peut lui
        # rendre, seul un administrateur nommant les chefs.
        if chef and not membre.get("chef"):
            croix = QPushButton("✕")
            croix.setFixedSize(30, 30)
            croix.setCursor(Qt.PointingHandCursor)
            croix.setToolTip("Retirer le rôle")
            croix.setStyleSheet(
                f"QPushButton{{background:transparent;color:{_ROUGE};"
                f"border:none;font-size:16px;font-weight:600;}}")
            croix.style_base = croix.styleSheet()
            croix.clicked.connect(lambda _=False, n=num: self._retirer(n))
            h.addWidget(croix)
            # Memorisee sur la ligne : _construire_nav les recollecte a
            # chaque reconstruction plutot que de les garder, sinon la
            # croix pointerait sur des widgets detruits.
            w._btn_croix = croix
        return w

    def _nom_affiche(self, numero):
        """Nom a montrer pour un numero d'equipier.

        Trois sources, dans cet ordre : MOI d'abord -- se lire soi-meme
        sous forme de numero est deroutant, et le client connait son
        propre pseudo sans que rien ne transite. Puis le carnet de
        contacts. Puis le numero brut, faute de mieux.
        """
        num = str(numero or "")
        if num and num == str(self._mon_numero() or ""):
            moi = getattr(self.services, "my_name", None)
            return str(moi) if moi else num
        return self._nom_contact(num) or num

    def _nom_contact(self, numero):
        """Nom du contact enregistre, ou None.

        Le serveur n'envoie que des NUMEROS : il ne divulgue aucun
        pseudo. Si le joueur a deja ce numero dans son carnet, c'est SON
        carnet qui donne le nom -- rien de nouveau ne transite, et un
        secouriste qui n'a echange avec personne ne voit que des
        numeros.
        """
        rep = getattr(self.services, "repertoire", None)
        if rep is None:
            return None
        try:
            return rep.nom_pour(str(numero))
        except Exception:
            return None

    # -----------------------------------------------------------------
    #  Parcours
    # -----------------------------------------------------------------

    def _aller_etape(self, k):
        if self._dans_champ:
            self._sortir_du_champ()
        self._etapes.setCurrentIndex(k)
        self._erreur.setVisible(False)
        self._construire_nav()

    def _annuler_confirmation(self):
        retour = self._retour_confirmation
        self._retour_confirmation = None
        self._a_retirer = None
        self._aller_etape(self._ETAPE_CHOIX)
        if retour is not None:
            self._aller_page(retour)

    def _demander(self, type_urgence):
        self._type_confirme = type_urgence
        self._retour_confirmation = None
        self._a_retirer = None
        self._btn_conf.setText("Confirmer")
        self._conf_texte.setText(
            f"Confirmez-vous votre {U.libelle_type(type_urgence).lower()} ?")
        self._btn_conf.setStyleSheet(
            self._btn_conf.style_base.replace(
                _ROUGE, _ROUGE if type_urgence == U.TYPE_MEDICAL else _ACCENT))
        self._aller_etape(self._ETAPE_CONFIRME)

    def _confirmer(self):
        # La confirmation sert a DEUX choses : declencher une urgence, et
        # retirer un role. Une seule etape plutot que deux ecrans
        # presque identiques ; _retour_confirmation dit d'ou l'on vient.
        if self._retour_confirmation == self._PAGE_ADMIN:
            num, self._a_retirer = self._a_retirer, None
            self._retour_confirmation = None
            _envoyer({"type": "urgence_attribuer", "numero": num,
                      "retirer": True})
            self._aller_etape(self._ETAPE_CHOIX)
            self._aller_page(self._PAGE_ADMIN)
            return

        # La disponibilite est verifiee par le SERVEUR, seul a savoir qui
        # est en service. Le client capture d'abord, puis envoie : un
        # refus arrive donc apres la lecture.
        #
        # L'inverse -- demander avant de capturer -- economiserait six
        # secondes d'OCR dans le cas ou personne n'ecoute, mais ajouterait
        # un aller-retour reseau AVANT chaque declenchement reussi. Or le
        # cas frequent est celui ou quelqu'un est de garde.
        self._action = "creer"
        self._type_en_cours = self._type_confirme
        self._lancer_capture()

    def _actualiser_position(self):
        self._action = "actualiser"
        self._lancer_capture()

    def _abandonner(self):
        # On n'efface RIEN localement : le serveur retire la demande et
        # renvoie l'etat. Retirer d'abord ferait disparaitre l'ecran
        # avant que le serveur ait confirme -- et si la trame se perd, la
        # demande vivrait encore sans que la victime le sache.
        _envoyer({"type": "urgence_abandonner"})

    def _accuser_expiration(self):
        # Le serveur a deja purge le signal ; il ne reste qu'a quitter
        # l'ecran d'expiration.
        self._texte.clear()
        self._aller_etape(self._ETAPE_CHOIX)

    # -----------------------------------------------------------------
    #  Capture
    # -----------------------------------------------------------------

    def _lancer_capture(self):
        if self._captureur is not None and self._captureur.isRunning():
            return
        if not self._zone:
            self._erreur.setText("Position illisible : zone OCR introuvable.")
            self._erreur.setVisible(True)
            self._aller_etape(self._ETAPE_CHOIX)
            return
        self._btn_conf.setEnabled(False)
        self._btn_maj.setEnabled(False)
        self._t0 = time.perf_counter()
        self._chrono.start()
        self._tic_capture()
        self._horo = _horodatage()
        self._dossier_log = _dossier_logs()
        self._captureur = _Captureur(self._zone, dossier=self._dossier_log,
                                     horo=self._horo, parent=self)
        self._captureur.fini.connect(self._capture_finie)
        self._captureur.start()
        self._log(f"capture ({self._action})")

    def _tic_capture(self):
        if self._t0 is None:
            return
        t = time.perf_counter() - self._t0
        libelle = f"Lecture de votre position… {t:.1f} s"
        if self._action == "creer":
            self._conf_texte.setText(libelle)
        else:
            self._btn_maj.setText(libelle)

    def _capture_finie(self, hier, journal, chemin_img, simple):
        self._chrono.stop()
        duree = (time.perf_counter() - self._t0) if self._t0 else 0.0
        self._t0 = None
        self._derniere_duree = duree
        self._btn_conf.setEnabled(True)
        self._btn_maj.setEnabled(True)
        self._btn_maj.setText("Actualiser ma position")

        maintenant = time.time()
        position = U.position_depuis_hierarchie(hier, maintenant,
                                                lecture_simple=simple)
        self._derniere_position = position

        if hier is None:
            # Sans position, on cree quand meme la demande : un signal
            # sans lieu vaut mieux que pas de signal -- le secouriste
            # saura au moins que quelqu'un a besoin d'aide, quitte a
            # demander par radio.
            self._log("position non lue, demande envoyee sans position")

        self._ecrire_compte_rendu(hier, journal, chemin_img, duree)

        if self._action == "creer":
            envoye = _envoyer({
                "type": "urgence_creer",
                "urgence_type": self._type_en_cours,
                "position": position,
                "texte": self._texte.toPlainText(),
            })
        else:
            envoye = _envoyer({"type": "urgence_position",
                               "position": position})
        if not envoye:
            self._aller_etape(self._ETAPE_CHOIX)
            self._erreur.setText(
                "Serveur injoignable : reconnectez-vous pour déclencher "
                "une urgence.")
            self._erreur.setVisible(True)
            return
        # L'ecran ne bascule PAS ici : il attend urgence_etat. Basculer
        # tout de suite montrerait une demande en cours que le serveur
        # aurait pu refuser.

    # -----------------------------------------------------------------
    #  Affichage de la demande
    # -----------------------------------------------------------------

    def _battement(self):
        """Rafraichit l'AGE affiche, une fois par seconde.

        L'etat, lui, vient du serveur : le battement ne le devine pas. Il
        ne redessine que ce qui depend de l'horloge locale -- sans quoi
        "Demande envoyée à l'instant" resterait affiche une heure.
        """
        d = self._etat.ma_demande
        if not d:
            self._pouls.stop()
            return
        self._d_age.setText(
            f"Demande envoyée {U.age_texte(d.get('cree_le'))}")

    def _rendre(self):
        """Redessine la page de demande depuis l'etat serveur.

        L'ecran ne garde AUCUN etat propre : tout vient de
        _EtatUrgence. C'est ce qui evite qu'un affichage survive a la
        disparition de ce qu'il decrit -- "les secours arrivent" doit
        redevenir "en attente" si le dernier preneur relache.
        """
        d = self._etat.ma_demande
        if not d:
            return
        self._d_type.setText(U.libelle_type(d.get("type")))
        pos = d.get("position")
        phrase = U.phrase_position(pos) if pos else "Position non lue."
        if pos and pos.get("lecture_simple"):
            phrase += f" {U.MESSAGE_MOUVEMENT}"
        self._d_phrase.setText(phrase)
        txt = (d.get("texte") or "").strip()
        self._d_desc.setText(f"« {txt} »" if txt else "")
        self._d_desc.setVisible(bool(txt))
        self._d_age.setText(
            f"Demande envoyée {U.age_texte(d.get('cree_le'))}")

        if d.get("preneurs"):
            self._d_etat.setText("Urgence prise, les secours arrivent")
            self._d_etat.setStyleSheet(
                f"color:{_VERT};font-size:14px;font-weight:600;padding:6px;")
        else:
            self._d_etat.setText("Urgence en attente")
            self._d_etat.setStyleSheet(
                f"color:{_ORANGE};font-size:14px;font-weight:600;"
                f"padding:6px;")

    # -----------------------------------------------------------------
    #  Mesure de MA position  (cote secouriste)
    # -----------------------------------------------------------------
    #
    # Deux usages, un seul mecanisme :
    #   - PONCTUEL a l'ouverture de la liste, pour classer les demandes
    #     par proximite. Une mesure suffit pour toutes : c'est ma
    #     position qui coute six secondes, pas la comparaison.
    #   - CONTINU sur l'ecran de suivi, entre 10 et 30 s selon la
    #     distance. Il ne demarre qu'apres la prise en charge.

    def _lancer_mesure(self, pour_liste=False):
        if self._captureur is not None and self._captureur.isRunning():
            # Une capture tourne deja. On repasse plutot que d'abandonner
            # en silence -- sinon l'ecran afficherait une distance figee
            # sans que rien ne l'explique.
            if not pour_liste:
                self._minuteur.start(2000)
            return
        if not self._zone:
            self._s_distance.setText("Zone OCR introuvable")
            return
        self._pour_liste = bool(pour_liste)
        self._decompte.stop()
        self._echeance = None
        self._btn_situer.setEnabled(False)
        self._btn_situer.setText("Lecture…")
        # Lecture SIMPLE : le secouriste se deplace, et il remesure
        # regulierement. Exiger deux lectures concordantes a 5 m pres
        # refuserait tout des qu'il vole -- observe le 13/08, 233 m
        # d'ecart entre deux lectures en Cutlass.
        self._captureur = _Captureur(self._zone, dossier=None,
                                     double_lecture=False, parent=self)
        self._captureur.fini.connect(self._mesure_finie)
        self._captureur.start()

    def _mesure_finie(self, hier, journal, _img, _simple):
        self._btn_situer.setEnabled(True)
        self._btn_situer.setText("Situer les demandes")
        maintenant = time.time()
        self._ma_position = U.position_depuis_hierarchie(
            hier, maintenant, lecture_simple=True)

        if self._pour_liste:
            self._pour_liste = False
            self._rendre_liste()
            self._construire_nav()
            return

        sig = self._signal_suivi()
        if sig is None:
            return
        det = U.distance_detail(sig.get("position"), self._ma_position)
        d = det.get("distance")
        if d is None:
            # Regle STRICTE sur l'ecran de suivi : pas de referentiel
            # commun, pas de nombre. La distance systeme derive avec
            # l'orbite, et un chiffre credible mais faux enverrait le
            # secouriste ailleurs.
            self._s_distance.setText("Pas de distance")
            self._s_tendance.setText(
                "Rejoignez la zone indiquée : la distance apparaîtra dès "
                "que vous partagerez un container avec la victime.")
        else:
            self._s_distance.setText(U.distance_texte(d))
            t = ""
            if self._derniere_distance is not None:
                dt = maintenant - (self._derniere_mesure or maintenant)
                t = U.tendance(self._derniere_distance, d, dt)
                t = {"rapprochement": "Vous vous rapprochez",
                     "eloignement": "Vous vous éloignez",
                     "stable": "Distance stable"}.get(t, "")
            self._s_tendance.setText(t)
            self._derniere_distance = d
            self._derniere_mesure = maintenant
        self._replanifier(d)

    def _replanifier(self, distance):
        """Programme la mesure suivante, sur l'ecran de suivi seulement.

        Le delai suit la distance qu'on VIENT de mesurer : il se resserre
        en approche, et reste au maximum tant qu'aucun referentiel commun
        n'existe -- la mesure ne rend alors rien d'utile, mais elle reste
        necessaire pour DETECTER l'arrivee dans le container de la
        victime.
        """
        if self._pages.currentIndex() != self._PAGE_LISTE:
            return
        if self._vues_dem.currentIndex() != 1:
            return
        delai = U.delai_rafraichissement(distance)
        self._minuteur.start(int(delai * 1000))
        self._echeance = time.monotonic() + delai
        self._decompte.start()
        self._tic_decompte()

    def _tic_decompte(self):
        if self._echeance is None:
            self._decompte.stop()
            return
        reste = self._echeance - time.monotonic()
        if reste <= 0:
            self._decompte.stop()
            return
        self._s_age.setToolTip(f"Prochaine mesure dans {reste:.0f} s")

    # -----------------------------------------------------------------
    #  Etat pousse par le serveur
    # -----------------------------------------------------------------

    def appliquer_etat(self, data):
        """Recoit urgence_etat et redessine TOUT.

        Point d'entree unique de tout changement d'ecran. Le serveur
        pousse cet etat apres chaque action, mais aussi sans qu'on ait
        rien demande : la demande a ete prise, elle a expire, un collegue
        a pointe. C'est pour ca que l'ecran se deduit entierement de
        l'etat plutot que d'etre pilote par les clics.
        """
        avant_demande = self._etat.ma_demande
        avait_suivi = self._signal_suivi() is not None
        self._etat.appliquer(data)
        err = (data or {}).get("erreur") or ""

        # La barre d'onglets n'apparait qu'avec un role : un joueur
        # ordinaire n'a qu'un ecran, et une barre a un seul onglet est du
        # decor qui coute de la place.
        avait_barre = self._barre.isVisible()
        self._barre.setVisible(bool(self._etat.role))
        if avait_barre and not self._etat.role:
            self._aller_page(self._PAGE_DEMANDE)

        self._rendre_liste()
        self._rendre_admin()

        # Ecran de suivi des qu'une demande est prise, liste sinon.
        sig = self._signal_suivi()
        self._vues_dem.setCurrentIndex(1 if sig else 0)
        if sig:
            self._rendre_suivi(sig)
            if not avait_suivi:
                # Nouvelle prise en charge : on remesure tout de suite
                # plutot que d'attendre le premier delai, sinon l'ecran
                # afficherait un tiret pendant une demi-minute.
                self._derniere_distance = None
                self._derniere_mesure = None
                self._lancer_mesure()
        elif avait_suivi:
            self._minuteur.stop()
            self._decompte.stop()
            self._echeance = None

        # Erreur d'attribution : elle appartient a l'onglet du chef.
        if err and self._pages.currentIndex() == self._PAGE_ADMIN:
            self._a_erreur.setText(err)
            self._a_erreur.setVisible(True)
        else:
            self._a_erreur.setVisible(False)

        d = self._etat.ma_demande
        if d:
            self._rendre()
            self._aller_etape(self._ETAPE_DEMANDE)
            self._pouls.start()
        else:
            self._pouls.stop()
            if avant_demande is not None and not err:
                # La demande a disparu sans qu'on l'ait abandonnee : elle
                # a expire, ou un secouriste l'a terminee. Dans les deux
                # cas la victime doit le savoir, sinon son ecran
                # reviendrait au choix sans explication.
                self._aller_etape(self._ETAPE_EXPIREE)
            else:
                if not err:
                    self._texte.clear()
                self._aller_etape(self._ETAPE_CHOIX)
        if err and self._pages.currentIndex() == self._PAGE_DEMANDE:
            self._erreur.setText(err)
            self._erreur.setVisible(True)
        self._construire_nav()

    # -----------------------------------------------------------------
    #  Journal
    # -----------------------------------------------------------------

    def _mon_numero(self):
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

    def _log(self, msg):
        try:
            fn = getattr(self.services, "log", None)
            if callable(fn):
                fn(f"[URGENCE] {msg}")
        except Exception:
            pass

    def _ecrire_compte_rendu(self, hier, journal, chemin_img, duree):
        if not self._dossier_log:
            return ""
        chemin = os.path.join(self._dossier_log,
                              f"urgence_{self._horo}.txt")
        L = []
        L.append("=" * 62)
        L.append(f"URGENCE — {self._horo}  ({self._action})")
        L.append("=" * 62)
        L.append(f"Type      : {self._type_en_cours}")
        L.append(f"Numéro    : {self._mon_numero()}")
        L.append(f"Message   : {self._texte.toPlainText()!r}")
        L.append(f"Durée     : {duree:.2f} s")
        L.append(f"Zone OCR  : {self._zone}")
        L.append(f"Image     : {chemin_img or '(non enregistrée)'}")
        L.append("")
        if hier is None:
            L.append("RESULTAT : LECTURE REFUSEE")
        else:
            L.append("RESULTAT : lecture acceptée")
            for i, d in enumerate(hier.get("chain") or []):
                L.append(f"  [{i}] name={d.get('name')!r}")
                L.append(f"      cid={d.get('container_id')!r}")
                L.append(f"      local=({d.get('x')}, {d.get('y')}, "
                         f"{d.get('z')})")
            sysc = hier.get("system") or {}
            L.append(f"  SolarSystem : ({sysc.get('x')}, {sysc.get('y')}, "
                     f"{sysc.get('z')})")
            L.append("")
            L.append(f"PHRASE : {U.phrase_position(self._derniere_position)}")
            tech = U.phrase_technique(self._derniere_position)
            if tech:
                L.append(f"  technique : {tech}")
        L.append("")
        L.append("JOURNAL OCR")
        L.append(journal)
        try:
            with open(chemin, "w", encoding="utf-8") as f:
                f.write("\n".join(L))
        except Exception as e:
            self._log(f"ecriture du compte rendu KO : {e!r}")
            return ""
        return chemin

    # -- navigation D-pad ----------------------------------------------

    def _construire_nav(self):
        """Cibles de la croix directionnelle, de haut en bas.

        La liste depend de la page ET de l'etape : sinon la croix
        activerait des boutons invisibles, ou pire, le declenchement
        depuis la page de demande en cours.
        """
        page = self._pages.currentIndex()
        if page == self._PAGE_LISTE:
            if self._vues_dem.currentIndex() == 1:
                self._nav = [
                    (self._btn_terminer, self._btn_terminer.style_base),
                    (self._btn_relacher, self._btn_relacher.style_base),
                ]
            else:
                # Les boutons "Prendre" sont crees a la volee : on les
                # collecte a chaque reconstruction plutot que de les
                # memoriser, sinon la croix pointerait sur des widgets
                # detruits.
                self._nav = [(self._btn_situer,
                              self._btn_situer.style_base)]
                for i in range(self._l_liste.count() - 1):
                    w = self._l_liste.itemAt(i).widget()
                    btn = getattr(w, "_btn_prendre", None) if w else None
                    if btn is not None:
                        self._nav.append((btn, btn.style_base))
        elif page == self._PAGE_ADMIN:
            self._nav = []
            if self._etat.chef:
                self._nav.append((self._champ_numero,
                                  self._champ_numero_base))
                self._nav.append((self._btn_recruter,
                                  self._btn_recruter.style_base))
                # [18/08/2026] Les croix de retrait sont des cibles a
                # part entiere. Sans elles, un chef au D-pad pouvait
                # recruter mais jamais retirer -- l'equipe ne savait que
                # grandir.
                for i in range(self._a_liste.count() - 1):
                    w = self._a_liste.itemAt(i).widget()
                    croix = getattr(w, "_btn_croix", None) if w else None
                    if croix is not None:
                        self._nav.append((croix, croix.style_base))
            else:
                self._nav.append((self._btn_service,
                                  self._btn_service.style_base))
        else:
            k = self._etapes.currentIndex()
            if k == self._ETAPE_CHOIX:
                # La description EN PREMIER : c'est le haut de l'ecran, et
                # commencer sur un bouton de declenchement mettrait la
                # surbrillance sur l'action irreversible des l'ouverture.
                self._nav = [
                    (self._texte, self._texte_base),
                    (self._btn_med, self._btn_med.style_base),
                    (self._btn_sec, self._btn_sec.style_base),
                ]
            elif k == self._ETAPE_CONFIRME:
                self._nav = [
                    (self._btn_conf, self._btn_conf.style_base),
                    (self._btn_annul, self._btn_annul.style_base),
                ]
            elif k == self._ETAPE_DEMANDE:
                self._nav = [
                    (self._btn_maj, self._btn_maj.style_base),
                    (self._btn_abandon, self._btn_abandon.style_base),
                ]
            else:
                self._nav = [(self._btn_ok, self._btn_ok.style_base)]
        self._cible = min(self._cible, max(0, len(self._nav) - 1))
        self._peindre_nav()

    def _peindre_nav(self):
        for i, (w, base) in enumerate(self._nav):
            if i != self._cible:
                w.setStyleSheet(base)
                continue
            # Le contour s'applique au type reel du widget : un selecteur
            # QPushButton n'aurait aucun effet sur un champ de saisie, et
            # la cible paraitrait perdue.
            if w is self._texte:
                sel = "QPlainTextEdit"
            elif w is self._champ_numero:
                sel = "QLineEdit"
            else:
                sel = "QPushButton"
            # Contour ACCENT pendant la saisie, noir en simple selection :
            # sans ca, rien ne distingue "le champ est vise" de "j'ecris
            # dedans", et on ne sait pas si Entree va ouvrir le clavier
            # ou en sortir.
            en_saisie = (self._dans_champ
                         and w is getattr(self, "_champ_actif", None))
            couleur = _ACCENT if en_saisie else _HALO
            w.setStyleSheet(base + f"{sel}{{border:2px solid {couleur};}}")

    def _aller_page(self, k):
        self._pages.setCurrentIndex(k)
        for i, b in enumerate(self._onglets):
            actif = (i == k)
            b.setChecked(actif)
            b.setStyleSheet(
                f"QPushButton{{background:"
                f"{_ACCENT if actif else _GRISE};"
                f"color:{'#ffffff' if actif else _MUTED};border:none;"
                f"border-radius:8px;padding:5px;font-size:11px;}}")
        # La mesure continue ne tourne QUE sur l'ecran de suivi : ailleurs
        # elle prendrait de l'OCR a la boucle de proximite pour un ecran
        # que personne ne regarde.
        if not (k == self._PAGE_LISTE
                and self._vues_dem.currentIndex() == 1):
            self._minuteur.stop()
            self._decompte.stop()
            self._echeance = None
        self._cible = 0
        self._construire_nav()

    def handle_nav(self, direction):
        if self._dans_champ:
            # Dans le champ, tout appartient au widget de saisie : frappe,
            # curseur, effacement. Seule Entree nous concerne, pour en
            # sortir. On consomme le reste pour que les fleches ne
            # deplacent pas la selection pendant qu'on ecrit.
            if direction == "enter":
                self._sortir_du_champ()
                self._cible = min(self._cible + 1, len(self._nav) - 1)
                self._peindre_nav()
            return True
        if direction in ("left", "right") and self._barre.isVisible():
            k = self._pages.currentIndex()
            n = self._pages.count()
            self._aller_page((k - 1) % n if direction == "left"
                             else (k + 1) % n)
            return True
        if not self._nav:
            return False
        if direction == "up":
            self._cible = (self._cible - 1) % len(self._nav)
            self._peindre_nav()
            return True
        if direction == "down":
            self._cible = (self._cible + 1) % len(self._nav)
            self._peindre_nav()
            return True
        if direction == "enter":
            w = self._nav[self._cible][0]
            if w in (self._texte, self._champ_numero):
                self._entrer_dans_champ(w)
                return True
            if w.isEnabled():
                w.click()
            return True
        return False

    # -- saisie dans un champ -------------------------------------------
    #
    # [13/08/2026] Un setFocus() seul ne suffit PAS. L'overlay du
    # telephone porte le flag Qt.Tool : la fenetre ne prend pas le focus
    # clavier systeme, et les frappes partiraient dans Star Citizen --
    # avec le risque de declencher des commandes de jeu en essayant
    # d'ecrire.
    #
    # L'overlay expose entrer_dans_champ(widget), qui force la fenetre au
    # premier plan. C'est ce que fait deja l'app Travail pour ses champs.

    def _entrer_dans_champ(self, widget=None):
        widget = widget or self._texte
        self._champ_actif = widget
        self._dans_champ = True
        ov = self.window()
        fn = getattr(ov, "entrer_dans_champ", None)
        if fn is not None:
            fn(widget)
        else:
            # Repli hors overlay (tests, fenetre autonome).
            try:
                widget.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass
        self._peindre_nav()

    def _champ_vide(self) -> bool:
        w = getattr(self, "_champ_actif", None) or self._texte
        try:
            txt = (w.toPlainText() if hasattr(w, "toPlainText")
                   else w.text())
        except Exception:
            return True
        return not (txt or "").strip()

    def _sortir_du_champ(self):
        w = getattr(self, "_champ_actif", None) or self._texte
        self._dans_champ = False
        self._champ_actif = None
        try:
            w.clearFocus()
            self.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass
        self._peindre_nav()

    def handle_back(self) -> bool:
        """Retour : renonce a la confirmation avant de quitter l'app.

        Depuis la demande en cours, ne consomme pas : on ne quitte une
        demande que par le bouton d'abandon, pour qu'aucun retour
        reflexe ne la supprime.
        """
        if self._dans_champ:
            # [13/08/2026] Le retour arriere arrive ici comme un "esc",
            # EN PLUS d'etre recu par le champ qui a le focus. Sortir
            # sans condition faisait donc quitter la saisie a la premiere
            # faute de frappe -- le caractere etait bien efface, mais on
            # se retrouvait dehors.
            #
            # On ne sort que si le champ est VIDE, comme l'app Travail :
            # le retour arriere efface tant qu'il reste quelque chose,
            # puis ferme la saisie. Entree sort a tout moment.
            if self._champ_vide():
                self._sortir_du_champ()
            return True
        if self._etapes.currentIndex() == self._ETAPE_CONFIRME:
            self._aller_etape(self._ETAPE_CHOIX)
            return True
        return False

    # -- cycle de vie ---------------------------------------------------

    def on_show(self):
        # La zone est relue a chaque ouverture : elle peut avoir ete
        # recalibree dans les Parametres entre deux passages, et garder
        # l'ancienne ferait lire a cote sans que rien ne le signale.
        self._zone, self._origine_zone = _zone_ocr()

        # L'etat vient du SERVEUR : on le redemande a chaque ouverture
        # plutot que de reafficher celui d'il y a une heure. La demande a
        # pu etre prise, expirer, ou etre terminee entre-temps.
        _envoyer({"type": "urgence_etat"})

        # Ouverture sur l'onglet utile : les demandes si l'on est de
        # service, l'administratif sinon -- c'est la qu'on pointe.
        if self._etat.role:
            self._aller_page(self._PAGE_LISTE if self._etat.en_service
                             else self._PAGE_ADMIN)

        if self._etat.ma_demande:
            self._rendre()
            self._aller_etape(self._ETAPE_DEMANDE)
            self._pouls.start()

    def on_hide(self):
        # Le pouls continue volontairement : c'est lui qui fait avancer
        # la demande pendant que le joueur fait autre chose, comme le
        # fera le vrai serveur. Il ne coute rien -- un battement par
        # seconde, aucune capture d'ecran.
        #
        # La mesure secouriste, elle, s'arrete : elle prend de l'OCR a la
        # boucle de proximite pour un ecran que personne ne regarde.
        self._minuteur.stop()
        self._decompte.stop()
        self._echeance = None
