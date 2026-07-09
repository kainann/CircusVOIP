# -*- coding: utf-8 -*-
"""
circusvoip_phone_apps
=====================

Socle d'integration des applications du CircusPhone (v0.3).

Ce module fournit les BRIQUES que l'overlay (`PhoneOverlayWindow` dans
`circusvoip_client.py`) importe pour heberger des applications dans son
`QStackedWidget`. Il ne depend QUE de PySide6 : aucune reference a la
MainWindow, au core, au reseau. Le decouplage passe par `PhoneServices`,
un petit bundle d'acces injecte a chaque app (log, envoi WS, feed gamelog,
photos de pairs) — c'est ce qui permet d'ecrire le code v0.3 hors du
monolithe sans le coupler a `client.py`.

Contenu :
  - PhoneServices : bundle de services injecte aux apps (decouplage).
  - PhoneApp      : classe de base = le CONTRAT que toute app implemente
                    (metadonnees pour le home, cycle de vie on_show/on_hide,
                    navigation D-pad handle_nav).
  - HomeEntry     : une case du home (app v0.3 OU ecran natif existant).
  - PhoneHome     : l'ecran d'accueil = grille d'icones sur fond d'ecran,
                    avec navigation 2D (clavier D-pad + souris).
  - PHONE_APPS    : registre des apps v0.3 (la SEULE liste a editer quand
                    on ajoute une app).
  - build_app_entries() : helper qui transforme PHONE_APPS en HomeEntry.

Integration cote overlay (resume, fait dans client.py, PAS ici) :
  1. construire un PhoneServices (log=_on_log, send_ws=..., gamelog=...,
     photo_of=_photo_provider) ;
  2. composer la liste HomeEntry = entrees ecrans natifs (Appels/Messagerie)
     + build_app_entries(PHONE_APPS, launcher) ;
  3. creer PhoneHome(screen_w, screen_h, screen_rad, entries, wallpaper),
     l'inserer en INDEX 0 du stack, en faire l'ecran courant par defaut ;
  4. router sig_nav_key vers l'ecran courant (home.handle_nav / app.handle_nav) ;
  5. cabler une touche "Retour" universelle -> on_hide() de l'app + retour home.

Le bloc __main__ en fin de fichier est un HARNAIS DE TEST VISUEL
supprimable : il monte un PhoneHome dans un chassis CircusPhone pour
verifier le rendu et la navigation. Il ne fait pas partie de l'integration.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Optional, Union

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap,
    QGuiApplication, QLinearGradient, QRadialGradient, QBrush,
)
from PySide6.QtWidgets import QApplication, QWidget


# ======================================================================
#  Palette : reprise telle quelle du CircusPhone + accents GitHub-dark
#  (memes valeurs que circusvoip_client.py / les protos, pour coherence).
# ======================================================================
PHONE_BODY_COLOR   = "#1a1a1a"   # corps du telephone
PHONE_BTN_COLOR    = "#0a0a0a"   # boutons lateraux decoratifs
PHONE_BANNER_GREY  = "#888888"   # "Circus" (petit, gris)
PHONE_BANNER_WHITE = "#ffffff"   # "Phone" (grand, blanc)
PHONE_ACCENT       = "#2f6fed"   # accent (selection)

# Fond d'ecran par defaut (aucun wallpaper fourni) + textes du home.
HOME_BG_DARK       = "#0d1117"   # aplat sombre de repli ultime
# Fond d'ecran PAR DEFAUT (aucun fond configure) : degrade genere, pour ne
# pas afficher un aplat noir. Sur le theme (bleu nuit spatial).
HOME_BG_DEFAULT_TOP = "#16263f"
HOME_BG_DEFAULT_BOT = "#0a0d14"
HOME_TILE_BG       = "#161b22"   # fond d'une tuile d'icone (glyphe)
HOME_TILE_BORDER   = "#30363d"   # bord de tuile
HOME_TEXT          = "#c9d1d9"   # libelle d'app
HOME_TEXT_MUTED    = "#6e7681"   # secondaire
HOME_SEL_HALO      = "#2f6fed"   # halo de selection (accent)


# ======================================================================
#  PhoneServices : bundle de services injecte a chaque app.
# ======================================================================
@dataclass
class PhoneServices:
    """Acces minimal dont une app peut avoir besoin, injecte a la
    construction. On passe CE bundle plutot que la MainWindow, pour que
    les apps ne connaissent jamais le monolithe (decouplage : axe 6).

    Tous les champs sont optionnels : une app non concernee n'y touche
    pas. L'overlay remplit ce qu'il peut au moment de la construction.

    Champs :
      log      : logger best-effort (= MainWindow._on_log). Toujours
                 appelable sans crasher meme si non fourni (cf. __post_init__).
      send_ws  : envoi d'un message JSON au serveur de positions (8888).
                 Retourne True si planifie. Utilise par les apps a etat
                 serveur (poker aUEC, futurs jeux multijoueurs).
      gamelog  : feed du Game.log deja taile par le thread existant
                 (c2-gamelog-tail-smart). Utilise par le Wallet pour ne
                 PAS ouvrir un second tail concurrent. Contrat attendu :
                 un objet exposant subscribe(callback)/unsubscribe(callback)
                 ou l'overlay branchera les lignes ; laisse libre ici.
      photo_of : bytes JPEG d'un pair (avatar) ou None. Reutilise le
                 _photo_provider existant (D5).
    """
    log:      Callable[[str], None]        = None
    send_ws:  Callable[[dict], bool]       = None
    gamelog:  object                       = None
    photo_of: Callable[[str], Optional[bytes]] = None
    my_name:  Optional[str]                = None   # pseudo du joueur local
                                                    # (rempli par l'overlay ;
                                                    # utilise par le multijoueur)
    pos_provider: Callable[[], Optional[dict]] = None  # -> position OCR live
                                                    # {"zone","x","y","z"} ;
                                                    # utilise par la proximite
                                                    # (ex. tables de billard)
    view_photo: Callable[[str], None] = None        # ouvre une photo EN GRAND
                                                    # sur l'ecran de jeu (fenetre
                                                    # separee) ; fourni par
                                                    # l'overlay a la galerie Photos

    def __post_init__(self):
        # Garantir que log() est toujours appelable : evite des getattr
        # defensifs dans chaque app. No-op si l'overlay n'a rien fourni.
        if self.log is None:
            self.log = lambda _msg: None


# ======================================================================
#  PhoneApp : le CONTRAT que toute application v0.3 implemente.
# ======================================================================
class PhoneApp(QWidget):
    """Classe de base d'une application du CircusPhone.

    Une app est un QWidget pose dans le QStackedWidget de l'overlay. Elle
    occupe la zone ECRAN du telephone (le chassis est peint par l'overlay,
    une seule fois — l'app ne dessine JAMAIS de chassis). Elle se
    dimensionne dynamiquement a la taille que le stack lui donne ; on
    utilise self.width()/height() au paint, pas une taille mise en cache.

    Une app declare ses METADONNEES via des attributs de CLASSE (lus par
    le home sans instancier l'app) :
      APP_ID            : cle unique (str), stable, sans espace.
      APP_NAME          : libelle affiche sous l'icone.
      APP_ICON          : QPixmap, chemin str, glyphe (1 caractere) ou None.
                          None => le home dessine l'initiale du nom.
      CAPTURES_KEYBOARD : True pour les jeux (Snake, Poker...). Indique a
                          l'overlay de basculer en capture clavier brute
                          (keyPressEvent de l'app) au lieu du D-pad. False
                          => l'overlay route les fleches via handle_nav().

    Cycle de vie (appele par l'overlay, jamais par l'app elle-meme) :
      on_show() : l'app devient l'ecran courant. (Re)demarre timers/threads.
      on_hide() : on quitte l'app. PAUSE timers/threads, libere le lourd.
                  C'est ce qui garantit qu'aucun jeu ne tourne en fond.

    Navigation D-pad (apps NON-jeu uniquement) :
      handle_nav(direction) : 'up'|'down'|'left'|'right'|'enter'.
                              Retourne True si l'app a consomme la touche.
                              Defaut : ne consomme rien.
    """

    # --- metadonnees (a surcharger dans chaque app) ---
    APP_ID:   str = "app"
    APP_NAME: str = "App"
    APP_ICON: Union[QPixmap, str, None] = None
    CAPTURES_KEYBOARD: bool = False

    # Emis par une app pour demander a l'overlay de revenir au home (ex.
    # bouton "Quitter" d'un menu de jeu). L'overlay connecte ce signal a
    # _go_home au lancement de l'app. Decouplage : l'app ne connait pas
    # l'overlay, elle se contente d'emettre.
    sig_request_home = Signal()

    def __init__(self, screen_w: int, screen_h: int, screen_radius: int,
                 services: PhoneServices, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # Geometrie de l'ecran telephone (memes valeurs que l'overlay).
        # screen_radius sert aux apps qui clippent leur fond aux coins
        # arrondis (comme le home).
        self._screen_w   = int(screen_w)
        self._screen_h   = int(screen_h)
        self._screen_rad = int(screen_radius)
        self.services    = services
        # Garde anti-fuite de la touche d'OUVERTURE : quand un jeu capture-
        # clavier est lance via Entree depuis le home, cette meme Entree fuit
        # dans son keyPressEvent (focus Qt) et validerait le 1er bouton du
        # menu. Si l'overlay desarme cette garde au lancement, elle ne se
        # (re)arme qu'au relachement d'une touche de validation (cf.
        # keyReleaseEvent), si bien que l'Entree d'ouverture est ignoree.
        # Vrai par defaut (lancement souris / pas de fuite).
        self._confirm_armed = True
        # On NE fixe PAS la taille : le QStackedWidget redimensionne ses
        # pages. Les valeurs ci-dessus servent de reference au design.

    def keyReleaseEvent(self, event):
        """Arme la validation au relachement d'une touche de validation
        (Espace/Entree). Sert a ignorer la touche d'ouverture qui a fui dans
        un menu de jeu (cf. _confirm_armed)."""
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._confirm_armed = True
        super().keyReleaseEvent(event)

    # --- cycle de vie (defaut = no-op) ---
    def on_show(self) -> None:
        """Devient l'ecran courant. A surcharger pour (re)demarrer."""
        pass

    def on_hide(self) -> None:
        """Quitte l'ecran. A surcharger pour PAUSER timers/threads."""
        pass

    # --- navigation (defaut = ne consomme rien) ---
    def handle_nav(self, direction: str) -> bool:
        """D-pad pour les apps non-jeu. Retourne True si consomme."""
        return False

    def handle_back(self) -> bool:
        """Action 'retour' (Echap). Retourne True si l'app a consomme le
        retour (ex. revenir a son propre menu interne) ; False => l'overlay
        revient au home du telephone. Defaut : non consomme (-> home)."""
        return False


# ======================================================================
#  HomeEntry : une case du home.
# ======================================================================
@dataclass
class HomeEntry:
    """Une case de l'ecran d'accueil. Unifie les deux types de
    destinations :
      - une APP v0.3 (widget au contrat PhoneApp) ;
      - un ECRAN NATIF existant (contacts/appels) qu'on rejoint par un
        simple setCurrentWidget.

    Dans les deux cas, `launch` est un callable SANS argument construit par
    l'overlay (lui seul connait le stack et le cycle de vie). PhoneHome se
    contente de l'appeler — il ne sait pas ce qu'il y a derriere.

    Champs :
      entry_id : cle unique (pour debug / selection memorisee).
      name     : libelle sous l'icone.
      icon     : QPixmap, chemin str, glyphe (1 caractere) ou None.
      launch   : callable() -> affiche la destination (+ on_show cote app).
    """
    entry_id: str
    name:     str
    icon:     Union[QPixmap, str, None]
    launch:   Callable[[], None]


# ======================================================================
#  PhoneHome : l'ecran d'accueil (grille d'icones sur fond d'ecran).
# ======================================================================
class PhoneHome(QWidget):
    """Ecran d'accueil du CircusPhone. Pose en INDEX 0 du stack de
    l'overlay, c'est l'ecran par defaut a l'ouverture du telephone.

    Rendu : peint le fond d'ecran (clippe aux coins arrondis de l'ecran),
    puis une grille d'icones + libelles. La case selectionnee (navigation
    clavier) recoit un halo accent.

    Entrees : clic souris ET D-pad declenchent tous deux entry.launch().
    La geometrie est calculee dynamiquement depuis la taille courante du
    widget (responsive : si le stack le redimensionne, le rendu suit).
    """

    COLS = 3   # nombre de colonnes de la grille

    # Emis apres un lancement (entry_id), pour que l'overlay puisse tracer
    # l'app courante / le cycle de vie. Optionnel : la destination est deja
    # executee via entry.launch() ; ce signal est un simple hook.
    sig_launched = Signal(str)

    def __init__(self, screen_w: int, screen_h: int, screen_radius: int,
                 entries: Optional[list] = None,
                 wallpaper: Optional[QPixmap] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._screen_rad = int(screen_radius)
        self._entries: list = list(entries or [])
        self._wallpaper: Optional[QPixmap] = wallpaper
        self._sel = 0   # index selectionne (navigation D-pad)
        # Taille de reference (le widget peut etre redimensionne par le
        # stack ; le paint utilise self.width()/height() de toute facon).
        self.resize(int(screen_w), int(screen_h))

    # ------------------------------------------------------------------
    #  API publique (appelee par l'overlay)
    # ------------------------------------------------------------------
    def set_entries(self, entries: list) -> None:
        """Remplace la liste d'entrees et reaffiche."""
        self._entries = list(entries or [])
        self._sel = min(self._sel, max(0, len(self._entries) - 1))
        self.update()

    def set_wallpaper(self, pixmap: Optional[QPixmap]) -> None:
        """Change le fond d'ecran (None = aplat sombre) et reaffiche."""
        self._wallpaper = pixmap
        self.update()

    @property
    def selected_index(self) -> int:
        return self._sel

    def select(self, index: int) -> None:
        if 0 <= index < len(self._entries):
            self._sel = index
            self.update()

    # ------------------------------------------------------------------
    #  Navigation 2D (D-pad) — etend la logique 1D des contacts.
    # ------------------------------------------------------------------
    def handle_nav(self, direction: str) -> bool:
        """Deplace la selection dans la grille ; 'enter' lance la case.
        Retourne True (le home consomme toujours la touche tant qu'il est
        l'ecran courant). Memes directions que sig_nav_key cote overlay."""
        n = len(self._entries)
        if n == 0:
            return True
        c = self.COLS
        if direction == "left":
            self._sel = max(0, self._sel - 1)
        elif direction == "right":
            self._sel = min(n - 1, self._sel + 1)
        elif direction == "up":
            self._sel = max(0, self._sel - c)
        elif direction == "down":
            self._sel = min(n - 1, self._sel + c)
        elif direction == "enter":
            self._launch(self._sel)
            return True
        self.update()
        return True

    def _launch(self, index: int) -> None:
        if 0 <= index < len(self._entries):
            entry = self._entries[index]
            try:
                if callable(entry.launch):
                    entry.launch()
            finally:
                self.sig_launched.emit(entry.entry_id)

    # ------------------------------------------------------------------
    #  Geometrie de la grille (calculee depuis la taille courante).
    # ------------------------------------------------------------------
    def _layout_cells(self) -> list:
        """Retourne [(icon_rect, label_rect, index), ...] pour la taille
        courante. icon_rect est un carre ; label_rect est dessous."""
        w, h = self.width(), self.height()
        cols = self.COLS
        margin_x = w * 0.06
        top      = h * 0.09          # marge haute (laisse respirer le bandeau)
        usable_w = w - 2 * margin_x
        col_w    = usable_w / cols
        icon_sz  = col_w * 0.60      # cote du carre d'icone
        row_h    = col_w * 1.20      # icone + libelle + interligne
        cells = []
        for i in range(len(self._entries)):
            row = i // cols
            col = i % cols
            cx = margin_x + col * col_w + col_w / 2.0
            cy = top + row * row_h
            icon_rect = QRectF(cx - icon_sz / 2.0, cy, icon_sz, icon_sz)
            label_rect = QRectF(
                margin_x + col * col_w, cy + icon_sz + h * 0.006,
                col_w, row_h - icon_sz - h * 0.01
            )
            cells.append((icon_rect, label_rect, i))
        return cells

    # ------------------------------------------------------------------
    #  Rendu
    # ------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()

        # 1) Clip aux coins arrondis de l'ecran telephone.
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), self._screen_rad, self._screen_rad)
        p.setClipPath(path)

        # 2) Fond d'ecran (couvrant, recentre) ou aplat sombre de repli.
        if self._wallpaper is not None and not self._wallpaper.isNull():
            scaled = self._wallpaper.scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            # Recentrer le surplus.
            ox = (scaled.width() - w) // 2
            oy = (scaled.height() - h) // 2
            p.drawPixmap(0, 0, scaled, ox, oy, w, h)
            # Voile leger pour la lisibilite des libelles sur photo claire.
            p.fillRect(self.rect(), QColor(0, 0, 0, 60))
        else:
            # Aucun fond configure : degrade par defaut (au lieu d'un noir).
            g = QLinearGradient(0, 0, 0, h)
            g.setColorAt(0.0, QColor(HOME_BG_DEFAULT_TOP))
            g.setColorAt(1.0, QColor(HOME_BG_DEFAULT_BOT))
            p.fillRect(self.rect(), QBrush(g))
            rg = QRadialGradient(w * 0.5, h * 0.22, max(w, h) * 0.75)
            rg.setColorAt(0.0, QColor(255, 255, 255, 22))
            rg.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(self.rect(), QBrush(rg))

        # 3) Grille d'icones.
        cells = self._layout_cells()
        label_font = QFont()
        label_font.setPointSizeF(max(7.0, w * 0.035))
        for icon_rect, label_rect, i in cells:
            entry = self._entries[i]
            # Halo de selection (derriere l'icone).
            if i == self._sel:
                halo = icon_rect.adjusted(-4, -4, 4, 4)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(HOME_SEL_HALO))
                rad = icon_rect.width() * 0.28
                p.drawRoundedRect(halo, rad, rad)
            self._draw_icon(p, entry, icon_rect)
            # Libelle.
            p.setFont(label_font)
            p.setPen(QColor(HOME_TEXT))
            name = self._elide(entry.name, QFontMetrics(label_font),
                               int(label_rect.width()))
            p.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, name)
        p.end()

    def _draw_icon(self, p: QPainter, entry: HomeEntry, rect: QRectF) -> None:
        """Dessine l'icone d'une case. Gere QPixmap, chemin str, glyphe ou
        repli sur l'initiale du nom."""
        icon = entry.icon
        rad = rect.width() * 0.24

        pix: Optional[QPixmap] = None
        glyph: Optional[str] = None
        if isinstance(icon, QPixmap):
            pix = icon if not icon.isNull() else None
        elif isinstance(icon, str) and icon:
            # Chemin de fichier ? sinon on traite comme un glyphe court.
            if len(icon) <= 2:
                glyph = icon
            else:
                loaded = QPixmap(icon)
                pix = loaded if not loaded.isNull() else None

        if pix is not None:
            # Pixmap clippe dans un carre arrondi.
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(rect, rad, rad)
            p.setClipPath(clip)
            scaled = pix.scaled(
                int(rect.width()), int(rect.height()),
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            p.drawPixmap(rect.topLeft(), scaled)
            p.restore()
            return

        # Repli : tuile sombre + glyphe (ou initiale du nom).
        p.setPen(QColor(HOME_TILE_BORDER))
        p.setBrush(QColor(HOME_TILE_BG))
        p.drawRoundedRect(rect, rad, rad)
        text = glyph if glyph else (entry.name[:1].upper() if entry.name else "?")
        gf = QFont()
        gf.setPointSizeF(max(10.0, rect.height() * 0.42))
        gf.setBold(True)
        p.setFont(gf)
        p.setPen(QColor(HOME_TEXT))
        p.drawText(rect, Qt.AlignCenter, text)

    @staticmethod
    def _elide(text: str, fm: QFontMetrics, width: int) -> str:
        """Tronque avec '...' si le libelle depasse la largeur de cellule."""
        try:
            return fm.elidedText(text, Qt.ElideRight, max(8, width))
        except Exception:
            return text

    # ------------------------------------------------------------------
    #  Souris : clic sur une case = lancement.
    # ------------------------------------------------------------------
    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        px, py = pos.x(), pos.y()
        for icon_rect, label_rect, i in self._layout_cells():
            # Zone cliquable = icone + libelle (un peu elargie).
            hit = icon_rect.united(label_rect).adjusted(-4, -4, 4, 4)
            if hit.contains(px, py):
                self._sel = i
                self.update()
                self._launch(i)
                return


# ======================================================================
#  Registre des apps v0.3.
# ======================================================================
# LA SEULE LISTE A EDITER quand on ajoute une application. On y met les
# CLASSES (pas des instances) : le home lit leurs metadonnees sans les
# instancier, et l'overlay les instancie a la demande (lazy-load) au 1er
# lancement. Vide pour l'instant : on la remplit au fil de l'integration
# (Wallet en premier, puis Snake, etc.).
#
# Exemple une fois les apps adaptees au contrat :
#   from circusvoip_phone_wallet import WalletApp
#   from circusvoip_phone_snake  import SnakeApp
#   PHONE_APPS = [WalletApp, SnakeApp]
PHONE_APPS: list = []


def build_app_entries(app_classes: list,
                      launcher: Callable[[type], Callable[[], None]]) -> list:
    """Transforme une liste de classes PhoneApp en liste de HomeEntry.

    `launcher` est fourni par l'overlay : il prend une classe d'app et
    retourne le callable de lancement correspondant (qui instancie en lazy,
    fait setCurrentWidget et appelle on_show). On garde cette fabrique
    cote overlay car elle a besoin du stack ; ce module reste sans etat.

    Le `C=AppClass` dans la lambda capture la classe par valeur (sinon
    toutes les entrees referenceraient la derniere classe de la boucle).
    """
    entries = []
    for AppClass in app_classes:
        entries.append(HomeEntry(
            entry_id=AppClass.APP_ID,
            name=AppClass.APP_NAME,
            icon=AppClass.APP_ICON,
            launch=launcher(AppClass),
        ))
    return entries


# ======================================================================
#  HARNAIS DE TEST VISUEL — supprimable.
#  Ne fait PAS partie de l'integration. Monte un PhoneHome dans un chassis
#  CircusPhone (memes maths que l'overlay) pour verifier rendu + nav.
#    Fleches / clic : naviguer    Entree : lancer (print)    Echap : quitter
# ======================================================================
def _harness_main() -> int:
    app = QApplication(sys.argv)

    # Geometrie chassis identique a PhoneOverlayWindow.
    screen = QGuiApplication.primaryScreen()
    try:
        geo = screen.availableGeometry()
        scr_h = geo.height()
    except Exception:
        scr_h = 1080
    body_h = max(420, min(760, int(scr_h * 0.62)))
    body_w = int(body_h * (200.0 / 440.0))
    sx, sy = body_w / 200.0, body_h / 440.0
    radius   = int(28 * sx)
    banner_h = int(56 * sy)
    screen_x = int(12 * sx)
    screen_y = banner_h
    screen_w = body_w - 2 * screen_x
    screen_h = body_h - banner_h - int(16 * sy)
    screen_rad = int(14 * sx)

    class _Chassis(QWidget):
        """Fenetre frameless qui peint le chassis et heberge le home."""
        def __init__(self):
            super().__init__(
                None,
                Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
            )
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(body_w, body_h)
            self._drag = None

            # Entrees de demo : glyphes uniquement (le harnais ne charge pas
            # d'apps reelles). launch = print, pour verifier le declenchement.
            def mk(name):
                return lambda: print(f"[HOME] launch -> {name}")
            entries = [
                HomeEntry("calls", "Appels", "\u260E", mk("Appels")),
                HomeEntry("msg",   "Messagerie", "\u2709", mk("Messagerie")),
                HomeEntry("wallet", "Portefeuille", "\u20B5", mk("Portefeuille")),
                HomeEntry("snake", "Snake", "\U0001F40D", mk("Snake")),
                HomeEntry("poker", "Poker", "\u2660", mk("Poker")),
                HomeEntry("photo", "Photo", "\U0001F4F7", mk("Photo")),
            ]
            self.home = PhoneHome(screen_w, screen_h, screen_rad, entries, None, self)
            self.home.move(screen_x, screen_y)
            self.home.resize(screen_w, screen_h)

        def paintEvent(self, _ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            # corps
            p.setBrush(QColor(PHONE_BODY_COLOR))
            p.drawRoundedRect(0, 0, body_w, body_h, radius, radius)
            # boutons lateraux decoratifs
            p.setBrush(QColor(PHONE_BTN_COLOR))
            bw = max(2, int(body_w * 0.015))
            p.drawRoundedRect(0, int(body_h * 0.25), bw, int(body_h * 0.05), 1, 1)
            p.drawRoundedRect(0, int(body_h * 0.33), bw, int(body_h * 0.09), 1, 1)
            p.drawRoundedRect(body_w - bw, int(body_h * 0.36), bw,
                              int(body_h * 0.14), 1, 1)
            # bandeau "Circus" gris + "Phone" blanc
            f1 = QFont(); f1.setPointSizeF(max(7.0, body_w * 0.05))
            f2 = QFont(); f2.setPointSizeF(max(10.0, body_w * 0.085)); f2.setBold(True)
            fm1 = QFontMetrics(f1)
            t1, t2 = "Circus", "Phone"
            total = fm1.horizontalAdvance(t1) + QFontMetrics(f2).horizontalAdvance(t2)
            x0 = (body_w - total) / 2.0
            base = banner_h * 0.66
            p.setFont(f1); p.setPen(QColor(PHONE_BANNER_GREY))
            p.drawText(int(x0), int(base), t1)
            p.setFont(f2); p.setPen(QColor(PHONE_BANNER_WHITE))
            p.drawText(int(x0 + fm1.horizontalAdvance(t1)), int(base), t2)
            p.end()

        # Clavier -> handle_nav du home (simule sig_nav_key de l'overlay).
        def keyPressEvent(self, ev):
            k = ev.key()
            if k == Qt.Key_Escape:
                self.close(); return
            mapping = {
                Qt.Key_Left: "left", Qt.Key_Right: "right",
                Qt.Key_Up: "up", Qt.Key_Down: "down",
                Qt.Key_Return: "enter", Qt.Key_Enter: "enter",
                Qt.Key_Space: "enter",
            }
            d = mapping.get(k)
            if d:
                self.home.handle_nav(d)

        # Deplacer la fenetre en glissant le chassis.
        def mousePressEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
        def mouseMoveEvent(self, ev):
            if self._drag is not None and (ev.buttons() & Qt.LeftButton):
                self.move(ev.globalPosition().toPoint() - self._drag)
        def mouseReleaseEvent(self, _ev):
            self._drag = None

    win = _Chassis()
    win.show()
    win.raise_()
    win.activateWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(_harness_main())
