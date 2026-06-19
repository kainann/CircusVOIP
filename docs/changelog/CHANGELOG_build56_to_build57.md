# Changelog build 56 → 57

Périmètre : commits `83350c2` (build 56) → `949aa62` (bump build 57), plus
`f626f9b` qui appartient à la release v0.2.0 build 57 (publiée sous le tag
`client-v0.2.0`). Côté commits « purs » 56→57, seul le bump version existe ;
le gros du changement audio est porté par `f626f9b`, dont le message
(« disable auto-update ») ne reflète pas le contenu réel.

Fichier principal touché : `client/circusvoip_audio_io.py`.

---

## Audio — anti-crackling (jitter buffer, hystérésis, PLC)

Refonte du chemin de réception/mix audio pour supprimer le crackling
diagnostiqué via les CSV du 02/06/2026 (sessions Skywat, Alex,
Firesstones). Trois mécanismes ajoutés, par sender.

### 1. Jitter buffer warmup
- Nouvelle constante `JITTER_BUFFER_WARMUP_FRAMES = 5` (≈100 ms de latence
  ajoutée ; valeur montée de 3→5 après le test Alex où 3 trames se
  stabilisaient parfois à 1 seule en queue).
- État par sender `_jb_state` (`"waiting"` | `"playing"`). Tant que la queue
  n'a pas atteint le seuil de warmup, le sender contribue **silence** au mix
  sans compter d'underrun (silence volontaire).
- Nouveaux dicts d'état/stats : `_jb_state`, `_jb_warmup_count`,
  `_jb_silent_blocks`, `_jb_zero_streak`.
- Un nouveau sender démarre en `"waiting"` dès sa création dans
  `_remote_buffers` (cf. `feed`).
- Contexte : diagnostic Skywat — 82,6 % des callbacks output trouvaient la
  queue à 0-1 trame alors que 98 % des trames arrivaient dans la fenêtre
  10-25 ms ⇒ 1439 underruns / 8514 callbacks (16,9 %) sur 170 s.

### 2. Hystérésis sur le retour en warmup
- Nouvelle constante `JITTER_BUFFER_ZERO_STREAK = 5` (tolère un trou
  ≈100 ms = N×20 ms).
- Le retour `"playing"` → `"waiting"` n'est plus déclenché au premier
  `qsize()==0`, mais seulement après `JITTER_BUFFER_ZERO_STREAK` callbacks
  consécutifs à queue vide. En-deçà, on accepte l'underrun ponctuel (20 ms
  de silence) plutôt qu'un re-warmup pénalisant (60 ms).
- Compteur `_jb_zero_streak` incrémenté sur queue vide en `"playing"`, remis
  à 0 sur pop réussi.
- Contexte : tests Firesstones — des mini-trous de 50-100 ms déclenchaient
  un re-warmup ⇒ 71 pops résiduels sur 222 s.

### 3. PLC (Packet Loss Concealment)
- Nouvelle constante `PLC_GAIN = 0.5` (−6 dB).
- Sur **vrai** underrun (`_is_real_underrun` : sender actif, non mute, trame
  manquante), on rejoue la dernière trame brute reçue atténuée de `PLC_GAIN`
  au lieu d'écrire du silence.
- Anti-bouclage strict : un seul PLC par trame mémorisée (`_plc_used`), pas
  de cascade. Les underruns consécutifs suivants réécrivent silence.
- Trame brute utilisée (avant traitement radio/phone) ; le filtre biquad
  radio n'est pas réappliqué (état incohérent masqué par l'atténuation
  −6 dB).
- Routage PLC identique au pop normal : phone → `mix_phone`, sinon
  `mix_radio`/`mix_prox` selon `is_radio`.
- Nouveaux dicts : `_plc_last_frame`, `_plc_used`, `_plc_applied_total`.

---

## Diagnostic — log audio RX détaillé (optionnel)

- Import optionnel du module autonome `circusvoip_audio_rx_logger`
  (fallback `_audio_rx_logger = None` ⇒ tous les appels no-op si le fichier
  est absent).
- Nouvelle méthode `AudioIO.set_audio_rx_log_enabled(enabled, pseudo,
  debug_dir)` : toggle depuis l'UI (« Activer le log audio détaillé » dans
  les Paramètres). Écrit un CSV séparé dans `circusvoip_debug/audio_rx/`.
  Retourne `False` si module absent / déjà dans l'état demandé / échec
  ouverture.
- `_on_output_block` : appel `log_out(...)` en fin de callback (no-op si
  inactif, branchement conditionnel pour éviter tout calcul à 50 Hz).
  Collecte : `callback_period_ms` (jitter sounddevice via `_last_callback_ts`),
  snapshot `senders_state` après pops (q, vol, under, jb, streak, plc),
  `mix_peak_pre_tanh` / `mix_peak_post_tanh`, cumuls truncations/silence,
  flags (`cave_echo`, `beep`, `soundboard`, `sonnerie`).
- `feed` : appel `log_rx(...)` par trame reçue. Collecte `delta_ms` (depuis
  trame précédente du même sender), `q_before`/`q_after`, `outcome`
  (`OK` / `DROP_QUEUE_FULL`), `msg_type` (0x00 prox / 0x01 radio / 0x03
  phone — radio canal et profil non distingués ici).
- Nouvel attribut `_last_callback_ts`.

---

## Release

- `chore: disable auto-update check for public release v0.2.0` (`f626f9b`) :
  désactivation de la vérification d'auto-update pour la publication
  publique. (Le même commit embarque la refonte audio ci-dessus.)
- `chore: bump to v0.2.0 stable build 057` (`949aa62`) : bump
  `client/circusvoip_version.json`, build 056 → **057**, channel `stable`.

---

## Note de cohérence

Le fichier `client/circusvoip_audio_rx_logger.py` reste **untracked** dans
le dépôt (module de debug exclu du suivi Git). L'import optionnel garantit
que l'absence du module ne casse rien (no-op). À décider si on souhaite le
versionner pour que le toggle UI soit fonctionnel chez les utilisateurs.
