# -*- coding: utf-8 -*-
"""
circusvoip_phone_games
======================

App "dossier Jeux" du CircusPhone (v0.3).

Objectif : regrouper les jeux dans une seule case du home. Au lancement,
le dossier affiche UNIQUEMENT les jeux (sous-grille), sans le reste des
apps. Selectionner un jeu demande a l'overlay de l'ouvrir.

Conception : plutot que d'introduire une notion de "groupe" dans
`PhoneHome`, le dossier EST une `PhoneApp` ordinaire (CAPTURES_KEYBOARD =
False) qui HEBERGE son propre `PhoneHome` interne. On reutilise ainsi tel
quel le rendu de grille, la selection, la nav D-pad et le clic souris.

Decouplage : le dossier ne connait pas l'overlay. Il expose
  - set_games(classes) : la liste des classes de jeux a afficher
    (injectee par l'overlay depuis le registre, comme set_wallpaper_dir
    de l'app Parametres) ;
  - sig_open_app(app_id) : emis quand l'utilisateur valide un jeu.
L'overlay branche ce signal sur sa fabrique _launch_app (cf. client).

Le graphe d'import reste acyclique : ce module n'importe que `PhoneApp`,
`PhoneHome` et `HomeEntry` depuis circusvoip_phone_apps. Il n'importe NI
le registre NI les modules de jeux (les classes arrivent par set_games).
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QVBoxLayout, QWidget

from circusvoip_phone_apps import PhoneApp, PhoneHome, HomeEntry


class GamesFolderApp(PhoneApp):
    """Case "Jeux" du home : ouvre une sous-grille ne contenant que les
    jeux. Selectionner un jeu emet sig_open_app(APP_ID) ; l'overlay ouvre
    l'app correspondante."""

    APP_ID:   str = "games"
    APP_NAME: str = "Jeux"
    APP_ICON = "\U0001F3AE"   # 🎮 manette
    CAPTURES_KEYBOARD: bool = False

    # Emis avec l'APP_ID du jeu choisi. L'overlay le resout vers la classe
    # (via le registre PHONE_GAMES) et lance le jeu, en memorisant qu'on
    # vient du dossier (pour que le retour du jeu revienne ici).
    sig_open_app = Signal(str)

    def __init__(self, screen_w: int, screen_h: int, screen_radius: int,
                 services, parent: Optional[QWidget] = None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self._games: List[type] = []
        # PhoneHome interne : meme rendu/nav que l'accueil, mais peuple
        # uniquement de jeux. Pas de fond par defaut -> degrade sombre,
        # qui distingue visuellement "on est dans un dossier". L'overlay
        # peut pousser le vrai fond via set_wallpaper() pour un rendu continu.
        self._inner = PhoneHome(screen_w, screen_h, screen_radius,
                                entries=[], wallpaper=None, parent=self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._inner)
        # Filtrage dynamique : certains jeux (ex. Billard) ne sont visibles
        # qu'a certains endroits en jeu (is_available). On re-filtre la grille
        # periodiquement tant que le dossier est ouvert, pour que l'icone
        # apparaisse/disparaisse selon la position.
        self._available_ids = None
        self._avail_timer = QTimer(self)
        # 250 ms (au lieu de 1 s) : reduit la latence d'apparition de l'icone
        # Billard quand on arrive pres d'une table. Le check est trivial
        # (quelques distances) et la grille n'est reconstruite QUE si
        # l'ensemble des jeux visibles change -> pas de cout notable.
        self._avail_timer.setInterval(250)
        self._avail_timer.timeout.connect(self._refresh_available)

    # ------------------------------------------------------------------
    #  API publique (appelee par l'overlay)
    # ------------------------------------------------------------------
    def set_games(self, game_classes: list) -> None:
        """Definit/Remplace la liste des classes de jeux affichees. La grille
        n'affiche que les jeux DISPONIBLES (cf. is_available) ; la liste
        complete est conservee pour le re-filtrage dynamique."""
        self._games = list(game_classes or [])
        self._available_ids = None      # force un rebuild au prochain filtrage
        self._build_entries()

    def _game_available(self, Cls) -> bool:
        """Un jeu est affiche sauf s'il expose is_available(services) -> False
        (ex. Billard visible seulement pres d'un vrai billard en jeu)."""
        fn = getattr(Cls, "is_available", None)
        if not callable(fn):
            return True
        try:
            return bool(fn(self.services))
        except Exception:
            return True                 # en cas de doute, on n'enleve pas le jeu

    def _build_entries(self) -> None:
        """(Re)construit la sous-grille a partir des jeux disponibles."""
        avail = [C for C in self._games if self._game_available(C)]
        self._available_ids = [getattr(C, "APP_ID", "") for C in avail]
        entries = []
        for Cls in avail:
            aid = getattr(Cls, "APP_ID", "")
            entries.append(HomeEntry(
                entry_id=aid,
                name=getattr(Cls, "APP_NAME", aid or "Jeu"),
                icon=getattr(Cls, "APP_ICON", None),
                launch=(lambda _aid=aid: self.sig_open_app.emit(_aid)),
            ))
        self._inner.set_entries(entries)

    def _refresh_available(self) -> None:
        """Re-evalue la disponibilite des jeux ; ne reconstruit la grille que
        si l'ensemble visible a change (evite de perturber la navigation)."""
        ids = [getattr(C, "APP_ID", "") for C in self._games
               if self._game_available(C)]
        if ids != self._available_ids:
            self._build_entries()

    def set_wallpaper(self, pixmap: Optional[QPixmap]) -> None:
        """Pousse un fond d'ecran a la sous-grille (rendu continu avec le
        home). Optionnel ; sans appel, degrade sombre par defaut."""
        try:
            self._inner.set_wallpaper(pixmap)
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  Cycle de vie + navigation (delegues a la sous-grille).
    # ------------------------------------------------------------------
    def on_show(self) -> None:
        # Re-filtre selon la position courante, puis repart sur le 1er jeu.
        self._refresh_available()
        self._avail_timer.start()
        try:
            self._inner.select(0)
        except Exception:
            pass

    def on_hide(self) -> None:
        self._avail_timer.stop()

    def handle_nav(self, direction: str) -> bool:
        """D-pad : delegue a la sous-grille (fleches + 'enter')."""
        try:
            return bool(self._inner.handle_nav(direction))
        except Exception:
            return False

    def handle_back(self) -> bool:
        """Retour depuis le dossier : non consomme -> l'overlay revient au
        home du telephone."""
        return False
