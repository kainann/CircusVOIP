# CircusVOIP — 0.4.0 Alpha Build 66

**Statut** : préparé — non publié
**Type** : mise à jour client **+ déploiement serveur** (les deux sont nécessaires)

> ⚠ **Rupture** : ce build rend le **compte obligatoire**. Un client build 65
> ou antérieur ne peut plus se connecter au serveur de développement. La mise à
> jour passe toujours par le port 8080, donc le bouton MAJ reste le chemin de
> sortie.

---

## Nouveautés joueur

### 🔗 Reliez votre compte Discord, une seule fois

- Nouveau bouton **COMPTE DISCORD** sur l'écran de connexion, avec l'état du
  compte juste à côté : *Aucun compte relié* en orange, *Relié* en vert.
- Un clic ouvre votre navigateur. Vous autorisez CircusVOIP, vous revenez, et
  c'est terminé. **Le navigateur ne se rouvrira plus jamais** : les connexions
  suivantes se font sans aucune interaction.
- Discord ne sert qu'à **savoir qui vous êtes**. Aucun message, aucun serveur,
  aucune liste d'amis n'est lu : la seule autorisation demandée est celle de
  connaître votre identifiant et votre pseudo Discord.
- Si Discord est indisponible un soir, cela n'empêche personne de jouer — seules
  les toutes premières liaisons sont concernées.

### 📇 Un numéro de téléphone attribué à la connexion

- À votre première connexion, l'annuaire vous attribue un **numéro de téléphone
  RP** à six chiffres, tiré au hasard dans la plage 420000-429999.
- Tiré au hasard et non dans l'ordre : un numéro séquentiel raconterait l'ordre
  d'arrivée des joueurs, ce qui n'a pas sa place en jeu.
- Ce numéro est **le vôtre pour de bon**. Il survit à un changement de pseudo,
  à une réinstallation, à un changement de machine.
- Il n'est attribué qu'au moment où vous entrez réellement en jeu : relier son
  compte sans jamais se connecter ne consomme aucun numéro.

### 🪪 Votre pseudo, vérifié à la connexion

- Le pseudo reste celui du champ **Nom**. Il est vérifié au moment où vous vous
  connectez, et deux joueurs ne peuvent plus porter le même.
- **La casse est ignorée** : `Hugo`, `hugo` et `HUGO` sont le même pseudo. Vous
  gardez l'écriture que vous avez choisie, elle s'affiche telle quelle.
- Changer de pseudo est libre : votre fiche vous suit, numéro compris, et
  l'ancien nom redevient disponible pour quelqu'un d'autre.
- Si le nom est déjà pris, la connexion est refusée avec un message explicite
  plutôt qu'une déconnexion silencieuse.

### 🚪 Connexion impossible sans compte

- Le bouton CONNECTER n'ouvre plus rien tant qu'aucun compte n'est relié : un
  message rouge apparaît au-dessus des champs pour le dire, à côté du bouton
  qui résout le problème.
- Le contrôle est aussi appliqué **côté serveur** : le message du client n'est
  qu'une politesse, c'est le serveur qui décide.

### 🗂 Écran de connexion réorganisé

- La ligne Nom / Serveur / MDP était saturée. L'état du compte et son bouton
  occupent désormais **leur propre ligne, au-dessus** — dans l'ordre réel des
  opérations : on relie son compte, puis on se connecte.

---

## Côté administration

### 📖 Annuaire consultable depuis l'admin

- Nouveau bouton **ANNUAIRE** dans la barre de connexion. Une ligne par fiche :
  numéro, pseudo, compte Discord associé.
- Chaque ligne porte une croix de suppression, avec **confirmation nominative**
  rappelant les deux conséquences : le joueur devra relier son compte, et son
  numéro repartira dans le tirage.
- Après suppression, le serveur renvoie la liste de lui-même — la fenêtre ne
  peut donc pas afficher un état différent du serveur.
- Les fiches **sans numéro** (compte relié, jamais connecté) remontent en tête.
- ⚠ **L'annuaire n'est PAS consultable par les joueurs**, et c'est délibéré :
  aucune recherche par pseudo ou par numéro ne leur est exposée. Un numéro
  s'échange en jeu, pas dans une liste.

### 🔧 Le port du serveur est enfin configurable dans l'admin

- L'adresse s'écrit `ip:port`, comme dans le client. Le port du serveur de
  développement ayant changé le 26/07 (5746), **l'admin ne pouvait plus se
  connecter du tout** : il tapait toujours sur 8888, en dur dans le code.
- Sans port précisé, on retombe sur 8888.

---

## Corrections

### 🔔 Le mannequin diffuse sa sonnerie comme un vrai client

- Quand on appelle un mannequin, son téléphone sonne **et diffuse en
  proximité**, exactement comme un joueur. Permet de régler le niveau de
  sonnerie sans mobiliser deux personnes.
