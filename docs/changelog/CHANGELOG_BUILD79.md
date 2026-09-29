# CircusVOIP v0.4.0 — Build 79

*21/09/2026*

## Nouveauté : app Annonces

Petites annonces **publiques** dans le CircusPhone : recrutement (secouristes,
sécurité…), événement, vente / achat. Tout joueur connecté voit toutes les
annonces.

- **Deux onglets** : « Annonces » (tout le tableau, boutons Appeler / Ajouter
  aux contacts) et « Mes annonces » (publier, retirer avec confirmation).
- **Un seul champ** : catégorie + description (10 à 300 caractères, 6 lignes).
- **Case « Inclure mon numéro de téléphone », cochée par défaut.** Décochée,
  l'annonce ne porte aucun contact : le serveur n'envoie jamais l'auteur aux
  autres joueurs.
- **Badge ★ Chef médical / Chef de la sécurité** sur les annonces de
  recrutement publiées par un chef. Calculé par le serveur à chaque envoi : un
  chef destitué le perd immédiatement.
- **Limites** : 3 annonces par joueur, 200 sur le serveur, 7 jours de vie,
  30 s entre deux publications d'un même joueur.
- **Ni son ni badge d'accueil** : sur un tableau public, ce serait sonner tout
  le serveur à chaque vente.
- **La saisie n'est jamais effacée par un autre joueur** : une publication
  reçue pendant qu'on écrit est mise de côté, l'écran se met à jour à la sortie
  du formulaire.
- **Annuler jette la saisie.** Le texte ne survit qu'aux sorties involontaires
  (appel entrant, téléphone fermé, changement d'onglet) : le bouton devient
  alors « Reprendre mon annonce ».
- Contrat clavier §5 ter respecté (champ, Retour arrière, suivi du focus
  souris).

### Admin

Nouvel onglet **ANNONCES** : catégorie, numéro et pseudo de l'auteur, numéro
affiché ou non, texte. Croix de retrait avec confirmation ; l'auteur n'est pas
prévenu. Mise à jour automatique à chaque publication ou retrait.

### Protocole

Joueur : `annonces_liste`, `annonces_publier`, `annonces_retirer` → réponse
unique `annonces_etat` (`reponse: true` pour le demandeur, `false` pour les
poussées). Admin : `annonces_admin_list`, `annonces_admin_retirer` →
`annonces_admin`. Côté client, `annonces_*` suit le routage des `mp_*` vers
l'app affichée : pas de signal dédié.

## Fichiers

| Fichier | MD5 | Où |
|---|---|---|
| `circusvoip_client.py` | `8df848230c5f5c02db85f2c095591465` | client |
| `circusvoip_phone_annonces_app.py` | `2b471016015e2a5f35ee79936846a60b` | client (**nouveau**) |
| `circusvoip_phone_annonces.py` | `1757aa944bea4f6344445ab3d180c800` | client **et** serveur (**nouveau**, même empreinte) |
| `circusvoip_server.py` | `3ab3f7b9b3674ca4c1e80fe294ca292f` | serveur |
| `circusvoip_annonces_store.py` | `e17d574f737d3256ea3b33e5946e5ee8` | serveur seul (**nouveau**, jamais dans `RELEASE_FILES`) |
| `circusvoip_admin.py` | `d6f27a636af18878a22ad61ca6a35dc2` | installeur serveur / admin |

`build_update.py` : deux entrées ajoutées à `RELEASE_FILES`
(`circusvoip_phone_annonces.py`, `circusvoip_phone_annonces_app.py`).

## À tester en groupe

Publication vue par un autre joueur app ouverte ; case décochée = « Sans
contact » chez l'autre ; badge chef ; saisie en cours pendant qu'un autre
publie ; retrait avec confirmation ; retrait admin ; plafond 3 et intervalle
30 s.
