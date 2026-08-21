# CircusVOIP — Installer son propre serveur

Guide d'installation d'une instance CircusVOIP sur un serveur Linux nu.
Testé sur **Ubuntu 24.04 LTS**.

Héberger son propre serveur permet à un groupe de joueurs d'avoir sa VOIP
positionnelle indépendante : le serveur relaie les positions et l'audio de
proximité entre les clients connectés.

La pile est volontairement minimaliste : **pas de base de données, pas de
reverse proxy, pas de conteneur**. Deux services, deux ports.

---

## Prérequis

- Un serveur Linux (Ubuntu 24.04 recommandé), accès `root` ou `sudo`.
- Python 3.12 (fourni de base sur Ubuntu 24.04).
- Deux ports TCP ouvrables : **8888** (positions) et **8889** (audio) par
  défaut. Depuis le 26/07/2026 ils sont **configurables** : voir §4.
- Aucun nom de domaine requis : les clients se connectent à l'adresse IP.

Dimensionnement : un petit VPS (2 vCPU / 2 Go) suffit largement pour un
groupe de quelques joueurs. Les positions sont diffusées à tous les clients
connectés, donc la charge croît rapidement avec le nombre de joueurs
simultanés ; le projet n'a pas été éprouvé au-delà d'une petite dizaine.

---

## 1. Utilisateur et arborescence

Les services ne doivent **jamais** tourner en `root`.

```bash
sudo adduser --system --group --home /home/circusvoip circusvoip
sudo mkdir -p /home/circusvoip/app
sudo chown -R circusvoip:circusvoip /home/circusvoip
```

## 2. Python et environnement virtuel

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

sudo -u circusvoip python3 -m venv /home/circusvoip/app/venv
sudo -u circusvoip /home/circusvoip/app/venv/bin/pip install \
    websockets cryptography
```

Deux dépendances, c'est tout (`cffi` et `pycparser` sont tirés
automatiquement par `cryptography`, qui sert à générer le certificat TLS).

## 3. Déposer le code serveur

Copier ces fichiers depuis `server/` du dépôt vers `/home/circusvoip/app/` :

| Fichier | Rôle |
|---|---|
| `circusvoip_server.py` | serveur de positions (port 8888 par défaut) |
| `circusvoip_mp_server.py` | lobbies multijoueur — **importé** par le précédent, pas un service |
| `circusvoip_audio_server.py` | serveur audio (port 8889 par défaut) |
| `circusvoip_server_config.py` | token d'accès **et ports** |
| `circusvoip_security.py` | TLS, tickets d'authentification |
| `circusvoip_accounts.py` | comptes joueurs, annuaire, attribution des numéros |
| `circusvoip_accounts_ws.py` | liaison Discord côté serveur |
| `circusvoip_discord_auth.py` | échange OAuth Discord (moitié serveur) |
| `circusvoip_phone_queue.py` | messagerie différée + anti-spam |

Les quatre derniers sont apparus avec la v0.4.0 (builds 66 et 69).

> ⚠ `circusvoip_accounts.py` et `circusvoip_accounts_ws.py` contiennent
> l'annuaire complet : ils ne doivent **jamais** être distribués aux clients.
> `circusvoip_discord_auth.py`, lui, est partagé — il vit des deux côtés.

> ℹ️ Ces quatre fichiers sont **facultatifs**. Sans eux le serveur démarre et
> fonctionne, simplement sans comptes Discord ni messagerie différée : les
> imports sont gardés. C'est ce qui permet de déployer en deux temps — mais
> l'absence est **silencieuse**, donc vérifiez plutôt deux fois.

```bash
sudo chown -R circusvoip:circusvoip /home/circusvoip/app
```

> ⚠ `circusvoip_security.py` doit rester **identique** à celui du client.
> Une divergence casse l'authentification.

## 4. Rien à configurer à la main

Au premier lancement, le serveur crée tout seul :

- `circusvoip_server_config.json` — **token d'accès joueurs**, généré
  aléatoirement (16 caractères) ;
- `circusvoip_admin_token.json` — token d'administration ;
- `cert.pem` / `key.pem` — certificat TLS auto-signé ;
- `circusvoip_profiles.json`, `circusvoip_channels.json`,
  `circusvoip_highscores.json`, `circusvoip_auth_tickets.json`,
  `profile_photos/` — état runtime, créés vides ;
- `circusvoip_accounts.json` — **comptes joueurs** : identité Discord, pseudo
  et numéro attribué. C'est le fichier le plus précieux du serveur : le
  perdre réattribue des numéros à tout le monde ;
- `phone_queue/` — messagerie différée, un fichier JSON par numéro de
  destinataire. Créé au démarrage, vide.

### Changer les ports

`circusvoip_server_config.json` contient aussi les ports :

```json
{ "port_positions": 8888, "port_audio": 8889, "port_update": 8080 }
```

Les modifier puis redémarrer les services suffit. Pensez à ouvrir les
nouveaux ports au pare-feu (§6) **et** à prévenir les joueurs : un client
configuré sur l'ancien port ne se connectera plus, avec un simple « serveur
injoignable » qui ne dit pas que le port est en cause.

Le token joueurs est affiché dans les logs au démarrage : c'est lui que les
joueurs saisiront dans leur client, avec l'adresse IP du serveur.

> 🔒 Ces fichiers contiennent vos secrets : ne les publiez jamais et ne les
> versionnez pas.

## 5. Services systemd

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

## 6. Pare-feu

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8888/tcp     # positions (wss)
sudo ufw allow 8889/tcp     # audio (wss)
sudo ufw enable
```

