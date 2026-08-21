#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[DISCORD 30/07/2026] Store des comptes / annuaire -- SERVEUR uniquement.

Socle du chantier "comptes Discord + annuaire" (point 10). Volontairement
SANS reseau et SANS dependance a Discord : ce module ne fait que gerer le
modele de donnees, l'unicite des pseudos et l'attribution des numeros. La
partie OAuth vient par-dessus et se contente d'appeler link_account() une
fois qu'elle a etabli un discord_id de facon fiable.

Decoupage voulu : ce fichier est testable seul, sans VPS, sans compte
Discord, sans serveur qui tourne.

--- Modele ---

L'identite est ancree sur le DISCORD_ID, jamais sur le pseudo :
  - le pseudo peut changer, la fiche (numero compris) suit ;
  - le pseudo Discord peut changer aussi, on ne s'en sert JAMAIS comme cle,
    il n'est stocke que pour affichage dans l'admin.

Chaque compte porte un JETON LOCAL emis par nous. Apres la liaison
initiale, c'est lui qui identifie le joueur a chaque connexion : Discord
n'est PAS resollicite. Consequences voulues :
  - Discord indisponible n'empeche personne de jouer, seules les nouvelles
    inscriptions sont bloquees ;
  - aucun token Discord n'est stocke chez le joueur ni chez nous ;
  - perte du jeton (changement de machine) -> on relance la liaison
    Discord, qui retombe sur le meme discord_id donc la meme fiche.

Le jeton n'est stocke que HACHE (sha256). Un vol du fichier de comptes ne
permet pas de se faire passer pour un joueur.

--- Unicite des pseudos ---

Insensible a la casse : "Hugo" et "hugo" sont le meme pseudo. On conserve
la forme SAISIE pour l'affichage et une cle NORMALISEE pour l'unicite.
casefold() plutot que lower() : identique sur l'ASCII, correct au-dela.
Les accents ne sont PAS replies (decision du 30/07) : "Hugo" et "Hugo"
accentue restent deux pseudos distincts.

--- Numeros ---

Plage 420000-429999 (10 000 numeros). Tirage ALEATOIRE parmi les libres,
et non sequentiel : un numero sequentiel trahirait l'ordre d'arrivee des
joueurs, ce qui n'a pas sa place en RP. Attribue une seule fois, a la
creation ; il survit aux changements de pseudo.

--- Annuaire ---

Reserve a l'ADMIN (decision du 30/07). Aucune fonction de ce module n'est
destinee a repondre a une requete joueur : il n'y a volontairement PAS de
recherche par pseudo ou par numero exposable au client. Le seul besoin du
client est son propre numero, rendu par le compte qu'il authentifie.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import threading
import time
from pathlib import Path

# ---------------------------------------------
#  Constantes
# ---------------------------------------------

# Plage de numeros de telephone RP.
NUMERO_MIN = 420000
NUMERO_MAX = 429999

# Longueur du jeton local (octets avant encodage url-safe).
_TOKEN_BYTES = 32

# [ROTATION 05/08/2026] Duree de survie de l'ANCIEN jeton apres une
# rotation. Elle ne couvre qu'un seul scenario : le client a recu son
# nouveau jeton dans le "welcome" mais a plante avant de l'ecrire sur
# disque -- il se reconnecterait alors avec un jeton deja invalide et
# se verrait imposer une re-liaison Discord incomprehensible.
#
# En marche normale cette fenetre ne s'ouvre PAS : la tolerance meurt au
# PREMIER des deux evenements -- delai ecoule OU premiere presentation
# reussie du nouveau jeton (cf. clear_prev_token). Le client se
# reconnectant avec son jeton neuf, l'ancien meurt en quelques secondes,
# pas en cinq minutes. Un voleur ne gagne donc pas une fenetre permanente
# de cinq minutes : il gagne le temps qui le separe de la prochaine
# connexion du vrai joueur, qui referme la porte.
_PREV_TOKEN_GRACE_S = 300.0

