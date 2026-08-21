# CircusPhone — Architecture du multijoueur (v0.3)

Document de conception. Objectif : poser **le système** commun aux jeux en réseau
(SolVsTerra, Poker, Billard) avant d'écrire le serveur et le réseau de chaque jeu.
Tout est ancré sur l'existant (serveur positions 8888, `send_ws`, positions par
joueur côté serveur, états MP déjà posés dans Billard/Poker).

---

## 1. Principe directeur

Tout passe par **le serveur CircusVOIP existant** (port 8888). Pas de Bluetooth ni
de LAN : ce qui était nommé « Bluetooth » dans le Poker devient « jouer avec les
joueurs proches », la proximité étant un **filtre serveur** basé sur les positions
qu'il connaît déjà (`clients[ws]["pos"]`, alimentées par l'OCR/VOIP positionnelle).

Une **seule** couche réseau partagée (`circusvoip_phone_mp.py`, classe `MpLobby`)
gère le flux commun. Les jeux ne parlent jamais au serveur en direct : ils créent
un `MpLobby`, écoutent ses signaux, et lui délèguent l'envoi/réception.

---

## 2. Périmètre par jeu

| Jeu         | Périmètre        | Qui peut jouer ensemble                          | Solo |
|-------------|------------------|--------------------------------------------------|------|
| SolVsTerra  | `server`         | **Tous** les joueurs connectés au serveur        | Oui  |
| Poker       | `proximity` 30 m | Joueurs **à ≤ 30 m** in-game                      | Oui  |
| Billard     | `proximity` 30 m | Joueurs **à ≤ 30 m** in-game                      | Non* |

\* Le Billard est déjà multijoueur-uniquement (décision v0.3). On peut lui ajouter
un Solo plus tard si besoin ; le système le permet sans changement.

La distance de 30 m réutilise la notion **5 m / 30 m** déjà présente côté client.
Le seuil est un paramètre serveur (`MP_PROX_RADIUS_M = 30.0`).

---

## 3. Flux utilisateur commun

```
[Menu du jeu]
   ├─ Solo  ───────────────► [Partie hors-ligne]   (jeu actuel, inchangé)
   └─ Multi ─► [Créer | Rejoindre]
                 ├─ Créer ────────────► [Lobby] (je suis hôte, en attente)
                 └─ Rejoindre ─► [Liste des parties] ─► (choix) ─► [Lobby] (membre)

[Lobby] : roster des joueurs + bouton "Prêt".
   Quand TOUS les membres (≥ 2) sont "Prêt" ─► la partie se lance pour tout le monde.
```

Règles :
- **Créer** : on attend qu'au moins un autre joueur rejoigne. L'hôte voit le roster
  se remplir en temps réel.
- **Rejoindre** : on demande la liste des parties ouvertes (filtrée serveur :
  proximité pour Poker/Billard, serveur entier pour SolVsTerra), on en choisit une.
- **Prêt** : chaque joueur bascule son état. **Lancement quand tous prêts et ≥ 2.**
  (variante possible : l'hôte confirme via un bouton "Lancer" actif seulement quand
  tous prêts — un seul interrupteur à changer dans le serveur.)

---

## 4. Machine à états des apps (partagée)

États ajoutés/alignés sur l'existant (`menu | mp | rules | playing`) :

```
menu        : accueil du jeu (Solo / Multi / Règles / Quitter ; Billard : pas de Solo)
mp_mode     : "Créer une partie" | "Rejoindre une partie"          (déjà : _mp_items)
mp_browse   : liste des parties ouvertes (Rejoindre)               (nouveau)
lobby       : roster + Prêt, en attente                            (nouveau)
playing     : partie (solo OU réseau)                              (déjà)
```

Mapping avec le code existant :
- Billard/Poker ont déjà `_state="mp"` et `_mp_items=["Créer une partie","Rejoindre
  une partie"]` + `_mp_activate()` (stub). `mp` devient `mp_mode`.
- `_mp_activate()` :
  - index 0 (Créer)   → `MpLobby.create(params)` puis `_state="lobby"`.
  - index 1 (Rejoindre) → `MpLobby.refresh_list()` puis `_state="mp_browse"`.

---

## 5. Protocole de lobby (messages `mp_*`)

