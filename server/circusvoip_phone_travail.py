#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[TRAVAIL 10/08/2026] Modele metier de l'app Travail du CircusPhone.

Ce fichier ne connait NI le reseau, NI Qt. Il contient les regles, et
elles seules : ce qu'est une mission, ce qui la rend valide, et quelles
transitions d'etat sont permises. Le serveur l'importe pour arbitrer, le
client pour valider une saisie avant de l'envoyer.

--- Le metier d'une mission est celui qu'on CHERCHE ---

C'est le point le plus contre-intuitif du modele, et celui qu'il ne faut
pas se tromper en relisant : `metier` n'est PAS le metier de l'auteur,
c'est celui de la personne recherchee. Un mineur publie une mission
`mercenaire` quand il cherche une escorte, ou `transporteur` pour faire
deplacer ses minerais.

C'est ce qui fait tourner le jeu de role : chacun a besoin des autres. Un
modele ou la mission porterait le metier de l'auteur produirait
exactement l'inverse -- des mineurs parlant a des mineurs.

--- Une seule mission en cours par joueur ---

Pas une liste : un ETAT. Un joueur a une mission ou n'en a pas. C'est ce
qui permet de l'afficher dans un bandeau permanent plutot que dans une
quatrieme page, et ce qui evite qu'un joueur reserve dix missions qu'il
ne fera jamais.

--- Le serveur arbitre, toujours ---

prendre() est ecrit pour etre appele COTE SERVEUR uniquement. Deux
joueurs peuvent cliquer sur la meme mission dans la meme seconde : sans
un arbitre unique, les deux croiraient l'avoir prise. Le client ne retire
donc jamais une mission de sa propre liste -- il attend la confirmation.

--- Expiration ---

30 jours depuis la CREATION. La decision initiale etait "pas
d'expiration", revue le jour meme : sans purge, une mission abandonnee en
silence -- l'auteur ne joue plus, l'executant a oublie -- reste
indefiniment. Sur un serveur de 100 joueurs, l'onglet devient illisible
en quelques mois, et il est alors trop tard pour purger sans casser des
choses.
"""

from __future__ import annotations

import time
import uuid

# ---------------------------------------------------------------------
#  Metiers
# ---------------------------------------------------------------------

# Liste FERMEE, volontairement. Un metier libre au clavier produirait
# "Mineur", "mineur", "MINEUR" et "mineur ", donc quatre populations qui
# ne se voient pas entre elles -- et l'app Urgence, plus tard, ne saurait
# plus qui appeler.
#
# Ordre alphabetique : c'est celui de l'affichage, et le garder ici evite
# qu'un ecran le retrie de son cote (une donnee triee a deux endroits
# finit triee differemment).
METIERS = (
    "artisan",
    "ferrailleur",
    "mecanicien",
    "mercenaire",
    "mineur",
    "pilote",
    "ravitailleur",
    "transporteur",
)

# Libelles affiches. Separes des identifiants parce que les identifiants
# voyagent sur le reseau et sont stockes : les accentuer les rendrait
# fragiles au premier probleme d'encodage, et impossibles a renommer sans
# migration.
LIBELLES = {
    "artisan":     "Artisan",
    "ferrailleur": "Ferrailleur",
    "mecanicien":  "Mécanicien",
    "mercenaire":  "Mercenaire",
    "mineur":      "Mineur",
    "pilote":      "Pilote",
    # Libelle long : "Ravitailleur en carburant" deborderait d'un bouton de
    # demi-largeur. Le metier est le ravitaillement, le carburant en est
    # l'objet -- le raccourci ne perd rien.
    "ravitailleur": "Ravitailleur",
    "transporteur": "Transporteur",
}

# Deux metiers maximum par joueur.
#
# Sans plafond, tout le monde finirait par tout etre, et l'app Urgence --
# "appeler un medecin" -- perdrait tout sens : n'importe qui repondrait a
# n'importe quoi. Deux laisse la souplesse (un mineur-pilote, un
# ingenieur-artisan) sans vider le mecanisme.
METIERS_MAX = 2

# ---------------------------------------------------------------------
#  Bornes de saisie
# ---------------------------------------------------------------------

# L'ecran du telephone est etroit : ces maximums sont des contraintes
# d'AFFICHAGE avant d'etre des contraintes de donnees.
TITRE_MIN_LEN = 3
TITRE_MAX_LEN = 40
DESCRIPTION_MAX_LEN = 300

# Paiement : TEXTE LIBRE, pas un nombre.
#
# Un montant en aUEC ne couvre pas ce qui se negocie reellement en jeu de
# role : "500k + carburant", "part du butin", "a discuter", "un aller
# simple vers Microtech". Imposer un entier obligerait a mentir dans la
# description a chaque fois que le paiement n'est pas une somme.
#
# Le prix de ce choix est assume : on ne pourra ni trier ni filtrer par
# montant, ni totaliser quoi que ce soit. C'est acceptable -- l'app est un
# tableau d'annonces, pas une comptabilite, et elle n'a aucun lien avec le
# Portefeuille.
PAIEMENT_MAX_LEN = 40

# Plafond de missions ouvertes par auteur.
#
# Sans lui, un seul joueur peut noyer la liste des 99 autres -- sans
# malveillance, juste en publiant a chaque idee. Les missions PRISES ne
# comptent pas : elles ne sont plus visibles dans la liste, donc elles
# n'encombrent personne.
MISSIONS_OUVERTES_MAX = 5

# Duree de vie depuis la creation.
EXPIRATION_S = 30 * 24 * 3600

# ---------------------------------------------------------------------
#  Etats
# ---------------------------------------------------------------------

ETAT_OUVERTE = "ouverte"   # visible, personne ne l'a prise
ETAT_PRISE   = "prise"     # un executant l'a acceptee
ETAT_CLOSE   = "close"     # terminee, conservee pour l'historique

_ETATS = (ETAT_OUVERTE, ETAT_PRISE, ETAT_CLOSE)


class TravailError(Exception):
    """Erreur metier, destinee a etre MONTREE au joueur.

    Le message doit donc etre lisible et actionnable : il finira dans une
    bulle d'erreur du telephone, pas dans un log.
    """


# ---------------------------------------------------------------------
#  Validation
# ---------------------------------------------------------------------

def valide_metier(metier) -> str:
    """Retourne l'identifiant de metier normalise, ou leve TravailError."""
    m = str(metier or "").strip().lower()
    if m not in METIERS:
        raise TravailError("Métier inconnu.")
    return m


