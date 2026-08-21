#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[DISCORD 30/07/2026] Liaison de compte Discord (OAuth2 + PKCE).

Deux moities dans un seul fichier, volontairement :

  COTE CLIENT  -- start_link_flow() ouvre le navigateur, recupere le code
                  d'autorisation sur une boucle locale, et rend
                  (code, code_verifier). Le client NE PARLE PAS a l'API
                  Discord : il ne fait que promener l'utilisateur.

  COTE SERVEUR -- exchange_and_identify() echange ce code contre un
                  access_token, appelle /users/@me et rend l'identite.
                  C'est la SEULE moitie qui detient le client_secret.

Pourquoi ce decoupage : le client est distribue en clair, tout secret
qu'on y mettrait n'en serait pas un. Et surtout, un client peut mentir :
s'il annoncait lui-meme "je suis le Discord X", n'importe qui pourrait se
faire passer pour n'importe qui. En faisant l'echange cote serveur, c'est
DISCORD qui affirme l'identite, pas le joueur.

PKCE (RFC 7636) protege le trajet par le navigateur : le code
d'autorisation qui transite dans l'URL de redirection ne vaut rien sans
le code_verifier, qui lui n'a jamais quitte la machine du joueur.

Aucune dependance : stdlib uniquement, rien a ajouter a
RELEASE_PIP_PACKAGES.

--- A configurer ---

Cote client  : DISCORD_CLIENT_ID, REDIRECT_PORT (doivent correspondre au
               portail Discord AU CARACTERE PRES).
Cote serveur : discord_client_secret dans circusvoip_server_config.json.
               Ne JAMAIS le mettre dans un fichier livre au joueur.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# ---------------------------------------------
#  Constantes
# ---------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"

# Port de la boucle locale sur laquelle Discord renvoie le joueur.
# DOIT etre declare a l'identique dans le portail Discord :
#   http://127.0.0.1:53682/callback
# Discord exige une correspondance EXACTE, d'ou un port fixe et non un
# port ephemere.
REDIRECT_PORT = 53682
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

# "identify" donne l'id, le pseudo Discord et l'avatar. C'est tout ce
# dont on a besoin. Pas d'email, pas de guilds : un scope qu'on ne
# demande pas est un scope qu'on n'a pas a proteger.
SCOPE = "identify"

# Delai laisse au joueur pour cliquer "Autoriser" dans son navigateur.
LINK_TIMEOUT_SEC = 180.0

# Delai des appels HTTP vers Discord (cote serveur).
HTTP_TIMEOUT_SEC = 15.0

# [DISCORD 30/07/2026] User-Agent OBLIGATOIRE. Sans lui, urllib envoie
# "Python-urllib/3.x" et Cloudflare -- qui est devant discord.com --
# rejette la requete en 403 avec "error code: 1010" (signature de
# navigateur bannie). L'erreur ne vient donc PAS de Discord et n'a rien a
# voir avec le client_secret : elle tombe avant meme d'atteindre l'API.
# Discord documente par ailleurs l'obligation d'un User-Agent identifiant
# l'application.
USER_AGENT = "CircusVOIP (https://github.com/circusvoip, 0.4.0)"


class DiscordAuthError(Exception):
    """Echec de la liaison. Message destine a etre montre au joueur."""


# ---------------------------------------------
#  PKCE
# ---------------------------------------------

def _b64url(data: bytes) -> str:
    """base64url SANS padding, comme l'exige la RFC 7636."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce() -> tuple[str, str]:
    """Retourne (code_verifier, code_challenge) en methode S256.

    Le verifier fait 43 caracteres (32 octets encodes), dans les bornes
    43-128 imposees par la RFC.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(client_id: str, code_challenge: str,
                        state: str, redirect_uri: str = REDIRECT_URI) -> str:
    params = {
        "client_id": str(client_id),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        # Force l'ecran de consentement plutot que de reutiliser une
        # autorisation existante : la liaison est un acte rare et
        # explicite, le joueur doit voir ce qu'il autorise.
        "prompt": "consent",
    }
    return DISCORD_AUTHORIZE + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------
#  COTE CLIENT : recuperation du code
# ---------------------------------------------

_PAGE_OK = """<!doctype html><html lang="fr"><meta charset="utf-8">
<title>CircusVOIP</title>
<body style="font-family:sans-serif;background:#1a1a1a;color:#eee;
text-align:center;padding-top:80px">
<h2>Compte Discord relie</h2>
<p>Vous pouvez fermer cette page et revenir a CircusVOIP.</p>
</body></html>"""

