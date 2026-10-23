# Facecam : des mots plus directs, des phrases plus denses

Analyse du 23 octobre 2026, centrée sur les quinze dernières publications accessibles de
[@theomcx](https://www.tiktok.com/@theomcx). Préférence retenue : français très oral,
un peu d'argot et un ton plus cash quand le récit s'y prête.

Le changement porte sur le choix des mots, les accroches et la coupe des paragraphes. Le générateur
dispose désormais d'exemples concrets de reformulation et d'une passe de réécriture distincte après
le premier brouillon. Sur les trois cas comparés, le corps final compte **17 à 37 % de mots en moins**.

## Corpus et limites de lecture

Collecte fraîche du profil : trente publications candidates, dédupliquées puis triées par date de
publication décroissante. Les quinze plus récentes couvrent le **6 juillet au 19 octobre 2026**.
L'ordre d'affichage des épingles ne détermine pas la sélection. **15/15 vidéos téléchargées et
transcrites**, sans remplacement par une publication plus ancienne. Huit premiers fichiers n'avaient
pas de piste audio malgré leurs métadonnées ; le format `download` de la même publication a permis
de récupérer une version avec audio et vidéo, vérifiée avec ffprobe.

Les quinze textes ont été lus intégralement. Une première transcription française `large-v3` utilise
le contrôle de couverture du projet. Une deuxième passe séquentielle avec le même modèle permet de
comparer les passages instables. Les deux versions sont conservées. Les quinze courts extraits du
fichier d'analyse ont été retrouvés dans les deux transcriptions, après normalisation des apostrophes
et espaces. Cela ne constitue pas une vérification audio humaine : les noms propres et certains
passages restent mal reconnus. La ponctuation ASR ne permet pas de mesurer précisément ses phrases
orales ou ses respirations. Les horodatages sont des estimations ASR.

Classification éditoriale : **10 présentations narratives**, **3 promotions** et **2 annonces de
compte**. Les promotions peuvent contenir un récit, mais leur catalogue et leur appel commercial
ne deviennent pas des consignes de narration. Parmi les dix présentations, la durée médiane est
de **66,8 secondes**, pour **250,5 mots ASR** après retrait de l'intro de marque reconnue en ouverture.
Les durées vont de 40,8 à 87,1 secondes et les textes de 153 à 338 mots. Ces nombres incluent le
reste de l'ouverture et l'annonce du titre. Ils décrivent ce corpus, sans prouver une rétention.

## Lecture des quinze publications

Les intitulés ci-dessous servent à repérer les sujets ; les transcriptions ne sont pas des sources
de référence sur l'orthographe des noms ou les faits des œuvres.

| Date | Publication | Format | Procédé à retenir |
| --- | --- | --- | --- |
| 19/10 | [Dreamland](https://www.tiktok.com/@theomcx/video/7698402215358237984) | Présentation | Une règle de pouvoir devient immédiatement personnelle : la peur des flammes vient de la perte de sa mère. Le contraste jour/nuit donne ensuite le rythme. |
| 12/10 | [The Vermilion Mask](https://www.tiktok.com/@theomcx/video/7695797702210243873) | Présentation | La conséquence du masque porte le choc ; elle donne une mission de réparation au héros, puis son pouvoir redevient un danger. |
| 08/10 | [Coloris](https://www.tiktok.com/@theomcx/video/7694313302687583521) | Présentation | « des moins que rien » rend la hiérarchie sociale immédiatement lisible. La menace sur un proche transforme le système de couleurs en conflit personnel. |
| 06/10 | [Psyren](https://www.tiktok.com/@theomcx/video/7693578136868490529) | Présentation | Une carte, un appel et une camarade disparue font progresser le mystère. Chaque élément change la question que l'on se pose. |
| 30/09 | [Nouveau compte jeux](https://www.tiktok.com/@theomcx/video/7691348513086623009) | Annonce | « on va pas se mentir » produit une courte connivence. Utile pour entendre le registre, sans en faire une transition obligatoire. |
| 27/09 | [Overgeared](https://www.tiktok.com/@theomcx/video/7690246072660135200) | Présentation | « sa nouvelle classe est cheatée » reçoit aussitôt une démonstration : niveau 1, équipement de niveau 60. Un contraste chiffré porte l'adjectif. |
| 26/09 | [Rematch × Blue Lock](https://www.tiktok.com/@theomcx/video/7689842673787637025) | Promotion | Une explication ample est ponctuée par « Rien que ça. ». La variation de longueur donne de l'oralité ; le catalogue commercial final sert un autre objectif. |
| 25/09 | [Changement d'intro](https://www.tiktok.com/@theomcx/video/7689488150833548576) | Annonce | Des phrases très brèves donnent du poids à une fausse annonce de fin. Le registre est intéressant, mais cette mécanique trompeuse n'est pas transférée aux faits d'une histoire. |
| 21/09 | [Bleach / Disney+](https://www.tiktok.com/@theomcx/video/7687991900934032672) | Promotion | Attaque et menace sont exprimées directement. Les superlatifs et les affirmations sur la diffusion appartiennent à cette promotion. |
| 03/09 | [L'école qui interdit les cheveux longs](https://www.tiktok.com/@theomcx/video/7681323019737763104) | Présentation | Trois centimètres suffisent à rendre la règle concrète ; « une exécution sociale » en donne le coût. Le même détail porte ensuite la révolte. |
| 01/09 | [False Memory](https://www.tiktok.com/@theomcx/video/7680568914891246881) | Présentation | « la fin du monde aura lieu demain à 19h » remplace une menace vague par une échéance. Le pouvoir nécessaire devient le point de départ d'une nouvelle contrainte. |
| 10/07 | [NTE / Shinku](https://www.tiktok.com/@theomcx/video/7660927358693608737) | Promotion | Une perte passée explique une réaction présente. Le récit fonctionne autour d'un lien affectif, avant les informations commerciales. |
| 09/07 | [Tomb Raider King](https://www.tiktok.com/@theomcx/video/7660548569056742689) | Présentation | « le sous-fifre » résume un rapport de domination. Humiliation, trahison et revanche donnent une progression par changements de statut. |
| 07/07 | [Black Torch](https://www.tiktok.com/@theomcx/video/7659806417624239392) | Présentation | « se faire trouer le ventre » donne une image immédiate. Le sauvetage puis la résurrection aboutissent à une nouvelle menace. Le mot violent correspond ici à un fait violent. |
| 06/07 | [The Exiled Heavy Knight](https://www.tiktok.com/@theomcx/video/7659442304847007008) | Présentation | « déshérité et banni » exprime directement la perte. La classe méprisée se révèle utile grâce à une connaissance que le héros possède déjà. |

## Ce qui rend cette voix efficace à transposer

**Les mots exposent un rapport de force.** Perdre sa famille, être humilié, servir de sous-fifre,
être recherché : ces formulations disent ce que le personnage subit. Notre ancienne rédaction
privilégiait plus facilement des formulations comme « prendre une décision radicale », « constater
une anomalie » ou « se retrouver dans une situation ». Le nouveau prompt demande de nommer l'action
et son coût. Un verbe fort doit rester exact : une blessure ne devient pas un massacre, un soupçon
ne devient pas une preuve, un risque d'expulsion ne devient pas une expulsion certaine.

**Le langage familier fonctionne parce qu'il appartient à la situation.** Le vocabulaire de jeu
est naturel quand le récit parle de niveaux et d'équipement. Une expression physique fonctionne
quand l'événement est physique. Ajouter « dinguerie » à un paragraphe scolaire laisse intacte sa
lourdeur. Le transfert retenu autorise l'argot courant, les négations orales et les verbes familiers,
sans quota ni grossièreté systématique.

**La concision vient de l'information retenue.** Une règle, une exception et sa conséquence peuvent
faire comprendre un système. Le contraste de niveaux dans Overgeared évite une liste de statistiques.
Le pouvoir de Vermilion Mask est compris par ce qu'il fait au personnage. Pour notre générateur,
le détail utile reste ; les déplacements, vérifications de lecture et commentaires émotionnels
peuvent être absorbés dans l'action qui fait avancer l'histoire.

**Le débit du récit alterne.** Ce corpus contient beaucoup de phrases liées au futur proche et des
explications assez longues. Il ne justifie pas une règle « huit mots maximum » ou un texte composé
uniquement de fragments. La consigne retenue alterne une liaison fluide et une conséquence brève.
Les connecteurs servent les changements d'enjeu ; répéter une annonce de bascule ne remplace pas
l'événement qui bascule.

**L'accroche n'est pas toujours narrative chez lui.** Plusieurs ouvertures utilisent une recommandation
très forte, un superlatif ou la peur de manquer une sortie. Nous conservons leur franchise et leur
vitesse, mais une fiche de scène ne suffit pas à déclarer une œuvre meilleure de l'année. Nos
accroches partent donc d'un conflit ou d'une conséquence vérifiable, généralement en 10 à 22 mots.
La question doit naître chez le spectateur ; l'accroche n'a pas besoin d'être interrogative.

## Traduction dans le générateur

- `narration.md` fixe le registre, les verbes, les nuances à conserver et des exemples originaux de
  coupe à l'échelle de la phrase et du paragraphe.
- `hooks.md` privilégie une information immédiate, des angles distincts et une accroche d'un souffle.
- `opinions.md` demande une seule idée concrète, sans vocabulaire de critique ni suite inventée.
- `script.md` traite la fiche comme un contrôle des faits. Le minimum du budget ne doit plus
  inciter à remplir ; le premier brouillon fait déjà l'objet d'une relecture silencieuse.
- `oral_edit.md` réalise une **deuxième génération distincte**, centrée sur le resserrage. Elle
  reçoit les faits, leurs incertitudes, le périmètre choisi, le premier brouillon et les textes fixes.
  Elle doit également retirer les détails que le premier brouillon aurait inventés.

La deuxième passe ajoute un appel au grand modèle choisi dans le projet, donc du temps et un coût
de génération. Elle est exécutée une seule fois, quelle que soit la durée ; elle ne lance pas de
boucle pour atteindre un quota. Le mode « garder toute l'histoire » conserve toutes ses branches
et tous ses événements, tout en permettant de resserrer les formulations.

Les deux réponses sont archivées. Les identifiants, l'ordre des blocs et les textes non vides sont
vérifiés avant toute sauvegarde du script final. Les extraits originaux et les textes choisis pour
l'accroche et l'avis ne sont pas remplacés par le modèle. Une erreur de réécriture ou une correction
manuelle concurrente préserve le script déjà enregistré. Les validations structurelles ne remplacent
pas une lecture éditoriale ni la vérification factuelle existante.

## Comparaison avant/après

Trois fiches réelles ont été figées avec leurs histoires choisies, leurs modèles et les mêmes
accroches/opinions. Les projets existants n'ont pas été réécrits. La référence « avant » est une
nouvelle génération avec les anciens prompts, et non un texte que l'utilisateur aurait déjà retouché.
Le résultat « après » combine les nouveaux prompts et la passe de réécriture. Les essais de
développement sont conservés séparément ; ce petit échantillon n'est pas un benchmark statistique.

| Histoire | Modèle du projet | Mots du corps avant | Mots du corps après | Réduction | Médiane des 8 hooks avant → après |
| --- | --- | ---: | ---: | ---: | ---: |
| Sofía et le dragon | GPT 6.1 Sol | 184 | 123 | 33,2 % | 23 → 18 |
| Pharma | Gemini 4 Argon | 269 | 224 | 16,7 % | 46 → 20 |
| Le héros revenu de guerre | Gemini 4 Argon | 194 | 122 | 37,1 % | 37 → 22,5 |

Comptage par `str.split()`. Les nombres du corps excluent l'accroche, l'avis et la phrase fixe de
révélation du titre. Les hooks ont été évalués séparément ; les mêmes hooks fixes ont servi à
comparer les corps. La longueur ne mesure à elle seule ni la qualité, ni la durée réelle de parole.
Le cas Pharma reste au-dessus de son budget indicatif : la passe améliore la densité sans garantir
automatiquement une durée précise.

Exemple de différence observable dans les générations de Pharma :

> Avant : « Pharma file à la bibliothèque pour vérifier s'il arrive à lire les ouvrages locaux.
> Sur place, il constate que la médecine de ce monde correspond à celle de la Terre, mais avec
> plusieurs siècles de retard. »

> Après : « Pharma consulte les livres de la bibliothèque : leur médecine correspond à celle de
> la Terre, mais avec des siècles de retard. »

Le nouveau texte remplace aussi le risque « d'être chassé de sa famille » par une formulation plus
orale : « sa famille risque de le virer ». Le degré de certitude reste identique.

Validation automatisée : **170 tests ciblés passent**, couvrant la génération, les histoires,
la répartition des mots, le corpus, les erreurs de la deuxième passe et les modifications concurrentes.
Deux essais réels supplémentaires, avec GPT et Gemini, utilisent une histoire synthétique contenant
un extrait original entre deux blocs, une branche secondaire et une issue incertaine. Les deux
versions finales gardent la branche secondaire, la tentative ratée et le risque conditionnel, sans
déplacer l'achat de la clé avant l'extrait qui l'introduit. Les résultats sont conservés dans
`evaluation/full-story-raw-<preset>.json`.

## Artefacts locaux

Tout le matériel de recherche est sous `backend/data/research/facecam-2026-10-23/`, ignoré par Git :

- `theomcx/manifest.json` et `discovery.json` : sélection figée, URLs, dates et métadonnées ;
- `theomcx/videos/<id>/video.mp4` : les quinze vidéos avec audio ;
- `transcript.json`, `transcript.txt`, `transcript-sequential.json` : les deux lectures ASR et leur provenance ;
- `style-analysis.json` : observation individuelle, court extrait vérifié, catégorie et empreinte vidéo ;
- `style-summary.json` : compteurs et statistiques du corpus ;
- `evaluation/` : entrées figées, anciens/nouveaux prompts, réponses brutes, essais intermédiaires
  et `comparison.json` ; les fichiers `*-final-script.json` contiennent les corps issus de la chaîne finale.

La collecte standard peut être reprise avec :

```bash
.pixi/envs/default/bin/python scripts/facecam_corpus.py \
  --creators theomcx --count 15 \
  --output backend/data/research/facecam-2026-10-23 --device cuda
```

Cette commande reprend le manifeste et les transcriptions primaires ; la seconde passe ASR et les
comparaisons de génération de cette étude sont des artefacts séparés. Les scripts des créateurs
restent dans la recherche locale et ne sont pas injectés dans les prompts de production.