Transport : JSON sur le websocket 8888. Émission via `send_ws` (=
`_core._ws_send_safe`), réception via le dispatch client (`if msg_type == …`) qui
route tout `mp_*` vers `MpLobby.handle_server_msg`.

### Client → Serveur

| type         | charge utile                         | effet |
|--------------|--------------------------------------|-------|
| `mp_create`  | `{game, scope, params}`              | crée un lobby ; le serveur attribue `lobby_id`, renvoie `mp_lobby_update`. |
| `mp_list`    | `{game, scope}`                      | demande les parties ouvertes (filtrées proximité côté serveur). |
| `mp_join`    | `{lobby_id}`                         | rejoint ; le serveur valide (proximité, capacité) et renvoie `mp_lobby_update` à tous. |
| `mp_ready`   | `{lobby_id, ready}`                  | bascule l'état prêt. |
| `mp_start`   | `{lobby_id}`                         | (hôte) demande le lancement ; le serveur exige tous prêts + ≥ min. |
| `mp_leave`   | `{lobby_id}`                         | quitte/ferme (hôte qui part = lobby dissous). |
| `mp_game`    | `{lobby_id, payload}`                | relaie un event de partie aux autres membres. |

### Serveur → Client

| type               | charge utile                                                   | effet |
|--------------------|----------------------------------------------------------------|-------|
| `mp_lobby_update`  | `{lobby_id, game, scope, host, members:[{name,ready}], params, state}` | snapshot complet du lobby (à chaque changement). |
| `mp_lobby_list`    | `{game, lobbies:[{lobby_id, host, count, max, params}]}`        | réponse à `mp_list`. |
| `mp_started`       | `{lobby_id, seed, order:[names], params}`                      | la partie démarre (seed déterministe + ordre des joueurs). |
| `mp_game`          | `{lobby_id, from, payload}`                                    | event de partie relayé. |
| `mp_error`         | `{code, msg}`                                                  | `lobby_full`, `too_far`, `not_found`, `not_host`, `not_ready`, … |
| `mp_closed`        | `{lobby_id, reason}`                                           | lobby dissous (hôte parti, vide, timeout). |

Le **serveur est autoritaire** : le client n'affiche que les snapshots reçus. Le
`seed` permet à tous les clients de générer la même partie (paquet de cartes,
positions initiales) sans tout faire transiter.

---

## 6. Proximité 30 m (Poker / Billard)

Calcul **côté serveur**, qui possède déjà les positions :

- À `mp_list` : le serveur ne renvoie que les lobbies dont l'**hôte est à ≤ 30 m**
  du demandeur (distance euclidienne sur `clients[ws]["pos"]`).
- À `mp_join` : le serveur revalide la distance hôte↔demandeur ; si > 30 m → `mp_error
  too_far`.
- En partie : si un joueur s'éloigne (> 30 m + marge), option « kick proximité » ou
  simple avertissement (à décider ; non bloquant pour la v1).

SolVsTerra (`scope="server"`) : aucun filtre de distance.

Avantage : le client n'a aucune logique de distance à maintenir ; le serveur, déjà
source de vérité des positions, tranche. (Le client connaît tout de même les
positions pour la VOIP, on pourra afficher la distance à titre indicatif.)

---

## 7. Module client partagé `circusvoip_phone_mp.py`

Livré (compile). Classe `MpLobby(QObject)` :

Signaux : `sig_list(list)`, `sig_lobby(dict)`, `sig_started(dict)`,
`sig_game(dict)`, `sig_error(code,msg)`, `sig_closed(reason)`.

Méthodes : `create(params)`, `refresh_list()`, `join(id)`, `set_ready(b)`,
`toggle_ready()`, `start()` (hôte), `leave()`, `send_game(payload)`,
`handle_server_msg(data)->bool`.

Helpers : `is_host()`, `all_ready()` (≥ 2 et tous prêts), `me()`.

Le jeu fait : `mp = MpLobby(game_id, scope, services.send_ws, my_name)`, branche les
signaux sur son rendu (roster, liste), et délègue l'échange d'events via
`send_game` / `sig_game`.

---

## 8. Câblage côté overlay (client)

Deux petits ajouts (non encore faits — à valider) :

