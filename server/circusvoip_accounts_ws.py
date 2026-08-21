#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[DISCORD 30/07/2026] Protocole WebSocket des comptes -- SERVEUR.

Couche mince entre les messages du client et circusvoip_accounts /
circusvoip_discord_auth. Suit le meme motif que _mp_handle dans
circusvoip_server.py : une liste de types, une fonction qui rend le
message a renvoyer (ou None). Ni async, ni websockets, ni socket :
appelable et testable seule.

--- Enchainement cote joueur ---

  1. discord_config   ->  le serveur annonce client_id + port de
                          redirection. Le client ne les code pas en dur :
                          changer d'application Discord ne demande alors
                          aucune mise a jour client.
  2. (navigateur)         le client fait tourner start_link_flow() et
                          obtient (code, code_verifier).
  4. discord_link     ->  le serveur echange le code chez Discord, cree
                          la fiche, renvoie pseudo + numero + jeton local.
  5. le client stocke le jeton local. Les connexions suivantes ne passent
     PLUS par Discord : "join" porte le jeton, le serveur reconnait le
     joueur.

--- Ce qui n'existe volontairement pas ---

Aucune recherche d'annuaire exposee au joueur : pas de "qui a ce
numero", pas de "quel est le numero de X". L'annuaire est reserve a
l'admin (decision du 30/07). Le seul renseignement qu'un joueur obtient
est SON PROPRE numero.

[SECURITE 31/07/2026] pseudo_check a ete RETIRE : il permettait de
tester l'existence d'un pseudo en boucle, donc d'enumerer les joueurs.
Il n'avait plus d'usage depuis que le pseudo se valide a la connexion et
non dans la fenetre de liaison.

--- Comptes de service ---

