#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[ANNONCES 21/09/2026] Modele metier de l'app Annonces du CircusPhone.

Ce fichier ne connait NI le reseau, NI Qt. Il contient les regles, et
elles seules. Le serveur l'importe pour arbitrer, le client pour valider
une saisie avant de l'envoyer.

>>> IL VA DES DEUX COTES. <<<
Comme circusvoip_phone_travail.py et circusvoip_phone_urgence.py. Ne le
pousser que d'un cote fait diverger client et serveur SANS erreur
visible : l'un accepterait ce que l'autre refuse.

--- Une annonce n'est pas une mission ---

L'app Travail publie des MISSIONS : elles visent UN metier, et elles ont
un cycle prendre -> abandonner -> clore avec un seul executant. Une
annonce est PUBLIQUE et personne ne la "prend" : on la lit, on appelle si
elle interesse. D'ou une app separee plutot qu'une categorie de plus dans
Travail -- greffer l'un sur l'autre aurait complique les deux.

Cycle : publier, retirer (auteur ou admin), expirer. Rien d'autre.

--- Le numero : une option, cochee par defaut ---

Regle du projet : les numeros s'echangent EN JEU, l'annuaire du serveur
reste reserve a l'administration. Une annonce publique qui porte le
numero de son auteur le montre a tout le serveur.

Decision du 21/09/2026 : c'est acceptable, parce que publier une petite
annonce, c'est CONSENTIR a donner son contact. La regle protege contre
la diffusion d'un numero SANS accord ; ici l'auteur le donne lui-meme, et
une case decochable le lui rappelle a chaque publication. Ecrit ici pour
que la question ne ressorte pas dans six mois comme une faille.

Case decochee : l'annonce ne porte AUCUN contact (un evenement "RDV a
Lorville 21 h" n'en a pas besoin). Le serveur garde l'auteur pour le
plafond, le retrait et la moderation, mais ne l'envoie JAMAIS aux autres
joueurs -- sinon la case ne protegerait rien.

--- Un seul champ de saisie ---