def valide_metiers_joueur(metiers) -> list[str]:
    """Normalise la liste de metiers d'un joueur.

    Deduplique et TRIE : deux clients qui envoient les memes metiers dans
    un ordre different doivent produire la meme fiche, sinon comparer
    deux etats devient impossible.
    """
    if metiers is None:
        return []
    if isinstance(metiers, str):
        metiers = [metiers]
    vus = []
    for m in metiers:
        mm = valide_metier(m)
        if mm not in vus:
            vus.append(mm)
    if len(vus) > METIERS_MAX:
        raise TravailError(
            f"{METIERS_MAX} métiers maximum. Décochez-en un d'abord.")
    return sorted(vus)


def valide_titre(titre) -> str:
    t = " ".join(str(titre or "").split())
    if len(t) < TITRE_MIN_LEN:
        raise TravailError(
            f"Le titre doit faire au moins {TITRE_MIN_LEN} caractères.")
    if len(t) > TITRE_MAX_LEN:
        raise TravailError(
            f"Le titre est limité à {TITRE_MAX_LEN} caractères.")
    return t


def valide_description(description) -> str:
    d = str(description or "").strip()
    if len(d) > DESCRIPTION_MAX_LEN:
        raise TravailError(
            f"La description est limitée à {DESCRIPTION_MAX_LEN} caractères.")
    return d


def valide_paiement(paiement) -> str:
    """Paiement en TEXTE LIBRE. Vide permis.

    On ne verifie que la longueur : le contenu est du langage, et le
    contraindre reviendrait a refuser des propositions parfaitement
    valides en jeu de role. Un champ vide vaut "non precise", ce qui est
    une information en soi -- pas une erreur de saisie.
    """
    p = " ".join(str(paiement or "").split())
    if len(p) > PAIEMENT_MAX_LEN:
        raise TravailError(
            f"Le paiement est limité à {PAIEMENT_MAX_LEN} caractères.")
    return p