La liaison Discord etant obligatoire pour se connecter (decision du
30/07), le mannequin et les tests de charge seraient bloques. L'admin
peut donc creer des comptes SANS Discord, dont la cle est prefixee
SERVICE_PREFIX. Ils ne peuvent pas etre crees depuis le client : ce
module ne les fabrique jamais, seul l'admin le fait.
"""

from __future__ import annotations

import secrets
import time

from circusvoip_accounts import AccountError, AccountStore, normalize_pseudo
import circusvoip_discord_auth as _discord

# Types de messages pris en charge, a brancher dans le dispatch du
# serveur comme _MP_TYPES.
ACCOUNT_TYPES = (
    "discord_config",
    "discord_link",
    "service_link",
)

# Prefixe des comptes crees a la main par l'admin (mannequin, tests de
# charge, depannage quand Discord est en panne).
SERVICE_PREFIX = "local:"

# Anti-abus sur la liaison : un echange rate coute un appel HTTP sortant
# vers Discord. Sans limite, une boucle chez un client mal ecrit (ou
# malveillant) transformerait le serveur en source de trafic.
_LINK_MAX_PER_IP = 5
_LINK_WINDOW_SEC = 300.0


class AccountsContext:
    """Etat que le serveur passe au handler.

    identify_fn est injectable pour que les tests n'appellent pas
    Discord : la seule dependance reseau du module est isolee ici.
    """

    def __init__(self, store: AccountStore, client_id: str,
                 client_secret: str, server_token: str,
                 identify_fn=None, log=None, allow_service: bool = False,
                 service_secret: str = ""):
        self.store = store
        self.client_id = str(client_id or "")
        self.client_secret = str(client_secret or "")
        self.server_token = str(server_token or "")
        self.identify_fn = identify_fn or _discord.exchange_and_identify
        self.log = log or (lambda msg: None)
        # [DISCORD 30/07/2026] Comptes de service : DESACTIVE par defaut.
        # Active, il permet de creer un compte SANS Discord avec le seul
        # token serveur -- c'est une porte de sortie pour l'outillage
        # (mannequin, tests de charge), donc aussi un contournement de
        # REQUIRE_ACCOUNT. A laisser a False sur un serveur de jeu.
        self.allow_service = bool(allow_service)
        # Secret propre aux comptes de service. Vide = fonction
        # inutilisable, meme si allow_service est vrai : deux verrous
        # plutot qu'un, parce que la consequence d'une ouverture est de
        # rendre Discord facultatif.
        self.service_secret = str(service_secret or "")
        # ip -> [timestamps des tentatives de liaison]
        self._link_hits: dict[str, list[float]] = {}

    def _rate_ok(self, ip: str) -> bool:
        now = time.time()
        hits = [t for t in self._link_hits.get(ip, [])
                if now - t < _LINK_WINDOW_SEC]
        if len(hits) >= _LINK_MAX_PER_IP:
            self._link_hits[ip] = hits
            return False
        hits.append(now)
        self._link_hits[ip] = hits
        return True


def _err(reason: str, message: str) -> dict:
    return {"type": "account_error", "reason": reason, "message": message}


def handle(msg_type: str, data: dict, ctx: AccountsContext,
           peer_ip: str = "?") -> dict | None:
    """Traite un message de compte. Retourne le message a renvoyer.

    Ne leve jamais : toute erreur devient un account_error, que le client
    sait afficher. Un plantage ici fermerait la connexion d'un joueur en
    pleine inscription.
    """
    try:
        if msg_type == "discord_config":
            if not ctx.client_id:
                return _err("not_configured",
                            "La liaison Discord n'est pas configuree "
                            "sur ce serveur.")
            return {
                "type": "discord_config",
                "client_id": ctx.client_id,
                "redirect_port": _discord.REDIRECT_PORT,
            }

        if msg_type == "service_link":
            if not ctx.allow_service:
                return _err("service_disabled",
                            "Les comptes de service sont desactives sur ce "
                            "serveur.")
            # [SECURITE 31/07/2026] Secret DEDIE, et non le mot de passe
            # joueur : celui-ci est connu de TOUS les joueurs, donc
            # n'importe lequel pouvait se fabriquer des comptes sans
            # Discord et contourner entierement REQUIRE_ACCOUNT. Le
            # secret de service n'est connu que de l'outillage.
            secret = str(data.get("service_secret", "") or "")
            if not (ctx.service_secret
                    and secrets.compare_digest(secret, ctx.service_secret)):
                return _err("invalid_token", "Secret de service invalide.")
            nom = str(data.get("service_name", "") or "").strip()
            if not nom:
                return _err("bad_request", "Nom de service manquant.")
            # Cle prefixee : un discord_id ne peut pas commencer par
            # "local:", les deux espaces de noms ne peuvent donc pas se
            # telescoper.
            sid = SERVICE_PREFIX + normalize_pseudo(nom)
            existait = ctx.store.get_by_discord_id(sid) is not None
            try:
                acc, local_token = ctx.store.link_account(sid, "", f"service:{nom}")
            except AccountError as e:
                return _err("link_failed", str(e))
            ctx.log(f"[COMPTES] compte de service "
                    f"{'retrouve' if existait else 'cree'} : {sid} ({peer_ip})")
            return {
                "type": "service_linked",
                "account_token": local_token,
                "existing": existait,
            }

        if msg_type == "discord_link":
            # Le token serveur reste exige : sans lui, n'importe qui
            # atteignant le port pourrait creer des comptes et consommer
            # des numeros. Discord dit QUI tu es, le token dit que tu as
            # le droit d'etre la.
            token = data.get("token", "")
            if not (ctx.server_token
                    and secrets.compare_digest(str(token), ctx.server_token)):
                return _err("invalid_token", "Token serveur invalide.")

            if not ctx._rate_ok(peer_ip):
                return _err("rate_limited",
                            "Trop de tentatives de liaison. Reessayez dans "
                            "quelques minutes.")

            code = data.get("code", "")
            verifier = data.get("code_verifier", "")
            if not code or not verifier:
                return _err("bad_request", "Reponse Discord incomplete.")

            try:
                ident = ctx.identify_fn(
                    ctx.client_id, ctx.client_secret, code, verifier)
            except Exception as e:
                ctx.log(f"[DISCORD] echec identification ({peer_ip}) : {e}")
                return _err("discord_failed", str(e))

            did = str(ident.get("discord_id", ""))
            if not did:
                return _err("discord_failed",
                            "Discord n'a pas renvoye d'identifiant.")
            if did.startswith(SERVICE_PREFIX):
                # Ceinture et bretelles : un discord_id ne peut pas
                # commencer par "local:", mais si ca arrivait, ce serait
                # une usurpation de compte de service.
                return _err("discord_failed", "Identifiant Discord invalide.")

            # Constate AVANT la creation : deduire "le compte existait"
            # d'une comparaison de timestamps serait fragile (deux appels
            # a time.time() a la creation ne rendent pas la meme valeur).
            existait = ctx.store.get_by_discord_id(did) is not None

            try:
                # Pas de pseudo ici : la liaison n'etablit que l'identite
                # et attribue le numero. Le pseudo est fixe a la
                # connexion (authenticate_join), a partir du champ Nom.
                acc, local_token = ctx.store.link_account(
                    did, "", ident.get("username", ""))
            except AccountError as e:
                return _err("link_failed", str(e))
            ctx.log(
                f"[DISCORD] {'re-liaison' if existait else 'nouveau compte'} "
                f"{acc['pseudo']} -> {acc['numero']} ({peer_ip})")
            return {
                "type": "discord_linked",
                "pseudo": acc["pseudo"],
                "numero": acc["numero"],
                # Seule occurrence du jeton en clair de toute sa vie :
                # le store n'en garde que le hash.
                "account_token": local_token,
                # Vrai si la fiche existait deja : le client peut dire
                # "compte retrouve" plutot que "compte cree".
                "existing": existait,
            }

    except Exception as e:  # filet : ne jamais tuer la connexion
        ctx.log(f"[COMPTES] erreur inattendue sur {msg_type} : {e}")
        return _err("internal", "Erreur interne.")

    return None


def _validate(pseudo: str) -> str:
    from circusvoip_accounts import validate_pseudo
    return validate_pseudo(pseudo)


# ---------------------------------------------
#  Authentification au join
# ---------------------------------------------

def authenticate_join(data: dict, ctx: AccountsContext) -> tuple[dict | None, dict | None]:
    """Resout l'identite d'un "join" a partir du jeton local.

    Retourne (compte, message_erreur). Exactement un des deux est None.

    A appeler dans le handler "join" APRES la verification du token
    serveur : les deux controles sont complementaires et aucun ne
    remplace l'autre.

    Le pseudo est celui de la FICHE, pas celui annonce par le client :
    sinon n'importe qui pourrait se presenter sous le nom d'un autre en
    editant sa config locale.
    """
    tok = data.get("account_token", "")
    if not tok:
        return None, _err(
            "account_required",
            "Ce serveur demande un compte. Reliez votre compte Discord.")
    acc, _via_prev = ctx.store.verify_token_ex(str(tok))
    if acc is None:
        return None, _err(
            "account_unknown",
            "Compte inconnu ou jeton perime. Reliez votre compte Discord.")

    # [ROTATION 05/08/2026] Journal OBLIGATOIRE de la tolerance. Accepter
    # un ancien jeton sans le dire serait un repli silencieux (§5 ter,
    # corollaire) : ces lignes sont le SEUL signal disant qu'un client a
    # recu un jeton neuf sans arriver a l'ecrire. Si elles apparaissent
    # regulierement, le probleme n'est pas la tolerance -- c'est la
    # sauvegarde de config cote client, et la tolerance ne fait que le
    # masquer.
    if _via_prev:
        print(f"[ROTATION] Connexion acceptee sur l'ANCIEN jeton : "
              f"discord_id={acc.get('discord_id')} "
              f"pseudo={acc.get('pseudo')!r}. Le client n'a pas ecrit le "
              f"jeton emis a sa connexion precedente.", flush=True)
        # Le jeton presente redevient le jeton COURANT avant que
        # _join_ok ne le fasse tourner. Il repassera donc en 'ancien'
        # juste apres, et le client -- qui ne detient que celui-la --
        # gardera un jeton valide a la connexion suivante. Sans cette
        # ligne, la tolerance ne rattrapait rien : elle repoussait
        # l'exclusion d'une connexion. Cf. restore_prev_as_current().
        try:
            ctx.store.restore_prev_as_current(acc["discord_id"])
        except Exception as e:
            print(f"[ROTATION] Restauration de l'ancien jeton impossible "
                  f"pour discord_id={acc.get('discord_id')} : {e!r}",
                  flush=True)
    else:
        # Le jeton COURANT a servi : preuve que le client l'a bien ecrit
        # sur disque. Le filet n'a plus lieu d'etre, on le retire tout de
        # suite -- c'est ce qui fait durer la fenetre quelques secondes
        # au lieu de cinq minutes.
        ctx.store.clear_prev_token(acc["discord_id"])

    # [DISCORD 30/07/2026] Le pseudo se regle ICI, pas a la liaison :
    # c'est le seul moment ou le joueur choisit sous quel nom il entre,
    # et ou l'unicite a un sens (deux joueurs ne peuvent pas etre en
    # ligne sous le meme nom). La fiche est mise a jour si le nom a
    # change -- le numero, lui, ne bouge jamais.
    demande = str(data.get("name", "") or "").strip()
    if not demande:
        if acc.get("pseudo"):
            return _join_ok(acc, ctx), None   # on garde l'existant
        return None, _err("bad_pseudo", "Renseignez un pseudo.")

    from circusvoip_accounts import normalize_pseudo
    if normalize_pseudo(demande) == normalize_pseudo(acc.get("pseudo", "")):
        return _join_ok(acc, ctx), None    # inchange (casse comprise)

    try:
        clean = _validate(demande)
    except AccountError as e:
        return None, _err("bad_pseudo", str(e))
    if ctx.store.pseudo_taken(clean, acc["discord_id"]):
        return None, _err(
            "pseudo_taken",
            f"Le pseudo « {clean} » est deja utilise par un autre joueur.")
    try:
        acc = ctx.store.rename(acc["discord_id"], clean)
    except AccountError as e:
        return None, _err("bad_pseudo", str(e))
    return _join_ok(acc, ctx), None


def _join_ok(acc: dict, ctx: AccountsContext) -> dict:
    """Entonnoir UNIQUE des sorties reussies de authenticate_join.

    Attribue le numero si besoin, puis fait tourner le jeton local.

    [ROTATION 05/08/2026] La rotation est ici, et nulle part ailleurs,
    pour une raison precise : authenticate_join a trois chemins de succes
    (pseudo inchange, pseudo absent mais fiche nommee, pseudo renomme).
    Faire tourner le jeton dans chacun d'eux, c'est se garantir qu'un
    quatrieme chemin ajoute plus tard oubliera de le faire -- et un
    chemin qui ne fait pas tourner le jeton ne casse RIEN de visible : il
    laisse juste un jeton eternel derriere lui, exactement le probleme
    qu'on est en train de corriger. Un ecran construit a deux endroits
    sera un jour construit de travers ; une rotation ecrite a trois
    endroits sera un jour oubliee au quatrieme.

    Le jeton neuf voyage sous la cle '_rotated_token', prefixee d'un
    souligne parce qu'elle ne doit JAMAIS etre persistee : 'acc' est une
    copie, mais la convention rend l'intention lisible pour la suite.
    """
    acc = _with_numero(acc, ctx)

    # [ROTATION 11/08/2026] ROTATION NEUTRALISEE.
    #
    # Le mecanisme est correct : un jeton vole ne valait plus que jusqu'a
    # la prochaine connexion de sa victime, au lieu de valoir a vie. Mais
    # il suppose un client qui ECRIT le jeton recu dans le welcome, et
    # cette condition ne tient pas pendant le developpement : chaque
    # version intermediaire du client, chaque fichier restaure, chaque
    # essai sur un build antecedent grille la session suivante passe la
    # fenetre de tolerance. Le cout etait quotidien pour un benefice nul
    # tant que le parc n'est pas stabilise.
    #
    # Rien n'est supprime : rotate_token, la tolerance, la restauration et
    # l'exemption des comptes de service restent ecrites et testees. Il
    # suffira de retirer ce return pour les reactiver -- de preference en
    # les conditionnant a une capacite annoncee par le client, ce qui est
    # la vraie solution et evite de refaire le meme constat.
    #
    # Les jetons deja emis restent valides : rien n'est invalide ici.
    return acc

    # [ROTATION 05/08/2026] Les COMPTES DE SERVICE sont hors rotation.
    # Un mannequin ou un test de charge n'a pas de session utilisateur a
    # proteger : le vol de son jeton ne donne acces a l'identite de
    # personne, et ces comptes ne sont creables que si
    # 'allow_service_accounts' est explicitement actif sur le serveur.
    # En face, le cout etait reel : ce sont des outils relances vingt
    # fois par session de dev, dont le jeton est saisi a la main dans un
    # fichier de config. Les faire tourner ajoutait une corvee de
    # ressaisie a l'endroit precis ou l'on veut zero friction.
    # L'exemption est explicite plutot que subie : sans elle, ces comptes
    # seraient de toute facon degrades en silence par le mecanisme de
    # tolerance, ce qui reviendrait au meme sans que ce soit ecrit nulle
    # part.
    if str(acc.get("discord_id", "")).startswith(SERVICE_PREFIX):
        return acc

    try:
        neuf = ctx.store.rotate_token(acc["discord_id"])
    except Exception as e:
        # Une rotation ratee ne doit pas refuser la connexion : le joueur
        # garde son jeton actuel, qui reste valide. On le dit, par contre.
        print(f"[ROTATION] Echec de rotation pour "
              f"discord_id={acc.get('discord_id')} : {e!r}", flush=True)
        return acc
    if neuf:
        acc["_rotated_token"] = neuf
    return acc


def _with_numero(acc: dict, ctx: AccountsContext) -> dict:
    """Attribue le numero si la fiche n'en a pas encore.

    C'est ICI que le joueur entre reellement dans l'annuaire : il a un
    compte, un pseudo valide et accepte, et il se connecte. Une liaison
    Discord restee sans suite ne consomme donc aucun numero.
    """
    if isinstance(acc.get("numero"), int):
        return acc
    try:
        return ctx.store.ensure_numero(acc["discord_id"])
    except Exception as e:
        ctx.log(f"[COMPTES] attribution de numero impossible : {e}")
        return acc
