# CircusVOIP — 0.4.0 Alpha Build 72

**Statut** : préparé — non publié
**Type** : mise à jour **client + serveur**. Le serveur doit être redéployé, et
**avant** le client.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_server.py` | VPS | `f17928fdf1434f011767a5d9fd230e84` |
| `circusvoip_accounts.py` | VPS | `2437f62e4a7f9027344b1dfe933dfb61` |
| `circusvoip_accounts_ws.py` | VPS | `8bf862108bdf5dd738b072bd70c8260e` |
| `circusvoip_travail_store.py` | VPS | `506d219efa34c86dae80af24e0251b2b` |
| `circusvoip_phone_travail.py` | **VPS + build** | `65099c43c50bd8a74d0dfb4cd551368a` |
| `circusvoip_client.py` | build | `41f2c8c8eb82dcf765f281c92f1374d7` |
| `circusvoip_core.py` | build | `24717295e236299ebd13f42932231c40` |
| `circusvoip_opus.py` | build | `49175c92653992c6521716bfdf002f30` |
| `circusvoip_audio_io.py` | build | `9a8d9ea152ae192b0dfa6a6d2b025db8` |
| `circusvoip_phone_travail_app.py` | build | `2eb590105cb7ec93c51ecddc75ad967d` |
| `circusvoip_phone_apps.py` | build | `a39d7a04c9605af5e9ebe53d07f61fd7` |
| `circusvoip_phone_annuaire.py` | build | `c64951353e44fe7cf2f356bc6ba5833a` |
| `build_update.py` | dépôt | `7bf340114443ad3821f2de5dc7a244d7` |
| `circusvoip_mannequin.py` | outil | `39b6251cc6adf11a53d5119ccd7dd853` |

> **`circusvoip_phone_travail.py` va des DEUX côtés.** C'est lui qui contient
> les règles des missions. Ne le pousser que d'un côté fait diverger le client
> et le serveur — sans erreur visible : ils appliqueraient simplement des
> règles différentes, et l'un accepterait ce que l'autre refuse.

> Aucune purge, aucune perte de données. Les jetons déjà émis restent valides.

---

## L'essentiel

Ce build apporte l'**app Travail**, un tableau d'annonces entre joueurs, et
achève le chantier de la **sonnerie de téléphone** commencé au build 71.

Il neutralise aussi la rotation du jeton local livrée au build 71 : le
mécanisme était correct, mais son coût en développement dépassait son bénéfice
tant que le parc de clients n'est pas stabilisé.

---

## 1. App Travail — tableau d'annonces

**Le besoin.** Faire se rencontrer les joueurs par métier plutôt que par
connaissance préalable. Un mineur qui cherche une escorte n'a aucun moyen, en
jeu, de savoir quels mercenaires sont disponibles.

**Le principe, et le point contre-intuitif.** Le métier d'une mission est celui
qu'on **cherche**, pas celui de l'auteur. Un mineur publie donc une mission
`mercenaire`. C'est ce qui fait tourner le jeu de rôle : chacun a besoin des
autres. Le modèle inverse — la mission portant le métier de son auteur —
produirait exactement le contraire, des mineurs parlant à des mineurs.

**Huit métiers**, deux maximum par joueur : artisan, ferrailleur, mécanicien,
mercenaire, mineur, pilote, ravitailleur, transporteur. Ils vivent dans la
fiche du joueur, à côté de son numéro : ils survivent aux reconnexions, et
l'app Urgence pourra plus tard lire l'annuaire au lieu de tenir un second
registre.

Le plafond de deux n'est pas cosmétique. Sans lui, tout le monde finirait par
tout être, et « appeler un mécanicien » perdrait tout sens.

**Le paiement est du texte libre.** Un montant en aUEC ne couvre pas ce qui se
négocie réellement : « part du butin », « carburant + 200k », « à discuter ».
Imposer un entier obligeait à mentir dans la description. Le prix est assumé :
on ne pourra ni trier ni filtrer par montant — c'est un tableau d'annonces, pas
une comptabilité.

**Le serveur arbitre, toujours.** Deux joueurs peuvent cliquer « Prendre » sur
la même mission dans la même seconde. Le client ne retire jamais une mission de
sa propre liste : il envoie une intention et redessine ce que le serveur
répond. Sans cette discipline, chaque écran donnerait raison à son
propriétaire, et l'un des deux se déplacerait pour rien.

**Répartition des droits.** L'auteur seul termine une mission — un exécutant
qui pourrait clore signerait sa propre livraison. L'exécutant seul abandonne,
ce qui l'empêche d'être bloqué par un auteur qui ne revient pas. Chacun garde
la main sur ce qui le concerne.

**Notification ciblée.** Une mission ne concerne qu'un métier : sur 100 joueurs
dont 10 mercenaires, la publication fait 10 messages et non 100. Même principe
que la diffusion par zone. Badge rouge sur l'icône et son de notification —
celui de la messagerie, pas un motif propre : le joueur apprend un son, pas
six.

**Expiration à 30 jours**, closes gardées 7 jours pour l'historique. Sans
purge, une annonce abandonnée en silence resterait indéfiniment et l'onglet
deviendrait illisible en quelques mois.

**Vérifié** : 171 contrôles automatisés, dont 50 threads simultanés sur une
même mission (un seul gagnant), 20 publications concurrentes (le plafond de 5
tient exactement), et un JSON corrompu qui n'est jamais écrasé.

---

## 2. Sonnerie de téléphone — flux dédié

**Le symptôme.** Le curseur « Sonnerie tél. » n'avait aucun effet sur les
sonneries des autres joueurs. On subissait le réglage de l'émetteur.

**La cause.** La sonnerie était **additionnée à la voix avant l'encodage**.
Une fois les deux sommées dans un seul signal Opus, plus rien ne les
distingue : le récepteur ne pouvait pas baisser l'une sans l'autre.

**Le correctif.** Un flag dédié `0x04`, avec son propre encodeur. Le volume est
désormais décidé par le récepteur : `son curseur × 0,05 × distance`.

Deux préalables ont dû être écrits :

- **Encodeur par flux** dans `circusvoip_opus.py`, en miroir du décodeur par
  flux du 31/07. Opus est à état : alterner deux contenus différents dans un
  encodeur unique le désynchronise.
- **File de réception par flux** dans `circusvoip_audio_io.py`. Une file unique
  par émetteur saturait — 100 trames par seconde entrantes pour 50 consommées
  — et produisait un grésillement avec la voix hachée.

**Effet de bord voulu** : le PTT ne met plus la sonnerie en pause. On peut
parler pendant que son téléphone sonne autour de soi.

`PHONE_RING_TX_FACTOR` passe de 0,50 à **0,05** — calibré à l'oreille en
session. Historique : 0,70 → 0,50 → 0,05.

---

## 3. Rotation du jeton local — neutralisée

Le mécanisme du build 71 est **conservé mais désactivé** par un retour anticipé
dans `_join_ok`.

Il était correct : un jeton volé ne valait plus que jusqu'à la prochaine
connexion de sa victime. Mais il suppose un client qui écrit le jeton reçu, et
cette condition ne tient pas pendant le développement — chaque version
intermédiaire, chaque fichier restauré grille la session suivante passé la
fenêtre de tolérance de 5 minutes.

Rien n'est supprimé. Il suffira de retirer un `return` pour réactiver, de
préférence en conditionnant la rotation à une capacité annoncée par le client.

---

## 4. Corrections diverses

**Atténuation verticale — rémanence de 8 secondes.** Le correctif du build 71
avait supprimé la cause structurelle du clignotement ; restait le bruit OCR sur
le nom lui-même. Un `rsi_polaris` lu `polaris` faisait tomber l'atténuation
pendant une lecture. La dernière détection franche vaut désormais 8 secondes.
Le journal distingue `[direct]` de `[remanence]` : sans quoi la rémanence
masquerait en silence un OCR qui se dégrade.

**Reconnexion audio muette.** Le bloc `finally` de l'ancien thread WebSocket
déposait sa sentinelle dans la file du **nouveau** thread, tuant son émetteur.
Socket ouvert, réception normale, mais plus rien n'était émis — et le serveur
affichait `parleurs=0` sans la moindre erreur. C'est une course : elle ne se
produisait que si l'ancien thread finissait après que le nouveau ait réassigné
la globale.

**Appel abandonné = appel manqué.** Raccrocher pendant la sonnerie après au
moins 5 secondes laisse désormais une trace, chez l'appelant comme chez
l'appelé. Auparavant, seule l'expiration des 45 secondes en produisait une : un
appel de 40 secondes disparaissait sans laisser de trace.

**Flèches de sens dans l'historique.** ↑ verte pour un appel sortant, ↓ rouge
pour un entrant. Le sens figurait déjà en toutes lettres, mais il fallait le
lire ; sur dix appels, l'œil cherche un motif. Le texte reste sous la flèche —
l'information n'est portée par la couleur seule nulle part.

**Messages de refus traduits.** Le bandeau rouge affichait la clôture WebSocket
brute — illisible, et surtout muette sur ce qu'il fallait faire. Huit motifs
sont désormais traduits en une phrase qui nomme la cause **et** l'action. Un
motif inconnu retombe sur le texte brut : mieux vaut un message technique qu'un
message faux. L'état de liaison passe à « À relier » en orange quand le compte
est refusé — afficher « Relié » en vert pendant qu'un bandeau rouge demande de
relier son compte invitait à ne rien faire.

**Micro affiché ≠ micro utilisé.** Après un repli au démarrage, le bouton
gardait le libellé du micro demandé tandis que la liste pointait sur celui
réellement ouvert. Le blocage de signal qui évite une boucle de redémarrage
empêchait aussi le rafraîchissement de l'affichage.

**Diagnostic micro.** `Erreur demarrage micro : <périphérique>` ne disait pas
pourquoi, alors que la cause exacte — `Invalid sample rate` — était connue du
code et imprimée sur une console que personne ne regarde. La raison, la
solution et la conséquence sont désormais affichées. Ce message a coûté deux
diagnostics d'une demi-journée le 06/08.

**`reset()` d'Opus.** Vidait une globale devenue fantôme après le passage à
l'encodeur par flux : les encodeurs réels survivaient aux reconnexions avec
l'état de la session précédente.

**Décodeur du mannequin.** Restait indexé sur le seul émetteur alors que le
client l'indexe sur `(émetteur, flag)` depuis le 31/07. Deux flux simultanés
du même joueur le désynchronisaient — grésillement et voix inintelligible.

---

## Ordre de déploiement

**1. Serveur d'abord.** Le client émet des trames `travail_*` qu'un serveur non
mis à jour ignorerait en silence : l'app afficherait « Chargement… »
indéfiniment, sans erreur.

```
scp circusvoip_server.py circusvoip_accounts.py circusvoip_accounts_ws.py \
    circusvoip_travail_store.py circusvoip_phone_travail.py \
    root@178.104.207.46:/home/circusvoip/app/
ssh root@178.104.207.46 "chown circusvoip:circusvoip /home/circusvoip/app/*.py \
    && systemctl restart circusvoip-server.service"
```

Vérifier l'absence de bandeau `[TRAVAIL] *** APP TRAVAIL DESACTIVEE ***` :

```
ssh root@178.104.207.46 "journalctl -u circusvoip-server.service -n 40 \
    --no-pager | grep -i 'TRAVAIL\|QUEUE\|COMPTES\|Traceback'"
```

**2. Puis le client.**

```
py -3 build_update.py --bump-build --notes "Build 72 — app Travail, sonnerie sur flux dédié"
```

---

## Ce que ce build ne corrige pas

- **`invalid_ticket` sur le serveur audio** après reconnexion — observé, cause
  non élucidée.
- **Le son de notification en rafale** si plusieurs missions arrivent d'affilée :
  aucun délai minimal entre deux sons.
- **L'atténuation de distance de la sonnerie** suit la même courbe que la voix,
  alors qu'un téléphone dans une poche porte moins loin.
- **18 fonctions publiques sans docstring** dans les trois modules Travail.
- **L'aller-retour réseau à deux clients réels** n'a jamais été testé : les 171
  contrôles portent sur les règles et le stockage isolément.
