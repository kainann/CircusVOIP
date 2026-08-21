# CircusVOIP — Changelog build 62

> **Version** : 0.3.0 **stable** build 62
> **Statut** : préparé — *non publié* (à marquer « publié le JJ/MM » une fois les tests OK)
> **Type** : mise à jour client + **1 correctif serveur déjà déployé** (22/07/2026)
> **Particularité** : premier build passé en canal **stable** — destiné à la
> publication GitHub et à un installeur `.exe` pour les nouveaux joueurs.

---

## Nouveautés joueur

### 📱 CircusPhone — raccourcis clavier par défaut

Les cinq raccourcis du téléphone étaient **vides** jusqu'ici : il fallait
aller les définir soi-même dans Paramètres, ce que personne ne faisait — la
fonctionnalité restait donc inutilisée. Ils ont désormais des valeurs par
défaut :

| Action | Touche |
|---|---|
| Ouvrir / Fermer le téléphone | **F6** |
| Décrocher | **F7** |
| Refuser / Raccrocher | **F8** |
| Mute micro (téléphone) | **F9** |
| Haut-parleur | **F10** |

- Les joueurs qui n'avaient rien configuré reçoivent ces défauts
  automatiquement à la mise à jour.
- Les raccourcis **déjà personnalisés sont conservés** : rien n'est écrasé.
- Tout reste modifiable dans **Paramètres**. Si vous effacez volontairement
  un raccourci, il ne réapparaît plus.
- ⚠ Ces touches ne sont pas bloquées : elles partent **aussi** dans Star
  Citizen. Si F6-F10 sont utilisées par vos commandes de jeu, réassignez-les
  dans Paramètres.

### 🖼 Fond d'écran — nouveau défaut

- Le fond par défaut du CircusPhone est maintenant un **noir uni**, plus
  neutre et plus lisible que l'ancien dégradé bleu nuit.
- L'image `hugolisoir.png` n'est plus le fond par défaut, et les fonds
  **hugolisoir**, **balok** et **ignis** ont été retirés du paquet.
- Les joueurs qui avaient sélectionné un de ces trois fonds retombent sur le
  fond noir. Les fonds importés par vos soins (tuile « + ») ne sont pas
  touchés.

### 👥 Compteur de joueurs en ligne

- Un libellé **« Joueurs en ligne : N »** s'affiche au-dessus de la liste
  des joueurs, dans le panneau de droite.
- Le compte inclut tout le monde, vous compris. Hors connexion, il affiche
  « — ».
- Mise à jour en temps réel à chaque arrivée ou départ.

### 🛰 VOIP — précision des zones (hangars Orison)

- Le **hangar « large top » d'Orison** est désormais correctement reconnu.
  Il ne l'était **jamais** : sur une session de test réelle, ses 162 relevés
  de position se répartissaient en **9 orthographes différentes**, toutes
  fausses, faute d'entrée de référence.
- Effet joueur : plus de micro-coupures d'audio de proximité entre joueurs
  situés dans ce hangar.
- Les autres tailles de hangars Orison ont été ajoutées par la même
  occasion.

### 🔄 Mise à jour

- Le bouton **« Vérifier les MAJ »** est masqué sur cette version stable.
  Les mises à jour restent vérifiées automatiquement à la connexion.

---

## Détails techniques (dev)

- **Raccourcis par défaut** : `phone_open_key`…`phone_speaker_key` passent de
  `None` à `f6`…`f10` dans `State` (`circusvoip_core.py`). Revirement assumé
  de la spec D4 étape 4 (« vides par défaut »). Côté client, **migration
  one-shot** au chargement de la config : les clés absentes **ou nulles**
  reçoivent le défaut, puis un flag `phone_keys_defaults_applied` est écrit.
  Le flag est ajouté à `_CORE_MANAGED_CFG_KEYS` — sinon `_save_cfg()` au
  close l'écraserait et la migration se rejouerait à chaque démarrage,
  ressuscitant un raccourci volontairement effacé.
- **Fond par défaut** : `_DEFAULT_TOP`/`_DEFAULT_BOT`
  (`circusvoip_phone_settings.py`) et `HOME_BG_DEFAULT_TOP`/`_BOT`
  (`circusvoip_phone_apps.py`) passent à `#000000` (deux stops identiques =
  aplat noir) ; les deux paires **doivent rester synchronisées**.
  `_default_phone_wallpaper_path()` (`circusvoip_client.py`) ne cherche plus
  `hugolisoir.png` et retourne toujours `None`. `build_update.py` n'ajoute
  plus `hugolisoir.png` de force à `RELEASE_FILES`.
