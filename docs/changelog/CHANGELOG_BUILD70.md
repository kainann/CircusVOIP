# CircusVOIP — 0.4.0 Alpha Build 70

**Statut** : préparé — non publié
**Type** : mise à jour **client uniquement**. Le serveur est déjà à jour
(déployé le 04/08 à 15:37, empreinte `72c368f5…`) : ne rien redéployer.

> Aucune purge, aucune perte de données.
> ⚠ Les appels reçus **avant** ce build et disparus de l'historique ne
> reviendront pas — ils n'avaient jamais été écrits sur le disque.

---

## L'essentiel

Ce build corrige quatre bugs trouvés en testant le build 69 en conditions
réelles, et achève le passage à l'identité par numéro commencé au build 68.
Trois des quatre bugs sont des séquelles du même chantier : la suppression de
l'écran natif au build 68 a laissé derrière elle des références au monde
d'avant, dans des branches secondaires que personne ne traversait en testant.

---

## Corrections

### ☎️ L'appel entrant affichait le pseudo au lieu du numéro

**Ce que tu voyais.** Le mannequin t'appelle, tu ouvres le téléphone, et
l'écran annonce `Mannequin_01` au lieu de `425054`. Toute la règle RP repose
sur l'inverse : on ne connaît de l'appelant que son numéro, sauf à l'avoir
enregistré dans son carnet.

**Pourquoi.** L'écran d'appel était construit à **deux endroits** :

- à l'arrivée de l'appel, avec le numéro — correct ;
- à l'**ouverture de l'overlay**, reconstruit depuis l'état mémorisé, qui ne
  retenait que le pseudo.

Le téléphone étant « dans la poche », il n'est presque jamais ouvert au moment
où ça sonne : c'est donc systématiquement la seconde branche qui servait.

