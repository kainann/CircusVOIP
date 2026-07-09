# -*- coding: utf-8 -*-
"""
circusvoip_mp_server
====================

Gestionnaire de LOBBIES multijoueurs cote serveur (port 8888).

Concu comme une logique PURE et testable, independante de websockets :
le serveur lui fournit deux callables (positions + joueurs en ligne), et
chaque handler RENVOIE la liste des messages a envoyer `[(dest_name, dict)]`.
Le serveur n'a plus qu'a `await _send_to_name(dest, msg)` pour chacun. Aucun
import reseau ici -> testable en isolation (cf. self-test en bas).

Decisions v0.3 (validees) :
  - Lancement AUTO : des que tous les membres (>=2) sont "prets", la partie
    part toute seule (mp_started a tous). `on_start` (hote) reste accepte
    comme repli/manuel mais n'est pas necessaire.
  - Buy-in Poker = jetons internes (params.buy_in libre, AUCUN lien Wallet).
  - Eloignement > 30 m en partie : on ne fait RIEN (la partie continue).

Perimetre (`scope`) :
  - "server"     : SolVsTerra — tout le serveur, pas de filtre distance.
  - "proximity"  : Poker/Billard — hote et joueurs a <= MP_PROX_RADIUS_M.

Une seule partie a la fois par joueur : create/join refuses si deja engage.
"""

from __future__ import annotations

import math
import secrets
from typing import Callable, Optional

MP_PROX_RADIUS_M = 30.0

# Rayon de proximite par jeu (metres). Le billard veut "MEME table" -> serre.
# Les autres jeux : rayon par defaut.
GAME_RADII = {
    "billard": 8.0,
}

# Capacites par jeu : (min, max). min = nb mini pour lancer.
GAME_CAPS = {
    "solvsterra": (2, 2),   # 1v1 strict (bataille navale : 2 flottes)
    "poker":      (2, 8),
    "billard":    (2, 2),
}
DEFAULT_CAP = (2, 8)

SCOPE_SERVER = "server"
SCOPE_PROXIMITY = "proximity"


def _dist(a: Optional[dict], b: Optional[dict]) -> Optional[float]:
    """Distance euclidienne entre deux positions {x,y,z}. None si manquant."""
    if not a or not b:
        return None
    try:
        return math.sqrt((a["x"] - b["x"]) ** 2
                         + (a["y"] - b["y"]) ** 2
                         + (a["z"] - b["z"]) ** 2)
    except (KeyError, TypeError):
        return None


def _within(a: Optional[dict], b: Optional[dict], radius: float) -> bool:
    """True si a et b sont 'proches' (meme lieu ET distance <= radius). Les
    coords x,y,z sont LOCALES au lieu, donc on exige d'abord la PREUVE du
    MEME lieu :
      - container_id present des DEUX cotes -> ils doivent etre EGAUX
        (le plus precis : hangars, vaisseaux, trams) ;
      - sinon zone presente des DEUX cotes -> elles doivent etre EGALES
        (interieurs de station sans cid ; 'ObjectContainer_Commercial' est
        partagee par plusieurs stations, mais un rayon serre + les coords
        distinguent la bonne table) ;
      - sinon, si une info de lieu n'existe que d'UN cote (asymetrie), on
        REFUSE : impossible de prouver le meme lieu, et comparer des coords
        locales de deux reperes differents n'a aucun sens. (Bug 03/07/2026 :
        un client sans zone/cid utiles pouvait rejoindre une partie de
        proximite depuis un autre lieu, la distance brute tombant par
        coincidence sous le rayon de 30 m.)
      - la distance seule ne sert de dernier recours que si AUCUNE info de
        lieu n'existe nulle part (tres vieux clients).
    NB : les chaines VIDES comptent comme ABSENTES ("" n'est pas un lieu)."""
    if not a or not b:
        return False
    ca = a.get("container_id") or None
    cb = b.get("container_id") or None
    za = a.get("zone") or None
    zb = b.get("zone") or None
    if ca is not None and cb is not None:
        if ca != cb:
            return False
    elif za is not None and zb is not None:
        if za != zb:
            return False
    elif ca or cb or za or zb:
        # Info de lieu asymetrique : pas de preuve du meme lieu -> refus.
        return False
    d = _dist(a, b)
    return d is not None and d <= radius


