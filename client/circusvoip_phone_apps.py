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
    QGuiApplication, QLinearGradient, QRadialGradient, QBrush, QPen,
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
# Fond d'ecran PAR DEFAUT = NOIR (release build 62). Les deux stops identiques
# => aplat noir. Doit matcher _DEFAULT_TOP/_BOT de circusvoip_phone_settings.
HOME_BG_DEFAULT_TOP = "#000000"
HOME_BG_DEFAULT_BOT = "#000000"
HOME_TILE_BG       = "#161b22"   # fond d'une tuile d'icone (glyphe)
HOME_TILE_BORDER   = "#30363d"   # bord de tuile
HOME_TEXT          = "#c9d1d9"   # libelle d'app
HOME_TEXT_MUTED    = "#6e7681"   # secondaire
HOME_SEL_HALO      = "#2f6fed"   # halo de selection (accent)


# ======================================================================
#  [build 61] Fabrique d'icones vectorielles du CircusPhone.
# ======================================================================
# Probleme resolu : les icones etaient des emoji rendus par la POLICE
# SYSTEME -> visuel different selon le PC de chaque joueur (Win10 / Win11 /
# police manquante = carre vide). Choix retenu (17/07/2026, apres essai
# d'icones dessinees jugees moins lisibles) : garder LE MEME visuel emoji
# qu'avant, mais embarque en dur dans le code.
#
# Usage cote app :   APP_ICON = LazyPhoneIcon("wallet", "\u20B5")
# Usage cote home :  make_phone_icon("calls")  (repli glyphe si echec)
# Le 2e argument est le GLYPHE DE REPLI (l'ancien emoji) si Qt n'est pas
# pret ou si le dessin echoue — jamais le cas en pratique, ceinture.

