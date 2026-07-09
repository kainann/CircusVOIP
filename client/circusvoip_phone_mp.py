# -*- coding: utf-8 -*-
"""
circusvoip_phone_mp
===================

Couche MULTIJOUEUR partagee des jeux du CircusPhone (v0.3).

But : un SEUL endroit qui gere le flux commun "Solo / Multi -> Creer /
Rejoindre -> Lobby (attente + pret) -> Lancement", pour que Poker,
SolVsTerra et Billard n'aient PAS chacun leur propre code reseau. Les
jeux ne connaissent que cette classe ; ils ne parlent jamais au serveur
en direct.

Transport : tout passe par le serveur de positions CircusVOIP (port 8888),
via `send_ws(dict) -> bool` (= _core._ws_send_safe, thread-safe) pour
l'emission, et via `handle_server_msg(data)` que l'overlay appelle quand
un message `mp_*` arrive (cf. dispatch client). PAS de Bluetooth/LAN : la
"proximite" (joueurs a <=30 m) est un FILTRE calcule par le SERVEUR a
partir des positions qu'il connait deja (clients[ws]["pos"]).

Perimetre par jeu (passe a la construction via `scope`) :
  - SCOPE_SERVER     : tout le serveur (SolVsTerra) ;
  - SCOPE_PROXIMITY  : joueurs proches in-game, <=30 m (Poker, Billard).

Cette classe est game-agnostique : elle gere le LOBBY et le RELAIS d'events
de partie (send_game / sig_game). La logique de regles vit dans chaque jeu.

NB : aucune dependance a l'overlay ni au monolithe. QObject/Signal pour
s'integrer au reste de Qt ; aucun import lourd.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal


# ----------------------------------------------------------------------
#  Constantes de protocole (types de messages mp_*). Documentees en detail
#  dans le doc d'architecture livre a cote. Garder ces chaines synchronisees
#  avec le handler serveur.
# ----------------------------------------------------------------------
# Client -> Serveur
MSG_CREATE = "mp_create"   # {game, scope, params}
MSG_LIST   = "mp_list"     # {game, scope}            -> demande la liste
MSG_JOIN   = "mp_join"     # {lobby_id}
MSG_READY  = "mp_ready"    # {lobby_id, ready}
MSG_START  = "mp_start"    # {lobby_id}               (hote uniquement)
MSG_LEAVE  = "mp_leave"    # {lobby_id}
MSG_GAME   = "mp_game"     # {lobby_id, payload}      relais d'event de partie

# Serveur -> Client
MSG_LOBBY_UPDATE = "mp_lobby_update"  # {lobby_id, game, scope, host,
                                      #  members:[{name,ready}], params, state}
MSG_LOBBY_LIST   = "mp_lobby_list"    # {game, lobbies:[{lobby_id,host,count,
                                      #  max,params}]}
MSG_STARTED      = "mp_started"       # {lobby_id, seed, order:[names], params}
MSG_GAME_EVENT   = "mp_game"          # {lobby_id, from, payload}
MSG_ERROR        = "mp_error"         # {code, msg}
MSG_CLOSED       = "mp_closed"        # {lobby_id, reason}

# Perimetres
SCOPE_SERVER = "server"
SCOPE_PROXIMITY = "proximity"

# Etats de lobby cote client (miroir de l'autorite serveur)
ST_IDLE = "idle"          # pas de session
ST_BROWSING = "browsing"  # ecran "Rejoindre" : liste demandee
ST_LOBBY = "lobby"        # dans un lobby, en attente
ST_PLAYING = "playing"    # partie lancee


class MpLobby(QObject):
    """Session multijoueur d'UN jeu. Une instance par session active.

    Signaux (l'app du jeu s'y abonne) :
      sig_list(list)     : lobbies ouverts recus (ecran Rejoindre).
                           Chaque item = {lobby_id, host, count, max, params}.
      sig_lobby(dict)    : snapshot du lobby courant (roster + prets + etat).
      sig_started(dict)  : la partie demarre (seed deterministe + ordre des
                           joueurs + params). Le jeu bascule en 'playing'.
      sig_game(dict)     : event de partie relaye par un pair ({from, payload}).
      sig_error(str,str) : (code, message) ex. ('lobby_full','...').
      sig_closed(str)    : le lobby est dissous (raison).

    Cycle type cote app :
      mp = MpLobby("poker", SCOPE_PROXIMITY, services.send_ws, my_name)
      mp.create({...}) | mp.refresh_list()->mp.join(id)
      mp.set_ready(True) ; quand tous prets -> sig_started -> jeu en reseau
      mp.send_game(payload) / sig_game pour l'echange d'events de partie
      mp.leave() a la sortie.
    """

    sig_list    = Signal(list)
    sig_lobby   = Signal(dict)
    sig_started = Signal(dict)
    sig_game    = Signal(dict)
    sig_error   = Signal(str, str)
    sig_closed  = Signal(str)

    def __init__(self, game_id: str, scope: str,
                 send_ws: Optional[Callable[[dict], bool]],
                 my_name: str, parent=None):
        super().__init__(parent)
        self.game_id = game_id
        self.scope = scope if scope in (SCOPE_SERVER, SCOPE_PROXIMITY) else SCOPE_SERVER
        self._send_ws = send_ws or (lambda _m: False)
        self.my_name = my_name or ""
        # Etat courant (autorite = serveur ; ici on garde le dernier snapshot)
        self.state = ST_IDLE
        self.lobby_id: Optional[str] = None
        self.host: Optional[str] = None
        self.members: list = []        # [{name, ready}]
        self.params: dict = {}

    # ------------------------------------------------------------------
    #  Helpers d'etat
    # ------------------------------------------------------------------
    def is_host(self) -> bool:
        return bool(self.host) and self.host == self.my_name

    def all_ready(self) -> bool:
        """True si >=2 joueurs et TOUS prets (condition de lancement)."""
        if len(self.members) < 2:
            return False
        return all(m.get("ready") for m in self.members)

    def me(self) -> Optional[dict]:
        for m in self.members:
            if m.get("name") == self.my_name:
                return m
        return None

    # ------------------------------------------------------------------
    #  Actions (Client -> Serveur)
    # ------------------------------------------------------------------
    def create(self, params: Optional[dict] = None) -> bool:
        self.params = dict(params or {})
        ok = self._tx({"type": MSG_CREATE, "game": self.game_id,
                       "scope": self.scope, "params": self.params})
        # L'etat reel sera confirme par mp_lobby_update ; on note l'intention.
        if ok:
            self.state = ST_LOBBY
        return ok

    def refresh_list(self) -> bool:
        self.state = ST_BROWSING
        return self._tx({"type": MSG_LIST, "game": self.game_id,
                         "scope": self.scope})

    def join(self, lobby_id: str) -> bool:
        return self._tx({"type": MSG_JOIN, "lobby_id": lobby_id})

    def set_ready(self, ready: bool) -> bool:
        if not self.lobby_id:
            return False
        return self._tx({"type": MSG_READY, "lobby_id": self.lobby_id,
                         "ready": bool(ready)})

    def toggle_ready(self) -> bool:
        m = self.me()
        return self.set_ready(not (m and m.get("ready")))

    def start(self) -> bool:
        """Demande de lancement (hote). Le serveur verifie all_ready + min."""
        if not self.lobby_id or not self.is_host():
            return False
        return self._tx({"type": MSG_START, "lobby_id": self.lobby_id})

    def leave(self) -> bool:
        if not self.lobby_id:
            self._reset()
            return True
        ok = self._tx({"type": MSG_LEAVE, "lobby_id": self.lobby_id})
        self._reset()
        return ok

    def send_game(self, payload: dict) -> bool:
        """Relaie un event de partie aux autres membres (via le serveur)."""
        if not self.lobby_id:
            return False
        return self._tx({"type": MSG_GAME, "lobby_id": self.lobby_id,
                         "payload": payload})

    # ------------------------------------------------------------------
    #  Reception (Serveur -> Client). Appele par le dispatch de l'overlay
    #  pour tout message dont le type commence par "mp_".
    # ------------------------------------------------------------------
    def handle_server_msg(self, data: dict) -> bool:
        """Traite un message mp_*. Retourne True si consomme.

        Filtre par jeu/lobby pour ignorer les messages d'une autre session
        (au cas ou plusieurs apps partageraient le flux)."""
        if not isinstance(data, dict):
            return False
        t = data.get("type")
        if not isinstance(t, str) or not t.startswith("mp_"):
            return False

        if t == MSG_LOBBY_LIST:
            if data.get("game") != self.game_id:
                return False
            self.sig_list.emit(list(data.get("lobbies") or []))
            return True

        if t == MSG_LOBBY_UPDATE:
            # Adopte le snapshot serveur comme verite.
            self.lobby_id = data.get("lobby_id")
            self.host = data.get("host")
            self.members = list(data.get("members") or [])
            self.params = dict(data.get("params") or self.params)
            self.state = ST_LOBBY
            self.sig_lobby.emit({
                "lobby_id": self.lobby_id, "host": self.host,
                "members": self.members, "params": self.params,
                "is_host": self.is_host(), "all_ready": self.all_ready(),
            })
            return True

        if t == MSG_STARTED:
            if data.get("lobby_id") not in (None, self.lobby_id):
                return False
            self.state = ST_PLAYING
            self.sig_started.emit({
                "lobby_id": self.lobby_id,
                "seed": data.get("seed"),
                "order": list(data.get("order") or []),
                "params": dict(data.get("params") or self.params),
            })
            return True

        if t == MSG_GAME_EVENT:
            if data.get("lobby_id") not in (None, self.lobby_id):
                return False
            self.sig_game.emit({"from": data.get("from"),
                                "payload": data.get("payload")})
            return True

        if t == MSG_ERROR:
            self.sig_error.emit(str(data.get("code") or "error"),
                                str(data.get("msg") or ""))
            return True

        if t == MSG_CLOSED:
            if data.get("lobby_id") not in (None, self.lobby_id):
                return False
            reason = str(data.get("reason") or "closed")
            self._reset()
            self.sig_closed.emit(reason)
            return True

        return False

    # ------------------------------------------------------------------
    #  Interne
    # ------------------------------------------------------------------
    def _tx(self, msg: dict) -> bool:
        try:
            return bool(self._send_ws(msg))
        except Exception:
            return False

    def _reset(self):
        self.state = ST_IDLE
        self.lobby_id = None
        self.host = None
        self.members = []
