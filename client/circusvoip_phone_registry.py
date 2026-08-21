# -*- coding: utf-8 -*-
"""
circusvoip_phone_registry
=========================

Registre des applications v0.3 du CircusPhone. C'EST LA SEULE LISTE A
EDITER quand on ajoute une app.

IMPORTANT (robustesse) : chaque app est importee INDEPENDAMMENT, dans son
propre try/except. Si un fichier d'app manque ou plante a l'import, SEULE
cette app est retiree du registre -- le reste du telephone continue de
fonctionner. Avant, un import "dur" en tete de module faisait echouer toute
la chaine (et l'overlay retombait sur l'ecran Contacts, home vide).

L'overlay lit PHONE_APPS pour construire les icones du home et instancier
chaque app a la demande. PHONE_GAMES = jeux ranges dans le dossier "Jeux".
"""

from __future__ import annotations

import sys


def _imp(module_name, class_name):
    """Importe class_name depuis module_name. Retourne la classe ou None
    (en loguant discretement sur stderr) si l'import echoue."""
    try:
        mod = __import__(module_name)
        return getattr(mod, class_name)
    except Exception as e:
        try:
            sys.stderr.write(
                f"[PHONE REGISTRY] app ignoree : {module_name}.{class_name} "
                f"-> {type(e).__name__}: {e}\n")
        except Exception:
            pass
        return None


# --- Jeux (dossier "Jeux"). Importes un par un. ----------------------
_GAME_SPECS = [
    ("circusvoip_phone_valakkar",   "ValakkarApp"),
    ("circusvoip_phone_solvsterra", "SolVsTerraApp"),
    ("circusvoip_phone_poker",      "PokerApp"),
    # Billard en DERNIER : son icone n'apparait que pres d'une vraie table
    # (visibilite conditionnelle) ; en 4e position elle s'ajoute a la fin de
    # la grille sans decaler les autres jeux.
    ("circusvoip_phone_billard",    "BilliardApp"),
]
PHONE_GAMES = [c for c in (_imp(m, n) for (m, n) in _GAME_SPECS) if c is not None]

# --- Apps de la racine du home (hors jeux). --------------------------
UrgenceApp      = _imp("circusvoip_phone_urgence_app", "UrgenceApp")
WalletApp       = _imp("circusvoip_phone_wallet",   "WalletApp")
BlueprintsApp   = _imp("circusvoip_phone_blueprints", "BlueprintsApp")
SettingsApp     = _imp("circusvoip_phone_settings", "SettingsApp")
GamesFolderApp  = _imp("circusvoip_phone_games",    "GamesFolderApp")
PhotosApp       = _imp("circusvoip_phone_photos",   "PhotosApp")

# Ordre d'apparition apres les ecrans natifs (Appels/Messagerie). On ne
# garde que les apps reellement importees. Le dossier Jeux n'apparait que
# s'il a pu etre importe ET qu'au moins un jeu est disponible.
# NB : Camera (fenetre separee), Photos et Parametres sont ajoutes A PART
# par l'overlay, dans cet ordre et EN DERNIER, apres Wallet/Jeux.
PHONE_APPS = []
# [URGENCE 12/08/2026] En TETE des apps : c'est la seule dont on ait
# besoin dans l'urgence, et la chercher au milieu des autres coute des
# secondes exactement quand elles comptent.
if UrgenceApp is not None:
    PHONE_APPS.append(UrgenceApp)
if WalletApp is not None:
    PHONE_APPS.append(WalletApp)
if BlueprintsApp is not None:
    PHONE_APPS.append(BlueprintsApp)
if GamesFolderApp is not None and PHONE_GAMES:
    PHONE_APPS.append(GamesFolderApp)

# Apps ajoutees explicitement par l'overlay, apres la Camera, dans l'ordre
# Camera -> Photos -> Parametres (Parametres toujours en dernier). None si
# l'import a echoue.
PHOTOS_APP   = PhotosApp
SETTINGS_APP = SettingsApp
