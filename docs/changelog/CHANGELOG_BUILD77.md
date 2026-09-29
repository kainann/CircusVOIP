# CircusVOIP — 0.4.0 Alpha Build 77

**Statut** : **publié le 03/09/2026** — serveur et client.
**Type** : mise à jour **client + serveur**. Le serveur a été déployé le 03/09,
**avant** le client.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_phone_urgence.py` | VPS **et** build | `c9ebe040a40ce930d8a2acd5c1309822` |
| `circusvoip_sc_ocr.py` | build | `de1771d08e9d9eabc43736bcd0785e5f` |
| `circusvoip_phone_apps.py` | build | `a1127c9887c8b363bd81eb69335eb5e9` |
| `circusvoip_phone_urgence_app.py` | build | `e2bfe3a810f30582cd67b38afd8839cb` |
| `circusvoip_phone_travail_app.py` | build | `c5c628c36712d037423c4a3216e480f1` |
| `circusvoip_phone_annuaire.py` | build | `e9f863453d3ddd25d2ca93203dbe67bc` |

> **`circusvoip_phone_urgence.py` est partagé.** Le serveur l'importe
> (`circusvoip_server.py`, `circusvoip_urgence_store.py`) mais n'appelle rien
> de ce qui change ici — il valide et route, c'est le client qui calcule. Le
> déposer côté serveur n'était donc pas fonctionnellement nécessaire ; il l'a
> été quand même, pour ne pas laisser deux versions d'un même module tourner
> en parallèle.

> **Le test de groupe du 31/08 n'a rien validé du build 76.** Un seul journal
> couvre la fenêtre 21:32 → 22:27, et c'est un client resté en **build 75** :
> la mise à jour proposée au démarrage n'a pas été appliquée, et rien ne
> l'applique automatiquement (point 31 de la TODO). Aucune ligne `[GROUPES]`,
> `[URGENCE]`, `[TRAVAIL]` ni `[PHONE]` dedans. Les trois autres joueurs n'ont
> rien remonté : la remontée ne part qu'à la fermeture propre du client, et un
> processus tué n'envoie rien. **Vérifier les builds avant de lancer un test**,
> et fermer par la croix.

---

## Identifiant du système stellaire — « Autre système » avant toute distance

**Symptôme** : un secouriste dans Pyro, une victime sur un point d'intérêt de
Nyx. L'écran de suivi affichait « Pas de distance — rejoignez la zone
indiquée », et le secouriste pouvait parcourir un système entier avant de
comprendre qu'il fallait d'abord en changer.

**Cause** : le HUD nomme le niveau système avec son identifiant d'instance —
`Zone: SolarSystem_783323188128 Pos: ...` sur le relevé du 28/08 — et chaque
système stellaire porte le sien. L'OCR le lit correctement (l'underscore
devient une espace, `_PAT_FIRST_CONTAINER` accepte les deux depuis le correctif
`[SEP_ID]` du 26/07), `_hier_lire_une_fois` le range dans `container_id`… et
`position_depuis_hierarchie` le jetait en ne recopiant que x/y/z. La donnée
traversait tout le pipeline et disparaissait à la frontière du module urgence.

Sans lui, `distance_detail` comparait des coordonnées `SolarSystem` de deux
systèmes différents — c'est-à-dire de deux repères différents — et pouvait
rendre un petit nombre. Le seuil de 1e11 de `_proximite()` supposait un repère
universel : il ne protégeait de rien.

**Correctif** :

- `position_depuis_hierarchie` transporte `cid` du niveau système, et **ne
  jette plus le bloc quand seules les coordonnées manquent**. C'est le cas de
  Skywat — le mot `SolarSystem` est lu, seuls les nombres échouent — et c'est
  précisément là que l'identifiant sert le plus.
- `meme_systeme(a, b)` compare les deux identifiants : vrai, faux, ou `None`
  quand l'un manque. Dans ce dernier cas on ne sait pas, et on le dit.
- `distance_detail` coupe **avant tout calcul** quand ils diffèrent, et rend
  `autre_systeme=True`. La liste affiche « Autre système » dès l'identifiant,
  sans passer par les coordonnées. L'écran de suivi dit « La victime n'est pas
  dans votre système stellaire. Rejoignez le sien avant de chercher. » au lieu
  d'inviter à parcourir la zone.
- Le compte rendu de balise montre `cid=` sur sa ligne `SolarSystem`.

Rejoué sur le texte OCR exact du relevé : l'identifiant `783323188128`
traverse jusqu'à la charge utile. Deux systèmes à coordonnées volontairement
proches → « autre système », aucune distance. Sans identifiant d'un côté,
rien n'est affirmé et la distance se calcule comme avant. Le mannequin
(`system: None`) continue de fonctionner.

> **Hypothèse de déploiement.** L'identifiant est celui d'une instance : il
> change avec la shard. Les joueurs partagent la même, donc une différence
> signifie bien un autre système. Si cette hypothèse cessait d'être vraie, le
> symptôme serait « Autre système » entre deux joueurs côte à côte — et la
> shard, déjà extraite par l'OCR (`_PAT_SHARD`, `server_id`), n'est consommée
> nulle part. Ce serait le premier endroit où regarder.

### Pyro IV et Pyro V n'étaient pas deux endroits

Trouvé en écrivant les tests du correctif précédent. `_memes_containers`
délègue à `are_containers_similar`, qui tolère deux caractères d'écart pour
absorber les fautes OCR — `l` lu `1`, `o` lu `0`, une lettre manquante. Mais
`pyro4` et `pyro5` diffèrent d'un seul caractère : les deux planètes étaient
considérées comme le **même container**, et l'écran affichait « Sur place »
avec une distance marquée **fiable**. Même chose pour deux grottes `_001` /
`_002`, ou deux stations Lagrange `p3l1` / `p2l4`.

**Correctif** : `_chiffres_divergents`. Deux clés `name:` de même longueur qui
diffèrent à une position où **les deux caractères sont des chiffres** désignent
deux lieux. Un chiffre contre un chiffre n'est pas une faute de lecture, c'est
le nom. Une lettre contre un chiffre reste tolérée ; une longueur différente
reste à la charge de la tolérance générale ; les cids **numériques** —
identifiants d'instance à 13 chiffres — la gardent entière, car là un chiffre
pour un autre est bien une faute OCR (le cas documenté `3` contre `8`).

Vérifié sur neuf cas : les trois confusions refusées, les trois tolérances
conservées, les trois OCR réelles toujours appariées. Le garde est dans le
module urgence ; `are_containers_similar` n'est pas touchée, elle sert aussi à
l'audio de proximité où la distance locale de 30 m séparait déjà les deux
planètes.

---

## Contrat clavier — le retour arrière n'effaçait pas

Deux défauts distincts, même symptôme : dans un champ de saisie, Retour arrière
sortait de l'écran au lieu d'effacer. Règle écrite dans `CIRCUSVOIP_PROJET.md`
§ 5 ter, « Contrat clavier des apps à champ de saisie ».

**Le mécanisme.** Le téléphone est un overlay `Qt.Tool` par-dessus Star
Citizen : les touches lui arrivent par un hook clavier global, pas par Qt.
Échap ayant été abandonnée — elle ouvrait le menu Options de SC — c'est Retour
arrière qui sert de « retour ». Le hook ne rend la main au champ que si l'app
le lui dit, via `dans_champ()` et `champ_courant_vide()` lus par `getattr`. Et
sous Windows, un filtre win32 **supprime la touche au niveau du système** dès
que l'app répond faux : le widget ne la reçoit jamais.

### App Urgence — le contrat n'était pas exposé

`phone_urgence_app` avait toute la machinerie interne (`_dans_champ`,
`_champ_vide()`, `_sortir_du_champ()`) mais n'a jamais exposé les deux
accesseurs publics. L'app était silencieusement traitée comme « jamais dans un
champ » : impossible d'effacer un caractère dans la description ou le numéro
d'une demande, flèches volées au curseur par le D-pad, sortie de champ
inopinée. Le commentaire daté du 13/08 dans `handle_back` affirmait que la
touche arrivait « EN PLUS d'être reçue par le champ » : c'était faux et
impossible, toute la logique avait été écrite contre un comportement qui
n'avait jamais eu lieu.

**Correctif** : `dans_champ()` et `champ_courant_vide()` ajoutées, relayant
l'existant.

### Trois apps — cliquer dans un champ n'armait pas le drapeau

Dans Urgence, Travail et les trois écrans de l'Annuaire, le drapeau « dans un
champ » n'était mis à vrai que par le chemin D-pad, `handle_nav("enter")` →
`_entrer_dans_champ()`. Un **clic souris** donne le focus à Qt sans passer par
là. Le défaut paraissait intermittent : au clavier ça marchait, à la souris
jamais. Aucun `focusInEvent` dans tout le projet.

Signalé en direct sur le formulaire de création de mission : le joueur clique
dans Titre, tape `TR`, corrige au retour arrière — `handle_back()` remet
`_creation` à faux, le formulaire se referme, et comme `_peindre_formulaire()`
reconstruit les champs vides à chaque rafraîchissement, tout est perdu.

**Correctif** : un `eventFilter` par app fait suivre le drapeau aux
`FocusIn` / `FocusOut` réels. Là où `champ_courant_vide()` lit le champ par
`_cible` (Travail, Contacts), la cible est réalignée sur le champ qui prend le
focus — sinon on interrogerait le mauvais pour décider si Retour arrière efface
ou ressort, et le halo resterait ailleurs. Dans la Messagerie, la liste
déroulante ouverte prend le focus à sa manière ; `dans_champ()` la couvre déjà
par `_liste_ouverte`, un `FocusOut` du champ pendant qu'elle est ouverte ne
change pas le verdict.

> Dans Appels, `_ed_num` n'est rendu visible que par `_entrer_dans_champ` : un
> clic ne peut pas l'atteindre et le filtre y est inerte. Posé quand même, pour
> le jour où le champ deviendra visible par défaut.

---

## OCR — famille des bunkers

Le 31/08, dans un seul bunker : **7 containers distincts pour 26 lectures**.
`bunker_XXX_cave_int_XXX` est désormais reconstruit par motif — les **mots**
sont réparés, jamais les chiffres. `018` au lieu de `013` est un numéro
plausible ; le corriger déplacerait le joueur dans un autre lieu réel. Le bruit
sur les chiffres est laissé à la zone collante.

Mesuré sur la session : 7 containers ramenés à 5. Aucune régression sur le
corpus témoin.

---

## Divers

### Trois icônes manquantes

Contacts, Travail et Urgence étaient rendues par la police système au lieu
d'être embarquées. SVG Twemoji officiels récupérés sur `jdecked/twemoji`.

### Le joueur se voit dans la liste des secouristes en service

Son pseudo (`services.my_name`) en tête et en gras. Ajout purement à
l'affichage : le serveur continue de l'exclure de `collegues`, cette liste
servant aussi à décider qui prévenir.

### Compte rendu de balise

La ligne `SolarSystem` montre désormais `cid=` avant les coordonnées.

---

## Ordre de déploiement

> Une seule ligne par commande sous `cmd`.

**1. Le serveur.** *(fait le 03/09)*

```cmd
scp "D:\Projet CircusVOIP\fichiers serveurs\circusvoip_phone_urgence.py" root@178.104.207.46:/home/circusvoip/app/
```

```cmd
ssh root@178.104.207.46 "chown circusvoip:circusvoip /home/circusvoip/app/*.py && systemctl restart circusvoip-server.service"
```

**Contrôler**, pas seulement redémarrer :

```cmd
ssh root@178.104.207.46 "md5sum /home/circusvoip/app/circusvoip_phone_urgence.py"
ssh root@178.104.207.46 "systemctl is-active circusvoip-server.service; journalctl -u circusvoip-server.service -n 30 --no-pager | grep -iE 'DESACTIVE|Traceback'"
```

`c9ebe040…`, `active`, et rien d'autre.

**2. Le client.** *(fait le 03/09)* Les six empreintes locales vérifiées au
`certutil` **avant** de builder.

```cmd
py -3 build_update.py --bump-build --notes "Build 77 - bunkers, icones, contrat clavier, identifiant systeme, Pyro IV/V"
```

**3. Pousser la release** :

```cmd
scp -r "D:\Projet CircusVOIP\updates" root@178.104.207.46:/home/circusvoip/
```

**Contrôles :**

```cmd
ssh root@178.104.207.46 "grep -o '\"build\"[^,]*' /home/circusvoip/updates/manifest.json"
```

```cmd
ssh root@178.104.207.46 "cd /home/circusvoip/updates/files && md5sum circusvoip_sc_ocr.py circusvoip_phone_apps.py circusvoip_phone_urgence.py circusvoip_phone_urgence_app.py circusvoip_phone_travail_app.py circusvoip_phone_annuaire.py"
```

`77`, puis les six empreintes du tableau. **Contrôlé conforme le 03/09.**

---

## À tester en priorité

Ce qui n'a jamais tourné en jeu :

1. **Cliquer** à la souris dans un champ — titre de mission, description
   d'urgence, nom de contact — puis corriger au retour arrière. Le caractère
   doit disparaître, l'écran rester. Champ vide + retour arrière doit ressortir
   du champ sans quitter l'écran.
2. Une balise entre deux joueurs dans **deux systèmes différents** : « Autre
   système » dès la liste, et le message explicite sur l'écran de suivi.
3. Une balise entre deux joueurs sur **Pyro IV et Pyro V** : plus de « Sur
   place ».
4. Le compte rendu de balise, ligne `SolarSystem` avec `cid=`.
5. La balise de Skywat — toujours le correctif du 76 le moins validé, et
   l'identifiant système dépend du même écran à 18 px.

---

## Ce que ce build ne corrige pas

- **Le rafraîchissement du formulaire de mission sur poussée serveur.**
  `appliquer_etat()` appelle `_rafraichir()` sans condition, qui reconstruit
  les trois champs vides. Un autre joueur qui publie pendant qu'on saisit
  efface la saisie. Deux conceptions possibles — ne pas rafraîchir formulaire
  ouvert, ou préserver la saisie — **non tranché**.
- **Le corollaire de la règle § 5 ter** — trace au premier passage dans
  `_app_dans_champ()` quand une app à champs n'expose pas le contrat, et classe
  de base `_AppAvecChamps` — est écrit dans la doc et **n'existe pas** dans le
  code. À faire, ou à retirer de la doc.
- **Le seuil de 1e11** de `_proximite()` reste comme repli quand un
  identifiant est illisible. Il n'a jamais été mesuré.
- **Un plantage à l'ouverture du téléphone**, 28/08 22:03, sans trace. Non
  reproduit.
- **Le déclencheur du défaut OCR en Pyro** reste inconnu.
- **Le `1` lu en tiret** dans les grands nombres sur les zones OCR de 18 px
  (Skywat). L'identifiant système contourne le problème pour la question
  « même système ? », pas pour la distance.
- **Auto-update au démarrage** (point 31). Un joueur peut encore jouer une
  session entière sur un build périmé sans le savoir.
- **Réglage de taille du téléphone** (demande joueur, 2K). La formule
  `body_h = max(420, min(760, scr_h × 0,62))` est dupliquée dans 7 fichiers,
  plafonne en pixels logiques, et `phone_settings` est hors grille (300×620).
  Chantier 0.5.0, en deux entrées distinctes : échelle d'abord, mode tablette
  ensuite seulement si l'échelle ne suffit pas.
- **`recuperer_logs_joueurs.ps1`** : un `.gz` resté après une décompression
  ratée s'auto-déclare présent et n'est jamais repris. Correctif d'une ligne,
  non appliqué. Le script n'est pas documenté dans `CIRCUSVOIP_INFRA.md`.
- **La copie projet de `circusvoip_phone_travail.py`** était périmée
  (`65099c43…` contre `077c9096…` publié). Le disque et le VPS sont justes ;
  c'est la copie dans le projet Claude qui est à remplacer.
- **Aucun journal `hugo` remonté depuis le 24/08.** Une des trois conditions
  de `_remonter_journal_debug` bloque en permanence sur le poste de dev :
  `debug_upload`, `DEBUG_OCR`, ou fermeture hors `closeEvent`.
