#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[QUEUE 03/08/2026] Messagerie differee + anti-spam -- SERVEUR uniquement.

Bloc 1 du chantier "messagerie/appels 24h/24" (§6 octies). Volontairement
SANS reseau : ce module ne fait que le modele de donnees, les plafonds et
les verdicts. Le serveur appelle et applique ; lui seul touche aux
WebSockets. Meme decoupage que circusvoip_accounts.py, et pour la meme
raison : ce fichier est testable seul, sans VPS et sans serveur qui tourne.

--- Ce qui est stocke, et sous quelle cle ---

Une file par DESTINATAIRE, nommee par son NUMERO : "425874.json".

Le numero, jamais le pseudo. Le routage du serveur travaille en pseudo
(_send_to_name), mais un pseudo est un affichage : il peut changer, et une
file "Kainan.json" qui attend trois semaines deviendrait introuvable. Le
numero est attribue une fois et ne bouge plus. C'est le meme defaut que
les photos de profil rangees par pseudo, connu et a corriger ailleurs ; on
ne le reproduit pas ici.

Consequence : la resolution numero -> pseudo se fait au REJEU, pas au
stockage.

--- Ce qui n'est PAS stocke ---

Un numero inattribue ne cree pas de file. Cote serveur, "numero inconnu"
et "joueur hors ligne" tombent aujourd'hui dans la meme branche
silencieuse ; ici ils se separent, sinon on materialiserait sur disque des
numeros qui n'existent pas -- et une file qui grossit pour un numero
inexistant est un vecteur de saturation gratuit.

--- Rejeu et acquittement ---

Les evenements ne sont retires QU'APRES acquittement du client (ack()).
Une deconnexion en cours de rejeu ne doit rien perdre. Tant que le client
n'acquitte pas, la file reste pleine : c'est voulu, mais ca implique un
repli cote serveur pour les clients anciens qui n'acquitteront jamais
(sinon leur file rejoue les memes evenements a chaque connexion). Ce repli
n'est PAS dans ce module : il releve du bloc 3.

--- Plafonds (decisions du 02-03/08) ---

- 20 Mo par joueur. Plafond atteint -> REFUS, jamais eviction du plus
  ancien : sinon un envoi en masse effacerait les vrais messages avant que
  le destinataire ne se connecte, et le flood deviendrait un outil de
  suppression.
