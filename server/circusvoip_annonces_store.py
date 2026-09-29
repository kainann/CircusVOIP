#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[ANNONCES 21/09/2026] Stockage serveur de l'app Annonces.

COTE SERVEUR uniquement : ni Qt ni rien de graphique, il tourne sur le
VPS headless. NE JAMAIS l'ajouter a RELEASE_FILES : il contient l'AUTEUR
de chaque annonce, y compris de celles publiees case "inclure mon
numero" decochee.

Aucune regle ici : elles vivent dans circusvoip_phone_annonces, importe
comme il l'est cote client. Ce module apporte ce que le module de regles
n'a pas :

  1. La PERSISTANCE, par ecriture atomique -- meme patron que
     circusvoip_travail_store. Un crash en pleine sauvegarde laisserait
     sinon un JSON tronque et toutes les annonces perdues sans un mot.

  2. Le PLAFOND GLOBAL et l'INTERVALLE de publication : seul le serveur
     voit toutes les annonces et tous les auteurs.

  3. La PURGE des annonces expirees (7 jours).

  4. Le RETRAIT PAR L'ADMIN (moderation), qui ne passe pas par la regle
     "auteur seul".
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import circusvoip_phone_annonces as A

_DEFAUT = Path(__file__).resolve().parent / "circusvoip_annonces.json"

# Plafond global. 100 joueurs a 3 annonces = 300 en theorie : de quoi
# rendre la liste illisible bien avant d'inquieter le disque. Le
# probleme est d'affichage avant d'etre technique.
ANNONCES_TOTAL_MAX = 200

_SCHEMA = 1


class AnnonceStore:
    """Annonces du serveur. Toutes les methodes sont sures en concurrence."""

    def __init__(self, chemin=None):
        self._chemin = Path(chemin or _DEFAUT)
        self._lock = threading.RLock()
        self._annonces: dict[str, dict] = {}
        # Derniere publication par auteur, en RAM seulement : apres un
        # redemarrage, l'intervalle repart a zero, ce qui est sans
        # consequence -- il vise le spam en rafale, pas un historique.
        self._derniere_pub: dict[str, float] = {}
        self._charger()

    # -- disque --

    def _charger(self):
        try:
            d = json.loads(self._chemin.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:
            # Fichier illisible : on NE l'ecrase PAS. Des annonces peut-
            # etre recuperables a la main seraient detruites sans que
            # personne sache qu'il y a eu perte.
            print(f"[ANNONCES] circusvoip_annonces.json illisible ({e}). "
                  f"Le serveur demarre SANS annonces ; le fichier n'a pas "
                  f"ete ecrase, sauvegardez-le avant toute publication.",
                  flush=True)
            return
        for a in d.get("annonces", []):
            if isinstance(a, dict) and a.get("id"):
                self._annonces[a["id"]] = a

    def _sauver(self):
        """A appeler sous _lock."""
        d = {"schema": _SCHEMA, "annonces": list(self._annonces.values())}
        try:
            self._chemin.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._chemin.parent),
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._chemin)
        except Exception as e:
            print(f"[ANNONCES] Echec de sauvegarde : {e!r}", flush=True)

    # -- lectures --

    def toutes(self, maintenant=None) -> list[dict]:
        """Annonces en cours, de la plus recente a la plus ancienne.

        Les expirees sont ecartees ICI, sans attendre la purge : celle-ci
        passe une fois par jour, et une annonce perimee ne doit pas
        rester visible 23 heures de plus.
        """
        with self._lock:
            out = [dict(a) for a in self._annonces.values()
                   if not A.est_expiree(a, maintenant)]
        out.sort(key=lambda a: a.get("cree_le") or 0, reverse=True)
        return out

    def par_id(self, aid):
        with self._lock:
            a = self._annonces.get(str(aid))
            return dict(a) if a else None

    # -- ecritures --

    def publier(self, auteur_numero, categorie, description,
                avec_numero=True, maintenant=None) -> dict:
        """Cree et enregistre une annonce. Leve AnnonceError si refusee."""
        ts = float(maintenant if maintenant is not None else time.time())
        auteur = str(auteur_numero or "")
        with self._lock:
            liste = [a for a in self._annonces.values()
                     if not A.est_expiree(a, ts)]
            if len(liste) >= ANNONCES_TOTAL_MAX:
                raise A.AnnonceError(
                    "Le tableau d'annonces est plein. Réessayez plus tard.")
            if not A.peut_publier(liste, auteur, ts):
                raise A.AnnonceError(
                    f"{A.ANNONCES_PAR_AUTEUR_MAX} annonces maximum. "
                    f"Retirez-en une d'abord.")
            derniere = self._derniere_pub.get(auteur, 0.0)
            attente = A.PUBLICATION_INTERVALLE_S - (ts - derniere)
            if attente > 0:
                raise A.AnnonceError(
                    f"Patientez {int(attente) + 1} s avant de publier "
                    f"une nouvelle annonce.")
            a = A.cree_annonce(auteur, categorie, description,
                               avec_numero, ts)
            self._annonces[a["id"]] = a
            self._derniere_pub[auteur] = ts
            self._sauver()
            return dict(a)

    def retirer(self, aid, auteur_numero) -> dict:
        """Retrait par l'AUTEUR."""
        with self._lock:
            a = self._annonces.get(str(aid))
            if a is None:
                raise A.AnnonceError("Cette annonce n'existe plus.")
            A.peut_retirer(a, auteur_numero)
            del self._annonces[str(aid)]
            self._sauver()
            return dict(a)

    def retirer_admin(self, aid) -> dict | None:
        """Retrait par l'ADMIN (moderation). None si deja absente.

        Pas d'AnnonceError : l'appelant est l'interface admin, pas un
        joueur, et "deja retiree" n'y est pas une faute.
        """
        with self._lock:
            a = self._annonces.pop(str(aid), None)
            if a is not None:
                self._sauver()
            return dict(a) if a else None

    # -- entretien --

    def purger(self, maintenant=None) -> int:
        ts = float(maintenant if maintenant is not None else time.time())
        with self._lock:
            avant = len(self._annonces)
            self._annonces = {k: a for k, a in self._annonces.items()
                              if not A.est_expiree(a, ts)}
            # Les horodatages d'intervalle anciens ne servent plus.
            self._derniere_pub = {
                k: v for k, v in self._derniere_pub.items()
                if ts - v < A.PUBLICATION_INTERVALLE_S}
            n = avant - len(self._annonces)
            if n:
                self._sauver()
        return n

    def stats(self) -> dict:
        with self._lock:
            cats = {}
            for a in self._annonces.values():
                c = a.get("categorie")
                cats[c] = cats.get(c, 0) + 1
            return {"total": len(self._annonces), "par_categorie": cats}
