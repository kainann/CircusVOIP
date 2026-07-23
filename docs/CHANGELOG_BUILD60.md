# CircusVOIP — Changelog build 60

> **Version** : 0.3.x alpha build 60
> **Statut** : déployé le 16/07/2026 — *changelog rédigé a posteriori le 17/07/2026*
> **Type** : mise à jour 100 % client (rien à déployer sur le VPS de jeu)

---

## Nouveautés joueur

### CircusPhone — rattrapage de l'historique via les anciens logs
- Les app **Blueprints** et **Portefeuille** lisent désormais les anciens
  `Game.log` que Star Citizen archive automatiquement à chaque lancement
  (dossier `LIVE\logbackups\`). Cela permet de retrouver ce qui a été reçu
  lors de sessions jouées **avant** d'avoir installé CircusVOIP, ou pendant
  lesquelles il n'était pas ouvert.
  - **Blueprints** : tous les blueprints débloqués depuis la mise à jour
    4.7 (version qui a introduit les blueprints).
  - **Portefeuille** : les transactions depuis le wipe de la 4.8 (l'argent
    antérieur au wipe n'existant plus, il n'est pas remonté).
- Seules les sessions **LIVE** sont prises en compte (les logs PTU/EPTU,
  qui correspondent à des données de test wipées, sont ignorés).
- Le scan tourne une fois au démarrage de l'app, dans un thread dédié pour
  ne pas figer l'interface, et n'est pas refait inutilement (les fichiers
  déjà lus ne sont pas relus).

---

## Détails techniques (dev)

- **Scanner générique** `LogBackupsScanThread` dans
  `circusvoip_phone_wallet.py` (module d'infra Game.log partagée, aux côtés
  de `find_gamelog` / `GameLogRawTailThread`). Paramétré par une *parser
  factory* (un parser neuf par fichier — nécessaire pour les automates
  multi-lignes comme celui du Portefeuille) et un *seuil de version*.
- **Filtres par fichier** (en-tête, ~100 premières lignes) :
  - Channel : `[Trace] Environment: PUB` (= LIVE ; repli sur `\LIVE\` dans
    la ligne `Executable:` si absent). PTU/EPTU rejetés.
  - Version : `Branch: sc-alpha-X.Y` avec (X, Y) ≥ seuil.
- **Seuils** : Blueprints `(4, 7)`, Portefeuille `(4, 8)` (constantes
  `LOGBACKUPS_MIN_VERSION` et `WALLET_LOGBACKUPS_MIN_VERSION`, à remonter
  si un futur wipe efface les données concernées).
- **Incrémental** : clé `logbackups_scanned` (`{nom: taille}`) dans le JSON
  de chaque app ; les fichiers déjà traités (y compris ceux rejetés par
  filtre) ne sont pas relus. Dédoublonnage via les clés `_seen` existantes,
  une seule sauvegarde JSON en fin de scan.
- **UI** : progression pendant le scan, résumé « X retrouvés dans Y
  sessions » à la fin.

Fichiers modifiés : `circusvoip_phone_wallet.py` (+ scanner générique),
`circusvoip_phone_blueprints.py` (import du scanner + intégration).
Couplage : blueprints importe le scanner depuis wallet → les deux vont
ensemble.

---

## Limites connues (corrigées au build 61)
- Le scan n'était lancé qu'au **démarrage** de l'app : une session jouée
  sans CircusVOIP pendant que le client restait ouvert n'était rattrapée
  qu'au redémarrage. → re-scan à l'ouverture ajouté au build 61.
- L'import du scanner dans Blueprints n'était pas défensif : un
  désalignement wallet/blueprints aurait pu affecter le tail. → import en
  deux temps ajouté au build 61.

## Reste à faire (dette, au moment du déploiement)
- GitHub `dev` : commit du build 60 (+ ce changelog).
- Marquer le changelog 59 « publié » (resté « préparé, non publié »).
- Annonce aux joueurs.
