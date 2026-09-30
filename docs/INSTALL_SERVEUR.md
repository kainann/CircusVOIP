# CircusVOIP — Installer son propre serveur

Guide d'installation d'une instance CircusVOIP **v0.4.0**.

Héberger son propre serveur permet à un groupe de joueurs d'avoir sa VOIP
positionnelle indépendante : le serveur relaie les positions et l'audio de
proximité entre les clients connectés, et fait tourner le CircusPhone
(appels, messagerie, Travail, Urgence, Groupes, Annonces).

Deux façons d'installer :

- **Windows** : l'installeur `CircusVOIP_Server_Setup_v0.4.0.exe`, qui
  contient aussi la console d'administration → §A ;
- **Linux** (Ubuntu 24.04 testé), en service systemd → §B.

Dans les deux cas, **une étape est obligatoire : créer une application
Discord** (§C). Sans elle, aucun joueur ne peut se connecter.

La pile est volontairement minimaliste : **pas de base de données, pas de
reverse proxy, pas de conteneur**. Deux services, deux ports.

---

## Pourquoi une application Discord

Depuis la v0.4.0, chaque joueur doit relier son compte Discord pour se
connecter. C'est ce qui lui attribue un **numéro de téléphone** fixe
(42xxxx) et une identité stable : son pseudo peut changer, son numéro et
ses contacts restent.

Chaque serveur utilise **sa propre** application Discord. Le client n'en
code aucune en dur : il demande au serveur laquelle utiliser.

Tant qu'elle n'est pas configurée, le serveur démarre normalement, mais le
client affiche **« La liaison Discord n'est pas configurée sur ce
serveur »** et la connexion est refusée.

---

## A. Windows — installeur

1. Lancer `CircusVOIP_Server_Setup_v0.4.0.exe`. Pas de droits
   administrateur nécessaires : l'installation se fait par défaut sous
   `%LOCALAPPDATA%\Programs\`.
2. Démarrer **CircusVOIP Server** une première fois, puis le fermer. Ce
   premier lancement crée la configuration dans le sous-dossier `app\` du
   dossier d'installation (voir « Fichiers créés au premier lancement »).
3. Configurer l'application Discord → **§C**.
4. Redémarrer le serveur.
5. Ouvrir les ports au **pare-feu Windows** (entrée, TCP) : 8888 et 8889
   par défaut, ou ceux choisis dans la configuration. Et, si la machine est
   derrière une box, les rediriger vers elle.

**CircusVOIP Admin**, installé avec le serveur, se connecte avec l'adresse
du serveur et le **token d'administration** (voir « Fichiers créés au
premier lancement »). En local : `127.0.0.1`.

---

## B. Linux — service systemd

### B1. Prérequis

- Un serveur Linux (Ubuntu 24.04 recommandé), accès `root` ou `sudo`.
- Python 3.12 (fourni de base sur Ubuntu 24.04).
- Deux ports TCP ouvrables : **8888** (positions) et **8889** (audio) par
  défaut, configurables (voir « Changer les ports »).
- Aucun nom de domaine requis : les clients se connectent à l'adresse IP.

Dimensionnement : un petit VPS (2 vCPU / 2 Go) suffit largement pour un
groupe de quelques joueurs. Les positions sont diffusées à tous les clients
connectés, donc la charge croît rapidement avec le nombre de joueurs
simultanés ; le projet n'a pas été éprouvé au-delà d'une petite dizaine.

### B2. Utilisateur et arborescence

Les services ne doivent **jamais** tourner en `root`.

```bash
sudo adduser --system --group --home /home/circusvoip circusvoip
sudo mkdir -p /home/circusvoip/app
sudo chown -R circusvoip:circusvoip /home/circusvoip
```

### B3. Python et environnement virtuel

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

sudo -u circusvoip python3 -m venv /home/circusvoip/app/venv
sudo -u circusvoip /home/circusvoip/app/venv/bin/pip install \
    websockets cryptography
```