1. **Réception** : dans le dispatch client (`if msg_type == …`), router tout type
   commençant par `mp_` vers l'app courante :
   ```python
   if isinstance(msg_type, str) and msg_type.startswith("mp_"):
       app = self._current_app
       if app is not None and hasattr(app, "handle_server_msg"):
           app.handle_server_msg(data)
       return
   ```
2. **Contrat app** : `PhoneApp` gagne une méthode optionnelle
   `handle_server_msg(data)` (no-op par défaut) que les jeux réseau surchargent
   pour relayer vers leur `MpLobby`.

`my_name` (pseudo du joueur) : déjà connu du client (table joueurs / welcome) ;
on l'expose à l'app via `PhoneServices` (champ `my_name` à ajouter) ou via un
getter de l'overlay.

---

## 9. Côté serveur (à implémenter)

Un registre de lobbies en mémoire + 7 handlers `mp_*`. Esquisse :

```python
MP_PROX_RADIUS_M = 30.0
lobbies = {}   # lobby_id -> {game, scope, host, members:{name:{ready}}, params, state}

# mp_create : crée lobby_id, host=demandeur, members={host:{ready:False}} -> update
# mp_list   : filtre par game ; si scope proximity, ne garde que hôtes <=30 m -> list
# mp_join   : valide capacité + proximité ; ajoute membre -> update à tous
# mp_ready  : maj ready ; si tous prêts & >=2 -> (auto) mp_started OU attend mp_start
# mp_start  : (hôte) si tous prêts & >=2 -> seed=random ; order=membres ; -> mp_started
# mp_leave  : retire ; hôte parti => mp_closed(host_left) à tous ; vide => purge
# mp_game   : relaie payload aux AUTRES membres (mp_game {from})
```

Distance serveur : `dist(clients[ws_a]["pos"], clients[ws_b]["pos"])`. Validation au
`join` et filtrage au `list`. Capacité : `params.max_players` (Poker 2–8, Billard 2,
SolVsTerra 2). Nettoyage : à la déconnexion d'un joueur, le retirer de ses lobbies
(réutiliser le hook de déconnexion existant qui gère déjà `leave`).

---

## 10. Intégrations spécifiques

- **Poker (aUEC)** : `params` porte le **buy-in** et les blindes (déjà esquissé dans
  l'app : `_lobby_focus`, buy-in). Le buy-in/score relie au Wallet (déjà capté côté
  log) — mais les gains de table ne sont **pas** dans le Game.log : score tenu par
  l'app/serveur, pas par le portefeuille. À cadrer.
- **SolVsTerra** : `order` = ordre de jeu ; le `seed` fixe la grille/anomalies
  communes. Partie à 2 (ou +) selon règles.
- **Billard 8-ball** : règles 2 couleurs déjà décrites ; le réseau relaie les coups
  (impulsion bille blanche) ; la physique tourne en **déterministe** depuis le même
  seed pour rester synchro (sinon arbitrage serveur des positions finales).

---

## 11. Ordre de construction proposé

1. **Overlay** : router `mp_*` → `handle_server_msg` + exposer `my_name` (petit).
2. **Serveur** : registre de lobbies + 7 handlers + distance/proximité (cœur réseau).
3. **UI lobby partagée** : écrans `mp_browse` (liste) et `lobby` (roster + Prêt),
   mutualisés (un mixin de rendu) pour les 3 jeux.
4. **Brancher un jeu pilote** : Poker (lobby déjà avancé) → valider le flux complet
   create/join/ready/start en réseau réel (machine de Florian).
5. **SolVsTerra** (scope serveur) puis **Billard** (relais de coups + déterminisme).
6. **Règles & scores** liés (8-ball, tableaux des scores).

---

## 12. Points à trancher (avant code serveur)

1. **Lancement** : auto (tous prêts ⇒ start) **ou** l'hôte confirme via "Lancer" ?
2. **Capacités** : Poker max ? (2–8 ?) ; SolVsTerra strictement 2 ou plus ?
3. **Éloignement en partie** (Poker/Billard) : kick à > 30 m, avertissement, ou rien ?
4. **Buy-in Poker** : on relie au portefeuille (aUEC réels suivis) ou score interne
   « jetons » sans lien Wallet ?
5. **Une partie à la fois par joueur** (probable) : on refuse create/join si déjà en
   lobby/partie ?
