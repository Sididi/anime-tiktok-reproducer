# Design — Dé-fingerprinting des voix IA (ElevenLabs)

**Date** : 2026-06-02
**Statut** : Approuvé (design), prêt pour planification d'implémentation

## Contexte & problème

Le contenu publié sur TikTok subit des strikes automatisés « contenu de faible qualité »
qui sont des **faux positifs** : le support TikTok confirme que ces strikes sont
déclenchés automatiquement à la détection d'un fingerprint ElevenLabs / IA dans
l'audio, indépendamment de la qualité réelle (élevée) du contenu. Contester
manuellement via les droits EEE n'est pas viable à terme.

Objectif : post-traiter **notre propre audio TTS** pour réduire/supprimer les
empreintes détectables, afin de stopper les faux positifs, en acceptant une
légère dégradation de qualité contrôlable.

### Ce qu'est le « fingerprint »

Deux choses distinctes, attaquées par les mêmes leviers de traitement audio :

1. **Watermark explicite** — marquage volontaire (quasi-inaudible, typiquement
   en haute fréquence / étalement de spectre) ajouté par ElevenLabs pour la
   traçabilité.
2. **Empreinte statistique du vocodeur** — artefacts naturels d'une voix
   neuronale (signal « trop propre », absence de bruit de fond, enveloppe
   spectrale régulière, rolloff HF caractéristique, respiration/micro-bruits
   absents) détectés par un classifieur, sans watermark.

### Honnêteté sur les garanties

L'indétectabilité à 100 % ne peut pas être **garantie** : le détecteur de TikTok
est une boîte noire que nous ne contrôlons pas. Ce design fournit une chaîne de
traitement **réglable** que l'utilisateur ajuste empiriquement (A/B test) jusqu'à
ce que les strikes cessent.

## Décisions de design (validées)

- **Niveau de départ** : `default` (profil haute qualité en deux étapes :
  première passe inspirée de `geeknik/ai-audio-fingerprint-remover`, puis passe
  ffmpeg locale très légère ; `moderate` reste disponible comme option plus
  forte si les strikes persistent).
- **Contrôle** : config globale uniquement (pas d'UI, pas d'override par run).
- **Approche technique** : chaîne de filtres **ffmpeg** (déjà présent via pydub ;
  ffmpeg n8.1 dispose de `rubberband`, `afftdn`, `anlmdn`, `aecho`, `asetrate`,
  `atempo`, `acrusher`, `highpass`/`lowpass`, `asoftclip`, `loudnorm`, `aresample`
  soxr). Pas de nouvelle dépendance Python (ex. `pedalboard`) écartée.
- **Conservation de l'original** : backup `tts_edited.raw.wav`.
- **Randomisation seedée par run** : évite de créer une nouvelle empreinte
  constante ; seed loggé pour reproductibilité.

## Architecture

Module isolé, sans état, sans réseau :

```
backend/app/services/voice_defingerprint.py

VoiceDefingerprintService.apply(
    input_path: Path,
    output_path: Path,
    *,
    level: str,            # "off" | "default" | "light" | "moderate" | "aggressive"
    seed: int | None = None,
) -> dict                  # métadonnées : {level, seed, params, applied: bool}
```

- Entrée : un WAV. Sortie : un WAV de **même durée exacte** (préservation
  garantie ; voir « Préservation de la durée »).
- Responsabilité unique : transformer un fichier audio. Testable isolément.
- `apply()` ne lève jamais d'exception qui casse le pipeline appelant
  (fail-open ; voir « Robustesse »).

### Point d'injection dans le pipeline

L'audio **publié** est `tts_edited.wav` (bundlé dans le manifest d'export,
`export_service.py:498`, importé dans Premiere). Son timing est **verrouillé**
par l'alignement forcé (les sous-titres dépendent des word timings), d'où la
contrainte de préservation de durée.

Injection **tout à la fin** du pipeline de traitement (`processing.py`), une fois
`tts_edited.wav` complètement finalisé (après auto-editor, alignement forcé,
`rebuild_tts_audio_with_playback_segments`, résolution des gaps), **juste avant
l'export/bundling** :

1. Si `level == "off"` → no-op (aucun backup, aucune passe ffmpeg).
2. Sinon : backup `tts_edited.wav` → `tts_edited.raw.wav` (original propre conservé).
3. `apply(tts_edited.raw.wav, tts_edited.wav, level=..., seed=<seed du run>)`.

Ainsi tout le timing est déjà figé et n'est pas touché ; l'audio publié est la
version traitée.

## Chaîne de traitement