class MpServer:
    """Registre de lobbies + handlers mp_*. Tous les handlers renvoient une
    liste de (dest_name, message_dict) a emettre par le serveur."""

    def __init__(self,
                 pos_of: Callable[[str], Optional[dict]],
                 online_names: Callable[[], list],
                 radius_m: float = MP_PROX_RADIUS_M):
        self._pos_of = pos_of
        self._online = online_names
        self.radius = radius_m
        # lobby_id -> dict(game, scope, host, members:{name:{"ready":bool}},
        #                  params, state)
        self.lobbies: dict = {}
        # name -> lobby_id (une partie a la fois)
        self._lobby_of: dict = {}

    # ------------------------------------------------------------------
    #  Handlers (appeles par le dispatch du serveur). Retour: [(name,msg)]
    # ------------------------------------------------------------------
    def on_create(self, name, game, scope, params):
        if self._lobby_of.get(name):
            return [self._err(name, "already_in", "Deja dans une partie.")]
        if game not in GAME_CAPS and game is not None:
            pass  # jeux inconnus tolere (caps par defaut)
        scope = scope if scope in (SCOPE_SERVER, SCOPE_PROXIMITY) else SCOPE_SERVER
        lobby_id = "L" + secrets.token_hex(4)
        self.lobbies[lobby_id] = {
            "game": game, "scope": scope, "host": name,
            "members": {name: {"ready": False}},
            "params": dict(params or {}), "state": "waiting",
        }
        self._lobby_of[name] = lobby_id
        return self._update_msgs(lobby_id)

    def on_list(self, name, game, scope):
        scope = scope if scope in (SCOPE_SERVER, SCOPE_PROXIMITY) else SCOPE_SERVER
        out = []
        for lid, lo in self.lobbies.items():
            if lo["game"] != game or lo["state"] != "waiting":
                continue
            cnt = len(lo["members"])
            _, mx = GAME_CAPS.get(game, DEFAULT_CAP)
            if cnt >= mx:
                continue
            # Filtre proximite : hote dans le MEME container et a <= rayon(jeu).
            if lo["scope"] == SCOPE_PROXIMITY:
                r = GAME_RADII.get(game, self.radius)
                if not _within(self._pos_of(name), self._pos_of(lo["host"]), r):
                    continue
            out.append({"lobby_id": lid, "host": lo["host"],
                        "count": cnt, "max": mx, "params": lo["params"]})
        return [(name, {"type": "mp_lobby_list", "game": game, "lobbies": out})]

    def on_join(self, name, lobby_id):
        lo = self.lobbies.get(lobby_id)
        if lo is None or lo["state"] != "waiting":
            return [self._err(name, "not_found", "Partie introuvable.")]
        if self._lobby_of.get(name) and self._lobby_of[name] != lobby_id:
            return [self._err(name, "already_in", "Deja dans une partie.")]
        if name in lo["members"]:
            return self._update_msgs(lobby_id)  # idempotent
        _, mx = GAME_CAPS.get(lo["game"], DEFAULT_CAP)
        if len(lo["members"]) >= mx:
            return [self._err(name, "lobby_full", "Partie pleine.")]
        if lo["scope"] == SCOPE_PROXIMITY:
            r = GAME_RADII.get(lo["game"], self.radius)
            if not _within(self._pos_of(name), self._pos_of(lo["host"]), r):
                msg = ("Va au même billard que l'hôte."
                       if lo["game"] == "billard"
                       else "Hôte trop loin / autre lieu.")
                return [self._err(name, "too_far", msg)]
        lo["members"][name] = {"ready": False}
        self._lobby_of[name] = lobby_id
        return self._update_msgs(lobby_id)

    def on_ready(self, name, lobby_id, ready):
        lo = self.lobbies.get(lobby_id)
        if lo is None or name not in lo["members"]:
            return [self._err(name, "not_found", "Partie introuvable.")]
        lo["members"][name]["ready"] = bool(ready)
        msgs = self._update_msgs(lobby_id)
        # Lancement AUTO : tous prets + >= min.
        msgs += self._maybe_autostart(lobby_id)
        return msgs

    def on_start(self, name, lobby_id):
        """Repli manuel (hote). Avec l'auto-start, rarement utile."""
        lo = self.lobbies.get(lobby_id)
        if lo is None:
            return [self._err(name, "not_found", "Partie introuvable.")]
        if lo["host"] != name:
            return [self._err(name, "not_host", "Seul l'hote peut lancer.")]
        started = self._maybe_autostart(lobby_id, force=True)
        if not started:
            return [self._err(name, "not_ready", "Joueurs pas tous prets.")]
        return started

    def on_leave(self, name, lobby_id=None):
        lobby_id = lobby_id or self._lobby_of.get(name)
        lo = self.lobbies.get(lobby_id)
        if lo is None:
            self._lobby_of.pop(name, None)
            return []
        return self._remove_member(lobby_id, name)

    def on_game(self, name, lobby_id, payload):
        lo = self.lobbies.get(lobby_id)
        if lo is None or name not in lo["members"]:
            return []
        # Relaie aux AUTRES membres.
        ev = {"type": "mp_game", "lobby_id": lobby_id,
              "from": name, "payload": payload}
        return [(m, ev) for m in lo["members"] if m != name]

    def on_disconnect(self, name):
        """A appeler quand un joueur se deconnecte (hook existant du serveur)."""
        lobby_id = self._lobby_of.get(name)
        if not lobby_id:
            return []
        return self._remove_member(lobby_id, name)

    # ------------------------------------------------------------------
    #  Interne
    # ------------------------------------------------------------------
    def _members_list(self, lo):
        return [{"name": n, "ready": d["ready"]} for n, d in lo["members"].items()]

    def _update_msgs(self, lobby_id):
        lo = self.lobbies.get(lobby_id)
        if lo is None:
            return []
        snap = {"type": "mp_lobby_update", "lobby_id": lobby_id,
                "game": lo["game"], "scope": lo["scope"], "host": lo["host"],
                "members": self._members_list(lo), "params": lo["params"],
                "state": lo["state"]}
        return [(m, snap) for m in lo["members"]]

    def _maybe_autostart(self, lobby_id, force=False):
        lo = self.lobbies.get(lobby_id)
        if lo is None or lo["state"] != "waiting":
            return []
        mn, _ = GAME_CAPS.get(lo["game"], DEFAULT_CAP)
        members = lo["members"]
        if len(members) < mn:
            return []
        if not force and not all(d["ready"] for d in members.values()):
            return []
        if force and not all(d["ready"] for d in members.values()):
            return []
        lo["state"] = "playing"
        order = list(members.keys())
        seed = secrets.randbelow(2**31)
        started = {"type": "mp_started", "lobby_id": lobby_id,
                   "seed": seed, "order": order, "params": lo["params"]}
        return [(m, started) for m in order]

    def _remove_member(self, lobby_id, name):
        lo = self.lobbies.get(lobby_id)
        if lo is None:
            self._lobby_of.pop(name, None)
            return []
        lo["members"].pop(name, None)
        self._lobby_of.pop(name, None)
        # Hote parti OU plus assez de monde -> on dissout.
        if name == lo["host"] or not lo["members"]:
            remaining = list(lo["members"].keys())
            self.lobbies.pop(lobby_id, None)
            for m in remaining:
                self._lobby_of.pop(m, None)
            reason = "host_left" if name == lo["host"] else "empty"
            return [(m, {"type": "mp_closed", "lobby_id": lobby_id,
                         "reason": reason}) for m in remaining]
        # Sinon : juste un membre en moins -> update aux restants.
        return self._update_msgs(lobby_id)

    @staticmethod
    def _err(name, code, msg):
        return (name, {"type": "mp_error", "code": code, "msg": msg})


