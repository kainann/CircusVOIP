#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[GROUPES 19/08/2026] Modele metier des discussions de groupe du
CircusPhone.

Ce fichier ne connait NI le reseau, NI Qt. Il contient les regles, et
elles seules : ce qu'est un groupe, ce qui le rend valide, et qui a le
droit d'y ecrire. Le serveur l'importe pour arbitrer, le client pour
valider une saisie avant de l'envoyer.

--- Un groupe est FIGE a sa creation ---

C'est la decision structurante, et elle simplifie tout le reste. Pas
d'invitation, pas d'acceptation, pas d'ajout ni de retrait apres coup :
le createur choisit ses membres, envoie, et la composition ne bouge plus
jamais. Il n'y a donc aucun etat transitoire a arbitrer, aucune course
entre deux clients, et aucun ecran de gestion a ecrire.

Le prix est assume : pour ajouter quelqu'un, on cree un nouveau groupe.
Sur des discussions entre amis -- l'usage vise -- c'est acceptable ; ca
ne le serait pas pour une organisation permanente.

--- Le groupe distribue les numeros, et c'est VOULU ---

Chaque membre voit les numeros des autres, y compris ceux de gens qu'il
n'a jamais croises en jeu. Ce n'est pas une entorse a la regle RP :
cette regle protege le NOM, pas le numero. Le serveur n'envoie jamais de
pseudo (cf. les marqueurs [RP 04/08/2026]), et un numero seul est
anonyme -- un membre ne verra un nom que s'il l'a deja dans SON carnet
local.

Autrement dit, un groupe ne revele l'identite de personne. Il donne un
moyen de contact, pas une identite, et c'est exactement ce que fait
donner son numero de vive voix.

--- Le createur n'a aucun privilege ---

Une fois le groupe cree, il est un membre comme les autres. Il ne peut
ni exclure, ni renommer, ni dissoudre. La seule asymetrie est
historique : c'est lui qui a choisi la composition initiale.

Consequence a connaitre : quitter un groupe est individuel et
definitif. Rien ne permet de "fermer" un groupe pour tout le monde, et
c'est cohérent avec le figeage -- un groupe meurt quand son dernier
membre le quitte.

--- Le serveur arbitre, toujours ---

Le client ne decide jamais qu'il appartient a un groupe : il le demande,
et le serveur repond. Un client modifie ne doit pas pouvoir s'inviter
dans une conversation en fabriquant un identifiant, d'ou membre() et
peut_ecrire(), ecrits pour etre appeles COTE SERVEUR avant tout routage.

--- Identifiant opaque ---