Deux dépendances, c'est tout (`cffi` et `pycparser` sont tirés
automatiquement par `cryptography`, qui sert à générer le certificat TLS).
L'échange avec Discord n'utilise que la bibliothèque standard.

### B4. Déposer le code serveur

Copier **tout le contenu** du dossier `server/` du dépôt vers
`/home/circusvoip/app/`, puis :

```bash
sudo chown -R circusvoip:circusvoip /home/circusvoip/app
```

Tout le dossier, et non une sélection : le serveur importe une vingtaine de
modules (comptes, apps du téléphone, règles partagées avec le client), et
**presque tous ces imports sont silencieux en cas d'absence**. Un fichier
oublié ne fait pas planter le serveur : il désactive une fonction sans le
dire — les comptes, les missions, les annonces… `circusvoip_admin.py`, qui
est une fenêtre graphique, est inutile sur un serveur sans écran mais
inoffensif.

> ⚠ `circusvoip_accounts.py`, `circusvoip_accounts_ws.py` et les
> `circusvoip_*_store.py` portent les données de **tous** les joueurs
> (annuaire, auteurs, groupes) : ils ne doivent **jamais** être distribués
> aux clients.

> ⚠ Les fichiers présents **à la fois** dans `client/` et `server/` du
> dépôt (`circusvoip_security.py`, `circusvoip_discord_auth.py`,
> `circusvoip_phone_contacts.py`, `circusvoip_phone_travail.py`,
> `circusvoip_phone_urgence.py`, `circusvoip_phone_groupes.py`,
> `circusvoip_phone_annonces.py`) doivent rester **identiques des deux
> côtés**. Une divergence ne produit aucune erreur : client et serveur
> appliquent simplement des règles différentes, et l'un refuse ce que
> l'autre accepte. Toujours les mettre à jour ensemble.

### B5. Services systemd

`/etc/systemd/system/circusvoip-server.service` :

```ini
[Unit]
Description=CircusVOIP Positions Server
After=network.target

[Service]
Type=simple
User=circusvoip
Group=circusvoip
WorkingDirectory=/home/circusvoip/app
ExecStart=/home/circusvoip/app/venv/bin/python3 /home/circusvoip/app/circusvoip_server.py --headless
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/circusvoip-audio.service` : identique, en remplaçant
`Description` par `CircusVOIP Audio Server` et le script par
`circusvoip_audio_server.py`.

Le drapeau `--headless` est **obligatoire** : sans lui, les serveurs tentent
de charger Tkinter et échouent sur une machine sans écran.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now circusvoip-server circusvoip-audio
sudo systemctl status circusvoip-server --no-pager
```

Puis configurer l'application Discord → **§C**, et redémarrer :

```bash
sudo systemctl restart circusvoip-server
```

### B6. Pare-feu

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8888/tcp     # positions (wss)
sudo ufw allow 8889/tcp     # audio (wss)
sudo ufw enable
```

Adaptez les numéros si vous avez changé les ports. Vérifiez aussi le
pare-feu **de l'hébergeur** (groupe de sécurité) : il est distinct d'`ufw`
et c'est la cause la plus fréquente de « les clients ne se connectent
pas ».

### B7. Dossiers de logs (optionnel)

```bash
sudo mkdir -p /var/log/circusvoip-positions /var/log/circusvoip-audio
sudo chown circusvoip:circusvoip /var/log/circusvoip-*
```

S'ils n'existent pas, les serveurs écrivent dans `circusvoip_debug/` à côté
du code. Aucune rotation automatique n'est prévue : pensez à purger
régulièrement, les logs de positions grossissent vite.

---

## C. Créer l'application Discord (obligatoire)

1. Ouvrir <https://discord.com/developers/applications> et cliquer
   **New Application**. Le nom est celui que verront vos joueurs au moment
   d'autoriser la liaison (par exemple le nom de votre groupe).
2. Onglet **OAuth2** → **Redirects** → ajouter **exactement** :

   ```
   http://127.0.0.1:53682/callback
   ```

   Au caractère près : `127.0.0.1` et non `localhost`, port `53682`,
   `/callback` sans barre finale. Discord refuse toute URI qui ne correspond
   pas exactement, et le joueur ne voit alors qu'une erreur dans son
   navigateur.