- **Compteur de joueurs** : label `lbl_online_count` branché sur
  `_refresh_no_other_players_label()`, déjà appelé sur join / leave / reset
  (welcome) / connexion / déconnexion. Compte `len(state.players) + 1`.
  100 % client-side : le serveur envoyait déjà tout le nécessaire.
- **Zones OCR** (`circusvoip_sc_ocr.py`) : ajout de la famille
  `hangar_{xl,large,medium,small}{top,front}_orison` à
  `_KNOWN_ZONES_INTERIORS`. Cause racine : `hangar_largetop_orison` était
  absent de la whitelist, donc le fuzzy matcher n'avait aucune cible et
  laissait passer les variantes brutes. Vérifié : les 9 graphies observées
  canonicalisent (distance 1-3 ≤ seuil 3 pour 22 caractères ; l'équivalence
  `n↔r` existante rattrape `hardar`→`hangar`), et aucune collision sur les
  784 zones connues.
- **Bouton MAJ** : les deux `addWidget` du groupe « Mise a jour » sont
  commentés. Le `QPushButton` et le `QGroupBox` restent **instanciés** —
  15 endroits du code référencent `self.btn_check_update`
  (`_set_update_button_style()`, `_on_update_available()`…), les supprimer
  lèverait une `AttributeError`. Décommenter pour un build de dev.

### Correctif serveur (déployé séparément le 22/07/2026)

- `circusvoip_audio_server.py` : les connexions non-WebSocket sur le port
  8889 (scans de ports, sondes) faisaient cracher un traceback complet
  (`opening handshake failed` / `InvalidUpgrade: missing Connection header`).
  Bénin mais polluant — repéré 3× dans l'analyse de la session du 20/07.
  Ajout d'un filtre de log (`_WsHandshakeNoiseFilter`, import défensif
  d'`InvalidHandshake`) qui réduit ces rejets à une ligne `WARNING`, sans
  toucher à la logique de connexion ; les vraies erreurs conservent leur
  traceback. **Déjà en production**, hors paquet client.

---

### Fichiers modifiés

**Client (distribués par la mise à jour)**
`circusvoip_client.py` · `circusvoip_core.py` · `circusvoip_sc_ocr.py` ·
`circusvoip_phone_settings.py` · `circusvoip_phone_apps.py`

**Outil de build (non distribué)**
`build_update.py`

**Serveur (VPS, déjà déployé)**
`circusvoip_audio_server.py`

**Assets** : suppression de `hugolisoir.png`, `balok.*` et `ignis.*` du
dossier `circusvoip_phone_wallpapers\`.

---

## Tests à faire avant de marquer « publié »

1. **Raccourcis** : sur une config sans raccourcis téléphone, au démarrage,
   Paramètres affiche F6→F10 et le journal indique
   `[CONFIG] Raccourcis CircusPhone par défaut appliqués`. F6 ouvre/ferme le
   téléphone. Sur une config où un raccourci était déjà personnalisé, celui-ci
   est conservé.
2. **Effacement volontaire** : effacer un raccourci, relancer → il reste
   effacé (le flag empêche la migration de se rejouer).
3. **Fond d'écran** : premier lancement sans fond choisi → **fond noir**.
   La grille Paramètres ne propose plus hugolisoir / balok / ignis.
4. **Compteur** : à 2 joueurs connectés, le panneau affiche « Joueurs en
   ligne : 2 » ; une déconnexion le fait passer à 1 ; hors connexion, « — ».
5. **Zones VOIP** (test réseau) : deux joueurs dans le hangar large top
   d'Orison s'entendent en proximité sans coupure ; vérifier dans les logs
   `[POS]` du VPS l'absence de variantes (`hangar_larcetop_orison`,
   `hardar_*`…).
6. **Bouton MAJ** : le groupe « Mise a jour » n'apparaît plus dans
   Paramètres, et le client démarre sans erreur (contrôle de non-régression
   de l'instanciation).
7. **Installeur** : le Setup produit un client complet — **toutes** les apps
   du CircusPhone présentes, skins et fonds d'écran disponibles (cf. pièges
   §5.4 / §5.8 de `CIRCUSVOIP_PROJET.md`).

## Reste à faire (dette)

- GitHub `dev` : commit du build 62 (+ ce changelog), puis merge `main` +
  tag `client-v0.3.0` pour la release stable.
- Marquer les changelogs **59, 60 et 61** « publié » (toujours en attente).
- Annonce aux joueurs une fois les tests validés — **mentionner les
  raccourcis F6-F10** (touches non bloquées, à réassigner en cas de conflit
  avec les binds Star Citizen).