def valide_numero(numero) -> str:
    """Numero de contact affiche sur l'annonce.

    La validation est deleguee au module Contacts : le format des numeros
    (6 chiffres, plage 42xxxx) y est deja defini, et le dupliquer ici
    garantirait qu'un jour les deux divergent.
    """
    n = str(numero or "").strip()
    try:
        from circusvoip_phone_contacts import normalise_numero, ContactError
    except Exception:
        # Module absent (tests unitaires isoles) : repli minimal.
        if not (n.isdigit() and len(n) == 6):
            raise TravailError("Numéro invalide (6 chiffres attendus).")
        return n
    try:
        return normalise_numero(n)
    except ContactError as e:
        raise TravailError(str(e))


# ---------------------------------------------------------------------
#  Mission
# ---------------------------------------------------------------------

def cree_mission(auteur_numero, metier, titre, paiement,
                 description="", maintenant=None) -> dict:
    """Fabrique une mission valide, ou leve TravailError.

    `metier` est le metier RECHERCHE (cf. en-tete du module).
    `auteur_numero` sert a la fois d'identite de l'auteur et de contact
    affiche : le joueur n'a pas a saisir deux fois la meme chose, et ca
    empeche de publier sous le numero de quelqu'un d'autre.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    return {
        "id":          uuid.uuid4().hex[:12],
        "metier":      valide_metier(metier),
        "titre":       valide_titre(titre),
        "description": valide_description(description),
        "paiement":    valide_paiement(paiement),
        "auteur":      valide_numero(auteur_numero),
        "cree_le":     ts,
        "etat":        ETAT_OUVERTE,
        "executant":   None,
        "pris_le":     None,
        "clos_le":     None,
    }


def est_expiree(mission, maintenant=None) -> bool:
    ts = float(maintenant if maintenant is not None else time.time())
    return (ts - float(mission.get("cree_le") or 0.0)) >= EXPIRATION_S


def est_visible(mission, metiers_joueur, maintenant=None) -> bool:
    """La mission apparait-elle dans l'onglet Missions de ce joueur ?

    Trois conditions : ouverte, non expiree, et destinee a l'un de ses
    metiers. L'auteur voit sa propre mission ici comme les autres -- la
    masquer donnerait l'impression qu'elle n'a pas ete publiee.
    """
    if mission.get("etat") != ETAT_OUVERTE:
        return False
    if est_expiree(mission, maintenant):
        return False
    return mission.get("metier") in (metiers_joueur or [])


def peut_publier(missions, auteur_numero, maintenant=None) -> bool:
    """Le plafond de missions OUVERTES est-il atteint pour cet auteur ?

    Les missions prises ou closes ne comptent pas : elles ne sont plus
    dans la liste, donc elles n'encombrent personne.
    """
    n = str(auteur_numero)
    ouvertes = [m for m in missions
                if m.get("auteur") == n
                and m.get("etat") == ETAT_OUVERTE
                and not est_expiree(m, maintenant)]
    return len(ouvertes) < MISSIONS_OUVERTES_MAX


# ---------------------------------------------------------------------
#  Transitions
# ---------------------------------------------------------------------
#
# Chacune leve TravailError avec un message montrable au joueur, et
# modifie la mission EN PLACE. Elles sont ecrites pour tourner cote
# SERVEUR : lui seul voit toutes les missions et peut donc arbitrer une
# course entre deux preneurs.

def prendre(mission, executant_numero, mission_en_cours=None,
            maintenant=None) -> dict:
    """Un executant accepte la mission.

    `mission_en_cours` est la mission que ce joueur a deja, ou None. Le
    passer explicitement plutot que d'aller la chercher garde ce module
    sans etat : c'est l'appelant qui sait ou vivent les missions.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    n = valide_numero(executant_numero)

    if mission.get("etat") == ETAT_PRISE:
        # Course entre deux preneurs : le message doit dire ce qui s'est
        # passe, pas "erreur". Le joueur n'a rien fait de mal.
        raise TravailError("Quelqu'un vient de prendre cette mission.")
    if mission.get("etat") != ETAT_OUVERTE:
        raise TravailError("Cette mission n'est plus disponible.")
    if est_expiree(mission, ts):
        raise TravailError("Cette mission a expiré.")
    if mission.get("auteur") == n:
        raise TravailError("Vous ne pouvez pas prendre votre propre mission.")
    if mission_en_cours is not None:
        raise TravailError(
            "Vous avez déjà une mission en cours. Terminez-la ou "
            "abandonnez-la d'abord.")

    mission["etat"] = ETAT_PRISE
    mission["executant"] = n
    mission["pris_le"] = ts
    return mission


