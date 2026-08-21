# CircusVOIP — 0.4.0 Alpha Build 71

**Statut** : préparé — non publié
**Type** : mise à jour **client + serveur**. Contrairement au build 70, le
serveur doit être redéployé — et **avant** le client.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_server.py` | VPS | `e2473e881b72c28587bb5b2970d3cba1` |
| `circusvoip_accounts.py` | VPS | `925b41cc28647a5d58963241b9cec7dc` |
| `circusvoip_accounts_ws.py` | VPS | `994d2751938163ab931b06b977379b2b` |
| `circusvoip_client.py` | build | `d36c355d38bc9370350ee1827ccb5150` |
| `circusvoip_core.py` | build | `79ca1857c1d7f83b9d474557036fbd4a` |
| `circusvoip_phone_settings.py` | build | `1f70c6d4199cb71f93f2adf8e41ef062` |

> **Ordre imposé : serveur d'abord.** L'inverse donne un client qui attend un
> `account_token` que personne n'émet — sans casse visible, sans rotation, et
> sans que rien ne le signale.

> Aucune purge, aucune perte de données. Les jetons déjà émis restent valides :
> le serveur n'invalide rien, il commence seulement à faire tourner à la
> connexion suivante.
>
> ⚠ Les appels manqués déjà enregistrés à la mauvaise heure ne se répareront
> pas : leur horodatage erroné est écrit sur le disque. Seuls les rejeux à
> venir seront corrects.

---

## L'essentiel

Ce build ferme deux failles et corrige deux bugs, tous trouvés en tirant le fil
d'une question de sécurité plutôt qu'en constatant un symptôme.

Le fil conducteur est le même que celui du build 70 : **une donnée qui existe
en double, ou qui voyage plus loin que nécessaire, finit par être lue au
mauvais endroit.** Ici, un jeton valable à vie, un numéro renvoyé à quelqu'un
qui ne l'avait pas demandé, un horodatage jeté au passage d'un signal.

---

## Sécurité

### 🔑 Le jeton local ne vaut plus à vie

**Le problème.** Le jeton émis à la liaison Discord était valable
indéfiniment, stocké en clair dans la config du client. Qui le lisait pouvait
se connecter en tant que sa victime — pour toujours, et sans jamais toucher à
son compte Discord.

C'est devenu plus gênant depuis que le blocage de compte est le mécanisme de
défense anti-spam principal : un jeton usurpé ne sert plus seulement à se faire
passer pour quelqu'un, il permet de **faire bloquer sa victime**. Le blocage
étant borné à 2 h, la nuisance plafonne — mais elle ne laisse aucune trace du
côté de l'attaquant.

**Correction.** Le serveur émet un jeton neuf à **chaque connexion acceptée**
et le glisse dans le `welcome` ; le client l'écrit immédiatement sur disque.
Un jeton volé ne vaut donc plus que jusqu'à la prochaine connexion de sa
victime, au lieu de valoir à vie.

Rien de visible pour le joueur : aucune interaction, aucun passage par Discord.
La re-liaison reste ce qu'elle était — rare, et réservée aux vrais cas
(installation, changement de machine).

**Tolérance de 5 minutes.** Un client peut recevoir son jeton neuf puis planter
avant de l'écrire. L'ancien reste alors accepté, mais la tolérance meurt au
**premier des deux événements** : délai écoulé, ou première connexion réussie
avec le jeton neuf. En marche normale, l'ancien meurt donc en quelques
secondes, pas en cinq minutes.

Toute connexion acceptée sur l'ancien jeton **écrit une ligne `[ROTATION]`**
dans les logs. Sans elle, la tolérance masquerait un problème d'écriture de
config au lieu de le révéler — un repli silencieux, ce que le corollaire de la
§5 ter interdit.

**Deux cas qui tuent la tolérance immédiatement :**

- une **re-liaison Discord** — ce n'est pas une rotation, c'est une reprise en
  main du compte, souvent *après* un vol supposé. La reconduire laisserait
  valide cinq minutes de plus précisément le jeton qu'on cherche à révoquer ;
- une connexion réussie avec le jeton courant, preuve que le client a écrit.

**Les comptes de service sont hors rotation.** Un mannequin ou un test de
charge n'a pas de session utilisateur à protéger, le vol de son jeton ne donne
l'identité de personne, et ces comptes ne sont créables que si
`allow_service_accounts` est explicitement actif. En face, le coût était réel :
ce sont des outils relancés vingt fois par session, dont le jeton se saisit à
la main. L'exemption est **explicite** plutôt que subie — sans elle, ces
comptes auraient de toute façon été dégradés en silence par le mécanisme de
tolérance, ce qui revenait au même sans être écrit nulle part.

`list_accounts()` filtre désormais `prev_token_hash` au même titre que
`token_hash` : pendant sa fenêtre, c'est un jeton valide, il n'a pas plus à
transiter vers l'admin que l'autre.

### 📵 Fin de l'oracle pseudo → numéro

**Le problème.** N'importe quel joueur pouvait obtenir le numéro de n'importe
qui. Les réponses `phone_call_ringing` et `phone_call_busy` portaient
`_numero_of_name(target)`, qui se replie sur `_pseudo_to_numero()` et
**parcourt l'annuaire**. Combiné au champ `target` en pseudo, le serveur
répondait donc à la question « quel est le numéro de ce pseudo ? ».

Ce n'était même pas un détournement : **c'est ce que faisait le bouton d'appel
de l'overlay à chaque clic.** La liste des joueurs connectés arrivant
gratuitement dans le `welcome`, un script la rejouait bouton par bouton et
reconstruisait la table complète.

Tout le soin mis ailleurs à ne pas renseigner un scanneur — un numéro
inattribué qui sonne dans le vide plutôt que de répondre « inexistant », une
photo de profil qui renvoie `none` de façon indistinguable — était annulé par
cette seule voie.

**Correction, en une règle.** Le serveur ne renvoie **jamais un numéro que
l'appelant ne lui a pas donné.** Les trois réponses portent maintenant le
numéro composé par l'appelant lui-même. Appel par numéro : valeur identique,
rien ne change à l'écran. Appel par pseudo : champ absent.

Les onze autres `*_numero` du serveur restent en place et sont légitimes :
`sender_numero` et `caller_numero` sont l'identification de l'appelant, c'est
le modèle RP lui-même ; `caller_numero` / `callee_numero` en fin d'appel vont à
deux joueurs déjà en relation.

**Effet visible.** L'appel lancé depuis l'overlay affiche un libellé neutre au
lieu du nom pendant la sonnerie. Le client vidant son dernier numéro composé,
il vaut mieux un affichage neutre qu'un affichage **faux** — sans ça, l'écran
aurait montré le nom de la personne appelée *précédemment*.

*Question restée ouverte, de conception et non de sécurité :* l'overlay permet
d'appeler quelqu'un sans connaître son numéro, ce qui contourne la règle RP au
même titre que les conversations de groupe (#23).

---

## Ajouts

### 📱 Ton numéro s'affiche dans l'app Paramètres

Le joueur ne connaissait son propre numéro que par l'onglet **Compte** de la
fenêtre principale — hors du jeu, donc inaccessible en pleine partie, alors que
c'est justement l'information qu'on donne à l'oral pour se faire rappeler.

Il apparaît désormais sous le logo de l'app Paramètres, sur une ligne :
`Numéro de téléphone : 424294`, le libellé atténué et le numéro en gras.

Deux choix qui méritent d'être écrits :

- **Les tailles de police sont mesurées, pas posées.** `QFontMetricsF` calcule
  la largeur réelle des deux fragments et la boucle rétrécit jusqu'à tenir dans
  92 % de l'écran. Une valeur en dur qui tombe juste sur un écran déborde sur un
  autre, ou après un changement de police.
- **Rien ne s'affiche si le numéro est inconnu** — compte non relié, ou premier
  lancement avant le `welcome`. Pas de `------`, pas de `000000` : une valeur de
  remplissage à l'endroit exact où le joueur vient chercher un numéro à dicter
  serait pire que l'absence.

L'app ne lit rien de l'état global : l'overlay lui **pousse** la valeur à
l'ouverture, comme pour le fond d'écran. Une app du téléphone qui irait chercher
`state.my_numero` elle-même créerait une seconde source pour la même donnée —
exactement ce que la §5 ter cherche à éviter.

---

## Corrections

### 🕐 Les appels manqués rejoués s'inscrivaient à l'heure de la reconnexion

**Ce que tu voyais.** Après une absence, tous les appels manqués apparaissaient
horodatés à la minute de ta reconnexion. Un week-end entier se tassait sur une
seule minute, dans le désordre — l'historique devenait inexploitable justement
quand il servait le plus.

**Pourquoi.** Le serveur envoyait bien l'horodatage d'origine, conservé dans la
file depuis le dépôt. C'est le **client qui le jetait** : le signal
`sig_phone_missed` ne transportait que trois arguments, et `_record_call`
écrivait `time.time()` en dur.

Détail révélateur : les messages et images différés transportaient leur
horodatage depuis le début. Seul le signal des appels manqués, ajouté en
dernier, avait été oublié — même piège qu'une liste à alimenter à la main.

**Correction.** L'horodatage remonte jusqu'à l'historique. Une valeur nulle ou
absurde retombe sur l'heure locale plutôt que de produire une ligne datée de
1970.

### 🔀 …et l'ordre de l'historique était faux

Trouvé en vérifiant le correctif précédent. L'app Appels affichait la liste par
**ordre d'insertion**, pas par date. Les deux coïncidaient tant que tout
arrivait en temps réel ; la messagerie différée a cassé l'équivalence.

Corriger l'heure **seule** aurait donné pire que le bug d'origine : l'appel de
samedi, rejoué au retour, s'insère en dernier — il se serait donc retrouvé **en
tête de liste**, affiché « samedi » au-dessus des appels de lundi. Une
contradiction visible à l'écran.

L'historique est désormais trié sur l'horodatage. Les entrées trop anciennes
pour en avoir un partent en fin de liste.

### 🔊 Atténuation verticale intermittente

**Ce que tu entendais.** Sur un vaisseau, l'atténuation entre ponts marchait,
puis ne marchait plus, puis remarchait. Sans motif apparent.

**Pourquoi.** Le poids vertical est recalculé à chaque tour de boucle de
position, sans mémoire. Toute la machinerie anti-bruit OCR protège
`container_id` et `container_name` — mais la branche `[CID SIMILAIRE]` ne
reportait **pas** `zone`, alors que la branche `[ZONE COLLANTE]` le faisait.

Sur une lecture bruitée rattrapée par la première, le container restait donc
stable — pas de micro-coupure, rien d'audible côté proximité — mais `zone`
gardait sa valeur bruitée, la détection de vaisseau échouait, et l'atténuation
sautait pour un tour. D'où l'intermittence : ce n'était pas « ça ne marche
pas », c'était « ça clignote ».

**Correction.** `zone` suit le même sort que `container_name`. La détection lit
en outre `zone` **puis** `container_name` en repli, comme le mannequin le
faisait déjà : les deux composants posaient jusqu'ici deux questions
différentes sur le même fait, ce qui rendait toute comparaison à l'oreille
ininterprétable.

Un journal `[VPROX]` signale les **bascules seulement**, pas chaque tour. Si
l'atténuation reclignote, ces lignes donnent la zone exacte qui a fait échouer
la whitelist — plutôt que de rouvrir le dossier à l'oreille.

---

## Robustesse

### 📮 L'absence de `phone_queue` ne passe plus inaperçue

Le garde d'import de `circusvoip_phone_queue` se terminait par un `pass`. Si le
module manquait, le serveur démarrait normalement et la messagerie différée
était silencieusement désactivée : tout message adressé à un joueur hors ligne
repartait à la poubelle, comme avant le 03/08, et personne ne le savait avant
la plainte d'un joueur.

Le serveur continue de démarrer — c'est voulu, un module de messagerie manquant
ne doit pas priver les joueurs de la VOIP — mais il ne se tait plus : un
bandeau sur `stderr` **et** `stdout` énonce la conséquence en clair.

Le garde avait été écrit pour permettre un déploiement en deux temps. Le module
étant déployé depuis le 03/08, l'argument ne tenait plus.

---

## Dette relevée pendant ce chantier

- **`[COMPTES] desactives`** part sur `stdout` seul, sur une ligne discrète —
  alors qu'un serveur sans comptes refuse *toute* connexion. Plus grave qu'une
  messagerie différée absente, signalé plus doucement. Et son commentaire
  invoque encore le déploiement en deux temps, argument périmé au même titre.
- **`_load_cfg()` du mannequin** renvoie `{}` sur fichier absent **comme** sur
  JSON invalide. Le mannequin démarre alors sur les valeurs par défaut, et le
  premier clic sur Connecter **réécrit** la config avec celles-ci : une virgule
  de trop suffit à perdre l'IP, le token et le `service_secret`. Corollaire de
  la §5 ter — un repli silencieux qui transforme une faute de frappe en perte
  de données.
- **`service_secret` absent de l'interface du mannequin**, donc invisible et
  irrécupérable en cas de perte du fichier.

---

## Vérifications

Neuf cas testés sur un store temporaire : rotation, tolérance acceptée, mort de
la tolérance à la première connexion normale, expiration, purge à la
re-liaison, non-fuite des hashs, jeton vide, fiche inconnue, enveloppe.

Le défaut corrigé en cours de route est mesuré :

```
Sans restauration de l'ancien jeton : exclusion à la connexion 3
Avec                                : 7 connexions d'affilée, aucune exclusion
Client qui écrit normalement        : rotation effective, jeton initial invalidé
```

---

## À tester en jeu

1. **Rotation** — deux connexions de suite, puis
   `journalctl -u circusvoip-server.service | grep ROTATION`. Vide = correct.
2. **Appel par numéro** — comportement et affichage inchangés.
3. **Appel depuis l'overlay** — doit sonner ; affichage neutre attendu.
4. **Numéro inexistant** — doit sonner dans le vide, comme un joueur hors
   ligne.
5. **Atténuation verticale** — deux ponts d'un même vaisseau ; les lignes
   `[VPROX]` ne doivent apparaître qu'aux vraies transitions.
6. **Appel manqué en différé** — l'heure affichée doit être celle de l'appel,
   et la ligne doit se placer à sa vraie position dans l'historique.
7. **Numéro dans l'app Paramètres** — présent et lisible ; vérifier aussi ce que
   fait l'écran avant la connexion au serveur, quand le numéro n'est pas encore
   connu.
