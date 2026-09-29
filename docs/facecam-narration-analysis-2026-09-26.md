# Refonte de la narration facecam — corpus du 26 septembre 2026

## Décision éditoriale

Le changement essentiel est de passer d'une réécriture du texte source à une présentation construite
depuis ses faits. Le moteur narratif doit être identifiable : un désir contrarié, une règle avec une
exception, une compétence qui ne résout pas le vrai problème, ou une réussite qui entraîne une complication.
L'accroche pose une situation propre à l'œuvre ; le corps lui apporte une réponse ; l'avis explique
l'intérêt de ce qui vient d'être montré.

On reprend des procédés observables, pas les scripts ni les signatures des créateurs. L'intro quotidienne
de theomcx est exclue. Ses autres expressions récurrentes ne deviennent pas notre nouveau slogan.
Les huit accroches proposées par l'application sont désormais indépendantes de l'accroche source.

## Corpus et méthode

Sources : [theomcx](https://www.tiktok.com/@theomcx) et [adhnime](https://www.tiktok.com/@adhnime).
Pour chaque compte, 90 publications candidates ont été récupérées, dédupliquées, classées par date de
publication décroissante, puis limitées aux 75 plus récentes accessibles. Une vidéo épinglée ancienne
ne remonte donc pas artificiellement dans l'échantillon. La sélection est figée dans un manifeste :
une erreur de téléchargement ne provoque pas son remplacement silencieux par une vidéo plus ancienne.

La période couverte est du **24 mars 2025 au 25 septembre 2026** pour theomcx et du **10 mai au
24 septembre 2026** pour adhnime. Les deux échantillons ont la même taille, mais pas la même durée
calendaire. Il ne faut pas confondre différence de créateur, évolution de format et choix des œuvres.

**Collecte terminée : 150/150 vidéos téléchargées, transcrites et annotées, sans échantillon remplacé.**
Cela représente environ **2 h 36 de vidéo** et **558 Mo** d'artefacts locaux, évaluations comprises.

| Mesure | theomcx | adhnime |
| --- | ---: | ---: |
| Vidéos / transcriptions / annotations | 75 / 75 / 75 | 75 / 75 / 75 |
| Présentations individuelles, classification dominante | 48 | 67 |
| Promotions | 16 | 1 |
| Listes | 3 | 6 |
| Annonces de compte | 3 | 1 |
| Critiques / autres formats | 2 / 3 | 0 / 0 |
| Durée médiane des présentations | 62,53 s | 63,65 s |
| Mots ASR médians des présentations, intro de marque retirée | 234 | 251 |

Les catégories décrivent la fonction dominante de la vidéo ; une présentation peut également avoir
une dimension promotionnelle. Les comparaisons de structure utilisent principalement les 115 présentations
individuelles. Une promotion contenant un vrai passage narratif peut apporter un exemple qualitatif,
sans entrer dans leurs statistiques.

Les vidéos complètes sont conservées localement. La transcription française utilise **large-v3**,
via faster-whisper et le contrôle de couverture vocale déjà présent dans le projet. Chaque vidéo
dispose de métadonnées, d'un SHA-256, de segments horodatés, du texte ASR intact et d'une version
d'analyse dont l'intro de marque est retirée lorsqu'elle est reconnue en ouverture. L'annotation exclut
également cette intro lorsqu'elle est citée ailleurs ou transcrite différemment.

Chaque transcription est analysée intégralement par le modèle configuré dans l'application : format,
type d'accroche, progression, point de vue, procédés susceptibles de soutenir l'attention, fin, place
du titre, limites et transfert possible. Deux courts extraits par vidéo sont vérifiés automatiquement
contre la transcription. Une lecture éditoriale des annotations et de transcriptions représentatives,
dont des œuvres traitées par les deux comptes, complète cette passe. Ce n'est pas une correction
humaine mot à mot de 150 transcriptions.

Les annonces de compte, listes et promotions restent dans les 75 publications de chaque compte.
Elles sont identifiées séparément pour ne pas prendre un lancement de jeu, une redirection YouTube
ou un changement d'intro pour le modèle d'une présentation individuelle. Les étiquettes sont des
annotations de modèle, donc une classification éditoriale révisable.

Les artefacts se trouvent dans `backend/data/research/facecam-2026-09-26/` :

- `<créateur>/manifest.json` : les 75 IDs, URLs, dates et statistiques publiques au moment de la collecte ;
- `<créateur>/videos/<id>/video.mp4` : vidéo complète ;
- `transcript.json` et `transcript.txt` : transcription, provenance et diagnostics ;
- `analysis.json` : analyse individuelle et extraits justificatifs ;
- `analysis-index.csv` : tableau comparatif des 150 publications ;
- `summary.json` : compteurs et statistiques descriptives ;
- `evaluation/` : comparaisons de génération sur trois projets existants et contrôles de répartition.

Ces données sont ignorées par Git. Seuls les outils, l'analyse synthétique et les règles de génération
font partie du changement de code. Les scripts des créateurs ne sont pas injectés dans la production.

## Ce que fait adhnime

Sur ses **67 présentations individuelles**, **37** accroches sont classées comme situations concrètes,
**25** comme paradoxes, **2** comme jugements, **2** comme promesses et **1** comme autre : **62/67**
partent donc d'une situation ou d'une contradiction identifiable. Les huit autres publications sont
six listes, une promotion et une annonce de compte.

### Une entrée déjà dramatique

L'accroche donne une anomalie à comprendre, pas une fiche de genre. Un bébé risque d'être surpris
en train de faire des abdos ; des professeurs doivent enseigner dans une prison de criminels ;
une invitation de sa patronne surprend un salarié persuadé de ne pas lui plaire. Le spectateur peut
formuler une question précise avant de connaître le nom du héros ou celui de l'œuvre.
Exemples : [le bébé](https://www.tiktok.com/@adhnime/video/7687328242130226465),
[la prison](https://www.tiktok.com/@adhnime/video/7684600830879616289),
[l'invitation](https://www.tiktok.com/@adhnime/video/7689152595507580193).

Plusieurs vidéos annoncent un point d'arrivée, reviennent à sa cause, puis rejoignent ce point.
L'invitation n'est pas laissée en promesse vague : le récit explique l'incident de voisinage et les
réactions qui y conduisent. Ce procédé est transférable au hook, même si l'ordre des plans du corps
reste fixé. Il faut payer cette promesse dans le récit, et ne pas seulement différer le titre.

### Une mécanique simple rendue visible

Le monde peut être complexe, mais une seule règle organise souvent le passage. Un homme perçoit
les femmes comme des pierres sauf une ; un personnage ne peut obtenir que quelques compétences,
mais peut les améliorer indéfiniment ; un pouvoir permet de copier les émotions sans en ressentir.
Une situation concrète démontre ensuite ce fonctionnement. Cela donne une progression compréhensible
sans exposer tout le système de pouvoirs.
Exemples : [perception et exception](https://www.tiktok.com/@adhnime/video/7688423079311052065),
[compétences limitées](https://www.tiktok.com/@adhnime/video/7686938002748165409),
[émotions copiées](https://www.tiktok.com/@adhnime/video/7671689771218898208).

### Des changements d'enjeu, pas seulement des événements

Les buts sont souvent très simples : protéger un proche, sortir d'un piège, être reconnu, séduire,
rester discret. Le récit devient intéressant quand un fait modifie la possibilité d'atteindre ce but.
Une vengeance paraît impossible lorsque les cibles sont déjà mortes ; une victoire au football crée
de nouvelles attentes ; une compétence extraordinaire empêche précisément de rester anonyme.
Exemples : [vengeance déplacée vers l'enfer](https://www.tiktok.com/@adhnime/video/7688721010731928865),
[but et attentes nouvelles](https://www.tiktok.com/@adhnime/video/7681704429904596246),
[puissance et discrétion incompatibles](https://www.tiktok.com/@adhnime/video/7674260817683942688).

Les mots « mais » et « alors » apparaissent chacun dans 66 des 67 transcriptions de présentations.
Ce comptage décrit une habitude de liaison ; multiplier ces mots dans nos scripts n'en reproduirait
pas la fonction. La règle utile est : **une relance doit modifier une information, une attente ou un
rapport de force**. Une suite de « sauf que » sans changement réel produit seulement un tic.

### Une sortie vers l'intérêt de lecture

La fin bascule souvent du personnage vers ce que l'œuvre propose : progression satisfaisante,
humour, tension psychologique, dynamique sentimentale. Le point de vue est lié à la scène racontée.
Il n'est cependant pas présent avec la même ampleur dans tous les épisodes : plusieurs vidéos
s'arrêtent sur le conflit et une indication de genre ou de titre.

Le titre est identifié en fin de transcription dans 43 des 67 présentations ; il est absent du texte
ASR dans 22 cas, au milieu dans un cas et incertain dans un autre. **Absent du texte ne signifie pas
absent de l'image.** Ces données ne justifient donc pas une règle absolue sur l'affichage du titre.
Notre carte finale est un choix conservé du produit.

## Ce que fait theomcx, intro de marque exclue

Dans ses **48 présentations individuelles**, l'ouverture après retrait de la marque est classée comme
situation concrète dans **19** cas, jugement dans **17**, paradoxe dans **7**, promesse dans **4** et
question dans **1**. Cette distribution confirme l'intérêt de privilégier l'entrée plus concrète
d'adhnime tout en reprenant la lisibilité des systèmes et des bascules chez theomcx.

La formule de marque a été retirée automatiquement de **56** transcriptions du corpus complet,
avec prise en charge de variantes ASR et de ponctuation. Une version mal transcrite dans une annonce
de compte est explicitement ignorée par l'analyse ; les autres ouvertures de type « je présente un
manga » ne sont pas assimilées arbitrairement à cette signature quotidienne.

Le titre apparaît en fin de texte dans **47/48** présentations, et n'est pas identifié dans l'autre.
Le marqueur de bascule récurrent de theomcx apparaît dans **30/48** textes. On en retient la fonction
de changement d'enjeu, sans ajouter cette expression aux règles de notre voix.

### Rendre le système du récit immédiatement lisible

Après l'intro écartée, de nombreuses vidéos établissent la règle du monde, la place du protagoniste,
puis l'exception qui le met en mouvement. Une magie tenue pour réservée à certains devient accessible
à une enfant ; une classe réputée inutile cache une stratégie puissante ; une vente à un démon se
transforme en adoption, puis en scolarité dangereuse. Le contraste est compréhensible avant de connaître
toute la mythologie.
Exemples : [la magie et son coût](https://www.tiktok.com/@theomcx/video/7626417459486149910),
[la classe méprisée](https://www.tiktok.com/@theomcx/video/7659442304847007008),
[l'adoption démoniaque](https://www.tiktok.com/@theomcx/video/7581885479893683478).

### Une bascule qui change le statut du héros

Le cœur du récit est souvent une transformation de position : protégé puis menacé, faible puis
dangereux, vainqueur puis accusé, expert dans un domaine et incompétent dans un autre. Les conséquences
rendent cette bascule intéressante. Sauver un chat ne donne pas seulement un pouvoir : cela fait du
héros une cible. Avoir l'expérience d'un assassin ne prépare pas à vivre au collège.
Exemples : [Black Torch](https://www.tiktok.com/@theomcx/video/7659806417624239392),
[Kill Blue](https://www.tiktok.com/@theomcx/video/7627886716187151638),
[victoire devenue accusation](https://www.tiktok.com/@theomcx/video/7649415965603548449).

### Une projection orale, parfois à la deuxième personne

Le passage par « tu » peut faire vivre une règle plutôt que l'expliquer abstraitement. Dans la vidéo
du jeu amoureux, le destinataire comprend d'abord les règles, puis ressent leur contradiction quand
les sentiments changent. Ce procédé est utile quand il simplifie une situation ; en faire une obligation
pour chaque script donnerait une nouvelle intro automatique.
[Exemple du jeu amoureux](https://www.tiktok.com/@theomcx/video/7629005813876821270).

### Des éléments à ne pas transférer automatiquement

Outre l'intro quotidienne explicitement exclue, plusieurs ouvertures reposent sur des superlatifs,
la peur de manquer une sortie, la disponibilité en VF ou une comparaison prestigieuse. Ce sont des
affirmations qui demandent une connaissance de l'œuvre, de sa diffusion ou une opinion réelle.
Un script source centré sur une scène ne nous donne pas nécessairement ces preuves.

La proximité orale peut être reprise sans les expressions signatures, les annonces de plateforme,
les affirmations de visionnage ou les détours commerciaux. La narration produite doit pouvoir être
assumée par notre créateur, pas lui inventer une expérience de spectateur.

## Deux comparaisons sur des œuvres communes

**Black Torch** permet de séparer le choix d'accroche de la matière racontée. Chez adhnime, le pouvoir
obtenu par fusion est annoncé d'entrée, puis expliqué. Chez theomcx, l'ouverture recommande la série,
avant de présenter les aptitudes et la compassion de Jiro. Les deux parcours rejoignent le même ressort :
le pouvoir qui le sauve le transforme ensuite en menace pour les autres camps. Le transfert intéressant
est cette double conséquence, avec une accroche concrète adaptée à notre vidéo.
[Adhnime](https://www.tiktok.com/@adhnime/video/7655729685573143830),
[theomcx](https://www.tiktok.com/@theomcx/video/7659806417624239392).

**Le jeu amoureux** montre deux points d'entrée possibles dans une même contradiction. Adhnime commence
par une déclaration impossible à interpréter ; theomcx fait entrer le public dans les règles du jeu.
Tous deux donnent au spectateur l'information qui manque aux personnages : les sentiments sont
réciproques. L'attention repose alors sur la manière dont ils vont sortir du blocage, pas sur un mystère
artificiellement conservé. On peut donc révéler une information et maintenir l'intérêt en changeant la
question dramatique. Les variantes factuelles de leurs résumés ne sont pas fusionnées comme une source
de vérité sur l'œuvre.
[Adhnime](https://www.tiktok.com/@adhnime/video/7675788213612563744),
[theomcx](https://www.tiktok.com/@theomcx/video/7629005813876821270).

## Traduction en génération facecam

| Observation | Changement concret |
| --- | --- |
| L'intérêt tient à une situation, pas à la formulation du script source | Extraction d'une fiche neutre ; la narration source n'est plus fournie aux étapes de rédaction |
| Les hooks changent avec le sujet | Huit propositions nouvelles, plusieurs points d'entrée, aucune première option traduite automatiquement |
| Les faits servent une tension principale | Le rédacteur choisit un fil conducteur et quelques mouvements narratifs, éclairés par le point de vue sélectionné |
| Le contexte devient utile quand il explique une action | Réduction de l'exposition et regroupement des actions secondaires |
| Une réussite peut relancer le conflit | Mise en avant des conséquences et des exceptions réellement présentes dans les faits |
| L'accroche reçoit une réponse | Consigne explicite de payer la promesse ; pas de révélation inventée ni de faux cliffhanger obligatoire |
| Le commentaire porte sur ce qu'on vient de voir | Avis sur le mécanisme narratif, sans faux visionnage ni jugement esthétique non documenté |
| L'oralité vient aussi du rythme | Alternance de phrases brèves et de liaisons, détails concrets, apartés rares ; aucun argot ou tic imposé |

Le corps n'a plus pour objectif de conserver le nombre de mots original. Le volume vient des faits et
de leur progression. Cela n'impose pas non plus un format de 250 mots à une histoire qui n'en a besoin
que de 150. Chez adhnime, les présentations ont une médiane de 251 mots ASR pour 63,65 secondes de
vidéo : c'est une référence de densité observée, pas un débit à imposer à une prise humaine. Les mots
ASR peuvent inclure des extraits insérés et leurs limites ne sont pas un alignement mot à mot.

## Compromis techniques

### Ce qui est implémenté

`FacecamNarrationService` réalise l'extraction et valide une structure typée. Chaque plan non-raw
appartient exactement une fois à un fait ou groupe de faits de son bloc, dans l'ordre. Les frontières
des extraits originaux sont vérifiées avant d'enregistrer la fiche. Un cache dépend de la référence,
du squelette et du prompt d'extraction ; un fichier incomplet ou périmé est reconstruit.

Les appels d'accroches, d'avis et de rédaction partagent cette fiche. Cela ajoute **un appel au modèle
principal lors de la première opération**, pas une analyse du corpus à chaque génération. La fiche est
visible dans les fichiers du projet. Les générations existantes ne sont pas réécrites automatiquement.

Les prompts définissent les fonctions narratives, avec des contraintes de faits et de montage. Il n'y
a ni entraînement du modèle sur ces 150 vidéos, ni recherche d'un script à recopier dans le corpus.
Cette option est vérifiable, ajustable rapidement et peu coûteuse à l'usage. Une adaptation entraînée
demanderait de construire et d'évaluer des couples « faits d'entrée → présentation souhaitée » ; les
150 sorties seules ne constituent pas encore ce jeu d'entraînement.

### La limite du montage conservé

L'ordre des plans et des extraits audio reste celui du projet. Le hook peut annoncer un fait tardif,
mais les faits du corps doivent continuer à correspondre aux plans, et ne traversent pas les frontières
raw. La répartition finale conserve chaque mot approuvé. Le montage et l'alignement de la prise humaine
utilisent ensuite les mécanismes existants.

Cela permet de transformer le rythme, le point d'entrée, l'importance donnée aux faits et les transitions.
Cela limite une reconstruction qui supprimerait la moitié des scènes ou raconterait un autre épisode.
Pour obtenir toute la liberté d'une présentation créée de zéro, il faudrait écrire depuis un dossier
plus large sur l'œuvre, puis **sélectionner de nouveaux plans après l'écriture**, avec recherche de
séquences, choix des extraits, couverture des passages de commentaire et contrôle des spoilers.
Ce serait une évolution du pipeline de montage, pas uniquement des prompts.

Une narration plus courte peut aussi conduire à des plans plus courts ou à une autre répartition.
Les contrôles de plages de mots prouvent que les scènes sont couvertes ; ils ne prouvent pas la qualité
visuelle du montage obtenu. La validation finale demande une vraie prise facecam et une prévisualisation.

### Faits et style sont deux validations différentes

La fiche extrait les affirmations de la source ; elle ne les certifie pas. Une erreur de nom ou de
causalité déjà présente peut subsister. Le contrôle documenté existant continue de se lancer après
la génération. Les ambiguïtés explicites sont omises ou reformulées autour des faits établis, sans
faire dire au créateur « selon la fiche » ou « ce n'est pas précisé ».

## Essais et limites de la validation

Trois projets existants ont servi à une comparaison avec l'ancien prompt, en conservant la même
accroche et le même avis pour les deux corps comparés. Ces essais sont isolés des projets de production.

| Projet | Extraits raw | Corps ancien prompt | Corps nouveau prompt |
| --- | ---: | ---: | ---: |
| Kill Blue | 0 | 369 mots | 218 mots |
| The Daily Life of the Immortal King | 1 | 293 mots | 210 mots |
| Mirai no Mirai | 4 | 276 mots | 186 mots |

Le gain n'est pas « moins de mots = meilleure rétention ». Dans ces essais, le nouveau texte condense
l'exposition, met davantage en évidence un conflit concret et évite certains bilans redondants.
L'essai Kill Blue organise notamment le récit autour du décalage entre compétence d'assassin et
maladresse à l'école. La comparaison a aussi révélé une première version trop prudente qui commentait
la provenance des faits ; les consignes d'extraction et de rédaction ont été corrigées puis retestées.

La répartition réelle par le modèle a également été validée sur les trois nouvelles générations :
indices couverts, plages contiguës non vides et mots conservés dans leur ordre. Les résultats sont
dans `evaluation/<projet>/assignment-check.json`. Ce contrôle ne remplace pas le visionnage d'un montage.

Les tests automatisés portent sur le partage de la fiche, la séparation des entrées source/rédaction,
les huit accroches sans option traduite imposée, l'invalidation du cache, le rejet des frontières raw
traversées, les réponses malformées et les révisions périmées. La suite facecam existante couvre les
protections de l'édition, de l'approbation, de l'enregistrement et du montage.

Validation finale : **46 tests passent** (`test_facecam.py` et `test_facecam_corpus.py`), scripts Python
compilables et absence d'erreur de whitespace dans le diff. Les trois comparaisons et leurs répartitions
utilisent le vrai modèle configuré, en complément des tests simulés. Il n'y a pas eu de publication ni
de nouveau montage réalisé à partir d'une prise humaine pendant ces essais.

Enfin, les transcriptions comportent des erreurs de noms et quelques mots tronqués aux frontières
des segments. Les extraits vérifiés prouvent que l'annotation s'appuie sur l'ASR, pas que l'ASR est
parfait. Les statistiques publiques ne donnent ni courbe de rétention, ni taux de complétion, ni test
causal. Les procédés décrits sont des **hypothèses éditoriales étayées par le corpus**, et leur effet
sur nos propres vidéos reste à mesurer après publication.

Le contrôle de couverture signale un intervalle de **2,11 secondes** sans texte dans une vidéo
promotionnelle de theomcx (`7492119209082670358`, 22,82–24,93 s). Il reste signalé dans `summary.json`
au lieu d'être rempli par une phrase supposée. Aucune des 115 présentations individuelles n'a de
trou détecté par ce contrôle ; cela ne garantit pas l'absence d'erreurs de reconnaissance.
