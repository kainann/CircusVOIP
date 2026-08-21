# CircusVOIP — 0.4.0 Alpha Build 74

**Statut** : **publié le 19/08/2026** — serveur et client.
**Type** : mise à jour **client + serveur**. Le serveur doit être redéployé, et
**avant** le client.

> **Déploiement propre**, contrairement au build 73. Les trois contrôles
> d'après-build sont passés : `circusvoip_phone_groupes.py` seul dans
> `updates/files/` (le store n'est pas parti), manifest en `74`, et les deux
> copies du fichier partagé à la **même empreinte** — celle du VPS et celle
> distribuée aux joueurs.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_server.py` | VPS | `761d617942fc95eb518382e6f608e6fd` |
| `circusvoip_groupes_store.py` | **VPS seul** | `8c73edec45e104526ecb241a2229ab21` |
| `circusvoip_phone_groupes.py` | **VPS + build** | `91fe66949e0901d4d1b01ee92b837bc9` |
| `circusvoip_client.py` | build | `291d1278fd195d5631c38cb5fbce6cc1` |
| `circusvoip_phone_annuaire.py` | build | `16b4cdc358fe44d6578f534a8ae98e13` |
| `build_update.py` | outil | `78ec4b250a9b568c9b1b353169266bfc` |

> **`circusvoip_phone_groupes.py` va des DEUX côtés** (cas C du §7 de l'INFRA),
> comme `circusvoip_phone_urgence.py` et `circusvoip_phone_travail.py`. Ne le
> pousser que d'un côté ne produit **aucune erreur** : le client valide
> simplement une saisie que le serveur refuse, ou l'inverse. Contrôler les deux
> empreintes après déploiement.
>
> **`circusvoip_groupes_store.py` ne doit JAMAIS entrer dans `RELEASE_FILES`** :
> il contient la composition de tous les groupes du serveur.

---

## Discussions de groupe (chantier principal)

Plusieurs joueurs partagent une conversation. Un groupe se crée depuis la
Messagerie, à partir du carnet de contacts.

**Décisions structurantes, à ne pas ré-ouvrir sans raison :**

- **Un groupe est FIGÉ à sa création.** Pas d'invitation, pas d'acceptation,
  pas d'ajout ni de retrait après coup. Il n'y a donc aucun état transitoire à
  arbitrer, aucune course entre deux clients, aucun écran de gestion. Pour
  ajouter quelqu'un, on crée un nouveau groupe.
- **Le groupe distribue les numéros, et c'est VOULU.** Ce n'est pas une entorse
  à la règle RP : cette règle protège le **nom**, pas le numéro. Le serveur
  n'envoie jamais de pseudo, et un numéro seul est anonyme — un membre ne verra
  un nom que s'il l'a déjà dans **son** carnet.
- **Le créateur n'a aucun privilège** une fois le groupe créé. Il ne peut ni
  exclure, ni renommer, ni dissoudre. Un groupe meurt quand son **dernier**
  membre le quitte, pas quand son créateur part.
- **Quitter est définitif et visible.** Les membres restants reçoivent une
  annonce ; les absents la reçoivent en différé.
- **À un seul membre, le groupe passe en LECTURE SEULE.** La conversation reste
  consultable, l'envoi est refusé — côté client *et* côté serveur.
- **Le serveur ne stocke AUCUN message**, seulement la composition. Les
  messages sont routés puis oubliés, comme les messages directs.

**Fichiers** : `circusvoip_phone_groupes.py` (règles, deux côtés),
`circusvoip_groupes_store.py` (registre serveur, persistant),
écrans dans `circusvoip_phone_annuaire.py` (pas de fichier d'app séparé).

**Trames** : `groupe_liste`, `groupe_creer`, `groupe_quitter`, `groupe_envoyer`.
Point d'entrée unique, état complet renvoyé après chaque action, plus une
poussée d'état vers les membres concernés sans qu'ils aient rien demandé.

### Deux réutilisations qui ont évité d'écrire un second modèle

**La conversation de groupe est rangée sous la clé `G:<id>`**, dans le même
dictionnaire que les conversations directes. Tout l'écran de conversation
existant fonctionne sans modification : bulles, brouillon, défilement. La
collision avec un numéro est structurellement impossible — un numéro est
purement numérique, `G:` ne l'est pas.

**Un message de groupe voyage dans une trame `phone_message_received`
ordinaire**, avec le groupe encodé en tête du corps : `[grp]<id>\n<texte>`.
Même technique que `[img]` pour les images. Conséquence décisive : le chemin
**direct** et le chemin **différé** sont identiques, et la messagerie hors ligne
a fonctionné sans toucher à `circusvoip_phone_queue.py`, dont le schéma est déjà
écrit sur le disque du VPS — aucune migration.

### Affichage

L'auteur est écrit dans le corps stocké, sous forme de **numéro** — le nom n'est
stocké nulle part, il est substitué à l'affichage. Un contact renommé ne réécrit
donc pas les conversations déjà enregistrées.

Les annonces du serveur portent un marqueur `[sys]` et s'affichent en texte gris
centré, sans bulle ni horodatage. Une annonce n'est pas un message.

> **Les annonces reçues avant ce build** sont stockées sans le marqueur et
> resteront affichées en bulle. Ce n'est pas un bug de cette version.

### Ordre des apps de l'écran d'accueil

Ordre **explicite**, choisi par l'utilisateur : Appels, Contacts, Messagerie,
Portefeuille, Blueprints, Jeux, Caméra, Travail, Urgence, Photos, Paramètres.

C'est désormais la **seule** source de l'ordre. Les positions choisies ailleurs
— dans le registre, ou par l'ordre des `append()` — n'ont plus d'effet. Une app
absente de la liste se place juste avant Paramètres plutôt que de disparaître.

---

## Correctifs de bugs préexistants

- **Perte totale possible des conversations.** `_phone_save_messages()` écrivait
  par `write_text()`, qui **tronque le fichier avant d'écrire**. Une coupure au
  milieu — crash, alt-F4, coupure de courant — laissait un JSON invalide ; au
  démarrage suivant la lecture échouait, le client repartait à vide, et la
  première sauvegarde **écrasait le fichier abîmé**. Aucun recours : le serveur
  n'archive pas les messages. Désormais écriture atomique par `os.replace()`, et
  un fichier illisible est renommé `.corrompu.json` au lieu d'être écrasé.
- **Refus d'envoi silencieux.** Le serveur émettait `phone_message_refused`
  (limitation de débit, ou file du destinataire pleine) avec le commentaire
  « un refus silencieux laisserait l'émetteur croire qu'il a été délivré » —
  mais **aucun client ne traitait cette trame**. Le refus était donc silencieux
  quand même, et le message restait affiché comme envoyé. Libellé volontairement
  neutre pour « file pleine » : dire laquelle confirmerait qu'un numéro existe et
  que la personne est absente.
- **Icône trombone jamais dessinée.** Un `from PySide6.QtCore import QPointF`
  **local** dans la branche `gear` de `paintEvent` rendait le nom local à
  **toute** la fonction, y compris avant la ligne d'import : les `QPointF()` de
  la branche `clip`, trente lignes plus haut, levaient
  `cannot access local variable 'QPointF'`. Le trombone retombait sur le glyphe
  de secours à chaque peinture.
- **Repli d'import incomplet.** Le repli de `circusvoip_phone_contacts` dans
  l'annuaire définissait `ContactError` mais **pas** `normalise_numero` : si ce
  module manquait, toute frappe dans le champ numéro levait `NameError` et
  l'écran paraissait mort, sans message.

---

## Ordre de déploiement

> Commandes à passer depuis `cmd` sous Windows : **une seule ligne chacune**.
> Le `\` de continuation est une syntaxe de shell Unix, il ne fonctionne pas ici.

**1. Le serveur d'abord.**

```cmd
scp "D:\Projet CircusVOIP\fichiers serveurs\circusvoip_server.py" "D:\Projet CircusVOIP\fichiers serveurs\circusvoip_groupes_store.py" "D:\Projet CircusVOIP\circusvoip_phone_groupes.py" root@178.104.207.46:/home/circusvoip/app/
```

```cmd
ssh root@178.104.207.46 "chown circusvoip:circusvoip /home/circusvoip/app/*.py && systemctl restart circusvoip-server.service"
```

```cmd
ssh root@178.104.207.46 "systemctl is-active circusvoip-server.service; journalctl -u circusvoip-server.service -n 30 --no-pager | grep -iE 'GROUPES|Traceback'"
```

Attendu : `active`, et **aucune ligne**. Un bandeau `*** GROUPES DESACTIVES ***`
signale un échec d'import, avec l'exception exacte — inutile d'aller plus loin,
aucune trame ne passera.

**2. Puis le client.** ⚠ **Avant de builder**, vérifier `RELEASE_FILES` :

```cmd
findstr /n "groupes" build_update.py
```

Deux lignes attendues : `circusvoip_phone_groupes.py` comme entrée de la liste,
et `circusvoip_groupes_store.py` uniquement dans le bloc
« NE JAMAIS ajouter ici ».

```cmd
py -3 build_update.py --bump-build --notes "Build 74 — discussions de groupe"
```

**Puis contrôler ce qui est réellement parti.** `build_update.py` n'a aucun
moyen de savoir qu'une liste est incomplète : il copie ce qu'on lui donne, sans
se plaindre. C'est l'oubli qui a coûté un second upload au build 73.

```cmd
ssh root@178.104.207.46 "ls /home/circusvoip/updates/files/ | grep groupes"
```

```cmd
ssh root@178.104.207.46 "md5sum /home/circusvoip/app/circusvoip_phone_groupes.py"
```

Une seule ligne au premier (`circusvoip_phone_groupes.py` ; deux voudrait dire
que le store est parti par erreur), et au second l'empreinte du tableau — c'est
ce contrôle qui protège du piège du fichier partagé.

---

## Ce que ce build ne corrige pas

- **Rien n'a été testé à deux clients réels.** L'arrivée d'un message chez
  l'autre, l'annonce de départ et le mode lecture seule ne sont vérifiés que par
  doublure. Le chemin différé — écrire à quelqu'un de déconnecté, puis le
  reconnecter — n'a jamais tourné.
- **Deux groupes homonymes aux mêmes membres sont indistinguables**, y compris
  dans la confirmation de sortie. C'est le prix du figeage.
- **Pas de suppression de conversation.** Écarté volontairement : la fonction
  n'existe nulle part dans le CircusPhone, ni pour les groupes ni pour les
  messages directs.
- **Plafond de 50 messages inchangé** pour les groupes. Il s'applique via le
  stockage partagé, sans avoir été écrit pour eux — et les annonces système
  consomment aussi des places. À dix membres actifs, l'historique part vite.
- **Rien n'indique visuellement qu'il faut appuyer sur droite** pour atteindre
  la croix « quitter ».
- **Le numéro n'est pas substitué dans les annonces différées** reçues avant ce
  build.
- **Seize imports locaux masquent un import global** dans
  `circusvoip_client.py`. Aucun n'est un bug aujourd'hui — le nom n'est jamais
  utilisé avant son import — mais ce sont des pièges dormants du même type que
  celui du trombone.
- **Quatre fichiers écrivent du JSON sans atomicité** : `core`, `mannequin`,
  `server_config`, `update_server`. Moins critiques (reconstructibles ou
  versionnés), mais `circusvoip_core.py` mérite un examen.
- **Un `except:` nu** dans `circusvoip_sc_ocr.py:2209`, qui avale aussi
  `KeyboardInterrupt`.