# Les emoji sont EMBARQUES en dur : ce sont les SVG Twemoji officiels
# (jdecked/twemoji, graphismes (c) Twitter/X et contributeurs, licence
# CC-BY 4.0 — attribution requise, la voici). Rendu via QtSvg -> le VRAI
# dessin d'emoji, strictement identique sur tous les PC, sans dependre de
# la police systeme (Win10/Win11/police manquante) et sans aucun fichier.
# Le Portefeuille (glyphe monetaire \u20B5, pas un emoji) est dessine.
_EMOJI_SVG = {
    "calls":  # 1F4DE combine, recolore bleu clair #4a90e2
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#4a90e2\" d=\"M34.06 26.407l-3.496-3.496c-1.93-1.93-5.06-1.93-6.989 0-.719.718-1.167 1.603-1.351 2.528-5.765-1.078-11.372-6.662-11.721-11.653.947-.176 1.854-.627 2.586-1.36 1.93-1.93 1.93-5.06 0-6.99L9.594 1.94c-1.93-1.93-5.06-1.93-6.99 0-10.486 10.486 20.97 41.942 31.456 31.456 1.929-1.929 1.929-5.059 0-6.989z\"/></svg>",
    "msg":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#CCD6DD\" d=\"M36 27c0 2.209-1.791 4-4 4H4c-2.209 0-4-1.791-4-4V9c0-2.209 1.791-4 4-4h28c2.209 0 4 1.791 4 4v18z\"/><path fill=\"#99AAB5\" d=\"M11.95 17.636L.637 28.949c-.027.028-.037.063-.06.091.34.57.814 1.043 1.384 1.384.029-.023.063-.033.09-.06L13.365 19.05c.39-.391.39-1.023 0-1.414-.392-.391-1.024-.391-1.415 0M35.423 29.04c-.021-.028-.033-.063-.06-.09L24.051 17.636c-.392-.391-1.024-.391-1.415 0-.391.392-.391 1.024 0 1.414l11.313 11.314c.026.026.062.037.09.06.571-.34 1.044-.814 1.384-1.384\"/><path fill=\"#99AAB5\" d=\"M32 5H4C1.791 5 0 6.791 0 9v1.03l14.528 14.496c1.894 1.893 4.988 1.893 6.884 0L36 10.009V9c0-2.209-1.791-4-4-4z\"/><path fill=\"#E1E8ED\" d=\"M32 5H4C2.412 5 1.051 5.934.405 7.275l14.766 14.767c1.562 1.562 4.096 1.562 5.657 0L35.595 7.275C34.949 5.934 33.589 5 32 5z\"/></svg>",
    "camera":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#66757F\" d=\"M4 5s0-1 1-1h6s1 0 1 1v2H4V5z\"/><path fill=\"#31373D\" d=\"M0 10s0-4 4-4h28s4 0 4 4v18s0 4-4 4H4s-4 0-4-4V10z\"/><circle fill=\"#CCD6DD\" cx=\"21\" cy=\"19\" r=\"10\"/><circle fill=\"#31373D\" cx=\"21\" cy=\"19\" r=\"8\"/><circle fill=\"#3B88C3\" cx=\"21\" cy=\"19\" r=\"5\"/><circle fill=\"#FFF\" cx=\"32.5\" cy=\"9.5\" r=\"1.5\"/><path fill=\"#F5F8FA\" d=\"M12 9.5c0 .829-.671 1.5-1.5 1.5h-5C4.671 11 4 10.329 4 9.5S4.671 8 5.5 8h5c.829 0 1.5.671 1.5 1.5z\"/></svg>",
    "blueprints":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#FFCC4D\" d=\"M35.106 33.172L2.828.894C1.273-.662 0-.135 0 2.065V32c0 2.2 1.8 4 4 4h29.935c2.2 0 2.727-1.272 1.171-2.828zM16.967 28H10c-1.1 0-2-.9-2-2v-6.968c0-1.1.637-1.363 1.414-.586l8.139 8.14c.777.777.513 1.414-.586 1.414z\"/></svg>",
    "games":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#31373D\" d=\"M2.13 14.856l-.004-.002S.075 27.271.075 29.061c0 1.824 1.343 3.302 3 3.302.68 0 1.3-.258 1.803-.678l10.166-8.938L2.13 14.856zm31.69 0l.004-.002s2.051 12.417 2.051 14.207c0 1.824-1.343 3.302-3 3.302-.68 0-1.3-.258-1.803-.678l-10.166-8.938 12.914-7.891z\"/><g fill=\"#14171A\"><circle cx=\"25.975\" cy=\"15.551\" r=\"8.5\"/><circle cx=\"9.975\" cy=\"15.551\" r=\"8.5\"/><path d=\"M9.975 7.051h16v16.87h-16z\"/></g><circle fill=\"#14171A\" cx=\"13.075\" cy=\"23.301\" r=\"5\"/><circle fill=\"#14171A\" cx=\"22.875\" cy=\"23.301\" r=\"5\"/><circle fill=\"#67757F\" cx=\"22.875\" cy=\"23.301\" r=\"3\"/><circle fill=\"#67757F\" cx=\"13.075\" cy=\"23.301\" r=\"3\"/><circle fill=\"#FFCC4D\" cx=\"25.735\" cy=\"11.133\" r=\"1.603\"/><circle fill=\"#77B255\" cx=\"25.735\" cy=\"17.607\" r=\"1.603\"/><circle fill=\"#50A5E6\" cx=\"22.498\" cy=\"14.37\" r=\"1.603\"/><circle fill=\"#DD2E44\" cx=\"28.972\" cy=\"14.37\" r=\"1.603\"/><path d=\"M11.148 12.514v-2.168c0-.279-.226-.505-.505-.505H9.085c-.279 0-.505.226-.505.505v2.168l1.284 1.285 1.284-1.285zm-2.569 3.63v2.168c0 .279.226.505.505.505h1.558c.279 0 .505-.226.505-.505v-2.168l-1.284-1.285-1.284 1.285zm5.269-3.1H11.68l-1.285 1.285 1.285 1.285h2.168c.279 0 .505-.227.505-.505V13.55c0-.279-.226-.506-.505-.506zm-5.799 0H5.88c-.279 0-.505.227-.505.505v1.558c0 .279.226.505.505.505h2.168l1.285-1.285-1.284-1.283z\" fill=\"#8899A6\"/></svg>",
    "settings":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#66757F\" d=\"M34 15h-3.362c-.324-1.369-.864-2.651-1.582-3.814l2.379-2.379c.781-.781.781-2.048 0-2.829l-1.414-1.414c-.781-.781-2.047-.781-2.828 0l-2.379 2.379C23.65 6.225 22.369 5.686 21 5.362V2c0-1.104-.896-2-2-2h-2c-1.104 0-2 .896-2 2v3.362c-1.369.324-2.651.864-3.814 1.582L8.808 4.565c-.781-.781-2.048-.781-2.828 0L4.565 5.979c-.781.781-.781 2.048-.001 2.829l2.379 2.379C6.225 12.35 5.686 13.632 5.362 15H2c-1.104 0-2 .896-2 2v2c0 1.104.896 2 2 2h3.362c.324 1.368.864 2.65 1.582 3.813l-2.379 2.379c-.78.78-.78 2.048.001 2.829l1.414 1.414c.78.78 2.047.78 2.828 0l2.379-2.379c1.163.719 2.445 1.258 3.814 1.582V34c0 1.104.896 2 2 2h2c1.104 0 2-.896 2-2v-3.362c1.368-.324 2.65-.864 3.813-1.582l2.379 2.379c.781.781 2.047.781 2.828 0l1.414-1.414c.781-.781.781-2.048 0-2.829l-2.379-2.379c.719-1.163 1.258-2.445 1.582-3.814H34c1.104 0 2-.896 2-2v-2C36 15.896 35.104 15 34 15zM18 26c-4.418 0-8-3.582-8-8s3.582-8 8-8 8 3.582 8 8-3.582 8-8 8z\"/></svg>",
    "valakkar":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#DD2E44\" d=\"M11.84 7.634c-.719 0-2.295 2.243-3.567 1.029-.44-.419 1.818-1.278 1.727-2.017-.075-.607-2.842-1.52-1.875-2.099.967-.578 2.418.841 3.513.866 2.382.055 4.212-.853 4.238-.866.541-.274 1.195-.052 1.464.496.27.547.051 1.213-.488 1.486-.131.066-2.225 1.105-5.012 1.105z\"/><path fill=\"#77B255\" d=\"M27.818 36c-3.967 0-8.182-2.912-8.182-8.308 0-1.374-.89-1.661-1.637-1.661-.746 0-1.636.287-1.636 1.661 0 5.396-4.216 8.308-8.182 8.308S0 33.23 0 27.692C0 14.4 14.182 12.565 14.182 14.4c0 1.835-7.636-1.107-7.636 12.185 0 2.215.89 2.769 1.636 2.769.747 0 1.637-.287 1.637-1.661 0-5.395 4.215-8.308 8.182-8.308 3.966 0 8.182 2.912 8.182 8.308 0 1.374.89 1.661 1.637 1.661s1.636-.287 1.636-1.661V11.077c0-3.855-3.417-4.431-5.454-4.431 0 0-3.272 1.108-6.545 1.108s-4.364-2.596-4.364-4.431C13.091 1.488 17.455 0 24 0c6.546 0 12 4.451 12 11.077v16.615C36 33.088 31.784 36 27.818 36z\"/><circle fill=\"#292F33\" cx=\"19\" cy=\"3\" r=\"1\"/></svg>",
    "solvsterra":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#CCD6DD\" d=\"M24 29l5-5L6 1H1v5z\"/><path fill=\"#9AAAB4\" d=\"M1 1v5l23 23 2.5-2.5z\"/><path fill=\"#D99E82\" d=\"M33.424 32.808c.284-.284.458-.626.531-.968l-5.242-6.195-.701-.702c-.564-.564-1.57-.473-2.248.205l-.614.612c-.677.677-.768 1.683-.204 2.247l.741.741 6.15 5.205c.345-.072.688-.247.974-.532l.613-.613z\"/><path d=\"M33.424 32.808c.284-.284.458-.626.531-.968l-1.342-1.586-.737 3.684c.331-.077.661-.243.935-.518l.613-.612zm-3.31-5.506l-.888 4.441 1.26 1.066.82-4.1zm-1.401-1.657l-.701-.702c-.096-.096-.208-.166-.326-.224l-.978 4.892 1.26 1.066.957-4.783-.212-.249zm-2.401-.888c-.195.095-.382.225-.548.392l-.614.611c-.254.254-.425.554-.511.86-.142.51-.046 1.035.307 1.387l.596.596.77-3.846c0-.001 0-.001 0 0z\" fill=\"#BF6952\"/><circle fill=\"#8A4633\" cx=\"33.25\" cy=\"33.25\" r=\"2.75\"/><path fill=\"#FFAC33\" d=\"M29.626 22.324c.404.404.404 1.059 0 1.462l-6.092 6.092c-.404.404-1.058.404-1.462 0-.404-.404-.404-1.058 0-1.462l6.092-6.092c.402-.404 1.058-.404 1.462 0z\"/><circle fill=\"#FFAC33\" cx=\"22.072\" cy=\"29.877\" r=\"1.75\"/><circle fill=\"#FFAC33\" cx=\"29.626\" cy=\"22.323\" r=\"1.75\"/><circle fill=\"#FFCC4D\" cx=\"22.072\" cy=\"29.877\" r=\"1\"/><circle fill=\"#FFCC4D\" cx=\"29.626\" cy=\"22.323\" r=\"1\"/><path fill=\"#FFAC33\" d=\"M33.903 29.342c.298.298.298.781 0 1.078l-3.476 3.475c-.298.298-.78.298-1.078 0-.298-.298-.298-.78 0-1.078l3.476-3.475c.297-.298.78-.298 1.078 0z\"/><path fill=\"#CCD6DD\" d=\"M12 29l-5-5L30 1h5v5z\"/><path fill=\"#9AAAB4\" d=\"M35 1v5L12 29l-2.5-2.5z\"/><path fill=\"#D99E82\" d=\"M2.576 32.808c-.284-.284-.458-.626-.531-.968l5.242-6.195.701-.702c.564-.564 1.57-.473 2.248.205l.613.612c.677.677.768 1.683.204 2.247l-.741.741-6.15 5.205c-.345-.072-.688-.247-.974-.532l-.612-.613z\"/><path d=\"M2.576 32.808c-.284-.284-.458-.626-.531-.968l1.342-1.586.737 3.684c-.331-.077-.661-.243-.935-.518l-.613-.612zm3.31-5.506l.888 4.441-1.26 1.066-.82-4.1zm1.401-1.657l.701-.702c.096-.096.208-.166.326-.224l.978 4.892-1.26 1.066-.957-4.783.212-.249zm2.401-.888c.195.095.382.225.548.392l.613.612c.254.254.425.554.511.86.142.51.046 1.035-.307 1.387l-.596.596-.769-3.847c0-.001 0-.001 0 0z\" fill=\"#BF6952\"/><circle fill=\"#8A4633\" cx=\"2.75\" cy=\"33.25\" r=\"2.75\"/><path fill=\"#FFAC33\" d=\"M6.374 22.324c-.404.404-.404 1.059 0 1.462l6.092 6.092c.404.404 1.058.404 1.462 0 .404-.404.404-1.058 0-1.462l-6.092-6.092c-.402-.404-1.058-.404-1.462 0z\"/><circle fill=\"#FFAC33\" cx=\"13.928\" cy=\"29.877\" r=\"1.75\"/><circle fill=\"#FFAC33\" cx=\"6.374\" cy=\"22.323\" r=\"1.75\"/><circle fill=\"#FFCC4D\" cx=\"13.928\" cy=\"29.877\" r=\"1\"/><circle fill=\"#FFCC4D\" cx=\"6.374\" cy=\"22.323\" r=\"1\"/><path fill=\"#FFAC33\" d=\"M2.097 29.342c-.298.298-.298.781 0 1.078l3.476 3.475c.298.298.78.298 1.078 0 .298-.298.298-.78 0-1.078l-3.476-3.475c-.297-.298-.78-.298-1.078 0z\"/></svg>",
    "poker":  # 2660 pique, recolore blanc #f2f4f7 (lisibilite sur tuile sombre)
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><path fill=\"#f2f4f7\" d=\"M32.799 20.336C32.799 11.456 18 .198 18 .198S3.201 11.456 3.201 20.336c0 6.946 8.175 10.172 12.766 5.173C15.631 29.688 11.247 33 7 33h.5c-.829 0-1.5.672-1.5 1.5S6.671 36 7.5 36h21c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5h.5c-4.246 0-8.632-3.312-8.967-7.491 4.591 4.999 12.766 1.773 12.766-5.173z\"/></svg>",
    "billard":
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 36 36\"><circle fill=\"#31373D\" cx=\"18\" cy=\"18\" r=\"18\"/><circle fill=\"#E1E8ED\" cx=\"18\" cy=\"18\" r=\"9\"/><path fill=\"#31373D\" d=\"M13.703 20.203c0-1.406.773-2.443 1.881-3.041-.826-.598-1.336-1.406-1.336-2.514 0-2.057 1.705-3.375 3.797-3.375 2.039 0 3.814 1.301 3.814 3.375 0 .984-.492 1.969-1.354 2.514 1.195.598 1.881 1.688 1.881 3.041 0 2.443-1.986 4.008-4.342 4.008-2.425 0-4.341-1.652-4.341-4.008zm2.742-.176c0 .896.527 1.758 1.6 1.758 1.002 0 1.6-.861 1.6-1.758 0-1.107-.633-1.758-1.6-1.758-1.02.001-1.6.774-1.6 1.758zm.334-5.097c0 .791.457 1.336 1.266 1.336.809 0 1.283-.545 1.283-1.336 0-.756-.457-1.336-1.283-1.336-.826 0-1.266.58-1.266 1.336z\"/></svg>",
}