def abandonner(mission, executant_numero) -> dict:
    """L'executant rend la mission : elle redevient disponible.

    L'auteur en est notifie par l'appelant -- une mission qu'on croyait
    prise en charge et qui ne l'est plus est une information dont il a
    besoin, sinon il attend quelqu'un qui ne viendra pas.
    """
    n = valide_numero(executant_numero)
    if mission.get("etat") != ETAT_PRISE:
        raise TravailError("Cette mission n'est pas en cours.")
    if mission.get("executant") != n:
        raise TravailError("Cette mission n'est pas la vôtre.")

    mission["etat"] = ETAT_OUVERTE
    mission["executant"] = None
    mission["pris_le"] = None
    return mission


def clore(mission, numero, maintenant=None) -> dict:
    """Termine la mission. AUTEUR SEUL.

    [TRAVAIL 11/08/2026] Restreint a l'auteur. C'est lui qui a demande le
    travail, donc lui seul qui peut dire qu'il est fait -- un executant
    pouvant clore signerait sa propre livraison.

    L'objection evidente serait le blocage : un auteur qui ne se
    reconnecte jamais laisserait l'executant avec une mission eternelle,
    donc incapable d'en prendre une autre. Elle ne tient pas, parce que
    abandonner() existe et n'appartient qu'a l'executant : il se libere
    quand il veut. Chacun garde la main sur ce qui le concerne.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    n = valide_numero(numero)
    if mission.get("etat") == ETAT_CLOSE:
        raise TravailError("Cette mission est déjà terminée.")
    if n != mission.get("auteur"):
        raise TravailError(
            "Seul l'auteur de la mission peut la terminer.")

    mission["etat"] = ETAT_CLOSE
    mission["clos_le"] = ts
    return mission


def retirer(mission, auteur_numero) -> dict:
    """L'auteur retire sa mission avant que quelqu'un ne la prenne.

    Distinct de clore() : rien n'a eu lieu. Interdit une fois la mission
    prise -- sinon l'auteur pourrait annuler sous les pieds de quelqu'un
    qui est deja en route.
    """
    n = valide_numero(auteur_numero)
    if mission.get("auteur") != n:
        raise TravailError("Cette mission n'est pas la vôtre.")
    if mission.get("etat") == ETAT_PRISE:
        raise TravailError(
            "Quelqu'un a pris cette mission. Vous pouvez la terminer, "
            "pas la retirer.")
    if mission.get("etat") == ETAT_CLOSE:
        raise TravailError("Cette mission est déjà terminée.")

    mission["etat"] = ETAT_CLOSE
    mission["clos_le"] = time.time()
    return mission


# ---------------------------------------------------------------------
#  Affichage
# ---------------------------------------------------------------------

def age_texte(cree_le, maintenant=None) -> str:
    """"Posté il y a X" : minutes, puis heures, puis jours.

    Volontairement imprecis. Sur une annonce, savoir s'il s'agit de 3 ou
    4 heures ne change rien a la decision de repondre ; l'ordre de
    grandeur, si. Une date exacte demanderait en plus de gerer les
    fuseaux entre joueurs.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    d = max(0.0, ts - float(cree_le or 0.0))
    if d < 60:
        return "à l'instant"
    if d < 3600:
        n = int(d // 60)
        return f"il y a {n} min"
    if d < 86400:
        n = int(d // 3600)
        return f"il y a {n} h"
    n = int(d // 86400)
    return f"il y a {n} jour" + ("s" if n > 1 else "")


def paiement_texte(paiement) -> str:
    """Paiement pour l'affichage. Vide -> mention explicite.

    Laisser la ligne vide donnerait l'impression d'une annonce
    incomplete ; "À négocier" dit que l'auteur n'a pas oublie le champ,
    il ne l'a pas fixe.
    """
    p = str(paiement or "").strip()
    return p if p else "À négocier"


def libelle_metier(metier) -> str:
    return LIBELLES.get(str(metier or "").lower(), str(metier or ""))
