#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[URGENCE 15/08/2026] Registre des signaux d'urgence, cote serveur.

Pendant du circusvoip_travail_store.py pour l'app Travail, avec une
difference qui change tout : ce registre n'ecrit RIEN sur disque.

--- Pourquoi rien sur disque ---

Un signal d'urgence vit quelques minutes et meurt avec la connexion de
la victime. Le persister obligerait a gerer une purge, une migration de
format, un JSON corrompu -- pour des donnees dont la duree de vie est
inferieure au temps entre deux redemarrages du serveur.

Et surtout, ca serait FAUX : un signal restaure au redemarrage
decrirait une victime qui n'est plus connectee, a une position qui n'a
plus cours. Mieux vaut qu'il disparaisse.

Meme raisonnement pour la prise de service : elle est liee a la
connexion. Un secouriste qui se reconnecte est hors service, il repointe
s'il le souhaite. Un etat de garde qui survivrait a un redemarrage
serait une promesse que personne ne tient.

Les ROLES, eux, sont persistants -- mais ils vivent dans la fiche du
joueur (circusvoip_accounts.py), pas ici.

--- Ce que ce module ne fait pas ---

Il n'arbitre pas les droits. "Est-ce bien un chef ?", "ce joueur a-t-il
deja l'autre role ?" appartiennent aux regles
(circusvoip_phone_urgence.py) et aux comptes. Ici on stocke, on cherche
et on purge.
"""

from __future__ import annotations

import threading
import time

import circusvoip_phone_urgence as U


class UrgenceStore:
    """Signaux actifs et prise de service. Tout en memoire, sous verrou.

    Le verrou est le meme pour les deux : un signal se cree en fonction
    de qui est en service, et les deux etats bougent depuis des
    connexions differentes. Deux verrous distincts laisseraient une
    fenetre ou l'on cree un signal pour quelqu'un qui vient de quitter
    son service.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # numero -> signal. UNE demande par joueur au plus : c'est la
        # regle metier, et l'indexer par auteur la rend impossible a
        # violer plutot que simplement interdite.
        self._signaux: dict[str, dict] = {}
        # numero -> role, pour les detenteurs EN SERVICE seulement.
        self._en_service: dict[str, str] = {}

    # -- prise de service ----------------------------------------------

    def prendre_service(self, numero, role, est_chef=False) -> bool:
        """Met un detenteur de role en service.

        Un chef n'a pas a pointer : il est en service des qu'il est
        connecte. L'appelant le declare a la connexion, et
        valide_prise_service() refuse qu'il le fasse a la main.
        """
        numero = str(numero)
        role = U.valide_role(role)
        with self._lock:
            self._en_service[numero] = role
            return True

    def quitter_service(self, numero) -> bool:
        """Retire du service, et relache les signaux pris.

        Les deux vont ensemble : un secouriste hors service n'est plus en
        route, et laisser son nom sur un signal ferait croire a la
        victime que quelqu'un vient. C'est le pire affichage possible --
        elle cesserait de chercher une autre solution.
        """
        numero = str(numero)
        with self._lock:
            parti = self._en_service.pop(numero, None) is not None
            for sig in self._signaux.values():
                U.relacher(sig, numero)
            return parti

    def est_en_service(self, numero) -> bool:
        with self._lock:
            return str(numero) in self._en_service

    def role_en_service(self, numero):
        with self._lock:
            return self._en_service.get(str(numero))

    def numeros_en_service(self, role=None) -> list:
        """Numeros en service, filtres par role.

        Le filtrage par role est fait ICI plutot que chez l'appelant :
        c'est le meme calcul pour la diffusion d'un signal et pour la
        liste des collegues, et le dupliquer garantirait qu'un jour les
        deux divergent -- sans erreur visible, juste des signaux qui
        n'arrivent pas.
        """
        with self._lock:
            if role is None:
                return list(self._en_service.keys())
            r = str(role).strip().lower()
            return [n for n, v in self._en_service.items() if v == r]

    def quelqu_un_dispo(self, type_urgence) -> bool:
        """Au moins un detenteur du role vise est-il en service ?

        Interroge AVANT la capture de position cote client : inutile de
        depenser six secondes d'OCR pour apprendre que personne
        n'ecoute.
        """
        voulu = U.ROLE_DESTINATAIRE.get(U.valide_type(type_urgence))
        return bool(self.numeros_en_service(voulu))

    # -- signaux --------------------------------------------------------

    def creer(self, numero, type_urgence, position, texte="") -> dict:
        """Cree le signal, ou leve UrgenceError.

        La disponibilite est verifiee ICI, pas seulement chez le client :
        un client bricole -- ou simplement en retard d'une seconde --
        pourrait creer un signal que personne ne recevrait, et la victime
        attendrait devant un ecran qui promet des secours.
        """
        numero = str(numero)
        type_urgence = U.valide_type(type_urgence)
        with self._lock:
            self._purger_verrouille()
            if numero in self._signaux:
                raise U.UrgenceError("Vous avez déjà une demande en cours.")
            if not self.quelqu_un_dispo(type_urgence):
                raise U.UrgenceError(
                    "Aucun secouriste n'est actuellement disponible.")
            sig = U.cree_signal(numero, type_urgence, position, texte)
            self._signaux[numero] = sig
            return dict(sig)

    def actualiser_position(self, numero, position) -> dict:
        """Repositionne une demande existante.

        Un blesse bouge encore : il fuit, il rampe vers un abri. La
        position figee au declenchement decrit alors un endroit qu'il a
        quitte. Il decide quand il peut la reprendre -- rafraichir tout
        seul supposerait qu'il est immobile et disponible, soit l'inverse
        du cas d'usage.
        """
        numero = str(numero)
        with self._lock:
            sig = self._signaux.get(numero)
            if sig is None or sig.get("etat") != U.ETAT_ACTIF:
                raise U.UrgenceError("Aucune demande en cours.")
            sig["position"] = position
            return dict(sig)

    def ma_demande(self, numero):
        with self._lock:
            self._purger_verrouille()
            sig = self._signaux.get(str(numero))
            return dict(sig) if sig else None

    def visibles(self, role, numero_lecteur=None) -> list:
        """Signaux qu'un detenteur de ce role doit voir.

        Cloisonnement STRICT par type : un medecin ne voit pas les
        signaux de securite. Une agression produit souvent les deux
        besoins -- c'est a la victime de declencher deux signaux si elle
        le juge utile, pas au code de le decider.

        Chaque entree porte deux drapeaux plutot que la liste des
        preneurs : `pris` (quelqu'un s'en occupe) et `mien` (moi). Le
        NOMBRE n'est jamais expose -- il n'aide pas a decider et il
        revele qui fait quoi.
        """
        r = str(role or "").strip().lower()
        moi = str(numero_lecteur) if numero_lecteur is not None else None
        out = []
        with self._lock:
            self._purger_verrouille()
            for sig in self._signaux.values():
                if not U.est_visible(sig, r):
                    continue
                preneurs = sig.get("preneurs") or []
                out.append({
                    "id": sig.get("id"),
                    "type": sig.get("type"),
                    "texte": sig.get("texte"),
                    "position": sig.get("position"),
                    "cree_le": sig.get("cree_le"),
                    "pris": bool(preneurs),
                    "mien": bool(moi and moi in preneurs),
                })
        out.sort(key=lambda s: s.get("cree_le") or 0.0)
        return out

    def _par_id(self, sig_id):
        for sig in self._signaux.values():
            if sig.get("id") == sig_id:
                return sig
        return None

    def prendre(self, sig_id, numero, role) -> dict:
        numero = str(numero)
        with self._lock:
            self._purger_verrouille()
            sig = self._par_id(sig_id)
            if sig is None:
                raise U.UrgenceError("Cette demande n'existe plus.")
            U.prendre(sig, numero, str(role or "").strip().lower(),
                      self.est_en_service(numero))
            return dict(sig)

    def relacher(self, sig_id, numero) -> dict:
        with self._lock:
            sig = self._par_id(sig_id)
            if sig is None:
                raise U.UrgenceError("Cette demande n'existe plus.")
            U.relacher(sig, str(numero))
            return dict(sig)

    def clore(self, sig_id, numero) -> dict:
        with self._lock:
            sig = self._par_id(sig_id)
            if sig is None:
                raise U.UrgenceError("Cette demande n'existe plus.")
            U.clore(sig, str(numero))
            auteur = str(sig.get("auteur") or "")
            self._signaux.pop(auteur, None)
            return dict(sig)

    def abandonner(self, numero) -> dict | None:
        """La victime retire sa demande.

        Rend le signal pour que l'appelant previenne les preneurs : sans
        ca, un secouriste continuerait de voler vers un signal mort.
        """
        numero = str(numero)
        with self._lock:
            sig = self._signaux.pop(numero, None)
            return dict(sig) if sig else None

    # -- deconnexion et purge -------------------------------------------

    def deconnecter(self, numero) -> dict | None:
        """Un joueur se deconnecte : sa demande meurt, son service aussi.

        Deconnecte = signal abandonne. Rien n'attend, rien ne se met en
        file. Le signal rendu permet de prevenir ceux qui l'avaient pris.
        """
        numero = str(numero)
        with self._lock:
            sig = self._signaux.pop(numero, None)
            self._en_service.pop(numero, None)
            for autre in self._signaux.values():
                U.relacher(autre, numero)
            return dict(sig) if sig else None

    def purger(self, maintenant=None) -> list:
        """Retire les signaux expires. Rend ceux qui viennent d'expirer.

        Appelee periodiquement par le serveur. Le retour permet de
        prevenir les victimes : sans notification, leur ecran resterait
        sur une demande que le serveur ne connait plus, et elles
        attendraient indefiniment.
        """
        with self._lock:
            return self._purger_verrouille(maintenant)

    def _purger_verrouille(self, maintenant=None) -> list:
        ts = float(maintenant if maintenant is not None else time.time())
        expires = []
        for num, sig in list(self._signaux.items()):
            if U.est_expire(sig, ts) or sig.get("etat") != U.ETAT_ACTIF:
                expires.append(dict(sig))
                self._signaux.pop(num, None)
        return expires

    def stats(self) -> dict:
        with self._lock:
            return {
                "signaux": len(self._signaux),
                "en_service": len(self._en_service),
            }