# ----------------------------------------------------------------------
#  Self-test headless (aucun websocket). Lance : python3 circusvoip_mp_server.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    POS = {"alice": {"x": 0, "y": 0, "z": 0},
           "bob":   {"x": 5, "y": 0, "z": 0},     # 5 m d'alice
           "carol": {"x": 50, "y": 0, "z": 0}}    # 50 m d'alice
    srv = MpServer(pos_of=lambda n: POS.get(n),
                   online_names=lambda: list(POS.keys()))

    def names(msgs):
        return [(d, m["type"]) for d, m in msgs]

    # Poker proximite : alice cree
    m = srv.on_create("alice", "poker", "proximity", {"buy_in": 500})
    lid = m[0][1]["lobby_id"]
    print("create:", names(m), "lobby", lid)

    # carol (50 m) liste -> ne doit PAS voir le lobby d'alice
    m = srv.on_list("carol", "poker", "proximity")
    print("list carol (loin):", m[0][1]["lobbies"])
    assert m[0][1]["lobbies"] == [], "carol ne devrait rien voir"

    # bob (5 m) liste -> doit voir
    m = srv.on_list("bob", "poker", "proximity")
    print("list bob (proche):", [l["lobby_id"] for l in m[0][1]["lobbies"]])
    assert len(m[0][1]["lobbies"]) == 1

    # carol tente de rejoindre -> too_far
    m = srv.on_join("carol", lid)
    print("join carol:", names(m))
    assert m[0][1]["code"] == "too_far"

    # bob rejoint -> update a alice+bob
    m = srv.on_join("bob", lid)
    print("join bob:", names(m))
    assert len(m) == 2

    # prets -> auto-start
    srv.on_ready("alice", lid, True)
    m = srv.on_ready("bob", lid, True)
    types = [t for _, t in names(m)]
    print("ready bob -> :", names(m))
    assert "mp_started" in types, "doit auto-demarrer"
    started = [msg for _, msg in m if msg["type"] == "mp_started"][0]
    print("  seed:", started["seed"], "order:", started["order"])

    # relais d'event
    m = srv.on_game("alice", lid, {"move": 42})
    print("game relay:", names(m))
    assert m and m[0][0] == "bob"

    # deconnexion hote -> lobby ferme pour bob
    m = srv.on_disconnect("alice")
    print("disconnect alice:", names(m))
    assert m and m[0][1]["type"] == "mp_closed"
    print("\nOK — tous les asserts passent.")