3. Toujours dans **OAuth2**, copier le **Client ID**, puis cliquer
   **Reset Secret** et copier le **Client Secret**.
4. Ajouter les deux valeurs dans `circusvoip_server_config.json` (dans
   `app\` du dossier d'installation sous Windows,
   `/home/circusvoip/app/` sous Linux), sans toucher aux autres clés :

   ```json
   {
     "token": "…",
     "port_positions": 8888,
     "port_audio": 8889,
     "port_update": 8080,
     "discord_client_id": "123456789012345678",
     "discord_client_secret": "votre-secret"
   }
   ```

5. Redémarrer le serveur : la configuration n'est lue qu'au démarrage.

Seul le scope `identify` est demandé : l'identifiant, le pseudo et l'avatar
Discord. Ni e-mail, ni liste de serveurs.

> 🔒 **Le Client Secret ne doit jamais quitter le serveur.** Il ne figure
> dans aucun fichier livré aux joueurs : c'est le serveur, et lui seul, qui
> échange avec Discord. Le Client ID, lui, est public — le client le
> reçoit pour ouvrir la page d'autorisation.

> ⚠ Ne pas activer `allow_service_accounts`. Cette clé, absente par
> défaut, autorise la création de comptes **sans** Discord pour
> l'outillage de test (mannequin, tests de charge) : activée sur un serveur
> de jeu, c'est un contournement de la liaison obligatoire.

---

## Fichiers créés au premier lancement

Rien à écrire à la main, **sauf les deux clés Discord** (§C). Le serveur
crée tout seul, à côté de son code :

- `circusvoip_server_config.json` — **mot de passe joueurs**, généré
  aléatoirement, et les ports. C'est dans ce fichier que vont les clés
  Discord ;
- `circusvoip_admin_token.json` — token d'administration ;
- `cert.pem` / `key.pem` — certificat TLS auto-signé ;
- `circusvoip_accounts.json` — **comptes joueurs** : identité Discord,
  pseudo, numéro attribué, bannissements. C'est le fichier le plus précieux
  du serveur : le perdre réattribue des numéros à tout le monde, ce qui
  invalide les carnets de contacts de chacun ;
- `phone_queue/` — messagerie différée, un fichier JSON par numéro de
  destinataire ;
- les fichiers d'état des apps (missions, groupes, annonces), des profils,
  canaux et meilleurs scores — créés vides, puis au fil de l'usage.

Le mot de passe joueurs est affiché au démarrage : c'est lui que les joueurs
saisiront dans leur client, avec l'adresse IP du serveur.

> 🔒 Ces fichiers contiennent vos secrets et les données de vos joueurs : ne
> les publiez jamais et ne les versionnez pas.

### Changer les ports

`circusvoip_server_config.json` contient aussi les ports :

```json
{ "port_positions": 8888, "port_audio": 8889, "port_update": 8080 }
```

Les modifier puis redémarrer suffit. Pensez à ouvrir les nouveaux ports au
pare-feu **et** à prévenir les joueurs : un client configuré sur l'ancien
port ne se connectera plus, avec un simple « serveur injoignable » qui ne
dit pas que le port est en cause. Les joueurs saisissent l'adresse sous la
forme `ip:port`.

---

## Vérification

Au démarrage, la fenêtre du serveur (Windows) ou le journal (Linux) :

```bash
sudo journalctl -u circusvoip-server -n 40 --no-pager
sudo journalctl -u circusvoip-audio  -n 30 --no-pager
```

doit montrer le démarrage, la ligne TLS, les ports d'écoute et le mot de
passe joueurs — et **aucune** ligne :

- `*** … DESACTIVE ***` : un module manque, l'exception exacte suit ;
- `[COMPTES] desactives` : les modules de comptes ne se chargent pas, et
  plus personne ne peut se connecter.

Puis le vrai test : un joueur saisit l'adresse et le mot de passe, relie son
compte avec le bouton **COMPTE DISCORD**, autorise dans son navigateur, et
se connecte. Il reçoit un numéro en 42xxxx.

### Vérifier la messagerie différée

Le module ne s'annonce pas au démarrage : sur un serveur neuf, **le silence
est normal** et ne prouve rien. Le témoin fiable est le dossier, créé au
chargement du module :

```bash
ls -ld /home/circusvoip/app/phone_queue/
```

---

## Notes

- **Certificat auto-signé** : c'est normal et suffisant. Le client ne
  vérifie pas la chaîne de certification — le chiffrement du transport est
  assuré, mais il n'y a pas d'authentification du serveur par certificat.
- **Mises à jour des clients** : un client interroge au démarrage le port
  8080 du serveur auquel il est relié, pour **signaler** une version plus
  récente. Il n'installe rien de lui-même. Ce guide n'installe pas de
  serveur de mise à jour : sans lui, l'appel échoue en silence et le client
  fonctionne normalement. Les joueurs se mettent à jour avec un nouvel
  installeur.
- **Mises à jour du serveur** : le serveur ne se met **pas** à jour tout
  seul. Réinstaller par-dessus (Windows) conserve la configuration et les
  données : l'installeur ne remplace aucun fichier `.json`. Sous Linux,
  copier les nouveaux fichiers puis
  `sudo systemctl restart circusvoip-server circusvoip-audio`.
- **Sauvegarde** : conservez `circusvoip_server_config.json`,
  `circusvoip_admin_token.json`, `circusvoip_accounts.json` et les autres
  JSON d'état. Perdre le mot de passe joueurs oblige chacun à en saisir un
  nouveau ; perdre les clés Discord oblige à les recopier depuis le portail
  (le secret devra être régénéré) ; perdre `circusvoip_accounts.json`
  réattribue les numéros de tout le monde.
- **Messagerie différée** : les messages en attente sont conservés **30
  jours** à partir de leur envoi, puis purgés automatiquement. Plafond de
  **20 Mo par joueur** ; au-delà, les nouveaux envois vers cette personne
  sont refusés et l'expéditeur en est informé.
- **Modération** : bannir un joueur depuis l'admin le déconnecte aussitôt.
  Le bannissement porte sur son compte **Discord** : il survit à la
  suppression de sa fiche, et relier à nouveau le même compte ne le lève
  pas.

## Dépannage

| Symptôme | Piste |
|---|---|
| « La liaison Discord n'est pas configurée sur ce serveur » | `discord_client_id` absent de `circusvoip_server_config.json`, ou serveur non redémarré après l'avoir ajouté |
| Le navigateur du joueur affiche une erreur Discord sur l'URI de redirection | l'URI déclarée dans le portail n'est pas **exactement** `http://127.0.0.1:53682/callback` |
| « Discord a refusé l'échange » | `discord_client_secret` faux, ou régénéré dans le portail sans être recopié |
| « port 53682 indisponible » chez le joueur | une autre liaison est en cours sur sa machine, ou un autre programme occupe ce port |
| « Connexion refusée : aucun compte relié » | le joueur n'a pas encore relié son compte Discord |
| Le service redémarre en boucle | `journalctl -u <service> -n 50` ; souvent `--headless` oublié ou une dépendance manquante |
| Les clients ne se connectent pas | port fermé côté pare-feu **ou** côté hébergeur (groupe de sécurité), ou non redirigé par la box |
| « token invalide » / mot de passe refusé | le mot de passe saisi ne correspond pas à `circusvoip_server_config.json` |
| Une app du téléphone reste vide ou refuse tout | un module serveur manque : chercher `DESACTIVE` au démarrage |
| Tracebacks `InvalidUpgrade` dans les logs audio | connexions non-WebSocket (scans de ports) rejetées : inoffensif |
| « serveur injoignable » alors que le service tourne | port changé sans que les clients le sachent, ou non ouvert au pare-feu |
| Un joueur ne peut plus se connecter sans raison | blocage anti-spam automatique, borné dans le temps, ou bannissement : voir l'onglet MODERATION de l'admin |