L'identifiant d'un groupe est tire au hasard, jamais derive de son nom
ni de ses membres. Un identifiant previsible permettrait de deviner
celui d'un groupe existant et de tenter d'y ecrire ; la verification
d'appartenance le refuserait, mais l'existence meme du groupe serait
alors observable.
"""

from __future__ import annotations

import re
import secrets
import time

# --- Bornes de saisie ---------------------------------------------------
#
# NOM_MAX_LEN est aligne sur TITRE_MAX_LEN des missions (40) : meme
# contrainte d'affichage, meme largeur d'ecran, aucune raison de
# diverger.
NOM_MIN_LEN = 1
NOM_MAX_LEN = 40

# Bornes de composition.
#
# MEMBRES_MIN vaut 2 : le createur, plus au moins un autre. Un "groupe"
# d'une personne est une conversation avec soi-meme ; un groupe de deux
# est un doublon de la messagerie directe, mais le refuser obligerait a
# expliquer pourquoi -- on l'autorise et on laisse le joueur juger.
#
# MEMBRES_MAX vaut 10, createur COMPRIS. C'est une limite d'affichage
# avant d'etre technique : chaque message de groupe est duplique vers
# tous les membres, et l'en-tete de conversation doit tenir a l'ecran.
MEMBRES_MIN = 2
MEMBRES_MAX = 10

# Plafond par joueur, groupes CREES et REJOINTS confondus.
#
# Compter les deux est volontaire : ne borner que la creation
# laisserait un joueur etre membre de cent groupes crees par d'autres,
# et sa liste de conversations deviendrait inutilisable sans qu'il ait
# rien fait de mal.
GROUPES_MAX_PAR_JOUEUR = 20

# Prefixe de la cle de conversation cote client.
#
# Les conversations sont indexees par NUMERO (cf. _phone_conversations).
# Un groupe n'a pas de numero : il lui faut donc une cle qui ne puisse
# JAMAIS entrer en collision avec un numero valide. Les numeros sont
# purement numeriques, ce prefixe ne l'est pas -- la collision est
# structurellement impossible, pas seulement improbable.
CLE_PREFIXE = "G:"

# Marqueur en tete du CORPS d'un message de groupe.
#
# Un message de groupe voyage dans une trame phone_message_received
# ORDINAIRE, avec l'identifiant du groupe encode devant le texte :
#
#     [grp]<id>\n<texte>
#
# Meme technique que PHONE_IMG_PREFIX ("[img]<fichier>"), qui fait deja
# ca pour les images. Ce n'est pas un detournement : c'est ce qui permet
# au chemin DIRECT et au chemin DIFFERE d'etre strictement identiques.
#
# La file hors ligne (circusvoip_phone_queue) stocke `kind`, `from` et
# `body`, et rien d'autre. Transporter le groupe dans un champ a part
# aurait impose d'ajouter un genre et un champ a un schema DEJA ecrit sur
# le disque du VPS -- avec une migration a prevoir, et des files
# existantes a relire. Ici, il n'y a rien a migrer.
#
# Un joueur ne peut pas usurper ce marqueur en le tapant a la main : le
# serveur reencode systematiquement le corps a partir du groupe qu'il a
# lui-meme verifie, et n'utilise jamais le prefixe recu du client.
MSG_PREFIXE = "[grp]"
_MSG_SEP = "\n"

# Marqueur d'un message SYSTEME (« a quitte le groupe »).
#
# Place APRES le decodage du groupe, donc dans le texte : le corps
# complet vaut "[grp]<id>\n[sys]a quitté le groupe".
#
# Il sert a distinguer, dans la conversation stockee, ce que le serveur a
# annonce de ce qu'un joueur a ecrit. Sans lui, quelqu'un pourrait taper
# « a quitté le groupe » et faire croire a un depart. Le client retire le
# marqueur a l'affichage.
MSG_SYSTEME = "[sys]"

# Texte des annonces systeme. Ici et pas cote serveur : le client doit
# pouvoir les reconnaitre, et deux copies divergeraient.
SYS_DEPART = "a quitté le groupe"


def encode_systeme(groupe_id, texte) -> str:
    """Corps d'une annonce systeme (depart d'un membre).

    A appeler COTE SERVEUR uniquement : une annonce fabriquee par un
    client serait un mensonge affiche comme une verite.
    """
    return encode_message(groupe_id, f"{MSG_SYSTEME}{texte}")


def est_systeme(texte) -> bool:
    """Vrai si ce texte DEJA DECODE est une annonce du serveur."""
    return isinstance(texte, str) and texte.startswith(MSG_SYSTEME)


def texte_systeme(texte) -> str:
    """Annonce systeme sans son marqueur, pour l'affichage."""
    if not est_systeme(texte):
        return texte if isinstance(texte, str) else ""
    return texte[len(MSG_SYSTEME):]


def encode_message(groupe_id, texte) -> str:
    """Corps de message portant son groupe. Cf. MSG_PREFIXE.

    A appeler COTE SERVEUR uniquement, avec un identifiant deja verifie.
    """
    return f"{MSG_PREFIXE}{groupe_id}{_MSG_SEP}{texte}"


def decode_message(body) -> tuple[str, str]:
    """Rend (groupe_id, texte). ("", body) si ce n'est pas un message de groupe.

    Ne leve jamais : le client passe ici TOUS les messages recus, dont
    l'immense majorite sont des messages directs.
    """
    if not isinstance(body, str) or not body.startswith(MSG_PREFIXE):
        return "", (body if isinstance(body, str) else "")
    reste = body[len(MSG_PREFIXE):]
    gid, sep, texte = reste.partition(_MSG_SEP)
    if not sep:
        # Marqueur sans separateur : corps malforme. On rend le texte
        # brut plutot que de le perdre -- un message illisible vaut mieux
        # qu'un message disparu sans trace.
        return "", body
    return gid, texte

# Longueur de l'identifiant, en octets tires au hasard (donc 2x en hex).
#
# 8 octets = 64 bits. Deviner un identifiant existant demanderait des
# milliards d'essais, tous refuses par la verification d'appartenance et
# tous traces cote serveur.
_ID_OCTETS = 8

# Un numero est une suite de chiffres. Repris tel quel de
# circusvoip_phone_contacts.normalise_numero, sans l'importer : ce
# module doit rester importable cote serveur, ou contacts (qui touche au
# disque du client) n'a rien a faire.
_RE_NUMERO = re.compile(r"^\d{1,15}$")