**Correction.** L'état d'appel porte désormais deux champs distincts et
explicitement séparés : `_phone_peer` (pseudo, **routage audio uniquement**)
et `_phone_peer_numero` (numéro, **tout ce qui s'affiche**). Les trois écrans
— entrant, sortant, en cours — lisent le second.

Le diagnostic a coûté une demi-douzaine de fausses pistes : compte de service
au pseudo vide, normalisation des pseudos, carnet de contacts, annuaire. Tout
fonctionnait ; seule la branche héritée était fautive.

### 📞 L'historique d'appels se vidait à la fermeture du client

**Ce que tu voyais.** Un appel apparaît dans l'historique pendant la session,
puis a disparu après avoir fermé et rouvert le client.

**Pourquoi.** L'historique vit dans la config client, sous `call_history`, et
il est écrit **en cours de session**. À la fermeture, le client réécrit la
config depuis un instantané pris au démarrage — donc sans l'appel. La valeur
du boot écrasait celle de la session.

**Correction.** `call_history` rejoint `_CORE_MANAGED_CFG_KEYS`, la liste des
clés protégées de cet écrasement.

> ⚠ **Cinquième occurrence du même piège**, après les raccourcis F6-F10 (b62),
> le fond d'écran du téléphone, le jeton Discord et le drapeau de purge 0.4.0.
> La liste est un correctif manuel qu'il faut penser à alimenter à chaque
> nouvelle clé écrite en session, et personne ne le fait spontanément. Le
> correctif structurel — que la sauvegarde relise le fichier disque et ne
> réécrive que les clés qu'elle gère — supprimerait la classe entière. **Non
> fait**, à mettre au backlog.

### 📵 Les appels manqués rejoués n'entraient pas dans l'historique

**Ce que tu voyais.** Le mannequin t'appelle pendant que tu es déconnecté, les
logs serveur montrent le dépôt puis le rejeu et l'acquittement — et rien
n'apparaît dans ton historique.

**Pourquoi.** Un appel manqué rejoué arrive **sans `call_id`** : l'appel est
terminé depuis longtemps. Le client passait par la clôture d'appel normale,
qui cherche cet identifiant dans les appels en cours, ne trouvait rien, et
sortait sans rien écrire. L'événement était reçu, acquitté, puis perdu.

**Correction.** Un appel manqué sans `call_id` est enregistré directement dans
l'historique.

### 🔴 Le rond rouge ne s'allumait plus sur l'app Messagerie

**Pourquoi.** Le badge était poussé sous l'identifiant `"msg"`, celui de
l'**écran natif Messagerie supprimé au build 68**. Les cases de l'accueil
portent désormais l'`APP_ID` de leur application, soit `"messagerie"` : le
badge ne correspondait plus à aucune case, et rien n'était dessiné.

**Correction.** L'identifiant est lu depuis `MessagerieApp.APP_ID` au lieu
d'être réécrit en dur. Un prochain renommage ne pourra plus casser le badge en
silence.

---

## Changement de protocole — numéros seuls

**Le pseudo a disparu des trames de téléphonie.** Neuf trames reprises : appel
entrant, sortant, accepté, manqué, occupé, message reçu, image reçue, et les
deux du rejeu différé. Elles ne transportent plus que des numéros.

**Pourquoi c'était nécessaire.** Le pseudo était encore envoyé « pour les
clients antérieurs ». Le client avait donc **deux sources pour la même
information**, et c'est exactement ce qui a produit le bug d'affichage
ci-dessus : une donnée en double finit toujours par être lue au mauvais
endroit. Le coût n'est pas le champ conservé, c'est la branche de code qui le
consomme et qu'on oublie de mettre à jour.

**La seule exception.** `phone_call_accepted` transporte encore deux pseudos,
sous les noms `caller_audio_id` et `callee_audio_id`. Ils sont indispensables :
le cœur audio filtre les trames de voix de l'appel en comparant le pseudo de
l'émetteur (`sender != state.phone_peer`). Les retirer couperait le son en
appel. Ils sont **nommés pour ce qu'ils font**, afin qu'aucun écran ne puisse
les afficher par mégarde. Le jour où le filtrage passera par le `call_id`, ils
disparaîtront.

**Nouvelle règle de conception** — écrite en **§5 ter** de `PROJET.md` :
*pas de compatibilité ascendante non demandée*. Quand une décision remplace un
mécanisme, l'ancien disparaît partout, y compris dans les chemins secondaires.
Une compatibilité se demande explicitement ; elle ne s'ajoute jamais d'office.
Le parc de clients n'est pas un argument.

---

## Outils de test (non distribués)

`circusvoip_mannequin.py` — hors `RELEASE_FILES`, purement local.

- **Appel par numéro.** Le champ accepte `42xxxx` comme un pseudo ; six
  chiffres partent en `target_numero`. Espaces et tirets tolérés.
- **Champ MESSAGE** : destinataire + texte + bouton SEND. Le mannequin ne
  savait ni envoyer ni recevoir de messages, ce qui rendait la messagerie
  différée **intestable** sans monter deux vrais clients.
- **Acquittement** des événements rejoués : sans lui, on ne pouvait observer
  que le repli à trois tentatives, jamais le chemin nominal.
- L'auto-répondeur et l'écho d'image existants sont **rétablis** — ils avaient
  été rendus inatteignables par l'ajout du nouveau gestionnaire, en tête de la
  même chaîne de conditions.

---

## Validé en conditions réelles

Testé sur le serveur de production avec deux comptes distincts :

- ✅ appel entrant affiché par numéro, téléphone ouvert **avant** comme
  **après** le début de la sonnerie ;
- ✅ appel manqué déposé en file (45 s de sonnerie dans le vide), rejoué à la
  reconnexion, acquitté, et **présent dans l'historique** ;
- ✅ historique conservé après fermeture et réouverture du client ;
- ✅ message envoyé à un joueur hors ligne, délivré à son retour ;
- ✅ badge rouge de l'app Messagerie.

**Non encore testé** : les **images** en différé. Le mannequin renvoie
automatiquement toute image reçue, donc un envoi pendant qu'il est déconnecté
couvrirait le dernier type d'événement de la file.

---

## Reste ouvert

- **Sauvegarde de config** : le correctif structurel décrit plus haut. Cinq
  occurrences du même piège, il y en aura une sixième.
- **Autres séquelles du build 68** : deux trouvées aujourd'hui, toutes deux
  dans des branches secondaires. Chercher dans le client les identifiants de
  l'ancien monde (`"msg"`, `_page_contacts`, `_page_convo`) et voir lesquels
  ne correspondent plus à rien.
- **Import gardé de `circusvoip_phone_queue`** : si le module manque, le
  serveur tourne sans messagerie différée **sans le dire**. C'est le repli
  silencieux que la règle §5 ter proscrit — il devrait s'annoncer au
  démarrage, ou refuser de démarrer.
- **Groupes** et **renommage de conversation** : conçus, non écrits. Point non
  tranché : un groupe distribuerait les numéros de tous ses membres, ce qui
  contourne la règle RP.
- **Jeton client** en clair dans la config, sans expiration. Plus gênant
  depuis que le blocage de compte est le mécanisme de défense principal :
  usurper un jeton permet de faire bannir sa victime.
- `allow_service_accounts` reste ouvert sur le VPS. Le mannequin ayant
  désormais son compte et son jeton, le refermer ne lui coûtera rien — le
  drapeau ne bloque que la **création** de nouveaux comptes de service.