La sortie est **durée préservée** (sortie trimmée/paddée à la durée exacte de
l'entrée). Paramètres randomisés par run dans les bornes du niveau (RNG seedé).
Le niveau `default` exécute d'abord une première passe locale inspirée de
`geeknik/ai-audio-fingerprint-remover` : réécriture WAV sans métadonnées,
atténuation ciblée de bandes hautes typiques de watermark, dither HF masqué,
micro-dynamiques et imperfection harmonique minuscule. Cette première passe est
implémentée avec `numpy`/`scipy`/`soundfile` déjà présents, sans dépendre du
projet archivé ni ajouter `librosa`/`mutagen`.

> Note : aucun changement de tempo (`atempo`) — il modifierait la durée et
> désynchroniserait les sous-titres. Pitch via `rubberband` (durée préservée).
> La queue de réverb (`aecho`) est trimmée à la durée d'origine.

| Brique | Rôle anti-fingerprint | `light` | `default` | `moderate` | `aggressive` |
|---|---|---|---|---|---|
| Passe geeknik locale | métadonnées, bandes watermark hautes, micro-imperfections quasi inaudibles | — | ✓ | — | — |
| Détour rééchantillonnage (`aresample` soxr 44.1k→48k→44.1k) | casse empreintes niveau échantillon, ~transparent | ✓ | ✓ | ✓ | ✓ |
| Bruit de fond (mix `anoisesrc` brown/pink) | supprime le « trop propre », brouille watermark HF | -58 dBFS | -60 dBFS | -46 dBFS | -38 dBFS |
| Pitch/formant micro-shift (`rubberband`, durée préservée) | altère la signature du vocodeur | ±5 cents, formants préservés | ±4 cents, formants préservés | ±15–30 cents, formants préservés | ±50 cents, formants déplacés |
| EQ haute fréquence (`lowpass`/`highshelf`) | détruit le watermark logé >14 kHz | rolloff ~18 kHz | ~18.5–19.5 kHz | ~16 kHz + shelf | ~14 kHz + shelf marqué |
| Réverb de pièce courte (`aecho`, queue trimmée) | « sonne enregistré dans une vraie pièce » | — | — | très légère | légère |
| Saturation douce (`asoftclip` tanh) | harmoniques non-neuronales | — | — | — | ✓ |
| Round-trip lossy (encode→décode AAC/Opus) | écrase détail spectral fin + watermarks fragiles | 192k | 224k | 128k | 96k (×2) |
| Loudnorm final (`loudnorm` EBU R128) | loudness cohérent | ✓ | ✓ | ✓ | ✓ |

### Implémentation

1. **Passe geeknik locale (`default` uniquement)** : lecture/écriture WAV propre,
   suppression ciblée de bandes hautes, dither HF masqué, micro-dynamiques et
   imperfection harmonique très faible.
2. **Passe DSP ffmpeg** : `filter_complex` avec entrée bruit (`anoisesrc`) +
   briques DSP, sortie WAV trimmée à la durée d'origine.
3. **Passe round-trip lossy** : encode vers AAC/Opus au bitrate du niveau puis
   re-décode en WAV (×2 en `aggressive`).

Le constructeur de filtergraph est une **fonction pure**
`_build_filtergraph(level, params) -> str`, testable. `params` est tiré d'un RNG
seedé par run (`random.Random(seed)`), avec des bornes par niveau définies dans
une table de presets (`_PRESETS`).

## Configuration

Dans `backend/app/config.py` (pattern `ATR_` existant) :

- `voice_defingerprint_level: str = "default"` → env `ATR_VOICE_DEFINGERPRINT_LEVEL`
- Valeurs : `off` | `default` | `light` | `moderate` | `aggressive`.
- `off` = no-op total (aucun backup, aucune passe ffmpeg).
- Validation : valeur inconnue → fallback `default` + warning loggé.

## Préservation de la durée

Point le plus critique (sync sous-titres). Le module :

1. Mesure la durée d'entrée.
2. Force la sortie à la même durée (`atrim` / `apad` selon besoin ; la queue de
   réverb est coupée).
3. Vérifie après traitement que `|durée_sortie - durée_entrée| <= ~1 ms`, sinon
   déclenche le fallback (voir Robustesse).

## Robustesse (fail-open)

Le dé-fingerprinting ne doit **jamais** casser un run :

- Toute erreur ffmpeg (passe DSP ou round-trip) → log warning + restauration de
  l'audio original depuis `tts_edited.raw.wav` ; `apply()` retourne
  `{applied: False, ...}` sans lever.
- Vérification post-traitement : sortie = WAV valide, même samplerate, durée
  conforme. Échec → fallback original.
- Une production réussie avec strike potentiel vaut mieux qu'un run cassé.

## Tests

- **Unitaires `_build_filtergraph`** : déterminisme à seed fixe ; bornes de
  paramètres respectées par niveau ; `off` → no-op.
- **Préservation de durée** : `apply()` sur un WAV de référence → durée identique
  (tolérance ~1 ms).
- **Fail-open** : ffmpeg simulé en échec → original conservé, aucune exception
  propagée, `applied: False`.
- **Smoke d'intégration** : sortie = WAV mono valide, même samplerate/durée que
  l'entrée.

## Hors scope (YAGNI)

- Pas d'UI ni d'override par run.
- Pas de détecteur de watermark intégré.
- Pas de mesure automatique de « détectabilité » (boîte noire — validation
  empirique par l'utilisateur).
- Pas de changement de tempo.

## Validation empirique (post-implémentation)

Workflow attendu de l'utilisateur : démarrer en `default`, publier, observer.
Si des strikes persistent → passer en `moderate`, puis `aggressive` via
`ATR_VOICE_DEFINGERPRINT_LEVEL` (aucun changement de code). Si la qualité est
prioritaire et les strikes cessent → tester `light`.