# Caracteres de controle : interdits dans un nom de groupe.
#
# Un retour a la ligne dans un nom casserait l'affichage de la liste des
# conversations, et un caractere nul pourrait tronquer un log.
_RE_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


class GroupeError(Exception):
    """Refus metier, porteur d'un message DESTINE AU JOUEUR.

    Meme convention que TravailError : le texte de l'exception est
    affiche tel quel dans le CircusPhone. Il doit donc rester lisible,
    en francais, et ne jamais reveler d'information qu'un joueur n'a pas
    a connaitre -- en particulier ne jamais confirmer l'existence d'un
    groupe auquel on n'appartient pas.
    """


def nouvel_id() -> str:
    """Identifiant opaque pour un nouveau groupe.

    Tire au hasard, jamais derive du nom ni des membres : cf. le
    paragraphe "Identifiant opaque" en tete de module.
    """
    return secrets.token_hex(_ID_OCTETS)


def cle_conversation(groupe_id) -> str:
    """Cle de stockage cote client pour ce groupe.

    Le client range les groupes dans le MEME dictionnaire que les
    conversations directes ; seule la cle les distingue. C'est ce qui
    permet de reutiliser tout l'ecran de conversation existant au lieu
    d'en ecrire un second.
    """
    return CLE_PREFIXE + str(groupe_id or "")


def est_cle_groupe(cle) -> bool:
    """Vrai si cette cle de conversation designe un groupe."""
    return isinstance(cle, str) and cle.startswith(CLE_PREFIXE)


def id_depuis_cle(cle) -> str:
    """Identifiant de groupe porte par une cle de conversation.

    Rend "" si la cle n'est pas celle d'un groupe, plutot que de lever :
    l'appelant parcourt un dictionnaire ou les deux types cohabitent.
    """
    if not est_cle_groupe(cle):
        return ""
    return cle[len(CLE_PREFIXE):]


def valide_numero(numero) -> str:
    """Numero normalise, ou "" s'il est invalide.

    Rend une chaine vide plutot que de lever : l'appelant valide souvent
    une liste entiere et veut ecarter les mauvais, pas s'arreter au
    premier.
    """
    if numero is None:
        return ""
    txt = str(numero).strip()
    if not _RE_NUMERO.match(txt):
        return ""
    return txt


def valide_nom(nom) -> str:
    """Nom de groupe normalise, ou "" s'il est invalide.

    Les espaces multiples sont ecrases : deux groupes nommes "Les  amis"
    et "Les amis" seraient indistinguables a l'oeil dans la liste des
    conversations.
    """
    if nom is None:
        return ""
    # Remplaces par une ESPACE, pas supprimes : supprimer collerait les
    # mots ("Les\namis" -> "Lesamis"), ce qui rend illisible un nom colle
    # depuis un texte sur deux lignes. L'ecrasement suivant absorbe
    # l'espace en trop.
    txt = _RE_CONTROLE.sub(" ", str(nom))
    txt = " ".join(txt.split())
    if not (NOM_MIN_LEN <= len(txt) <= NOM_MAX_LEN):
        return ""
    return txt


def valide_membres(membres, createur_numero=None) -> list[str]:
    """Liste de membres normalisee, ou [] si la composition est invalide.

    Le createur est ajoute d'office s'il n'y figure pas : un createur
    absent de son propre groupe ne recevrait pas les reponses, ce qui
    n'a aucun sens et serait tres deroutant a diagnostiquer.

    Les doublons sont ecrases. Sans ca, un membre ajoute deux fois
    recevrait chaque message en double -- et le compteur de membres
    mentirait.
    """
    if not isinstance(membres, (list, tuple, set)):
        return []
    vus: list[str] = []
    for m in membres:
        num = valide_numero(m)
        if num and num not in vus:
            vus.append(num)
    createur = valide_numero(createur_numero)
    if createur and createur not in vus:
        vus.append(createur)
    if not (MEMBRES_MIN <= len(vus) <= MEMBRES_MAX):
        return []
    return vus


def cree_groupe(createur_numero, nom, membres, maintenant=None) -> dict:
    """Groupe pret a etre stocke, ou {} si la saisie est invalide.

    Rend {} plutot que de lever, comme le reste du module : l'appelant
    cote serveur doit repondre une erreur au client, pas planter la
    boucle de traitement des trames.
    """
    createur = valide_numero(createur_numero)
    if not createur:
        return {}
    nom_ok = valide_nom(nom)
    if not nom_ok:
        return {}
    membres_ok = valide_membres(membres, createur)
    if not membres_ok:
        return {}
    ts = float(maintenant if maintenant is not None else time.time())
    return {
        "id":       nouvel_id(),
        "nom":      nom_ok,
        "createur": createur,
        # Liste FIGEE : cf. le paragraphe en tete de module. Elle ne
        # doit plus jamais etre reecrite, sauf par quitter().
        "membres":  list(membres_ok),
        "cree_le":  ts,
    }