- Compte sur le BASE64 tel qu'il est stocke, pas sur le binaire d'origine
  (sinon le compteur annonce 30 % de moins que l'occupation disque).
- Retention 30 jours depuis le MESSAGE, pas depuis la derniere connexion :
  sinon un joueur absent six mois revient a six mois d'arriere et le
  stockage n'a plus de borne superieure.

--- Anti-spam : DEUX compteurs, pas un ---

Les octets protegent la MACHINE, le nombre protege le JOUEUR. Un seul ne
suffit pas : 10 Mo de texte, c'est ~17 000 messages, soit 60 par seconde
chez la victime. Le serveur tient, la personne visee est noyee.

Fenetre glissante attachee au COMPTE, pas a la session : sinon un script
se reconnecte entre deux salves et n'atteint jamais l'escalade.

Escalade : refus -> silence 10 min -> deconnexion -> blocage de compte.
La deconnexion existe parce qu'un script qui recoit un refus recommence en
boucle, et chaque refus coute du traitement. Le blocage de compte est le
seul palier qui arrete une machine, et il n'est possible que parce que
REQUIRE_ACCOUNT est actif : un spammeur ne peut pas etre anonyme.

Le blocage de compte est RENDU comme verdict, il n'est pas ecrit ici : il
doit survivre a un systemctl restart, donc il va dans le store des comptes
(deja persiste) et non dans un fichier de plus. Les compteurs de fenetre,
eux, restent en RAM : un redemarrage les remet a zero et cinq minutes
perdues sont sans importance.

--- Exemptions ---

Le mannequin et le test de charge emettent a un rythme non humain par
nature. Sans exemption ils se font bloquer par leur propre serveur, et on
perd du temps a comprendre pourquoi. Passer leurs numeros dans
`exempts=`.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

# ---------------------------------------------
#  Plafonds -- valeurs arretees le 03/08/2026
# ---------------------------------------------

# 20 Mo par joueur. Repere : ~22 images pleine taille (900 Ko de base64),
# 80 a 140 images typiques, ou ~35 000 textes.
MAX_QUEUE_BYTES = 20 * 1024 * 1024

# Retention comptee depuis l'horodatage de l'evenement.
RETENTION_SECONDS = 30 * 24 * 3600

# Debit en OCTETS : protege le serveur. 10 Mo / 5 min laisse passer une
# trentaine d'images typiques -- hors d'atteinte d'un usage RP.
RATE_BYTES_LIMIT  = 10 * 1024 * 1024
RATE_BYTES_WINDOW = 300.0

# Debit en NOMBRE : protege le destinataire. 30 messages / min est tres
# au-dessus d'un rythme humain et tres en dessous d'un script.
RATE_COUNT_LIMIT  = 30
RATE_COUNT_WINDOW = 60.0

# Rejeu : nombre d'envois avant abandon d'un evenement non acquitte.
# Vise les clients anterieurs a l'ack, qui ne repondront jamais.
MAX_TENTATIVES_REJEU = 3

# Escalade.
REFUS_AVANT_SILENCE = 3       # refus dans la fenetre -> silence
SILENCE_SECONDS     = 600.0   # 10 min, decision du 02/08
TENTATIVES_AVANT_KICK = 3     # envois pendant le silence -> deconnexion
KICKS_AVANT_BLOCAGE   = 2     # deconnexions -> blocage de compte
BLOCAGE_SECONDS       = 2 * 3600.0   # borne: un faux positif ne doit JAMAIS
                                     # exclure quelqu'un definitivement sans
                                     # decision humaine.

# Types d'evenements admis dans la file.
KIND_MSG    = "msg"
KIND_IMG    = "img"
KIND_MISSED = "missed_call"
_KINDS = (KIND_MSG, KIND_IMG, KIND_MISSED)

# Verdicts rendus par RateLimiter.check().
V_OK      = "ok"
V_REFUS   = "refus"
V_SILENCE = "silence"
V_KICK    = "kick"
V_BLOCAGE = "blocage"


def _now() -> float:
    return time.time()


def _numero_valide(numero) -> str | None:
    """Normalise un numero en chaine de chiffres, ou None.

    Garde-fou de nommage de fichier autant que de validation : le numero
    sert de NOM DE FICHIER, donc tout ce qui n'est pas un entier decimal
    est refuse avant d'approcher le disque.
    """
    if numero is None:
        return None
    s = str(numero).strip()
    return s if s.isdigit() and 0 < len(s) <= 12 else None


# =============================================
#  File differee
# =============================================

class QueueStore:
    """Files differees, une par numero de destinataire.

    Format d'un fichier (phone_queue/425874.json) :
        {"numero": "425874",
         "events": [{"id": "...", "ts": 1754.., "kind": "msg",
                     "from": "428431", "body": "...", "bytes": 612}, ...]}

    Le total en octets est RECALCULE au chargement plutot que stocke : un
    compteur persiste peut deriver de son contenu (ecriture interrompue,
    edition a la main), et un plafond qui se croit atteint sans l'etre est
    invisible et bloque tout.
    """

    def __init__(self, path: Path | str | None = None):
        self._dir = Path(path) if path else Path("phone_queue")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ---- disque ----

    def _fichier(self, numero: str) -> Path:
        return self._dir / f"{numero}.json"

    def _load(self, numero: str) -> list:
        f = self._fichier(numero)
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            ev = data.get("events")
            return ev if isinstance(ev, list) else []
        except Exception:
            # Fichier corrompu : on ne le supprime pas (il peut contenir
            # des messages recuperables a la main) et on ne fait pas
            # tomber le serveur. La file est traitee comme vide, ce qui
            # refuse les nouveaux envois plutot que de les perdre.
            return []

    def _save(self, numero: str, events: list) -> bool:
        """Ecriture ATOMIQUE. Un serveur tue en plein write laisserait
        sinon un JSON tronque, donc une file entiere perdue."""
        f = self._fichier(numero)
        try:
            if not events:
                # Plus rien a garder : on retire le fichier au lieu de
                # laisser un squelette vide par numero ayant deja recu
                # quoi que ce soit.
                if f.exists():
                    f.unlink()
                return True
            tmp = f.with_suffix(f.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"numero": numero, "events": events},
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
            os.replace(tmp, f)
            return True
        except Exception:
            return False

    # ---- lecture ----

    def taille(self, numero) -> int:
        """Octets occupes par la file d'un joueur."""
        num = _numero_valide(numero)
        if num is None:
            return 0
        return sum(int(e.get("bytes") or 0) for e in self._load(num))

    def en_attente(self, numero) -> list:
        """Evenements en attente, du plus ancien au plus recent."""
        num = _numero_valide(numero)
        if num is None:
            return []
        ev = self._load(num)
        ev.sort(key=lambda e: e.get("ts") or 0.0)
        return ev

    def conversations(self, numero) -> list:
        """Evenements groupes PAR EMETTEUR, pour le rejeu conversation par
        conversation (§6 octies). Les groupes sont rendus dans l'ordre
        d'arrivee du premier evenement de chacun, et chaque groupe est
        complet : on vide une conversation avant de passer a la suivante.
        """
        groupes: dict[str, list] = {}
        for e in self.en_attente(numero):
            groupes.setdefault(str(e.get("from") or ""), []).append(e)
        return list(groupes.values())

    # ---- ecriture ----

    def enqueue(self, dest_numero, kind: str, from_numero, body: str = "",
                ts: float | None = None) -> tuple[str, dict | None]:
        """Ajoute un evenement. Rend (etat, evenement).

        etat vaut "ok", "plein" (plafond atteint -> l'emetteur DOIT etre
        prevenu, un refus silencieux lui laisse croire qu'il a ete
        delivre), "invalide" (numero mal forme ou type inconnu) ou
        "erreur" (echec disque).

        `body` est stocke TEL QUEL : pour une image, c'est le base64, sans
        re-encodage. Le re-encodage JPEG reste une decision interne au
        serveur, ajoutable plus tard sans toucher au protocole.
        """
        num = _numero_valide(dest_numero)
        src = _numero_valide(from_numero)
        if num is None or src is None or kind not in _KINDS:
            return "invalide", None
        if num == src:
            return "invalide", None          # auto-envoi

        body = body if isinstance(body, str) else ""
        ev = {
            "id":    uuid.uuid4().hex[:12],
            "ts":    float(ts if ts is not None else _now()),
            "kind":  kind,
            "from":  src,
            "body":  body,
            # Poids compte sur ce qui est REELLEMENT stocke.
            "bytes": len(body.encode("utf-8")) + 120,   # +metadonnees
        }

        events = self._load(num)
        total = sum(int(e.get("bytes") or 0) for e in events)
        if total + ev["bytes"] > MAX_QUEUE_BYTES:
            return "plein", None

        events.append(ev)
        return ("ok", ev) if self._save(num, events) else ("erreur", None)

    def ack(self, numero, event_ids) -> int:
        """Retire les evenements acquittes. Rend le nombre retire.

        Rien n'est retire avant acquittement : une deconnexion en cours de
        rejeu doit pouvoir reprendre ou elle s'est arretee.
        """
        num = _numero_valide(numero)
        if num is None:
            return 0
        ids = {str(i) for i in (event_ids or [])}
        if not ids:
            return 0
        events = self._load(num)
        restants = [e for e in events if str(e.get("id")) not in ids]
        retires = len(events) - len(restants)
        if retires:
            self._save(num, restants)
        return retires

    def marquer_envoye(self, numero, event_ids) -> list:
        """Compte une tentative de rejeu. Rend les ids ABANDONNES.

        Repli pour les clients anciens, qui ne connaissent pas l'ack et ne
        l'enverront jamais : sans ce compteur, leur file ne se viderait
        jamais et rejouerait les memes evenements a CHAQUE connexion. Au
        bout de MAX_TENTATIVES_REJEU envois, on retire quand meme.

        C'est un compromis assume : on prefere perdre un evenement chez un
        client ancien plutot que de le lui resservir indefiniment.
        """
        num = _numero_valide(numero)
        if num is None:
            return []
        ids = {str(i) for i in (event_ids or [])}
        if not ids:
            return []
        events = self._load(num)
        abandonnes = []
        restants = []
        for e in events:
            if str(e.get("id")) in ids:
                e["tries"] = int(e.get("tries") or 0) + 1
                if e["tries"] >= MAX_TENTATIVES_REJEU:
                    abandonnes.append(str(e.get("id")))
                    continue
            restants.append(e)
        self._save(num, restants)
        return abandonnes

    def vider(self, numero) -> int:
        """Vide la file d'un joueur (outil d'admin). Rend le nb retire."""
        num = _numero_valide(numero)
        if num is None:
            return 0
        n = len(self._load(num))
        self._save(num, [])
        return n

    # ---- entretien ----

    def purge(self, maintenant: float | None = None) -> list:
        """Retire les evenements expires. Rend la liste des suppressions,
        sous forme (numero, nb_evenements, octets).

        RENVOIE ce qu'elle supprime au lieu de le loguer elle-meme : sans
        trace on ne saura jamais si la purge tourne, mais le logging est
        au serveur, pas a ce module (qui doit rester testable seul).

        Appelee par une boucle asyncio toutes les 24 h ET au demarrage,
        pour rattraper les arrets prolonges.
        """
        now = maintenant if maintenant is not None else _now()
        limite = now - RETENTION_SECONDS
        supprimes = []
        try:
            fichiers = sorted(self._dir.glob("*.json"))
        except Exception:
            return supprimes
        for f in fichiers:
            num = f.stem
            if _numero_valide(num) is None:
                continue
            events = self._load(num)
            if not events:
                continue
            restants = [e for e in events if (e.get("ts") or 0.0) >= limite]
            if len(restants) == len(events):
                continue
            perdus = [e for e in events if (e.get("ts") or 0.0) < limite]
            octets = sum(int(e.get("bytes") or 0) for e in perdus)
            if self._save(num, restants):
                supprimes.append((num, len(perdus), octets))
        return supprimes

    def stats(self) -> dict:
        """Etat global, pour l'admin et les logs."""
        files = 0
        events = 0
        octets = 0
        try:
            for f in self._dir.glob("*.json"):
                if _numero_valide(f.stem) is None:
                    continue
                ev = self._load(f.stem)
                if not ev:
                    continue
                files += 1
                events += len(ev)
                octets += sum(int(e.get("bytes") or 0) for e in ev)
        except Exception:
            pass
        return {"files": files, "events": events, "bytes": octets}


# =============================================
#  Anti-spam
# =============================================

class RateLimiter:
    """Fenetre glissante par NUMERO, en octets ET en nombre.

    Etat volontairement en RAM : un redemarrage remet les fenetres a zero,
    et cinq minutes perdues sont sans importance. Seul le BLOCAGE de
    compte doit survivre a un restart -- il est rendu comme verdict et
    persiste par l'appelant dans le store des comptes.

    L'escalade se lit dans check() : chaque palier ne se declenche que si
    le precedent n'a pas suffi.
    """

    def __init__(self, exempts=None):
        # numero -> {"envois": [(ts, octets)], "refus": [ts],
        #            "silence_jusqua": ts, "tentatives": int, "kicks": int}
        self._etat: dict[str, dict] = {}
        # Mannequin, test de charge, comptes de service : ils emettent a un
        # rythme non humain PAR NATURE.
        self._exempts = {str(x) for x in (exempts or [])}

    def exempter(self, numero):
        self._exempts.add(str(numero))

    def _fiche(self, numero: str) -> dict:
        f = self._etat.get(numero)
        if f is None:
            f = {"envois": [], "refus": [], "silence_jusqua": 0.0,
                 "tentatives": 0, "kicks": 0}
            self._etat[numero] = f
        return f

    def check(self, from_numero, octets: int,
              maintenant: float | None = None) -> tuple[str, float]:
        """Verdict AVANT envoi. Rend (verdict, duree).

        - "ok"      : router normalement
        - "refus"   : ne pas router, prevenir l'emetteur
        - "silence" : refuser pendant `duree` secondes
        - "kick"    : fermer la WebSocket (un script qui recoit un refus
                      recommence en boucle, et chaque refus coute)
        - "blocage" : bloquer le COMPTE pendant `duree` secondes, a
                      persister par l'appelant

        Un verdict autre que "ok" ne consomme PAS de quota : sinon un
        emetteur deja bloque continuerait a alimenter sa propre fenetre et
        ne sortirait jamais du silence.
        """
        num = _numero_valide(from_numero)
        if num is None:
            return V_REFUS, 0.0
        if num in self._exempts:
            return V_OK, 0.0

        now = maintenant if maintenant is not None else _now()
        f = self._fiche(num)

        # --- deja sous silence ? ---
        if now < f["silence_jusqua"]:
            f["tentatives"] += 1
            if f["tentatives"] >= TENTATIVES_AVANT_KICK:
                f["tentatives"] = 0
                f["kicks"] += 1
                if f["kicks"] >= KICKS_AVANT_BLOCAGE:
                    f["kicks"] = 0
                    return V_BLOCAGE, BLOCAGE_SECONDS
                return V_KICK, 0.0
            return V_SILENCE, f["silence_jusqua"] - now

        # --- purge des fenetres ---
        f["envois"] = [(t, o) for (t, o) in f["envois"]
                       if now - t <= RATE_BYTES_WINDOW]
        f["refus"] = [t for t in f["refus"] if now - t <= RATE_BYTES_WINDOW]

        octets = max(0, int(octets or 0))
        total_octets = sum(o for (_t, o) in f["envois"]) + octets
        nb_recents = sum(1 for (t, _o) in f["envois"]
                         if now - t <= RATE_COUNT_WINDOW) + 1

        depasse = (total_octets > RATE_BYTES_LIMIT
                   or nb_recents > RATE_COUNT_LIMIT)
        if not depasse:
            f["envois"].append((now, octets))
            return V_OK, 0.0

        # --- refus, et escalade si repete ---
        f["refus"].append(now)
        if len(f["refus"]) >= REFUS_AVANT_SILENCE:
            f["refus"] = []
            f["tentatives"] = 0
            f["silence_jusqua"] = now + SILENCE_SECONDS
            return V_SILENCE, SILENCE_SECONDS
        return V_REFUS, 0.0

    def reset(self, numero):
        """Leve les compteurs d'un joueur (decision d'admin)."""
        self._etat.pop(str(numero), None)

    def entretien(self, maintenant: float | None = None) -> int:
        """Retire les fiches inactives. Rend le nombre retire.

        Sans ca, une fenetre purgee seulement a l'envoi laisserait en
        memoire les compteurs de joueurs partis depuis longtemps, et le
        dictionnaire grossirait indefiniment. A appeler dans le meme
        passage que QueueStore.purge().
        """
        now = maintenant if maintenant is not None else _now()
        morts = []
        for num, f in self._etat.items():
            dernier = max([t for (t, _o) in f["envois"]] + f["refus"]
                          + [f["silence_jusqua"]] or [0.0])
            if now - dernier > RATE_BYTES_WINDOW and now >= f["silence_jusqua"]:
                morts.append(num)
        for num in morts:
            self._etat.pop(num, None)
        return len(morts)

    def stats(self) -> dict:
        return {"suivis": len(self._etat), "exempts": len(self._exempts)}
