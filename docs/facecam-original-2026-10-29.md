# Facecam : une présentation originale depuis l’anime

**Accès direct ajouté :** l’option **Présentation originale**, dans « Choisir l’histoire à raconter »,
permet maintenant de démarrer sans analyser une histoire source et sans payer un premier script
intermédiaire. Le choix lui-même ne déclenche aucun appel IA. La fiche recherchée pour les propositions
d’accroche et d’avis est réutilisée par la première rédaction, y compris après un échec de celle-ci.
On peut aussi saisir directement son accroche, puis lancer la rédaction.

Le bouton **Recréer de zéro** apparaît après la génération d’un script, dans « Affiner votre script ».
Il conserve exactement l’accroche et le point de vue actuellement présents dans l’éditeur, y compris
les retouches manuelles. Il construit une nouvelle présentation depuis une recherche sur l’œuvre.
L’ancien corps, l’histoire sélectionnée, ses étapes, sa fin, ses découpes de mots et ses dialogues
ne sont transmis ni à la recherche ni aux rédacteurs.

Choix confirmés pour ce format : **60–75 secondes**, prémisse et début de l’histoire, **sans gros
spoilers**, narration continue **sans extraits audio originaux**. Les plans de narration disponibles
restent des illustrations pour le montage. Le titre conserve sa révélation finale habituelle.

## Corpus : les 50 publications les plus récentes accessibles