Categorie + description. Pas de titre, pas de prix, pas de date
(decision du 21/09/2026) : ce qui compte tient dans le texte, et chaque
champ de plus est un champ de plus a remplir au clavier en jeu.
"""

from __future__ import annotations

import time
import uuid

# ---------------------------------------------------------------------
#  Categories
# ---------------------------------------------------------------------

# Liste FERMEE. Une categorie libre produirait "Vente", "vente", "VENTE"
# -- trois filtres qui ne se voient pas entre eux.
CAT_RECRUTEMENT = "recrutement"
CAT_EVENEMENT   = "evenement"
CAT_VENTE       = "vente"

CATEGORIES = (CAT_RECRUTEMENT, CAT_EVENEMENT, CAT_VENTE)

# Libelles separes des identifiants : les identifiants voyagent et sont
# stockes, les accentuer les rendrait fragiles et impossibles a renommer
# sans migration.
LIBELLES = {
    CAT_RECRUTEMENT: "Recrutement",
    CAT_EVENEMENT:   "Événement",
    CAT_VENTE:       "Vente / Achat",
}

# ---------------------------------------------------------------------
#  Bornes
# ---------------------------------------------------------------------

# Contraintes d'AFFICHAGE avant d'etre des contraintes de donnees :
# l'ecran du telephone est etroit.
DESCRIPTION_MIN_LEN = 10
DESCRIPTION_MAX_LEN = 300
# Au-dela, une seule annonce occupe tout l'ecran a coups de lignes
# vides, et le joueur ne voit plus qu'elle.
DESCRIPTION_MAX_LIGNES = 6

# Plafond par auteur. Plus bas que Travail (5) : une annonce est vue par
# TOUT LE MONDE, pas par un metier. Un joueur qui en publie cinq occupe
# une part du tableau de chacun des autres.
ANNONCES_PAR_AUTEUR_MAX = 3

# Duree de vie. 7 jours et non 30 comme Travail : un evenement est
# date, une vente se perime vite. Une annonce de trois semaines a toutes
# les chances de ne plus etre d'actualite.
EXPIRATION_S = 7 * 24 * 3600

# Intervalle minimal entre deux publications d'un meme auteur.
#
# Le plafond seul ne suffit pas : publier / retirer en boucle ne le
# depasse jamais, mais repousse l'annonce en tete et redessine l'ecran
# de tous les joueurs a chaque tour.
PUBLICATION_INTERVALLE_S = 30


class AnnonceError(Exception):
    """Erreur metier, destinee a etre MONTREE au joueur.

    Le message finit dans une bulle du telephone : il doit etre lisible
    et dire quoi faire.
    """


# ---------------------------------------------------------------------
#  Validation
# ---------------------------------------------------------------------

def valide_categorie(categorie) -> str:
    c = str(categorie or "").strip().lower()
    if c not in CATEGORIES:
        raise AnnonceError("Catégorie inconnue.")
    return c


def valide_description(description) -> str:
    """Normalise et valide le texte.

    Espaces de fin de ligne retires, lignes vides consecutives reduites a
    une seule : elles ne portent rien et allongent la carte.
    """
    brut = str(description or "").replace("\r\n", "\n").replace("\r", "\n")
    lignes = [l.rstrip() for l in brut.split("\n")]
    propres = []
    for l in lignes:
        if not l.strip() and propres and not propres[-1].strip():
            continue
        propres.append(l)
    d = "\n".join(propres).strip()
    if len(d) < DESCRIPTION_MIN_LEN:
        raise AnnonceError(
            f"L'annonce doit faire au moins {DESCRIPTION_MIN_LEN} "
            f"caractères.")
    if len(d) > DESCRIPTION_MAX_LEN:
        raise AnnonceError(
            f"L'annonce est limitée à {DESCRIPTION_MAX_LEN} caractères.")
    if d.count("\n") + 1 > DESCRIPTION_MAX_LIGNES:
        raise AnnonceError(
            f"L'annonce est limitée à {DESCRIPTION_MAX_LIGNES} lignes.")
    return d


def valide_numero(numero) -> str:
    """Numero de l'auteur. Delegue au module Contacts, qui definit le
    format -- le dupliquer ici garantirait qu'un jour les deux divergent.
    """
    n = str(numero or "").strip()
    try:
        from circusvoip_phone_contacts import normalise_numero, ContactError
    except Exception:
        if not (n.isdigit() and len(n) == 6):
            raise AnnonceError("Numéro invalide (6 chiffres attendus).")
        return n
    try:
        return normalise_numero(n)
    except ContactError as e:
        raise AnnonceError(str(e))


# ---------------------------------------------------------------------
#  Annonce
# ---------------------------------------------------------------------

def cree_annonce(auteur_numero, categorie, description, avec_numero=True,
                 maintenant=None) -> dict:
    """Fabrique une annonce valide, ou leve AnnonceError.

    `auteur` est TOUJOURS enregistre, meme case decochee : il sert au
    plafond, au retrait et a la moderation. C'est `avec_numero` qui dit
    s'il peut sortir du serveur (cf. vue_publique).
    """
    ts = float(maintenant if maintenant is not None else time.time())
    return {
        "id":          uuid.uuid4().hex[:12],
        "categorie":   valide_categorie(categorie),
        "description": valide_description(description),
        "auteur":      valide_numero(auteur_numero),
        "avec_numero": bool(avec_numero),
        "cree_le":     ts,
    }


def est_expiree(annonce, maintenant=None) -> bool:
    ts = float(maintenant if maintenant is not None else time.time())
    return (ts - float(annonce.get("cree_le") or 0.0)) >= EXPIRATION_S


def compte_auteur(annonces, auteur_numero, maintenant=None) -> int:
    n = str(auteur_numero)
    return sum(1 for a in annonces
               if a.get("auteur") == n and not est_expiree(a, maintenant))


def peut_publier(annonces, auteur_numero, maintenant=None) -> bool:
    return compte_auteur(annonces, auteur_numero,
                         maintenant) < ANNONCES_PAR_AUTEUR_MAX


def peut_retirer(annonce, numero) -> None:
    """Leve AnnonceError si ce joueur n'est pas l'auteur.

    La moderation ne passe PAS par ici : l'admin a son propre chemin
    cote serveur, authentifie autrement.
    """
    if annonce.get("auteur") != str(numero or ""):
        raise AnnonceError("Cette annonce n'est pas la vôtre.")


def vue_publique(annonce, pour_numero, officiel="") -> dict:
    """Ce qu'un joueur recoit d'une annonce. Le SEUL format qui sort.

    L'auteur n'y figure JAMAIS en tant que tel :
      - `contact` porte son numero UNIQUEMENT si la case etait cochee ;
      - `mien` dit au demandeur s'il en est l'auteur, sans rien reveler
        aux autres.
    Envoyer `auteur` et laisser le client masquer ferait de la case une
    decoration : n'importe quel client modifie lirait le numero.

    `officiel` : libelle du badge (ex. "Chef médical"), calcule par le
    serveur AU MOMENT de l'envoi. Pas stocke dans l'annonce : un chef
    destitue perdrait sinon son titre partout sauf sur ses annonces,
    pendant sept jours.
    """
    return {
        "id":          annonce.get("id"),
        "categorie":   annonce.get("categorie"),
        "description": annonce.get("description"),
        "cree_le":     annonce.get("cree_le"),
        "contact":     (annonce.get("auteur")
                        if annonce.get("avec_numero") else None),
        "mien":        annonce.get("auteur") == str(pour_numero or ""),
        "officiel":    str(officiel or ""),
    }


def badge_officiel(annonce, chef_role_de_auteur, libelles_chef) -> str:
    """Libelle du badge, ou "".

    Seulement en RECRUTEMENT : c'est la qu'il dit quelque chose ("cette
    offre vient bien du chef medical"). Un chef qui vend son casque n'a
    pas a porter son titre -- le badge laisserait croire a une annonce
    de service.
    """
    if annonce.get("categorie") != CAT_RECRUTEMENT or not chef_role_de_auteur:
        return ""
    return str((libelles_chef or {}).get(chef_role_de_auteur, "") or "")


# ---------------------------------------------------------------------
#  Affichage
# ---------------------------------------------------------------------

def libelle_categorie(categorie) -> str:
    return LIBELLES.get(str(categorie or "").lower(), str(categorie or ""))


def age_texte(cree_le, maintenant=None) -> str:
    """"il y a X", volontairement imprecis -- meme texte que Travail."""
    ts = float(maintenant if maintenant is not None else time.time())
    d = max(0.0, ts - float(cree_le or 0.0))
    if d < 60:
        return "à l'instant"
    if d < 3600:
        return f"il y a {int(d // 60)} min"
    if d < 86400:
        return f"il y a {int(d // 3600)} h"
    n = int(d // 86400)
    return f"il y a {n} jour" + ("s" if n > 1 else "")


def echeance_texte(cree_le, maintenant=None) -> str:
    """"Expire dans X" pour l'AUTEUR, a partir de la derniere journee.

    Muet avant : sur sept jours, rappeler l'echeance des le premier
    ferait du bruit sur toutes les cartes.
    """
    ts = float(maintenant if maintenant is not None else time.time())
    reste = float(cree_le or 0.0) + EXPIRATION_S - ts
    if reste > 86400:
        return ""
    if reste <= 0:
        return "Expirée"
    if reste < 3600:
        return f"Expire dans {max(1, int(reste // 60))} min"
    return f"Expire dans {int(reste // 3600)} h"
