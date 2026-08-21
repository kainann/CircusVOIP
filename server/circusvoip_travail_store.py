#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[TRAVAIL 10/08/2026] Stockage serveur des missions de l'app Travail.

Ce module tourne COTE SERVEUR uniquement. Il n'importe ni Qt ni rien de
graphique : il doit s'executer sur le VPS headless, comme
circusvoip_phone_queue.

Il ne redefinit AUCUNE regle : elles vivent dans
circusvoip_phone_travail, importe ici comme il l'est cote client. C'est
tout l'interet de la separation -- le serveur et le client appliquent le
meme texte, pas deux copies qui divergeront au premier changement de
plafond.

--- Ce que ce module apporte que le module de regles n'a pas ---

  1. La PERSISTANCE. Ecriture atomique, comme phone_queue : un crash en
     pleine sauvegarde laisserait sinon un JSON tronque, et le
     rechargement repartirait a zero -- toutes les missions du serveur
     perdues, sans un mot.

  2. L'ARBITRAGE. Deux joueurs peuvent cliquer "Prendre" sur la meme
     mission dans la meme seconde. Le serveur est le seul a les voir
     tous les deux ; c'est donc lui, et lui seul, qui tranche. Le client
     ne retire jamais une mission de sa propre liste : il attend la
     confirmation.

  3. La PURGE. Les missions expirent a 30 jours (cf. EXPIRATION_S). Sans
     passage regulier, la liste ne se viderait jamais : une annonce
     abandonnee en silence -- l'auteur ne joue plus, l'executant a
     oublie -- resterait indefiniment. Sur 100 joueurs, l'onglet devient
     illisible en quelques mois.

  4. Le CIBLAGE. Une mission concerne UN metier. La notifier a tout le
     monde ferait 100 destinataires la ou 10 suffisent. destinataires()
     rend la liste exacte, sur le meme principe que la diffusion par
     zone.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import circusvoip_phone_travail as T

_DEFAUT = Path(__file__).resolve().parent / "circusvoip_missions.json"

# Plafond global, tous auteurs confondus.
#
# MISSIONS_OUVERTES_MAX borne DEJA ce qu'un joueur peut publier ; ce
# plafond-ci borne ce qu'un serveur peut porter. Sur 100 joueurs a 5
# missions chacun, la limite theorique est 500 : de quoi rendre l'onglet
# inutilisable bien avant d'inquieter le disque. Le probleme est
# d'affichage avant d'etre technique.
MISSIONS_TOTAL_MAX = 400

_SCHEMA = 1


