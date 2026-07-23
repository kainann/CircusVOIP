# -*- coding: utf-8 -*-
"""
poker_engine
============

Moteur Texas Hold'em PUR : aucune dépendance à Qt ni au réseau. Toute la
logique du jeu vit ici sous forme de machine à états qu'on pilote en lui
envoyant des actions (fold / check / call / raise).

Pourquoi ce découpage : l'objectif est un poker MULTIJOUEUR. Le même moteur
sert :
  - aujourd'hui  : piloté par l'UI locale + des bots (test solo) ;
  - plus tard    : piloté par le serveur CircusVoIP, qui reçoit les actions
                   des joueurs distants (validées par legal_actions) et
                   diffuse l'état via to_dict() à tous les clients.

Utilisation type :
    g = HoldemGame(["Toi", "Bot 1", "Bot 2"], stacks=1000, sb=10, bb=20)
    g.start_hand()
    while not g.hand_over:
        p = g.current               # joueur à qui d'agir (index)
        la = g.legal_actions()      # actions légales + montants
        g.act("call")               # ou "fold" / "check" / "raise", amount=...
    # g.last_results : gains par joueur ; g.community ; g.players[i].hole

L'évaluateur de mains (7 cartes -> meilleure main de 5) renvoie un score
totalement ordonné, donc les égalités/kickers se départagent correctement
et les pots se partagent à parts égales en cas d'ex-aequo.

Cartes : tuple (rank, suit). rank 2..14 (J=11, Q=12, K=13, A=14).
         suit 0..3 (0=♥ coeur, 1=♦ carreau, 2=♣ trefle, 3=♠ pique).
"""

from __future__ import annotations

import random
from collections import Counter
from itertools import combinations


RANKS = list(range(2, 15))
SUITS = (0, 1, 2, 3)
RANK_STR = {11: "J", 12: "Q", 13: "K", 14: "A"}
SUIT_STR = {0: "♥", 1: "♦", 2: "♣", 3: "♠"}

# Noms FR des catégories (index = code catégorie du score)
CATEGORY_FR = {
    8: "Quinte flush", 7: "Carré", 6: "Full", 5: "Couleur",
    4: "Quinte", 3: "Brelan", 2: "Deux paires", 1: "Paire", 0: "Hauteur",
}


def rank_label(r):
    return RANK_STR.get(r, str(r))


def make_deck():
    return [(r, s) for r in RANKS for s in SUITS]


