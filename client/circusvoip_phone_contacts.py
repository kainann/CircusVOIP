#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[CONTACTS 31/07/2026] Repertoire LOCAL du CircusPhone.

Un contact = un nom choisi par le joueur + un numero. Rien d'autre.

--- Ce que ce fichier N'EST PAS ---

Ce n'est pas l'annuaire du serveur. L'annuaire (discord_id -> pseudo,
numero) vit sur le VPS et reste reserve a l'administration : aucun joueur
n'y accede, ni en lecture ni en recherche. Ici, chaque joueur tient SON
carnet, alimente uniquement a la main, a partir de numeros qu'on lui a
donnes en jeu ou qui apparaissent dans son historique d'appels.

Consequence directe : deux joueurs peuvent noter la meme personne sous
des noms differents, et c'est voulu -- c'est un carnet d'adresses, pas un
annuaire partage.

--- Le nom n'est jamais stocke ailleurs ---

L'historique d'appels et les messages ne retiennent QUE le numero. Le nom
est substitue a l'affichage, via nom_pour(). Deux consequences utiles :
  - ajouter un contact renomme retroactivement TOUTES les lignes de ce
    numero, passees comme futures, sans rien avoir a mettre a jour ;
  - le supprimer les fait toutes redevenir des numeros.

--- Purge 0.4.0 ---

purge_donnees_0_4_0() efface les contacts, messages et images herites des
versions precedentes. Ils sont inexploitables dans le nouveau modele :
l'ancien annuaire se remplissait tout seul de PSEUDOS, sans numeros, donc
aucune de ces entrees n'est appelable. Garde par un drapeau one-shot.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

# Un numero valide : 6 chiffres, commence par 42 (plage 420000-429999).
_RE_NUMERO = re.compile(r"^42\d{4}$")

NUMERO_MIN = 420000
NUMERO_MAX = 429999

# Bornes du nom de contact. Le max evite qu'un nom casse l'affichage sur
# l'ecran etroit du telephone.
NOM_MIN_LEN = 1
NOM_MAX_LEN = 20

# Drapeau de purge. DOIT etre declare core-managed cote client : il est
# ecrit en cours de session, et _save_cfg au close le reecraserait avec
# la valeur du boot -> la purge rejouerait a chaque demarrage et
# effacerait aussi ce qui a ete cree depuis. Meme piege que
# phone_keys_defaults_applied au build 62.
CLE_PURGE = "phone_data_purged_040"


class ContactError(Exception):
    """Erreur metier, destinee a etre montree au joueur."""


# ---------------------------------------------
#  Validation
# ---------------------------------------------

def normalise_numero(saisie) -> str:
    """Retourne le numero nettoye, ou leve ContactError.

    Tolere les espaces et separateurs de saisie ("42 00 78", "42-0078") :
    un joueur qui recopie un numero entendu de vive voix les ajoute
    naturellement.
    """
    s = re.sub(r"[\s.\-]", "", str(saisie or ""))
    if not s.isdigit():
        raise ContactError("Le numéro ne doit contenir que des chiffres.")
    if len(s) != 6:
        raise ContactError("Le numéro doit faire 6 chiffres.")
    if not _RE_NUMERO.match(s):
        raise ContactError("Le numéro doit commencer par 42.")
    return s


def numero_valide(saisie) -> bool:
    """Version silencieuse, pour griser un bouton pendant la saisie."""
    try:
        normalise_numero(saisie)
        return True
    except ContactError:
        return False


def normalise_nom(saisie: str) -> str:
    """Nom nettoye (espaces de bord, espaces internes multiples).

    Contrairement aux pseudos serveur, AUCUNE unicite n'est imposee : le
    joueur a le droit d'avoir deux "Hugo" dans son carnet, c'est le sien.
    """
    nom = " ".join(str(saisie or "").split())
    if len(nom) < NOM_MIN_LEN:
        raise ContactError("Renseignez un nom.")
    if len(nom) > NOM_MAX_LEN:
        raise ContactError(f"Nom trop long (maximum {NOM_MAX_LEN} caractères).")
    return nom


# ---------------------------------------------
#  Repertoire
# ---------------------------------------------