- La sonnerie s'arme automatiquement sur appel entrant et s'arrête sur toutes
  les sorties : décroché, refus, expiration, raccrochage d'en face.
- Un bouton **SONNERIE (prox)** permet aussi de la déclencher à la main.

### 🔇 Le mannequin envoyait du PCM brut depuis le passage à Opus

- Sa boucle de test audio n'avait pas été migrée le 26/07 : elle envoyait des
  échantillons bruts là où le client attend de l'Opus. **Les boutons EMETTRE
  PROX et PTT RADIO du mannequin étaient donc inaudibles** contre tout client
  0.4 depuis cette date.

### 🎱 Table de billard du Starlancer MAX

- Ajoutée. L'icône Billard apparaît désormais à bord.

---

## Notes internes

- **Modèle d'identité.** L'identité est ancrée sur le `discord_id`, jamais sur
  le pseudo : le pseudo change, la fiche suit. Après la liaison, un **jeton
  local** émis par nous prend le relais et Discord n'est plus resollicité —
  d'où l'indépendance à sa disponibilité. Le jeton n'est stocké que **haché** :
  le vol du fichier de comptes ne permet pas d'usurper un joueur.
- **Répartition des fichiers**, à ne pas confondre :
  - `circusvoip_accounts.py`, `circusvoip_accounts_ws.py` → **serveur SEUL**,
    jamais dans `RELEASE_FILES` (ils contiennent l'annuaire complet et sa
    lecture).
  - `circusvoip_discord_link.py` → client seul.
  - `circusvoip_discord_auth.py` → **les deux** : `start_link_flow()` chez le
    joueur, `exchange_and_identify()` sur le VPS. Oublié côté serveur au
    premier déploiement → `[COMPTES] désactivés`.
  - `circusvoip_admin.py` → ni l'un ni l'autre.
- **PKCE, et le secret côté serveur.** Le client ne détient aucun secret : il
  promène l'utilisateur dans son navigateur et rapporte un code qui ne vaut
  rien sans le `code_verifier`. C'est le **serveur** qui échange ce code et
  appelle `/users/@me` — donc c'est Discord qui affirme l'identité, pas le
  client, qui pourrait mentir.
- **Cloudflare 1010.** Sans en-tête `User-Agent`, Cloudflare (devant
  `discord.com`) rejette l'échange en 403 `error code: 1010`. L'erreur tombe
  avant d'atteindre l'API et n'a rien à voir avec le `client_secret`.
- **Plantage à la fermeture de la fenêtre de liaison.** `accept()` ne déclenche
  pas `closeEvent` : le `QThread` du worker survivait à la liaison réussie et
  Qt détruisait un thread en cours. L'arrêt est passé dans `done()`, par où
  transitent validation, annulation et croix.
- **Clés `core-managed`.** `account_token`, `account_pseudo` et
  `account_numero` sont écrites *en cours* de session ; sans cette déclaration,
  `_save_cfg` au close les réécraserait avec la valeur du boot et il faudrait
  repasser par Discord à chaque lancement. Même piège que
  `phone_keys_defaults_applied` au build 62.
- **Comptes de service.** `allow_service_accounts` dans la config serveur
  autorise la création d'un compte SANS Discord (clé préfixée `local:`), pour
  le mannequin et les tests de charge. **Désactivé par défaut**, et à laisser
  ainsi sur un serveur de jeu : c'est un contournement de la liaison
  obligatoire pour quiconque connaît le mot de passe serveur.
- **Un décodeur Opus par émetteur**, encore. La sonnerie du mannequin a d'abord
  été émise dans un flux `0x00` séparé, alors que sa capture micro en alimente
  déjà un : deux trames entrelacées dans le même décodeur → son dégradé et
  saccadé. Elle est désormais **mélangée dans la trame de capture**, comme le
  fait `core`.
- **Config serveur à renseigner** : `discord_client_id` (public) et
  `discord_client_secret` (jamais livré au client). L'URI de redirection
  `http://127.0.0.1:53682/callback` doit être déclarée au caractère près dans
  le portail Discord.

---

## Reste à faire sur ce chantier

- Commande admin de **réattribution de numéro** (`set_numero` / `new_numero`
  existent dans le store, aucune commande ne les expose) — cas RP du changement
  de personnage.
- Création d'un compte de service **depuis l'admin**, ce qui permettrait de
  refermer `allow_service_accounts`.
- Couper l'**auto-enrichissement** de `circusphone_annuaire.json` : il
  reconstitue un annuaire par la porte de derrière, contre la décision de le
  réserver à l'admin.
- **Router la messagerie par numéro** et non par pseudo — sans quoi connaître
  un numéro ne sert à rien, et un changement de pseudo casse les conversations.
- Purge des anciens contacts, messages et images.
