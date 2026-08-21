#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[GROUPES 19/08/2026] Stockage serveur des discussions de groupe.

Ce module tourne COTE SERVEUR uniquement. Il n'importe ni Qt ni rien de
graphique : il doit s'executer sur le VPS headless, comme
circusvoip_travail_store et circusvoip_phone_queue.

Il ne redefinit AUCUNE regle : elles vivent dans
circusvoip_phone_groupes, importe ici comme il l'est cote client. Le
serveur et le client appliquent donc le meme texte, pas deux copies qui
divergeront au premier changement de plafond.

--- Pourquoi persistant, contrairement aux signaux d'urgence ---

Le registre d'urgence vit en RAM et meurt avec le serveur : un signal
est un evenement, et un signal perdu est un signal qu'on relance. Un
groupe est l'inverse -- une structure durable, que ses membres
s'attendent a retrouver apres un redemarrage. Le perdre effacerait des
conversations sans un mot, et personne ne pourrait le reconstituer :
seul le createur connaissait la composition.

D'ou l'ecriture atomique, reprise de phone_queue : un crash en pleine
sauvegarde laisserait sinon un JSON tronque, et le rechargement
repartirait a zero.

--- Ce que ce module N'A PAS ---

Pas d'expiration. Travail purge a 30 jours parce qu'une annonce est un
evenement date ; un groupe d'amis inactif six mois n'est pas abandonne
pour autant. La seule purge est celle des groupes VIDES, que plus aucun
membre ne peut quitter et qui occuperaient sinon un identifiant
indefiniment.

Pas de stockage des MESSAGES. Le store tient la composition des groupes,
rien d'autre. Les messages sont routes puis oublies, exactement comme
les messages directs : c'est le client qui les conserve. Un serveur qui
archiverait les conversations de groupe deviendrait une cible autrement
plus interessante, pour un service que personne n'a demande.

--- Le serveur arbitre ---

Toutes les methodes sont sures en concurrence. Le point qui compte est
membres_de() : c'est la seule barriere entre un client modifie et la
conversation d'autrui. Aucun routage ne doit se faire sans etre passe
par elle.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

import circusvoip_phone_groupes as G

_DEFAUT = Path(__file__).resolve().parent / "circusvoip_groupes.json"

# Plafond global, tous createurs confondus.
#
# GROUPES_MAX_PAR_JOUEUR borne DEJA ce qu'un joueur peut porter ; ce
# plafond-ci borne ce qu'un serveur peut porter. Sur 100 joueurs a 20
# groupes, la limite theorique est 2000, dont beaucoup de doublons a
# 2 membres. Le plafond protege le disque et le temps de chargement,
# pas l'affichage -- chaque joueur ne voit que les siens.
GROUPES_TOTAL_MAX = 2000

_SCHEMA = 1


def _copie(groupe: dict) -> dict:
    """Copie d'un groupe SANS partage de la liste des membres.

    dict() seul est une copie de SURFACE : la liste `membres` resterait
    partagee avec le store, et n'importe quel appelant pourrait y ajouter
    ou en retirer quelqu'un sans passer par le verrou ni par les regles.
    Le defaut ne casse rien de visible -- il donne juste a tout le monde
    un droit d'ecriture qu'on croyait reserve.
    """
    g = dict(groupe)
    g["membres"] = list(groupe.get("membres") or [])
    return g


