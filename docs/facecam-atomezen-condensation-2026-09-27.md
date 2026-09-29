# Facecam : choisir le récit avant de l'écrire

Analyse du 27 septembre 2026. Comparaison de la vidéo
[atomezen](https://www.tiktok.com/@atomezen/video/7666849486450265366), de la
[source espagnole](https://www.tiktok.com/@narradoya2/video/7666139512665296148)
et du projet local `d038484047ac`.

## Conclusion de la comparaison

Atomezen conserve presque toute la première partie, dans le même ordre, et abandonne la suite.
Sur cette partie commune, son français contient **327 mots contre 316 dans notre version**.
La durée de 1:04 ne démontre donc pas une forte compression phrase par phrase. Elle résulte
d'abord d'un périmètre plus court, accompagné d'un débit effectif particulièrement élevé.

Le changement utile pour notre facecam est de **choisir une histoire et son point d'arrivée avant
de rédiger**, puis d'organiser les images autour de ce choix. La fiche factuelle introduite dans
la première refonte aide à sortir de la formulation source, mais ne suffit pas tant que le système
exige de couvrir tous les plans et conserve toute la progression initiale.

Ce document propose des choix éditoriaux et techniques. Il ne modifie pas la génération ni le projet.

## Données, méthode et limites

La vidéo d'atomezen a été téléchargée avec ses métadonnées, puis transcrite en français avec
faster-whisper `large-v3`, horodatage des mots activé. La source espagnole a également été
retranscrite indépendamment pour vérifier les passages ambigus de la transcription du projet.
Des planches de photogrammes complètent la comparaison textuelle. La provenance commune des plans
est confirmée par l'utilisateur ; cette analyse n'est pas un nouvel appariement exhaustif des images.

Les durées viennent de `ffprobe`, les nombres de mots de `str.split()` sur les textes complets.
Les contractions comptent comme un seul mot lorsqu'elles ne contiennent pas d'espace. L'ASR peut
faire varier légèrement ces nombres, notamment les noms propres. Les repères d'atomezen et de la
retranscription espagnole sont des estimations ASR, pas un alignement manuel à l'image près.

Le projet est une reproduction française standard, antérieure à la récente refonte facecam.
Il ne constitue donc pas une mesure directe du résultat de cette refonte. L'examen du code facecam
montre toutefois que la contrainte de couverture intégrale y subsiste.

| Version | Durée mesurée | Mots | Débit effectif |
| --- | ---: | ---: | ---: |
| Source espagnole | 105,12 s | 439 dans la transcription du projet | Non comparé au français |
| Notre synthèse française, `new_tts.wav` | 138,16 s | 463 | 201 mots/min |
| Notre audio après traitement, `output/tts_edited.wav` | 130,45 s | Même script de 463 mots | 213 mots/min |
| Vidéo atomezen | 64,77 s | 327 | 303 mots/min |

Les **2:18** évoquées correspondent bien à la synthèse française. L'audio traité local est plus court,
à 2:10,45. Aucun export vidéo final n'est présent dans ce dossier pour en mesurer la durée exacte.
Le débit est calculé sur toute la durée du média : il inclut pauses et montage. Cela ne permet pas
de déterminer si l'enregistrement a été accéléré, ni de reconstituer la méthode de travail du créateur.

Les fichiers d'analyse sont conservés dans `backend/data/research/atomezen-2026-09-27/`, ignoré par Git :

- `videos/7666849486450265366/` : vidéo, métadonnées, transcription JSON et texte ;
- `source-independent-transcript.json` : vérification indépendante de la source espagnole ;
- `frames/` : planches visuelles des deux vidéos ;
- `comparison.json` : mesures et empreintes SHA-256 des entrées ;
- `compare.py` : recalcul des mesures, sans écriture dans le projet source.

## Ce qu'atomezen conserve et ce qu'il retire

Voici une correspondance sémantique, pas une reproduction de son script.

| Position chez atomezen | Contenu conservé depuis la source | Fonction dans le récit |
| --- | --- | --- |
| 0–12,7 s | Retour, vêtements abîmés, vêtements offerts, visite refusée, papiers brûlés, démarche à la guilde | Accumulation de difficultés ordinaires ; beaucoup de logistique reste racontée |
| 12,8–23,4 s | Statue à son effigie, reconnaissance publique de son héroïsme, longue guerre, amis persuadés de sa mort | Première contradiction forte : célèbre comme héros disparu, inconnu à son retour |
| 23,5–35,9 s | Ancien compagnon qui le reconnaît, retrouvailles, arrivée du second, joie, surprise devant sa jeunesse | Résolution de la question de la reconnaissance personnelle ; nouvelle anomalie |
| 36–41,6 s | Découverte de son visage dans un miroir, hypothèse sur la force vitale absorbée | Explication du rajeunissement, avec une répétition partielle de la surprise précédente |
| 41,7–48,5 s | Amis devenus roi et maître de guilde, conversation des trois | Mesure du temps écoulé et du changement de statut |
| 48,5–58 s | Choix d'une nouvelle identité, reprise comme simple aventurier, rang S caché et classe F affichée | Décision du héros et nouveau point de départ ; clôture possible d'un premier arc |
| 58,1–61,9 s | La fille du maître de guilde remarque la déférence du majordome | Amorce secondaire, sans résolution dans cette vidéo |
| 62–64,7 s | Demande d'une mission contre des gobelins | Transition vers la suite, puis arrêt |

La vidéo s'arrête avant le refus de la réceptionniste. Toute la suite disparaît : danger de la
mission pour les novices, obligation de former un groupe, deux partenaires, présentation de leurs
capacités, trajet, lecture des traces, découverte de leur falsification, soupçon d'une intelligence
derrière les gobelins et décision du héros de laisser apprendre les jeunes en les protégeant.

Dans notre script, ce bloc supprimé représente **147 mots sur 463**, soit environ **32 %**.
Les 316 autres mots couvrent pratiquement les mêmes événements qu'atomezen. Son texte de 327 mots
est légèrement plus long sur ce périmètre commun, d'environ 3,5 %.

La dernière action retenue se termine vers **70,26 s dans la source espagnole** et **87,56 s dans
notre audio français traité**. Atomezen lui consacre environ 64,77 s. Il reprend donc essentiellement
les 70 premières secondes de la source de 105 secondes. Il conserve notamment des détails que
l'on pourrait supprimer dans une véritable réécriture dense : vêtements, déplacements, démarche
administrative, miroir après la découverte du rajeunissement, réaction de la fille.

Cela exclut l'interprétation « il raconte la même histoire entière en deux fois moins de temps ».
Il raconte une portion plus courte et l'énonce vite. Une lecture hypothétique de ses 327 mots au
débit moyen de notre synthèse durerait **97,58 s**, soit environ 1:38. Cette simulation suppose un
débit uniforme ; elle illustre le poids du débit, sans attribuer un pourcentage causal exact à
chaque facteur de réduction.

## Remodelage et rétention : ce qu'on peut réellement en déduire

**La transformation principale est une coupe de périmètre.** L'ordre des révélations, les petits
déplacements et même les amorces secondaires restent très proches de la source. On observe une
adaptation française et un arrêt anticipé, pas une reconstruction globale de la dramaturgie.
Nous ne connaissons pas son processus de rédaction, ni la raison exacte du point de coupe.

**L'accroche parlée reste descriptive.** Elle commence par le retour du personnage. La légende de
publication expose davantage la situation dramatique — retour après une longue absence et héros
cru mort — mais le texte prononcé n'en fait pas immédiatement une contradiction forte. Pour notre
facecam, les accroches spécifiques au récit étudiées chez adhnime restent une meilleure direction.

**Les bascules utiles viennent en grande partie des événements de la source.** La statue transforme
un simple retour en problème d'identité. Les retrouvailles répondent à ce problème mais révèlent le
rajeunissement. Les nouveaux statuts des amis rendent les années perdues concrètes. Enfin, cacher
son rang transforme un retour de héros en reprise volontaire au bas de l'échelle. Il faut conserver
ces changements d'attente, pas seulement les connecteurs qui les accompagnent.

**La fin est perfectible pour notre objectif.** Le choix de l'identité et du rang apparent produit
un aboutissement vers 58 s. Les six dernières secondes conservent deux départs de sous-intrigues,
dont la demande de mission, sans développer leur enjeu. On ne peut pas conclure qu'ils ont été
ajoutés pour dépasser une minute. On peut en revanche choisir, dans notre version, de consacrer
ce temps à un aboutissement plus net plutôt qu'à une nouvelle branche laissée en suspens.

**Débit et rétention ne sont pas équivalents.** Les 303 mots/min observés expliquent une partie
de la brièveté. Ils ne prouvent ni une meilleure compréhension ni une meilleure rétention : nous
n'avons pas les courbes d'audience. Pour une prise facecam expressive, la durée doit dépendre de
la lecture réelle de l'utilisateur. Copier ce débit comme règle universelle serait fragile.

Les 327 mots d'atomezen dépassent aussi les médianes des présentations individuelles du
[corpus précédent](facecam-narration-analysis-2026-09-26.md) : 234 chez theomcx, intro de marque
retirée, et 251 chez adhnime. Ces corpus décrivent d'autres vidéos, pas une expérience contrôlée,
mais ils confirment qu'un format voisin d'une minute n'impose pas de viser 327 mots.

## Deux choix d'histoire à partir de la même matière

Les techniques de forme issues du corpus adhnime/theomcx restent pertinentes : contradiction
immédiate, contexte introduit quand il devient nécessaire, bascules compréhensibles, fin qui répond
à l'accroche. Aucun slogan de créateur n'est à reproduire. La nouvelle étape consiste à décider
quels faits méritent cette minute.

### A. Le héros disparu choisit l'anonymat

Question centrale : comment un héros célébré par une statue en arrive-t-il à repartir au rang F ?

Garder la contradiction statue/porte fermée, l'absence prolongée, les retrouvailles, le changement
de génération et la décision d'une identité discrète. Regrouper les déplacements et les retrouvailles ;
retirer les vêtements offerts, la discussion avec le passant, la réaction de la fille et toute la
mission. Le miroir peut illustrer le rajeunissement sans exiger une phrase sur le fait de s'y regarder.

Cette option développe un arc proche du périmètre d'atomezen, avec une sélection plus stricte des
détails et une vraie conclusion. Elle est simple à monter en conservant l'ordre principal des plans.

Exemple original de travail, fondé sur les faits du projet ; texte à lire et à ajuster, pas durée garantie :

> On lui a construit une statue de héros. Pourtant, quand il revient à la capitale, le majordome de son ancien compagnon lui ferme la porte au nez.
>
> Pendant des années, il a combattu les démons. Ses papiers ont brûlé dans la bataille et ses amis le croient mort depuis longtemps. Alors, quand il découvre cette statue sur la place, il comprend que la ville célèbre un homme qu'elle pense avoir perdu.
>
> À la guilde, un ancien compagnon le reconnaît immédiatement. Un deuxième arrive, et tous les deux pleurent en le retrouvant. Ils ont changé de vie : l'un est devenu roi, l'autre dirige la guilde.
>
> Lui aussi a changé, mais dans l'autre sens. Il se découvre un visage d'adolescent. Il pense que la force vitale absorbée pendant son combat l'a rajeuni.
>
> Il faut maintenant lui refaire des papiers. Et comme tout le monde le croit mort, il préfère éviter les problèmes : il choisit une nouvelle identité et recommence comme simple aventurier.
>
> Son ami lui prépare donc des papiers qui cachent son rang S. Dessus, une seule chose apparaît : sa seconde classe, guerrier de niveau F.
>
> Le voilà inscrit parmi les débutants, alors que la capitale lui a déjà élevé une statue. Et c'est lui qui a choisi cette nouvelle vie.
>
> Le titre s'affiche juste ici.

### B. Le faux débutant devient le protecteur des novices

Question centrale : que se passe-t-il quand on traite un aventurier de rang S comme un débutant ?

Le retour devient un contexte très court qui explique la fausse identité. Supprimer la logistique,
le miroir, les statuts des deux amis et la réaction de la fille. Garder le rang dissimulé, le refus
de la mission en solo, le groupe imposé, les traces trompeuses et la décision de laisser les jeunes
apprendre. La spécialité de la mage et le détail du trajet ne servent pas cet enjeu.

Cette option prend sa conclusion dans le dernier tiers qu'atomezen supprime. Elle montre pourquoi
une règle « garder seulement le début » serait insuffisante. Pour ce cas, c'est mon option préférée :
le contraste initial produit une complication concrète puis un changement de rôle. Ce jugement est
éditorial, pas une prédiction mesurée de performance.

Exemple original de travail :

> Sur ses papiers, ce type est un aventurier débutant. En réalité, son ami a volontairement caché son rang S.
>
> Il vient de retrouver la capitale après des années de combat contre les démons. Tout le monde le croyait mort. On lui a même construit une statue. Mais pour éviter les problèmes, il choisit une nouvelle identité et recommence en bas de l'échelle.
>
> Sauf que cette couverture va lui compliquer une mission toute simple : éliminer des gobelins.
>
> La réceptionniste refuse de le laisser partir seul. Trop de novices sont morts en les sous-estimant. Il doit rejoindre un groupe, et quand deux jeunes aventuriers l'invitent, elle accepte pour lui avant qu'il puisse refuser.
>
> Les voilà donc partis avec un compagnon dont ils ignorent le véritable niveau. Sur place, le jeune pisteur examine les traces : il estime qu'il y a une dizaine de gobelins. Le héros le félicite. Son raisonnement serait bon dans une situation normale.
>
> Mais ici, quelqu'un a maquillé les empreintes. Et un gobelin ordinaire n'est pas assez malin pour faire ça. Il y a donc autre chose derrière cette mission.
>
> Le héros l'a compris, mais il garde le silence. Il veut laisser les débutants apprendre, tout en restant prêt à les protéger.
>
> Celui qu'on empêchait de partir seul devient finalement celui qui veille sur les autres.
>
> Le titre s'affiche juste ici.

Les deux propositions n'ajoutent pas d'avis personnel. L'opinion reste optionnelle comme dans
l'interface actuelle. Le nombre de mots inclut l'accroche et l'annonce du titre.

| Proposition | Mots | Durée illustrative au débit de notre synthèse, carte de 1 s comprise |
| --- | ---: | ---: |
| A — retour et anonymat | 217 | 65,75 s |
| B — faux débutant et protection des novices | 223 | 67,54 s |

Ces durées sont des calculs, pas des enregistrements des propositions.

## Cible temporelle et compromis de lecture

Proposition de cible : **65 à 70 secondes pour le montage final**. Cela laisse une marge au-dessus
de la minute visée par l'utilisateur. TikTok décrit son programme comme récompensant des contenus
originaux de qualité de plus d'une minute ; cette durée est une condition, pas une garantie de
monétisation. [Présentation officielle du programme](https://newsroom.tiktok.com/introducing-the-new-creator-rewards-program?lang=en).

La formule de travail est :

`durée ≈ mots prononcés / débit effectif personnel + extraits audio originaux + carte finale`

Le débit effectif inclut déjà les pauses normales de lecture : ne pas les déduire une deuxième fois.
Le budget de mots inclut hook, récit, opinion éventuelle et annonce du titre. Le code actuel ajoute
une carte finale d'une seconde. Ce projet n'a aucun extrait audio original (`raw`). À titre
illustratif, au débit de sa synthèse initiale, 215 à 230 mots donnent environ 65 à 70 secondes
carte comprise. Ce n'est pas une mesure du débit facecam de l'utilisateur.

Il faudra calibrer la fourchette sur une lecture réelle, puis mesurer le montage après suppression
des silences et insertion des éventuels extraits. Un plafond de mots seul ne garantit pas la durée.
Si le résultat dépasse la cible, retirer ou regrouper une étape secondaire ; s'il est trop court,
développer une conséquence utile déjà établie. Éviter d'ajouter des répétitions ou de changer
automatiquement la vitesse pour remplir la fenêtre.

## Pourquoi un changement de prompt ne suffit pas

Le code conserve actuellement plusieurs contraintes liées au montage source :

| Endroit | Contrainte actuelle | Conséquence |
| --- | --- | --- |
| `FacecamNarrationService.validate_brief()` | Les indices de faits doivent couvrir exactement tous les plans de chaque bloc, dans l'ordre | L'inventaire reste exhaustif ; aucune sélection éditoriale séparée ne filtre ce qui arrive au rédacteur |
| Prompts `brief.md` et `script.md` | Tous les blocs et leurs plans restent présents ; les ancrages et la chronologie sont conservés | La demande de concision entre en tension avec l'obligation de préserver la couverture |
| `FacecamService.validate_ranges()` | Tous les indices attendus, avec une plage de mots non vide pour chacun | Un plan abandonné ne peut pas simplement disparaître de la répartition |
| `FacecamService.approve()` | Reconstruction de la liste complète de références et de correspondances | Modifier uniquement le texte laisserait en place la structure du montage initial |
| `FacecamMediaService.manifest()` | Une scène avec texte vide termine un segment de narration | Le texte vide sert à gérer les interruptions, notamment les extraits originaux ; il n'est pas un marqueur fiable de suppression |

La fiche factuelle peut rester exhaustive : elle constitue une réserve de faits. C'est la nouvelle
**sélection éditoriale** qui doit décider ce qu'on utilise. Il faut distinguer un fait disponible,
un fait raconté et une image qui illustre le récit. Une seule phrase peut se dérouler sur plusieurs
plans sans commenter toutes leurs actions.

Un détail concret montre la limite d'une sélection uniquement par numéro de scène : le point où
s'arrête atomezen tombe **dans la scène source 42**. Elle contient à la fois la fin de la demande
de mission et le début du refus. Il faut pouvoir raccourcir un extrait à l'intérieur d'un plan,
ou choisir une autre illustration de la phrase finale, plutôt que conserver automatiquement la
scène entière et son événement suivant.

## Mise en œuvre proposée, après choix éditorial

1. **Conserver l'inventaire des faits et des images disponibles.** Les données du projet source
   restent une référence immuable. Les erreurs ou ambiguïtés de noms ne doivent pas être corrigées
   de mémoire ; les exemples ci-dessus utilisent les rôles des personnages.
2. **Proposer deux ou trois angles avant les accroches.** Pour chaque angle : promesse, point
   d'arrivée, trois ou quatre étapes, faits retenus, faits regroupés, faits écartés et budget.
   La comparaison A/B de ce document est le prototype de ce choix dans le produit.
3. **Écrire les accroches puis le corps à partir de l'angle choisi.** Le rédacteur reçoit les
   faits sélectionnés, leurs dépendances et le budget total. Les techniques du corpus initial
   restent des règles de forme. Une opinion choisie consomme une part du même budget.
4. **Construire le montage à partir des plans sélectionnés.** Réutiliser les correspondances déjà
   trouvées dans l'anime, filtrer les plans inutiles et autoriser des coupes internes. Conserver
   les identifiants source dans une table explicite, distincte des indices de sortie. Il n'est pas
   nécessaire de rechercher tous les plans à nouveau pour cette première version.
5. **Contrôler cohérence et durée.** L'accroche reçoit une réponse ; chaque transition reste
   compréhensible malgré les omissions ; les images correspondent aux faits ; aucune nouvelle
   cause n'est inventée pour relier les coupes. Mesurer ensuite la durée de la vraie prise montée.

La première version peut garder l'ordre des plans sélectionnés dans le corps. Cela suffit pour
les deux angles proposés, avec une accroche facecam qui annonce la contradiction. Autoriser une
réorganisation générale des images ajouterait une difficulté d'alignement qui n'est pas nécessaire
pour prouver ce changement éditorial. Les projets avec dialogues originaux demandent une politique
explicite sur les extraits conservés ; ce cas sans `raw` permet d'évaluer la sélection sans ce
compromis supplémentaire.

Les contrôles techniques utiles, lors de l'implémentation, porteront sur les plans réellement
supprimés du montage, la coupe interne à la scène 42, la stabilité des références source, la
correspondance voix/images et la durée finale. Une simple vérification du nombre de mots ne
validerait pas le nouveau comportement.