Le profil [theomcx](https://www.tiktok.com/@theomcx) a été interrogé le **29 octobre 2026**.
Les 65 publications candidates ont été dédupliquées et classées par date décroissante, indépendamment
des épingles, puis limitées aux 50 premières. La sélection est figée, sans remplacement en cas d’échec.
Elle couvre le **2 octobre 2025 au 28 octobre 2026** : il s’agit des dernières publications, pas
d’un échantillon de 50 jours. La plus récente est la [rencontre dans le dernier train](https://www.tiktok.com/@theomcx/video/7701752831459495201).

**50 vidéos avec audio, 50 transcriptions complètes, 50 annotations**, environ **48 min 11 s**.
Quinze vidéos/transcriptions du corpus du 23 octobre ont été réutilisées après contrôle de leur
empreinte et de leurs pistes ; 35 ont été téléchargées et transcrites pour cette étude. Les fichiers
annoncés comme sonores mais dépourvus d’audio ont été récupérés avec le format `download` de la même
publication. Cette reprise est maintenant intégrée au collecteur.

Les transcriptions utilisent `faster-whisper`, modèle **large-v3**, et le contrôle de couverture vocale
du projet. Aucun intervalle vocal non couvert n’est signalé dans ce corpus. Cela ne signifie pas
une transcription sans erreurs : les noms propres, homophones, répétitions et détails de phrases
restent parfois incertains. La ponctuation ASR n’est pas une mesure fiable de la syntaxe orale.
Les textes ont été lus intégralement par l’agent, en complément d’une annotation LLM par vidéo dont
les deux courts extraits justificatifs sont contrôlés contre le texte. Ce n’est pas une écoute
humaine des 50 vidéos ni une analyse visuelle exhaustive du montage.

L’[inventaire des 50 publications](facecam-original-corpus-2026-10-29.csv) conserve l’ordre, la date,
l’URL, le format, les mesures, une observation individuelle et les empreintes des vidéos/transcriptions.
Les catégories ont été revues avec les descriptions : Bleach/Disney+ et Galaxias/Pika sont classés
comme promotions malgré leur structure narrative. Les promotions incluent aussi les annonces
renvoyant vers une sélection en vidéo longue.

| Format dominant après revue | Nombre |
| --- | ---: |
| Présentation individuelle de fiction | 32 |
| Promotion commerciale ou d’une autre vidéo | 13 |
| Annonce de compte ou de playlists | 3 |
| Liste de recommandations | 1 |
| Autre / plaisanterie | 1 |

Sur les **32 présentations individuelles**, la durée médiane est **62,9 s**, pour **237 mots ASR**
après retrait de l’intro de marque lorsqu’elle est reconnue en ouverture. Les durées vont de
40,85 à 87,05 s et les textes de 153 à 337 mots. Le titre est prononcé en fin dans 28 cas ; Dreamland
est nommé dans le développement, et trois présentations renvoient au titre affiché sans le prononcer.
Ces mesures ne prouvent ni une rétention ni l’efficacité causale d’une formule. Les mots comptés
incluent l’ouverture utile, le corps et la conclusion, pas seulement la partie à générer.

## Ce qui structure les présentations

### Une singularité devient immédiatement un problème personnel

Une règle abstraite reçoit très vite un coût ou un objectif humain. Dans
[Dreamland](https://www.tiktok.com/@theomcx/video/7698402215358237984), la règle des pouvoirs liés
aux peurs s’articule à la perte maternelle ; la recherche d’un proche donne un sens à l’exploration.
Dans [L’Atelier des Sorciers](https://www.tiktok.com/@theomcx/video/7626417459486149910), le secret
de la magie entraîne un dommage personnel et un désir de réparation. L’exposition fait avancer
la motivation au lieu d’interrompre l’histoire.

Transfert : le générateur cherche une règle et ce qu’elle change pour quelqu’un. Il ne commence pas
par une fiche de monde, une époque, trois institutions et cinq noms. Le détail arrive avant
l’action qui en a besoin, et seulement s’il aide à comprendre l’enjeu.

### Le moteur dépend du genre et de l’œuvre

Le corpus ne justifie pas un unique schéma de héros humilié puis surpuissant. Plusieurs moteurs
coexistent, parfois dans la même présentation :

| Moteur | Exemple observé | Fonction dans le script |
| --- | --- | --- |
| Règle et exception | [L’école aux cheveux longs](https://www.tiktok.com/@theomcx/video/7681323019737763104) | La sanction rend la règle sensible ; un personnage permet de la mettre à l’épreuve. |
| Réussite qui crée un problème | [Black Torch](https://www.tiktok.com/@theomcx/video/7659806417624239392) | La survie et le pouvoir entraînent un statut dangereux. |
| Pouvoir qui ne résout pas le vrai obstacle | [Kill Blue](https://www.tiktok.com/@theomcx/video/7627886716187151638) | L’expertise de tueur ne règle pas l’inadaptation sociale. |
| Routine changée par une rencontre | [Le dernier train](https://www.tiktok.com/@theomcx/video/7701752831459495201) | Chaque information nouvelle modifie la relation et le regard sur l’autre. |
| Règle relationnelle qui bloque un désir partagé | [I Want to End This Love Game](https://www.tiktok.com/@theomcx/video/7629005813876821270) | Un jeu explique simultanément l’attirance, la résistance et le malentendu. |
| Recherche bloquée relancée par une rencontre | [Mao](https://www.tiktok.com/@theomcx/video/7626039999267392790) | Le nouveau personnage apporte un lien concret avec le problème initial. |

Transfert : la fiche identifie un moteur avant l’écriture. L’action peut être intense, la romance
gênée ou chaleureuse, le mystère construit autour d’un objet. Aucune menace de mort, compétition
ou revanche n’est ajoutée pour remplir une structure.

### La condensation vient du choix des informations

Dans [Overgeared](https://www.tiktok.com/@theomcx/video/7690246072660135200), un contraste entre
niveau et équipement explique l’avantage sans détailler toutes les statistiques. Dans
[Blades of the Guardians](https://www.tiktok.com/@theomcx/video/7579310126318882070), une contrainte
mesurable et la présence de l’enfant suffisent à démontrer la puissance du personnage. Dans
[The Faraway Paladin](https://www.tiktok.com/@theomcx/video/7645329223363726625), les étapes
d’apprentissage sont regroupées par compétences avant l’épreuve qui en montre le résultat.

La leçon n’est pas de raccourcir toutes les phrases. Une règle, une preuve et une conséquence
peuvent remplacer une liste de capacités ou une scène racontée geste par geste. Le moteur prépare
une réserve d'informations utiles plus riche que le futur script, puis sélectionne celles qui
portent la présentation. Aucun quota ne doit pousser à empiler des anecdotes faibles.
Il peut écarter un nom, un trajet, une discussion et un objet sans perdre le mécanisme de l’œuvre.
Une information déjà contenue dans le hook est acquise ; le corps avance vers ce qu’elle déclenche.

Les premiers essais avaient encore tendance à raconter une rencontre à l’université en détail.
Les consignes ont donc été renforcées sur ce point précis : présenter la relation et ce qu’elle
met en mouvement, sans rejouer les invitations, les présentations et les déplacements.

La demande de réduire le descriptif avait conduit à pousser l'ajout de développements et à
limiter la mise en place à deux phrases/environ un quart du corps. Le retour du créateur sur
**Dragon Hatching** a montré que ce réglage dégradait la narration en succession d'événements.
Ces contraintes ont été retirées. Le moteur du récit et la promesse du hook guident désormais
la sélection : une règle précise, une motivation établie ou une conséquence personnelle peuvent
approfondir le même enjeu, sans ajouter la scène suivante. La réécriture préserve une construction
qui fonctionne et les liaisons ou réactions utiles, en corrigeant les passages descriptifs faibles.
La cible reste 60–75 secondes, sans nouvelle passe payante ni gros spoiler. Le futur proche reste
le temps principal des événements, avec les exceptions pertinentes.

Une relecture ciblée de sept transcriptions du corpus confirme pourquoi le contexte ne doit pas
être éliminé mécaniquement. [I Want to End This Love Game](https://www.tiktok.com/@theomcx/video/7629005813876821270)
développe une même règle et le malentendu qu'elle entretient. [Kill Blue](https://www.tiktok.com/@theomcx/video/7627886716187151638)
fait porter sa présentation sur le décalage entre les compétences du personnage et sa mission.
[Le dernier train](https://www.tiktok.com/@theomcx/video/7701752831459495201) raconte davantage une
scène, mais ses détails successifs transforment la gêne, la complicité et la compréhension de
l'identité de l'autre. Une scène n'est donc pas exclue par principe : son intérêt commande sa place.
Il s'agit d'une relecture du corpus déjà collecté, sans nouvelle collecte ni nouvelle transcription.

Le cas Dragon Hatching a aussi révélé une incohérence : l'identité d'humain réincarné était dans
`premise` et `protagonist`, mais la consigne limitait le corps à `setup_facts`, centré sur les
péripéties. Ces champs de contexte sont maintenant utilisables. La fiche fraîche doit également
conserver les éléments essentiels de prémisse parmi ses faits cités. Les incertitudes priment
sur tous les champs ; `narrative_engine` reste un guide narratif et non une preuve supplémentaire.

### Une narration liée, avec des conséquences nettes

Le présent installe souvent une règle ou une situation ; le futur proche accompagne les changements.
Des phrases assez longues gardent ensemble une intention, une action et son résultat, puis une
phrase brève donne du poids à la conséquence. Les connecteurs d’opposition sont fréquents, mais
leur présence seule ne produit aucune progression. Une relance fait évoluer la compréhension
ou l'attente ; elle n'exige pas un événement supplémentaire.

Dans [The Vermilion Mask](https://www.tiktok.com/@theomcx/video/7695797702210243873), se défendre
risque de reproduire le problème qui a créé la quête. Dans
[Iruma-kun](https://www.tiktok.com/@theomcx/video/7581885479893683478), les aides du protecteur
contrarient le désir de discrétion. Le lien entre les événements compte davantage que la répétition
d’une formule annonçant une bascule.

Transfert : relier les faits par leurs causes réelles, varier la longueur et conserver une voix
continue. Aucun quota de mots par phrase, aucune relance automatique, aucun faux lien causal pour
faire paraître une succession plus spectaculaire.

### Des mots qui font comprendre le rapport de force

Les niveaux, la perte de statut, la honte, l’exclusion ou le risque d’être découvert donnent des
images immédiatement compréhensibles. Le vocabulaire familier change selon le sujet : un terme
de jeu sert un système de progression ; une romance repose davantage sur la gêne et la complicité.
Un intensif peut donner du relief, mais ne remplace pas le fait qui rend la situation remarquable.

Transfert : verbes directs, négations orales possibles, quelques accents expressifs et peu de
vocabulaire de critique. Les prompts comportent des exemples originaux de reformulation et écartent
les formulations qui commentent la narration au lieu de raconter. La deuxième passe reprend aussi
les paragraphes : elle ne se limite pas à remplacer des mots par des synonymes familiers.

### Une fin qui ouvre la promesse de la série

Beaucoup de présentations quittent leur situation initiale quand le conflit durable ou la relation
centrale devient clair. Le spectateur sait pourquoi la suite peut l’intéresser, sans recevoir toute
la résolution. Une scène peut être résolue avant cette ouverture : laisser tout incompréhensible
n’est pas nécessaire pour susciter la curiosité.

Le corpus contient également des révélations importantes, des superlatifs, des CTA et des informations
commerciales. Leur présence est décrite, mais les choix du produit priment : pas de gros spoilers
nouveaux, pas de signature de créateur ni de CTA ajouté, pas de jugement technique ou de disponibilité
inventé. La conclusion personnelle reste celle choisie par l’utilisateur ; sans point de vue,
le récit passe directement à la phrase de révélation du titre.

## Une chaîne réellement indépendante de l’ancienne histoire

1. L’autosauvegarde termine les modifications de l’éditeur avant l’envoi de la révision.
2. La recherche reçoit uniquement l’identité de l’anime, le hook, le point de vue et le périmètre
   de spoilers. Elle consulte des sources web et en conserve les citations.
3. Une fiche structurée distingue prémisse, protagoniste, moteur, faits du début, frontière de
   spoilers et incertitudes. Chaque fait doit citer une URL effectivement renvoyée par la recherche.
   Une fiche vide ou des références inventées font échouer l’opération sans remplacer le brouillon.
4. Le rédacteur construit un nouveau corps, sans anciens paragraphes, plans, dialogues ou histoire.
   La longueur dépend de la cible de 60–75 s et des textes fixes, pas de la longueur du récit source.
5. Une passe distincte reprend la sélection des détails, l’oralité, les répétitions et la fidélité
   aux faits. L’application conserve les textes fixes et impose un corps unique non vide.
6. Le changement de mode, le script et l’invalidation de l’ancien montage sont enregistrés ensemble
   sous contrôle de révision. Une recherche, une réponse invalide ou une modification concurrente
   conserve le brouillon précédent. La vérification factuelle habituelle suit la génération.

Les réponses payantes, la recherche et l’état précédent sont archivés dans
`facecam/generations/original-<id>.json`. Les sources de la nouvelle fiche sont consultables dans
l’interface. Le modèle reste celui sélectionné dans le projet. Une nouvelle génération ou de
nouvelles propositions en mode original continuent d’utiliser le contexte de l’anime. Revenir à
une histoire source rétablit le mode antérieur et ses extraits protégés.

La répartition des mots sur les illustrations intervient **après** l’écriture. Elle ne réécrit pas
le texte. Les références originales demeurent intactes ; le nouveau montage écarte les passages
audio originaux et réutilise une illustration existante pour la conclusion. Cela conserve le
parcours de validation, prompteur, enregistrement, traitement et export. Les images disponibles
ne constituent pas une nouvelle recherche visuelle de tous les événements racontés.
Si la source contient plus de plans que le nouveau texte n’a de mots à répartir, une sélection
répartie du premier au dernier plan permet la validation sans allonger le script. Les plans non
retenus restent candidats pour la piste d’illustrations de l’éditeur.

La durée reste une estimation à 3,35 mots/s, carte finale comprise ; la lecture réelle décide.
Conserver mot pour mot un hook déjà inexact ou révélateur d’un spoiler ne le corrige pas : le nouveau
corps ne doit pas amplifier ce problème, et la recherche/les annotations le signalent. Les citations
et contrôles de structure ne garantissent pas l’exactitude sémantique de chaque phrase.

**Coût de la vérification :** la recherche web et la création d’annotations utilisaient toutes deux
le gros modèle de rédaction. Elles utilisent désormais l’entrée dédiée `fact_check` du catalogue,
configurée sur `google/gemini-3.8-flash`, raisonnement faible et sortie plafonnée à 4 096 tokens.
Ce choix est indépendant du modèle d’écriture et s’applique également au contrôle automatique après
génération. Une erreur ne déclenche pas de repli vers le gros modèle. Les frais éventuels de recherche
web subsistent. Les essais de génération consignés ci-dessous précèdent ce changement de modèle ;
le nouveau routage est vérifié par des tests simulant les requêtes du fournisseur, sans nouvel appel payant.
L’accès direct, le partage de la recherche, la reprise après échec et ce routage sont couverts par
la suite mise à jour : 300 tests backend ciblés et 30 scénarios navigateur, avec compilation et lint réussis.

## Vérification et artefacts

### Correction de la narration après le retour sur Dragon Hatching

Le projet confirmé par le créateur est `00ab034f4bec`, en présentation originale. Les comparaisons
utilisent son accroche exacte, sa fiche et son modèle configuré, sans remplacer son script enregistré.
Une référence reprend les consignes antérieures à la révision de densité, avec la même consigne
de futur proche, pour isoler autant que possible le changement de construction.

Les premiers essais de correction restaient trop linéaires et ajoutaient parfois un commentaire
après chaque péripétie. Leurs résultats ne sont pas présentés comme une réussite. Les dernières
consignes ont été simplifiées autour de la sélection d'un fil conducteur avant les faits ; un
exemple entièrement inventé illustre comment la même règle peut devenir un problème personnel.
La recherche et l'extraction demandent aussi les motivations et le contexte utiles, sans imposer
un déroulé par épisode. Aucun extrait de theomcx n'est injecté dans les prompts de production.

| Dernier essai relu | Mode | Mots, textes fixes compris | Durée estimée |
| --- | --- | ---: | ---: |
| Dragon Hatching | Présentation originale, fiche existante | 232 | 70,3 s |
| Love Unseen Beneath the Clear Night Sky | Présentation originale, fiche existante | 236 | 71,4 s |
| I Became a Legend After My 10 Year-Long Last Stand | Histoire sélectionnée | 197 | 59,8 s |

Dragon Hatching rétablit l'identité d'humain réincarné et présente le désir de se rapprocher des
humains avant les rencontres qui vont compliquer ce désir. Son choix d'évolution retrouve ainsi
une raison personnelle. La romance développe le contraste et la détermination qui rapprochent
les personnages, en écartant plusieurs gestes intermédiaires des essais précédents. Le cas
classique conserve son histoire sélectionnée et son point d'arrivée. La chronologie reste
présente là où elle sert le récit : le correctif ne demande pas de la brouiller.

Un essai distinct a réexécuté l'extraction de la fiche Dragon Hatching depuis la recherche déjà
payée : l'identité et la prémisse figurent bien parmi les faits cités. Les derniers essais de
rédaction réutilisent volontairement les anciennes fiches pour vérifier le chemin des projets
existants. Aucune nouvelle recherche web ni vérification factuelle complète n'a été lancée dans
cette comparaison. Les réserves de sources restent applicables, notamment les événements de
Dragon Hatching provenant d'un récapitulatif secondaire et l'accroche conservée à l'identique.

**155 tests ciblés passent** après la dernière modification. Ils contrôlent les contrats de
génération et la conservation des textes, pas la qualité littéraire. La revue de ces quelques
sorties est qualitative ; elle ne garantit pas une amélioration systématique ni une durée réelle.
La cible est indicative et le cas classique reste plus court. Aucun modèle, nombre de passes de
production ou routage de vérification des faits n'a changé pour cette correction narrative.

Les comparaisons, entrées, prompts exacts, réponses, scripts d'évaluation et `review.json` sont
conservés dans `backend/data/research/facecam-narrative-rebalance-2026-10-29/`, ignoré par Git.
Les essais finaux originaux sont `dragon-final-narrative/` et `romance-final-narrative/` ; le cas
classique est `classic-rebalanced/`. `dragon-full-pipeline/` conserve l'essai d'extraction révisée
avec une version intermédiaire des consignes de rédaction. Les brouillons des projets restent intacts.

### Essais de la révision sur la densité, avant correction narrative

Ces essais décrivent la révision antérieure au retour sur Dragon Hatching. Ils montrent un
élargissement des informations et des limites de durée ; ils ne démontrent pas à eux seuls
une amélioration de la narration. Les consignes de mise en place et d'ajout de faits évaluées
ci-dessous ont depuis été corrigées comme décrit plus haut.

Deux nouveaux essais complets utilisent des copies isolées des projets Pharmacy et Love Unseen,
avec leur modèle de rédaction configuré, `google/gemini-4-argon`. Les ajustements suivants
réutilisent la recherche payée et, selon l'étape testée, la fiche ou le premier brouillon.
Tous les essais sont conservés sous `evaluation-density/`, avec leurs prompts et réponses ;
les scripts des projets utilisés par le créateur ne sont pas remplacés.

La réserve recherchée contient davantage d'événements : Pharmacy dépasse la seule découverte
des pouvoirs pour raconter les premiers soins et l'ouverture de l'officine, une étape décrite
dans le [synopsis officiel de l'épisode 4](https://isekai-yakkyoku.jp/story/04.html). Love Unseen
avance vers le refus du club, les démarches pour acheter des feux d'artifice et leur annulation
au festival, décrits dans les [synopsis officiels des épisodes 3 à 6](https://sh-anime.shochiku.co.jp/kakekoi/story/).

| Essai revu | Ancienne version | Nouvelle version | Durée estimée |
| --- | ---: | ---: | ---: |
| Pharmacy, reprise de la passe éditoriale | 231 mots | 246 mots | 74,4 s |
| Love Unseen, dernière rédaction et réécriture | 225 mots | 260 mots | 78,6 s |

Les textes fixes sont conservés. Le second essai dépasse encore légèrement la cible : les
consignes de sélection et de longueur restent indicatives, sans suppression automatique de mots
ni boucle payante de régénération. Les essais intermédiaires montrent aussi que réclamer un
nombre de faits favorise parfois des anecdotes faibles ; les consignes finales n'imposent donc
aucun quota. La progression est enrichie, mais les formulations et raccourcis factuels demandent
toujours une relecture. Cette revue éditoriale et ces contrôles ponctuels de sources ne constituent
pas une nouvelle vérification factuelle exhaustive.

**155 tests ciblés** passent sur les parcours de génération Facecam, présentation originale,
histoires sélectionnées et analyse d'histoires. Ils vérifient les contrats et la conservation
des textes, pas la qualité littéraire. Aucun changement d'API, de modèle ou de nombre de passes
n'est nécessaire pour cette révision.

### Vérification initiale de la fonctionnalité

Les tests couvrent l’absence de l’ancien récit dans tous les appels de rédaction, la conservation
exacte des textes retouchés, le point de vue facultatif, les erreurs à chaque étape, les citations,
les conflits de révision, le passage jusqu’à l’approbation et le retour au mode histoire. Le navigateur
vérifie l’autosauvegarde avant le clic, le rechargement pendant le travail, les boutons indisponibles
pendant la génération, l’échec et la persistance du nouveau mode.

Les essais réels sont effectués sur des copies isolées de trois projets : **Parallel World Pharmacy**,
**Love Unseen Beneath the Clear Night Sky** et **Ishura**. Ils ne modifient pas les brouillons utilisés
par le créateur. Les premiers essais et la reprise après ajustement des consignes sont conservés
séparément ; les annotations factuelles restent consultables, y compris lorsqu’elles signalent
une erreur dans le nouveau corps ou une incertitude dans un hook inchangé.

| Projet isolé | Mots du script existant | Mots du nouvel essai | Durée estimée |
| --- | ---: | ---: | ---: |
| Parallel World Pharmacy | 276 | 231 | 70,0 s |
| Love Unseen Beneath the Clear Night Sky | 302 | 225 | 68,2 s |
| Ishura, essai recentré sur l’angle | 217 | 217 | 65,8 s |

Tous les comptages incluent les textes fixes ; ce sont des estimations, pas des enregistrements.
Le hook et l’avis éventuel sont conservés. Le cas Ishura a révélé un risque particulier : présenter
une œuvre chorale pouvait faire abandonner le personnage du hook au profit d’un autre duo. Les
consignes demandent désormais de garder ce personnage comme fil conducteur. Le dernier essai
réutilise la recherche web déjà obtenue et refait la fiche, la rédaction et la réécriture ; il est
conservé dans `evaluation-angle/`, sans nouvelle vérification factuelle complète.

Les vérifications réelles ne sont pas toutes sans signalement : l’essai romance comporte une
incertitude et une chronologie signalée comme contradictoire. Les contrôles d’Ishura signalent
notamment des affirmations insuffisamment étayées dans le hook inchangé. Le premier essai Pharmacy
a été vérifié sans signalement, mais la vérification de sa version ajustée a échoué avec une **erreur
402 du fournisseur**, y compris lors de la reprise séquentielle. Aucun résultat de vérification
antérieur n’est présenté comme validation de cette nouvelle version.

Validation du code : **273 tests backend Facecam**, **29 tests navigateur Facecam**, compilation
TypeScript/Vite et lint des fichiers frontend modifiés réussis. Après le dernier ajustement de
synchronisation des choix, les cinq tests navigateur concernés ont également été rejoués avec succès.
Le bouton a été inspecté dans l’interface locale et la route est chargée par le backend en cours.

Le matériel local est sous `backend/data/research/facecam-original-2026-10-29/`, ignoré par Git :

- `theomcx/manifest.json` et `discovery.json` : sélection et candidats de la collecte ;
- `theomcx/videos/<id>/` : vidéo, métadonnées, transcription horodatée et annotation automatique ;
- `summary.json` : compteurs automatiques ; `reviewed-summary.json` et `reviewed-inventory.json` : revue éditoriale ;
- `evaluation/`, `evaluation-refined/` et `evaluation-angle/` : entrées isolées, réponses et résultats des essais.

Reprendre la collecte figée et ses contrôles :

```bash
.pixi/envs/default/bin/python scripts/facecam_corpus.py \
  --creators theomcx --count 50 \
  --output backend/data/research/facecam-original-2026-10-29 --device cuda
.pixi/envs/default/bin/python scripts/analyze_facecam_corpus.py \
  --output backend/data/research/facecam-original-2026-10-29
.pixi/envs/default/bin/python scripts/summarize_facecam_corpus.py \
  --output backend/data/research/facecam-original-2026-10-29
```

Ces commandes reprennent les annotations automatiques ; les observations et corrections de
classification de l’inventaire publié sont une revue distincte. Pour actualiser les dernières
publications à une autre date, utiliser un nouveau dossier plutôt que remplacer ce manifeste.
