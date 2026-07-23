# CircusVOIP — Changelog build 61

> **Version** : 0.3.x alpha build 61
> **Statut** : préparé — *non publié* (à marquer « publié le JJ/MM » une fois les tests OK)
> **Type** : mise à jour 100 % client (rien à déployer sur le VPS de jeu)

---

## Nouveautés joueur

### CircusPhone — app Blueprints : recettes de fabrication
- Chaque blueprint débloqué est maintenant **cliquable** dans la liste.
  À la sélection, une page plein écran affiche la **recette** : nom du
  blueprint en grand, **temps de fabrication**, puis la liste des
  **matériaux avec leurs quantités** (en SCU ou à l'unité).
- Navigation D-pad : haut/bas déplacent le curseur sur les blueprints,
  Entrée ouvre la recette, Retour revient à la liste.
- Les données viennent d'un fichier `blueprints_materials.json` mis à jour
  à chaque patch de Star Citizen. Un blueprint absent des données affiche
  « Recette introuvable » plutôt qu'une information erronée.

### CircusPhone — rattrapage de l'historique via les anciens logs
- Les app **Blueprints** et **Portefeuille** relisent désormais les
  anciens `Game.log` archivés par Star Citizen (dossier `LIVE\logbackups\`)
  pour retrouver ce qui a été reçu lors de sessions jouées **sans**
  CircusVOIP ouvert.
  - Blueprints : tout ce qui a été débloqué depuis la 4.7.
  - Portefeuille : les transactions depuis le wipe 4.8.
- Le scan est relancé à chaque ouverture de l'app (une session jouée sans
  CircusVOIP est rattrapée sans avoir à redémarrer le client).

### CircusPhone — icônes et affichage
- Les icônes de l'écran d'accueil ne dépendent plus de la police système :
  elles sont **identiques sur tous les PC** (fini les icônes différentes
  ou les carrés vides selon Windows 10 / 11).
- Icône **Appels** : combiné bleu, plus lisible. Icône **Poker** : pique
  blanc.
- **Badge de notification** (pastille rouge) sur l'icône Messagerie de
  l'écran d'accueil tant qu'il reste au moins un message non lu.

### CircusPhone — navigation Messagerie
- Dans une conversation, depuis le champ de saisie, **flèche haut** amène
  désormais sur le **dernier message** reçu (puis remonte les messages),
  au lieu de tomber sur le bouton Retour.
- Correction du curseur de liste (Blueprints et contacts) : le premier
  appui sur une flèche **révèle** la sélection sur la première ligne au
  lieu de sauter la première.

### VOIP — précision des zones (proximité)
- Reconnaissance de nouvelles zones : **Kruger L-21 Wolf**, **GLSN Basher**,
  **Drake Ironclad**, vaisseaux de mission (Cutlass / Caterpillar
  d'abordage), ascenseur arrière du **RSI Polaris**, Origin **125a**,
  Teach's Ship Shop, et boutique de vaisseaux.
- L'audio de proximité est plus stable : les lectures OCR bruitées d'un
  nom de zone ne provoquent plus de micro-coupures avec les joueurs du
  même vaisseau (mécanisme de « zone collante » + corrections de lecture).

---

## Détails techniques (dev)

- **Scan `logbackups\`** : thread générique `LogBackupsScanThread` dans
  `circusvoip_phone_wallet.py` (infra Game.log partagée), consommé par
  Blueprints (seuil 4.7) et Portefeuille (seuil 4.8) via une *parser
  factory*. Filtres d'en-tête : channel `Environment: PUB` (= LIVE, PTU
  rejeté) et `Branch: sc-alpha-X.Y` ≥ seuil. Incrémental (clé
  `logbackups_scanned`, nom+taille). Re-scan branché sur `on_show()`.
- **Recettes** : `blueprints_materials.json` chargé à côté du module
  (cache sur mtime). Matching par `produced_display` (exact normalisé puis
  sous-chaîne, repli clé technique / `produced_item`). Noms de matériaux
  via le champ `display` du fichier, table FR pour les métaux communs.
- **Zones OCR** (`circusvoip_sc_ocr.py`) : 8 entrées whitelist ajoutées,
  équivalence OCR `m↔n`, correctif du strip des ID d'instance coupés par
  underscore, fonction `_is_sticky_zone_variant` (seuil 6). Côté
  `circusvoip_core.py` : branche `[ZONE COLLANTE]` dans le bloc
  `[CID SIMILAIRE]` (mêmes gardes de position <5 m).
  Validé sur l'intégralité du log positions du VPS (49 418 positions,
  9 jours) : 99,99 % des lectures normalisées.
- **Icônes** : SVG Twemoji embarqués en dur (aucun fichier image), rendus
  via QtSvg. Fabrique `make_phone_icon` + descripteur `LazyPhoneIcon`
  dans `circusvoip_phone_apps.py` ; repli sur l'ancien glyphe si QtSvg
  indisponible.
- **Badge Messagerie** : `PhoneHome.set_badges()` + relais
  `set_home_msg_badge()`, branché sur `_phone_refresh_overlay_contacts`
  (source unique de vérité de l'état non-lu).
- **Build** : `blueprints_materials.json` ajouté à `RELEASE_FILES` dans
  `build_update.py` (sinon les recettes seraient vides chez le joueur).

Fichiers modifiés : `circusvoip_client.py`, `circusvoip_core.py`,
`circusvoip_sc_ocr.py`, `circusvoip_phone_apps.py`,
`circusvoip_phone_wallet.py`, `circusvoip_phone_blueprints.py`,
`blueprints_materials.json` (nouveau), `build_update.py`.
Couplages : wallet ↔ blueprints (import du scanner), sc_ocr ↔ core
(import de `_is_sticky_zone_variant`).

---

## Tests à faire avant de marquer « publié »

1. **Mise à jour** : un client passe bien en build 61 via l'updater, et
   `blueprints_materials.json` est présent dans le `manifest.json` poussé.
2. **Recettes** : ouvrir l'app Blueprints, sélectionner un blueprint connu
   (ex. R97 « Righteous » Shotgun) → nom, temps et matériaux s'affichent.
   Un blueprint absent des données → message « Recette introuvable ».
3. **Rattrapage logbackups** : après une session jouée sans CircusVOIP,
   rouvrir Blueprints/Portefeuille → les éléments de la session
   apparaissent (compteur « X retrouvés dans Y sessions »).
4. **Icônes & badge** : icônes identiques (Appels bleu, Poker pique
   blanc) ; recevoir un MP téléphone fermé → pastille rouge sur l'icône
   Messagerie, qui disparaît une fois la conversation ouverte.
5. **Navigation** : en conversation, flèche haut depuis le champ →
   dernier message. En liste Blueprints, premier appui bas → 1re ligne.
6. **Zones VOIP** (test réseau) : deux joueurs dans le même Wolf / Cutlass
   de mission s'entendent sans coupure ; vérifier dans les logs `[POS]`
   du VPS l'absence de variantes non normalisées (`kric_*`, `cuflass`…).

## Reste à faire (dette)
- GitHub `dev` : commit du build 61 (+ changelog).
- Marquer les changelogs 59 et 60 « publié » (toujours en attente).
- Annonce aux joueurs une fois les tests validés.