# ----------------------------------------------------------------------
# Évaluation
# ----------------------------------------------------------------------
def _score5(cards):
    """Score d'une main de 5 cartes : tuple comparable (catégorie d'abord,
    puis départages). Plus grand = meilleur."""
    rks = sorted((c[0] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    rc = Counter(rks)
    # groupes (rang) triés par (effectif, rang) décroissant
    groups = sorted(rc.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    counts = [g[1] for g in groups]
    gr = [g[0] for g in groups]
    is_flush = len(set(suits)) == 1
    uniq = sorted(set(rks), reverse=True)
    straight_high = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:   # roue A-2-3-4-5
            straight_high = 5

    if is_flush and straight_high:
        return (8, straight_high)
    if counts[0] == 4:
        return (7, gr[0], gr[1])
    if counts[0] == 3 and counts[1] == 2:
        return (6, gr[0], gr[1])
    if is_flush:
        return (5, tuple(rks))
    if straight_high:
        return (4, straight_high)
    if counts[0] == 3:
        kick = tuple(sorted((r for r in rks if r != gr[0]), reverse=True))
        return (3, gr[0], kick)
    if counts[0] == 2 and counts[1] == 2:
        hp, lp = max(gr[0], gr[1]), min(gr[0], gr[1])
        kick = max(r for r in rks if r != hp and r != lp)
        return (2, hp, lp, kick)
    if counts[0] == 2:
        kick = tuple(sorted((r for r in rks if r != gr[0]), reverse=True))
        return (1, gr[0], kick)
    return (0, tuple(rks))


def best_score(cards):
    """Meilleur score parmi toutes les mains de 5 cartes contenues dans
    'cards' (5, 6 ou 7 cartes)."""
    if len(cards) <= 5:
        return _score5(cards)
    return max(_score5(combo) for combo in combinations(cards, 5))


def category_name(score):
    cat = score[0]
    if cat == 8 and score[1] == 14:
        return "Quinte flush royale"
    return CATEGORY_FR[cat]


# ----------------------------------------------------------------------
# Joueur
# ----------------------------------------------------------------------
class Player:
    def __init__(self, name, stack, is_human=False):
        self.name = name
        self.stack = stack
        self.is_human = is_human
        self.hole = []          # cartes privées
        self.folded = False
        self.all_in = False
        self.street_bet = 0     # mise sur la street courante
        self.contributed = 0    # total misé sur la main (pour les pots)
        self.in_hand = False    # participe à la main en cours
        self.last_action = ""   # pour l'affichage ("fold", "call", ...)

    def reset_for_hand(self):
        self.hole = []
        self.folded = False
        self.all_in = False
        self.street_bet = 0
        self.contributed = 0
        self.in_hand = self.stack > 0
        self.last_action = ""


# ----------------------------------------------------------------------
# Partie
# ----------------------------------------------------------------------
class HoldemGame:
    STREETS = ("preflop", "flop", "turn", "river", "showdown")

    def __init__(self, names, stacks=1000, sb=10, bb=20, rng=None):
        if isinstance(stacks, int):
            stacks = [stacks] * len(names)
        self.players = [Player(n, s) for n, s in zip(names, stacks)]
        self.players[0].is_human = True   # le siège 0 = joueur local par défaut
        self.sb = sb
        self.bb = bb
        self.rng = rng or random.Random()
        self.button = -1            # incrémenté à chaque main
        self.deck = []
        self.community = []
        self.street = None
        self.current = None         # index du joueur à qui d'agir
        self.current_bet = 0        # plus haute mise sur la street
        self.min_raise = bb         # incrément minimal de relance
        self._need_action = set()   # indices devant encore agir
        self.hand_over = True
        self.last_results = {}      # index -> gain net (>0) sur la dernière main
        self.message = ""

    # ---- helpers ----
    def _active_seats(self):
        """Joueurs encore en jeu (non couchés, pas tapis), dans l'ordre."""
        return [i for i, p in enumerate(self.players)
                if p.in_hand and not p.folded and not p.all_in]

    def _contesting(self):
        """Joueurs non couchés (tapis inclus) : ceux qui peuvent gagner."""
        return [i for i, p in enumerate(self.players) if p.in_hand and not p.folded]

    def _next_seat(self, start, pred):
        n = len(self.players)
        for k in range(1, n + 1):
            i = (start + k) % n
            if pred(self.players[i]):
                return i
        return None

    # ---- démarrage de main ----
    def start_hand(self):
        contenders = [i for i, p in enumerate(self.players) if p.stack > 0]
        if len(contenders) < 2:
            self.message = "Partie terminée."
            self.hand_over = True
            return False

        for p in self.players:
            p.reset_for_hand()
        self.community = []
        self.deck = make_deck()
        self.rng.shuffle(self.deck)
        self.last_results = {}
        self.hand_over = False
        self.street = "preflop"

        # Bouton + blindes (on saute les joueurs sans tapis)
        self.button = self._next_seat(self.button, lambda p: p.in_hand)
        if len([i for i in contenders]) == 2:
            sb_seat = self.button                       # heads-up : bouton = SB
        else:
            sb_seat = self._next_seat(self.button, lambda p: p.in_hand)
        bb_seat = self._next_seat(sb_seat, lambda p: p.in_hand)

        self._post(sb_seat, self.sb)
        self._post(bb_seat, self.bb)
        self.current_bet = self.bb
        self.min_raise = self.bb

        # Distribution des cartes privées
        for p in self.players:
            if p.in_hand:
                p.hole = [self.deck.pop(), self.deck.pop()]

        # Premier à parler préflop = après la BB
        self._need_action = set(i for i, p in enumerate(self.players)
                                if p.in_hand and not p.all_in)
        self.current = self._next_seat(bb_seat, lambda p: p.in_hand and not p.all_in)
        self.message = "Préflop"
        return True

    def _post(self, seat, amount):
        p = self.players[seat]
        pay = min(amount, p.stack)
        p.stack -= pay
        p.street_bet += pay
        p.contributed += pay
        if p.stack == 0:
            p.all_in = True

    # ---- actions légales ----
    def legal_actions(self):
        """Actions possibles pour le joueur courant + montants associés."""
        if self.hand_over or self.current is None:
            return {}
        p = self.players[self.current]
        to_call = self.current_bet - p.street_bet
        can_check = (to_call == 0)
        call_amount = min(to_call, p.stack)
        # relance : porter sa mise de street à 'raise_to'
        min_raise_to = self.current_bet + self.min_raise
        max_raise_to = p.street_bet + p.stack          # tapis
        can_raise = p.stack > to_call                  # il reste des jetons après suivre
        if min_raise_to > max_raise_to:
            min_raise_to = max_raise_to                # relance minimale = tapis
        return {
            "fold": True,
            "check": can_check,
            "call": (not can_check) and call_amount > 0,
            "call_amount": call_amount,
            "raise": can_raise,
            "min_raise_to": min_raise_to,
            "max_raise_to": max_raise_to,
            "to_call": to_call,
        }

    # ---- appliquer une action ----
    def act(self, action, amount=0):
        """Applique l'action du joueur courant. 'amount' = mise de street
        VISÉE pour un raise (raise_to). Retourne True si acceptée."""
        if self.hand_over or self.current is None:
            return False
        la = self.legal_actions()
        i = self.current
        p = self.players[i]

        if action == "fold":
            p.folded = True
            p.last_action = "couche"
            self._need_action.discard(i)

        elif action == "check":
            if not la["check"]:
                return False
            p.last_action = "check"
            self._need_action.discard(i)

        elif action == "call":
            pay = la["call_amount"]
            self._take(p, pay)
            p.last_action = "tapis" if p.all_in else "suit"
            self._need_action.discard(i)

        elif action == "raise":
            raise_to = max(amount, la["min_raise_to"])
            raise_to = min(raise_to, la["max_raise_to"])
            inc = raise_to - p.street_bet
            prev_bet = self.current_bet
            self._take(p, inc)
            # incrément de relance effectif (pour la relance min suivante)
            if raise_to - prev_bet >= self.min_raise:
                self.min_raise = raise_to - prev_bet
            self.current_bet = max(self.current_bet, p.street_bet)
            p.last_action = "tapis" if p.all_in else "relance"
            # tout le monde (actif) doit reparler, sauf lui
            self._need_action = set(self._active_seats())
            self._need_action.discard(i)
        else:
            return False

        # Fin de main par abandon ?
        if len(self._contesting()) == 1:
            self._award_uncontested()
            return True

        self._advance()
        return True

    def _take(self, p, amount):
        amount = min(amount, p.stack)
        p.stack -= amount
        p.street_bet += amount
        p.contributed += amount
        if p.stack == 0:
            p.all_in = True

    def _advance(self):
        """Passe la main au prochain à agir, ou clôt la street si plus
        personne ne doit parler."""
        nxt = self._next_actionable(self.current)
        if nxt is None:
            self._next_street()
        else:
            self.current = nxt

    def _next_actionable(self, start):
        n = len(self.players)
        for k in range(1, n + 1):
            i = (start + k) % n
            p = self.players[i]
            if i in self._need_action and p.in_hand and not p.folded and not p.all_in:
                return i
        return None

    def _next_street(self):
        # Réinitialise les mises de street
        for p in self.players:
            p.street_bet = 0
        self.current_bet = 0
        self.min_raise = self.bb

        order = ["preflop", "flop", "turn", "river", "showdown"]
        nxt = order[order.index(self.street) + 1]
        self.street = nxt

        # Si <=1 joueur peut encore agir (tapis), on dévoile jusqu'au bout.
        if nxt == "flop":
            self.community += [self.deck.pop() for _ in range(3)]
        elif nxt in ("turn", "river"):
            self.community.append(self.deck.pop())
        elif nxt == "showdown":
            self._showdown()
            return

        self.message = nxt.capitalize()
        self._need_action = set(self._active_seats())
        if len(self._need_action) <= 1:
            # personne ne peut plus miser : on enchaîne les streets jusqu'au showdown
            self._need_action = set()
            self._advance_runout()
            return
        # premier à parler post-flop = premier joueur actif après le bouton
        self.current = self._next_actionable(self.button)

    def _advance_runout(self):
        """Tapis généralisé : dévoile le reste du board puis showdown."""
        while self.street != "showdown":
            order = ["preflop", "flop", "turn", "river", "showdown"]
            nxt = order[order.index(self.street) + 1]
            self.street = nxt
            if nxt == "flop":
                self.community += [self.deck.pop() for _ in range(3)]
            elif nxt in ("turn", "river"):
                self.community.append(self.deck.pop())
            elif nxt == "showdown":
                self._showdown()
                return

    # ---- fin de main ----
    def _award_uncontested(self):
        winner = self._contesting()[0]
        pot = sum(p.contributed for p in self.players)
        self.players[winner].stack += pot
        self.last_results = {winner: pot - self.players[winner].contributed}
        self.current = None
        self.hand_over = True
        self.message = f"{self.players[winner].name} remporte le pot ({pot})."

    def _showdown(self):
        self.current = None
        self.street = "showdown"
        scored = {i: best_score(p.hole + self.community)
                  for i, p in enumerate(self.players)
                  if p.in_hand and not p.folded}

        # Pots annexes : on "épluche" les contributions par paliers. À chaque
        # palier, l'argent misé (y compris la "dead money" des couchés) revient
        # au meilleur jeu parmi les NON-couchés ayant contribué à ce palier.
        contrib = {i: p.contributed for i, p in enumerate(self.players)
                   if p.contributed > 0}
        gains = {i: 0 for i in range(len(self.players))}

        while contrib:
            level = min(contrib.values())
            layer_contributors = list(contrib.keys())
            pot = level * len(layer_contributors)
            for i in layer_contributors:
                contrib[i] -= level
                if contrib[i] == 0:
                    del contrib[i]
            eligible = [i for i in layer_contributors if i in scored]
            if not eligible:
                # personne de non-couché à ce palier : la dead money rejoint
                # le meilleur jeu global encore en lice.
                eligible = list(scored.keys())
            if not eligible:
                continue
            best = max(scored[i] for i in eligible)
            winners = [i for i in eligible if scored[i] == best]
            share, rem = divmod(pot, len(winners))
            for w in winners:
                gains[w] += share
            if rem:
                # jeton(s) indivisible(s) : au 1er gagnant après le bouton
                order = sorted(winners,
                               key=lambda w: (w - self.button) % len(self.players))
                gains[order[0]] += rem

        for i, g in gains.items():
            self.players[i].stack += g
        self.last_results = {i: g for i, g in gains.items() if g > 0}
        self._showdown_scores = scored
        self.hand_over = True
        if self.last_results:
            best_i = max(self.last_results, key=lambda k: self.last_results[k])
            self.message = (f"{self.players[best_i].name} gagne avec "
                            f"{category_name(scored[best_i])}.")
        else:
            self.message = "Showdown."

    # ---- introspection / réseau ----
    def to_dict(self, viewer=None):
        """État sérialisable pour le rendu / la diffusion réseau. Si 'viewer'
        est donné, masque les cartes des autres (utile côté serveur)."""
        return {
            "street": self.street,
            "community": list(self.community),
            "pot": sum(p.contributed for p in self.players),
            "current": self.current,
            "button": self.button,
            "current_bet": self.current_bet,
            "hand_over": self.hand_over,
            "message": self.message,
            "results": dict(self.last_results),
            "players": [
                {
                    "name": p.name, "stack": p.stack, "bet": p.street_bet,
                    "folded": p.folded, "all_in": p.all_in, "in_hand": p.in_hand,
                    "last": p.last_action,
                    "hole": (list(p.hole) if (viewer is None or viewer == i
                             or (self.hand_over and not p.folded)) else None),
                }
                for i, p in enumerate(self.players)
            ],
        }