class MissionStore:
    """Missions du serveur. Toutes les methodes sont sures en concurrence."""

    def __init__(self, chemin=None):
        self._chemin = Path(chemin or _DEFAUT)
        self._lock = threading.RLock()
        self._missions: dict[str, dict] = {}
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
            # missions peut-etre recuperables a la main, et personne ne
            # saurait qu'il y a eu perte. On le dit, et on demarre vide.
            print(f"[TRAVAIL] circusvoip_missions.json illisible ({e}). "
                  f"Le serveur demarre SANS missions ; le fichier n'a pas "
                  f"ete ecrase, sauvegardez-le avant toute publication.",
                  flush=True)
            return
        for m in d.get("missions", []):
            if isinstance(m, dict) and m.get("id"):
                self._missions[m["id"]] = m

    def _sauver(self):
        """A appeler sous _lock."""
        d = {"schema": _SCHEMA, "missions": list(self._missions.values())}
        try:
            self._chemin.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._chemin.parent),
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._chemin)
        except Exception as e:
            print(f"[TRAVAIL] Echec de sauvegarde : {e!r}", flush=True)

    # -- lectures --

    def par_id(self, mid):
        with self._lock:
            m = self._missions.get(str(mid))
            return dict(m) if m else None

    def en_cours(self, numero):
        """Mission que ce joueur execute, ou None. Au plus une."""
        n = str(numero)
        with self._lock:
            for m in self._missions.values():
                if m.get("etat") == T.ETAT_PRISE and m.get("executant") == n:
                    return dict(m)
        return None

    def visibles(self, metiers, maintenant=None):
        """Missions ouvertes destinees a l'un de ces metiers.

        Triees du plus recent au plus ancien : une annonce fraiche a plus
        de chances d'etre encore d'actualite.
        """
        with self._lock:
            out = [dict(m) for m in self._missions.values()
                   if T.est_visible(m, metiers, maintenant)]
        out.sort(key=lambda m: m.get("cree_le") or 0, reverse=True)
        return out

    def de_auteur(self, numero):
        n = str(numero)
        with self._lock:
            out = [dict(m) for m in self._missions.values()
                   if m.get("auteur") == n and m.get("etat") != T.ETAT_CLOSE]
        out.sort(key=lambda m: m.get("cree_le") or 0, reverse=True)
        return out

    # -- ecritures --

    def publier(self, auteur_numero, metier, titre, paiement,
                description="", maintenant=None) -> dict:
        """Cree et enregistre une mission. Leve TravailError si refusee."""
        with self._lock:
            liste = list(self._missions.values())
            if len(liste) >= MISSIONS_TOTAL_MAX:
                raise T.TravailError(
                    "Le tableau d'annonces est plein. Réessayez plus tard.")
            if not T.peut_publier(liste, auteur_numero, maintenant):
                raise T.TravailError(
                    f"{T.MISSIONS_OUVERTES_MAX} missions ouvertes maximum. "
                    f"Retirez-en une d'abord.")
            m = T.cree_mission(auteur_numero, metier, titre, paiement,
                               description, maintenant)
            self._missions[m["id"]] = m
            self._sauver()
            return dict(m)

    def prendre(self, mid, executant_numero, maintenant=None) -> dict:
        """Attribue la mission. C'EST ICI QUE LA COURSE SE TRANCHE.

        Le verrou couvre la lecture de l'etat ET son ecriture : sans lui,
        deux requetes simultanees liraient toutes deux "ouverte" avant que
        l'une n'ecrive, et les deux joueurs croiraient avoir la mission.
        """
        with self._lock:
            m = self._missions.get(str(mid))
            if m is None:
                raise T.TravailError("Cette mission n'existe plus.")
            deja = self.en_cours(executant_numero)
            T.prendre(m, executant_numero, deja, maintenant)
            self._sauver()
            return dict(m)

    def abandonner(self, mid, executant_numero) -> dict:
        with self._lock:
            m = self._missions.get(str(mid))
            if m is None:
                raise T.TravailError("Cette mission n'existe plus.")
            T.abandonner(m, executant_numero)
            self._sauver()
            return dict(m)

    def clore(self, mid, numero, maintenant=None) -> dict:
        with self._lock:
            m = self._missions.get(str(mid))
            if m is None:
                raise T.TravailError("Cette mission n'existe plus.")
            T.clore(m, numero, maintenant)
            self._sauver()
            return dict(m)

    def retirer(self, mid, auteur_numero) -> dict:
        with self._lock:
            m = self._missions.get(str(mid))
            if m is None:
                raise T.TravailError("Cette mission n'existe plus.")
            T.retirer(m, auteur_numero)
            self._sauver()
            return dict(m)

    # -- entretien --

    def purger(self, maintenant=None) -> int:
        """Supprime les missions expirees et les closes anciennes.

        Deux traitements distincts :
          - EXPIREE : depassee sans avoir abouti, elle n'interesse plus
            personne.
          - CLOSE : gardee 7 jours pour que les deux parties la voient
            dans leur historique, puis retiree. La conserver a vie ferait
            croitre le fichier sans limite.
        """
        ts = float(maintenant if maintenant is not None else time.time())
        garde_close = 7 * 24 * 3600
        with self._lock:
            avant = len(self._missions)
            self._missions = {
                k: m for k, m in self._missions.items()
                if not (
                    T.est_expiree(m, ts)
                    or (m.get("etat") == T.ETAT_CLOSE
                        and ts - float(m.get("clos_le") or 0.0) > garde_close)
                )
            }
            n = avant - len(self._missions)
            if n:
                self._sauver()
        return n

    def stats(self) -> dict:
        with self._lock:
            etats = {}
            for m in self._missions.values():
                etats[m.get("etat")] = etats.get(m.get("etat"), 0) + 1
            return {"total": len(self._missions), "par_etat": etats}


# ---------------------------------------------------------------------
#  Ciblage des notifications
# ---------------------------------------------------------------------

def destinataires(mission, metiers_par_numero) -> list[str]:
    """Numeros a prevenir de la publication d'une mission.

    Ceux qui exercent le metier RECHERCHE, moins l'auteur -- se notifier
    soi-meme de sa propre annonce n'apprend rien et donne l'impression
    d'un bug.

    On cible plutot que de diffuser a tous : sur 100 joueurs dont 10
    mercenaires, c'est 10 messages au lieu de 100. Meme principe que la
    diffusion des positions par zone.
    """
    cible = mission.get("metier")
    auteur = mission.get("auteur")
    out = []
    for numero, metiers in (metiers_par_numero or {}).items():
        if numero == auteur:
            continue
        if cible in (metiers or []):
            out.append(numero)
    return out