class GroupeStore:
    """Groupes du serveur. Toutes les methodes sont sures en concurrence."""

    def __init__(self, chemin=None):
        self._chemin = Path(chemin or _DEFAUT)
        self._lock = threading.RLock()
        self._groupes: dict[str, dict] = {}
        self._charger()

    # -- disque --

    def _charger(self):
        try:
            d = json.loads(self._chemin.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:
            # Fichier illisible : on NE repart PAS a zero en silence.
            # Ecraser un JSON corrompu par un fichier vide detruirait des
            # groupes peut-etre recuperables a la main, et personne ne
            # saurait qu'il y a eu perte -- les joueurs verraient
            # seulement leurs conversations de groupe disparaitre.
            print(f"[GROUPES] circusvoip_groupes.json illisible ({e}). "
                  f"Le serveur demarre SANS groupes ; le fichier n'a pas "
                  f"ete ecrase, sauvegardez-le avant toute creation.",
                  flush=True)
            return
        for g in d.get("groupes", []):
            if isinstance(g, dict) and g.get("id") and not G.est_vide(g):
                self._groupes[str(g["id"])] = g

    def _sauver(self):
        """A appeler sous _lock."""
        d = {"schema": _SCHEMA, "groupes": list(self._groupes.values())}
        try:
            self._chemin.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._chemin.parent),
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._chemin)
        except Exception as e:
            print(f"[GROUPES] Echec de sauvegarde : {e!r}", flush=True)

    # -- lectures --

    def par_id(self, gid):
        """Groupe par identifiant, ou None.

        Ne verifie AUCUNE appartenance : c'est un acces brut, reserve au
        code interne. Tout chemin declenche par un client doit passer par
        membres_de() ou verifier G.membre() derriere.
        """
        with self._lock:
            g = self._groupes.get(str(gid))
            return _copie(g) if g else None

    def membres_de(self, gid, numero):
        """Membres de ce groupe, SI le demandeur en fait partie.

        Rend None si le groupe n'existe pas OU si le demandeur n'en est
        pas membre -- les deux cas sont volontairement indistinguables.
        Repondre "ce groupe existe mais vous n'en etes pas" confirmerait
        l'existence d'un identifiant a qui le devine.

        C'est la barriere de securite du module. Aucun routage ne doit se
        faire sans etre passe par ici.
        """
        with self._lock:
            g = self._groupes.get(str(gid))
            if not g or not G.membre(g, numero):
                return None
            return list(g.get("membres") or [])

    def de_joueur(self, numero):
        """Groupes dont ce joueur est membre, du plus recent au plus ancien.

        Le tri est sur la date de CREATION, pas sur l'activite : le store
        ne connait pas les messages. C'est le client qui reclassera sa
        liste de conversations sur le dernier message recu.
        """
        num = G.valide_numero(numero)
        if not num:
            return []
        with self._lock:
            out = [_copie(g) for g in self._groupes.values()
                   if G.membre(g, num)]
        out.sort(key=lambda g: float(g.get("cree_le", 0.0)), reverse=True)
        return out

    def nb_de_joueur(self, numero) -> int:
        """Nombre de groupes portes par ce joueur, crees ET rejoints."""
        num = G.valide_numero(numero)
        if not num:
            return 0
        with self._lock:
            return sum(1 for g in self._groupes.values() if G.membre(g, num))

    # -- ecritures --

    def creer(self, createur_numero, nom, membres, maintenant=None) -> dict:
        """Cree et enregistre un groupe. Leve GroupeError si refuse.

        Le plafond est verifie pour le CREATEUR seul, pas pour les
        membres qu'il ajoute. C'est un choix : verifier chaque membre
        ferait echouer la creation entiere a cause d'un tiers, sans que
        le createur puisse rien y faire ni meme comprendre lequel bloque
        -- et le lui dire renseignerait sur l'activite d'autrui.

        La consequence est assumee : un joueur peut depasser
        GROUPES_MAX_PAR_JOUEUR s'il est beaucoup invite. Le plafond
        borne ce qu'il CREE, et sert de garde-fou, pas de regle stricte.
        """
        with self._lock:
            if len(self._groupes) >= GROUPES_TOTAL_MAX:
                raise G.GroupeError(
                    "Le serveur ne peut plus créer de groupe. "
                    "Contactez un administrateur.")
            if not G.peut_creer(self.nb_de_joueur(createur_numero)):
                raise G.GroupeError(
                    f"{G.GROUPES_MAX_PAR_JOUEUR} groupes maximum. "
                    f"Quittez-en un d'abord.")
            g = G.cree_groupe(createur_numero, nom, membres, maintenant)
            if not g:
                # cree_groupe rend {} pour toute saisie invalide sans
                # dire laquelle. On ne peut donc pas etre plus precis
                # ici sans dupliquer ses regles -- et cette duplication
                # divergerait au premier changement de plafond.
                raise G.GroupeError(
                    f"Groupe invalide : il faut un nom "
                    f"({G.NOM_MAX_LEN} caractères maximum) et de "
                    f"{G.MEMBRES_MIN} à {G.MEMBRES_MAX} membres.")
            self._groupes[g["id"]] = g
            self._sauver()
            return _copie(g)

    def quitter(self, gid, numero) -> dict:
        """Retire ce membre. Leve GroupeError s'il n'en fait pas partie.

        Un groupe devenu vide est SUPPRIME dans la foulee : plus aucun
        membre ne pourrait le quitter, il resterait donc sur disque pour
        toujours.

        Rend le groupe tel qu'il est APRES le depart, pour que l'appelant
        sache a qui notifier -- y compris vide, auquel cas il n'y a
        personne a prevenir.
        """
        with self._lock:
            g = self._groupes.get(str(gid))
            if not g or not G.membre(g, numero):
                # Meme message dans les deux cas : cf. membres_de().
                raise G.GroupeError("Ce groupe n'existe pas.")
            g = G.quitter(g, numero)
            if G.est_vide(g):
                self._groupes.pop(str(gid), None)
            else:
                self._groupes[str(gid)] = g
            self._sauver()
            return _copie(g)

    def purger(self) -> int:
        """Supprime les groupes vides. Rend le nombre de suppressions.

        Ne devrait rien trouver : quitter() supprime deja au fil de
        l'eau. Le filet existe pour les fichiers ecrits par une version
        anterieure, ou modifies a la main -- un groupe vide est
        invisible et donc indebogable autrement.
        """
        with self._lock:
            morts = [gid for gid, g in self._groupes.items()
                     if G.est_vide(g)]
            for gid in morts:
                self._groupes.pop(gid, None)
            if morts:
                self._sauver()
            return len(morts)

    def stats(self) -> dict:
        """Compteurs pour la console d'administration."""
        with self._lock:
            total = len(self._groupes)
            membres = sum(len(g.get("membres") or [])
                          for g in self._groupes.values())
        return {
            "groupes": total,
            "adhesions": membres,
            "taille_moyenne": round(membres / total, 1) if total else 0.0,
        }
