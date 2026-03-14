/**
 * Anime TikTok Reproducer - Premiere Pro 2025 Automation Script (v7.7 - EXTERNAL SUBTITLE MOGRT LOAD)
 *
 * CHANGES from v7.6:
 * - Removed all in-script subtitle MOGRT generation logic.
 * - Loads pre-generated subtitle MOGRT files from /subtitles.
 * - Uses subtitles.srt only for timeline timing (start/end).
 */

(function () {
  // ========================================================================
  // 1. CONFIGURATION
  // ========================================================================
  var ROOT_DIR = new File($.fileName).parent.fsName;
  var ASSETS_DIR = ROOT_DIR + "/assets";
  var SOURCES_DIR = ROOT_DIR + "/sources";

  var SEQUENCE_PRESET_PATH = ASSETS_DIR + "/TikTok60fps.sqpreset";
  var BORDER_MOGRT_PATH = ASSETS_DIR + "/White border 10px.mogrt";
  var AUDIO_FILENAME = "tts_edited.wav";
  var CATEGORY_OVERLAY_FILENAME = "category_overlay.png";
  var TITLE_OVERLAY_FILENAME = "title_overlay.png";
  var MUSIC_FILENAME = "credits song for my death.wav";
  var MUSIC_GAIN_DB = -23.0;
  var PROJECT_PURGE_BIN_NAME = "__ATR_PURGE__";
  var BACKGROUND_PRESET_NAME = "SPM Anime Background";
  var BACKGROUND_PRESET_FILE_PATH =
    ASSETS_DIR + "/" + BACKGROUND_PRESET_NAME + ".prfpset";
  var FOREGROUND_PRESET_NAME = "SPM Anime Foreground";
  var FOREGROUND_PRESET_FILE_PATH =
    ASSETS_DIR + "/" + FOREGROUND_PRESET_NAME + ".prfpset";
  var CATEGORY_TITLE_PRESET_NAME = "SPM Anime Category Title";
  var CATEGORY_TITLE_PRESET_FILE_PATH =
    ASSETS_DIR + "/" + CATEGORY_TITLE_PRESET_NAME + ".prfpset";
  var SUBTITLE_MOGRT_DIR = ROOT_DIR + "/subtitles";
  var SUBTITLE_SRT_PATH = ROOT_DIR + "/let_this_grieving_soul_retire.fr_FR.srt";
  var RAW_SCENE_SUBTITLE_MANIFEST_PATH =
    ROOT_DIR + "/raw_scene_subtitles/manifest.json";

  // --- SCENES DATA ---
  var scenes = [
    {
      scene_index: 0,
      start: 0.0,
      end: 1.75,
      text: "À la base, ce type était super faible,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 13936,
      source_out_frame: 14002,
      source_in: 581.247333,
      source_out: 584.000083,
      clip_duration: 2.7527,
      target_duration: 1.75,
      speed_ratio: 1.573,
      effective_speed: 1.573,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 1,
      start: 1.75,
      end: 3.333333,
      text: "mais il possédait des dizaines de bagues",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5935,
      source_out_frame: 5972,
      source_in: 247.538958,
      source_out: 249.082167,
      clip_duration: 1.5432,
      target_duration: 1.5833,
      speed_ratio: 0.9747,
      effective_speed: 0.9747,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 2,
      start: 3.333333,
      end: 5.616667,
      text: "capables de résister aux attaques les plus mortelles.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5995,
      source_out_frame: 6050,
      source_in: 250.041458,
      source_out: 252.335417,
      clip_duration: 2.294,
      target_duration: 2.2833,
      speed_ratio: 1.0047,
      effective_speed: 1.0047,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 3,
      start: 5.616667,
      end: 8.116667,
      text: "Du coup, aucune attaque ne pouvait le blesser, même pas un peu.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9459,
      source_out_frame: 9507,
      source_in: 394.519125,
      source_out: 396.521125,
      clip_duration: 2.002,
      target_duration: 2.5,
      speed_ratio: 0.8008,
      effective_speed: 0.8008,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 4,
      start: 8.116667,
      end: 10.85,
      text: "Ce qui donnait l'impression aux autres qu'il avait une force terrifiante.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 1582,
      source_out_frame: 1648,
      source_in: 65.982583,
      source_out: 68.735333,
      clip_duration: 2.7528,
      target_duration: 2.7333,
      speed_ratio: 1.0071,
      effective_speed: 1.0071,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 5,
      start: 10.85,
      end: 14.35,
      text: "Un jour, le président de l'Association des Explorateurs l'a averti en privé.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5239,
      source_out_frame: 5347,
      source_in: 218.509958,
      source_out: 223.014458,
      clip_duration: 4.5045,
      target_duration: 3.5,
      speed_ratio: 1.287,
      effective_speed: 1.287,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 6,
      start: 14.35,
      end: 16.816667,
      text: "Un explorateur de niveau 7 était en route pour l'Empire,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5107,
      source_out_frame: 5155,
      source_in: 213.004458,
      source_out: 215.006458,
      clip_duration: 2.002,
      target_duration: 2.4667,
      speed_ratio: 0.8116,
      effective_speed: 0.8116,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 7,
      start: 16.816667,
      end: 20.016667,
      text: "et le président espérait qu'il éviterait tout conflit avec cet individu.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5407,
      source_out_frame: 5492,
      source_in: 225.516958,
      source_out: 229.062167,
      clip_duration: 3.5452,
      target_duration: 3.2,
      speed_ratio: 1.1079,
      effective_speed: 1.1079,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 8,
      start: 20.016667,
      end: 21.65,
      text: "Après tout, c'était un vrai poids lourd,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 12816,
      source_out_frame: 12862,
      source_in: 534.534,
      source_out: 536.452583,
      clip_duration: 1.9186,
      target_duration: 1.6333,
      speed_ratio: 1.1746,
      effective_speed: 1.1746,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 9,
      start: 21.65,
      end: 23.783333,
      text: "doté de la force des Chroniques du Tueur de Dragons.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 12864,
      source_out_frame: 12912,
      source_in: 536.536,
      source_out: 538.538,
      clip_duration: 2.002,
      target_duration: 2.1333,
      speed_ratio: 0.9384,
      effective_speed: 0.9384,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 10,
      start: 23.783333,
      end: 27.166667,
      text: "Pourtant, notre héros a répondu qu'il n'avait jamais entendu parler de lui.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5563,
      source_out_frame: 5635,
      source_in: 232.023458,
      source_out: 235.026458,
      clip_duration: 3.003,
      target_duration: 3.3833,
      speed_ratio: 0.8876,
      effective_speed: 0.8876,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 11,
      start: 27.166667,
      end: 30.15,
      text: "Mais sa réponse directe a été mal interprétée par le président.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5659,
      source_out_frame: 5738,
      source_in: 236.027458,
      source_out: 239.322417,
      clip_duration: 3.295,
      target_duration: 2.9833,
      speed_ratio: 1.1045,
      effective_speed: 1.1045,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 12,
      start: 30.15,
      end: 36.6,
      text: "Car il était reconnu comme le seul surpuissant de niveau 8. Alors le président l'a giflé. La gifle en elle-même n'était pas si grave.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5743,
      source_out_frame: 5863,
      source_in: 239.530958,
      source_out: 244.535958,
      clip_duration: 5.005,
      target_duration: 6.45,
      speed_ratio: 0.776,
      effective_speed: 0.776,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 13,
      start: 36.6,
      end: 38.5,
      text: "Mais elle a fait voler en éclats ses bagues.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5923,
      source_out_frame: 5972,
      source_in: 247.038458,
      source_out: 249.082167,
      clip_duration: 2.0437,
      target_duration: 1.9,
      speed_ratio: 1.0756,
      effective_speed: 1.0756,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 14,
      start: 38.5,
      end: 41.116667,
      text: "Bien sûr, sa propre faiblesse en était l'une des raisons.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 5983,
      source_out_frame: 6030,
      source_in: 249.540958,
      source_out: 251.50125,
      clip_duration: 1.9603,
      target_duration: 2.6167,
      speed_ratio: 0.7492,
      effective_speed: 0.75,
      leaves_gap: true,
      used_alternative: false,
    },
    {
      scene_index: 15,
      start: 41.116667,
      end: 45.0,
      text: "Et le président était lui-même un ancien et puissant explorateur de niveau 7.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 02",
      source_in_frame: 33950,
      source_out_frame: 34044,
      source_in: 1415.997917,
      source_out: 1419.9185,
      clip_duration: 3.9206,
      target_duration: 3.8833,
      speed_ratio: 1.0096,
      effective_speed: 1.0096,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 16,
      start: 45.0,
      end: 48.416667,
      text: "Mais notre héros avait déjà vécu ce genre de situation de nombreuses fois.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 6056,
      source_out_frame: 6158,
      source_in: 252.585667,
      source_out: 256.839917,
      clip_duration: 4.2542,
      target_duration: 3.4167,
      speed_ratio: 1.2451,
      effective_speed: 1.2451,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 17,
      start: 48.416667,
      end: 50.433333,
      text: "Après ça, il est allé voir son amie d'enfance.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 8272,
      source_out_frame: 8320,
      source_in: 345.011333,
      source_out: 347.013333,
      clip_duration: 2.002,
      target_duration: 2.0167,
      speed_ratio: 0.9927,
      effective_speed: 0.9927,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 18,
      start: 50.433333,
      end: 54.133333,
      text: "Comme convenu, il lui a rapporté tout le matériel de recherche dont elle avait besoin,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 8344,
      source_out_frame: 8416,
      source_in: 348.014333,
      source_out: 351.017333,
      clip_duration: 3.003,
      target_duration: 3.7,
      speed_ratio: 0.8116,
      effective_speed: 0.8116,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 19,
      start: 54.133333,
      end: 56.35,
      text: "même s'il n'avait aucune idée de ce qu'il y avait dedans.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 8500,
      source_out_frame: 8548,
      source_in: 354.520833,
      source_out: 356.522833,
      clip_duration: 2.002,
      target_duration: 2.2167,
      speed_ratio: 0.9032,
      effective_speed: 0.9032,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 20,
      start: 56.35,
      end: 58.766667,
      text: "Quand son amie a retiré le tissu qui recouvrait le tout,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 8956,
      source_out_frame: 9039,
      source_in: 373.539833,
      source_out: 377.001625,
      clip_duration: 3.4618,
      target_duration: 2.4167,
      speed_ratio: 1.4325,
      effective_speed: 1.4325,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 21,
      start: 58.766667,
      end: 62.833333,
      text: "il a été choqué de découvrir que c'était en fait un familier de dévoreur malveillant.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 8828,
      source_out_frame: 8932,
      source_in: 368.201167,
      source_out: 372.538833,
      clip_duration: 4.3377,
      target_duration: 4.0667,
      speed_ratio: 1.0666,
      effective_speed: 1.0666,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 22,
      start: 62.833333,
      end: 64.183333,
      text: "Mais ce à quoi il ne s'attendait pas,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9075,
      source_out_frame: 9111,
      source_in: 378.503125,
      source_out: 380.004625,
      clip_duration: 1.5015,
      target_duration: 1.35,
      speed_ratio: 1.1122,
      effective_speed: 1.1122,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 23,
      start: 64.183333,
      end: 66.55,
      text: "c'est que ce n'était qu'un déguisement pour le dévoreur.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9159,
      source_out_frame: 9207,
      source_in: 382.006625,
      source_out: 384.008625,
      clip_duration: 2.002,
      target_duration: 2.3667,
      speed_ratio: 0.8459,
      effective_speed: 0.8459,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 24,
      start: 66.55,
      end: 71.016667,
      text: "Même s'il était petit, le dévoreur avait la force d'une créature de niveau 6 maximal.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9244,
      source_out_frame: 9351,
      source_in: 385.551833,
      source_out: 390.014625,
      clip_duration: 4.4628,
      target_duration: 4.4667,
      speed_ratio: 0.9991,
      effective_speed: 0.9991,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 25,
      start: 71.016667,
      end: 74.716667,
      text: "Ce n'est qu'en le prenant dans ses bras qu'il a compris que quelque chose n'allait pas.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9364,
      source_out_frame: 9454,
      source_in: 390.556833,
      source_out: 394.310583,
      clip_duration: 3.7537,
      target_duration: 3.7,
      speed_ratio: 1.0145,
      effective_speed: 1.0145,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 26,
      start: 74.716667,
      end: 80.616667,
      text: "Le dévoreur secouait juste la queue pour jouer, mais pour une raison inconnue, chaque mouvement activait ses bagues sans causer de dégâts.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9495,
      source_out_frame: 9615,
      source_in: 396.020625,
      source_out: 401.025625,
      clip_duration: 5.005,
      target_duration: 5.9,
      speed_ratio: 0.8483,
      effective_speed: 0.8483,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 27,
      start: 80.616667,
      end: 82.8,
      text: "Heureusement, son amie le lui a vite repris des mains,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9628,
      source_out_frame: 9687,
      source_in: 401.567833,
      source_out: 404.028625,
      clip_duration: 2.4608,
      target_duration: 2.1833,
      speed_ratio: 1.1271,
      effective_speed: 1.1271,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 28,
      start: 82.8,
      end: 86.883333,
      text: "lui permettant de s'en sortir avant que ses bagues ne soient complètement vidées de leur énergie.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9706,
      source_out_frame: 9796,
      source_in: 404.821083,
      source_out: 408.574833,
      clip_duration: 3.7537,
      target_duration: 4.0833,
      speed_ratio: 0.9193,
      effective_speed: 0.9193,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 29,
      start: 86.883333,
      end: 88.833333,
      text: "Même si ses bagues n'étaient pas à usage unique,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 9795,
      source_out_frame: 9843,
      source_in: 408.533125,
      source_out: 410.535125,
      clip_duration: 2.002,
      target_duration: 1.95,
      speed_ratio: 1.0267,
      effective_speed: 1.0267,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 30,
      start: 88.833333,
      end: 94.25,
      text: "elles devaient être rechargées en pouvoir magique pour fonctionner. Et la quantité de magie nécessaire était colossale.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 10018,
      source_out_frame: 10130,
      source_in: 417.834083,
      source_out: 422.505417,
      clip_duration: 4.6713,
      target_duration: 5.4167,
      speed_ratio: 0.8624,
      effective_speed: 0.8624,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 31,
      start: 94.25,
      end: 98.133333,
      text: "Si son autre amie d'enfance était encore là, ça n'aurait été qu'une formalité.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 10310,
      source_out_frame: 10418,
      source_in: 430.012917,
      source_out: 434.517417,
      clip_duration: 4.5045,
      target_duration: 3.8833,
      speed_ratio: 1.16,
      effective_speed: 1.16,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 32,
      start: 98.133333,
      end: 99.55,
      text: "Mais vu la tournure des événements,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 10564,
      source_out_frame: 10598,
      source_in: 440.606833,
      source_out: 442.024917,
      clip_duration: 1.4181,
      target_duration: 1.4167,
      speed_ratio: 1.001,
      effective_speed: 1.001,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 33,
      start: 99.55,
      end: 102.466667,
      text: "il craignait de ne pas pouvoir tenir jusqu'à son retour.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 10802,
      source_out_frame: 10886,
      source_in: 450.533417,
      source_out: 454.036917,
      clip_duration: 3.5035,
      target_duration: 2.9167,
      speed_ratio: 1.2012,
      effective_speed: 1.2012,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 34,
      start: 102.466667,
      end: 104.45,
      text: "Alors qu'il était complètement désemparé,",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 11569,
      source_out_frame: 11605,
      source_in: 482.523708,
      source_out: 484.025208,
      clip_duration: 1.5015,
      target_duration: 1.9833,
      speed_ratio: 0.7571,
      effective_speed: 0.7571,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 35,
      start: 104.45,
      end: 106.133333,
      text: "son amie a eu une idée pour l'aider.",
      clipName: "[EMBER] Nageki no Bourei wa Intai shitai - 10",
      source_in_frame: 11768,
      source_out_frame: 11809,
      source_in: 490.823667,
      source_out: 492.533708,
      clip_duration: 1.71,
      target_duration: 1.6833,
      speed_ratio: 1.0159,
      effective_speed: 1.0159,
      leaves_gap: false,
      used_alternative: false,
    },
  ];

  // ========================================================================
  // 2. LOGGING & UTILS
  // ========================================================================
  function log(msg) {
    $.writeln("[ATR] " + msg);
  }
  function sleep(ms) {
    $.sleep(ms);
  }
  var TICKS_PER_SECOND = 254016000000; // Premiere Pro timebase constant
  var SEQ_FPS = 60; // TikTok preset is 60fps
  var SOURCE_FPS_NUM = 24000;
  var SOURCE_FPS_DEN = 1001;
  var TICKS_PER_FRAME = TICKS_PER_SECOND / SEQ_FPS;
  var TRACK_ITEM_WAIT_STEP_MS = 15;
  var TRACK_ITEM_WAIT_MAX_STEP_MS = 45;
  var TRACK_ITEM_WAIT_STEP_BACKOFF_MS = 10;
  var TRACK_ITEM_WAIT_MAX_MS = 180;
  var SPEED_RETRY_FAST_WAIT_MS = 12;
  var SPEED_RETRY_LONG_WAIT_MS = 60;
  var PROJECT_ITEM_CACHE = {};
  var PROJECT_ITEM_CACHE_WARMED = false;
  var PRESET_FILE_TEXT_CACHE = {};
  var PRESET_PARSED_DATA_CACHE = {};
  var PRESET_EFFECT_VALUE_ENTRIES_CACHE = {};
  var LUMETRI_PRESET_VALUES_CACHE = {};
  var LUMETRI_PRESET_ARB_STRINGS_CACHE = {};
  var LUMETRI_LOOK_PATH_CACHE = {};
  var VIDEO_EFFECT_RESOLVE_CACHE = {};
  var KNOWN_MEDIA_EXTENSIONS = {
    ".mkv": true,
    ".mp4": true,
    ".mov": true,
    ".avi": true,
    ".webm": true,
    ".m4v": true,
    ".wav": true,
    ".mp3": true,
    ".m4a": true,
    ".aac": true,
    ".flac": true,
    ".ogg": true,
    ".aiff": true,
    ".aif": true,
  };
  var PERF_PROFILE_ENABLED = true;
  var PERF_LOG_EACH_SUBTITLE_BATCH = 50;
  var MUTATE_TRANSIENT_A2_SCENE_AUDIO = false;
  var AUDIO_GAIN_RAW_REFERENCE_AT_0DB = null;
  var AUDIO_GAIN_RAW_FALLBACK_AT_0DB = 0.1778279410038923;
  var SETVALUE_UPDATE_UI = false;
  var QE_EFFECT_VERIFY_SAMPLE_CLIPS = 3;
  var QE_EFFECT_VERIFY_WAIT_STEP_MS = 5;
  var QE_EFFECT_VERIFY_WAIT_MAX_MS = 10;
  var PERF_TIMERS = {};
  var PERF_PHASE_TOTALS = {};
  var PERF_COUNTERS = {};
  var QE_TRACK_RESOLVE_CACHE = {};
  var QE_TRACK_ITEM_HINTS = {};

  function perfNowMs() {
    return new Date().getTime();
  }

  function perfStart(key) {
    if (!PERF_PROFILE_ENABLED || !key) return;
    PERF_TIMERS[key] = perfNowMs();
  }

  function perfEnd(key, label) {
    if (!PERF_PROFILE_ENABLED || !key) return 0;
    var start = PERF_TIMERS[key];
    if (typeof start !== "number") return 0;
    var elapsed = perfNowMs() - start;
    delete PERF_TIMERS[key];

    if (PERF_PHASE_TOTALS[key] === undefined) PERF_PHASE_TOTALS[key] = 0;
    PERF_PHASE_TOTALS[key] += elapsed;
    if (label) {
      log("[PERF] " + label + ": " + elapsed + " ms");
    }
    return elapsed;
  }

  function perfCounterInc(key, amount) {
    if (!PERF_PROFILE_ENABLED || !key) return;
    var delta = typeof amount === "number" ? amount : 1;
    if (PERF_COUNTERS[key] === undefined) PERF_COUNTERS[key] = 0;
    PERF_COUNTERS[key] += delta;
  }

  function perfLogSummary() {
    if (!PERF_PROFILE_ENABLED) return;
    var order = [
      "total",
      "purge",
      "preload",
      "scenes",
      "scenes_placement",
      "scenes_speed",
      "scenes_scale",
      "music",
      "presets",
      "presets_v1",
      "presets_v3",
      "presets_v5",
      "subtitles",
    ];
    log("----- PERF SUMMARY -----");
    for (var i = 0; i < order.length; i++) {
      var k = order[i];
      if (PERF_PHASE_TOTALS[k] !== undefined) {
        log("[PERF] " + k + ": " + PERF_PHASE_TOTALS[k] + " ms");
      }
    }
    var counterOrder = [
      "qeEffectApplyCalls",
      "qeEffectApplyFailures",
      "qeEffectFallbackSearches",
      "qeEffectVerifySleepMs",
      "qeEffectPreMappedItems",
      "qeEffectContextReusedItems",
      "importMGTCalls",
      "speedApplyCalls",
      "speedApplyFullScans",
      "clearSelectionSelectionItems",
      "clearSelectionFallbackTrackScans",
    ];
    log("----- PERF COUNTERS -----");
    for (var j = 0; j < counterOrder.length; j++) {
      var cKey = counterOrder[j];
      if (PERF_COUNTERS[cKey] !== undefined) {
        log("[PERF] " + cKey + ": " + PERF_COUNTERS[cKey]);
      }
    }
    log("------------------------");
  }

  function snapSecondsToFrame(sec) {
    // Add small epsilon to prevent floating-point rounding errors
    // Values like 17.583333 (representing 1055/60) become 1054.99998 due to float precision
    // Without epsilon, this can cause 1-frame drift when truncated elsewhere
    return Math.round(sec * SEQ_FPS + 0.0001) / SEQ_FPS;
  }

  function secondsToTicks(sec) {
    // Snap to frame boundaries to avoid 1-frame drift
    // Add small epsilon to prevent floating-point rounding errors (same as snapSecondsToFrame)
    return Math.round(sec * SEQ_FPS + 0.0001) * TICKS_PER_FRAME;
  }

  function secondsToRawTicks(sec) {
    // Raw tick conversion (no sequence-frame snap), used for source in/out.
    return Math.round(sec * TICKS_PER_SECOND);
  }

  function sourceFramesToRawTicks(frame) {
    // Exact source frame -> ticks conversion (avoids decimal precision drift).
    return Math.round(
      (frame * TICKS_PER_SECOND * SOURCE_FPS_DEN) / SOURCE_FPS_NUM,
    );
  }

  function sourceFrameDurationTicks() {
    return (TICKS_PER_SECOND * SOURCE_FPS_DEN) / SOURCE_FPS_NUM;
  }

  function buildSequenceTimeFromSeconds(sec) {
    var t = new Time();
    try {
      // 2025: ticks might need to be a String or Number.
      // Safe to assign Number, PPro handles it.
      t.ticks = secondsToTicks(sec).toString();
    } catch (e) {
      t.seconds = sec;
    }
    return t;
  }

  function buildRawTimeFromSeconds(sec) {
    var t = new Time();
    try {
      t.ticks = secondsToRawTicks(sec).toString();
    } catch (e) {
      t.seconds = sec;
    }
    return t;
  }

  function buildRawTimeFromSourceFrame(frame, centerInFrame) {
    var t = new Time();
    var center = !!centerInFrame;
    var frameTicks = sourceFrameDurationTicks();
    var targetTicks =
      sourceFramesToRawTicks(frame) + (center ? Math.floor(frameTicks / 2) : 0);
    try {
      t.ticks = Math.round(targetTicks).toString();
    } catch (e) {
      // Fallback should be unreachable with valid frame numbers.
      t.ticks = secondsToRawTicks(
        (frame * SOURCE_FPS_DEN) / SOURCE_FPS_NUM,
      ).toString();
    }
    return t;
  }

  function getStartTicks(item) {
    if (!item || !item.start) return null;
    // PPro 2024/2025: .ticks is often a String. Parse it!
    if (item.start.ticks !== undefined) {
      var val = parseInt(item.start.ticks, 10);
      if (!isNaN(val)) return val;
    }
    // Fallback
    if (typeof item.start.seconds === "number") {
      return secondsToTicks(item.start.seconds);
    }
    return null;
  }

  function stripKnownExtension(name) {
    var txt = name ? name.toString() : "";
    var trimmed = txt.replace(/^\s+|\s+$/g, "");
    var dotPos = trimmed.lastIndexOf(".");
    if (dotPos <= 0) return trimmed;
    var ext = trimmed.substring(dotPos).toLowerCase();
    if (!KNOWN_MEDIA_EXTENSIONS[ext]) return trimmed;
    return trimmed.substring(0, dotPos);
  }

  function normalizeLooseName(name) {
    var txt = stripKnownExtension(name).toLowerCase();
    if (!txt) return "";
    txt = txt.replace(/[\u2018\u2019\u0060']/g, "_");
    txt = txt.replace(/[\\\/:\*\?"<>\|]+/g, "_");
    txt = txt.replace(/\s+/g, "_");
    txt = txt.replace(/_+/g, "_");
    txt = txt.replace(/^_+|_+$/g, "");
    return txt;
  }

  function normalizeNameKey(name) {
    if (!name) return "";
    return normalizeLooseName(name);
  }

  function cacheProjectItemByName(name, item) {
    if (!name || !item) return;
    var key = normalizeNameKey(name);
    if (!key) return;
    PROJECT_ITEM_CACHE[key] = item;
  }

  function cacheProjectItem(item) {
    if (!item || !item.name) return;
    var itemName = item.name.toString();
    cacheProjectItemByName(itemName, item);
    cacheProjectItemByName(stripKnownExtension(itemName), item);
  }

  function getCachedProjectItem(name) {
    var key = normalizeNameKey(name);
    if (!key) return null;
    var item = PROJECT_ITEM_CACHE[key];
    if (!item) return null;
    try {
      // Touch one property to validate stale object refs.
      var _ = item.name;
      return item;
    } catch (e) {
      delete PROJECT_ITEM_CACHE[key];
      return null;
    }
  }

  function warmProjectItemCache() {
    if (PROJECT_ITEM_CACHE_WARMED || !app.project || !app.project.rootItem)
      return;
    var walk = function (bin) {
      if (!bin || !bin.children) return;
      for (var i = 0; i < bin.children.numItems; i++) {
        var item = bin.children[i];
        if (!item) continue;
        if (item.type !== ProjectItemType.BIN) {
          cacheProjectItem(item);
        }
        if (item.type === ProjectItemType.BIN) {
          walk(item);
        }
      }
    };
    walk(app.project.rootItem);
    PROJECT_ITEM_CACHE_WARMED = true;
  }

  function isItemNameMatch(itemName, nameRef) {
    if (!nameRef) return true;
    var itemNameNorm = itemName ? itemName.toString() : "";
    var nameRefNorm = nameRef ? nameRef.toString() : "";
    if (itemNameNorm.replace(/\s/g, "") === "") return false;

    if (itemNameNorm.indexOf(nameRefNorm) !== -1) return true;
    if (nameRefNorm.indexOf(itemNameNorm) !== -1) return true;

    var itemLoose = normalizeLooseName(itemNameNorm);
    var refLoose = normalizeLooseName(nameRefNorm);
    if (!itemLoose || !refLoose) return false;
    if (itemLoose === refLoose) return true;
    if (itemLoose.indexOf(refLoose) !== -1) return true;
    if (refLoose.indexOf(itemLoose) !== -1) return true;
    return false;
  }

  function isTrackItemMatch(item, targetTicks, toleranceTicks, nameRef) {
    if (!item) return false;
    var itemTicks = getStartTicks(item);
    if (itemTicks === null) return false;
    if (Math.abs(itemTicks - targetTicks) > toleranceTicks) return false;
    if (!nameRef) return true;
    var itemName = item.name ? item.name.toString() : "";
    return isItemNameMatch(itemName, nameRef);
  }

  function findTrackItemAtStart(track, startSeconds, nameRef) {
    if (!track || !track.clips) return null;

    var targetTicks = secondsToTicks(startSeconds);
    // Relaxed tolerance
    var toleranceTicks = secondsToTicks(0.1);

    var bestItem = null;
    var minDiff = toleranceTicks + 1;

    for (var i = 0; i < track.clips.numItems; i++) {
      var item = track.clips[i];
      if (!item) continue;
      var itemTicks = getStartTicks(item);
      if (itemTicks === null) continue;
      var diff = Math.abs(itemTicks - targetTicks);
      if (diff <= toleranceTicks) {
        if (nameRef) {
          var itemName = item.name ? item.name.toString() : "";
          if (!isItemNameMatch(itemName, nameRef)) continue;
        }
        if (diff < minDiff) {
          minDiff = diff;
          bestItem = item;
        }
      }
    }
    return bestItem;
  }

  function findRecentTrackItemAtStart(track, startSeconds, nameRef) {
    if (!track || !track.clips || track.clips.numItems <= 0) return null;
    var targetTicks = secondsToTicks(startSeconds);
    var toleranceTicks = secondsToTicks(0.2);
    var tailWindow = 8;
    var startIdx = track.clips.numItems - 1;
    var stopIdx = Math.max(0, startIdx - tailWindow + 1);

    for (var i = startIdx; i >= stopIdx; i--) {
      var item = track.clips[i];
      if (isTrackItemMatch(item, targetTicks, toleranceTicks, nameRef)) {
        return item;
      }
    }
    return null;
  }

  function waitForTrackItemAtStart(track, startSeconds, nameRef, maxWaitMs) {
    if (!track) return null;
    var timeout =
      typeof maxWaitMs === "number" ? maxWaitMs : TRACK_ITEM_WAIT_MAX_MS;
    var waited = 0;
    var waitStep = TRACK_ITEM_WAIT_STEP_MS;
    var item = findRecentTrackItemAtStart(track, startSeconds, nameRef);
    if (item) return item;
    item = findTrackItemAtStart(track, startSeconds, nameRef);
    if (item) return item;

    while (waited < timeout) {
      var sleepMs = Math.min(waitStep, timeout - waited);
      if (sleepMs <= 0) break;
      sleep(sleepMs);
      waited += sleepMs;
      item = findRecentTrackItemAtStart(track, startSeconds, nameRef);
      if (item) return item;
      if (waitStep < TRACK_ITEM_WAIT_MAX_STEP_MS) {
        waitStep = Math.min(
          TRACK_ITEM_WAIT_MAX_STEP_MS,
          waitStep + TRACK_ITEM_WAIT_STEP_BACKOFF_MS,
        );
      }
    }
    return findTrackItemAtStart(track, startSeconds, nameRef);
  }

  function resolvePlacedItemFast(track, startSeconds, nameRef, maxWaitMs) {
    if (!track || !track.clips || track.clips.numItems <= 0) {
      return waitForTrackItemAtStart(track, startSeconds, nameRef, maxWaitMs);
    }
    var targetTicks = secondsToTicks(startSeconds);
    var toleranceTicks = secondsToTicks(0.2);
    var lastIdx = track.clips.numItems - 1;

    var lastItem = track.clips[lastIdx];
    if (isTrackItemMatch(lastItem, targetTicks, toleranceTicks, nameRef)) {
      return lastItem;
    }

    var tailWindow = 8;
    var stopIdx = Math.max(0, lastIdx - tailWindow + 1);
    for (var i = lastIdx - 1; i >= stopIdx; i--) {
      var item = track.clips[i];
      if (isTrackItemMatch(item, targetTicks, toleranceTicks, nameRef)) {
        return item;
      }
    }
    return waitForTrackItemAtStart(track, startSeconds, nameRef, maxWaitMs);
  }

  function setTrackItemInOutFromItem(
    item,
    inSeconds,
    outSeconds,
    inFrame,
    outFrame,
  ) {
    if (!item) return null;
    try {
      if (typeof inFrame === "number" && typeof outFrame === "number") {
        // In-point is nudged to frame center to avoid boundary rounding to previous frame.
        item.inPoint = buildRawTimeFromSourceFrame(inFrame, true);
        // Out-point stays on frame boundary (exclusive end frame).
        item.outPoint = buildRawTimeFromSourceFrame(outFrame, false);
      } else {
        item.inPoint = buildRawTimeFromSeconds(inSeconds);
        item.outPoint = buildRawTimeFromSeconds(outSeconds);
      }
    } catch (e) {
      return null;
    }
    return item;
  }

  function enforceTrackItemDuration(item, durationSeconds) {
    if (!item || typeof durationSeconds !== "number") return;
    try {
      var startSec = null;
      if (item.start && typeof item.start.seconds === "number") {
        startSec = item.start.seconds;
      } else {
        var startTicks = getStartTicks(item);
        if (typeof startTicks === "number") {
          startSec = startTicks / TICKS_PER_SECOND;
        }
      }
      if (typeof startSec === "number") {
        item.end = buildSequenceTimeFromSeconds(startSec + durationSeconds);
      }
    } catch (e) {}
  }

  function timeObjectToSeconds(timeObj) {
    if (!timeObj) return null;
    try {
      if (timeObj.ticks !== undefined) {
        var ticksVal = parseInt(timeObj.ticks, 10);
        if (!isNaN(ticksVal)) return ticksVal / TICKS_PER_SECOND;
      }
    } catch (e0) {}
    try {
      if (typeof timeObj.seconds === "number") return timeObj.seconds;
      if (typeof timeObj.secs === "number") return timeObj.secs;
    } catch (e1) {}
    return null;
  }

  function getTrackItemStartSeconds(item) {
    if (!item) return null;
    var startSec = timeObjectToSeconds(item.start);
    if (typeof startSec === "number") return startSec;
    var startTicks = getStartTicks(item);
    if (typeof startTicks === "number") return startTicks / TICKS_PER_SECOND;
    return null;
  }

  function getTrackItemEndSeconds(item) {
    if (!item) return null;
    var endSec = timeObjectToSeconds(item.end);
    if (typeof endSec === "number") return endSec;

    var startSec = getTrackItemStartSeconds(item);
    if (typeof startSec !== "number") return null;

    var durSec = null;
    try {
      if (item.duration && typeof item.duration.seconds === "number") {
        durSec = item.duration.seconds;
      } else if (item.duration && item.duration.ticks !== undefined) {
        var durTicks = parseInt(item.duration.ticks, 10);
        if (!isNaN(durTicks)) durSec = durTicks / TICKS_PER_SECOND;
      }
    } catch (e0) {}
    if (typeof durSec === "number") return startSec + durSec;
    return null;
  }

  function setTrackItemEndSeconds(item, endSec) {
    if (!item || typeof endSec !== "number") return false;
    try {
      item.end = endSec;
      return true;
    } catch (e0) {}
    try {
      item.end = buildSequenceTimeFromSeconds(endSec);
      return true;
    } catch (e1) {}
    return false;
  }

  function getMotionComponent(item) {
    if (!item || !item.components) return null;
    var fallback =
      item.components.numItems > 1 ? item.components[1] : item.components[0];
    for (var c = 0; c < item.components.numItems; c++) {
      var comp = item.components[c];
      if (!comp || !comp.displayName) continue;
      if (comp.displayName === "Motion" || comp.displayName === "Trajectoire") {
        return comp;
      }
    }
    return fallback || null;
  }

  function setPropertyValueFast(prop, value) {
    if (!prop || !prop.setValue) return false;
    try {
      prop.setValue(value, SETVALUE_UPDATE_UI ? 1 : 0);
      return true;
    } catch (e0) {}
    try {
      prop.setValue(value);
      return true;
    } catch (e1) {}
    return false;
  }

  function refreshSequenceUI(sequence) {
    if (SETVALUE_UPDATE_UI || !sequence) return;
    try {
      if (sequence.getPlayerPosition && sequence.setPlayerPosition) {
        var pos = sequence.getPlayerPosition();
        if (pos && pos.ticks !== undefined) {
          sequence.setPlayerPosition(pos.ticks.toString());
        }
      }
    } catch (e0) {}
  }

  function setScaleOnItem(item, scaleVal) {
    if (!item) return false;
    var motion = getMotionComponent(item);
    if (!motion || !motion.properties) return false;
    for (var p = 0; p < motion.properties.numItems; p++) {
      var prop = motion.properties[p];
      if (!prop || !prop.displayName) continue;
      if (
        prop.displayName === "Scale" ||
        prop.displayName === "Echelle" ||
        prop.displayName === "\u00c9chelle"
      ) {
        if (setPropertyValueFast(prop, scaleVal)) {
          return true;
        }
        return false;
      }
    }
    return false;
  }

  function logClipDuration(item, targetSeconds, label) {
    if (!item || !label) return;
    try {
      var dur = null;
      if (item.duration && typeof item.duration.seconds === "number") {
        dur = item.duration.seconds;
      } else if (item.duration && typeof item.duration.ticks === "number") {
        dur = item.duration.ticks / TICKS_PER_SECOND;
      }
      if (typeof dur === "number") {
        log(
          label +
            " duration " +
            dur.toFixed(4) +
            "s (target " +
            targetSeconds.toFixed(4) +
            "s)",
        );
      }
    } catch (e) {}
  }

  function findProjectItem(name) {
    var cached = getCachedProjectItem(name);
    if (cached) return cached;
    var findInBin = function (bin) {
      for (var i = 0; i < bin.children.numItems; i++) {
        var item = bin.children[i];
        if (item.name === name && item.type !== ProjectItemType.BIN) {
          cacheProjectItem(item);
          return item;
        }
        if (item.type === ProjectItemType.BIN) {
          var found = findInBin(item);
          if (found) return found;
        }
      }
      return null;
    };
    return findInBin(app.project.rootItem);
  }

  function findProjectItemLoose(name) {
    var target = normalizeLooseName(name);
    if (!target) return null;
    var findInBin = function (bin) {
      for (var i = 0; i < bin.children.numItems; i++) {
        var item = bin.children[i];
        if (!item) continue;
        if (item.type !== ProjectItemType.BIN) {
          var itemName = item.name ? item.name.toString() : "";
          if (normalizeLooseName(itemName) === target) {
            cacheProjectItem(item);
            return item;
          }
        }
        if (item.type === ProjectItemType.BIN) {
          var found = findInBin(item);
          if (found) return found;
        }
      }
      return null;
    };
    return findInBin(app.project.rootItem);
  }

  function resolveClipFilePath(cleanName, nameNoExt) {
    var searchPaths = [
      ROOT_DIR + "/" + cleanName,
      ROOT_DIR + "/" + nameNoExt,
      ROOT_DIR + "/" + cleanName + ".wav",
      SOURCES_DIR + "/" + cleanName,
      SOURCES_DIR + "/" + nameNoExt,
      SOURCES_DIR + "/" + nameNoExt + ".mkv",
      SOURCES_DIR + "/" + nameNoExt + ".mp4",
      SOURCES_DIR + "/" + nameNoExt + ".mov",
      SOURCES_DIR + "/" + nameNoExt + ".avi",
      SOURCES_DIR + "/" + nameNoExt + ".webm",
      SOURCES_DIR + "/" + nameNoExt + ".m4v",
      SOURCES_DIR + "/" + nameNoExt + ".wav",
      SOURCES_DIR + "/" + nameNoExt + ".mp3",
    ];
    for (var i = 0; i < searchPaths.length; i++) {
      var f = new File(searchPaths[i]);
      if (f.exists) return f;
    }
    return null;
  }

  function hasProjectItemForName(name) {
    if (!name) return false;
    var cleanName = name.toString().replace(/^\s+|\s+$/g, "");
    if (!cleanName) return false;
    var nameNoExt = stripKnownExtension(cleanName);
    return !!(
      getCachedProjectItem(cleanName) || getCachedProjectItem(nameNoExt)
    );
  }

  function preloadProjectItemsBatch(nameMap) {
    var stats = {
      requested: 0,
      importBatchCount: 0,
      resolved: 0,
      unresolved: 0,
    };
    if (!nameMap) return stats;

    var pending = [];
    var importPaths = [];
    var seenPaths = {};

    for (var nm in nameMap) {
      if (!nameMap.hasOwnProperty(nm)) continue;
      stats.requested++;
      var cleanName = nm.toString().replace(/^\s+|\s+$/g, "");
      var nameNoExt = stripKnownExtension(cleanName);

      var item =
        getCachedProjectItem(cleanName) ||
        getCachedProjectItem(nameNoExt) ||
        findProjectItem(cleanName) ||
        findProjectItem(nameNoExt) ||
        findProjectItemLoose(cleanName) ||
        findProjectItemLoose(nameNoExt);
      if (item) {
        cacheProjectItem(item);
        cacheProjectItemByName(cleanName, item);
        cacheProjectItemByName(nameNoExt, item);
        stats.resolved++;
        continue;
      }

      var f = resolveClipFilePath(cleanName, nameNoExt);
      if (!f) {
        stats.unresolved++;
        continue;
      }

      pending.push({
        cleanName: cleanName,
        nameNoExt: nameNoExt,
        fileName: f.name,
        displayName: f.displayName,
      });

      if (!seenPaths[f.fsName]) {
        seenPaths[f.fsName] = true;
        importPaths.push(f.fsName);
      }
    }

    if (importPaths.length > 0) {
      app.project.importFiles(importPaths, true, app.project.rootItem, false);
      stats.importBatchCount = importPaths.length;
    }

    // Re-cache once after batch import for faster lookups.
    PROJECT_ITEM_CACHE_WARMED = false;
    warmProjectItemCache();

    for (var i = 0; i < pending.length; i++) {
      var p = pending[i];
      var item = null;
      var retries = 0;
      while (!item && retries < 4) {
        item =
          getCachedProjectItem(p.cleanName) ||
          getCachedProjectItem(p.nameNoExt) ||
          findProjectItem(p.fileName) ||
          findProjectItem(p.displayName) ||
          findProjectItem(p.cleanName) ||
          findProjectItem(p.nameNoExt) ||
          findProjectItem(stripKnownExtension(p.fileName)) ||
          findProjectItemLoose(p.fileName) ||
          findProjectItemLoose(p.displayName) ||
          findProjectItemLoose(p.cleanName) ||
          findProjectItemLoose(p.nameNoExt);
        if (!item) {
          sleep(20);
          retries++;
        }
      }
      if (item) {
        cacheProjectItem(item);
        cacheProjectItemByName(p.cleanName, item);
        cacheProjectItemByName(p.nameNoExt, item);
        stats.resolved++;
      } else {
        stats.unresolved++;
      }
    }

    return stats;
  }

  function getOrImportClip(clipName) {
    var cleanName = clipName.replace(/^\s+|\s+$/g, "");
    var nameNoExt = stripKnownExtension(cleanName);

    var item = getCachedProjectItem(cleanName);
    if (item) return item;
    item = getCachedProjectItem(nameNoExt);
    if (item) return item;

    item = findProjectItem(cleanName);
    if (item) return item;
    item = findProjectItem(nameNoExt);
    if (item) return item;
    item = findProjectItemLoose(cleanName);
    if (item) return item;
    item = findProjectItemLoose(nameNoExt);
    if (item) return item;

    var f = resolveClipFilePath(cleanName, nameNoExt);
    if (f) {
      app.project.importFiles([f.fsName], true, app.project.rootItem, false);
      // Small bounded retry; import indexing can be async.
      var retries = 0;
      while (!item && retries < 8) {
        item = findProjectItem(f.name);
        if (!item) item = findProjectItem(f.displayName);
        if (!item) item = findProjectItem(nameNoExt);
        if (!item) item = findProjectItem(stripKnownExtension(f.name));
        if (!item) item = findProjectItemLoose(f.name);
        if (!item) item = findProjectItemLoose(f.displayName);
        if (!item) item = findProjectItemLoose(cleanName);
        if (!item) item = findProjectItemLoose(nameNoExt);
        if (!item) {
          sleep(40);
          retries++;
        }
      }
      if (item) {
        cacheProjectItem(item);
        cacheProjectItemByName(cleanName, item);
        cacheProjectItemByName(nameNoExt, item);
      }
      return item;
    }
    log("Error: Clip not found: " + cleanName);
    return null;
  }

  function buildRawAudioSubclipName(scene) {
    if (!scene) return "";
    return (
      "__ATR_RAW_AUDIO__" +
      scene.scene_index +
      "__" +
      scene.source_in_frame +
      "_" +
      scene.source_out_frame
    );
  }

  function getOrCreateRawAudioSubclip(scene) {
    if (!scene || !scene.is_raw) return null;
    var subclipName = buildRawAudioSubclipName(scene);
    if (!subclipName) return null;

    var existing =
      getCachedProjectItem(subclipName) ||
      findProjectItem(subclipName) ||
      findProjectItemLoose(subclipName);
    if (existing) {
      cacheProjectItem(existing);
      cacheProjectItemByName(subclipName, existing);
      return existing;
    }

    var sourceItem = getOrImportClip(scene.clipName);
    if (!sourceItem || !sourceItem.createSubClip) {
      return null;
    }

    var startTicks =
      typeof scene.source_in_frame === "number"
        ? Math.round(sourceFramesToRawTicks(scene.source_in_frame))
        : Math.round(secondsToRawTicks(scene.source_in));
    var endTicks =
      typeof scene.source_out_frame === "number"
        ? Math.round(sourceFramesToRawTicks(scene.source_out_frame))
        : Math.round(secondsToRawTicks(scene.source_out));
    if (!(endTicks > startTicks)) {
      endTicks = startTicks + Math.round(sourceFrameDurationTicks());
    }

    var subclip = null;
    try {
      // ProjectItem.createSubClip(name, startTicks, endTicks, hasHardBoundaries, takeVideo, takeAudio)
      subclip = sourceItem.createSubClip(
        subclipName,
        startTicks.toString(),
        endTicks.toString(),
        1,
        0,
        1,
      );
    } catch (e0) {}

    if (!subclip) {
      subclip =
        findProjectItem(subclipName) || findProjectItemLoose(subclipName);
    }
    if (subclip) {
      cacheProjectItem(subclip);
      cacheProjectItemByName(subclipName, subclip);
    }
    return subclip;
  }

  // ========================================================================
  // 3. MAIN LOGIC
  // ========================================================================
  function main() {
    PERF_TIMERS = {};
    PERF_PHASE_TOTALS = {};
    PERF_COUNTERS = {};
    PRESET_PARSED_DATA_CACHE = {};
    VIDEO_EFFECT_RESOLVE_CACHE = {};
    QE_TRACK_RESOLVE_CACHE = {};
    QE_TRACK_ITEM_HINTS = {};
    perfStart("total");

    app.enableQE();
    if (!app.project) {
      alert("Open a project.");
      return;
    }
    log("Purging project to start fresh...");
    perfStart("purge");
    if (!purgeProjectCompletely()) {
      perfEnd("purge", "Purge");
      alert("Error: Could not fully purge the project. Aborting.");
      return;
    }
    perfEnd("purge", "Purge");

    var seqName = "ATR_Layered_" + Math.floor(Math.random() * 9999);
    var presetFile = new File(SEQUENCE_PRESET_PATH);
    var sequence;

    if (presetFile.exists) {
      qe.project.newSequence(seqName, presetFile.fsName);
      sequence = app.project.activeSequence;
    } else {
      sequence = app.project.createNewSequence(seqName, "ID_1");
    }

    // --- ENSURE TRACKS (V=6, A=4) ---
    // A4 is used for raw scene audio (active, not muted)
    ensureVideoTracks(sequence, 6);
    ensureAudioTracks(sequence, 4);
    var qeSeq = null;
    try {
      qeSeq = qe.project.getActiveSequence();
    } catch (eQe) {}
    var qeTrackCache = {};

    // Mapping Tracks
    // V1: Index 0 (Back)
    // V2: Index 1 (Border)
    // V3: Index 2 (Main)
    // V4: Index 3 (Subs)
    // V5: Index 4 (Reserved)
    // V6: Index 5 (Reserved)
    // A1: Index 0 (Source audio muted)
    // A2: Index 1 (TTS)
    // A3: Index 2 (Music bed)
    // A4: Index 3 (Raw scene audio - active)

    var v1 = sequence.videoTracks[0];
    var v2 =
      sequence.videoTracks.numTracks > 1 ? sequence.videoTracks[1] : null;
    var v3 =
      sequence.videoTracks.numTracks > 2 ? sequence.videoTracks[2] : null;
    var v4 =
      sequence.videoTracks.numTracks > 3 ? sequence.videoTracks[3] : null;
    var v5 =
      sequence.videoTracks.numTracks > 4 ? sequence.videoTracks[4] : null;
    var v6 =
      sequence.videoTracks.numTracks > 5 ? sequence.videoTracks[5] : null;
    var a1 = sequence.audioTracks[0];
    var a2 = sequence.audioTracks.numTracks > 1 ? sequence.audioTracks[1] : a1;
    var a3 =
      sequence.audioTracks.numTracks > 2 ? sequence.audioTracks[2] : null;
    var a4 =
      sequence.audioTracks.numTracks > 3 ? sequence.audioTracks[3] : null;

    // --- MUTE A1 (Clip Audio) ---
    try {
      a1.setMute(1);
    } catch (e) {}

    // Warm cache once, then preload only the source clips we will use.
    perfStart("preload");
    warmProjectItemCache();
    var preloadNames = {};
    for (var i = 0; i < scenes.length; i++) {
      preloadNames[scenes[i].clipName] = true;
    }
    preloadNames[AUDIO_FILENAME] = true;
    preloadNames[CATEGORY_OVERLAY_FILENAME] = true;
    preloadNames[TITLE_OVERLAY_FILENAME] = true;
    if (trimSpaces(MUSIC_FILENAME) !== "") {
      preloadNames[MUSIC_FILENAME] = true;
    }
    var preloadStats = preloadProjectItemsBatch(preloadNames);
    for (var preloadName in preloadNames) {
      if (preloadNames.hasOwnProperty(preloadName)) {
        if (!hasProjectItemForName(preloadName)) {
          getOrImportClip(preloadName);
        }
      }
    }
    if (PERF_PROFILE_ENABLED) {
      log(
        "[PERF] Preload batch: requested " +
          preloadStats.requested +
          ", imported " +
          preloadStats.importBatchCount +
          ", resolved " +
          preloadStats.resolved +
          ", unresolved " +
          preloadStats.unresolved +
          ".",
      );
    }
    perfEnd("preload", "Preload");

    // --- MARKERS ---
    log("Creating Markers...");
    for (var i = 0; i < scenes.length; i++) {
      var mStart = snapSecondsToFrame(scenes[i].start);
      var mEnd = snapSecondsToFrame(scenes[i].end);
      var m = sequence.markers.createMarker(mStart);
      m.name = "Scene " + scenes[i].scene_index;
      m.duration = mEnd - mStart;
    }

    // --- INTERLEAVED PROCESSING (V1 & V3) ---
    // V1 (Background) & V3 (Main)
    log("Processing Scenes (Layering & Speed)...");
    perfStart("scenes");
    var nameCleaner = function (n) {
      return stripKnownExtension(n);
    }; // Helper

    for (var i = 0; i < scenes.length; i++) {
      var s = scenes[i];
      var startSec = snapSecondsToFrame(s.start);
      var clip = getOrImportClip(s.clipName);
      var cleanName = nameCleaner(s.clipName);

      if (clip) {
        perfStart("scenes_placement");
        // 1. PLACE ON V3 (Main)
        if (v3) v3.overwriteClip(clip, startSec);

        // 2. PLACE ON V1 (Background)
        if (v1) v1.overwriteClip(clip, startSec);

        // 2b. SET PER-INSTANCE IN/OUT (TrackItem) TO AVOID UNIT AMBIGUITY
        var v3Item = null;
        var v1Item = null;
        var a1Item = null;
        var a2Item = null;
        if (v3) {
          v3Item = resolvePlacedItemFast(
            v3,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          v3Item = setTrackItemInOutFromItem(
            v3Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }
        if (v1) {
          v1Item = resolvePlacedItemFast(
            v1,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          v1Item = setTrackItemInOutFromItem(
            v1Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }
        if (a1) {
          a1Item = resolvePlacedItemFast(
            a1,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          a1Item = setTrackItemInOutFromItem(
            a1Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }
        if (MUTATE_TRANSIENT_A2_SCENE_AUDIO && a2 && a2 !== a1) {
          a2Item = resolvePlacedItemFast(
            a2,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          a2Item = setTrackItemInOutFromItem(
            a2Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }
        perfEnd("scenes_placement");

        // 3. ENFORCE DURATION
        // Raw scenes stay at native speed but still need an explicit timeline
        // duration so trailing raw clips do not extend past final playback.
        var newDurationSeconds = snapSecondsToFrame(s.target_duration);
        enforceTrackItemDuration(v3Item, newDurationSeconds);
        enforceTrackItemDuration(v1Item, newDurationSeconds);
        enforceTrackItemDuration(a1Item, newDurationSeconds);
        if (MUTATE_TRANSIENT_A2_SCENE_AUDIO) {
          enforceTrackItemDuration(a2Item, newDurationSeconds);
        }

        // 4. APPLY SPEED (Both V1, V3, A1, and optionally A2)
        // QE setSpeed often fails to ripple-edit duration for speedups, so we pre-resize above.
        if (!s.is_raw && Math.abs(s.effective_speed - 1.0) > 0.01) {
          perfStart("scenes_speed");
          if (v3)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              2,
              "Video",
              cleanName,
              sequence,
              qeSeq,
              qeTrackCache,
              QE_TRACK_ITEM_HINTS,
            );
          if (v1)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              0,
              "Video",
              cleanName,
              sequence,
              qeSeq,
              qeTrackCache,
              QE_TRACK_ITEM_HINTS,
            );
          if (a1 && a1Item)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              0,
              "Audio",
              cleanName,
              sequence,
              qeSeq,
              qeTrackCache,
              QE_TRACK_ITEM_HINTS,
            );
          if (MUTATE_TRANSIENT_A2_SCENE_AUDIO && a2 && a2Item && a2 !== a1)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              1,
              "Audio",
              cleanName,
              sequence,
              qeSeq,
              qeTrackCache,
              QE_TRACK_ITEM_HINTS,
            );
          perfEnd("scenes_speed");
        }

        // 4. APPLY SCALE (Standard API)
        perfStart("scenes_scale");
        if (!setScaleOnItem(v3Item, 75) && v3)
          setScaleAndPosition(v3, startSec, 75); // Main Scaled Down
        if (!setScaleOnItem(v1Item, 183) && v1)
          setScaleAndPosition(v1, startSec, 183); // Background Scaled Up
        perfEnd("scenes_scale");

        if (v3Item) {
          logClipDuration(v3Item, s.target_duration, "Scene " + s.scene_index);
        } else if (v3) {
          var v3ItemForLog = findTrackItemAtStart(v3, startSec, cleanName);
          if (v3ItemForLog) {
            logClipDuration(
              v3ItemForLog,
              s.target_duration,
              "Scene " + s.scene_index,
            );
          }
        }
      }
    }
    perfEnd("scenes", "Scenes");

    // --- IMPORT TTS (A2), THEN BUILD OVERLAYS + MUSIC FROM TTS DURATION ---
    log("Importing TTS to A2...");
    var ttsItem = getOrImportClip(AUDIO_FILENAME);
    if (!a2 || !ttsItem) {
      alert("Error: Missing TTS track or file '" + AUDIO_FILENAME + "'.");
      return;
    }
    a2.overwriteClip(ttsItem, 0);

    // Cleanup all audio tracks except A1 and A2 (TTS)
    cleanupAudioTracks(1, AUDIO_FILENAME);

    var ttsNameNoExt = stripKnownExtension(AUDIO_FILENAME);
    var ttsTrackItem =
      resolvePlacedItemFast(a2, 0, ttsNameNoExt, TRACK_ITEM_WAIT_MAX_MS) ||
      resolvePlacedItemFast(a2, 0, AUDIO_FILENAME, TRACK_ITEM_WAIT_MAX_MS) ||
      findTrackItemAtStart(a2, 0, null);
    var ttsEndSec = ttsTrackItem ? getTrackItemEndSeconds(ttsTrackItem) : null;
    if (typeof ttsEndSec !== "number" || !(ttsEndSec > 0)) {
      alert(
        "Error: Unable to resolve TTS end time from '" + AUDIO_FILENAME + "'.",
      );
      return;
    }
    ttsEndSec = snapSecondsToFrame(ttsEndSec);

    var sequenceEndSec = ttsEndSec;

    // --- V2: BORDER MOGRT ---
    if (v2 && new File(BORDER_MOGRT_PATH).exists) {
      log("Adding Border Mogrt to V2...");
      try {
        if (sequenceEndSec > 0) {
          var mgt = sequence.importMGT(BORDER_MOGRT_PATH, 0, 1, 0);
          if (mgt) {
            setTrackItemEndSeconds(mgt, sequenceEndSec);
            log("Border Mogrt inserted. Duration: " + sequenceEndSec);
          }
        }
      } catch (e) {
        log("Border Mogrt Error: " + e.message);
      }
    }

    duplicateRawSceneAudioToTrack(a4, scenes);

    log("Adding overlays on V5 and V6...");
    if (!placeOverlayOnTrack(v5, CATEGORY_OVERLAY_FILENAME, sequenceEndSec)) {
      log("Warning: Failed to place " + CATEGORY_OVERLAY_FILENAME + " on V5.");
    }
    if (!placeOverlayOnTrack(v6, TITLE_OVERLAY_FILENAME, sequenceEndSec)) {
      log("Warning: Failed to place " + TITLE_OVERLAY_FILENAME + " on V6.");
    }

    var musicFilenameTrimmed = trimSpaces(MUSIC_FILENAME);
    if (musicFilenameTrimmed !== "") {
      log("Adding looped music bed on A3...");
      if (!a3) {
        log("Warning: A3 track is unavailable, skipping music bed.");
      } else {
        var musicItem = getOrImportClip(musicFilenameTrimmed);
        if (!musicItem) {
          log(
            "Warning: Music file '" +
              musicFilenameTrimmed +
              "' not found. Skipping music bed.",
          );
        } else {
          perfStart("music");
          if (!buildLoopedMusicBed(a3, musicItem, sequenceEndSec, MUSIC_GAIN_DB)) {
            log("Warning: Could not fully build looped music bed.");
          }
          perfEnd("music", "Music");
        }
      }
    } else {
      log("Skipping music bed (MUSIC_FILENAME is empty).");
    }

    // --- APPLY VIDEO PRESETS ---
    perfStart("presets");
    log("Applying Background preset on V1...");
    perfStart("presets_v1");
    applyVideoPresetToTrackItems(
      0,
      BACKGROUND_PRESET_NAME,
      BACKGROUND_PRESET_FILE_PATH,
      qeSeq,
      QE_TRACK_RESOLVE_CACHE,
    );
    perfEnd("presets_v1");
    log("Applying Foreground preset on V3...");
    perfStart("presets_v3");
    applyVideoPresetToTrackItems(
      2,
      FOREGROUND_PRESET_NAME,
      FOREGROUND_PRESET_FILE_PATH,
      qeSeq,
      QE_TRACK_RESOLVE_CACHE,
    );
    perfEnd("presets_v3");
    log("Applying Category Title preset on V5...");
    perfStart("presets_v5");
    applyVideoPresetToTrackItems(
      4,
      CATEGORY_TITLE_PRESET_NAME,
      CATEGORY_TITLE_PRESET_FILE_PATH,
      qeSeq,
      QE_TRACK_RESOLVE_CACHE,
    );
    perfEnd("presets_v5");
    perfEnd("presets", "Presets");

    // --- V4: SUBTITLES (SRT timings + external MOGRT files) ---
    log("Loading subtitle MOGRT files to V4...");
    perfStart("subtitles");
    importUnifiedSubtitles(
      sequence,
      3,
      SUBTITLE_MOGRT_DIR,
      SUBTITLE_SRT_PATH,
      RAW_SCENE_SUBTITLE_MANIFEST_PATH,
    );
    perfEnd("subtitles", "Subtitles");
    refreshSequenceUI(sequence);
    perfEnd("total");
    perfLogSummary();

    alert(
      "Script Complete (v7.7 Layered - Presets + External Subtitle MOGRTs).",
    );
  }

  // ========================================================================
  // 4. HELPERS
  // ========================================================================

  function trimSpaces(value) {
    if (value === null || value === undefined) return "";
    return value.toString().replace(/^\s+|\s+$/g, "");
  }

  function parseSrtTimestampToSeconds(rawTimecode) {
    var tc = trimSpaces(rawTimecode);
    if (!tc) return null;
    var m = /^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})$/.exec(tc);
    if (!m) return null;
    var h = parseInt(m[1], 10);
    var mn = parseInt(m[2], 10);
    var s = parseInt(m[3], 10);
    var msTxt = (m[4] + "00").substr(0, 3);
    var ms = parseInt(msTxt, 10);
    if (isNaN(h) || isNaN(mn) || isNaN(s) || isNaN(ms)) return null;
    return h * 3600 + mn * 60 + s + ms / 1000;
  }

  function parseSrtEntries(filePath) {
    var entries = [];
    if (!filePath) return entries;
    var f = new File(filePath);
    if (!f.exists || !f.open("r")) return entries;

    var content = "";
    try {
      content = f.read();
    } catch (e0) {
      content = "";
    }
    f.close();
    if (!content) return entries;

    content = content
      .replace(/^\uFEFF/, "")
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");

    var blocks = content.split(/\n{2,}/);
    for (var bi = 0; bi < blocks.length; bi++) {
      var block = blocks[bi];
      if (!block) continue;
      var lines = block.split("\n");
      if (!lines || lines.length < 2) continue;

      while (lines.length > 0 && trimSpaces(lines[0]) === "") lines.shift();
      while (lines.length > 0 && trimSpaces(lines[lines.length - 1]) === "")
        lines.pop();
      if (lines.length < 2) continue;

      var cursor = 0;
      if (/^\d+$/.test(trimSpaces(lines[0]))) cursor = 1;
      if (cursor >= lines.length) continue;

      var timingLine = lines[cursor];
      if (timingLine.indexOf("-->") === -1) continue;
      var timingParts = timingLine.split(/-->/);
      if (!timingParts || timingParts.length < 2) continue;

      var startSec = parseSrtTimestampToSeconds(timingParts[0]);
      var endSec = parseSrtTimestampToSeconds(timingParts[1]);
      if (startSec === null || endSec === null || endSec <= startSec) continue;

      var hasText = false;
      for (var li = cursor + 1; li < lines.length; li++) {
        var txtLine = lines[li];
        if (txtLine === null || txtLine === undefined) continue;
        if (trimSpaces(txtLine) === "") continue;
        hasText = true;
        break;
      }
      if (!hasText) continue;

      entries.push({
        index: entries.length + 1,
        start: startSec,
        end: endSec,
      });
    }
    return entries;
  }

  function parseSubtitleMogrtIndex(fileName) {
    if (!fileName) return null;
    var m = /^subtitle_(\d+)\.mogrt$/i.exec(trimSpaces(fileName));
    if (!m || m.length < 2) return null;
    var n = parseInt(m[1], 10);
    return isNaN(n) ? null : n;
  }

  function listSubtitleMogrtFilesSorted(folderPath) {
    var result = [];
    if (!folderPath) return result;
    var folder = new Folder(folderPath);
    if (!folder.exists) return result;

    var entries = folder.getFiles();
    var sortable = [];
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i];
      if (!(entry instanceof File)) continue;
      var idx = parseSubtitleMogrtIndex(entry.name);
      if (idx === null) continue;
      sortable.push({
        file: entry,
        index: idx,
        name: entry.name ? entry.name.toLowerCase() : "",
      });
    }

    sortable.sort(function (a, b) {
      if (a.index !== b.index) return a.index - b.index;
      if (a.name < b.name) return -1;
      if (a.name > b.name) return 1;
      return 0;
    });

    for (var j = 0; j < sortable.length; j++) {
      result.push(sortable[j].file);
    }
    return result;
  }

  function readJsonFile(filePath) {
    if (!filePath) return null;
    var f = new File(filePath);
    if (!f.exists || !f.open("r")) return null;
    var content = "";
    try {
      content = f.read();
    } catch (e0) {
      content = "";
    }
    f.close();
    if (!content) return null;
    try {
      return JSON.parse(content);
    } catch (e1) {
      return null;
    }
  }

  function buildClassicSubtitlePlacements(subtitleDirPath, srtPath) {
    var stats = {
      timings: 0,
      mogrtsFound: 0,
      timingsUnused: 0,
      mogrtsUnused: 0,
    };
    var placements = [];

    var entries = parseSrtEntries(srtPath);
    stats.timings = entries.length;
    if (entries.length <= 0) {
      return {
        stats: stats,
        placements: placements,
      };
    }

    var subtitleDir = new Folder(subtitleDirPath);
    if (!subtitleDir.exists) {
      return {
        stats: stats,
        placements: placements,
      };
    }

    var mogrtFiles = listSubtitleMogrtFilesSorted(subtitleDirPath);
    stats.mogrtsFound = mogrtFiles.length;
    if (stats.mogrtsFound <= 0) {
      return {
        stats: stats,
        placements: placements,
      };
    }

    var pairCount = Math.min(stats.timings, stats.mogrtsFound);
    stats.timingsUnused = stats.timings - pairCount;
    stats.mogrtsUnused = stats.mogrtsFound - pairCount;

    for (var k = 0; k < pairCount; k++) {
      var entry = entries[k];
      var mogrtFile = mogrtFiles[k];
      var startSec = snapSecondsToFrame(entry.start);
      var endSec = snapSecondsToFrame(entry.end);
      if (endSec <= startSec) {
        endSec = snapSecondsToFrame(startSec + 1 / SEQ_FPS);
      }
      placements.push({
        kind: "classic",
        idx: k + 1,
        mogrtPath: mogrtFile.fsName,
        startSec: startSec,
        startTicksStr: secondsToTicks(startSec).toString(),
        endSec: endSec,
        endTimeObj: buildSequenceTimeFromSeconds(endSec),
      });
    }

    return {
      stats: stats,
      placements: placements,
    };
  }

  function loadRawSceneSubtitleImagePlacements(manifestPath) {
    var stats = {
      entries: 0,
      skipped: 0,
    };
    var placements = [];

    var payload = readJsonFile(manifestPath);
    if (!payload || !payload.entries || !payload.entries.length) {
      return {
        stats: stats,
        placements: placements,
      };
    }

    stats.entries = payload.entries.length;
    for (var i = 0; i < payload.entries.length; i++) {
      var entry = payload.entries[i];
      if (!entry) continue;
      var relativeAssetPath = trimSpaces(entry.relative_asset_path);
      if (!relativeAssetPath) {
        stats.skipped++;
        continue;
      }

      var startSec = snapSecondsToFrame(entry.start);
      var endSec = snapSecondsToFrame(entry.end);
      if (endSec <= startSec) {
        endSec = snapSecondsToFrame(startSec + 1 / SEQ_FPS);
      }
      placements.push({
        kind: "raw",
        idx: i + 1,
        relativeAssetPath: relativeAssetPath,
        startSec: startSec,
        endSec: endSec,
      });
    }

    placements.sort(function (a, b) {
      if (a.startSec !== b.startSec) return a.startSec - b.startSec;
      if (a.endSec !== b.endSec) return a.endSec - b.endSec;
      return a.idx - b.idx;
    });

    return {
      stats: stats,
      placements: placements,
    };
  }

  function compareSubtitlePlacementOrder(a, b, frameDurationSec) {
    var delta = a.startSec - b.startSec;
    if (
      a.kind !== b.kind &&
      Math.abs(delta) <= frameDurationSec
    ) {
      return a.kind === "classic" ? -1 : 1;
    }
    if (delta !== 0) return delta < 0 ? -1 : 1;
    if (a.kind !== b.kind) return a.kind === "classic" ? -1 : 1;
    if (a.endSec !== b.endSec) return a.endSec < b.endSec ? -1 : 1;
    return (a.idx || 0) - (b.idx || 0);
  }

  function clampRawSubtitlePlacementsAgainstClassic(
    rawPlacements,
    classicPlacements,
    frameDurationSec,
  ) {
    var result = {
      placements: [],
      dropped: 0,
      trimmed: 0,
    };
    if (!rawPlacements || rawPlacements.length <= 0) return result;

    var classics = classicPlacements ? classicPlacements.slice(0) : [];
    classics.sort(function (a, b) {
      if (a.startSec !== b.startSec) return a.startSec - b.startSec;
      return a.endSec - b.endSec;
    });

    for (var i = 0; i < rawPlacements.length; i++) {
      var raw = rawPlacements[i];
      if (!raw || !(raw.endSec > raw.startSec)) {
        result.dropped++;
        continue;
      }

      var overlaps = [];
      for (var c = 0; c < classics.length; c++) {
        var classic = classics[c];
        if (!classic) continue;
        if (classic.endSec <= raw.startSec || classic.startSec >= raw.endSec) {
          continue;
        }
        overlaps.push(classic);
      }

      if (overlaps.length <= 0) {
        result.placements.push(raw);
        continue;
      }

      overlaps.sort(function (a, b) {
        if (a.startSec !== b.startSec) return a.startSec - b.startSec;
        return a.endSec - b.endSec;
      });

      var firstOverlap = overlaps[0];
      var lastOverlap = overlaps[overlaps.length - 1];
      var beforeStart = raw.startSec;
      var beforeEnd = Math.min(firstOverlap.startSec, raw.endSec);
      var afterStart = Math.max(lastOverlap.endSec, raw.startSec);
      var afterEnd = raw.endSec;
      var beforeDuration = beforeEnd - beforeStart;
      var afterDuration = afterEnd - afterStart;

      var newStart = null;
      var newEnd = null;
      if (beforeDuration > frameDurationSec && afterDuration <= frameDurationSec) {
        newStart = beforeStart;
        newEnd = beforeEnd;
      } else if (
        afterDuration > frameDurationSec &&
        beforeDuration <= frameDurationSec
      ) {
        newStart = afterStart;
        newEnd = afterEnd;
      } else {
        result.dropped++;
        continue;
      }

      if (!(newEnd - newStart > frameDurationSec)) {
        result.dropped++;
        continue;
      }

      result.placements.push({
        kind: raw.kind,
        idx: raw.idx,
        relativeAssetPath: raw.relativeAssetPath,
        startSec: snapSecondsToFrame(newStart),
        endSec: snapSecondsToFrame(newEnd),
      });
      result.trimmed++;
    }
    return result;
  }

  function buildMergedSubtitlePlacementSchedule(
    classicPlacements,
    rawPlacements,
    frameDurationSec,
  ) {
    var merge = {
      placements: [],
      rawDropped: 0,
      rawTrimmed: 0,
    };
    var clampedRaw = clampRawSubtitlePlacementsAgainstClassic(
      rawPlacements,
      classicPlacements,
      frameDurationSec,
    );
    merge.rawDropped = clampedRaw.dropped;
    merge.rawTrimmed = clampedRaw.trimmed;
    merge.placements = classicPlacements.slice(0).concat(clampedRaw.placements);
    merge.placements.sort(function (a, b) {
      return compareSubtitlePlacementOrder(a, b, frameDurationSec);
    });
    return merge;
  }

  function importUnifiedSubtitles(
    sequence,
    videoTrackIndex,
    subtitleDirPath,
    srtPath,
    rawManifestPath,
    enableSecondsFallback,
  ) {
    var stats = {
      classicTimings: 0,
      classicMogrtsFound: 0,
      classicUnusedTimings: 0,
      classicUnusedMogrts: 0,
      rawEntries: 0,
      rawDropped: 0,
      rawTrimmed: 0,
      scheduled: 0,
      inserted: 0,
      failed: 0,
    };

    if (
      !sequence ||
      !sequence.videoTracks ||
      sequence.videoTracks.numTracks <= videoTrackIndex
    ) {
      log(
        "Warning: Cannot add subtitles (missing sequence track V" +
          (videoTrackIndex + 1) +
          ").",
      );
      return stats;
    }

    var classicResult = buildClassicSubtitlePlacements(subtitleDirPath, srtPath);
    var rawResult = loadRawSceneSubtitleImagePlacements(rawManifestPath);
    var frameDurationSec = 1 / SEQ_FPS;
    var schedule = buildMergedSubtitlePlacementSchedule(
      classicResult.placements,
      rawResult.placements,
      frameDurationSec,
    );
    var track = sequence.videoTracks[videoTrackIndex];
    var useSecondsFallback = enableSecondsFallback !== false;

    stats.classicTimings = classicResult.stats.timings;
    stats.classicMogrtsFound = classicResult.stats.mogrtsFound;
    stats.classicUnusedTimings = classicResult.stats.timingsUnused;
    stats.classicUnusedMogrts = classicResult.stats.mogrtsUnused;
    stats.rawEntries = rawResult.stats.entries;
    stats.rawDropped = schedule.rawDropped;
    stats.rawTrimmed = schedule.rawTrimmed;
    stats.scheduled = schedule.placements.length;

    if (stats.classicTimings <= 0 && stats.rawEntries <= 0) {
      return stats;
    }
    if (stats.classicTimings > 0 && stats.classicMogrtsFound <= 0) {
      log("Warning: No subtitle MOGRT files found in " + subtitleDirPath + ".");
    }

    for (var p = 0; p < schedule.placements.length; p++) {
      var placement = schedule.placements[p];
      if (!placement) continue;

      if (placement.kind === "classic") {
        var mogrtItem = null;
        try {
          perfCounterInc("importMGTCalls");
          mogrtItem = sequence.importMGT(
            placement.mogrtPath,
            placement.startTicksStr,
            videoTrackIndex,
            0,
          );
        } catch (e0) {}
        if (!mogrtItem && useSecondsFallback) {
          try {
            perfCounterInc("importMGTCalls");
            mogrtItem = sequence.importMGT(
              placement.mogrtPath,
              placement.startSec,
              videoTrackIndex,
              0,
            );
          } catch (e1) {}
        }
        if (!mogrtItem) {
          stats.failed++;
          continue;
        }
        try {
          mogrtItem.end = placement.endTimeObj;
        } catch (e2) {
          try {
            mogrtItem.end = placement.endSec;
          } catch (e3) {
            stats.failed++;
            continue;
          }
        }
        stats.inserted++;
      } else {
        var clip = getOrImportClip(placement.relativeAssetPath);
        if (!clip) {
          stats.failed++;
          continue;
        }
        try {
          track.overwriteClip(clip, placement.startSec);
        } catch (e4) {
          stats.failed++;
          continue;
        }

        var assetName = placement.relativeAssetPath;
        assetName = assetName.replace(/^.*[\\\/]/, "");
        var assetNameNoExt = stripKnownExtension(assetName);
        var item =
          resolvePlacedItemFast(
            track,
            placement.startSec,
            assetNameNoExt,
            TRACK_ITEM_WAIT_MAX_MS,
          ) ||
          resolvePlacedItemFast(
            track,
            placement.startSec,
            assetName,
            TRACK_ITEM_WAIT_MAX_MS,
          ) ||
          findTrackItemAtStart(track, placement.startSec, assetNameNoExt) ||
          findTrackItemAtStart(track, placement.startSec, assetName) ||
          findTrackItemAtStart(track, placement.startSec, null);
        if (!item || !setTrackItemEndSeconds(item, placement.endSec)) {
          stats.failed++;
          continue;
        }
        stats.inserted++;
      }

      if (
        PERF_PROFILE_ENABLED &&
        PERF_LOG_EACH_SUBTITLE_BATCH > 0 &&
        ((p + 1) % PERF_LOG_EACH_SUBTITLE_BATCH === 0 ||
          p === schedule.placements.length - 1)
      ) {
        log(
          "[PERF] Subtitles progress: " +
            (p + 1) +
            "/" +
            schedule.placements.length +
            " (inserted " +
            stats.inserted +
            ", failed " +
            stats.failed +
            ").",
        );
      }
    }

    log(
      "Subtitles on V" +
        (videoTrackIndex + 1) +
        ": classic timings " +
        stats.classicTimings +
        ", classic mogrts " +
        stats.classicMogrtsFound +
        ", raw entries " +
        stats.rawEntries +
        ", raw trimmed " +
        stats.rawTrimmed +
        ", raw dropped " +
        stats.rawDropped +
        ", scheduled " +
        stats.scheduled +
        ", inserted " +
        stats.inserted +
        ", failed " +
        stats.failed +
        ", classic timings unused " +
        stats.classicUnusedTimings +
        ", classic mogrts unused " +
        stats.classicUnusedMogrts +
        ".",
    );
    return stats;
  }

  function clearSelection(sequence, updateUI) {
    if (!sequence) return;
    var uiRefresh = !!updateUI;
    var clearedViaSelection = 0;
    try {
      if (sequence.getSelection) {
        var selection = sequence.getSelection();
        if (selection) {
          var selectionCount = 0;
          if (selection.length !== undefined) {
            selectionCount = selection.length;
          } else if (selection.numItems !== undefined) {
            selectionCount = selection.numItems;
          }
          for (var si = 0; si < selectionCount; si++) {
            var selectedItem = selection[si];
            if (!selectedItem && selection.getItemAt) {
              try {
                selectedItem = selection.getItemAt(si);
              } catch (eGetItem) {}
            }
            if (!selectedItem || !selectedItem.setSelected) continue;
            try {
              selectedItem.setSelected(false, uiRefresh);
              clearedViaSelection++;
            } catch (eSetSelected) {}
          }
        }
      }
    } catch (e0) {}
    if (clearedViaSelection > 0) {
      perfCounterInc("clearSelectionSelectionItems", clearedViaSelection);
      return;
    }
    try {
      var tracks = sequence.videoTracks;
      var trackCount = tracks ? tracks.numTracks : 0;
      for (var i = 0; i < trackCount; i++) {
        var track = tracks[i];
        if (!track || !track.clips) continue;
        var clips = track.clips;
        var clipCount = clips.numItems;
        for (var j = 0; j < clipCount; j++) {
          clips[j].setSelected(false, uiRefresh);
        }
      }
      perfCounterInc("clearSelectionFallbackTrackScans");
      // Clear Audio as well if needed? Usually audio tracks are less prone to this crash but good practice.
      // Skipping to save time/performance unless necessary.
    } catch (e) {}
  }

  function getQETrackCacheKey(trackType, trackIndex) {
    return (trackType === "Audio" ? "A" : "V") + ":" + trackIndex;
  }

  function getCachedQETrack(qeSeq, trackType, trackIndex, qeTrackCache) {
    var key = getQETrackCacheKey(trackType, trackIndex);
    var cache = qeTrackCache || {};
    var cached = cache[key];
    if (cached) {
      try {
        var _ = cached.numItems;
        return cached;
      } catch (e0) {
        cache[key] = null;
      }
    }
    var track = null;
    try {
      if (trackType === "Audio") track = qeSeq.getAudioTrackAt(trackIndex);
      else track = qeSeq.getVideoTrackAt(trackIndex);
    } catch (e1) {}
    if (track) cache[key] = track;
    return track;
  }

  function safeApplySpeedQE(
    startTime,
    speed,
    trackIndex,
    trackType,
    clipNameRef,
    sequence,
    qeSeq,
    qeTrackCache,
    qeHints,
  ) {
    try {
      if (!qeSeq) {
        qeSeq = qe.project.getActiveSequence();
      }
      if (!qeSeq) return false;
      var qeTrack = getCachedQETrack(
        qeSeq,
        trackType,
        trackIndex,
        qeTrackCache,
      );
      if (!qeTrack) return false;

      var targetTicks = secondsToTicks(startTime);
      var toleranceTicks = secondsToTicks(0.2);
      var retriedAfterReset = false;
      var hintMap = qeHints || {};
      var hintKey = getQETrackCacheKey(trackType, trackIndex);

      var itemMatches = function (item) {
        if (!item || typeof item.start === "undefined") return false;
        var startTicks = getQEItemStartTicks(item);
        var matchTime = false;

        if (typeof startTicks === "number" && !isNaN(startTicks)) {
          matchTime = Math.abs(startTicks - targetTicks) < toleranceTicks;
        } else {
          try {
            matchTime = Math.abs(item.start.secs - startTime) < 0.2;
          } catch (e0) {
            matchTime = false;
          }
        }
        if (!matchTime) return false;

        if (clipNameRef) {
          var itemName = item.name ? item.name.toString() : "";
          if (!isItemNameMatch(itemName, clipNameRef)) return false;
        }
        return true;
      };

      var applyOnItem = function (item, idx) {
        try {
          // args: speed, stretch, reverse, ripple, flicker
          perfCounterInc("speedApplyCalls");
          item.setSpeed(speed, "", false, false, false);
          hintMap[hintKey] = idx;
          return true;
        } catch (err) {
          var errMsg = err && err.message ? err.message.toString() : "";
          if (
            !retriedAfterReset &&
            errMsg.toLowerCase().indexOf("invalid trackitem") !== -1
          ) {
            retriedAfterReset = true;
            clearSelection(sequence || app.project.activeSequence, false);
            sleep(SPEED_RETRY_FAST_WAIT_MS);
            try {
              perfCounterInc("speedApplyCalls");
              item.setSpeed(speed, "", false, false, false);
              hintMap[hintKey] = idx;
              return true;
            } catch (err2) {
              var err2Msg = err2 && err2.message ? err2.message.toString() : "";
              if (err2Msg.toLowerCase().indexOf("invalid trackitem") !== -1) {
                // Keep the long retry only for repeated invalid-trackitem failures.
                sleep(SPEED_RETRY_LONG_WAIT_MS);
                try {
                  perfCounterInc("speedApplyCalls");
                  item.setSpeed(speed, "", false, false, false);
                  hintMap[hintKey] = idx;
                  return true;
                } catch (err3) {
                  log(
                    "Speed Apply Retry Error: " +
                      (err3 && err3.message ? err3.message : err3),
                  );
                }
              } else {
                log("Speed Apply Retry Error: " + err2Msg);
              }
            }
          } else {
            log("Speed Apply Error: " + errMsg);
          }
        }
        return false;
      };

      var hintIdx = hintMap[hintKey];
      if (
        typeof hintIdx === "number" &&
        hintIdx >= 0 &&
        hintIdx < qeTrack.numItems
      ) {
        try {
          var hinted = qeTrack.getItemAt(hintIdx);
          if (itemMatches(hinted)) {
            return applyOnItem(hinted, hintIdx);
          }
        } catch (eHint) {}
      }

      var tailWindow = 12;
      var startIdx = qeTrack.numItems - 1;
      var stopIdx = Math.max(0, startIdx - tailWindow + 1);
      for (var ti = startIdx; ti >= stopIdx; ti--) {
        try {
          var tailItem = qeTrack.getItemAt(ti);
          if (!itemMatches(tailItem)) continue;
          return applyOnItem(tailItem, ti);
        } catch (eTail) {}
      }

      // Full fallback search.
      perfCounterInc("speedApplyFullScans");
      for (var i = qeTrack.numItems - 1; i >= 0; i--) {
        try {
          var item = qeTrack.getItemAt(i);
          if (!itemMatches(item)) continue;
          return applyOnItem(item, i);
        } catch (eFull) {}
      }
      log(
        "Warning: Could not find clip at " +
          startTime +
          " (" +
          clipNameRef +
          ") for Speed change.",
      );
      return false;
    } catch (e) {
      log("QE Speed Fail: " + e.message);
      return false;
    }
  }

  function resolveVideoEffectByName(effectName) {
    if (!effectName) return null;
    var name = effectName.toString().replace(/^\s+|\s+$/g, "");
    if (!name) return null;
    if (VIDEO_EFFECT_RESOLVE_CACHE[name] !== undefined) {
      return VIDEO_EFFECT_RESOLVE_CACHE[name];
    }
    var resolved = null;
    try {
      resolved = qe.project.getVideoEffectByName(name);
    } catch (e) {}
    VIDEO_EFFECT_RESOLVE_CACHE[name] = resolved;
    return resolved;
  }

  function pushUnique(arr, value) {
    if (!arr || !value) return;
    for (var i = 0; i < arr.length; i++) {
      if (arr[i] === value) return;
    }
    arr.push(value);
  }

  function decodeXmlEntities(text) {
    if (!text) return "";
    return text
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'");
  }

  function readPresetFileText(filePath) {
    if (!filePath) return "";
    if (PRESET_FILE_TEXT_CACHE[filePath] !== undefined) {
      return PRESET_FILE_TEXT_CACHE[filePath];
    }
    var f = new File(filePath);
    if (!f.exists || !f.open("r")) {
      PRESET_FILE_TEXT_CACHE[filePath] = "";
      return "";
    }
    var content = "";
    try {
      content = f.read();
    } catch (e) {
      content = "";
    }
    f.close();
    PRESET_FILE_TEXT_CACHE[filePath] = content || "";
    return PRESET_FILE_TEXT_CACHE[filePath];
  }

  function extractVideoFilterEntriesFromPresetFile(filePath) {
    var entries = [];
    if (!filePath) return entries;
    var content = readPresetFileText(filePath);
    if (!content) return entries;

    var re =
      /<VideoFilterComponent[\s\S]*?<DisplayName>([\s\S]*?)<\/DisplayName>[\s\S]*?<MatchName>([\s\S]*?)<\/MatchName>[\s\S]*?<\/VideoFilterComponent>/g;
    var m = null;
    while ((m = re.exec(content)) !== null) {
      var displayName = decodeXmlEntities(
        m[1] ? m[1].replace(/^\s+|\s+$/g, "") : "",
      );
      var matchName = decodeXmlEntities(
        m[2] ? m[2].replace(/^\s+|\s+$/g, "") : "",
      );
      if (!displayName && !matchName) continue;
      entries.push({ displayName: displayName, matchName: matchName });
    }
    return entries;
  }

  function parsePresetScalarValue(raw) {
    if (raw === null || raw === undefined) return null;
    var txt = raw.toString().replace(/^\s+|\s+$/g, "");
    if (txt === "") return null;
    if (/^(true|false)$/i.test(txt)) return txt.toLowerCase() === "true";
    var n = parseFloat(txt);
    if (!isNaN(n) && /^[-+0-9.]+$/.test(txt)) return n;
    return null;
  }

  function parsePresetStartKeyframeValue(rawStartKeyframe) {
    if (rawStartKeyframe === null || rawStartKeyframe === undefined)
      return null;
    var txt = rawStartKeyframe.toString().replace(/^\s+|\s+$/g, "");
    if (!txt) return null;
    var parts = txt.split(",");
    if (!parts || parts.length < 2) return null;
    return parsePresetScalarValue(parts[1]);
  }

  function extractPresetEffectParamValue(node) {
    if (!node) return null;

    var currentValue = null;
    var currMatch = /<CurrentValue>([\s\S]*?)<\/CurrentValue>/.exec(node);
    if (currMatch && currMatch.length >= 2) {
      currentValue = parsePresetScalarValue(currMatch[1]);
    }

    var startValue = null;
    var startMatch = /<StartKeyframe>([\s\S]*?)<\/StartKeyframe>/.exec(node);
    if (startMatch && startMatch.length >= 2) {
      startValue = parsePresetStartKeyframeValue(startMatch[1]);
    }

    if (currentValue === null) return startValue;
    if (
      typeof currentValue === "number" &&
      Math.abs(currentValue) <= 0.000001 &&
      typeof startValue === "number" &&
      Math.abs(startValue) > 0.000001
    ) {
      return startValue;
    }
    if (
      typeof currentValue === "boolean" &&
      currentValue === false &&
      typeof startValue === "boolean" &&
      startValue === true
    ) {
      return startValue;
    }
    return currentValue;
  }

  function extractPresetEffectParamValuePreferStart(node) {
    if (!node) return null;

    var startValue = null;
    var startMatch = /<StartKeyframe>([\s\S]*?)<\/StartKeyframe>/.exec(node);
    if (startMatch && startMatch.length >= 2) {
      startValue = parsePresetStartKeyframeValue(startMatch[1]);
    }
    if (startValue !== null) return startValue;

    var currentValue = null;
    var currMatch = /<CurrentValue>([\s\S]*?)<\/CurrentValue>/.exec(node);
    if (currMatch && currMatch.length >= 2) {
      currentValue = parsePresetScalarValue(currMatch[1]);
    }
    return currentValue;
  }

  function decodeBase64ToBytes(base64Text) {
    if (!base64Text) return [];
    var src = base64Text.toString().replace(/\s+/g, "");
    if (!src) return [];

    var alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    var bytes = [];
    var i = 0;
    while (i < src.length) {
      var c0 = src.charAt(i++);
      var c1 = src.charAt(i++);
      var c2 = i < src.length ? src.charAt(i++) : "=";
      var c3 = i < src.length ? src.charAt(i++) : "=";
      if (!c0 || !c1) break;

      var b0 = alphabet.indexOf(c0);
      var b1 = alphabet.indexOf(c1);
      var b2 = c2 === "=" ? -1 : alphabet.indexOf(c2);
      var b3 = c3 === "=" ? -1 : alphabet.indexOf(c3);
      if (b0 < 0 || b1 < 0) continue;
      if (b2 < 0 && c2 !== "=") continue;
      if (b3 < 0 && c3 !== "=") continue;

      var bits =
        (b0 << 18) | (b1 << 12) | ((b2 < 0 ? 0 : b2) << 6) | (b3 < 0 ? 0 : b3);
      bytes.push((bits >> 16) & 255);
      if (b2 >= 0) bytes.push((bits >> 8) & 255);
      if (b3 >= 0) bytes.push(bits & 255);
    }
    return bytes;
  }

  function decodeBase64Utf16LE(base64Text) {
    var bytes = decodeBase64ToBytes(base64Text);
    if (!bytes || bytes.length <= 0) return "";

    var chars = [];
    for (var i = 0; i + 1 < bytes.length; i += 2) {
      var code = bytes[i] | (bytes[i + 1] << 8);
      if (code === 0) break;
      chars.push(String.fromCharCode(code));
    }
    return chars.join("");
  }

  function extractLumetriArbStringsByIndex(filePath) {
    if (!filePath) return {};
    if (LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath] !== undefined) {
      return LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath];
    }

    var result = {};
    var content = readPresetFileText(filePath);
    if (!content) {
      LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath] = result;
      return result;
    }

    var lumetriBlockMatch =
      /<VideoFilterComponent[\s\S]*?<MatchName>\s*AE\.ADBE Lumetri\s*<\/MatchName>[\s\S]*?<\/VideoFilterComponent>/i.exec(
        content,
      );
    if (!lumetriBlockMatch || !lumetriBlockMatch[0]) {
      LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath] = result;
      return result;
    }
    var lumetriBlock = lumetriBlockMatch[0];

    var paramRefByIndex = {};
    var reRef = /<Param Index="(\d+)" ObjectRef="(\d+)"\/>/g;
    var mRef = null;
    while ((mRef = reRef.exec(lumetriBlock)) !== null) {
      var idx = parseInt(mRef[1], 10);
      if (isNaN(idx)) continue;
      paramRefByIndex[idx] = mRef[2];
    }

    // Decode only Look-related indexes to avoid expensive parsing of large Arb payloads.
    var targetIndexes = [32, 33];
    for (var ti = 0; ti < targetIndexes.length; ti++) {
      var targetIdx = targetIndexes[ti];
      var objRef = paramRefByIndex[targetIdx];
      if (!objRef) continue;

      var reNode = new RegExp(
        '<ArbVideoComponentParam\\s+ObjectID="' +
          objRef +
          '"[\\s\\S]*?<\\/ArbVideoComponentParam>',
        "i",
      );
      var nodeMatch = reNode.exec(content);
      if (!nodeMatch || !nodeMatch[0]) continue;

      var valMatch =
        /<StartKeyframeValue[^>]*>([\s\S]*?)<\/StartKeyframeValue>/.exec(
          nodeMatch[0],
        );
      if (!valMatch || valMatch.length < 2) continue;
      var b64 = valMatch[1] ? valMatch[1].replace(/^\s+|\s+$/g, "") : "";
      if (!b64) continue;

      var decoded = decodeBase64Utf16LE(b64)
        .replace(/\u0000+$/g, "")
        .replace(/^\s+|\s+$/g, "");
      if (!decoded) continue;
      result[targetIdx] = decoded;
    }

    LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath] = result;
    return result;
  }

  function isAbsoluteLutPath(value) {
    if (!value) return false;
    var v = value.toString().replace(/^\s+|\s+$/g, "");
    if (!v) return false;
    var hasAbsoluteRoot =
      /^[A-Za-z]:[\\\/]/.test(v) || /^\\\\/.test(v) || /^\//.test(v);
    var hasLutExt = /\.(itx|cube|look)(\/)?$/i.test(v);
    return hasAbsoluteRoot && hasLutExt;
  }

  function resolveLumetriLookPathForPreset(presetFilePath) {
    var cacheKey = presetFilePath || "__missing_preset__";
    if (LUMETRI_LOOK_PATH_CACHE[cacheKey] !== undefined) {
      return LUMETRI_LOOK_PATH_CACHE[cacheKey];
    }
    if (!presetFilePath) {
      LUMETRI_LOOK_PATH_CACHE[cacheKey] = "";
      return "";
    }

    var values = extractLumetriArbStringsByIndex(presetFilePath);
    var indexes = [32, 33];
    for (var i = 0; i < indexes.length; i++) {
      var idx = indexes[i];
      var candidate = values[idx];
      if (!candidate) continue;
      if (isAbsoluteLutPath(candidate)) {
        LUMETRI_LOOK_PATH_CACHE[cacheKey] = candidate;
        return candidate;
      }
    }

    LUMETRI_LOOK_PATH_CACHE[cacheKey] = "";
    return "";
  }

  function buildLookPathCandidates(pathValue) {
    var candidates = [];
    var basePath = pathValue
      ? pathValue.toString().replace(/^\s+|\s+$/g, "")
      : "";
    if (!basePath) return candidates;
    if (basePath.charAt(basePath.length - 1) !== "/") basePath += "/";
    pushUnique(candidates, basePath);
    return candidates;
  }

  function extractLumetriPresetValuesByIndex(filePath) {
    if (!filePath) return [];
    if (LUMETRI_PRESET_VALUES_CACHE[filePath] !== undefined) {
      return LUMETRI_PRESET_VALUES_CACHE[filePath];
    }
    var content = readPresetFileText(filePath);
    if (!content) {
      LUMETRI_PRESET_VALUES_CACHE[filePath] = [];
      return [];
    }

    var lumetriBlockMatch =
      /<VideoFilterComponent[\s\S]*?<MatchName>\s*AE\.ADBE Lumetri\s*<\/MatchName>[\s\S]*?<\/VideoFilterComponent>/i.exec(
        content,
      );
    if (!lumetriBlockMatch || !lumetriBlockMatch[0]) {
      LUMETRI_PRESET_VALUES_CACHE[filePath] = [];
      return [];
    }
    var lumetriBlock = lumetriBlockMatch[0];

    var paramRefs = [];
    var reRef = /<Param Index="(\d+)" ObjectRef="(\d+)"\/>/g;
    var mRef = null;
    while ((mRef = reRef.exec(lumetriBlock)) !== null) {
      paramRefs.push({ index: parseInt(mRef[1], 10), objectRef: mRef[2] });
    }
    if (paramRefs.length <= 0) {
      LUMETRI_PRESET_VALUES_CACHE[filePath] = [];
      return [];
    }

    var valueByObjectRef = {};
    var reParamNode =
      /<VideoComponentParam\s+ObjectID="(\d+)"[\s\S]*?<\/VideoComponentParam>/g;
    var mNode = null;
    while ((mNode = reParamNode.exec(content)) !== null) {
      var objectId = mNode[1];
      var node = mNode[0];
      var value = extractPresetEffectParamValue(node);
      if (value === null) continue;

      var nameMatch = /<Name>([\s\S]*?)<\/Name>/.exec(node);
      var name =
        nameMatch && nameMatch[1]
          ? decodeXmlEntities(nameMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      var typeMatch =
        /<ParameterControlType>([\s\S]*?)<\/ParameterControlType>/.exec(node);
      var controlType =
        typeMatch && typeMatch[1] ? parseInt(typeMatch[1], 10) : null;

      valueByObjectRef[objectId] = {
        value: value,
        name: name,
        controlType: controlType,
      };
    }

    var result = [];
    for (var i = 0; i < paramRefs.length; i++) {
      var ref = paramRefs[i];
      var data = valueByObjectRef[ref.objectRef];
      if (!data) continue;
      result.push({
        index: ref.index,
        value: data.value,
        name: data.name,
        controlType: data.controlType,
      });
    }
    result.sort(function (a, b) {
      return a.index - b.index;
    });

    LUMETRI_PRESET_VALUES_CACHE[filePath] = result;
    return result;
  }

  function isMeaningfulLumetriValue(value) {
    if (typeof value === "boolean") return value === true;
    if (typeof value === "number") return Math.abs(value) > 0.000001;
    return false;
  }

  function getLumetriComponent(item) {
    if (!item || !item.components) return null;
    for (var c = 0; c < item.components.numItems; c++) {
      var comp = item.components[c];
      if (!comp || !comp.displayName) continue;
      var nm = comp.displayName.toString();
      if (
        nm === "Couleur Lumetri" ||
        nm === "Lumetri Color" ||
        nm.toLowerCase().indexOf("lumetri") !== -1
      ) {
        return comp;
      }
    }
    return null;
  }

  function applyLumetriPresetValuesToTrack(
    stdTrack,
    presetFilePath,
    presetData,
    trackClipContexts,
  ) {
    var stats = {
      settingsApplied: 0,
      clipsWithLumetri: 0,
      clipsUpdated: 0,
      propWrites: 0,
      propFails: 0,
    };
    if (!stdTrack || !stdTrack.clips) return stats;

    var parsed = presetData || getPresetParsedData(presetFilePath);
    var values = parsed && parsed.lumetriValues ? parsed.lumetriValues : [];
    if (values.length <= 0) return stats;
    stats.settingsApplied = values.length;

    var contexts = trackClipContexts || buildTrackClipContexts(stdTrack);
    for (var ci = 0; ci < contexts.length; ci++) {
      var context = contexts[ci];
      if (!context || !context.clip) continue;
      var lumetri = getTrackClipLumetriComponent(context);
      if (!lumetri || !lumetri.properties) continue;
      stats.clipsWithLumetri++;

      var clipWrites = 0;
      var properties = lumetri.properties;
      var propCount = properties.numItems;
      for (var vi = 0; vi < values.length; vi++) {
        var setting = values[vi];
        if (setting.index < 0 || setting.index >= propCount) continue;
        var prop = properties[setting.index];
        if (!prop) continue;
        if (setPropertyValueFast(prop, setting.value)) {
          stats.propWrites++;
          clipWrites++;
        } else {
          stats.propFails++;
        }
      }
      if (clipWrites > 0) stats.clipsUpdated++;
    }
    return stats;
  }

  function applyLumetriLookPathToTrack(
    stdTrack,
    presetFilePath,
    presetData,
    trackClipContexts,
  ) {
    var stats = {
      resolvedPath: "",
      candidateCount: 0,
      pathUsed: "",
      clipsWithLumetri: 0,
      clipsUpdated: 0,
      propWrites: 0,
      propFails: 0,
    };
    if (!stdTrack || !stdTrack.clips) return stats;

    var parsed = presetData || getPresetParsedData(presetFilePath);
    var resolvedPath =
      parsed && parsed.lumetriLookPath !== undefined
        ? parsed.lumetriLookPath
        : resolveLumetriLookPathForPreset(presetFilePath);
    stats.resolvedPath = resolvedPath;
    var candidates = buildLookPathCandidates(resolvedPath);
    stats.candidateCount = candidates.length;
    if (candidates.length <= 0) return stats;

    var targetIndexes = [32, 33];
    var contexts = trackClipContexts || buildTrackClipContexts(stdTrack);
    for (var ci = 0; ci < contexts.length; ci++) {
      var context = contexts[ci];
      if (!context || !context.clip) continue;
      var lumetri = getTrackClipLumetriComponent(context);
      if (!lumetri || !lumetri.properties) continue;
      stats.clipsWithLumetri++;

      var clipApplied = false;
      var properties = lumetri.properties;
      var propCount = properties.numItems;
      for (var pi = 0; pi < candidates.length && !clipApplied; pi++) {
        var pathCandidate = candidates[pi];
        for (var ti = 0; ti < targetIndexes.length; ti++) {
          var idx = targetIndexes[ti];
          if (idx < 0 || idx >= propCount) continue;
          var prop = properties[idx];
          if (!prop) continue;
          if (setPropertyValueFast(prop, pathCandidate)) {
            stats.propWrites++;
            clipApplied = true;
            if (!stats.pathUsed) {
              stats.pathUsed = pathCandidate;
            } else if (stats.pathUsed !== pathCandidate) {
              stats.pathUsed = "mixed";
            }
            break;
          } else {
            stats.propFails++;
          }
        }
      }

      if (clipApplied) stats.clipsUpdated++;
    }
    return stats;
  }

  function isLumetriMatchName(matchName) {
    if (!matchName) return false;
    var m = matchName.toString().toLowerCase();
    return m.indexOf("adbe lumetri") !== -1 || m.indexOf("lumetri") !== -1;
  }

  function extractPresetEffectValueEntries(filePath) {
    if (!filePath) return [];
    if (PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath] !== undefined) {
      return PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath];
    }

    var content = readPresetFileText(filePath);
    if (!content) {
      PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath] = [];
      return [];
    }

    var valueByObjectRef = {};
    var reParamNode =
      /<VideoComponentParam\s+ObjectID="(\d+)"[\s\S]*?<\/VideoComponentParam>/g;
    var mNode = null;
    while ((mNode = reParamNode.exec(content)) !== null) {
      var objectId = mNode[1];
      var node = mNode[0];
      var value = extractPresetEffectParamValuePreferStart(node);
      if (value === null) continue;

      var nameMatch = /<Name>([\s\S]*?)<\/Name>/.exec(node);
      var name =
        nameMatch && nameMatch[1]
          ? decodeXmlEntities(nameMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      var typeMatch =
        /<ParameterControlType>([\s\S]*?)<\/ParameterControlType>/.exec(node);
      var controlType =
        typeMatch && typeMatch[1] ? parseInt(typeMatch[1], 10) : null;
      valueByObjectRef[objectId] = {
        value: value,
        name: name,
        controlType: controlType,
      };
    }

    var entries = [];
    var reComponent = /<VideoFilterComponent[\s\S]*?<\/VideoFilterComponent>/g;
    var mComp = null;
    while ((mComp = reComponent.exec(content)) !== null) {
      var block = mComp[0];
      var displayMatch = /<DisplayName>([\s\S]*?)<\/DisplayName>/.exec(block);
      var matchMatch = /<MatchName>([\s\S]*?)<\/MatchName>/.exec(block);
      var displayName =
        displayMatch && displayMatch[1]
          ? decodeXmlEntities(displayMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      var matchName =
        matchMatch && matchMatch[1]
          ? decodeXmlEntities(matchMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      if (!displayName && !matchName) continue;

      var values = [];
      var reRef = /<Param Index="(\d+)" ObjectRef="(\d+)"\/>/g;
      var mRef = null;
      while ((mRef = reRef.exec(block)) !== null) {
        var idx = parseInt(mRef[1], 10);
        var objRef = mRef[2];
        var data = valueByObjectRef[objRef];
        if (!data) continue;
        values.push({
          index: idx,
          value: data.value,
          name: data.name,
          controlType: data.controlType,
        });
      }
      values.sort(function (a, b) {
        return a.index - b.index;
      });

      entries.push({
        displayName: displayName,
        matchName: matchName,
        values: values,
      });
    }

    PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath] = entries;
    return entries;
  }

  function getPresetParsedData(filePath) {
    var cacheKey = filePath || "__missing_preset__";
    if (PRESET_PARSED_DATA_CACHE[cacheKey] !== undefined) {
      return PRESET_PARSED_DATA_CACHE[cacheKey];
    }

    var filterEntries = extractVideoFilterEntriesFromPresetFile(filePath);
    var effectEntries = extractPresetEffectValueEntries(filePath);

    var nonLumetriEffectEntries = [];
    for (var i = 0; i < effectEntries.length; i++) {
      var entry = effectEntries[i];
      if (!entry || !entry.values || entry.values.length <= 0) continue;
      if (isLumetriMatchName(entry.matchName)) continue;
      nonLumetriEffectEntries.push(entry);
    }

    var lumetriRawValues = extractLumetriPresetValuesByIndex(filePath);
    var lumetriValues = [];
    for (var vi = 0; vi < lumetriRawValues.length; vi++) {
      var rawValue = lumetriRawValues[vi];
      if (rawValue && isMeaningfulLumetriValue(rawValue.value)) {
        lumetriValues.push(rawValue);
      }
    }

    var parsed = {
      filterEntries: filterEntries,
      effectEntries: effectEntries,
      nonLumetriEffectEntries: nonLumetriEffectEntries,
      lumetriValues: lumetriValues,
      lumetriLookPath: resolveLumetriLookPathForPreset(filePath),
    };
    PRESET_PARSED_DATA_CACHE[cacheKey] = parsed;
    return parsed;
  }

  function buildEffectEntriesWithCandidates(entries) {
    var prepared = [];
    if (!entries) return prepared;
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i];
      if (!entry) continue;
      var candidates = getFallbackEffectNameCandidates(
        entry.matchName,
        entry.displayName,
      );
      var candidatesLower = [];
      for (var c = 0; c < candidates.length; c++) {
        var candidate = candidates[c];
        if (!candidate) continue;
        candidatesLower.push(candidate.toString().toLowerCase());
      }
      prepared.push({
        entry: entry,
        label: entry.displayName || entry.matchName || "Effect " + (i + 1),
        candidates: candidates,
        candidatesLower: candidatesLower,
      });
    }
    return prepared;
  }

  function buildTrackClipContexts(stdTrack) {
    var contexts = [];
    if (!stdTrack || !stdTrack.clips) return contexts;
    var clips = stdTrack.clips;
    var clipCount = clips.numItems;
    for (var i = 0; i < clipCount; i++) {
      var clip = clips[i];
      if (!clip) continue;

      var nameRef = "";
      try {
        if (clip.projectItem && clip.projectItem.name) {
          nameRef = clip.projectItem.name.toString();
        } else if (clip.name) {
          nameRef = clip.name.toString();
        }
      } catch (e0) {}
      var cleanNameRef = nameRef ? stripKnownExtension(nameRef) : "";

      var componentNameMap = {};
      var lumetriComponent = null;
      try {
        if (clip.components) {
          var components = clip.components;
          var compCount = components.numItems;
          for (var c = 0; c < compCount; c++) {
            var comp = components[c];
            if (!comp || !comp.displayName) continue;
            var compName = comp.displayName.toString();
            if (!compName) continue;
            var lowerName = compName.toLowerCase();
            if (componentNameMap[lowerName] === undefined) {
              componentNameMap[lowerName] = comp;
            }
            if (
              !lumetriComponent &&
              (compName === "Couleur Lumetri" ||
                compName === "Lumetri Color" ||
                lowerName.indexOf("lumetri") !== -1)
            ) {
              lumetriComponent = comp;
            }
          }
        }
      } catch (e1) {}

      contexts.push({
        clipIndex: i,
        clip: clip,
        startSec: getTrackItemStartSeconds(clip),
        nameRef: nameRef,
        cleanNameRef: cleanNameRef,
        componentNameMap: componentNameMap,
        lumetriComponent: lumetriComponent,
        qeItem: null,
        qeResolvedAttempted: false,
      });
    }
    return contexts;
  }

  function getTrackClipLumetriComponent(context) {
    if (!context) return null;
    var lumetri = context.lumetriComponent;
    if (lumetri) {
      try {
        var _ = lumetri.properties;
        return lumetri;
      } catch (e0) {
        lumetri = null;
      }
    }
    lumetri = getLumetriComponent(context.clip);
    if (!lumetri) return null;
    context.lumetriComponent = lumetri;
    try {
      if (lumetri.displayName && context.componentNameMap) {
        context.componentNameMap[lumetri.displayName.toString().toLowerCase()] =
          lumetri;
      }
    } catch (e1) {}
    return lumetri;
  }

  function findComponentByEffectEntryFromContext(context, entry, candidates) {
    if (!context || !entry) return null;
    var nameMap = context.componentNameMap || null;
    if (nameMap && candidates && candidates.length > 0) {
      for (var c = 0; c < candidates.length; c++) {
        var candidate = candidates[c];
        if (!candidate) continue;
        var mapped = nameMap[candidate.toString().toLowerCase()];
        if (mapped) return mapped;
      }
    }
    var resolved = findComponentByEffectEntry(context.clip, entry, candidates);
    if (resolved && resolved.displayName && nameMap) {
      try {
        nameMap[resolved.displayName.toString().toLowerCase()] = resolved;
      } catch (e0) {}
    }
    return resolved;
  }

  function resolveQEItemForTrackContext(context, qeTrack, qeItemIndex) {
    if (!context || !qeTrack) return null;
    if (context.qeResolvedAttempted && !context.qeItem) return null;
    if (context.qeItem) {
      try {
        var _ = context.qeItem.start;
        perfCounterInc("qeEffectContextReusedItems");
        return context.qeItem;
      } catch (e0) {
        context.qeItem = null;
        context.qeResolvedAttempted = false;
      }
    }
    if (typeof context.startSec !== "number") {
      context.qeResolvedAttempted = true;
      return null;
    }

    var qeItem = findQETrackItemAtStartInIndex(
      qeItemIndex,
      context.startSec,
      context.cleanNameRef,
    );
    if (!qeItem) {
      perfCounterInc("qeEffectFallbackSearches");
      qeItem = findQETrackItemAtStartInTrack(
        qeTrack,
        context.startSec,
        context.cleanNameRef,
      );
    }
    if (!qeItem && context.nameRef) {
      perfCounterInc("qeEffectFallbackSearches");
      qeItem = findQETrackItemAtStartInTrack(
        qeTrack,
        context.startSec,
        context.nameRef,
      );
    }
    if (!qeItem) {
      perfCounterInc("qeEffectFallbackSearches");
      qeItem = findQETrackItemAtStartInTrack(qeTrack, context.startSec, null);
    }

    if (qeItem) {
      context.qeItem = qeItem;
    }
    context.qeResolvedAttempted = true;
    return qeItem;
  }

  function buildQEMatchMapForTrack(trackClipContexts, qeTrack, qeItemIndex) {
    var stats = {
      totalClips: 0,
      matchedQEItems: 0,
      unmatchedQEItems: 0,
    };
    if (!trackClipContexts || !qeTrack) return stats;
    for (var i = 0; i < trackClipContexts.length; i++) {
      var context = trackClipContexts[i];
      if (!context || !context.clip) continue;
      stats.totalClips++;
      var qeItem = resolveQEItemForTrackContext(context, qeTrack, qeItemIndex);
      if (qeItem) {
        stats.matchedQEItems++;
        perfCounterInc("qeEffectPreMappedItems");
      } else {
        stats.unmatchedQEItems++;
      }
    }
    return stats;
  }

  function equalsIgnoreCase(a, b) {
    if (a === null || a === undefined || b === null || b === undefined)
      return false;
    return a.toString().toLowerCase() === b.toString().toLowerCase();
  }

  function findComponentByEffectEntry(clip, entry, precomputedCandidates) {
    if (!clip || !clip.components || !entry) return null;
    var candidates =
      precomputedCandidates ||
      getFallbackEffectNameCandidates(entry.matchName, entry.displayName);
    var components = clip.components;
    var compCount = components.numItems;
    for (var i = 0; i < compCount; i++) {
      var comp = components[i];
      if (!comp || !comp.displayName) continue;
      var compName = comp.displayName.toString();
      for (var c = 0; c < candidates.length; c++) {
        if (candidates[c] && equalsIgnoreCase(compName, candidates[c])) {
          return comp;
        }
      }
    }
    return null;
  }

  function applyNonLumetriPresetValuesToTrack(
    stdTrack,
    presetFilePath,
    presetData,
    trackClipContexts,
    preparedEntries,
  ) {
    var stats = {
      effectsWithValues: 0,
      clipsWithComponents: 0,
      clipsUpdated: 0,
      propWrites: 0,
      propFails: 0,
    };
    if (!stdTrack || !stdTrack.clips) return stats;

    var parsed = presetData || getPresetParsedData(presetFilePath);
    var entries = parsed ? parsed.nonLumetriEffectEntries : [];
    if (!entries || entries.length <= 0) return stats;

    var prepared =
      preparedEntries && preparedEntries.length > 0
        ? preparedEntries
        : buildEffectEntriesWithCandidates(entries);
    if (prepared.length <= 0) return stats;
    stats.effectsWithValues = prepared.length;

    var contexts = trackClipContexts || buildTrackClipContexts(stdTrack);
    for (var ci = 0; ci < contexts.length; ci++) {
      var context = contexts[ci];
      if (!context || !context.clip) continue;
      var clipWrites = 0;
      var hadComponent = false;

      for (var ei = 0; ei < prepared.length; ei++) {
        var preparedEntry = prepared[ei];
        var entry = preparedEntry.entry;
        var component = findComponentByEffectEntryFromContext(
          context,
          entry,
          preparedEntry.candidates,
        );
        if (!component || !component.properties) continue;
        hadComponent = true;

        var properties = component.properties;
        var propCount = properties.numItems;
        for (var vi = 0; vi < entry.values.length; vi++) {
          var setting = entry.values[vi];
          if (setting.index < 0 || setting.index >= propCount) continue;
          var prop = properties[setting.index];
          if (!prop) continue;
          if (setPropertyValueFast(prop, setting.value)) {
            stats.propWrites++;
            clipWrites++;
          } else {
            stats.propFails++;
          }
        }
      }

      if (hadComponent) stats.clipsWithComponents++;
      if (clipWrites > 0) stats.clipsUpdated++;
    }
    return stats;
  }

  function getFallbackEffectNameCandidates(matchName, displayName) {
    var candidates = [];
    var m = matchName ? matchName.toString() : "";
    var d = displayName ? displayName.toString() : "";
    var lower = m.toLowerCase();

    // Prefer display names first; QE can resolve these better than internal match names.
    pushUnique(candidates, d);

    if (
      m === "AE.ADBE Horizontal Flip" ||
      lower.indexOf("horizontal flip") !== -1
    ) {
      pushUnique(candidates, "Miroir horizontal");
      pushUnique(candidates, "Horizontal Flip");
    }
    if (m === "AE.ADBE Lumetri" || lower.indexOf("lumetri") !== -1) {
      pushUnique(candidates, "Couleur Lumetri");
      pushUnique(candidates, "Lumetri Color");
      pushUnique(candidates, "Lumetri");
    }

    var stripped = m.replace(/^AE\.ADBE\s*/g, "");
    if (stripped && stripped !== m) {
      pushUnique(candidates, stripped);
    }

    // Keep raw match name as last resort.
    pushUnique(candidates, m);
    return candidates;
  }

  function getQEItemStartTicks(qeItem) {
    if (!qeItem || !qeItem.start) return null;
    var startTicks = null;
    try {
      if (qeItem.start.ticks !== undefined) {
        startTicks = parseInt(qeItem.start.ticks, 10);
      }
    } catch (e0) {}

    if (typeof startTicks === "number" && !isNaN(startTicks)) {
      return startTicks;
    }

    try {
      if (typeof qeItem.start.seconds === "number") {
        return secondsToTicks(qeItem.start.seconds);
      }
      if (typeof qeItem.start.secs === "number") {
        return secondsToTicks(qeItem.start.secs);
      }
    } catch (e1) {}

    return null;
  }

  function getTrackItemComponentsCount(item) {
    try {
      if (item && item.components) return item.components.numItems;
    } catch (e) {}
    return -1;
  }

  function findQETrackItemAtStartInTrack(qeTrack, startSeconds, nameRef) {
    if (!qeTrack) return null;
    var targetTicks = secondsToTicks(startSeconds);
    var toleranceTicks = secondsToTicks(0.2);

    for (var i = qeTrack.numItems - 1; i >= 0; i--) {
      var qeItem = null;
      try {
        qeItem = qeTrack.getItemAt(i);
      } catch (e0) {}
      if (!qeItem) continue;

      var qeStartTicks = getQEItemStartTicks(qeItem);
      if (qeStartTicks === null) continue;
      if (Math.abs(qeStartTicks - targetTicks) > toleranceTicks) continue;

      if (nameRef) {
        var qeName = qeItem.name ? qeItem.name.toString() : "";
        if (!isItemNameMatch(qeName, nameRef)) continue;
      }
      return qeItem;
    }
    return null;
  }

  function buildQETrackItemIndex(qeTrack) {
    var index = {
      byTicks: {},
      byTicksName: {},
    };
    if (!qeTrack) return index;

    for (var i = qeTrack.numItems - 1; i >= 0; i--) {
      var qeItem = null;
      try {
        qeItem = qeTrack.getItemAt(i);
      } catch (e0) {}
      if (!qeItem) continue;

      var startTicks = getQEItemStartTicks(qeItem);
      if (startTicks === null) continue;

      var nameNorm = normalizeNameKey(
        qeItem.name ? qeItem.name.toString() : "",
      );
      var tickKey = startTicks.toString();
      var tickNameKey = tickKey + "|" + nameNorm;

      if (!index.byTicks[tickKey]) index.byTicks[tickKey] = [];
      index.byTicks[tickKey].push(qeItem);

      if (!index.byTicksName[tickNameKey]) index.byTicksName[tickNameKey] = [];
      index.byTicksName[tickNameKey].push(qeItem);
    }
    return index;
  }

  function findQETrackItemAtStartInIndex(index, startSeconds, nameRef) {
    if (!index) return null;
    var targetTicks = secondsToTicks(startSeconds);
    var targetNameKey = normalizeNameKey(nameRef || "");

    // Exact (ticks + name)
    if (targetNameKey) {
      var exactKey = targetTicks.toString() + "|" + targetNameKey;
      var exactItems = index.byTicksName[exactKey];
      if (exactItems && exactItems.length > 0) return exactItems[0];
    }

    // Fuzzy scan in +/- tolerance frames on indexed buckets.
    var toleranceFrames = Math.ceil(0.2 * SEQ_FPS);
    for (var frameOff = 0; frameOff <= toleranceFrames; frameOff++) {
      var offsets = frameOff === 0 ? [0] : [frameOff, -frameOff];
      for (var oi = 0; oi < offsets.length; oi++) {
        var ticks = targetTicks + offsets[oi] * TICKS_PER_FRAME;
        var key = ticks.toString();
        var items = index.byTicks[key];
        if (!items || items.length <= 0) continue;
        if (!nameRef) return items[0];
        for (var ii = 0; ii < items.length; ii++) {
          var item = items[ii];
          var itemName = item && item.name ? item.name.toString() : "";
          if (isItemNameMatch(itemName, nameRef)) {
            return item;
          }
        }
      }
    }
    return null;
  }

  function getQEVideoTrackAtSafe(qeSeq, idx) {
    if (!qeSeq || typeof idx !== "number" || idx < 0) return null;
    try {
      return qeSeq.getVideoTrackAt(idx);
    } catch (e) {}
    return null;
  }

  function scoreQETrackAgainstStandardTrack(stdTrack, qeTrack, maxSamples) {
    var score = { matches: 0, samples: 0 };
    if (!stdTrack || !stdTrack.clips || !qeTrack) return score;

    var total = stdTrack.clips.numItems;
    if (total <= 0) return score;

    var sampleLimit = Math.min(total, Math.max(1, maxSamples || 8));
    for (var i = 0; i < sampleLimit; i++) {
      var stdItem = stdTrack.clips[i];
      if (!stdItem) continue;
      var startSec = getTrackItemStartSeconds(stdItem);
      if (typeof startSec !== "number") continue;
      score.samples++;
      var nm = "";
      try {
        nm =
          stdItem.projectItem && stdItem.projectItem.name
            ? stdItem.projectItem.name.toString()
            : "";
      } catch (e0) {}
      nm = nm ? stripKnownExtension(nm) : "";
      if (findQETrackItemAtStartInTrack(qeTrack, startSec, nm)) {
        score.matches++;
      }
    }
    return score;
  }

  function resolveBestQEVideoTrackForStandardTrack(
    qeSeq,
    stdTrack,
    preferredIdx,
  ) {
    var bestTrack = getQEVideoTrackAtSafe(qeSeq, preferredIdx);
    var bestIdx = preferredIdx;
    var bestScore = scoreQETrackAgainstStandardTrack(stdTrack, bestTrack, 8);

    for (var i = 0; i < 16; i++) {
      var t = getQEVideoTrackAtSafe(qeSeq, i);
      if (!t) continue;
      var s = scoreQETrackAgainstStandardTrack(stdTrack, t, 8);
      if (s.matches > bestScore.matches) {
        bestTrack = t;
        bestIdx = i;
        bestScore = s;
      }
    }

    return { track: bestTrack, index: bestIdx, score: bestScore };
  }

  function applyQEEffectToTrackWithVerification(
    stdTrack,
    qeTrack,
    effectObj,
    qeItemIndex,
    trackClipContexts,
  ) {
    var stats = {
      totalClips: 0,
      matchedQEItems: 0,
      applyCalls: 0,
      verifiedChanges: 0,
      assumedChanges: 0,
      sampledVerifications: 0,
      strictFallbackMode: false,
      noChange: 0,
      failedCalls: 0,
    };
    if (!stdTrack || !stdTrack.clips || !qeTrack || !effectObj) return stats;

    var contexts = trackClipContexts || buildTrackClipContexts(stdTrack);
    var forceStrictVerification = false;
    for (var i = 0; i < contexts.length; i++) {
      var context = contexts[i];
      if (!context) continue;
      var stdItem = context.clip;
      if (!stdItem) continue;
      stats.totalClips++;

      var qeItem = resolveQEItemForTrackContext(context, qeTrack, qeItemIndex);
      if (!qeItem) continue;

      stats.matchedQEItems++;
      var shouldVerify =
        forceStrictVerification ||
        stats.sampledVerifications < QE_EFFECT_VERIFY_SAMPLE_CLIPS;
      var before = shouldVerify ? getTrackItemComponentsCount(stdItem) : -1;

      try {
        perfCounterInc("qeEffectApplyCalls");
        qeItem.addVideoEffect(effectObj);
        stats.applyCalls++;
      } catch (e1) {
        stats.failedCalls++;
        perfCounterInc("qeEffectApplyFailures");
        context.qeItem = null;
        context.qeResolvedAttempted = false;
        continue;
      }

      if (!shouldVerify || before < 0) {
        stats.assumedChanges++;
        continue;
      }

      stats.sampledVerifications++;
      var after = getTrackItemComponentsCount(stdItem);
      if (!(before >= 0 && after > before)) {
        var waitedMs = 0;
        while (waitedMs < QE_EFFECT_VERIFY_WAIT_MAX_MS) {
          var waitChunk = Math.min(
            QE_EFFECT_VERIFY_WAIT_STEP_MS,
            QE_EFFECT_VERIFY_WAIT_MAX_MS - waitedMs,
          );
          if (waitChunk <= 0) break;
          sleep(waitChunk);
          waitedMs += waitChunk;
          perfCounterInc("qeEffectVerifySleepMs", waitChunk);
          after = getTrackItemComponentsCount(stdItem);
          if (before >= 0 && after > before) break;
        }
      }
      if (before >= 0 && after > before) stats.verifiedChanges++;
      else {
        stats.noChange++;
        if (!forceStrictVerification) {
          forceStrictVerification = true;
          stats.strictFallbackMode = true;
        }
      }
    }
    return stats;
  }

  function applyVideoPresetToTrackItems(
    videoTrackIndex,
    presetName,
    presetFilePath,
    qeSeqOverride,
    qeTrackResolveCache,
  ) {
    var sequence = app.project ? app.project.activeSequence : null;
    if (
      !sequence ||
      !sequence.videoTracks ||
      sequence.videoTracks.numTracks <= videoTrackIndex
    ) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (missing sequence track V" +
          (videoTrackIndex + 1) +
          ").",
      );
      return false;
    }
    var stdTrack = sequence.videoTracks[videoTrackIndex];
    if (!stdTrack || !stdTrack.clips || stdTrack.clips.numItems <= 0) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (V" +
          (videoTrackIndex + 1) +
          " has no clips).",
      );
      return false;
    }

    app.enableQE();
    var qeSeq = qeSeqOverride || null;
    if (!qeSeq) {
      try {
        qeSeq = qe.project.getActiveSequence();
      } catch (e0) {}
    }
    if (!qeSeq) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (no active QE sequence).",
      );
      return false;
    }

    var resolveCache = qeTrackResolveCache || QE_TRACK_RESOLVE_CACHE;
    var resolveKey = "v" + videoTrackIndex;
    var qeTrackInfo = resolveCache[resolveKey];
    var useCachedTrack = false;
    if (qeTrackInfo && qeTrackInfo.track) {
      try {
        var _ = qeTrackInfo.track.numItems;
        useCachedTrack = true;
      } catch (eCached) {
        useCachedTrack = false;
      }
    }
    if (!useCachedTrack) {
      qeTrackInfo = resolveBestQEVideoTrackForStandardTrack(
        qeSeq,
        stdTrack,
        videoTrackIndex,
      );
      resolveCache[resolveKey] = qeTrackInfo;
    }
    if (!qeTrackInfo.track) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (no QE video track found).",
      );
      return false;
    }
    if (qeTrackInfo.index !== videoTrackIndex) {
      log(
        "Info: QE track remap for V" +
          (videoTrackIndex + 1) +
          " -> QE track " +
          qeTrackInfo.index +
          " (match " +
          qeTrackInfo.score.matches +
          "/" +
          qeTrackInfo.score.samples +
          ").",
      );
    }
    var qeItemIndex = buildQETrackItemIndex(qeTrackInfo.track);
    var presetData = getPresetParsedData(presetFilePath);
    var trackClipContexts = buildTrackClipContexts(stdTrack);
    var qeMatchStats = buildQEMatchMapForTrack(
      trackClipContexts,
      qeTrackInfo.track,
      qeItemIndex,
    );
    if (qeMatchStats.unmatchedQEItems > 0) {
      log(
        "Info: QE pre-map matched " +
          qeMatchStats.matchedQEItems +
          "/" +
          qeMatchStats.totalClips +
          " clips for '" +
          presetName +
          "'.",
      );
    }

    var totalVerified = 0;
    var totalAssumed = 0;
    var anyApplied = false;
    var unresolvedEffects = 0;
    var filterEntries = presetData ? presetData.filterEntries : [];
    var preparedFilterEntries = buildEffectEntriesWithCandidates(filterEntries);
    if (filterEntries.length <= 0) {
      var hint = "";
      try {
        var pf = new File(presetFilePath);
        hint = pf.exists
          ? " .prfpset exists but no filter entries were parsed."
          : " .prfpset file not found at: " + presetFilePath;
      } catch (e2) {}
      log(
        "Warning: Fallback unavailable for preset '" + presetName + "'." + hint,
      );
    } else {
      for (var eIdx = 0; eIdx < preparedFilterEntries.length; eIdx++) {
        var preparedEffect = preparedFilterEntries[eIdx];
        var label = preparedEffect.label;
        var candidates = preparedEffect.candidates;
        var effectAppliedForEntry = false;

        for (var c = 0; c < candidates.length; c++) {
          var candidateName = candidates[c];
          if (!candidateName) continue;
          var effectCandidate = resolveVideoEffectByName(candidateName);
          if (!effectCandidate) continue;

          var st = applyQEEffectToTrackWithVerification(
            stdTrack,
            qeTrackInfo.track,
            effectCandidate,
            qeItemIndex,
            trackClipContexts,
          );
          if (st.applyCalls > 0) anyApplied = true;
          log(
            "Fallback candidate '" +
              candidateName +
              "' for '" +
              label +
              "': verified " +
              st.verifiedChanges +
              "/" +
              st.totalClips +
              " clip(s), assumed " +
              st.assumedChanges +
              ", sampled checks " +
              st.sampledVerifications +
              (st.strictFallbackMode ? ", strict fallback enabled." : "."),
          );

          if (
            st.verifiedChanges > 0 ||
            (st.assumedChanges > 0 && st.failedCalls === 0 && st.noChange === 0)
          ) {
            totalVerified += st.verifiedChanges;
            totalAssumed += st.assumedChanges;
            effectAppliedForEntry = true;
            break;
          }
        }

        if (!effectAppliedForEntry) {
          unresolvedEffects++;
          log("Warning: Could not apply fallback for '" + label + "'.");
        }
      }
    }

    var preparedNonLumetriEntries = buildEffectEntriesWithCandidates(
      presetData ? presetData.nonLumetriEffectEntries : [],
    );
    var nonLumetriSync = applyNonLumetriPresetValuesToTrack(
      stdTrack,
      presetFilePath,
      presetData,
      trackClipContexts,
      preparedNonLumetriEntries,
    );
    var nonLumetriWrites = nonLumetriSync.propWrites;
    if (
      nonLumetriSync.effectsWithValues > 0 &&
      nonLumetriSync.clipsWithComponents > 0
    ) {
      log(
        "Effect values synced from .prfpset (non-Lumetri): " +
          nonLumetriSync.clipsUpdated +
          "/" +
          nonLumetriSync.clipsWithComponents +
          " clip(s), " +
          nonLumetriWrites +
          " write(s), " +
          nonLumetriSync.propFails +
          " fail(s).",
      );
    }

    var lumetriSync = applyLumetriPresetValuesToTrack(
      stdTrack,
      presetFilePath,
      presetData,
      trackClipContexts,
    );
    var lumetriWrites = lumetriSync.propWrites;
    if (lumetriSync.settingsApplied > 0 && lumetriSync.clipsWithLumetri > 0) {
      log(
        "Lumetri values synced from .prfpset: " +
          lumetriSync.clipsUpdated +
          "/" +
          lumetriSync.clipsWithLumetri +
          " clip(s), " +
          lumetriWrites +
          " write(s), " +
          lumetriSync.propFails +
          " fail(s).",
      );
    }

    var lookPathSync = applyLumetriLookPathToTrack(
      stdTrack,
      presetFilePath,
      presetData,
      trackClipContexts,
    );
    var lookPathWrites = lookPathSync.propWrites;
    if (lookPathSync.resolvedPath) {
      log("Lumetri Look path resolved: " + lookPathSync.resolvedPath);
    } else {
      log(
        "Warning: Lumetri Look path not found in preset '" + presetName + "').",
      );
    }
    if (lookPathSync.candidateCount > 0 && lookPathSync.clipsWithLumetri > 0) {
      log(
        "Lumetri Look path synced from .prfpset: " +
          lookPathSync.clipsUpdated +
          "/" +
          lookPathSync.clipsWithLumetri +
          " clip(s), " +
          lookPathWrites +
          " write(s), " +
          lookPathSync.propFails +
          " fail(s), path mode: " +
          (lookPathSync.pathUsed || "none") +
          ".",
      );
    }

    if (
      totalVerified > 0 ||
      totalAssumed > 0 ||
      nonLumetriWrites > 0 ||
      lumetriWrites > 0 ||
      lookPathWrites > 0
    ) {
      log(
        "Preset pipeline completed for '" +
          presetName +
          "' on V" +
          (videoTrackIndex + 1) +
          " (verified component changes: " +
          totalVerified +
          ", assumed component changes: " +
          totalAssumed +
          ", non-Lumetri writes: " +
          nonLumetriWrites +
          ", lumetri writes: " +
          lumetriWrites +
          ", lumetri look path writes: " +
          lookPathWrites +
          ").",
      );
      return true;
    }

    log(
      "Warning: Preset pipeline finished for '" +
        presetName +
        "' but no verifiable component change was detected." +
        (anyApplied
          ? " QE may be swallowing preset/effect application in this Premiere build."
          : " No QE effect application call succeeded.") +
        (unresolvedEffects > 0
          ? " Unresolved effects from preset: " + unresolvedEffects + "."
          : ""),
    );
    return false;
  }

  function setScaleAndPosition(track, startTime, scaleVal) {
    if (!track) return;
    var item = findRecentTrackItemAtStart(track, startTime, null);
    if (!item) {
      item = findTrackItemAtStart(track, startTime, null);
    }
    if (item) {
      setScaleOnItem(item, scaleVal);
    }
  }

  function placeOverlayOnTrack(track, filename, endSec) {
    if (!track || !filename || typeof endSec !== "number" || !(endSec > 0))
      return false;

    var overlayItem = getOrImportClip(filename);
    if (!overlayItem) {
      log("Warning: Overlay not found: " + filename);
      return false;
    }

    try {
      track.overwriteClip(overlayItem, 0);
    } catch (e0) {
      log(
        "Warning: Failed to place overlay '" +
          filename +
          "': " +
          (e0 && e0.message ? e0.message : e0),
      );
      return false;
    }

    var filenameNoExt = stripKnownExtension(filename);
    var item =
      resolvePlacedItemFast(track, 0, filenameNoExt, TRACK_ITEM_WAIT_MAX_MS) ||
      resolvePlacedItemFast(track, 0, filename, TRACK_ITEM_WAIT_MAX_MS) ||
      findTrackItemAtStart(track, 0, null);

    if (!item) {
      log(
        "Warning: Overlay track item not found on timeline for '" +
          filename +
          "'.",
      );
      return false;
    }

    if (!setTrackItemEndSeconds(item, endSec)) {
      log(
        "Warning: Could not trim overlay '" +
          filename +
          "' to " +
          endSec +
          "s.",
      );
      return false;
    }
    return true;
  }

  function duplicateRawSceneAudioToTrack(
    a4,
    scenes,
  ) {
    if (!a4 || !scenes || scenes.length <= 0) return;

    log("Duplicating raw scene audio to A4...");
    for (var i = 0; i < scenes.length; i++) {
      var s = scenes[i];
      if (!s || !s.is_raw) continue;

      var startSec = snapSecondsToFrame(s.start);
      var subclipName = buildRawAudioSubclipName(s);
      var subclip = getOrCreateRawAudioSubclip(s);
      if (!subclip) {
        log(
          "Warning: Could not resolve raw audio source for scene " +
            s.scene_index +
            ".",
        );
        continue;
      }

      try {
        a4.overwriteClip(subclip, startSec);
      } catch (eOverwrite) {
        log(
          "Warning: Failed to place raw audio on A4 for scene " +
            s.scene_index +
            ": " +
            (eOverwrite && eOverwrite.message ? eOverwrite.message : eOverwrite),
        );
        continue;
      }

      var a4Item =
        resolvePlacedItemFast(a4, startSec, subclipName, TRACK_ITEM_WAIT_MAX_MS) ||
        findTrackItemAtStart(a4, startSec, subclipName) ||
        findTrackItemAtStart(a4, startSec, null);
      if (!a4Item) {
        log(
          "Warning: Could not resolve raw audio clip on A4 for scene " +
            s.scene_index +
            ".",
        );
      }
    }
  }

  function scoreAudioGainProperty(prop) {
    if (!prop) return -1;
    var propName = prop.displayName
      ? prop.displayName.toString().toLowerCase()
      : "";
    var propMatch = prop.matchName
      ? prop.matchName.toString().toLowerCase()
      : "";

    if (
      propName.indexOf("mute") !== -1 ||
      propName.indexOf("muet") !== -1 ||
      propName.indexOf("sourdine") !== -1 ||
      propName.indexOf("bypass") !== -1 ||
      propMatch.indexOf("mute") !== -1 ||
      propMatch.indexOf("bypass") !== -1
    ) {
      return -1;
    }

    if (propName === "level" || propName === "niveau") return 100;
    if (propName === "volume level" || propName === "gain") return 95;
    if (propName.indexOf("volume level") !== -1) return 90;
    if (
      propName.indexOf("level") !== -1 ||
      propName.indexOf("niveau") !== -1 ||
      propName.indexOf("gain") !== -1
    ) {
      return 80;
    }
    if (propMatch.indexOf("level") !== -1 || propMatch.indexOf("gain") !== -1) {
      return 70;
    }
    return -1;
  }

  function applyTrackItemGainDb(trackItem, gainDb) {
    if (!trackItem || typeof gainDb !== "number" || !trackItem.components)
      return false;

    var itemName = trackItem.name ? trackItem.name.toString() : "audio-item";
    var volumeComponent = null;
    for (var c = 0; c < trackItem.components.numItems; c++) {
      var comp = trackItem.components[c];
      if (!comp) continue;
      var compName = comp.displayName
        ? comp.displayName.toString().toLowerCase()
        : "";
      var compMatch = comp.matchName
        ? comp.matchName.toString().toLowerCase()
        : "";
      if (
        compName.indexOf("volume") !== -1 ||
        compMatch.indexOf("volume") !== -1
      ) {
        volumeComponent = comp;
        break;
      }
    }
    if (!volumeComponent || !volumeComponent.properties) {
      log(
        "Warning: Music gain skipped for '" +
          itemName +
          "' (no Volume component).",
      );
      return false;
    }

    var targetProp = null;
    var targetScore = -1;
    for (var p = 0; p < volumeComponent.properties.numItems; p++) {
      var prop = volumeComponent.properties[p];
      if (!prop) continue;
      var score = scoreAudioGainProperty(prop);
      if (score > targetScore) {
        targetScore = score;
        targetProp = prop;
      }
    }

    if (!targetProp || targetScore < 0) {
      log(
        "Warning: Music gain skipped for '" +
          itemName +
          "' (no safe Level/Gain property found).",
      );
      return false;
    }

    var refRaw = AUDIO_GAIN_RAW_REFERENCE_AT_0DB;
    if (!(typeof refRaw === "number" && isFinite(refRaw) && refRaw > 0)) {
      try {
        if (targetProp.getValue) {
          var rawCandidate = targetProp.getValue();
          if (typeof rawCandidate === "string") {
            rawCandidate = parseFloat(rawCandidate);
          }
          if (
            typeof rawCandidate === "number" &&
            isFinite(rawCandidate) &&
            rawCandidate > 0
          ) {
            refRaw = rawCandidate;
          }
        }
      } catch (eRefRead) {}
    }
    if (!(typeof refRaw === "number" && isFinite(refRaw) && refRaw > 0)) {
      refRaw = AUDIO_GAIN_RAW_FALLBACK_AT_0DB;
    }
    AUDIO_GAIN_RAW_REFERENCE_AT_0DB = refRaw;

    var rawTarget = refRaw * Math.pow(10, gainDb / 20);
    if (
      !(typeof rawTarget === "number" && isFinite(rawTarget) && rawTarget > 0)
    ) {
      log(
        "Warning: Music gain skipped for '" +
          itemName +
          "' (invalid raw target from " +
          gainDb +
          " dB).",
      );
      return false;
    }

    if (!setPropertyValueFast(targetProp, rawTarget)) {
      var targetName = targetProp.displayName
        ? targetProp.displayName.toString()
        : "unknown-property";
      log(
        "Warning: Music gain write failed on '" +
          itemName +
          "' property '" +
          targetName +
          "'.",
      );
      return false;
    }

    try {
      if (targetProp.getValue) {
        var readBack = targetProp.getValue();
        if (typeof readBack === "string") {
          readBack = parseFloat(readBack);
        }
        if (
          typeof readBack !== "number" ||
          !isFinite(readBack) ||
          readBack <= 0
        ) {
          log(
            "Warning: Music gain readback looks invalid on '" +
              itemName +
              "' after write (value: " +
              readBack +
              ").",
          );
        }
      }
    } catch (eReadBack) {}
    return true;
  }

  function buildLoopedMusicBed(track, musicItem, targetEndSec, gainDb) {
    if (
      !track ||
      !musicItem ||
      typeof targetEndSec !== "number" ||
      !(targetEndSec > 0)
    ) {
      return false;
    }

    var clipName = musicItem.name ? musicItem.name.toString() : "";
    var clipNameNoExt = stripKnownExtension(clipName);
    var minStep = 1 / SEQ_FPS;
    var cursor = 0;
    var maxLoops = 2000;

    for (var loopCount = 0; loopCount < maxLoops; loopCount++) {
      if (cursor >= targetEndSec - minStep / 2) return true;

      try {
        track.overwriteClip(musicItem, cursor);
      } catch (e0) {
        log("Warning: Music placement failed at " + cursor + "s.");
        return false;
      }

      var placedItem =
        resolvePlacedItemFast(
          track,
          cursor,
          clipNameNoExt,
          TRACK_ITEM_WAIT_MAX_MS,
        ) ||
        resolvePlacedItemFast(
          track,
          cursor,
          clipName,
          TRACK_ITEM_WAIT_MAX_MS,
        ) ||
        findTrackItemAtStart(track, cursor, null);

      if (!placedItem) {
        log("Warning: Could not resolve placed music clip at " + cursor + "s.");
        return false;
      }

      if (!applyTrackItemGainDb(placedItem, gainDb)) {
        log(
          "Warning: Could not set music gain to " +
            gainDb +
            " dB on segment at " +
            cursor.toFixed(3) +
            "s for '" +
            (clipNameNoExt || clipName || "music") +
            "'.",
        );
      }

      var placedStart = getTrackItemStartSeconds(placedItem);
      if (typeof placedStart !== "number") placedStart = cursor;
      var placedEnd = getTrackItemEndSeconds(placedItem);
      if (
        typeof placedEnd !== "number" ||
        placedEnd <= placedStart + 0.000001
      ) {
        log("Warning: Invalid music clip duration at " + cursor + "s.");
        return false;
      }

      if (placedEnd >= targetEndSec - minStep / 2) {
        setTrackItemEndSeconds(placedItem, targetEndSec);
        return true;
      }

      var nextCursor = snapSecondsToFrame(placedEnd);
      if (!(nextCursor > placedStart + 0.000001)) {
        nextCursor = snapSecondsToFrame(placedStart + minStep);
      }
      cursor = nextCursor;
    }

    log("Warning: Music loop guard reached before hitting target duration.");
    return false;
  }

  function ensureVideoTracks(sequence, desiredCount) {
    if (!sequence || !sequence.videoTracks) return;
    var existing = sequence.videoTracks.numTracks;
    if (existing >= desiredCount) return;

    app.enableQE();
    var qeSeq = qe.project.getActiveSequence();
    if (!qeSeq) return;
    var toAdd = desiredCount - existing;
    try {
      // addTracks(videoCount, insertAfterVideoIdx, audioCount)
      qeSeq.addTracks(toAdd, Math.max(0, existing - 1), 0);
    } catch (e) {
      // fallback
      for (var i = 0; i < toAdd; i++) {
        try {
          qeSeq.addTracks(1, Math.max(0, existing - 1 + i), 0);
        } catch (e2) {}
      }
    }
  }

  function ensureAudioTracks(sequence, desiredCount) {
    if (!sequence || !sequence.audioTracks) return;
    var existing = sequence.audioTracks.numTracks;
    if (existing >= desiredCount) return;

    app.enableQE();
    var qeSeq = qe.project.getActiveSequence();
    if (!qeSeq) return;
    var toAdd = desiredCount - existing;
    try {
      // addTracks(video, insertAfterVideo, audio, insertAfterAudio)
      qeSeq.addTracks(0, 0, toAdd, Math.max(0, existing - 1));
    } catch (e) {
      for (var i = 0; i < toAdd; i++) {
        try {
          qeSeq.addTracks(0);
        } catch (e2) {}
      }
    }
  }

  function purgeProjectCompletely() {
    if (!app.project || !app.project.rootItem) return false;
    var root = app.project.rootItem;
    var hadWarnings = false;

    try {
      if (
        app.project.sequences &&
        app.project.sequences.numSequences !== undefined
      ) {
        for (var s = app.project.sequences.numSequences - 1; s >= 0; s--) {
          var seq = app.project.sequences[s];
          if (!seq) continue;
          var deleted = false;
          try {
            if (seq.sequenceID !== undefined) {
              deleted = app.project.deleteSequence(seq.sequenceID);
            }
          } catch (e0) {}
          if (!deleted) {
            try {
              deleted = app.project.deleteSequence(seq);
            } catch (e1) {}
          }
          if (!deleted) {
            hadWarnings = true;
            var seqName = "";
            try {
              seqName = seq.name ? seq.name.toString() : "sequence#" + s;
            } catch (e2) {
              seqName = "sequence#" + s;
            }
            log("Warning: Could not delete sequence '" + seqName + "'.");
          }
        }
      }
    } catch (eSeq) {
      hadWarnings = true;
      log(
        "Warning: Sequence purge failed: " +
          (eSeq && eSeq.message ? eSeq.message : eSeq),
      );
    }

    var purgeBin = null;
    try {
      purgeBin = root.createBin(PROJECT_PURGE_BIN_NAME);
    } catch (eCreate) {}
    if (!purgeBin) {
      log(
        "Error: Could not create purge bin '" + PROJECT_PURGE_BIN_NAME + "'.",
      );
      return false;
    }

    var moveGuard = 0;
    while (root.children && root.children.numItems > 1 && moveGuard < 10000) {
      moveGuard++;
      var movedInPass = false;

      for (var i = root.children.numItems - 1; i >= 0; i--) {
        var child = root.children[i];
        if (!child || child === purgeBin) continue;
        try {
          child.moveBin(purgeBin);
          movedInPass = true;
        } catch (eMove) {
          hadWarnings = true;
          var childName = "";
          try {
            childName = child.name ? child.name.toString() : "item#" + i;
          } catch (eName) {
            childName = "item#" + i;
          }
          log(
            "Warning: Could not move item '" + childName + "' into purge bin.",
          );
        }
      }

      if (!movedInPass) break;
    }
    if (moveGuard >= 10000) {
      hadWarnings = true;
      log("Warning: Purge guard reached while moving project items.");
    }

    var deletedBin = false;
    try {
      deletedBin = purgeBin.deleteBin();
    } catch (eDel0) {}
    if (!deletedBin) {
      try {
        app.project.deleteBin(purgeBin);
        deletedBin = true;
      } catch (eDel1) {}
    }
    if (!deletedBin) {
      log(
        "Error: Could not delete purge bin '" + PROJECT_PURGE_BIN_NAME + "'.",
      );
      return false;
    }

    PROJECT_ITEM_CACHE = {};
    PROJECT_ITEM_CACHE_WARMED = false;

    var remainingSequences = 0;
    try {
      remainingSequences =
        app.project.sequences &&
        app.project.sequences.numSequences !== undefined
          ? app.project.sequences.numSequences
          : 0;
    } catch (e3) {
      remainingSequences = 0;
    }
    var remainingRootItems = 0;
    try {
      remainingRootItems = root.children ? root.children.numItems : 0;
    } catch (e4) {
      remainingRootItems = 0;
    }

    if (remainingSequences > 0 || remainingRootItems > 0) {
      log(
        "Error: Project purge incomplete (sequences: " +
          remainingSequences +
          ", root items: " +
          remainingRootItems +
          ").",
      );
      return false;
    }

    if (hadWarnings) {
      log("Info: Purge completed with warnings but final state is clean.");
    }
    return true;
  }

  function cleanupAudioTracks(ttsTrackIndex, ttsName) {
    var seq = app.project.activeSequence;
    if (!seq || !seq.audioTracks) return;
    for (var i = 0; i < seq.audioTracks.numTracks; i++) {
      if (i === 0) continue; // keep A1 (source audio, muted)
      if (i === 3) continue; // keep A4 (raw scene audio, active)
      var track = seq.audioTracks[i];
      if (!track || !track.clips) continue;
      for (var j = track.clips.numItems - 1; j >= 0; j--) {
        var clip = track.clips[j];
        var nm = clip && clip.projectItem ? clip.projectItem.name : "";
        var keep = i === ttsTrackIndex && nm === ttsName;
        if (!keep) {
          try {
            clip.remove(false, true);
          } catch (e1) {
            try {
              clip.remove();
            } catch (e2) {}
          }
        }
      }
    }
  }

  main();
})();
