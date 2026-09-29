# CircusVOIP — 0.4.0 Alpha Build 75

**Statut** : **publié le 27/08/2026** — serveur et client.
**Type** : mise à jour **client + serveur**. Le serveur doit être redéployé, et
**avant** le client.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_server.py` | VPS | `b234c25dd029363a68228a65f89a4d0a` |
| `circusvoip_client.py` | build | `a0daa7db35e36ee2629b2f342b7fac4d` |
| `circusvoip_sc_ocr.py` | build | `2c0004df23f2ae911d937324ceea15c9` |
| `circusvoip_phone_annuaire.py` | build | `58aa8b635a68e297c5df70ef017f6b37` |
| `circusvoip_core.py` | build | `174a7279d989e3313efa3a9e6bdea135` *(inchangé depuis b73)* |

> **Serveur déployé le 26/08, client publié le 27/08.** Mise à jour vérifiée sur
> un poste réel (`I:\CircusVOIP\Client\app`) : `version.json` en 75 et les
> quatre empreintes du tableau conformes après téléchargement.
>
> **Republication sans incrément.** Un premier build 75 a été poussé le 26/08,
> avant le correctif de l'atténuation verticale. Personne ne l'ayant
> téléchargé, il est republié sous le **même numéro**. Si un client l'avait
> déjà pris, il ne recevrait jamais le correctif : l'updater compare
> `remote.build > local.build` strictement (`circusvoip_client.py` l.1364).

---

## Atténuation verticale — la voix passait entre les ponts

**Symptôme** : dans un vaisseau, deux joueurs sur des ponts différents
s'entendaient par intermittence — volume à **0 % puis 100 % pendant quelques
millisecondes**, plusieurs fois par seconde, et **des deux côtés** de la
conversation.

**Cause** : quatre endroits écrivaient `state.players[name]["dist"]`, la valeur
qui décide du volume audio.

Le cœur y écrit la distance **pondérée** — l'écart vertical multiplié par 10
dans un vaisseau, soit 47,5 m pour un écart de 4,7 m. Mais deux fonctions
d'**affichage**, celles qui remplissent la carte « 12 m » d'un joueur,
recalculaient la distance **sans pondération** et écrasaient la valeur par
7,5 m.

Les deux boucles tournant en parallèle, elles se marchaient dessus plusieurs
fois par seconde. La boucle audio appliquait ce qu'elle trouvait au moment où
elle passait.

**Correctif** : l'affichage n'écrit plus dans `dist`. Il consomme cette valeur,
il ne la produit pas — elle appartient au cœur, seul à appliquer la
pondération.

> **Ironie du commentaire d'origine** : le code d'affichage était introduit avec
> la justification « doit être cohérent avec l'audio, sinon la card affiche
> 100 m alors que le user n'entend rien ». C'est précisément lui qui cassait la
> cohérence, dans l'autre sens.

### Comment il a été trouvé

Deux journées d'analyse n'y étaient pas parvenues : le journal s'arrêtait à la
position et au poids vertical, sans jamais montrer le **résultat**. Diagnostiquer
revenait à deviner.

Une ligne de journal temporaire, ajoutée le 25/08, a tranché en un test :

```
18:25:31.378  volume=0.81  dist_ponderee=7.5m   dz=4.71m  poids=x10
18:25:31.895  volume=0.00  dist_ponderee=47.5m  dz=4.71m  poids=x10
```

Même écart vertical, même poids annoncé, deux distances incompatibles — donc
deux calculs concurrents. **Cette ligne a été retirée** une fois le bug corrigé.

### Ce qui n'était PAS en cause

Écarté par les mesures, après avoir été suspecté :

- **la lecture OCR de la position** — `z` était stable à ±15 cm ;
- **la configuration des joueurs** — tous sur la même version, case cochée ;
- **le monte-charge** du vaisseau (container distinct) ;
- **la garde anti-saut** de position ;
- **le seuil de la courbe de volume**.

---

## Remontée des journaux de débogage

Un joueur envoie son journal au serveur **à la fermeture du client**.

**Pourquoi** : la proximité se calcule des **deux côtés**. Le journal d'un
joueur ne dit rien du volume entendu par l'autre. Sans les deux, une fuite ou
une coupure audio ne se diagnostique pas — constat fait sur le bug ci-dessus.

**Comment** : le journal est compressé (gzip, ~13 %) et transmis en base64 dans
une trame `debug_log` sur la WebSocket **déjà ouverte**. Pas de nouveau port,
pas de serveur HTTP, et l'envoi est authentifié — on sait qui envoie.

Mesures réelles : 12 min de jeu = 200 Ko brut → 34 Ko transmis ; 3 h = 2200 Ko
→ 376 Ko. La limite d'une trame WebSocket est d'1 Mo.

**Troncature à 4 Mo bruts**, côté client. Au-delà, la **fin** du journal est
conservée — un problème se diagnostique à partir de ce qui vient de se passer —
avec un en-tête indiquant ce qui a été retiré. Sans cette troncature, une
session de plus de 8 h aurait échoué en silence, précisément la plus
intéressante à analyser.

**Destination** : `/home/circusvoip/logs_joueurs/` sur le VPS, en `chmod 600`,
nommé `<pseudo>_<horodatage>_<fichier>.gz`.

### Vie privée

Ces journaux contiennent les **positions en jeu** du joueur, les zones
traversées et les pseudos croisés.

Une case **« Envoyer le journal de débogage à la fermeture (alpha) »** est
ajoutée aux Réglages, cochée par défaut, avec une infobulle qui dit exactement
ce qui part — et ce qui ne part **jamais** : ni messages, ni contacts, ni voix.

### Gardes côté serveur

- **Nom de fichier neutralisé** — il vient du client et ne doit jamais servir
  tel quel à construire un chemin.
- **Plafond de 800 Ko** compressés, soit plus de 8 h de session.
- **Vérification gzip avant écriture** — un faux `.gz` ne se découvrirait que
  le jour où on en a besoin.
- **Un envoi par connexion**, et l'ensemble de suivi est purgé à la
  déconnexion : sans ça, une entrée par connexion s'accumulerait pour toute la
  durée de vie du serveur.
- **Collision à la seconde** — un suffixe évite qu'une reconnexion rapide
  écrase le fichier précédent.

---

## Autres correctifs

### Coordonnées décalées par une lecture OCR

`52m` était parfois lu `5 Om` : le chiffre devient un `O` **et** une espace
s'insère, ce qui coupe le nombre. L'analyseur ne trouvait plus que deux valeurs
au lieu de trois et les décalait — `z` passait de `-113` à `5`.

L'erreur étant **systématique** — même confusion, même faux résultat — la garde
anti-saut, qui suppose une erreur aléatoire, finissait par converger dessus
après trois lectures identiques.

La correction existante ne couvrait que le `O` **collé** à un chiffre. Le cas
avec espace est ajouté, avec une garde stricte : un chiffre doit précéder, donc
les noms de zone comme `Orison` ne sont jamais touchés.

**Vérifié sur les 1427 lectures d'un journal réel : 16 réparées, aucune
abîmée.**

### Noms des auteurs dans les conversations de groupe

Les noms s'affichaient correctement à l'ouverture d'une conversation de groupe,
puis redevenaient des numéros dès qu'un message arrivait.

**Quatre** endroits construisent les bulles d'une conversation ; la substitution
du nom n'était branchée que sur **deux** — l'ouverture et un rafraîchissement.
Les deux manquants étaient ceux du rafraîchissement après envoi et à la
réception.

Un contrôle automatique vérifie désormais que tous les appels passent par la
substitution.

### Écran Contacts vide : rien n'était sélectionné

Sur une liste de contacts vide, le curseur pointait sur le premier contact —
inexistant. Rien ne s'allumait, et Entrée ne faisait rien : il fallait deviner
qu'il faut appuyer sur **haut** pour atteindre les onglets. Précisément l'écran
où le joueur n'a encore rien et doit aller vers « Ajouter ».

La liste vide se pose maintenant sur la barre d'onglets. **Même correctif pour
l'historique des appels**, qui avait le même défaut.

Au passage : `_numeros_hist` conservait les numéros de l'historique précédent
alors que `_lignes_hist` était vidé — de quoi appeler un mauvais numéro après un
changement d'historique.

---

## Ordre de déploiement

> Une seule ligne par commande sous `cmd`. Le `\` de continuation est une
> syntaxe de shell Unix.

**1. Le serveur.** *(fait le 26/08)*

```cmd
scp "D:\Projet CircusVOIP\fichiers serveurs\circusvoip_server.py" root@178.104.207.46:/home/circusvoip/app/
```

```cmd
ssh root@178.104.207.46 "chown circusvoip:circusvoip /home/circusvoip/app/*.py && systemctl restart circusvoip-server.service"
```

**Créer le dossier de réception**, sans lequel rien n'est écrit :

```cmd
ssh root@178.104.207.46 "mkdir -p /home/circusvoip/logs_joueurs && chown circusvoip:circusvoip /home/circusvoip/logs_joueurs && chmod 700 /home/circusvoip/logs_joueurs"
```

**2. Le client.** *(fait le 27/08)* Vérifier les empreintes du tableau **avant**
de builder.

```cmd
py -3 build_update.py --notes "Build 75 - remontee des journaux, correctif attenuation verticale"
```

⚠ **Pas de `--bump-build`** : republication sous le même numéro.

**3. Pousser la release** *(fait le 27/08)* — c'est l'étape oubliée au premier
essai :

```cmd
scp -r "D:\Projet CircusVOIP\updates" root@178.104.207.46:/home/circusvoip/
```

**Contrôles :**

```cmd
ssh root@178.104.207.46 "grep -o '\"build\"[^,]*' /home/circusvoip/updates/manifest.json"
```

```cmd
ssh root@178.104.207.46 "md5sum /home/circusvoip/updates/files/circusvoip_client.py /home/circusvoip/updates/files/circusvoip_sc_ocr.py"
```

`75`, puis les deux empreintes du tableau.

---

## Ce que ce build ne corrige pas

- **Le seuil de coupure reste serré.** Un étage fait ~3 m, et 3 × 10 = 30 m,
  soit exactement la portée audible. À 2,6 m d'écart, le volume vaut encore
  1 % — inaudible, mais non nul. Un vaisseau aux ponts plus rapprochés
  laisserait passer un filet de son.
- **`dist = 0.0` à la réception d'une position** quand la sienne n'est pas
  encore connue, soit **volume plein** un court instant à la connexion d'un
  joueur (`circusvoip_client.py` l.3056). Écrasé aussitôt par le cœur, mais
  jamais examiné.
- **Rien ne purge `/home/circusvoip/logs_joueurs/`.**
- **La case d'atténuation verticale est toujours désactivée par défaut**, et
  chaque client calcule son propre volume : deux joueurs réglés différemment ne
  s'entendent pas de la même façon. Le passage en stable prévoit de forcer
  l'état actif (§5.7 détail #7).
- **`pip_packages/nvidia_ml_py-*.whl` introuvable** au build : les statistiques
  GPU manqueront à qui n'a pas déjà `pynvml`. Antérieur à ce build.
- **Le dépôt GitHub est en retard** d'un build.