# Bornes du pseudo. Le max evite qu'un pseudo casse les affichages
# (table des joueurs, overlays, admin) ; le min evite les pseudos vides
# ou d'un seul caractere, trop faciles a confondre.
PSEUDO_MIN_LEN = 3
PSEUDO_MAX_LEN = 24

# Caracteres refuses dans un pseudo. On reste permissif (le RP aime les
# noms exotiques) mais on ferme ce qui casse l'affichage ou le JSON.
_PSEUDO_FORBIDDEN = set('\t\r\n\x00"\\')

_DEFAULT_PATH = Path(__file__).resolve().parent / "circusvoip_accounts.json"

_SCHEMA_VERSION = 1


class AccountError(Exception):
    """Erreur metier (pseudo invalide, deja pris, plage saturee...).

    Distincte des erreurs d'IO : l'appelant doit pouvoir renvoyer ca au
    client comme un refus normal, pas comme une panne serveur.
    """


# ---------------------------------------------
#  Normalisation des pseudos
# ---------------------------------------------

def normalize_pseudo(pseudo: str) -> str:
    """Retourne la cle d'unicite d'un pseudo (casse ignoree, espaces
    de bord retires, espaces internes multiples reduits a un seul).

    Ne valide RIEN : sert aussi a comparer un pseudo existant, meme si
    les regles de validation ont change depuis sa creation.
    """
    if not isinstance(pseudo, str):
        return ""
    # Les espaces internes multiples sont reduits : "Hugo  Lisoir" et
    # "Hugo Lisoir" ne doivent pas etre deux pseudos differents, l'ecart
    # etant invisible a l'oeil dans une liste.
    return " ".join(pseudo.split()).casefold()


def validate_pseudo(pseudo: str) -> str:
    """Valide un pseudo SAISI et retourne sa forme d'affichage nettoyee
    (espaces de bord retires, espaces internes normalises).

    Leve AccountError si le pseudo est refusable.
    """
    if not isinstance(pseudo, str):
        raise AccountError("pseudo invalide")
    clean = " ".join(pseudo.split())
    if len(clean) < PSEUDO_MIN_LEN:
        raise AccountError(
            f"pseudo trop court (minimum {PSEUDO_MIN_LEN} caracteres)")
    if len(clean) > PSEUDO_MAX_LEN:
        raise AccountError(
            f"pseudo trop long (maximum {PSEUDO_MAX_LEN} caracteres)")
    if any(c in _PSEUDO_FORBIDDEN for c in clean):
        raise AccountError("pseudo contient un caractere interdit")
    return clean


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


# ---------------------------------------------
#  Store
# ---------------------------------------------

