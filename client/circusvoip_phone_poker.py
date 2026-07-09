# -*- coding: utf-8 -*-
"""
circusvoip_phone_poker
======================

Application « Poker » du CircusPhone (v0.3) : Texas Hold'em au contrat
`PhoneApp`, en LOCAL contre des bots, par-dessus le moteur pur
circusvoip_phone_poker_engine.

Le moteur reste PUR : ici on ne fait que (a) lui envoyer tes actions au
clavier, (b) faire jouer les bots via ai_decide, et (c) dessiner l'état.

Réseau VOLONTAIREMENT absent pour l'instant : le multijoueur (Poker mais
aussi Valakkar et Sol VS Terra) passera par une COUCHE RESEAU PARTAGEE a
construire plus tard. Le jour venu, ce sont les sieges non-humains et
l'orchestration de partie (_bot_step / _arm_turn_timer / distribution) qui
seront pilotes par le serveur a la place des bots locaux — le moteur, lui,
ne bougera pas. La structure actuelle (sieges Player(is_human=...),
ai_decide isolable) est faite pour ce remplacement.

Jeu : capture clavier brute (CAPTURES_KEYBOARD=True). Deux timers — un
single-shot pour l'action des bots, un continu (200 ms) pour le compte a
rebours du tour ; on_hide les met en pause, on_show les relance si besoin.

À ton tour :
    F            se coucher
    V / Entrée   checker ou suivre
    Haut / Bas   ajuster le montant de relance
    R            relancer (au montant affiché)
    T            tapis (all-in)
Lobby (avant la partie) : Haut/Bas changent le réglage ciblé, Gauche/Droite
le règlent, Entrée lance. Entre les mains : Entrée distribue la suivante.

Dépendances : circusvoip_phone_apps (PhoneApp/PhoneServices) et
circusvoip_phone_poker_engine. Le bloc __main__ est un harnais supprimable.
"""

from __future__ import annotations

import random
import time
import sys

from PySide6.QtCore import Qt, QTimer, QRectF, QEvent, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont, QFontMetrics, QKeyEvent, QGuiApplication,
    QPainterPath,
)
from PySide6.QtWidgets import QApplication, QWidget

import circusvoip_phone_poker_engine as pe
from circusvoip_phone_apps import PhoneApp, PhoneServices
try:
    import circusvoip_phone_mp as mp
except Exception:               # module absent -> multijoueur desactive,
    mp = None                   # mais le jeu (et le telephone) restent OK.


# ======================================================================
# Palette
# ======================================================================
_BG        = "#0d1117"
_FELT      = "#123524"   # tapis vert
_FELT_2    = "#0f2c1e"
_PANEL     = "#161b22"
_BORDER    = "#30363d"
_TEXT      = "#c9d1d9"
_MUTED     = "#6e7681"
_ACCENT    = "#3fb950"
_GOLD      = "#d4a72c"
_CHIP      = "#58a6ff"
_RED       = "#f85149"
_CARD_BG   = "#f2efe6"
_CARD_BACK = "#1f4a8a"
_CARD_EDGE = "#b9b29f"
_SUIT_RED  = "#c0392b"
_SUIT_BLK  = "#16202b"
_OVERLAY   = "#0d1117dd"

_PHONE_BODY_COLOR   = "#1a1a1a"
_PHONE_BTN_COLOR    = "#0a0a0a"
_PHONE_BANNER_GREY  = "#888888"
_PHONE_BANNER_WHITE = "#ffffff"

_BOT_DELAY_MS = 650


def fmt_auec(n):
    """Formate un montant en aUEC avec séparateur de milliers (espace)."""
    return f"{int(n):,}".replace(",", " ") + " aUEC"


# ======================================================================
# IA des bots (côté "client", pas dans le moteur pur)
# ======================================================================
def ai_decide(game, seat):
    p = game.players[seat]
    la = game.legal_actions()
    if not la:
        return "check", 0
    rng = game.rng
    if game.community:
        score = pe.best_score(p.hole + game.community)
        strength = min(0.97, score[0] / 8.0 * 0.75 + 0.12)
    else:
        r = sorted((p.hole[0][0], p.hole[1][0]), reverse=True)
        suited = p.hole[0][1] == p.hole[1][1]
        if r[0] == r[1]:
            strength = 0.50 + (r[0] - 2) / 24.0
        else:
            strength = (r[0] + r[1]) / 40.0
            if suited:
                strength += 0.08
            if r[0] - r[1] == 1:
                strength += 0.05
            if r[0] >= 13:
                strength += 0.05
        strength = min(0.95, strength)

    to_call = la.get("to_call", 0)
    pot = sum(pl.contributed for pl in game.players)
    rnd = rng.random()

    def raise_amt():
        return min(la["max_raise_to"], la["min_raise_to"] + game.bb * 2)

    if to_call == 0:
        if strength > 0.62 and la.get("raise") and rnd < 0.55:
            return "raise", raise_amt()
        return "check", 0
    pot_odds = to_call / (pot + to_call + 1e-9)
    if strength < 0.30 and rnd < 0.85:
        return "fold", 0
    if strength > 0.80 and la.get("raise") and rnd < 0.5:
        return "raise", raise_amt()
    if strength > pot_odds * 0.85 or rnd < 0.15:
        return "call", 0
    return "fold", 0