_PAGE_KO = """<!doctype html><html lang="fr"><meta charset="utf-8">
<title>CircusVOIP</title>
<body style="font-family:sans-serif;background:#1a1a1a;color:#eee;
text-align:center;padding-top:80px">
<h2>Liaison annulee</h2>
<p>{msg}</p>
</body></html>"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serveur d'un seul coup : encaisse la redirection puis s'arrete."""

    # Renseignes par start_link_flow avant le service.
    expected_state = ""
    result: dict = {}
    done_event: threading.Event | None = None

    def do_GET(self):  # noqa: N802 (nom impose par BaseHTTPRequestHandler)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        qs = urllib.parse.parse_qs(parsed.query)

        def finish(code: int, body: str):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            if _CallbackHandler.done_event:
                _CallbackHandler.done_event.set()

        # Refus du joueur, ou erreur cote Discord.
        if "error" in qs:
            desc = qs.get("error_description", [qs["error"][0]])[0]
            _CallbackHandler.result = {"error": desc}
            finish(200, _PAGE_KO.format(msg="Vous avez refuse l'autorisation."))
            return

        # Verification de l'etat : protege du CSRF, et surtout empeche
        # qu'une redirection fabriquee par un tiers nous fasse avaler un
        # code d'autorisation qui n'est pas le notre.
        state = qs.get("state", [""])[0]
        if not secrets.compare_digest(state, _CallbackHandler.expected_state):
            _CallbackHandler.result = {"error": "etat invalide (state)"}
            finish(400, _PAGE_KO.format(msg="Requete invalide."))
            return

        code = qs.get("code", [""])[0]
        if not code:
            _CallbackHandler.result = {"error": "code absent"}
            finish(400, _PAGE_KO.format(msg="Reponse incomplete."))
            return

        _CallbackHandler.result = {"code": code}
        finish(200, _PAGE_OK)

    def log_message(self, *args):
        """Silence : sinon chaque requete part sur stderr du client."""


def start_link_flow(client_id: str,
                    redirect_port: int = REDIRECT_PORT,
                    timeout: float = LINK_TIMEOUT_SEC,
                    open_browser: bool = True) -> tuple[str, str]:
    """COTE CLIENT. Ouvre le navigateur, attend le retour de Discord.

    Retourne (code, code_verifier), a transmettre au serveur qui fera
    l'echange. Leve DiscordAuthError sur refus, expiration ou erreur.

    Le port doit etre libre : s'il ne l'est pas, on echoue explicitement
    plutot que de laisser le joueur devant un navigateur qui tourne dans
    le vide. Changer de port ne servirait a rien, Discord n'accepte que
    l'URI declaree.
    """
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://127.0.0.1:{redirect_port}/callback"

    _CallbackHandler.expected_state = state
    _CallbackHandler.result = {}
    _CallbackHandler.done_event = threading.Event()

    try:
        httpd = http.server.HTTPServer(("127.0.0.1", redirect_port),
                                       _CallbackHandler)
    except OSError as e:
        raise DiscordAuthError(
            f"port {redirect_port} indisponible ({e}). Une autre instance "
            "de CircusVOIP est-elle en cours de liaison ?"
        ) from e

    httpd.timeout = 1.0
    thread = threading.Thread(target=httpd.serve_forever,
                              kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()

    url = build_authorize_url(client_id, challenge, state, redirect_uri)
    try:
        if open_browser:
            webbrowser.open(url)
        if not _CallbackHandler.done_event.wait(timeout):
            raise DiscordAuthError(
                "delai depasse : aucune reponse de Discord. "
                "La liaison a-t-elle ete validee dans le navigateur ?")
        res = _CallbackHandler.result
        if "error" in res:
            raise DiscordAuthError(res["error"])
        return res["code"], verifier
    finally:
        # Le serveur local ne doit pas survivre a la liaison : il ecoute
        # sur la machine du joueur.
        httpd.shutdown()
        httpd.server_close()


def is_redirect_port_free(port: int = REDIRECT_PORT) -> bool:
    """Verifie que le port de redirection est libre, sans rien ouvrir.

    Permet de prevenir le joueur AVANT d'ouvrir son navigateur.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------
#  COTE SERVEUR : echange + identite
# ---------------------------------------------

def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise DiscordAuthError(
            f"Discord a refuse l'echange ({e.code}) {detail}") from e
    except Exception as e:
        raise DiscordAuthError(f"Discord injoignable : {e}") from e


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise DiscordAuthError(f"lecture du profil Discord impossible : {e}") from e


def exchange_and_identify(client_id: str, client_secret: str,
                          code: str, code_verifier: str,
                          redirect_uri: str = REDIRECT_URI) -> dict:
    """COTE SERVEUR. Echange le code contre un access_token, puis lit
    l'identite Discord.

    Retourne {"discord_id": str, "username": str}.

    L'access_token n'est ni retourne ni conserve : il ne sert qu'a
    l'appel /users/@me qui suit immediatement. Une fois l'identite
    etablie, on n'a plus rien a demander a Discord -- c'est notre propre
    jeton local (cf circusvoip_accounts) qui prend le relais.
    """
    if not client_secret:
        raise DiscordAuthError(
            "discord_client_secret absent de la config serveur")

    tok = _post_form(f"{DISCORD_API}/oauth2/token", {
        "client_id": str(client_id),
        "client_secret": str(client_secret),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    })
    access = tok.get("access_token")
    if not access:
        raise DiscordAuthError("Discord n'a pas renvoye d'access_token")

    me = _get_json(f"{DISCORD_API}/users/@me", access)
    did = str(me.get("id", ""))
    if not did:
        raise DiscordAuthError("Discord n'a pas renvoye d'identifiant")

    # global_name = nom d'affichage actuel, username = identifiant.
    # Purement informatif (colonne de l'admin) : ces deux valeurs
    # changent, seul l'id est stable et sert de cle.
    username = me.get("global_name") or me.get("username") or ""
    return {"discord_id": did, "username": str(username)}