def membre(groupe, numero) -> bool:
    """Vrai si ce numero fait partie du groupe.

    A appeler COTE SERVEUR avant tout routage. C'est la seule barriere
    entre un client modifie et la conversation d'autrui : sans elle, un
    identifiant devine suffirait a ecrire dans un groupe, ou a en lire
    les membres.
    """
    if not isinstance(groupe, dict):
        return False
    num = valide_numero(numero)
    if not num:
        return False
    return num in (groupe.get("membres") or [])


def peut_ecrire(groupe, numero) -> bool:
    """Vrai si ce numero peut poster dans ce groupe.

    Etre membre ne suffit PAS : il faut rester au moins deux. Un groupe
    reduit a une personne passe en LECTURE SEULE -- la conversation reste
    consultable, mais plus rien ne peut y etre envoye, faute de
    destinataire.

    C'est bien ici que ca se decide, comme prevu quand cette fonction a
    ete separee de membre() : les appels de routage pointaient deja au
    bon endroit, il n'y a rien eu a deplacer.
    """
    if not membre(groupe, numero):
        return False
    return len(groupe.get("membres") or []) >= 2


def est_seul(groupe) -> bool:
    """Vrai si le groupe est reduit a un seul membre.

    Distinct de est_vide() : un groupe seul EXISTE encore et se lit, un
    groupe vide est supprime. C'est la difference entre « tout le monde
    est parti » et « il n'y a plus personne ».
    """
    if not isinstance(groupe, dict):
        return False
    return len(groupe.get("membres") or []) == 1


def destinataires(groupe, sauf=None) -> list[str]:
    """Numeros a qui router un message de ce groupe.

    `sauf` ecarte l'emetteur : son propre client a deja affiche le
    message localement. Le lui renvoyer l'afficherait deux fois.
    """
    if not isinstance(groupe, dict):
        return []
    hors = valide_numero(sauf)
    return [m for m in (groupe.get("membres") or []) if m != hors]


def quitter(groupe, numero) -> dict:
    """Retire ce membre du groupe. Rend le groupe modifie.

    Seule operation qui touche a la composition apres creation. Elle est
    individuelle et definitive : rien ne permet de revenir, puisque rien
    ne permet d'ajouter.

    Le groupe est rendu meme vide -- c'est a l'appelant (le store) de
    decider qu'un groupe sans membre doit disparaitre. Ce module ne
    connait pas le stockage.
    """
    if not isinstance(groupe, dict):
        return {}
    num = valide_numero(numero)
    if not num:
        return groupe
    restants = [m for m in (groupe.get("membres") or []) if m != num]
    groupe = dict(groupe)
    groupe["membres"] = restants
    return groupe


def est_vide(groupe) -> bool:
    """Vrai si plus personne n'est membre.

    Un groupe vide n'est pas seulement inutile : il occupe un
    identifiant et de la place sur disque indefiniment, puisque aucun
    membre ne peut plus le quitter pour declencher sa purge.
    """
    if not isinstance(groupe, dict):
        return True
    return not (groupe.get("membres") or [])


def peut_creer(nb_groupes_actuels) -> bool:
    """Vrai si ce joueur peut encore creer ou rejoindre un groupe.

    Compte les groupes crees ET rejoints : cf. GROUPES_MAX_PAR_JOUEUR.
    """
    try:
        return int(nb_groupes_actuels) < GROUPES_MAX_PAR_JOUEUR
    except Exception:
        return False


def resume_membres(groupe, repertoire=None, sauf=None) -> str:
    """Libelle des membres pour l'en-tete de conversation.

    `repertoire` est le carnet LOCAL de celui qui regarde : une fonction
    numero -> nom ou None. C'est lui, et lui seul, qui peut substituer un
    nom -- le serveur n'envoie jamais de pseudo. Un membre inconnu reste
    donc affiche par son numero, ce qui est le comportement voulu et non
    un defaut.
    """
    if not isinstance(groupe, dict):
        return ""
    hors = valide_numero(sauf)
    noms = []
    for num in (groupe.get("membres") or []):
        if hors and num == hors:
            continue
        libelle = None
        if repertoire is not None:
            try:
                libelle = repertoire(num)
            except Exception:
                libelle = None
        noms.append(libelle or num)
    return ", ".join(noms)
