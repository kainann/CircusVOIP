#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[URGENCE 12/08/2026] Modele metier de l'app Urgence du CircusPhone.

Ce fichier ne connait NI le reseau, NI Qt. Il contient les regles, et
elles seules. Le serveur l'importe pour arbitrer, le client pour valider
avant d'envoyer.

>>> IL VA DES DEUX COTES. <<<
Comme circusvoip_phone_travail.py. Ne le pousser que d'un cote fait
diverger client et serveur SANS erreur visible : ils appliqueraient
simplement des regles differentes, et l'un accepterait ce que l'autre
refuse.

--- Un SIGNAL, pas un appel ---

Le point de depart de toute la conception. Un signal ne porte pas de
numero, donc il ne distribue pas d'annuaire, donc il ne contourne pas la
regle RP qui veut que les numeros s'echangent en jeu. C'est ce qui
distingue cette app du bouton d'appel de l'overlay (#39) et des groupes
(#23), tous deux bloques sur cette question.

Consequence : le serveur relaie, il n'arbitre pas une mise en relation.

--- Deux types, deux populations cloisonnees ---

Medical et securite. Un signal medical ne part PAS aux agents de
securite et reciproquement : a eux de s'organiser en jeu. Une agression
produit souvent les deux besoins -- c'est a la victime de declencher
deux signaux si elle le juge utile, pas au code de le decider.

--- Les roles ne sont PAS des metiers ---

Les huit metiers de l'app Travail sont AUTO-DECLARES : le joueur coche,
le serveur valide contre une liste fermee. La validation porte sur
QUOI, jamais sur QUI a le droit.

`medecin` et `securite` sont ATTRIBUES. S'ils vivaient dans la meme
liste, un client bricole enverrait `travail_metiers` avec `["medecin"]`
et se decernerait le role -- le serveur l'accepterait, puisque le nom
serait dans la liste fermee.

D'ou deux champs distincts dans la fiche, ecrits par deux chemins
differents. `valide_role_non_metier()` existe pour que le chemin des
metiers puisse REFUSER explicitement un identifiant de role.

Effet de bord heureux : METIERS_MAX = 2 ne s'applique pas aux roles. Un
medecin reste mineur-pilote s'il le souhaite.

--- Chefs ---

Un chef par role, designe cote admin. Il distribue et retire son role
depuis l'app, par NUMERO. L'attribution est immediate : pas
d'acceptation par le destinataire.

Ce choix ouvre un oracle d'existence -- un chef peut enumerer les
numeros et savoir lesquels sont attribues. Il est ASSUME : l'oracle
n'est ouvert qu'aux deux comptes designes a la main par un
administrateur, et l'acceptation aurait alourdi l'ecran pour un gain
theorique. C'est ecrit ici pour que la question ne ressorte pas dans six
mois comme une faille qu'on croirait avoir manquee.

Un chef ne peut pas faire de chef : sinon le controle se perd en deux
sauts.

--- Prise de service ---

Un detenteur de role connecte n'est pas de garde. Il pointe quand il
accepte de repondre, et il est HORS SERVICE a chaque connexion.

Ca n'est pas qu'un confort. Sans prise de service, "aucun medecin
disponible" serait faux des qu'un medecin joue a autre chose, et
l'ecran d'equipe du chef afficherait qui est connecte -- une
information subie. Avec, tout devient declaratif.

Le chef est en service de fait : il est le filet de securite. Il n'est
pas connecte en permanence pour autant, donc le message "personne de
disponible" garde tout son sens.

--- Position ---

FIGEE au declenchement. La victime est au sol, souvent incapable de
relancer quoi que ce soit -- une position vivante supposerait un client
qui tourne et un joueur qui bouge, soit l'inverse du cas d'usage.

Le prix est l'age : au bout de 40 minutes, un secouriste peut partir
vers un point ou la victime n'est plus. L'age est donc affiche a cote de
la position, jamais implicite.

--- Duree de vie ---

En RAM, jamais sur disque. Un joueur deconnecte est un signal
abandonne : rien a purger, rien a migrer, rien qui survive au
redemarrage. Expiration a 1 h pour le reste.
"""

from __future__ import annotations

import re
import time
import uuid

# ---------------------------------------------------------------------
#  Roles
# ---------------------------------------------------------------------

ROLE_MEDECIN  = "medecin"
ROLE_SECURITE = "securite"

# Liste FERMEE, comme les metiers, et pour la meme raison : un role
# libre au clavier produirait plusieurs populations qui ne se voient pas
# entre elles.
ROLES = (ROLE_MEDECIN, ROLE_SECURITE)

# Libelles separes des identifiants : les identifiants voyagent sur le
# reseau et sont stockes, les accentuer les rendrait fragiles au premier
# probleme d'encodage.
# [15/08/2026] Libelles fixes : "Médecin" et "Agent de sécurité".
#
# Ils sont separes des identifiants pour pouvoir changer SANS migration :
# `securite` voyage sur le reseau et vit dans les fiches, le libelle ne
# sort qu'a l'ecran. Si le corps prend un autre nom en jeu -- Advocacy,
# Marshal -- c'est une ligne a changer ici, et rien d'autre.
LIBELLES_ROLE = {
    ROLE_MEDECIN:  "Médecin",
    ROLE_SECURITE: "Agent de sécurité",
}

LIBELLES_CHEF = {
    ROLE_MEDECIN:  "Chef médical",
    ROLE_SECURITE: "Chef de la sécurité",
}

# Un joueur ne cumule pas les deux roles. Un medecin qui serait aussi
# agent de securite recevrait tout, et la separation des deux
# populations -- qui est le coeur du modele -- n'existerait plus.
CUMUL_ROLES = False

# ---------------------------------------------------------------------
#  Types d'urgence
# ---------------------------------------------------------------------

TYPE_MEDICAL  = "medical"
TYPE_SECURITE = "securite"

TYPES = (TYPE_MEDICAL, TYPE_SECURITE)

LIBELLES_TYPE = {
    TYPE_MEDICAL:  "Urgence médicale",
    TYPE_SECURITE: "Urgence sécurité",
}

# Quel role recoit quel type. Table explicite plutot qu'une egalite de
# chaines : les identifiants se ressemblent aujourd'hui (securite ->
# securite), et compter dessus casserait au premier renommage.
ROLE_DESTINATAIRE = {
    TYPE_MEDICAL:  ROLE_MEDECIN,
    TYPE_SECURITE: ROLE_SECURITE,
}

# ---------------------------------------------------------------------
#  Bornes
# ---------------------------------------------------------------------

# Message libre joint au signal. 120 caracteres, contre 300 pour une
# description de mission : un signal d'urgence se lit d'un coup d'oeil,
# et il est ecrit par quelqu'un qui n'a pas le temps.
TEXTE_MAX_LEN = 120

# Duree de vie d'un signal, depuis le declenchement.
EXPIRATION_S = 3600

# Pas de bornage du nombre de declenchements pour l'instant. C'est un
# choix, pas un oubli : un signal diffuse a tous les detenteurs en
# service EST une primitive de broadcast, donc abusable. On verra a
# l'usage si un plafond horaire devient necessaire.

# ---------------------------------------------------------------------
#  Etats
# ---------------------------------------------------------------------

ETAT_ACTIF = "actif"   # visible, secourable
ETAT_CLOS  = "clos"    # termine par la victime ou par un secouriste

_ETATS = (ETAT_ACTIF, ETAT_CLOS)

# ---------------------------------------------------------------------
#  Rafraichissement de la distance
# ---------------------------------------------------------------------
#
# Calculer une distance oblige le SECOURISTE a capturer sa propre
# hierarchie : la boucle temps reel ne lui donne que des coordonnees
# locales, inutilisables face a celles d'un autre container. Chaque
# rafraichissement coute donc deux passes EasyOCR -- d'ou une cadence,
# et non un calcul continu.

# Bornes du rafraichissement automatique de la distance, en secondes.
#
# [13/08/2026] Remplacent les deux paliers 60 s / 10 s de la spec, qui
# n'avaient jamais ete confrontes au cout reel : une mesure prend 4,4 a
# 8,9 s (moyenne 6,3 s sur les releves du 13/08), parce qu'elle lit huit
# lignes deux fois au lieu d'une.
#
# A 10 s d'intervalle, le cycle complet fait donc ~16 s dont 6 d'OCR --
# environ 40 % du temps pris a la boucle de proximite, qui n'a pas de
# cadence fixe et consomme tout ce que la machine donne. C'est assume en
# approche finale, ou la distance est ce qu'on regarde ; ca ne le serait
# pas en permanence, d'ou le palier haut.
RAFRAICHISSEMENT_MIN_S = 10.0
RAFRAICHISSEMENT_MAX_S = 30.0

# Distances entre lesquelles le delai varie. Sous 1 km on cherche a vue,
# au-dela de 100 km on voyage -- dans les deux cas mesurer plus souvent
# n'apprend rien de plus.
DISTANCE_PROCHE_M = 1_000.0
DISTANCE_LOIN_M = 100_000.0

# Conserves : la spec d'origine s'y refere, et le seuil d'approche sert
# encore a decider quand l'ecran bascule en mode distance.
SEUIL_APPROCHE_M = 50_000.0

VITESSE_MAX_M_S = 300_000.0


class UrgenceError(Exception):
    """Erreur metier, destinee a etre MONTREE au joueur.

    Le message doit donc etre lisible et actionnable : il finira dans
    une bulle d'erreur du telephone, pas dans un log.
    """


# ---------------------------------------------------------------------
#  Validation
# ---------------------------------------------------------------------

def valide_role(role) -> str:
    """Retourne l'identifiant de role normalise, ou leve UrgenceError."""
    r = str(role or "").strip().lower()
    if r not in ROLES:
        raise UrgenceError("Rôle inconnu.")
    return r


def valide_role_non_metier(nom) -> None:
    """Refuse un identifiant de role la ou un METIER est attendu.

    A appeler depuis le chemin `travail_metiers`. Sans ce garde-fou, un
    client bricole se decernerait `medecin` en le glissant dans sa liste
    de metiers : le nom serait inconnu de METIERS, donc deja refuse
    aujourd'hui -- mais le jour ou quelqu'un ajoutera `medecin` aux
    metiers "pour l'affichage", la faille s'ouvrira en silence. Ce
    controle explicite la ferme d'avance.
    """
    if str(nom or "").strip().lower() in ROLES:
        raise UrgenceError(
            "Ce rôle est attribué par un chef, il ne se déclare pas.")


def valide_type(type_urgence) -> str:
    t = str(type_urgence or "").strip().lower()
    if t not in TYPES:
        raise UrgenceError("Type d'urgence inconnu.")
    return t


def valide_texte(texte) -> str:
    """Message libre joint au signal. Vide permis.

    Vide vaut "rien a preciser", ce qui est une information : la victime
    n'a pas oublie le champ, elle n'avait rien a dire -- ou pas le temps.
    """
    t = " ".join(str(texte or "").split())
    if len(t) > TEXTE_MAX_LEN:
        raise UrgenceError(
            f"Le message est limité à {TEXTE_MAX_LEN} caractères.")
    return t


def valide_numero(numero) -> str:
    """Numero d'un joueur.

    Validation deleguee au module Contacts, comme dans Travail : le
    format (6 chiffres, plage 42xxxx) y est deja defini, et le dupliquer
    ici garantirait qu'un jour les deux divergent.
    """
    n = str(numero or "").strip()
    try:
        from circusvoip_phone_contacts import normalise_numero, ContactError
    except Exception:
        # Module absent (tests unitaires isoles) : repli minimal.
        if not (n.isdigit() and len(n) == 6):
            raise UrgenceError("Numéro invalide (6 chiffres attendus).")
        return n
    try:
        return normalise_numero(n)
    except ContactError as e:
        raise UrgenceError(str(e))


# ---------------------------------------------------------------------
#  Attribution des roles
# ---------------------------------------------------------------------
#
# Ces fonctions tournent COTE SERVEUR. Le client peut les appeler pour
# griser un bouton, mais la decision qui compte est celle du serveur :
# lui seul voit les fiches des deux joueurs.

def valide_attribution(role_chef, chef_est_chef, role_actuel_cible) -> str:
    """Un chef peut-il donner SON role a ce joueur ?

    `role_chef` est le role du chef, donc celui qu'il distribue : un
    chef medical ne fait que des medecins. Il n'y a pas de parametre
    "role a donner" -- l'introduire permettrait a un chef de nommer dans
    l'autre corps.
    """
    r = valide_role(role_chef)
    if not chef_est_chef:
        raise UrgenceError("Seul un chef peut attribuer ce rôle.")
    actuel = str(role_actuel_cible or "").strip().lower() or None
    if actuel == r:
        raise UrgenceError("Ce joueur a déjà ce rôle.")
    if actuel and not CUMUL_ROLES:
        # Refus, pas remplacement. Remplacer silencieusement retirerait
        # quelqu'un de l'autre equipe sans que son chef ni l'interesse
        # ne l'apprennent.
        raise UrgenceError(
            f"Ce joueur est déjà {LIBELLES_ROLE.get(actuel, actuel)}. "
            f"Il doit d'abord demander à son chef de le lui retirer.")
        # NB : le message NOMME l'autre role. C'est le seul endroit ou un
        # role "voit" l'autre, et c'est voulu -- sans lui, le joueur ne
        # saurait pas vers quel chef se tourner.
    return r


def valide_retrait(role_chef, chef_est_chef, role_actuel_cible) -> str:
    """Un chef retire son role a un joueur."""
    r = valide_role(role_chef)
    if not chef_est_chef:
        raise UrgenceError("Seul un chef peut retirer ce rôle.")
    actuel = str(role_actuel_cible or "").strip().lower() or None
    if actuel != r:
        raise UrgenceError("Ce joueur n'a pas ce rôle.")
    return r


def peut_nommer_chef(*_args, **_kwargs) -> bool:
    """Toujours False : un chef ne fabrique pas de chef.

    Ecrit comme une fonction plutot que laisse implicite pour que le
    refus soit trouvable a la lecture, et pour qu'un futur appelant
    tombe sur le commentaire plutot que sur l'absence de code. Les chefs
    sont designes depuis la console d'admin, uniquement.
    """
    return False


# ---------------------------------------------------------------------
#  Prise de service
# ---------------------------------------------------------------------

def est_en_service(role, est_chef, pointe) -> bool:
    """Ce joueur recoit-il les signaux ?

    Le chef est en service des qu'il est connecte : il est le filet de
    securite du dispositif. Les autres pointent.
    """
    if not role:
        return False
    if est_chef:
        return True
    return bool(pointe)


def valide_prise_service(role, est_chef) -> None:
    """Pointer suppose un role, et n'a pas de sens pour un chef."""
    valide_role(role)
    if est_chef:
        raise UrgenceError(
            "Un chef est de service dès qu'il est connecté.")


def peut_voir_liste(role) -> bool:
    """Hors service, on VOIT la liste sans etre notifie.

    Voir sans etre derange permet de rentrer en renfort quand ca chauffe.
    C'est la notification, pas la lecture, qui distingue les deux etats.
    """
    return bool(role)


# ---------------------------------------------------------------------
#  Signal
# ---------------------------------------------------------------------

def cree_signal(auteur_numero, type_urgence, position, texte="",
                maintenant=None) -> dict:
    """Fabrique un signal valide, ou leve UrgenceError.

    `position` vient de position_depuis_hierarchie(). None est accepte :
    une capture OCR peut echouer, et un signal sans position vaut mieux
    qu'un signal non emis -- le secouriste saura au moins que quelqu'un
    a besoin d'aide, quitte a demander par radio.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    return {
        "id":         uuid.uuid4().hex[:12],
        "type":       valide_type(type_urgence),
        "auteur":     valide_numero(auteur_numero),
        "texte":      valide_texte(texte),
        "position":   position or None,
        "cree_le":    ts,
        "etat":       ETAT_ACTIF,
        # Prise MULTIPLE : une equipe intervient a plusieurs. La liste
        # est nominative en interne mais affichee en COMPTE -- plus
        # lisible, et ca evite d'exposer des pseudos de connectes.
        "preneurs":   [],
        "clos_le":    None,
        "clos_par":   None,
    }


def est_expire(signal, maintenant=None) -> bool:
    ts = float(maintenant if maintenant is not None else time.time())
    return (ts - float(signal.get("cree_le") or 0.0)) >= EXPIRATION_S


def est_visible(signal, role_joueur, maintenant=None) -> bool:
    """Le signal apparait-il dans l'app de ce joueur ?

    Cloisonnement strict par type : un medecin ne voit pas les signaux
    de securite. La victime, elle, consulte son propre signal par
    est_le_sien() -- elle doit pouvoir suivre son etat, sinon un signal
    clos par un secouriste disparaitrait de son ecran sans explication.
    """
    if signal.get("etat") != ETAT_ACTIF:
        return False
    if est_expire(signal, maintenant):
        return False
    return ROLE_DESTINATAIRE.get(signal.get("type")) == role_joueur


def est_le_sien(signal, numero) -> bool:
    return str(signal.get("auteur") or "") == str(numero or "")


def destinataires(signal, fiches_en_service) -> list:
    """Numeros a notifier.

    `fiches_en_service` : iterable de (numero, role). Le filtrage est
    fait ici plutot que dans le serveur pour que client et serveur aient
    exactement la meme notion de "qui recoit" -- c'est le genre d'ecart
    qui ne produit aucune erreur, juste des signaux qui n'arrivent pas.
    """
    voulu = ROLE_DESTINATAIRE.get(signal.get("type"))
    return [str(n) for (n, r) in (fiches_en_service or []) if r == voulu]


# ---------------------------------------------------------------------
#  Transitions
# ---------------------------------------------------------------------
#
# Chacune leve UrgenceError avec un message montrable, et modifie le
# signal EN PLACE. Ecrites pour tourner COTE SERVEUR : lui seul voit
# tous les signaux et peut arbitrer.

def prendre(signal, numero, role, en_service, maintenant=None) -> dict:
    """Un secouriste s'annonce sur le signal.

    Prise MULTIPLE, contrairement aux missions de Travail : une equipe
    peut intervenir. La prise est informative -- elle dit aux autres que
    quelqu'un s'en occupe -- et n'exclut personne.

    Pointer est obligatoire : prendre depuis l'etat hors service
    reviendrait a vider le sens de la prise de service.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    n = valide_numero(numero)

    if signal.get("etat") != ETAT_ACTIF:
        raise UrgenceError("Ce signal n'est plus actif.")
    if est_expire(signal, ts):
        raise UrgenceError("Ce signal a expiré.")
    if ROLE_DESTINATAIRE.get(signal.get("type")) != role:
        raise UrgenceError("Ce signal ne concerne pas votre rôle.")
    if not en_service:
        raise UrgenceError(
            "Prenez votre service avant de répondre à un signal.")
    if est_le_sien(signal, n):
        raise UrgenceError("C'est votre propre signal.")
    if n in (signal.get("preneurs") or []):
        raise UrgenceError("Vous êtes déjà sur ce signal.")

    signal.setdefault("preneurs", []).append(n)
    return signal


def relacher(signal, numero) -> dict:
    """Un secouriste se retire sans terminer.

    Indispensable : sans lui, quelqu'un qui abandonne laisse le signal
    marque comme pris jusqu'a l'expiration, ce qui decourage les autres
    d'y aller.

    Silencieux si le joueur n'etait pas preneur -- c'est le cas d'un
    double clic ou d'une reprise apres reconnexion, pas une erreur a
    montrer.
    """
    n = valide_numero(numero)
    pris = signal.get("preneurs") or []
    if n in pris:
        pris.remove(n)
    signal["preneurs"] = pris
    return signal


def retirer_deconnecte(signal, numero) -> dict:
    """La deconnexion d'un preneur le retire de la prise.

    Meme regle que pour la victime : deconnecte = abandonne. Le signal,
    lui, reste visible -- c'est le secouriste qui disparait, pas le
    besoin.
    """
    return relacher(signal, numero)


def clore(signal, numero, maintenant=None) -> dict:
    """Termine le signal. VICTIME ou PRENEUR.

    Les deux, contrairement aux missions ou l'auteur seul termine. Ici
    la victime est peut-etre incapable d'agir -- c'est le cas d'usage --
    donc lui reserver la cloture laisserait des signaux ouverts une
    heure durant.

    L'ecran de la victime doit refleter l'etat reel du signal : sans ca,
    un signal clos par un secouriste disparaitrait de son telephone sans
    qu'elle sache si l'aide arrive ou pas.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    n = valide_numero(numero)
    if signal.get("etat") == ETAT_CLOS:
        raise UrgenceError("Ce signal est déjà terminé.")
    if not est_le_sien(signal, n) and n not in (signal.get("preneurs") or []):
        raise UrgenceError(
            "Seule la victime ou un secouriste sur place peut "
            "terminer ce signal.")

    signal["etat"] = ETAT_CLOS
    signal["clos_le"] = ts
    signal["clos_par"] = n
    return signal


# ---------------------------------------------------------------------
#  Position
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
#  Noms de planetes et de lunes
# ---------------------------------------------------------------------
#
# [URGENCE 12/08/2026] Seul endroit du dispositif ou un nom est TRADUIT.
# Ailleurs on affiche le nom brut de SC, pour ne rien inventer : les
# avant-postes, stations et champs d'asteroides n'ont pas de liste fiable
# et un nom maquille est indetectable.
#
# Les corps planetaires sont l'exception : leur liste est fixe, courte, et
# ne bouge qu'a l'ajout d'un systeme. Surtout, "OOc_Stanton_1_Hurston" ne
# se lit pas -- alors que c'est la destination que le secouriste doit
# selectionner dans son ordinateur de vol.
#
# Deux mecanismes, et aucun n'invente :
#
#   1. Stanton -- le nom reel est DEJA dans le container. On retire le
#      prefixe "ooc_<systeme>_<numero><lettre>_" et on garde le reste.
#      Aucune table a maintenir : une lune ajoutee par CIG marche seule.
#
#   2. Pyro -- les containers sont purement numeriques (pyro5e), donc
#      illisibles. La correspondance est reprise telle quelle des
#      commentaires de _KNOWN_ZONES_STATIONS, ou elle est deja
#      documentee ; on ne fait que la rendre exploitable.

_PLANETES = {
    # Pyro : noms internes numeriques, sans rapport avec l'affichage.
    "pyro1":  "Pyro I",
    "pyro2":  "Monox",
    "pyro3":  "Bloom",
    "pyro4":  "Pyro IV",
    "pyro5":  "Pyro V",
    "pyro6":  "Terminus",
    "pyro5a": "Ignis",
    "pyro5b": "Vatra",
    "pyro5c": "Adir",
    "pyro5d": "Fairo",
    "pyro5e": "Fuego",
    "pyro5f": "Vuur",
    # Formes courtes de Stanton, quand le HUD les affiche sans prefixe.
    "hurston":   "Hurston",
    "crusader":  "Crusader",
    "arccorp":   "ArcCorp",
    "microtech": "microTech",
    "delamar":   "Delamar",
}

# "ooc_stanton_2b_daymar" -> "daymar" ; "ooc_stanton_1_hurston" -> "hurston"
_RE_OOC = re.compile(r"^ooc_[a-z]+_\d+[a-z]?_(.+)$", re.IGNORECASE)

# Points de Lagrange : "ooc_stanton1_l1" -> "HUR-L1".
#
# [13/08/2026] C'est ainsi que la starmap les nomme : trois premieres
# lettres de la planete en majuscules, tiret, numero du point. Un joueur
# lit "HUR-L1" et sait ou aller ; "ooc_stanton1_l1" ne designe rien pour
# lui.
#
# Stanton UNIQUEMENT. Les autres systemes ne suivent pas cette
# convention, et rien ne dit qu'ils nomment leurs points de la meme
# facon -- traduire au jugé produirait un nom credible et faux.
_RE_LAGRANGE = re.compile(r"^ooc_stanton([1-9])_l([1-9])$", re.IGNORECASE)

# Numero de planete Stanton -> nom, dont on prend les trois premieres
# lettres. Table explicite : le numero n'est pas devinable depuis le nom.
_PLANETES_STANTON = {
    "1": "Hurston",
    "2": "Crusader",
    "3": "ArcCorp",
    "4": "microTech",
}


def nom_lagrange(zone_ou_nom):
    """Nom starmap d'un point de Lagrange de Stanton, ou None."""
    z = str(zone_ou_nom or "").strip().lower().replace(" ", "_")
    m = _RE_LAGRANGE.match(z)
    if not m:
        return None
    planete = _PLANETES_STANTON.get(m.group(1))
    if not planete:
        return None
    return f"{planete[:3].upper()}-L{m.group(2)}"


def nom_planete(zone_ou_nom):
    """Nom affichable d'un corps planetaire, ou None si ce n'en est pas un.

    None n'est pas un echec : c'est la reponse pour un vaisseau, un
    avant-poste ou un champ d'asteroides, dont le nom brut sera affiche
    tel quel.
    """
    z = str(zone_ou_nom or "").strip().lower().replace(" ", "_")
    if not z:
        return None
    lag = nom_lagrange(z)
    if lag:
        return lag
    if z in _PLANETES:
        return _PLANETES[z]
    m = _RE_OOC.match(z)
    if m:
        brut = m.group(1)
        # Le nom extrait peut lui-meme avoir une forme connue
        # ("microtech" -> "microTech"), sinon on capitalise.
        return _PLANETES.get(brut, brut.replace("_", " ").title())
    return None


def _est_grotte(entree) -> bool:
    """Le niveau est-il une grotte ?

    On delegue a _is_cave_container du module OCR, qui sert deja a
    declencher l'echo audio : les grottes y sont reconnues par les
    prefixes "rock01_" et "sand01_", avec la tolerance OCR sur o/0 et
    l/1. Redefinir le test ici garantirait qu'un jour les deux
    divergent, et qu'une grotte ait de l'echo sans etre nommee grotte.

    Repli sur False cote serveur, ou le module OCR est absent : le nom
    brut s'affiche alors, comme avant.
    """
    try:
        from circusvoip_sc_ocr import is_cave_container
    except Exception:
        return False
    try:
        return bool(is_cave_container(str((entree or {}).get("cid") or ""),
                                      str((entree or {}).get("name") or "")))
    except Exception:
        return False


# "ugf" = underground facility, le marqueur des bunkers. Observe sous
# plusieurs formes -- ugf_lta_, ugf_cor_, ugf_dls_ -- ou seul le prefixe
# est stable. On teste donc le segment "ugf" et rien d'autre.
_RE_BUNKER = re.compile(r"(?:^|[_\-])ugf(?:[_\-]|$)", re.IGNORECASE)


def _est_bunker(entree) -> bool:
    """Le niveau est-il un bunker ?

    Meme raisonnement que pour les grottes : le container ne designe
    rien sur la starmap, et les noms des differents bunkers ne sont pas
    connus -- mais "bunker" est en soi un renseignement utile, il dit
    qu'il faut descendre et que l'entree est au sol.

    Le test porte sur "ugf" comme SEGMENT, pas comme sous-chaine : sans
    la contrainte de separateurs, n'importe quel nom contenant ces trois
    lettres passerait pour un bunker.
    """
    for cle in ("cid", "name"):
        v = str((entree or {}).get(cle) or "")
        if v.startswith("name:"):
            v = v[5:]
        v = re.sub(r"[\s\-]+", "_", v)
        if _RE_BUNKER.search(v):
            return True
    return False


def _est_vaisseau(entree) -> bool:
    """Le niveau est-il un vaisseau ?

    Sert UNIQUEMENT a decider si la phrase se termine par "dans
    l'espace" : un vaisseau qui n'est contenu dans rien flotte, alors
    qu'un avant-poste ou un champ d'asteroides est une destination.

    La liste vit dans le module OCR, qui est absent du serveur -- d'ou
    le repli sur False. Un vaisseau non reconnu produit "sur
    <nom du vaisseau>", soit l'etat d'avant cette regle : pas de
    regression, et rien d'invente.
    """
    nom = str((entree or {}).get("name") or "").strip().lower()
    nom = nom.replace(" ", "_")
    if not nom:
        return False
    try:
        from circusvoip_sc_ocr import _KNOWN_ZONES_SHIPS
    except Exception:
        return False
    return nom in _KNOWN_ZONES_SHIPS


def nom_affiche(entree) -> str:
    """Nom d'un niveau tel qu'il apparait a l'ecran.

    Deux traductions seulement, et pour la meme raison : le nom brut
    n'est pas lisible ET la categorie est certaine.

      - corps planetaires -> leur vrai nom ;
      - grottes -> "Grotte", car elles n'ont PAS de vrai nom.
        "rock01_occu_001_size03_001_int" ne designe rien sur la
        starmap ; le seul renseignement utilisable est qu'il s'agit
        d'une grotte, et c'en est un vrai -- on n'y entre pas en
        vaisseau, et il faut descendre a pied.
      - bunkers -> "Bunker", meme raisonnement. Le marqueur "ugf"
        (underground facility) est stable la ou la suite du nom varie
        d'un bunker a l'autre.

    Tout le reste garde son nom brut. Le nom d'origine reste de toute
    facon dans la charge utile : seul l'affichage change.
    """
    if isinstance(entree, str):
        return nom_planete(entree) or entree
    if _est_grotte(entree):
        return "Grotte"
    if _est_bunker(entree):
        return "Bunker"
    nom = str((entree or {}).get("name") or "?")
    # La cle normalisee est plus fiable que le nom brut pour la
    # reconnaissance : c'est elle qui a traverse la canonicalisation.
    cle = str((entree or {}).get("cid") or "")
    if cle.startswith("name:"):
        cle = cle[5:]
    return nom_planete(cle) or nom_planete(nom) or nom


MESSAGE_MOUVEMENT = "Position relevée en déplacement — peut avoir bougé."


def position_depuis_hierarchie(hier, maintenant=None, lecture_simple=False):
    """Convertit le retour de sc_ocr.capture_hierarchy() en charge utile.

    On garde les coordonnees LOCALES de chaque niveau, et pas seulement
    celles du systeme.

    [URGENCE 12/08/2026] La premiere version ne transportait que
    SolarSystem, au motif que tout le monde le partage donc qu'une
    distance est toujours calculable. C'est vrai, mais SolarSystem est
    aussi le SEUL repere ou un joueur immobile se deplace : une session
    sur Hurston a montre des coordonnees locales figees au centimetre
    pendant que les coordonnees systeme derivaient de 600 m/s -- la
    planete orbite.

    Consequence : une position systeme figee vieillit de ~2 100 km en
    une heure, et un secouriste naviguerait vers du vide. Les
    coordonnees locales, elles, restent justes tant que la victime ne
    bouge pas -- ce qui est precisement le cas d'usage.

    Les noms sont transportes BRUTS pour l'affichage. La cle
    d'appariement, elle, est la forme normalisee : c'est elle qui doit
    coincider entre deux clients, et c'est deja ce que fait l'audio de
    proximite.
    """
    if not hier:
        return None
    sysc = hier.get("system") or {}
    if sysc.get("x") is None:
        # Sans coordonnees systeme, pas de repli possible entre deux
        # containers differents. On garde quand meme la chaine : savoir
        # OU aller vaut mieux que rien.
        sysc = None
    chaine = []
    for d in (hier.get("chain") or []):
        chaine.append({
            "name": str(d.get("name") or "?"),
            "cid":  d.get("container_id"),
            "x": d.get("x"), "y": d.get("y"), "z": d.get("z"),
        })
    return {
        "chain": chaine,
        "system": ({"x": float(sysc["x"]), "y": float(sysc["y"]),
                    "z": float(sysc["z"])} if sysc else None),
        "capture_le": float(maintenant if maintenant is not None
                            else time.time()),
        # [13/08/2026] Vrai quand la double lecture a echoue et qu'on a
        # accepte une lecture unique. C'est le cas d'un blesse qui bouge
        # encore -- il fuit, ou il rampe vers un abri. Refuser le signal
        # serait pire que le rendre approximatif : sans signal, personne
        # ne vient. Mais le secouriste doit le savoir, donc l'ecran le
        # dit au lieu de laisser croire a une position exacte.
        "lecture_simple": bool(lecture_simple),
    }


def noms_chaine(position) -> list:
    """Noms de la chaine, du plus proche au plus large, prets a afficher."""
    return [nom_affiche(d) for d in ((position or {}).get("chain") or [])]


def phrase_position(position) -> str:
    """Phrase lisible decrivant ou se trouve la victime.

    Forme : "depuis A, dans B, sur C", du plus PROCHE au plus LARGE. Le
    dernier maillon prend `sur` : c'est la destination de navigation.

    Aucune preposition ne depend de la NATURE d'un niveau intermediaire,
    seulement de sa position dans la chaine. Les releves du 12/08 ont
    montre qu'aucune regle de profondeur ne tient : Levski a un seul
    niveau intermediaire la ou un site minier en a deux.

    UNE exception, sur le dernier maillon seulement : s'il s'agit d'un
    vaisseau, il n'y a pas de destination -- un vaisseau que rien
    n'englobe flotte. La phrase se termine alors par "dans l'espace".

    [URGENCE 12/08/2026] La regle precedente disait "un seul maillon =
    dans l'espace". Elle etait fausse : un joueur a pied sur Hurston
    produit exactement un maillon, et il n'est pas dans l'espace. C'est
    la nature du dernier maillon qui tranche, pas leur nombre.
    """
    chaine = list((position or {}).get("chain") or [])
    if not chaine:
        return "Position inconnue."
    noms = [nom_affiche(d) for d in chaine]
    # L'avertissement d'injoignabilite est AJOUTE a la phrase, jamais
    # substitue : la destination reste utile puisque la victime peut
    # sortir de son hangar, et le secouriste doit deja etre en route.
    suffixe = f" {MESSAGE_INJOIGNABLE}" if est_injoignable(position) else ""
    espace = _est_vaisseau(chaine[-1])

    if len(noms) == 1:
        if espace:
            return f"Signal depuis {noms[0]}, dans l'espace.{suffixe}"
        return f"Signal sur {noms[0]}.{suffixe}"

    milieu = ", ".join(f"dans {n}" for n in noms[1:-1])
    milieu = (milieu + ", ") if milieu else ""
    if espace:
        return (f"Signal depuis {noms[0]}, {milieu}dans {noms[-1]}, "
                f"dans l'espace.{suffixe}")
    return f"Signal depuis {noms[0]}, {milieu}sur {noms[-1]}.{suffixe}"


# Containers dont le nom NE DESIGNE PAS un lieu unique.
#
# [URGENCE 13/08/2026] "ObjectContainer_Commercial" est partage par TOUTES
# les stations de Stanton -- Port Tressler, MIC L1, Everus Harbor, HUR L5,
# CRU L5 -- et les coordonnees y sont LOCALES a chaque station. Deux
# joueurs dans deux stations differentes portent donc le meme identifiant
# a quelques dizaines de metres l'un de l'autre.
#
# Apparier dessus donnerait une distance courte, credible et fausse : un
# secouriste a Everus Harbor lirait "victime a 30 m" alors qu'elle est en
# orbite d'une autre planete. On refuse l'appariement et on remonte au
# niveau au-dessus, ou le corps planetaire discrimine.
#
# circusvoip_mp_server.py connait le meme piege et s'en protege par un
# rayon serre. Cette voie n'est pas transposable ici : la proximite audio
# peut se tromper d'une salle sans consequence, un secouriste envoye sur
# la mauvaise station perd son intervention.
#
# NB : la forme A TIRET ("ObjectContainer-ugf_lta_a_0001_int",
# "ObjectContainer-lorville_cbd_int") est, elle, parfaitement
# discriminante -- c'est un nom de lieu precis. Seule la forme generique
# a underscore pose probleme.
_CONTAINERS_AMBIGUS = (
    "objectcontainer_commercial",
)


def _est_hangar(entree) -> bool:
    """Le niveau est-il un hangar ?

    [13/08/2026] TOUS les hangars de Star Citizen sont personnels : seul
    leur locataire y entre. Deux joueurs ne peuvent donc JAMAIS s'y
    trouver ensemble.

    Consequence pour l'appariement : un hangar n'est jamais un
    referentiel commun. Ce n'est pas une question de nom ambigu comme
    ObjectContainer_Commercial, c'est que la situation decrite est
    impossible. Sans ce filtre, deux joueurs chacun dans SON hangar
    pourraient etre apparies sur un identifiant reutilise et affiches a
    quelques metres l'un de l'autre.

    Consequence pour le signal : un blesse dans son hangar est
    INJOIGNABLE. Aucun secouriste ne peut entrer, quelle que soit la
    distance. L'app le dit franchement plutot que d'afficher une
    destination vers laquelle personne ne peut aller.

    Delegue a is_big_hangar du module OCR quand il est la, mais reconnait
    ici TOUTES les tailles : l'autre fonction ne retient que large et XL,
    parce qu'elle sert a la reverb -- un petit hangar est tout aussi
    ferme, il resonne juste moins.
    """
    for cle in ("cid", "name"):
        v = str((entree or {}).get(cle) or "")
        if v.startswith("name:"):
            v = v[5:]
        v = re.sub(r"[\s\-]+", "_", v).strip("_").lower()
        if v.startswith("hangar_"):
            return True
    return False


def est_injoignable(position) -> bool:
    """La victime est-elle dans un lieu ou personne ne peut la rejoindre ?

    Aujourd'hui : son hangar. Le test porte sur le maillon le PLUS
    PROFOND -- c'est celui ou elle se trouve. Un hangar plus haut dans la
    chaine n'aurait pas de sens (rien ne contient un hangar), mais si SC
    en produisait un un jour, mieux vaut ne pas conclure a tort.
    """
    chaine = (position or {}).get("chain") or []
    return bool(chaine) and _est_hangar(chaine[0])


MESSAGE_INJOIGNABLE = (
    "Hangar privé : aucun secouriste ne peut entrer.")


def _cle_container(entree) -> str:
    cle = str((entree or {}).get("cid") or "").strip().lower()
    if cle.startswith("name:"):
        cle = cle[5:]
    return cle


def _container_discriminant(entree) -> bool:
    """Ce niveau designe-t-il un lieu UNIQUE ?

    Un cid numerique est un identifiant d'instance emis par le serveur
    SC : toujours unique. Un repli `name:` vaut ce que vaut le nom, d'ou
    la liste des exceptions connues.
    """
    cid = str((entree or {}).get("cid") or "")
    if cid and not cid.startswith("name:"):
        return True
    return _cle_container(entree) not in _CONTAINERS_AMBIGUS


def phrase_technique(position) -> str:
    """Chaine des noms BRUTS, du plus proche au plus large.

    [13/08/2026] Complement de phrase_position(), pas remplacement.

    "Grotte" et "Bunker" disent au secouriste QUOI chercher -- une
    entree au sol, une descente a pied -- ce que "rock01_unoc_001_
    size02_001_int" ne dit a personne. Mais le nom technique reste la
    seule designation exacte du lieu : deux grottes voisines sur la meme
    lune se ressemblent, et c'est lui qui les distingue.

    Les deux sont donc affiches : la phrase pour chercher, le technique
    pour identifier. Aucune information n'est perdue au passage, et
    aucune n'est maquillee.

    Rend "" quand rien n'est traduit -- inutile de repeter la phrase.
    """
    chaine = list((position or {}).get("chain") or [])
    if not chaine:
        return ""
    bruts = [str(d.get("name") or "?") for d in chaine]
    if bruts == [nom_affiche(d) for d in chaine]:
        return ""
    return " › ".join(reversed(bruts))


def _memes_containers(a, b) -> bool:
    """Deux niveaux designent-ils le meme container ?

    Aucune lune ni planete de Star Citizen ne porte d'identifiant
    numerique : leur cid est toujours un repli `name:<zone>`, donc une
    chaine issue de l'OCR. L'appariement par nom n'est pas un
    compromis, c'est la seule voie -- et elle est deja eprouvee, l'audio
    de proximite l'utilise depuis des mois.

    On delegue a _are_containers_similar, qui tolere deux caracteres
    d'ecart : c'est ce qui evite qu'un `l` lu `1` separe deux joueurs
    reellement au meme endroit. Le module OCR est absent du serveur, d'ou
    le repli sur une egalite stricte -- plus severe, jamais faux.
    """
    ca, cb = a.get("cid"), b.get("cid")
    if not ca or not cb:
        return False
    if not (_container_discriminant(a) and _container_discriminant(b)):
        return False
    if _est_hangar(a) or _est_hangar(b):
        return False
    if ca == cb:
        return True
    try:
        from circusvoip_sc_ocr import are_containers_similar
    except Exception:
        return False
    try:
        return bool(are_containers_similar(str(ca), str(cb)))
    except Exception:
        return False


def _distance_locale(a, b):
    for axe in ("x", "y", "z"):
        if a.get(axe) is None or b.get(axe) is None:
            return None
    dx = float(a["x"]) - float(b["x"])
    dy = float(a["y"]) - float(b["y"])
    dz = float(a["z"]) - float(b["z"])
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def _est_orbital(entree) -> bool:
    """Ce niveau tourne-t-il autour de quelque chose ?

    nom_planete() reconnait les corps planetaires ET les points de
    Lagrange -- les deux orbitent, et c'est exactement ce qui fait
    deriver les coordonnees SolarSystem.
    """
    for cle in ("cid", "name"):
        v = str((entree or {}).get(cle) or "")
        if v.startswith("name:"):
            v = v[5:]
        if nom_planete(v):
            return True
    return False


def _chaine_orbitale(position) -> bool:
    """Un niveau orbital figure-t-il dans la chaine ?

    Teste la chaine ENTIERE, pas le dernier maillon : une grotte sur
    Hurston a Hurston au-dessus d'elle, un vaisseau a HUR-L1 a le point
    de Lagrange au-dessus. C'est la presence du niveau orbital qui
    compte, pas sa position.
    """
    return any(_est_orbital(d) for d in ((position or {}).get("chain") or []))


def distance_detail(position_a, position_b) -> dict:
    """Distance entre deux positions, et DANS QUEL REPERE.

    On cherche le container commun le plus PROFOND. Ses coordonnees
    locales sont stables : deux joueurs immobiles dans le meme hangar y
    gardent la meme distance indefiniment.

    A defaut, repli sur SolarSystem -- le seul repere que tous
    partagent, donc une distance toujours disponible. Mais c'est aussi
    celui ou tout derive : la position de la victime a ete figee au
    declenchement, et la planete sous elle a continue d'orbiter a
    ~600 m/s. `fiable` vaut alors False, et l'ecran doit le dire au lieu
    d'afficher un nombre qui inspire une confiance qu'il ne merite pas.

    Retour : {"distance", "repere", "fiable"}. distance None = aucun
    calcul possible.
    """
    vide = {"distance": None, "repere": None, "fiable": False}
    if not position_a or not position_b:
        return vide

    ca = (position_a or {}).get("chain") or []
    cb = (position_b or {}).get("chain") or []
    # Du plus profond au plus large : le premier niveau commun trouve
    # est le plus precis.
    for na in ca:
        for nb in cb:
            if _memes_containers(na, nb):
                d = _distance_locale(na, nb)
                if d is not None:
                    return {"distance": d,
                            "repere": str(na.get("name") or "?"),
                            "fiable": True}

    # [13/08/2026] Repli SolarSystem : SEULEMENT si aucune des deux
    # chaines ne contient un niveau orbital.
    #
    # Un joueur immobile sur Hurston garde des coordonnees locales figees
    # au centimetre pendant que ses coordonnees systeme derivent de
    # 600 m/s : c'est la planete qui tourne. Une position figee au
    # declenchement vieillit donc d'environ 2 100 km par heure, et le
    # secouriste volerait vers un point de vide.
    #
    # Observe le 13/08 : victime a HUR-L1, secouriste dans un hangar de
    # Lorville -- l'app affichait 1 289 497 km, un nombre credible et
    # denue de sens. Un point de Lagrange orbite avec sa planete, au meme
    # titre qu'elle.
    #
    # Les deux chaines sont testees : si le SECOURISTE est sur un astre,
    # ce sont ses propres coordonnees qui derivent, et l'ecart est tout
    # aussi faux.
    #
    # Reste le cas ou les deux flottent librement -- vaisseau en vol,
    # champ d'asteroides. La, rien n'orbite, la distance systeme est
    # juste, et c'est le SEUL repere disponible puisqu'il n'y a aucune
    # destination a selectionner.
    if _chaine_orbitale(position_a) or _chaine_orbitale(position_b):
        return vide

    sa = (position_a or {}).get("system")
    sb = (position_b or {}).get("system")
    if not sa or not sb:
        return vide
    try:
        dx = float(sa["x"]) - float(sb["x"])
        dy = float(sa["y"]) - float(sb["y"])
        dz = float(sa["z"]) - float(sb["z"])
    except Exception:
        return vide
    return {"distance": (dx * dx + dy * dy + dz * dz) ** 0.5,
            "repere": "SolarSystem", "fiable": False}


def distance_m(position_a, position_b):
    """Distance seule, en metres. None si incalculable.

    Raccourci sur distance_detail() pour les appelants qui n'ont pas
    besoin de savoir dans quel repere elle a ete obtenue.
    """
    return distance_detail(position_a, position_b).get("distance")


def delai_rafraichissement(distance) -> float:
    """Secondes a attendre avant de recalculer la distance.

    Varie continument entre RAFRAICHISSEMENT_MIN_S et
    RAFRAICHISSEMENT_MAX_S, en echelle LOGARITHMIQUE : entre 1 et 100 km
    la distance couvre deux ordres de grandeur, et une interpolation
    lineaire laisserait le delai colle au maximum sur presque toute la
    plage. Passer de 40 a 20 km compte autant que passer de 4 a 2.

    distance None -> delai maximum, jamais rien. Tant qu'aucun
    referentiel commun n'existe, la mesure ne peut rien rendre -- mais
    elle reste necessaire, plus lentement, pour DETECTER l'arrivee du
    secouriste dans le container de la victime.
    """
    if distance is None:
        return RAFRAICHISSEMENT_MAX_S
    try:
        d = float(distance)
    except Exception:
        return RAFRAICHISSEMENT_MAX_S
    if d <= DISTANCE_PROCHE_M:
        return RAFRAICHISSEMENT_MIN_S
    if d >= DISTANCE_LOIN_M:
        return RAFRAICHISSEMENT_MAX_S
    import math
    t = ((math.log10(d) - math.log10(DISTANCE_PROCHE_M))
         / (math.log10(DISTANCE_LOIN_M) - math.log10(DISTANCE_PROCHE_M)))
    return RAFRAICHISSEMENT_MIN_S + t * (RAFRAICHISSEMENT_MAX_S
                                         - RAFRAICHISSEMENT_MIN_S)


def variation_suspecte(distance_avant, distance_apres, dt) -> bool:
    """La variation est-elle trop grande pour etre un deplacement ?

    Garde anti-saut. Un chiffre mal lu sur les dix de la position
    systeme deplace la victime de milliers de kilometres en restant
    parfaitement plausible ; comparer a une vitesse maximale est le seul
    moyen de le voir. Au-dela, on garde la valeur precedente plutot que
    d'afficher un bond.
    """
    try:
        if distance_avant is None or distance_apres is None:
            return False
        dt = float(dt or 0.0)
        if dt <= 0:
            return False
        return abs(float(distance_apres) - float(distance_avant)) / dt \
            > VITESSE_MAX_M_S
    except Exception:
        return False


def tendance(distance_avant, distance_apres, dt=None) -> str:
    """"rapprochement" / "eloignement" / "stable" / "inconnue".

    La tendance dit au secouriste s'il va dans le bon sens, ce qu'une
    distance seule ne dit pas -- surtout a 10 minutes d'intervalle en
    voyage quantique.
    """
    if distance_avant is None or distance_apres is None:
        return "inconnue"
    if dt is not None and variation_suspecte(distance_avant,
                                             distance_apres, dt):
        return "inconnue"
    d = float(distance_apres) - float(distance_avant)
    # 1 % de la distance courante : un seuil absolu serait soit du bruit
    # a l'echelle du systeme, soit insensible en approche.
    seuil = max(50.0, abs(float(distance_avant)) * 0.01)
    if d < -seuil:
        return "rapprochement"
    if d > seuil:
        return "eloignement"
    return "stable"


# ---------------------------------------------------------------------
#  Affichage
# ---------------------------------------------------------------------

def distance_texte(distance) -> str:
    """Distance lisible. La precision suit l'echelle, pas l'inverse.

    A 12 millions de kilometres, le metre pres n'informe personne ; a
    200 m, il decide de la direction ou regarder.
    """
    if distance is None:
        return "Distance inconnue"
    d = float(distance)
    if d < 1000:
        return f"{int(round(d))} m"
    if d < 1_000_000:
        return f"{d / 1000:.1f} km".replace(".", ",")
    return f"{d / 1000:,.0f} km".replace(",", " ")


def age_texte(cree_le, maintenant=None) -> str:
    """"il y a X". Volontairement imprecis, sauf au debut.

    Les minutes comptent ici, contrairement a une annonce de Travail :
    la position est figee au declenchement, donc l'age dit a quel point
    elle a vieilli. C'est l'information qui evite de partir vers un
    point ou la victime n'est plus.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    d = max(0.0, ts - float(cree_le or 0.0))
    if d < 60:
        return "à l'instant"
    if d < 3600:
        n = int(d // 60)
        return f"il y a {n} min"
    n = int(d // 3600)
    return f"il y a {n} h"


def preneurs_texte(signal) -> str:
    """Etat de prise en charge, en COMPTE et non en noms.

    Plus lisible qu'une liste de pseudos, et ca evite d'exposer qui est
    connecte. Le detail se coordonne a la voix, pas dans l'app.
    """
    n = len(signal.get("preneurs") or [])
    if n == 0:
        return "Personne n'a répondu"
    if n == 1:
        return "1 secouriste en route"
    return f"{n} secouristes en route"


def libelle_role(role) -> str:
    return LIBELLES_ROLE.get(str(role or "").lower(), str(role or ""))


def libelle_type(type_urgence) -> str:
    return LIBELLES_TYPE.get(str(type_urgence or "").lower(),
                             str(type_urgence or ""))