# ======================================================================
# Widget de jeu
# ======================================================================
class PokerApp(PhoneApp):
    """Poker (Texas Hold'em) du CircusPhone, en LOCAL contre des bots.
    Deux timers (action bot + chrono de tour) mis en pause via on_hide et
    relances via on_show. Concu pour qu'un futur serveur remplace les bots
    et l'orchestration sans toucher au moteur pur."""

    APP_ID = "poker"
    APP_NAME = "Poker"
    APP_ICON = "\u2660"              # ♠
    CAPTURES_KEYBOARD = True         # jeu : l'overlay route les touches brutes

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.resize(int(screen_w), int(screen_h))
        self.setFocusPolicy(Qt.StrongFocus)
        self._screen_radius = int(screen_radius)

        # Pause : retient quels timers tournaient quand on a cache l'app, pour
        # les relancer au retour (sinon partie figee / tour bloque).
        self._paused = False
        self._bot_was_active = False
        self._turn_was_active = False

        self.game = pe.HoldemGame(["Toi", "Bot 1", "Bot 2", "Bot 3"],
                                  stacks=1000, sb=10, bb=20,
                                  rng=random.Random())
        self._raise_to = 0
        self._bot_timer = QTimer(self)
        self._bot_timer.setSingleShot(True)
        self._bot_timer.timeout.connect(self._bot_step)

        # Chrono de tour : 20 s par joueur, sinon action par défaut (check/fold).
        self.TURN_SECONDS = 20.0
        self._time_left = 0.0
        self._turn_timer = QTimer(self)
        self._turn_timer.setInterval(200)
        self._turn_timer.timeout.connect(self._turn_tick)

        # Orchestration de PARTIE (vit ici, pas dans le moteur pur ; en multi
        # ce sera le serveur qui tiendra ce rôle) : une partie = N mains.
        self._hand_presets = [5, 10, 20, 50]
        self._preset_idx = 1                 # défaut : 10 mains
        self._hands_total = self._hand_presets[self._preset_idx]
        self._hand_no = 0
        # Mise : 1 jeton = 1 aUEC (monnaie de Star Citizen). Choix de la cave
        # (buy-in) au lobby ; les blindes en découlent.
        self._stake_presets = [
            (5000, 25, 50), (10000, 50, 100),
            (25000, 100, 200), (50000, 250, 500),
        ]
        self._stake_idx = 1
        self._buyin = self._stake_presets[self._stake_idx][0]
        # Nombre de joueurs (toi + bots). Le moteur gère 2..N ; on borne
        # l'UI à 6 pour rester lisible sur le téléphone.
        self._player_presets = [2, 3, 4, 5, 6]
        self._players_idx = 2                # défaut : 4 joueurs
        self._lobby_focus = 0                # 0=mains, 1=mise, 2=joueurs
        self._lobby_for_mp = False           # l'ecran d'options sert-il a CREER
                                             # une partie multi ? (sinon = solo)
        self._mo_scroll = 0.0                # défilement de l'écran de fin
        self._mo_max_scroll = 0.0
        # Menu d'accueil + choix du mode, en amont du lobby.
        self._menu_items = ["Jouer", "Règles", "Quitter"]
        self._menu_index = 0
        self._menu_rects = []
        self._mode_items = ["Solo", "Multijoueur"]
        self._mode_index = 0
        self._mode_rects = []
        # Ecran multijoueur (apres "Multijoueur") : creer / rejoindre une partie.
        self._mp_items = ["Créer une partie", "Rejoindre une partie"]
        self._mp_index = 0
        self._mp_rects = []
        # --- Session multijoueur (lobby reseau partage) ---
        self._mp = None                 # MpLobby (cree a la demande)
        self._mp_list = []              # parties ouvertes (ecran mp_browse)
        self._mp_list_idx = 0
        self._mp_roster = []            # [{name, ready}] du lobby courant
        self._mp_host = None
        self._mp_notice = ""            # message transitoire (erreur / info)
        # --- Synchro de partie en reseau (lockstep deterministe) ---
        self._is_net = False            # partie en cours = reseau ?
        self._applying_remote = False   # garde anti-rebroadcast d'une action recue
        self._net_seat = None           # mon siege en reseau (0 = hote/autorite chrono)
        self._net_advance_pending = False  # enchainement de main programme ?
        # menu | mode | mp | mp_browse | mp_lobby | rules | lobby | playing | match_over
        self._phase = "menu"

    # --- Cycle de vie (contrat PhoneApp) ------------------------------
    def on_show(self):
        """Devient l'ecran courant : on revient TOUJOURS au menu d'accueil
        (ouvrir le jeu depuis la liste d'apps doit afficher son menu, pas
        reprendre un etat precedent). Prend le focus clavier ; timers stoppes."""
        self._paused = False
        self._bot_timer.stop()
        self._turn_timer.stop()
        self._bot_was_active = False
        self._turn_was_active = False
        # Si un salon multi etait actif, le quitter (on repart au menu).
        if self._mp is not None and getattr(self._mp, "lobby_id", None):
            try:
                self._mp.leave()
            except Exception:
                pass
        self._mp_roster = []
        self._mp_host = None
        self._mp_notice = ""
        self._is_net = False
        self._applying_remote = False
        self._net_advance_pending = False
        self._lobby_for_mp = False
        self._phase = "menu"
        self._menu_index = 0
        self.setFocus()
        self.update()

    def handle_back(self) -> bool:
        """Retour (Echap) : Regles/Mode -> menu ; partie ou lobby -> menu (on
        arrete les timers) ; menu -> non consomme (l'overlay revient au home)."""
        if self._phase in ("rules", "mode"):
            self._phase = "menu"
            self.update()
            return True
        if self._phase == "mp":
            self._phase = "mode"        # retour au choix Solo / Multijoueur
            self.update()
            return True
        if self._phase == "mp_browse":
            self._mp_notice = ""
            self._phase = "mp"
            self.update()
            return True
        if self._phase == "mp_lobby":
            if self._mp:
                self._mp.leave()
            self._mp_roster = []
            self._mp_host = None
            self._mp_notice = ""
            self._phase = "mp"
            self.update()
            return True
        if self._phase in ("lobby", "playing", "match_over"):
            self._bot_timer.stop()
            self._turn_timer.stop()
            if self._phase == "lobby" and self._lobby_for_mp:
                # On annule la creation multi -> retour a l'ecran creer/rejoindre.
                self._lobby_for_mp = False
                self._phase = "mp"
                self.update()
                return True
            self._phase = "menu"
            self._menu_index = 0
            self.update()
            return True
        return False

    def _menu_activate(self):
        """Active le bouton selectionne du menu d'accueil."""
        idx = self._menu_index
        if idx == 0:            # Jouer -> ecran de choix du mode (Solo/Multijoueur)
            self._mode_index = 0
            self._phase = "mode"
            self.update()
        elif idx == 1:          # Règles
            self._phase = "rules"
            self.update()
        elif idx == 2:          # Quitter -> home du telephone
            try:
                self.sig_request_home.emit()
            except Exception:
                pass

    def _lobby_rows(self):
        """Nombre de reglages affiches : 3 en solo (mains/mise/joueurs) ; 2 en
        creation multi (mains/mise — le nombre de joueurs vient des humains qui
        rejoignent le salon)."""
        return 2 if self._lobby_for_mp else 3

    def _mode_activate(self):
        """Active le mode choisi (Solo / Multijoueur)."""
        if self._mode_index == 0:       # Solo -> lobby (reglages) puis partie
            self._lobby_for_mp = False
            self._lobby_focus = 0
            self._phase = "lobby"
            self.update()
        else:                           # Multijoueur -> ecran creer/rejoindre
            self._mp_index = 0
            self._phase = "mp"
            self.update()

    def handle_server_msg(self, data) -> bool:
        """Route un message serveur mp_* vers la session multijoueur. Appele
        par l'overlay (dispatch reseau) quand le Poker est l'app courante."""
        if self._mp is None:
            return False
        try:
            return bool(self._mp.handle_server_msg(data))
        except Exception:
            return False

    def _mp_get(self):
        """Cree (a la demande) la session multijoueur partagee et branche ses
        signaux. Poker = proximite (joueurs a <=30 m). Retourne None si le
        module multijoueur est absent (multi desactive proprement)."""
        if mp is None:
            return None
        if self._mp is not None:
            return self._mp
        send_ws = getattr(self.services, "send_ws", None)
        my_name = getattr(self.services, "my_name", None) or "Toi"
        self._mp = mp.MpLobby("poker", mp.SCOPE_PROXIMITY, send_ws, my_name, self)
        self._mp.sig_list.connect(self._on_mp_list)
        self._mp.sig_lobby.connect(self._on_mp_lobby)
        self._mp.sig_started.connect(self._on_mp_started)
        self._mp.sig_game.connect(self._on_mp_game)
        self._mp.sig_error.connect(self._on_mp_error)
        self._mp.sig_closed.connect(self._on_mp_closed)
        return self._mp

    def _mp_activate(self):
        """Creer / rejoindre une partie de proximite (joueurs a <=30 m)."""
        m = self._mp_get()
        if m is None:               # module multijoueur absent
            self._mp_notice = "Multijoueur indisponible (module manquant)."
            self.update()
            return
        self._mp_notice = ""
        if self._mp_index == 0:          # Créer -> ecran d'options PUIS creation
            self._lobby_for_mp = True
            self._lobby_focus = 0
            self._phase = "lobby"
        else:                            # Rejoindre
            self._mp_list = []
            self._mp_list_idx = 0
            m.refresh_list()
            self._phase = "mp_browse"
        self.update()

    def _mp_create_with_options(self):
        """Cree le salon multi avec les reglages choisis dans l'ecran d'options
        (mains / cave / blindes). Le nombre de joueurs vient des humains qui
        rejoignent. Les params sont transmis a tous via le snapshot serveur."""
        m = self._mp_get()
        if m is None:
            self._mp_notice = "Multijoueur indisponible (module manquant)."
            self._phase = "mp"
            self.update()
            return
        buyin, sb, bb = self._stake_presets[self._stake_idx]
        params = {"buy_in": buyin, "sb": sb, "bb": bb,
                  "hands": self._hand_presets[self._preset_idx]}
        # Affichage optimiste : on se montre seul (hote) en attendant le
        # snapshot serveur (mp_lobby_update).
        self._mp_roster = [{"name": m.my_name, "ready": False}]
        self._mp_host = m.my_name
        m.create(params)
        self._lobby_for_mp = False
        self._phase = "mp_lobby"
        self.update()

    # --- Signaux MpLobby (serveur -> UI) ------------------------------
    def _on_mp_list(self, lobbies):
        self._mp_list = list(lobbies or [])
        if self._mp_list_idx >= len(self._mp_list):
            self._mp_list_idx = max(0, len(self._mp_list) - 1)
        if self._phase == "mp_browse":
            self.update()

    def _on_mp_lobby(self, snap):
        self._mp_roster = list(snap.get("members") or [])
        self._mp_host = snap.get("host")
        if self._phase in ("mp", "mp_browse"):
            self._phase = "mp_lobby"
        self.update()

    def _on_mp_started(self, payload):
        # Premier jet : on lance une partie locale amorcee avec le seed et la
        # table renvoyes par le serveur. Le vrai jeu en reseau (relais des
        # actions via sig_game) viendra ensuite.
        self._start_network_match(payload)

    def _on_mp_error(self, code, msg):
        self._mp_notice = msg or code
        self.update()

    def _on_mp_closed(self, reason):
        self._mp_notice = ("Partie fermée : l'hôte est parti."
                           if reason == "host_left" else "Partie fermée.")
        self._mp_roster = []
        self._mp_host = None
        self._phase = "mp"
        self.update()

    def _start_network_match(self, payload):
        """Demarre la partie a partir du snapshot serveur (seed + ordre).
        Premier jet : moteur local amorce ; la synchro reseau des actions est
        a faire (relais via MpLobby.send_game / sig_game)."""
        order = payload.get("order") or [m["name"] for m in self._mp_roster]
        params = payload.get("params") or {}
        seed = payload.get("seed")
        buyin = params.get("buy_in", self._stake_presets[self._stake_idx][0])
        sb = params.get("sb", 50)
        bb = params.get("bb", 100)
        self._buyin = buyin          # cave reelle de la partie -> base de calcul
                                     # du net (stack - cave) a la fin. Sans ca,
                                     # le solde affiche etait faux (cave par
                                     # defaut au lieu de celle choisie/recue).
        names = list(order) or ["Toi"]
        self._hands_total = params.get("hands", self._hands_total)
        self.game = pe.HoldemGame(names, stacks=buyin, sb=sb, bb=bb,
                                  rng=random.Random(seed))
        # Heros = MON siege (pas le siege 0 = hote). On garde l'ordre des
        # joueurs identique partout (meme seed -> meme distribution par
        # siege) ; seul l'affichage "moi en bas" differe d'un client a
        # l'autre. Sans ca, tout le monde voit l'hote comme etant lui-meme.
        # IMPORTANT : on utilise le nom REELLEMENT enregistre par le lobby
        # (self._mp.my_name), qui figure forcement dans 'order'. Utiliser
        # services.my_name (qui peut etre None -> lobby = "Toi") faisait
        # echouer le match de nom et retomber sur le siege 0 (= l'hote/le
        # mannequin), si bien que le client jouait pour le mauvais siege et la
        # partie n'avancait plus (relances en boucle).
        my_name = getattr(self.services, "my_name", None) \
            or (getattr(self._mp, "my_name", None) if self._mp is not None
                else None) or "Toi"
        # On marque EXACTEMENT UN siege comme "moi". Crucial : si deux joueurs
        # portent le meme nom (ex. tous "Toi" faute de pseudo distinct),
        # marquer tous les sieges correspondants rendrait CHAQUE tour jouable
        # cote client -> on pourrait relancer en boucle sans que la partie
        # avance. On prend donc le PREMIER siege au nom correspondant ; a
        # defaut, l'index dans 'order' ; en dernier recours, le siege 0.
        my_seat = None
        for idx, pl in enumerate(self.game.players):
            if my_seat is None and pl.name == my_name:
                pl.is_human = True
                my_seat = idx
            else:
                pl.is_human = False
        if my_seat is None:
            try:
                my_seat = list(order).index(my_name)
            except (ValueError, IndexError):
                my_seat = 0
            self.game.players[my_seat].is_human = True
        self._net_seat = my_seat
        try:
            self.services.log("[POKER NET] mon siege = %d (%s) sur %s"
                              % (my_seat, my_name, list(order)))
        except Exception:
            pass
        self._is_net = True
        self._applying_remote = False
        self._net_advance_pending = False
        self._hand_no = 0
        self._phase = "playing"
        self._deal_next()

    def on_hide(self):
        """Quitte l'ecran : met la partie en pause (les deux timers). Le
        compte a rebours du tour gele (=_time_left n'est plus decremente
        timer arrete). Idempotent (overlay + hideEvent)."""
        if self._paused:
            return
        self._paused = True
        self._bot_was_active = self._bot_timer.isActive()
        self._turn_was_active = self._turn_timer.isActive()
        self._bot_timer.stop()
        self._turn_timer.stop()

    # ---- orchestration de partie ----
    def _start_match(self):
        self._hands_total = self._hand_presets[self._preset_idx]
        buyin, sb, bb = self._stake_presets[self._stake_idx]
        self._buyin = buyin
        n = self._player_presets[self._players_idx]
        names = ["Toi"] + [f"Bot {i}" for i in range(1, n)]
        self.game = pe.HoldemGame(names, stacks=buyin, sb=sb, bb=bb,
                                  rng=random.Random())
        self._hand_no = 0
        self._phase = "playing"
        self._deal_next()

    def _lobby_change(self, delta):
        # Anti-rebond : la repetition clavier (pynput) peut declencher 2 appels
        # quasi simultanes -> le reglage sauterait de 2 (on ne verrait que 10 et
        # 50 au lieu de 5/10/20/50). On ignore un 2e changement < 120 ms apres
        # le precedent.
        now = time.monotonic()
        if now - getattr(self, "_lobby_last_change", 0.0) < 0.12:
            return
        self._lobby_last_change = now
        if self._lobby_focus == 0:
            self._preset_idx = (self._preset_idx + delta) % len(self._hand_presets)
        elif self._lobby_focus == 1:
            self._stake_idx = (self._stake_idx + delta) % len(self._stake_presets)
        elif not self._lobby_for_mp:     # ligne "joueurs" : solo uniquement
            self._players_idx = (self._players_idx + delta) % len(self._player_presets)
        self.update()

    def _deal_next(self):
        self._hand_no += 1
        if not self.game.start_hand():
            self._phase = "match_over"
            self._mo_scroll = 0.0
            self._turn_timer.stop()
            self.update()
            return
        self._sync_raise()
        self.update()
        self._after_action()

    def _advance_after_hand(self):
        """Validation après une main : main suivante, ou fin de partie."""
        contenders = [p for p in self.game.players if p.stack > 0]
        if self._hand_no >= self._hands_total or len(contenders) < 2:
            self._phase = "match_over"
            self._mo_scroll = 0.0
            self._turn_timer.stop()
            self.update()
        else:
            self._deal_next()

    def _standings(self):
        return sorted(range(len(self.game.players)),
                      key=lambda i: self.game.players[i].stack, reverse=True)

    def _settlement(self):
        """Qui doit envoyer combien à qui (aUEC), pour solder la partie.
        net = stack final - cave. Appariement glouton créditeurs/débiteurs."""
        nets = [(i, self.game.players[i].stack - self._buyin)
                for i in range(len(self.game.players))]
        creditors = sorted([[i, n] for i, n in nets if n > 0],
                            key=lambda x: -x[1])
        debtors = sorted([[i, -n] for i, n in nets if n < 0],
                         key=lambda x: -x[1])
        transfers = []
        ci = di = 0
        while ci < len(creditors) and di < len(debtors):
            cred, dr = creditors[ci], debtors[di]
            amt = min(cred[1], dr[1])
            if amt > 0:
                transfers.append((dr[0], cred[0], amt))   # (de, vers, montant)
            cred[1] -= amt
            dr[1] -= amt
            if cred[1] == 0:
                ci += 1
            if dr[1] == 0:
                di += 1
        return nets, transfers

    # ---- chrono de tour ----
    def _arm_turn_timer(self):
        g = self.game
        if (self._phase == "playing" and not g.hand_over
                and g.current is not None):
            self._time_left = self.TURN_SECONDS
            self._turn_timer.start()
        else:
            self._turn_timer.stop()

    def _turn_tick(self):
        self._time_left -= 0.2
        if self._time_left <= 0:
            self._turn_timer.stop()
            self._on_timeout()
        else:
            self.update()

    def _on_timeout(self):
        g = self.game
        if g.hand_over or g.current is None:
            return
        la = g.legal_actions()
        act = "check" if la.get("check") else "fold"
        if self._is_net:
            # Autorite unique du chrono = l'hote (siege 0), qui possede le
            # moteur. Lui seul agit a l'expiration (pour le joueur courant, que
            # ce soit lui-meme ou un distant absent/inactif) et relaie l'action
            # -> pas de double-couchage entre clients. Les autres clients ne
            # font qu'afficher le decompte.
            if self._net_seat == 0:
                try:
                    self.services.log("[POKER NET] TIMEOUT siege=%s -> %s"
                                      % (g.current, act))
                except Exception:
                    pass
                self._do_action(act)
            return
        # Solo
        if g.players[g.current].is_human:
            g.act(act)
            self._after_action()
        else:
            self._bot_step()


    def _sync_raise(self):
        la = self.game.legal_actions()
        if la and la.get("raise"):
            self._raise_to = la["min_raise_to"]

    def _human_turn(self):
        g = self.game
        return (not g.hand_over and g.current is not None
                and g.players[g.current].is_human)

    def _do_action(self, action, amount=0):
        """Applique une action de jeu au siege courant. En reseau, relaie
        l'action aux autres joueurs (sauf si on applique une action recue)."""
        g = self.game
        if g is None or g.current is None or g.hand_over:
            return
        seat = g.current
        g.act(action, int(amount or 0))
        if self._is_net:
            try:
                self.services.log(
                    "[POKER NET] act %s (%s) seat=%s amount=%s -> current=%s "
                    "over=%s hand=%s" % (
                        action, "DISTANTE" if self._applying_remote else "LOCALE",
                        seat, int(amount or 0), g.current, g.hand_over,
                        self._hand_no))
            except Exception:
                pass
        if self._is_net and self._mp is not None and not self._applying_remote:
            try:
                self._mp.send_game({"k": "act", "action": action,
                                    "amount": int(amount or 0),
                                    "hand": self._hand_no, "seat": seat})
            except Exception:
                pass
        self._after_action()

    def _on_mp_game(self, ev):
        """Action distante recue : on l'applique a l'identique (lockstep).
        Les clients partagent seed + ordre des sieges, donc appliquer la
        meme suite d'actions garde les etats synchronises."""
        if not self._is_net or self.game is None or self._phase != "playing":
            return
        payload = (ev or {}).get("payload") or {}
        kind = payload.get("k")
        if kind == "turn":
            return            # indice de tour : pour clients sans moteur
        if kind != "act":
            return
        # Visibilite : on logue chaque action recue (meme si rejetee ensuite).
        try:
            self.services.log("[POKER NET] action recue de "
                              f"{(ev or {}).get('from')} : {payload.get('action')}")
        except Exception:
            pass
        g = self.game
        if g.hand_over or g.current is None:
            return
        # Defensif : la main ET le siege attendu doivent correspondre.
        if payload.get("hand") != self._hand_no \
                or payload.get("seat") != g.current:
            try:
                self.services.log(
                    "[POKER NET] action IGNOREE (desync) : hand recu=%s "
                    "attendu=%s | seat recu=%s current=%s" % (
                        payload.get("hand"), self._hand_no,
                        payload.get("seat"), g.current))
            except Exception:
                pass
            return
        self._applying_remote = True
        try:
            self._do_action(payload.get("action"),
                            int(payload.get("amount") or 0))
        finally:
            self._applying_remote = False

    def _schedule_net_advance(self):
        """En reseau, programme l'enchainement de la main suivante (une seule
        fois). Tous les clients distribuent la meme main via le rng partage."""
        if self._net_advance_pending:
            return
        self._net_advance_pending = True
        QTimer.singleShot(2800, self._net_auto_advance)

    def _net_auto_advance(self):
        self._net_advance_pending = False
        if (self._is_net and self._phase == "playing"
                and self.game is not None and self.game.hand_over):
            self._advance_after_hand()

    def _after_action(self):
        self.update()
        g = self.game
        if g.hand_over or g.current is None:
            self._turn_timer.stop()
            if self._is_net:
                self._schedule_net_advance()   # enchaine la main suivante
            return
        if self._is_net:
            # Chrono de tour ACTIF en reseau : tous les clients affichent le
            # decompte ; a l'expiration, seul l'hote (siege 0) couche le joueur
            # courant (cf. _on_timeout) -> un joueur absent/inactif ne bloque
            # plus la partie.
            self._arm_turn_timer()
            if g.players[g.current].is_human:
                self._sync_raise()
            else:
                # Tour d'un joueur DISTANT : on lui signale (avec les actions
                # legales) pour les clients sans moteur (ex. mannequin de test).
                if self._mp is not None:
                    try:
                        self._mp.send_game({
                            "k": "turn", "seat": g.current,
                            "hand": self._hand_no,
                            "legal": g.legal_actions(),
                        })
                        self.services.log("[POKER NET] hint TURN -> seat=%s "
                                          "hand=%s" % (g.current, self._hand_no))
                    except Exception:
                        pass
            return
        self._arm_turn_timer()
        if g.players[g.current].is_human:
            self._sync_raise()
        else:
            self._bot_timer.start(_BOT_DELAY_MS)

    def _bot_step(self):
        g = self.game
        if self._is_net:               # aucun bot en reseau
            return
        if g.hand_over or g.current is None or g.players[g.current].is_human:
            return
        action, amount = ai_decide(g, g.current)
        g.act(action, amount)
        self._after_action()

    # ---- entrées ----
    def keyPressEvent(self, event: QKeyEvent):
        k = event.key()
        g = self.game
        confirm = (Qt.Key_Return, Qt.Key_Enter)

        # Menu d'accueil : navigation verticale + validation.
        if self._phase == "menu":
            if k in (Qt.Key_Up,):
                self._menu_index = (self._menu_index - 1) % len(self._menu_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._menu_index = (self._menu_index + 1) % len(self._menu_items)
                self.update()
            elif k in confirm:
                if self._confirm_armed:
                    self._menu_activate()
            return

        # Choix du mode : Solo / Multijoueur.
        if self._phase == "mode":
            if k in (Qt.Key_Up,):
                self._mode_index = (self._mode_index - 1) % len(self._mode_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._mode_index = (self._mode_index + 1) % len(self._mode_items)
                self.update()
            elif k in confirm:
                self._mode_activate()
            elif k == Qt.Key_Backspace:
                self._phase = "menu"
                self.update()
            return

        # Phase "mp" : creer / rejoindre une partie.
        if self._phase == "mp":
            if k in (Qt.Key_Up,):
                self._mp_index = (self._mp_index - 1) % len(self._mp_items)
                self.update()
            elif k in (Qt.Key_Down,):
                self._mp_index = (self._mp_index + 1) % len(self._mp_items)
                self.update()
            elif k in confirm:
                self._mp_activate()
            elif k == Qt.Key_Backspace:
                self._phase = "mode"
                self.update()
            return

        # Phase "mp_browse" : liste des parties a rejoindre.
        if self._phase == "mp_browse":
            n = len(self._mp_list)
            if k in (Qt.Key_Up,) and n:
                self._mp_list_idx = (self._mp_list_idx - 1) % n
                self.update()
            elif k in (Qt.Key_Down,) and n:
                self._mp_list_idx = (self._mp_list_idx + 1) % n
                self.update()
            elif k == Qt.Key_R:                 # rafraichir la liste
                self._mp_notice = ""
                if self._mp:
                    self._mp.refresh_list()
            elif k in confirm and n:
                lid = self._mp_list[self._mp_list_idx].get("lobby_id")
                if lid and self._mp:
                    self._mp_notice = ""
                    self._mp.join(lid)          # -> sig_lobby bascule l'ecran
            elif k == Qt.Key_Backspace:
                self._mp_notice = ""
                self._phase = "mp"
                self.update()
            return

        # Phase "mp_lobby" : roster + Pret. Lancement AUTO (cote serveur)
        # des que tous prets -> sig_started -> phase "playing".
        if self._phase == "mp_lobby":
            if k in confirm:
                if self._mp:
                    self._mp.toggle_ready()     # snapshot serveur rafraichira
            elif k == Qt.Key_Backspace:
                if self._mp:
                    self._mp.leave()
                self._mp_roster = []
                self._mp_host = None
                self._mp_notice = ""
                self._phase = "mp"
                self.update()
            return

        # Ecran Regles : toute validation revient au menu.
        if self._phase == "rules":
            if k in confirm or k == Qt.Key_Backspace:
                self._phase = "menu"
                self.update()
            return

        if self._phase == "lobby":
            nrows = self._lobby_rows()
            if k in (Qt.Key_Up,):
                self._lobby_focus = (self._lobby_focus - 1) % nrows
                self.update()
            elif k in (Qt.Key_Down,):
                self._lobby_focus = (self._lobby_focus + 1) % nrows
                self.update()
            elif k in (Qt.Key_Left,):
                self._lobby_change(-1)
            elif k in (Qt.Key_Right,):
                self._lobby_change(+1)
            elif k in confirm:
                if self._lobby_for_mp:
                    self._mp_create_with_options()
                else:
                    self._start_match()
            return

        if self._phase == "match_over":
            step = self.height() * 0.12
            if k in (Qt.Key_Up,):
                self._mo_scroll = max(0.0, self._mo_scroll - step)
                self.update()
            elif k in (Qt.Key_Down,):
                self._mo_scroll = min(self._mo_max_scroll, self._mo_scroll + step)
                self.update()
            elif k in confirm:
                if self._is_net:
                    # Fin d'une partie RESEAU : on ne relance pas en solo. On
                    # quitte proprement le salon et on revient au menu du Poker.
                    self._is_net = False
                    if self._mp is not None:
                        try:
                            self._mp.leave()
                        except Exception:
                            pass
                    self._phase = "menu"
                    self._menu_index = 0
                else:
                    self._phase = "lobby"      # rejouer en solo (ecran options)
                self.update()
            return

        # phase "playing"
        if g.hand_over:
            # En reseau, l'enchainement est automatique (tous les clients).
            if k in confirm and not self._is_net:
                self._advance_after_hand()
            return
        if not self._human_turn():
            return
        la = g.legal_actions()
        if k == Qt.Key_F:
            self._do_action("fold")
        elif k in (Qt.Key_V,) + confirm:
            # V et non C : C = "s'allonger" chez certains joueurs SC.
            self._do_action("check" if la.get("check") else "call")
        elif k in (Qt.Key_Up,):
            if la.get("raise"):
                self._raise_to = min(la["max_raise_to"],
                                     self._raise_to + g.min_raise)
                self.update()
        elif k in (Qt.Key_Down,):
            if la.get("raise"):
                self._raise_to = max(la["min_raise_to"],
                                     self._raise_to - g.min_raise)
                self.update()
        elif k == Qt.Key_R:
            if la.get("raise"):
                self._do_action("raise", self._raise_to)
        elif k == Qt.Key_T:
            if la.get("raise"):
                self._do_action("raise", la["max_raise_to"])

    def mousePressEvent(self, event):
        """Clic souris sur les ecrans a boutons (menu / mode), ou retour depuis
        les Regles. Les phases de jeu (lobby/playing/match_over) restent au
        clavier."""
        if event.button() != Qt.LeftButton:
            return
        if self._phase == "rules":
            self._phase = "menu"
            self.update()
            return
        if self._phase == "menu":
            pos = event.position()
            for i, r in enumerate(self._menu_rects):
                if r.contains(pos):
                    self._menu_index = i
                    self.update()
                    self._menu_activate()
                    return
            return
        if self._phase == "mode":
            pos = event.position()
            for i, r in enumerate(self._mode_rects):
                if r.contains(pos):
                    self._mode_index = i
                    self.update()
                    self._mode_activate()
                    return
            return
        if self._phase == "mp":
            pos = event.position()
            for i, r in enumerate(self._mp_rects):
                if r.contains(pos):
                    self._mp_index = i
                    self.update()
                    self._mp_activate()
                    return
            return
        super().mousePressEvent(event)

    def hideEvent(self, event):
        # Filet de securite : pause via on_hide si l'app est cachee sans
        # passer par l'overlay (ex. un appel entrant bascule l'ecran).
        self.on_hide()
        super().hideEvent(event)

    def wheelEvent(self, event):
        if self._phase == "match_over" and self._mo_max_scroll > 0:
            d = -event.angleDelta().y() / 120.0 * (self.height() * 0.09)
            self._mo_scroll = min(self._mo_max_scroll, max(0.0, self._mo_scroll + d))
            self.update()
            event.accept()

    # ==================================================================
    # Rendu
    # ==================================================================
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._screen_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()),
                                self._screen_radius, self._screen_radius)
            p.setClipPath(path)
        p.fillRect(self.rect(), QColor(_BG))

        g = self.game
        w, h = self.width(), self.height()
        pad = max(4, int(w * 0.03))

        if self._phase == "menu":
            self._draw_menu(p)
            p.end()
            return
        if self._phase == "mode":
            self._draw_mode(p)
            p.end()
            return
        if self._phase == "mp":
            self._draw_mp(p)
            p.end()
            return
        if self._phase == "mp_browse":
            self._draw_mp_browse(p)
            p.end()
            return
        if self._phase == "mp_lobby":
            self._draw_mp_lobby(p)
            p.end()
            return
        if self._phase == "rules":
            self._draw_rules(p)
            p.end()
            return
        if self._phase == "lobby":
            self._draw_lobby(p)
            p.end()
            return
        if self._phase == "match_over":
            self._draw_match_over(p)
            p.end()
            return

        # --- Sièges adverses (haut) : hauteur adaptative selon leur nombre ---
        opp = [i for i in range(len(g.players)) if not g.players[i].is_human]
        n_opp = max(1, len(opp))
        # On réserve au plus ~44% de l'écran aux adversaires ; chaque siège
        # rétrécit quand ils sont nombreux (jusqu'à 6 joueurs = 5 sièges).
        seat_h = int(min(h * 0.085, (h * 0.44) / n_opp))
        seat_h = max(26, seat_h)
        y = pad
        for idx in opp:
            self._draw_opponent(p, pad, y, w - 2 * pad, seat_h - 4, idx)
            y += seat_h

        # --- Espace restant réparti entre tapis / ton siège / barre ---
        felt_top = y + 2
        remaining = h - felt_top - pad
        felt_h = int(remaining * 0.42)
        felt = QRectF(pad, felt_top, w - 2 * pad, felt_h)
        p.setPen(QPen(QColor(_BORDER), 1))
        p.setBrush(QColor(_FELT))
        p.drawRoundedRect(felt, 14, 14)

        # Pot
        pot = sum(pl.contributed for pl in g.players)
        fp = QFont("Consolas", max(8, int(felt_h * 0.10)))
        fp.setBold(True)
        p.setFont(fp)
        p.setPen(QColor(_GOLD))
        p.drawText(QRectF(felt.x(), felt.y() + 4, felt.width(), felt_h * 0.22),
                   Qt.AlignCenter, f"POT  {pot}")
        # Compteur de mains de la partie (haut-droite du tapis)
        fc = QFont("Consolas", max(7, int(felt_h * 0.075)))
        p.setFont(fc)
        p.setPen(QColor(_MUTED))
        p.drawText(QRectF(felt.x(), felt.y() + 3, felt.width() - 8, felt_h * 0.18),
                   Qt.AlignRight | Qt.AlignTop,
                   f"Main {self._hand_no}/{self._hands_total}")

        # Cartes communes (5 emplacements) — taille calée sur le tapis
        ch = int(felt_h * 0.46)
        cw = int(ch / 1.42)
        maxcw = int(felt.width() / 6.2)
        if cw > maxcw:
            cw, ch = maxcw, int(maxcw * 1.42)
        gapc = (felt.width() - 5 * cw) / 6
        cy = felt.y() + felt_h - ch - felt_h * 0.10
        for i in range(5):
            cx = felt.x() + gapc + i * (cw + gapc)
            card = g.community[i] if i < len(g.community) else None
            self._draw_card(p, cx, cy, cw, ch, card, face_up=card is not None,
                            empty=card is None)

        # --- Ton siège (bas) ---
        you = next(i for i, pl in enumerate(g.players) if pl.is_human)
        yp = g.players[you]
        by = felt_top + felt_h + 6
        # tes cartes
        ycw = int(w * 0.17)
        ych = int(ycw * 1.42)
        reveal_you = True
        cxs = pad + 4
        for j in range(2):
            card = yp.hole[j] if j < len(yp.hole) else None
            self._draw_card(p, cxs + j * (ycw + 6), by, ycw, ych,
                            card, face_up=card is not None, empty=card is None,
                            highlight=(_GOLD if (g.current == you and not g.hand_over) else None))
        # ton stack + mise
        fst = QFont("Consolas", max(8, int(ych * 0.16)))
        fst.setBold(True)
        p.setFont(fst)
        p.setPen(QColor(_TEXT))
        tx = cxs + 2 * (ycw + 6) + 6
        p.drawText(tx, int(by + ych * 0.4), f"{yp.name}")
        p.setPen(QColor(_CHIP))
        p.drawText(tx, int(by + ych * 0.72), f"{yp.stack} jetons")
        if yp.street_bet:
            p.setPen(QColor(_GOLD))
            p.drawText(tx, int(by + ych * 1.0), f"mise {yp.street_bet}")

        # --- Barre d'action / statut (bas) ---
        bar_y = by + ych + 6
        bar = QRectF(pad, bar_y, w - 2 * pad, h - bar_y - pad)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_PANEL))
        p.drawRoundedRect(bar, 8, 8)
        if not g.hand_over and g.current is not None:
            self._draw_timer(p, bar)
        self._draw_actionbar(p, bar, you)
        p.end()

    def _draw_opponent(self, p, x, y, w, h, idx):
        g = self.game
        pl = g.players[idx]
        active = (g.current == idx and not g.hand_over)
        p.setPen(QPen(QColor(_ACCENT if active else _BORDER), 2 if active else 1))
        p.setBrush(QColor(_PANEL if not pl.folded else "#10141a"))
        p.drawRoundedRect(QRectF(x, y, w, h), 7, 7)

        # mini-cartes
        mcw = int(h * 0.46)
        mch = int(mcw * 1.4)
        reveal = g.hand_over and not pl.folded
        for j in range(2):
            card = pl.hole[j] if j < len(pl.hole) else None
            self._draw_card(p, x + 6 + j * (mcw + 3), y + (h - mch) / 2,
                            mcw, mch, card, face_up=reveal and card is not None,
                            empty=not pl.in_hand or pl.folded)
        tx = x + 6 + 2 * (mcw + 3) + 6
        f = QFont("Consolas", max(7, int(h * 0.26)))
        f.setBold(True)
        p.setFont(f)
        nm = pl.name + ("  D" if idx == g.button else "")
        p.setPen(QColor(_MUTED if pl.folded else _TEXT))
        p.drawText(int(tx), int(y + h * 0.42), nm)
        f2 = QFont("Consolas", max(7, int(h * 0.24)))
        p.setFont(f2)
        p.setPen(QColor(_CHIP))
        p.drawText(int(tx), int(y + h * 0.78), f"{pl.stack}")
        # mise + action (à droite)
        if pl.street_bet:
            p.setPen(QColor(_GOLD))
            p.drawText(QRectF(x, y, w - 8, h), Qt.AlignRight | Qt.AlignTop,
                       f" {pl.street_bet}")
        if pl.last_action:
            p.setPen(QColor(_RED if pl.last_action == "couche" else _MUTED))
            p.drawText(QRectF(x, y, w - 8, h), Qt.AlignRight | Qt.AlignBottom,
                       pl.last_action + " ")

    def _draw_actionbar(self, p, bar, you):
        g = self.game
        # Police calée sur la LARGEUR (sinon le texte déborde sur un grand bar).
        size = max(7, min(int(bar.width() * 0.055), int(bar.height() * 0.22)))
        f = QFont("Consolas", size)
        f.setBold(True)
        p.setFont(f)
        if g.hand_over:
            p.setPen(QColor(_TEXT))
            contenders = [pl for pl in g.players if pl.stack > 0]
            last = (self._hand_no >= self._hands_total or len(contenders) < 2)
            nxt = "[Entrée] classement final" if last else "[Entrée] main suivante"
            p.drawText(bar, Qt.AlignCenter | Qt.TextWordWrap, g.message + "\n" + nxt)
            return
        if not self._human_turn():
            p.setPen(QColor(_MUTED))
            cur = g.players[g.current].name if g.current is not None else ""
            txt = f"{g.message}\nAu tour de {cur}…"
            if self._is_net and self._turn_timer.isActive():
                secs = max(0, int(self._time_left + 0.999))
                txt += f"  ({secs}s)"
            p.drawText(bar, Qt.AlignCenter | Qt.TextWordWrap, txt)
            return
        la = g.legal_actions()
        call_lbl = "check" if la.get("check") else f"suivre {la['call_amount']}"
        size = max(7, min(int(bar.width() * 0.044), int(bar.height() * 0.15)))
        f = QFont("Consolas", size)
        f.setBold(True)
        p.setFont(f)
        line1 = f"[F] couche   [V] {call_lbl}"
        if la.get("raise"):
            line2 = f"[R] relance {self._raise_to}   [T] tapis"
            line3 = "Haut/Bas : relance"
        else:
            line2 = "(relance impossible)"
            line3 = ""
        p.setPen(QColor(_ACCENT))
        p.drawText(bar, Qt.AlignCenter, line1 + "\n" + line2 + "\n" + line3)

    def _draw_timer(self, p, bar):
        frac = max(0.0, min(1.0, self._time_left / self.TURN_SECONDS))
        secs = max(0, int(self._time_left + 0.999))
        col = _ACCENT if frac > 0.4 else (_GOLD if frac > 0.2 else _RED)
        sh = max(3.0, bar.height() * 0.06)
        x0, y0, fullw = bar.x() + 6, bar.y() + 4, bar.width() - 12
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_BORDER))
        p.drawRoundedRect(QRectF(x0, y0, fullw, sh), sh / 2, sh / 2)
        if frac > 0:
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(x0, y0, fullw * frac, sh), sh / 2, sh / 2)
        fz = QFont("Consolas", max(7, int(bar.height() * 0.13)))
        fz.setBold(True)
        p.setFont(fz)
        p.setPen(QColor(col))
        p.drawText(QRectF(x0, y0 + sh + 1, fullw, bar.height() * 0.2),
                   Qt.AlignRight | Qt.AlignTop, f"{secs}s")

    def _draw_button_screen(self, p, title, items, sel_index):
        """Ecran a boutons verticaux (titre + liste). Retourne la liste des
        QRectF (pour la souris). Mutualise par le menu et l'ecran de mode."""
        w, h = self.width(), self.height()
        p.setPen(QColor(_ACCENT))
        ft = QFont("Consolas", max(15, w // 9))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.08, w, h * 0.22),
                   int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                   title)
        rects = []
        bw = w * 0.80
        bh = max(32.0, h * 0.10)
        gap = bh * 0.38
        x = (w - bw) / 2.0
        y0 = h * 0.40
        fb = QFont("Consolas", max(9, w // 18))
        fb.setBold(True)
        rad = bh * 0.28
        for i, label in enumerate(items):
            r = QRectF(x, y0 + i * (bh + gap), bw, bh)
            rects.append(r)
            if i == sel_index:
                p.setBrush(QColor(_ACCENT))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor(_BG))
            else:
                p.setBrush(QColor(_PANEL))
                p.setPen(QPen(QColor(_BORDER), 1.5))
                p.drawRoundedRect(r, rad, rad)
                p.setPen(QColor(_TEXT))
            p.setFont(fb)
            p.drawText(r, Qt.AlignCenter, label)
        return rects

    def _draw_menu(self, p):
        """Menu d'accueil : 'Poker' + 4 boutons."""
        self._menu_rects = self._draw_button_screen(
            p, "Poker", self._menu_items, self._menu_index)

    def _draw_mode(self, p):
        """Choix du mode (apres 'Jouer') : Solo / Multijoueur."""
        self._mode_rects = self._draw_button_screen(
            p, "Mode de jeu", self._mode_items, self._mode_index)

    def _draw_mp(self, p):
        """Ecran multijoueur (apres 'Multijoueur') : creer / rejoindre."""
        self._mp_rects = self._draw_button_screen(
            p, "Multijoueur", self._mp_items, self._mp_index)
        w, h = self.width(), self.height()
        p.setPen(QColor(_GOLD if self._mp_notice else _MUTED))
        fh = QFont("Consolas", max(8, w // 26))
        p.setFont(fh)
        p.drawText(QRectF(w * 0.04, h * 0.85, w * 0.92, h * 0.12),
                   int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                   self._mp_notice or "Joueurs à 30 m  ·  jetons internes")

    def _draw_mp_browse(self, p):
        """Liste des parties ouvertes a proximite (Rejoindre)."""
        w, h = self.width(), self.height()
        p.setPen(QColor(_ACCENT))
        ft = QFont("Consolas", max(14, w // 10)); ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.05, w, h * 0.12), Qt.AlignCenter, "Rejoindre")

        if not self._mp_list:
            p.setPen(QColor(_MUTED))
            fm = QFont("Consolas", max(9, w // 20)); p.setFont(fm)
            p.drawText(QRectF(w * 0.08, h * 0.30, w * 0.84, h * 0.40),
                       int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap),
                       "Aucune partie à proximité.\n\n[R] rafraîchir")
        else:
            ry = h * 0.22
            rh = max(48.0, h * 0.17)         # plus haut : titre + 2 lignes
            gap = rh * 0.16
            for i, lo in enumerate(self._mp_list):
                r = QRectF(w * 0.07, ry + i * (rh + gap), w * 0.86, rh)
                sel = (i == self._mp_list_idx)
                p.setBrush(QColor(_ACCENT) if sel else QColor(_PANEL))
                p.setPen(Qt.NoPen if sel else QPen(QColor(_BORDER), 1.5))
                p.drawRoundedRect(r, rh * 0.18, rh * 0.18)
                avail = r.width() - 20
                tx = r.x() + 10

                def _fit(text, base_pt, min_pt, bold):
                    """Plus grande police (>= min_pt) pour que 'text' tienne
                    dans 'avail' ; sinon elide. Pose la police sur le painter
                    et renvoie le texte (eventuellement elide)."""
                    pt = base_pt
                    f = QFont("Consolas", pt); f.setBold(bold)
                    p.setFont(f)
                    while pt > min_pt and \
                            p.fontMetrics().horizontalAdvance(text) > avail:
                        pt -= 1
                        f = QFont("Consolas", pt); f.setBold(bold)
                        p.setFont(f)
                    if p.fontMetrics().horizontalAdvance(text) > avail:
                        return p.fontMetrics().elidedText(
                            text, Qt.ElideRight, int(avail))
                    return text

                cnt = lo.get("count", 1); mx = lo.get("max", 8)
                params = lo.get("params") or {}
                buyin = params.get("buy_in")
                hands = params.get("hands")

                # Titre (adaptatif, gras)
                p.setPen(QColor(_BG) if sel else QColor(_TEXT))
                title = _fit(f"Partie de {lo.get('host', '?')}",
                             max(11, w // 16), 9, True)
                p.drawText(QRectF(tx, r.y() + rh * 0.06, avail, rh * 0.36),
                           int(Qt.AlignLeft | Qt.AlignVCenter), title)

                # Ligne 1 : joueurs
                p.setPen(QColor(_BG) if sel else QColor(_MUTED))
                line1 = _fit(f"{cnt}/{mx} joueurs", max(9, w // 22), 8, False)
                p.drawText(QRectF(tx, r.y() + rh * 0.44, avail, rh * 0.26),
                           int(Qt.AlignLeft | Qt.AlignVCenter), line1)

                # Ligne 2 : manches · cave
                bits = []
                if hands is not None:
                    bits.append(f"{hands} manches")
                if buyin is not None:
                    bits.append(f"cave {fmt_auec(buyin)}")
                if bits:
                    line2 = _fit("  ·  ".join(bits), max(9, w // 22), 8, False)
                    p.drawText(QRectF(tx, r.y() + rh * 0.70, avail, rh * 0.26),
                               int(Qt.AlignLeft | Qt.AlignVCenter), line2)

        p.setPen(QColor(_MUTED))
        fh = QFont("Consolas", max(7, w // 28)); p.setFont(fh)
        p.drawText(QRectF(0, h * 0.88, w, h * 0.06), Qt.AlignCenter,
                   "Haut/Bas : choisir   [Entrée] rejoindre   Retour : annuler")

    def _draw_mp_lobby(self, p):
        """Salon d'attente : roster + etat Pret. Lancement auto quand tous prets."""
        w, h = self.width(), self.height()
        p.setPen(QColor(_GOLD))
        ft = QFont("Consolas", max(14, w // 10)); ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.04, w, h * 0.11), Qt.AlignCenter, "Salon")

        my_name = (getattr(self.services, "my_name", None) or "Toi")
        roster = self._mp_roster or []
        n_ready = sum(1 for m in roster if m.get("ready"))
        p.setPen(QColor(_MUTED))
        fs = QFont("Consolas", max(8, w // 24)); p.setFont(fs)
        status = (f"{len(roster)} joueur(s)  ·  {n_ready} prêt(s)"
                  if roster else "Connexion au salon…")
        p.drawText(QRectF(0, h * 0.155, w, h * 0.05), Qt.AlignCenter, status)

        ry = h * 0.24
        rh = max(30.0, h * 0.11)
        gap = rh * 0.20
        fn = QFont("Consolas", max(9, w // 18)); fn.setBold(True)
        ftag = QFont("Consolas", max(8, w // 26))
        for i, mmb in enumerate(roster[:5]):
            r = QRectF(w * 0.07, ry + i * (rh + gap), w * 0.86, rh)
            ready = bool(mmb.get("ready"))
            p.setBrush(QColor(_PANEL))
            p.setPen(QPen(QColor(_ACCENT if ready else _BORDER), 2 if ready else 1.4))
            p.drawRoundedRect(r, rh * 0.24, rh * 0.24)
            # pastille prete
            dot = rh * 0.34
            p.setBrush(QColor(_ACCENT if ready else _MUTED))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(r.x() + 10, r.y() + (rh - dot) / 2, dot, dot))
            # nom (+ tags)
            name = mmb.get("name", "?")
            tags = []
            if name == self._mp_host:
                tags.append("hôte")
            if name == my_name:
                tags.append("toi")
            label = name + (f"  ({', '.join(tags)})" if tags else "")
            p.setFont(fn)
            p.setPen(QColor(_TEXT))
            p.drawText(QRectF(r.x() + 10 + dot + 10, r.y(),
                              r.width() * 0.62, rh),
                       int(Qt.AlignLeft | Qt.AlignVCenter), label)
            p.setFont(ftag)
            p.setPen(QColor(_ACCENT if ready else _MUTED))
            p.drawText(QRectF(r.x(), r.y(), r.width() - 12, rh),
                       int(Qt.AlignRight | Qt.AlignVCenter),
                       "PRÊT" if ready else "en attente")

        # message transitoire (erreur)
        if self._mp_notice:
            p.setPen(QColor(_GOLD))
            fnz = QFont("Consolas", max(8, w // 26)); p.setFont(fnz)
            p.drawText(QRectF(w * 0.05, h * 0.78, w * 0.90, h * 0.07),
                       int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap),
                       self._mp_notice)

        # bas : action Pret + rappel lancement auto
        me = next((m for m in roster if m.get("name") == my_name), None)
        i_am_ready = bool(me and me.get("ready"))
        p.setPen(QColor(_TEXT))
        fc = QFont("Consolas", max(9, w // 18)); fc.setBold(True)
        p.setFont(fc)
        p.drawText(QRectF(0, h * 0.85, w, h * 0.07), Qt.AlignCenter,
                   "[Entrée] pas prêt" if i_am_ready else "[Entrée] je suis prêt")
        p.setPen(QColor(_MUTED))
        fh = QFont("Consolas", max(7, w // 30)); p.setFont(fh)
        p.drawText(QRectF(0, h * 0.93, w, h * 0.05), Qt.AlignCenter,
                   "Lancement auto quand tous prêts   ·   Retour : quitter")

    def _draw_rules(self, p):
        """Ecran Regles : titre + explication + rappel retour."""
        w, h = self.width(), self.height()
        p.setPen(QColor(_ACCENT))
        ft = QFont("Consolas", max(14, w // 10))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.07, w, h * 0.14),
                   Qt.AlignCenter, "Règles")
        p.setPen(QColor(_TEXT))
        fr = QFont("Consolas", max(9, w // 22))
        p.setFont(fr)
        txt = ("Forme la meilleure main de poker. À ton tour : F pour te "
               "coucher, V ou Entrée pour suivre, Haut/Bas pour régler la "
               "relance, R pour relancer, T pour le tapis.")
        p.drawText(QRectF(w * 0.08, h * 0.24, w * 0.84, h * 0.56),
                   int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap), txt)
        p.setPen(QColor(_MUTED))
        fh = QFont("Consolas", max(8, w // 26))
        p.setFont(fh)
        p.drawText(QRectF(0, h * 0.87, w, h * 0.10),
                   Qt.AlignCenter, "Échap ou clic : retour")

    def _draw_lobby(self, p):
        w, h = self.width(), self.height()
        p.setPen(QColor(_GOLD))
        ft = QFont("Consolas", max(15, w // 9))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.04, w, h * 0.11), Qt.AlignCenter, "POKER")
        p.setPen(QColor(_MUTED))
        fs = QFont("Consolas", max(8, w // 24))
        p.setFont(fs)
        p.drawText(QRectF(0, h * 0.155, w, h * 0.05), Qt.AlignCenter,
                   "Créer une partie multi" if self._lobby_for_mp
                   else "Texas Hold'em")

        n = self._hand_presets[self._preset_idx]
        buyin = self._stake_presets[self._stake_idx][0]
        nplayers = self._player_presets[self._players_idx]
        rows = [("Nombre de mains", f"\u2039  {n}  \u203a"),
                ("Mise (cave)", f"\u2039  {fmt_auec(buyin)}  \u203a")]
        if not self._lobby_for_mp:
            rows.append((f"Joueurs (toi + {nplayers - 1} bots)",
                         f"\u2039  {nplayers}  \u203a"))
        ry = h * 0.23
        rh = h * 0.165
        flab = QFont("Consolas", max(8, w // 21))
        fval = QFont("Consolas", max(11, w // 13))
        fval.setBold(True)
        for i, (lab, val) in enumerate(rows):
            focus = (i == self._lobby_focus)
            box = QRectF(w * 0.06, ry, w * 0.88, rh - 8)
            if focus:
                p.setPen(QPen(QColor(_ACCENT), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(box, 8, 8)
            p.setFont(flab)
            p.setPen(QColor(_MUTED))
            p.drawText(QRectF(box.x(), box.y() + 3, box.width(), rh * 0.4),
                       Qt.AlignCenter, lab)
            p.setFont(fval)
            p.setPen(QColor(_ACCENT if focus else _TEXT))
            p.drawText(QRectF(box.x(), box.y() + rh * 0.36, box.width(), rh * 0.5),
                       Qt.AlignCenter, val)
            ry += rh

        p.setPen(QColor(_MUTED))
        fhint = QFont("Consolas", max(7, w // 28))
        p.setFont(fhint)
        p.drawText(QRectF(0, h * 0.77, w, h * 0.05), Qt.AlignCenter,
                   "Haut/Bas : choix   \u2190/\u2192 : régler")
        p.setPen(QColor(_TEXT))
        fc = QFont("Consolas", max(9, w // 18))
        fc.setBold(True)
        p.setFont(fc)
        p.drawText(QRectF(0, h * 0.85, w, h * 0.08), Qt.AlignCenter,
                   "[Entrée] créer le salon" if self._lobby_for_mp
                   else "[Entrée] commencer")

    def _draw_match_over(self, p):
        w, h = self.width(), self.height()
        g = self.game
        order = self._standings()
        nets, all_transfers = self._settlement()
        net_by = dict(nets)
        you = next(i for i, pl in enumerate(g.players) if pl.is_human)
        # On n'affiche que les transferts qui ME concernent (envoi ou réception).
        my_transfers = [(frm, to, amt) for (frm, to, amt) in all_transfers
                        if frm == you or to == you]

        # Titre (fixe)
        p.setPen(QColor(_GOLD))
        ft = QFont("Consolas", max(12, w // 13))
        ft.setBold(True)
        p.setFont(ft)
        p.drawText(QRectF(0, h * 0.03, w, h * 0.10), Qt.AlignCenter, "PARTIE TERMINÉE")

        # Pied (fixe)
        footer = QRectF(0, h * 0.92, w, h * 0.07)
        # Zone défilable entre le titre et le pied
        content_top = h * 0.155
        view = QRectF(0, content_top, w, footer.top() - 4 - content_top)

        # Lignes de contenu : bilans nets puis transferts
        rowh = max(20.0, h * 0.066)
        rows = [("net", rank, i) for rank, i in enumerate(order, start=1)]
        rows.append(("gap",))
        rows.append(("header",))
        if not my_transfers:
            msg = ("tout le monde à égalité" if not all_transfers
                   else "rien à régler pour toi")
            rows.append(("msg", msg))
        else:
            # On separe ce que TU dois ENVOYER (payer) de ce que tu dois
            # RECEVOIR. "À envoyer" en premier (en haut).
            sends = [(f, t, a) for (f, t, a) in my_transfers if f == you]
            recvs = [(f, t, a) for (f, t, a) in my_transfers if t == you]
            if sends:
                rows.append(("subhdr", "À envoyer", False))
                rows += [("xfer", f, t, a) for (f, t, a) in sends]
            if recvs:
                rows.append(("subhdr", "À recevoir", True))
                rows += [("xfer", f, t, a) for (f, t, a) in recvs]

        total_h = sum(rowh * 0.5 if r[0] == "gap" else rowh for r in rows)
        self._mo_max_scroll = max(0.0, total_h - view.height())
        self._mo_scroll = min(self._mo_max_scroll, max(0.0, self._mo_scroll))

        fr = QFont("Consolas", max(8, w // 21))
        fh = QFont("Consolas", max(9, w // 18))
        fh.setBold(True)

        p.save()
        p.setClipRect(view)
        y = content_top - self._mo_scroll
        for r in rows:
            if r[0] == "gap":
                y += rowh * 0.5
                continue
            if r[0] == "net":
                rank, i = r[1], r[2]
                pl = g.players[i]
                net = net_by[i]
                amt_w = w * 0.30
                gap = w * 0.02
                name_w = w * 0.88 - amt_w - gap
                name_box = QRectF(w * 0.06, y, name_w, rowh)
                amt_box = QRectF(w * 0.06 + name_w + gap, y, amt_w, rowh)
                p.setFont(fr)
                p.setPen(QColor(_GOLD if rank == 1 else _TEXT))
                label = f"{rank}. {pl.name}"
                try:
                    label = p.fontMetrics().elidedText(
                        label, Qt.ElideRight, int(name_w))
                except Exception:
                    pass
                p.drawText(name_box, Qt.AlignLeft | Qt.AlignVCenter, label)
                sign = "+" if net > 0 else ""
                p.setPen(QColor(_ACCENT if net > 0 else (_RED if net < 0 else _MUTED)))
                p.drawText(amt_box, Qt.AlignRight | Qt.AlignVCenter,
                           f"{sign}{net:,}".replace(",", " "))
            elif r[0] == "header":
                p.setFont(fh)
                p.setPen(QColor(_CHIP))
                p.drawText(QRectF(0, y, w, rowh), Qt.AlignCenter, "Tes transferts aUEC")
            elif r[0] == "msg":
                p.setFont(fr)
                p.setPen(QColor(_MUTED))
                p.drawText(QRectF(0, y, w, rowh), Qt.AlignCenter, r[1])
            elif r[0] == "subhdr":
                # Sous-titre "À envoyer" (rouge) / "À recevoir" (vert), aligne a
                # gauche pour coiffer les lignes qui suivent.
                receiving = r[2]
                p.setFont(fh)
                p.setPen(QColor(_ACCENT if receiving else _RED))
                p.drawText(QRectF(w * 0.06, y, w * 0.88, rowh),
                           Qt.AlignLeft | Qt.AlignVCenter, r[1])
            elif r[0] == "xfer":
                frm, to, amt = r[1], r[2], r[3]
                p.setFont(fr)
                receiving = (to == you)
                # Le sous-titre ("À envoyer"/"À recevoir") donne deja le sens :
                # ici on n'affiche que l'AUTRE joueur + le montant colore.
                amt_w = w * 0.30
                gap = w * 0.02
                name_w = w * 0.88 - amt_w - gap
                # leger retrait sous le sous-titre
                name_box = QRectF(w * 0.10, y, name_w - w * 0.04, rowh)
                amt_box = QRectF(w * 0.06 + name_w + gap, y, amt_w, rowh)
                other = g.players[frm if receiving else to].name
                label = other
                try:
                    label = p.fontMetrics().elidedText(
                        label, Qt.ElideRight, int(name_box.width()))
                except Exception:
                    pass
                p.setPen(QColor(_TEXT))
                p.drawText(name_box, Qt.AlignLeft | Qt.AlignVCenter, label)
                sign = "+" if receiving else "-"
                p.setPen(QColor(_ACCENT if receiving else _RED))
                p.drawText(amt_box, Qt.AlignRight | Qt.AlignVCenter,
                           f"{sign}{amt:,}".replace(",", " "))
            y += rowh
        p.restore()

        # Barre de défilement + indice quand il y a plus de contenu
        if self._mo_max_scroll > 0:
            track = QRectF(w - 6, view.top(), 3, view.height())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(_BORDER))
            p.drawRoundedRect(track, 1.5, 1.5)
            thumb_h = max(14.0, view.height() * view.height() / total_h)
            thumb_y = view.top() + (view.height() - thumb_h) * (
                self._mo_scroll / self._mo_max_scroll)
            p.setBrush(QColor(_MUTED))
            p.drawRoundedRect(QRectF(w - 6, thumb_y, 3, thumb_h), 1.5, 1.5)
            p.setPen(QColor(_MUTED))
            p.setFont(QFont("Consolas", max(7, w // 27)))
            p.drawText(QRectF(0, h * 0.122, w, h * 0.032),
                       Qt.AlignCenter, "Haut/Bas : défiler")

        # Pied (fixe) — masque puis texte
        p.fillRect(footer.adjusted(0, -3, 0, 0), QColor(_BG))
        p.setPen(QColor(_TEXT))
        fc = QFont("Consolas", max(8, w // 21))
        fc.setBold(True)
        p.setFont(fc)
        p.drawText(footer, Qt.AlignCenter,
                   "[Entrée] retour au menu" if self._is_net
                   else "[Entrée] nouvelle partie")

    # ---- carte ----
    def _draw_card(self, p, x, y, w, h, card, face_up, empty=False, highlight=None):
        r = max(3.0, w * 0.16)
        rect = QRectF(x, y, w, h)
        if empty:
            p.setPen(QPen(QColor(_BORDER), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, r, r)
            return
        if not face_up:
            p.setPen(QPen(QColor("#13346b"), 1))
            p.setBrush(QColor(_CARD_BACK))
            p.drawRoundedRect(rect, r, r)
            p.setPen(QPen(QColor("#2f6fed"), 1))
            p.drawRoundedRect(QRectF(x + w * 0.18, y + h * 0.12,
                                     w * 0.64, h * 0.76), r * 0.6, r * 0.6)
        else:
            p.setPen(QPen(QColor(_CARD_EDGE), 1))
            p.setBrush(QColor(_CARD_BG))
            p.drawRoundedRect(rect, r, r)
            rank, suit = card
            col = _SUIT_RED if suit in (0, 1) else _SUIT_BLK
            fr = QFont("Consolas", max(7, int(h * 0.30)))
            fr.setBold(True)
            p.setFont(fr)
            p.setPen(QColor(col))
            p.drawText(QRectF(x + w * 0.08, y + h * 0.02, w * 0.7, h * 0.4),
                       Qt.AlignLeft | Qt.AlignVCenter, pe.rank_label(rank))
            fs = QFont("Arial", max(8, int(h * 0.40)))
            p.setFont(fs)
            p.drawText(QRectF(x, y + h * 0.18, w, h * 0.7),
                       Qt.AlignCenter, pe.SUIT_STR[suit])
        if highlight:
            p.setPen(QPen(QColor(highlight), max(2, int(w * 0.08))))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, r, r)


class _Harness(QWidget):
    """HARNAIS DE TEST VISUEL (supprimable) : châssis CircusPhone qui monte
    la PokerApp. Géométrie adaptative identique au client."""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("CircusPhone — Poker (harnais)")
        screen = QGuiApplication.primaryScreen()
        try:
            scr_h = screen.availableGeometry().height()
        except Exception:
            scr_h = 1080
        body_h = max(420, min(760, int(scr_h * 0.62)))
        body_w = int(body_h * (200.0 / 440.0))
        sx, sy = body_w / 200.0, body_h / 440.0
        self._body_w, self._body_h = body_w, body_h
        self._radius     = int(28 * sx)
        self._banner_h   = int(56 * sy)
        self._screen_x   = int(12 * sx)
        self._screen_y   = self._banner_h
        self._screen_w   = body_w - 2 * self._screen_x
        self._screen_h   = body_h - self._banner_h - int(16 * sy)
        self._screen_rad = int(14 * sx)
        self.setFixedSize(body_w, body_h)
        self.setFocusPolicy(Qt.StrongFocus)
        self.game = PokerApp(self._screen_w, self._screen_h,
                             self._screen_rad, PhoneServices(), parent=self)
        self.game.move(self._screen_x, self._screen_y)
        self._drag_offset = None
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                # Comme dans le client : en jeu/lobby/regles/mode -> retour
                # menu ; au menu -> on ferme (retour au home du telephone).
                if not self.game.handle_back():
                    self.game.on_hide()
                    self.close()
            else:
                self.game.keyPressEvent(event)
            return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = (
                e.globalPosition().toPoint() - self.frameGeometry().topLeft())
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_offset = None

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self._body_w, self._body_h
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_PHONE_BODY_COLOR))
        p.drawRoundedRect(0, 0, w, h, self._radius, self._radius)
        p.setBrush(QColor(_PHONE_BTN_COLOR))
        bw = max(2, int(w * 0.015))
        p.drawRoundedRect(0, int(h * 0.25), bw, int(h * 0.05), 1, 1)
        p.drawRoundedRect(0, int(h * 0.33), bw, int(h * 0.09), 1, 1)
        p.drawRoundedRect(0, int(h * 0.44), bw, int(h * 0.09), 1, 1)
        p.drawRoundedRect(w - bw, int(h * 0.36), bw, int(h * 0.14), 1, 1)
        cx = w / 2
        cy = self._banner_h / 2 + int(self._banner_h * 0.08)
        f_small = QFont("sans-serif")
        f_small.setPixelSize(max(10, int(self._banner_h * 0.26)))
        f_small.setWeight(QFont.Medium)
        f_big = QFont("sans-serif")
        f_big.setPixelSize(max(14, int(self._banner_h * 0.40)))
        f_big.setWeight(QFont.DemiBold)
        fm_s, fm_b = QFontMetrics(f_small), QFontMetrics(f_big)
        wc = fm_s.horizontalAdvance("Circus")
        wp = fm_b.horizontalAdvance("Phone")
        x0 = cx - (wc + wp) / 2
        baseline = cy + fm_b.ascent() / 2
        p.setFont(f_small)
        p.setPen(QColor(_PHONE_BANNER_GREY))
        p.drawText(int(x0), int(baseline), "Circus")
        p.setFont(f_big)
        p.setPen(QColor(_PHONE_BANNER_WHITE))
        p.drawText(int(x0 + wc), int(baseline), "Phone")
        p.end()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = _Harness()
    win.show()
    sys.exit(app.exec())