def make_phone_icon(kind: str, size: int = 128):
    """Icone de l'ecran d'accueil : tuile sombre (identique au rendu
    glyphe historique de _draw_icon) + emoji Twemoji embarque, ou glyphe
    \u20B5 dessine pour le Portefeuille. Retourne None si Qt/QtSvg
    indisponibles ou kind inconnu (l'appelant retombe sur son glyphe)."""
    if kind != "wallet" and kind not in _EMOJI_SVG:
        return None
    try:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = float(size)
        # Tuile sombre arrondie : memes couleurs et meme rayon (24% de la
        # largeur) que le repli glyphe de PhoneHome._draw_icon.
        rad = s * 0.24
        p.setPen(QColor(HOME_TILE_BORDER))
        p.setBrush(QColor(HOME_TILE_BG))
        p.drawRoundedRect(QRectF(0.5, 0.5, s - 1, s - 1), rad, rad)

        if kind == "wallet":
            # Glyphe cedi (\u20B5) dessine : un C epais + barre verticale.
            pen = QPen(QColor(HOME_TEXT), s * 0.085, Qt.SolidLine,
                       Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            r = s * 0.24
            c = s / 2.0
            p.drawArc(QRectF(c - r, c - r, 2 * r, 2 * r), 50 * 16, 260 * 16)
            p.drawLine(int(c), int(c - r * 1.45), int(c), int(c + r * 1.45))
        else:
            from PySide6.QtSvg import QSvgRenderer
            renderer = QSvgRenderer()
            ok = renderer.load(bytes(_EMOJI_SVG[kind], "utf-8"))
            if not ok:
                p.end()
                return None
            # Emoji centre a ~64% de la tuile (equivalent visuel du glyphe
            # historique a 42% de point-size).
            m = s * 0.18
            renderer.render(p, QRectF(m, m, s - 2 * m, s - 2 * m))

        p.end()
        if not pm.isNull():
            return pm
    except Exception:
        pass
    return None


class LazyPhoneIcon:
    """Descripteur generique : dessine l'icone `kind` au PREMIER acces a
    Class.APP_ICON (quand Qt est pret), la met en cache SUR LA CLASSE
    porteuse, et retombe sur `fallback` (l'ancien glyphe emoji) si le
    dessin echoue. Generalisation du pattern _LazyPolaroidIcon de l'app
    Photos a toutes les icones du telephone (build 61 : fin des emoji,
    rendu identique sur tous les PC)."""

    def __init__(self, kind: str, fallback: str = ""):
        self._kind = kind
        self._fallback = fallback
        self._attr = f"_icon_cache_{kind}"

    def __get__(self, obj, objtype=None):
        cls = objtype or type(obj)
        cached = getattr(cls, self._attr, None)
        if cached is None:
            cached = make_phone_icon(self._kind)
            if cached is None:
                return self._fallback     # pas de cache : on retentera
            setattr(cls, self._attr, cached)
        return cached


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
        # [build 61] entry_ids porteurs d'un badge de notification (rond
        # rouge en coin d'icone, comme la pastille unread des contacts).
        # Rempli par l'overlay via set_badges() ; le home ne calcule rien.
        self._badges: set = set()
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

    def set_badges(self, entry_ids) -> None:
        """[build 61] Definit l'ensemble des entry_ids qui affichent un
        badge de notification (rond rouge en coin d'icone). Idempotent :
        ne repeint que si l'ensemble change. L'overlay pousse l'etat (ex.
        {"msg"} quand il existe des MP non lus, set() sinon) ; le home ne
        connait pas la semantique, il dessine."""
        new = set(entry_ids or ())
        if new != self._badges:
            self._badges = new
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
            # [build 61] Badge de notification : rond rouge en coin
            # superieur droit de l'icone (meme rouge #e5484d que la
            # pastille unread des lignes de contact). Lisere sombre pour
            # rester lisible sur fond d'ecran clair. Dessine APRES l'icone
            # pour deborder legerement du coin (style smartphone).
            if entry.entry_id in self._badges:
                d = max(8.0, icon_rect.width() * 0.26)
                bx = icon_rect.right() - d * 0.62
                by = icon_rect.top() - d * 0.38
                p.setPen(QPen(QColor(0, 0, 0, 170), max(1.0, d * 0.10)))
                p.setBrush(QColor("#e5484d"))
                p.drawEllipse(QRectF(bx, by, d, d))
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