class AccountStore:
    """Store des comptes, persiste en JSON.

    Thread-safe (RLock) : le serveur le touche depuis plusieurs handlers
    WebSocket.

    Les INDEX (pseudo -> discord_id, numero -> discord_id) ne sont PAS
    persistes, ils sont reconstruits au chargement. Un index persiste est
    une deuxieme source de verite qui finit toujours par diverger de la
    premiere apres un plantage ou une edition manuelle du fichier.
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _DEFAULT_PATH
        self._lock = threading.RLock()
        self._accounts: dict[str, dict] = {}
        self._idx_pseudo: dict[str, str] = {}
        self._idx_numero: dict[int, str] = {}
        self._load()

    # --- persistance -------------------------------------------------

    def _load(self):
        with self._lock:
            self._accounts = {}
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    accounts = raw.get("accounts", {})
                    if isinstance(accounts, dict):
                        self._accounts = {
                            str(k): v for k, v in accounts.items()
                            if isinstance(v, dict)
                        }
                except Exception as e:
                    # On ne part PAS d'un store vide en silence : ecraser
                    # l'annuaire de tout le monde parce qu'un JSON est
                    # tronque serait pire que de refuser de demarrer.
                    raise AccountError(
                        f"fichier de comptes illisible ({self._path}) : {e}"
                    ) from e
            self._rebuild_indexes()

    def _rebuild_indexes(self):
        self._idx_pseudo = {}
        self._idx_numero = {}
        for did, acc in self._accounts.items():
            key = normalize_pseudo(acc.get("pseudo", ""))
            if key:
                self._idx_pseudo[key] = did
            num = acc.get("numero")
            if isinstance(num, int):
                self._idx_numero[num] = did

    def _save(self):
        """Ecriture atomique : fichier temporaire + os.replace. Sans ca,
        une coupure en pleine ecriture laisse un JSON tronque, donc un
        annuaire perdu.
        """
        with self._lock:
            data = {
                "version": _SCHEMA_VERSION,
                "saved_at": _now(),
                "accounts": self._accounts,
            }
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)

    # --- lecture -----------------------------------------------------

    def get_by_discord_id(self, discord_id: str) -> dict | None:
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            return dict(acc) if acc else None

    def get_by_numero(self, numero: int) -> dict | None:
        """Resolution numero -> compte. Sert au ROUTAGE de la messagerie
        (point 21c), pas a une recherche exposee au joueur.
        """
        with self._lock:
            did = self._idx_numero.get(int(numero))
            return dict(self._accounts[did]) if did else None

    def pseudo_taken(self, pseudo: str, discord_id: str | None = None) -> bool:
        """True si le pseudo est deja pris PAR QUELQU'UN D'AUTRE.

        discord_id permet de reposer la question pour soi-meme sans
        obtenir un faux positif : un joueur doit pouvoir "changer" son
        pseudo pour la meme chose avec une autre casse.
        """
        key = normalize_pseudo(pseudo)
        if not key:
            return False
        with self._lock:
            owner = self._idx_pseudo.get(key)
            if owner is None:
                return False
            return owner != str(discord_id) if discord_id else True

    def verify_token(self, token: str) -> dict | None:
        """Retourne le compte correspondant au jeton local, ou None.

        Enveloppe de verify_token_ex pour les appelants qui n'ont pas
        besoin de savoir PAR QUEL jeton l'authentification est passee.
        """
        acc, _via_prev = self.verify_token_ex(token)
        return acc

    def verify_token_ex(self, token: str) -> tuple[dict | None, bool]:
        """Comme verify_token, mais dit aussi si c'est l'ANCIEN jeton.

        Retourne (compte, via_ancien_jeton).

        Comparaison en temps constant sur le hash, dans les deux cas.

        L'ancien jeton n'est accepte que si 'prev_expires' n'est pas
        depasse. L'appelant DOIT journaliser un via_prev=True : accepter
        silencieusement un jeton perime serait un repli silencieux, et
        ces lignes sont le seul signal disant que des clients perdent
        leur jeton neuf quelque part.
        """
        if not token:
            return None, False
        h = _hash_token(token)
        now = _now()
        with self._lock:
            for acc in self._accounts.values():
                stored = acc.get("token_hash", "")
                if stored and secrets.compare_digest(stored, h):
                    return dict(acc), False
            # Second passage seulement : le jeton COURANT prime toujours,
            # meme si un ancien hash trainait encore quelque part.
            for acc in self._accounts.values():
                prev = acc.get("prev_token_hash", "")
                if not prev:
                    continue
                if now > float(acc.get("prev_expires", 0.0) or 0.0):
                    continue
                if secrets.compare_digest(prev, h):
                    return dict(acc), True
        return None, False

    def rotate_token(self, discord_id: str) -> str | None:
        """Emet un jeton neuf et retrograde l'actuel en 'prev'.

        Retourne le nouveau jeton EN CLAIR, ou None si la fiche est
        inconnue. Comme link_account, le clair n'existe qu'ici.

        Appelee a chaque connexion acceptee : un jeton vole ne vaut donc
        plus que jusqu'a la prochaine connexion de sa victime, au lieu de
        valoir a vie.
        """
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            if acc is None:
                return None
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            ancien = acc.get("token_hash", "")
            if ancien:
                acc["prev_token_hash"] = ancien
                acc["prev_expires"] = _now() + _PREV_TOKEN_GRACE_S
            acc["token_hash"] = _hash_token(token)
            acc["updated_at"] = _now()
            self._save()
            return token

    def restore_prev_as_current(self, discord_id: str) -> bool:
        """Remet l'ANCIEN jeton en position courante.

        Appelee quand une connexion s'est authentifiee par la tolerance,
        JUSTE AVANT la rotation.

        [ROTATION 05/08/2026] Sans elle, la tolerance ne rattrapait rien :
        elle repoussait l'exclusion d'une connexion. Deroule du defaut,
        pour un client qui n'ecrit jamais son jeton (le mannequin) --
        T0 est le seul jeton qu'il possede :

          co.1 : presente T0 (courant) -> rotation : courant T1, ancien T0
          co.2 : presente T0 (ancien)  -> rotation : courant T2, ancien T1
                 ... T0 vient de disparaitre, et le client n'a que T0
          co.3 : presente T0           -> account_unknown, definitif

        La rotation detruisait donc le seul jeton que le client detenait,
        exactement ce que la fenetre etait censee eviter. En remettant
        l'ancien en position courante avant de tourner, il repasse en
        'ancien' juste apres : le client conserve indefiniment un jeton
        valide. La degradation devient stable au lieu d'etre fatale, et
        la ligne [ROTATION] dans les logs dit qui est dans ce cas.

        Retourne True si une restauration a eu lieu.
        """
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            if acc is None:
                return False
            prev = acc.get("prev_token_hash", "")
            if not prev:
                return False
            acc["token_hash"] = prev
            acc.pop("prev_token_hash", None)
            acc.pop("prev_expires", None)
            acc["updated_at"] = _now()
            self._save()
            return True

    def clear_prev_token(self, discord_id: str) -> bool:
        """Tue la tolerance : l'ancien jeton ne vaut plus rien.

        Appelee des qu'une connexion passe avec le jeton COURANT -- la
        preuve que le client a bien ecrit le sien sur disque, donc que
        le filet n'a plus lieu d'etre. C'est ce qui fait que la fenetre
        de tolerance dure des secondes en marche normale.

        Retourne True si quelque chose a ete efface (evite une ecriture
        disque a chaque connexion pour rien).
        """
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            if acc is None:
                return False
            if not acc.get("prev_token_hash") and "prev_expires" not in acc:
                return False
            acc.pop("prev_token_hash", None)
            acc.pop("prev_expires", None)
            self._save()
            return True

    def list_accounts(self) -> list[dict]:
        """Annuaire complet, pour l'interface ADMIN uniquement (point 28).

        Les hashs de jeton sont retires : l'admin n'en a aucun usage et
        ils n'ont pas a transiter sur le reseau. 'prev_token_hash' est
        retire pour la meme raison que 'token_hash' -- c'est un jeton
        encore valide pendant sa fenetre de tolerance.
        """
        with self._lock:
            out = []
            for acc in self._accounts.values():
                clean = {k: v for k, v in acc.items()
                         if k not in ("token_hash", "prev_token_hash")}
                out.append(clean)
        # Les fiches sans numero (compte relie, jamais connecte) passent
        # en tete : ce sont celles qui meritent un coup d'oeil.
        out.sort(key=lambda a: (a.get("numero") is not None,
                                a.get("numero") or 0))
        return out

    # --- numeros -----------------------------------------------------

    def _allocate_numero(self) -> int:
        """Tire un numero libre au hasard dans la plage.

        Tirage direct tant que la plage est peu remplie (cas normal :
        quelques dizaines de comptes sur 10 000 numeros), repli sur le
        tirage parmi les libres quand elle se remplit -- sinon le tirage
        direct tournerait longtemps a l'approche de la saturation.
        """
        with self._lock:
            total = NUMERO_MAX - NUMERO_MIN + 1
            used = self._idx_numero
            if len(used) >= total:
                raise AccountError(
                    "plage de numeros saturee "
                    f"({NUMERO_MIN}-{NUMERO_MAX})")
            if len(used) < total // 2:
                while True:
                    n = random.randint(NUMERO_MIN, NUMERO_MAX)
                    if n not in used:
                        return n
            libres = [n for n in range(NUMERO_MIN, NUMERO_MAX + 1)
                      if n not in used]
            return random.choice(libres)

    # --- ecriture ----------------------------------------------------

    def link_account(self, discord_id: str, pseudo: str = "",
                     discord_username: str = "") -> tuple[dict, str]:
        """Cree la fiche d'un discord_id, ou la retrouve si elle existe.

        Retourne (compte, jeton_local_en_clair).

        Le jeton en clair n'existe qu'ici : il est renvoye une fois au
        client et n'est jamais relu ensuite (on ne stocke que son hash).

        Cas d'un compte EXISTANT : on ne touche ni au pseudo ni au numero,
        et on emet un NOUVEAU jeton. C'est le chemin "j'ai change de
        machine / perdu mon jeton" -- refaire la liaison Discord retombe
        sur le meme discord_id, donc la meme fiche.
        """
        discord_id = str(discord_id)
        if not discord_id:
            raise AccountError("discord_id vide")

        with self._lock:
            existing = self._accounts.get(discord_id)
            if existing is not None:
                token = secrets.token_urlsafe(_TOKEN_BYTES)
                existing["token_hash"] = _hash_token(token)
                # [ROTATION 05/08/2026] Une re-liaison Discord n'est PAS une
                # rotation : c'est une reprise en main explicite du compte
                # par son proprietaire, souvent APRES un vol suppose. Elle
                # doit donc tuer la tolerance au lieu de la reconduire --
                # sinon le jeton que le joueur cherche justement a revoquer
                # resterait valide cinq minutes de plus.
                existing.pop("prev_token_hash", None)
                existing.pop("prev_expires", None)
                existing["updated_at"] = _now()
                if discord_username:
                    existing["discord_username"] = discord_username
                self._save()
                return dict(existing), token

            # [DISCORD 30/07/2026] Le pseudo est OPTIONNEL a la creation.
            # La liaison Discord n'etablit que l'identite et attribue le
            # numero ; le pseudo est fixe a la CONNEXION, a partir du
            # champ Nom du client (cf authenticate_join). Une fiche sans
            # pseudo n'occupe aucune cle d'unicite : _rebuild_indexes
            # ignore les cles vides.
            clean = ""
            if pseudo:
                clean = validate_pseudo(pseudo)
                if self.pseudo_taken(clean, discord_id):
                    raise AccountError(f"pseudo deja pris : {clean}")

            token = secrets.token_urlsafe(_TOKEN_BYTES)
            acc = {
                "discord_id": discord_id,
                "discord_username": discord_username,
                "pseudo": clean,
                # [DISCORD 30/07/2026] PAS de numero a la liaison. Relier
                # son compte n'etablit que l'identite ; le numero est
                # attribue a la premiere CONNEXION (ensure_numero), quand
                # le joueur entre reellement dans l'annuaire. Sinon une
                # liaison sans suite consommerait un numero pour rien, et
                # la plage se viderait de fiches fantomes.
                "numero": None,
                "token_hash": _hash_token(token),
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._accounts[discord_id] = acc
            self._rebuild_indexes()
            self._save()
            return dict(acc), token

    def ensure_numero(self, discord_id: str) -> dict:
        """Attribue un numero si la fiche n'en a pas encore. Idempotent :
        une fiche qui en a un garde le sien.

        Appele a la CONNEXION, pas a la liaison.
        """
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            if acc is None:
                raise AccountError("compte inconnu")
            if isinstance(acc.get("numero"), int):
                return dict(acc)
            acc["numero"] = self._allocate_numero()
            acc["updated_at"] = _now()
            self._rebuild_indexes()
            self._save()
            return dict(acc)

    def set_metiers(self, discord_id: str, metiers, notifs=None) -> dict:
        """Metiers du joueur (app Travail), stockes dans SA FICHE.

        [TRAVAIL 10/08/2026] Ici et pas dans un fichier a part, pour deux
        raisons :

          - le metier survit ainsi aux reconnexions et aux changements de
            machine, comme le numero. Le garder cote client obligerait a
            le rechoisir apres chaque reinstallation ;
          - l'app Urgence, plus tard, devra repondre a "qui est
            mecanicien ?". La reponse se lit alors dans l'annuaire, sans
            second registre a tenir a jour -- une donnee en double finit
            toujours par etre lue au mauvais endroit.

        La validation est deleguee au module de regles : ce sont les
        MEMES bornes que celles appliquees cote client (2 metiers
        maximum, liste fermee), pas une seconde copie.
        """
        discord_id = str(discord_id)
        # [URGENCE 15/08/2026] Un identifiant de ROLE ne passe pas par
        # ici. Aujourd'hui `medecin` n'est pas dans METIERS, donc la
        # faille n'existe pas -- elle s'ouvrirait le jour ou quelqu'un
        # l'ajouterait "pour l'affichage", et un client bricole se
        # decernerait le role via travail_metiers. Le controle explicite
        # la ferme d'avance.
        try:
            import circusvoip_phone_urgence as _U
            for m in (metiers or []):
                _U.valide_role_non_metier(m)
        except ImportError:
            pass
        except Exception as e:
            raise AccountError(str(e))
        try:
            import circusvoip_phone_travail as _T
            propres = _T.valide_metiers_joueur(metiers)
        except ImportError:
            raise AccountError("module Travail indisponible")
        except Exception as e:
            raise AccountError(str(e))
        with self._lock:
            acc = self._accounts.get(discord_id)
            if acc is None:
                raise AccountError("compte inconnu")
            acc["metiers"] = propres
            if notifs is not None:
                acc["travail_notifs"] = bool(notifs)
            acc["updated_at"] = _now()
            self._save()
            return dict(acc)

    # --- roles Urgence -------------------------------------------------
    #
    # [URGENCE 15/08/2026] Champ SEPARE des metiers, et ce n'est pas du
    # rangement.
    #
    # Les huit metiers de l'app Travail sont AUTO-DECLARES : le joueur
    # coche, le serveur valide contre une liste fermee. La validation
    # porte sur QUOI, jamais sur QUI a le droit.
    #
    # `medecin` et `securite` sont ATTRIBUES par un chef. S'ils vivaient
    # dans la meme liste, un client bricole enverrait `travail_metiers`
    # avec ["medecin"] et se decernerait le role -- le serveur
    # l'accepterait, puisque le nom serait dans la liste fermee. Deux
    # champs, deux chemins d'ecriture, et set_metiers() refuse
    # explicitement un identifiant de role (voir plus haut).
    #
    # Effet de bord voulu : METIERS_MAX = 2 ne s'applique pas aux roles.
    # Un medecin reste mineur-pilote s'il le souhaite.

    def set_role(self, discord_id: str, role, par: str | None = None) -> dict:
        """Attribue ou retire le role Urgence d'un joueur.

        role None ou "" retire le role. La validation du DROIT (est-ce
        bien un chef ? le joueur a-t-il deja l'autre role ?) appartient a
        l'appelant : ce module stocke, il n'arbitre pas.

        `par` note QUI a attribue. Sans cette trace, la seule question
        qui compte le jour d'un probleme -- "pourquoi ce joueur a-t-il
        l'acces ?" -- n'a pas de reponse.
        """
        discord_id = str(discord_id)
        if role in (None, "", False):
            propre = None
        else:
            try:
                import circusvoip_phone_urgence as _U
                propre = _U.valide_role(role)
            except ImportError:
                raise AccountError("module Urgence indisponible")
            except Exception as e:
                raise AccountError(str(e))
        with self._lock:
            acc = self._accounts.get(discord_id)
            if acc is None:
                raise AccountError("compte inconnu")
            if propre is None:
                acc.pop("role", None)
                acc.pop("role_par", None)
                acc.pop("role_le", None)
            else:
                acc["role"] = propre
                acc["role_par"] = str(par) if par else None
                acc["role_le"] = _now()
            acc["updated_at"] = _now()
            self._save()
            return dict(acc)

    def set_chef(self, discord_id: str | None, role) -> dict:
        """Designe LE chef d'un role, ou le retire si discord_id est None.

        Un seul chef par role : la designation retire le drapeau a tous
        les autres avant de le poser. Sans ce nettoyage, deux chefs
        pourraient coexister apres un changement -- et rien ne le
        signalerait.

        Le chef recoit aussi le role lui-meme : il est de service de
        fait, donc il doit etre dans la population qui recoit les
        signaux.

        L'equipe SURVIT au changement de chef : les roles deja attribues
        ne sont pas touches, le nouveau chef herite. Vider l'equipe
        obligerait a tout redistribuer pour un simple remplacement.
        """
        try:
            import circusvoip_phone_urgence as _U
            propre = _U.valide_role(role)
        except ImportError:
            raise AccountError("module Urgence indisponible")
        except Exception as e:
            raise AccountError(str(e))
        with self._lock:
            for acc in self._accounts.values():
                if acc.get("chef") == propre:
                    acc.pop("chef", None)
                    acc["updated_at"] = _now()
            if discord_id is None:
                self._save()
                return {}
            acc = self._accounts.get(str(discord_id))
            if acc is None:
                raise AccountError("compte inconnu")
            acc["chef"] = propre
            acc["role"] = propre
            acc["role_le"] = _now()
            acc["updated_at"] = _now()
            self._save()
            return dict(acc)

    def chefs(self) -> dict:
        """{role: fiche} des chefs en place. Role absent = pas de chef."""
        out = {}
        with self._lock:
            for acc in self._accounts.values():
                r = acc.get("chef")
                if r:
                    out[str(r)] = dict(acc)
        return out

    def role_de_numero(self, numero) -> str | None:
        with self._lock:
            did = self._idx_numero.get(int(numero)) if numero else None
            if not did:
                return None
            return self._accounts[did].get("role")

    def equipe(self, role) -> list:
        """Fiches des detenteurs d'un role, chef compris.

        Sert a l'ecran d'equipe du chef, qui voit TOUT le monde -- en
        service ou non. Les autres detenteurs ne voient que ceux en
        service, et cette information-la ne vit pas ici : elle est
        RUNTIME, liee a la connexion, et n'a rien a faire sur disque.
        """
        r = str(role or "").strip().lower()
        out = []
        with self._lock:
            for acc in self._accounts.values():
                if acc.get("role") == r:
                    out.append(dict(acc))
        out.sort(key=lambda a: (not a.get("chef"), a.get("numero") or 0))
        return out

    def metiers_par_numero(self) -> dict:
        """{numero: [metiers]} pour tous les comptes qui en ont declare.

        Sert au CIBLAGE des notifications : une mission ne concerne qu'un
        metier, la diffuser a tout le monde ferait 100 messages la ou 10
        suffisent.
        """
        out = {}
        with self._lock:
            for acc in self._accounts.values():
                num = acc.get("numero")
                mets = acc.get("metiers") or []
                if num and mets:
                    out[str(num)] = list(mets)
        return out

    def rename(self, discord_id: str, new_pseudo: str) -> dict:
        """Change le pseudo d'un compte. Le numero NE BOUGE PAS.

        L'ancienne cle de pseudo est liberee (via _rebuild_indexes) :
        sans ca, un joueur qui change trois fois de nom immobiliserait
        trois pseudos.
        """
        discord_id = str(discord_id)
        clean = validate_pseudo(new_pseudo)
        with self._lock:
            acc = self._accounts.get(discord_id)
            if acc is None:
                raise AccountError("compte inconnu")
            if self.pseudo_taken(clean, discord_id):
                raise AccountError(f"pseudo deja pris : {clean}")
            acc["pseudo"] = clean
            acc["updated_at"] = _now()
            self._rebuild_indexes()
            self._save()
            return dict(acc)

    def set_numero(self, discord_id: str, numero: int) -> dict:
        """Reattribution MANUELLE d'un numero, depuis l'admin (point 28).

        Prevu pour le cas RP "changement de personnage" : le modele fait
        survivre le numero au changement de pseudo, l'admin doit pouvoir
        trancher autrement au cas par cas.
        """
        discord_id = str(discord_id)
        numero = int(numero)
        if not (NUMERO_MIN <= numero <= NUMERO_MAX):
            raise AccountError(
                f"numero hors plage ({NUMERO_MIN}-{NUMERO_MAX})")
        with self._lock:
            acc = self._accounts.get(discord_id)
            if acc is None:
                raise AccountError("compte inconnu")
            owner = self._idx_numero.get(numero)
            if owner is not None and owner != discord_id:
                raise AccountError(f"numero deja attribue : {numero}")
            acc["numero"] = numero
            acc["updated_at"] = _now()
            self._rebuild_indexes()
            self._save()
            return dict(acc)

    def new_numero(self, discord_id: str) -> dict:
        """Retire un numero au hasard pour un compte existant (admin)."""
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            if acc is None:
                raise AccountError("compte inconnu")
            old = acc.get("numero")
            if isinstance(old, int):
                self._idx_numero.pop(old, None)
            try:
                acc["numero"] = self._allocate_numero()
            except AccountError:
                if isinstance(old, int):
                    self._idx_numero[old] = str(discord_id)
                raise
            acc["updated_at"] = _now()
            self._rebuild_indexes()
            self._save()
            return dict(acc)

    def delete(self, discord_id: str) -> bool:
        """Supprime une fiche (admin). Le numero redevient disponible."""
        with self._lock:
            if str(discord_id) not in self._accounts:
                return False
            del self._accounts[str(discord_id)]
            self._rebuild_indexes()
            self._save()
            return True

    # ---------------------------------------------
    #  [QUEUE 03/08/2026] Blocage temporaire de compte
    # ---------------------------------------------
    # Palier ultime de l'anti-spam. Il vit ICI et non dans un fichier a
    # part pour une seule raison : il doit survivre a un systemctl restart.
    # Un blocage en RAM se leverait au prochain redemarrage, et il
    # suffirait de l'attendre. Les compteurs de fenetre, eux, restent en
    # memoire du serveur -- cinq minutes perdues sont sans importance.
    #
    # TOUJOURS borne dans le temps. Un faux positif ne doit jamais exclure
    # quelqu'un definitivement sans decision humaine : le bannissement
    # definitif reste une action d'admin, pas une consequence automatique.

    def block_numero(self, numero: int, duree_s: float) -> dict | None:
        """Bloque le compte portant ce numero pendant `duree_s` secondes.

        Rend la fiche mise a jour, ou None si le numero est inconnu.
        Un blocage plus court que celui deja en cours ne le raccourcit
        pas : on garde la date la plus lointaine.
        """
        try:
            numero = int(numero)
        except (TypeError, ValueError):
            return None
        with self._lock:
            did = self._idx_numero.get(numero)
            acc = self._accounts.get(did) if did else None
            if acc is None:
                return None
            jusqua = _now() + max(0.0, float(duree_s))
            if jusqua > float(acc.get("blocked_until") or 0.0):
                acc["blocked_until"] = jusqua
                acc["updated_at"] = _now()
                self._save()
            return dict(acc)

    def unblock(self, discord_id: str) -> bool:
        """Leve un blocage (decision d'admin)."""
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            if acc is None or not acc.get("blocked_until"):
                return False
            acc["blocked_until"] = 0.0
            acc["updated_at"] = _now()
            self._save()
            return True

    def blocked_until(self, discord_id: str) -> float:
        """Fin du blocage (epoch), ou 0.0 si le compte n'est pas bloque.

        Un blocage expire est rendu comme 0.0 : inutile de le nettoyer, la
        comparaison suffit.
        """
        with self._lock:
            acc = self._accounts.get(str(discord_id))
            if acc is None:
                return 0.0
            fin = float(acc.get("blocked_until") or 0.0)
            return fin if fin > _now() else 0.0

    def count(self) -> int:
        with self._lock:
            return len(self._accounts)