class Repertoire:
    """Carnet local, persiste en JSON.

    Structure : {"contacts": {"420078": "Kainan", ...}}

    Indexe par NUMERO et non par nom : c'est le numero qui identifie, le
    nom n'est qu'une etiquette. Un numero ne peut donc apparaitre qu'une
    fois -- reajouter un numero connu revient a le renommer, ce qui est
    le comportement attendu.
    """

    def __init__(self, chemin: Path | str):
        self._chemin = Path(chemin)
        self._contacts: dict[str, str] = {}
        self._charger()

    def _charger(self):
        self._contacts = {}
        if not self._chemin.exists():
            return
        try:
            brut = json.loads(self._chemin.read_text(encoding="utf-8"))
            contacts = brut.get("contacts", {})
            if isinstance(contacts, dict):
                for num, nom in contacts.items():
                    # On revalide au chargement : un fichier edite a la
                    # main ne doit pas injecter n'importe quoi.
                    try:
                        self._contacts[normalise_numero(num)] = normalise_nom(nom)
                    except ContactError:
                        continue
        except Exception:
            # Fichier illisible : on repart d'un carnet vide plutot que
            # d'empecher le telephone de s'ouvrir. Contrairement a
            # l'annuaire serveur, la perte est locale et limitee.
            self._contacts = {}

    def _sauver(self):
        """Ecriture atomique : sans elle, une coupure en pleine ecriture
        laisse un JSON tronque, donc un carnet perdu."""
        data = {"contacts": self._contacts}
        tmp = self._chemin.with_suffix(self._chemin.suffix + ".tmp")
        self._chemin.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self._chemin)

    # --- lecture ---

    def nom_pour(self, numero) -> str | None:
        """Nom du contact, ou None si le numero est inconnu.

        Point d'entree UNIQUE de la substitution nom/numero. Tout ce qui
        affiche un correspondant passe par ici : appel entrant, appel
        sortant, historique, messagerie.
        """
        try:
            return self._contacts.get(normalise_numero(numero))
        except ContactError:
            return None

    def afficher(self, numero) -> str:
        """Nom si connu, numero sinon. Jamais vide."""
        return self.nom_pour(numero) or str(numero or "?")

    def liste(self) -> list[tuple[str, str]]:
        """[(numero, nom)] trie par nom, insensible a la casse."""
        return sorted(self._contacts.items(), key=lambda kv: kv[1].casefold())

    def contient(self, numero) -> bool:
        return self.nom_pour(numero) is not None

    def __len__(self) -> int:
        return len(self._contacts)

    # --- ecriture ---

    def ajouter(self, nom: str, numero) -> tuple[str, str]:
        """Ajoute ou renomme. Retourne (numero, nom) normalises.

        Leve ContactError si le nom ou le numero est invalide.
        """
        num = normalise_numero(numero)
        n = normalise_nom(nom)
        self._contacts[num] = n
        self._sauver()
        return num, n

    def supprimer(self, numero) -> bool:
        try:
            num = normalise_numero(numero)
        except ContactError:
            return False
        if num not in self._contacts:
            return False
        del self._contacts[num]
        self._sauver()
        return True


# ---------------------------------------------
#  Purge des donnees anterieures
# ---------------------------------------------

def purge_donnees_0_4_0(base_dir: Path | str, cfg: dict,
                        log=None) -> bool:
    """Efface contacts, messages et images herites d'avant la 0.4.0.

    Retourne True si la purge a eu lieu (donc si cfg a ete modifie et
    doit etre sauve par l'appelant).

    Pourquoi effacer : l'ancien annuaire s'auto-remplissait de PSEUDOS
    des joueurs vus connectes, sans aucun numero. Dans le nouveau modele
    ou l'on appelle un numero, aucune de ces entrees n'est exploitable.
    Les conversations sont indexees par pseudo, pour la meme raison.

    One-shot, garde par CLE_PURGE : sans le drapeau, la purge rejouerait
    a chaque demarrage et effacerait aussi les contacts crees depuis.

    Best-effort : un fichier verrouille ne doit pas empecher le client de
    demarrer. On pose le drapeau meme en cas d'echec partiel -- reessayer
    a chaque lancement ne reussirait pas davantage et masquerait le
    probleme.
    """
    if cfg.get(CLE_PURGE):
        return False

    base = Path(base_dir)
    dire = log or (lambda m: None)
    efface = []

    for nom in ("circusphone_annuaire.json", "circusphone_messages.json"):
        f = base / nom
        try:
            if f.exists():
                f.unlink()
                efface.append(nom)
        except Exception as e:
            dire(f"[PURGE] {nom} : {e}")

    dossier = base / "circusphone_images"
    try:
        if dossier.exists():
            n = sum(1 for _ in dossier.iterdir())
            shutil.rmtree(dossier, ignore_errors=True)
            if n:
                efface.append(f"{n} image(s)")
    except Exception as e:
        dire(f"[PURGE] images : {e}")

    # [CONTACTS 31/07/2026] L'historique d'appels aussi. Il n'est pas
    # dans un fichier a part : il vit dans la config client, sous
    # "call_history", ce qui lui a fait echapper au premier menage.
    # Ses entrees sont indexees par PSEUDO et n'ont pas de numero : ni
    # rappelables, ni ajoutables aux contacts. Les laisser afficherait
    # une liste de noms inertes, ce qui contredit en plus la regle
    # d'affichage par numero.
    try:
        if cfg.pop("call_history", None):
            efface.append("historique d'appels")
    except Exception as e:
        dire(f"[PURGE] historique : {e}")

    cfg[CLE_PURGE] = True
    if efface:
        dire("[PURGE] Données CircusPhone antérieures effacées : "
             + ", ".join(efface))
    return True