Adaptez les numéros si vous avez changé les ports (§4). Vérifiez aussi le
pare-feu **de l'hébergeur** (groupe de sécurité) : il est distinct d'`ufw` et
c'est la cause la plus fréquente de « les clients ne se connectent pas ».

## 7. Dossiers de logs (optionnel)

```bash
sudo mkdir -p /var/log/circusvoip-positions /var/log/circusvoip-audio
sudo chown circusvoip:circusvoip /var/log/circusvoip-*
```

S'ils n'existent pas, les serveurs écrivent dans `circusvoip_debug/` à côté
du code. Aucune rotation automatique n'est prévue : pensez à purger
régulièrement, les logs de positions grossissent vite.

---

## Vérification

```bash
sudo journalctl -u circusvoip-server -n 30 --no-pager
sudo journalctl -u circusvoip-audio  -n 30 --no-pager
```

Vous devez voir le démarrage, la ligne TLS, le port d'écoute et le token.
Côté client : saisir l'adresse IP du serveur et le token, puis se connecter.

### Vérifier la messagerie différée

Le module ne s'annonce pas au démarrage, et la purge de rétention ne logue
que si elle supprime quelque chose : sur un serveur neuf, **le silence est
normal** et ne prouve rien. Le témoin fiable est le dossier, créé au
chargement du module :

```bash
ls -ld /home/circusvoip/app/phone_queue/
```

S'il existe et appartient à `circusvoip`, le module est chargé. S'il manque
alors que `circusvoip_phone_queue.py` est bien présent, l'import a échoué —
et le serveur tourne alors comme avant, sans le dire.

En fonctionnement, la file d'un joueur hors ligne apparaît sous son numéro
(`425874.json`) et disparaît après son retour :

```bash
ls -l /home/circusvoip/app/phone_queue/
journalctl -u circusvoip-server -f | grep -E 'QUEUE|PHONE-'
```

## Notes

- **Certificat auto-signé** : c'est normal et suffisant. Le client ne vérifie
  pas la chaîne de certification — le chiffrement du transport est assuré,
  mais il n'y a pas d'authentification du serveur par certificat.
- **Serveur de mise à jour** : le client interroge au démarrage un service
  d'update optionnel sur le port 8080. S'il est absent, l'appel échoue en
  silence et le client fonctionne normalement. Ce guide ne l'installe pas.
- **Mises à jour du serveur** : le serveur ne se met **pas** à jour tout seul.
  Pour appliquer une nouvelle version, copiez les fichiers puis
  `sudo systemctl restart circusvoip-server circusvoip-audio`.
- **Sauvegarde** : conservez `circusvoip_server_config.json`,
  `circusvoip_admin_token.json`, `circusvoip_accounts.json` et les JSON
  d'état. Perdre le token joueurs oblige tous les joueurs à en saisir un
  nouveau ; perdre `circusvoip_accounts.json` réattribue les numéros de tout
  le monde, ce qui invalide les carnets de contacts de chacun.
- **Messagerie différée** : les messages en attente sont conservés **30
  jours** à partir de leur envoi, puis purgés automatiquement (au démarrage,
  puis toutes les 24 h). Plafond de **20 Mo par joueur** ; au-delà, les
  nouveaux envois vers cette personne sont refusés et l'expéditeur en est
  informé. Les anciens ne sont jamais effacés pour faire de la place.

## Dépannage

| Symptôme | Piste |
|---|---|
| Le service redémarre en boucle | `journalctl -u <service> -n 50` ; souvent `--headless` oublié ou une dépendance manquante |
| Les clients ne se connectent pas | port fermé côté pare-feu **ou** côté hébergeur (groupe de sécurité) |
| « token invalide » | le token saisi ne correspond pas à `circusvoip_server_config.json` |
| Tracebacks `InvalidUpgrade` dans les logs audio | connexions non-WebSocket (scans de ports) rejetées : inoffensif |
| « serveur injoignable » alors que le service tourne | port changé dans `circusvoip_server_config.json` sans que les clients le sachent, ou non ouvert au pare-feu |
| Les messages vers un joueur hors ligne se perdent encore | `circusvoip_phone_queue.py` absent de `/home/circusvoip/app/` : l'import est gardé, donc l'échec est silencieux |
| Un joueur ne peut plus se connecter sans raison | blocage anti-spam automatique, borné dans le temps. Vérifier `blocked_until` sur sa fiche dans `circusvoip_accounts.json` |
| Le mannequin ou un test de charge se fait bloquer | son compte n'est pas marqué compte de service (`discord_id` préfixé `local:`), donc il n'est pas exempté de quota |
