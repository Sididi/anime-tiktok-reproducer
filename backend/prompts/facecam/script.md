Écris le corps d'une présentation de fiction face caméra à partir de la fiche factuelle fournie.
Fais progresser les événements au futur proche, avec les exceptions de sens précisées dans la
consigne de temps. Le hook peut employer un autre temps sans imposer celui du corps.
Le créateur a choisi hook et, éventuellement, opinion : ils encadreront automatiquement le corps.
Ne les recopie pas. Si opinion est vide, aucun paragraphe d'avis ne sera ajouté : n'en invente pas,
n'ajoute ni recommandation personnelle ni bilan final, et termine directement le récit avant la révélation du titre.
Le ton reste expressif : des mots chocs et des hyperboles comme « complètement fou » ou « absolument dingue »
peuvent ponctuer les moments clés même sans opinion sélectionnée.
La phrase de révélation du titre sera également ajoutée automatiquement.

Si selected_story est présent, raconte uniquement cette histoire et termine sur son point d'arrivée.
La sélection des images est déjà faite : ne réintroduis pas les branches et détails écartés.
Une phrase peut couvrir plusieurs plans ; elle n'a pas à décrire chaque geste à l'écran.
La fiche sert à vérifier ce que tu racontes, pas à imposer une phrase par fait. Raconte les événements
utiles et leurs conséquences ; absorbe la logistique dans une action, sans détour ni double explication.
Si preserve_full_story vaut true, le créateur a choisi de garder toute l'histoire : conserve toutes
les branches, étapes et conséquences établies dans la fiche, jusqu'à la fin du récit source.
La durée est libre : ne supprime aucun événement pour viser une minute. Relie les événements
en un récit fluide, sans recopier la source ni répéter l'accroche ou les dialogues originaux.

Lorsqu'il est fourni, body_budget donne un repère de longueur pour le corps, après déduction de l'accroche,
de l'opinion éventuelle et de l'annonce du titre. Privilégie un récit dense, clair et complet dans ce repère.
Ce budget tient compte des extraits audio conservés et de la carte finale pour viser 65 à 70 secondes.
min_words n'est pas un plancher à remplir : un récit abouti plus court vaut mieux que des phrases diluées.
max_words invite à resserrer les formulations, pas à supprimer un événement nécessaire à la compréhension.
Ce repère n'impose pas de limite bloquante et ne justifie jamais d'allonger une phrase qui fonctionne déjà.
Si ton brouillon dépasse max_words, fais d'abord une vraie coupe des répétitions, transitions vides
et descriptions secondaires. Garde une version concise qui conserve la fluidité et la force des moments clés.
Une courte formule catchy ou un intensif utile n'est pas du remplissage à supprimer pour gagner deux mots.
Si le budget est trop faible ou nul, fournis quand même un récit complet et concis, sans supprimer de bloc.
Il ne s'applique pas au choix preserve_full_story.
Si les faits sont trop minces, reste plus court plutôt que répéter ou inventer pour remplir le temps.
Approfondis ce qui rend cette histoire intéressante : le désir du personnage, une règle précise,
le coût d'un choix, la portée d'un événement. Remplace un descriptif faible par une précision utile
de la fiche, sans ajouter automatiquement une scène ni raccourcir les liens qui font tenir le récit.

Construis le récit en quatre temps, avant de rédiger les paragraphes finaux :
1. Liste silencieusement les vrais moments clés de l'histoire choisie : ce qui déclenche l'action,
   révèle une règle utile, change l'objectif ou le rapport de force, oblige à choisir, ou produit
   une conséquence décisive. Chaque moment doit répondre à la promesse du hook ou préparer la suite.
   Un changement de plan, un déplacement ou une description ne constitue pas à lui seul un moment clé.
2. Mets ces moments dans leur chronologie réelle, avec selected_story.steps et ending comme repères.
   L'ordre des entrées facts suit les plans source : une source peut commencer par un teaser de la fin.
   À l'intérieur d'un même bloc body, reconstruis la progression qui mène au point d'arrivée choisi ;
   ne termine pas sur une ancienne étape juste parce qu'elle apparaît en dernier dans la fiche.
   Place le contexte utile juste avant le moment qu'il éclaire. Un fait annoncé par le hook est déjà
   connu ; le corps raconte ce qui y conduit ou ce qui en découle. Les blocs restent à leur place :
   ne déplace aucun fait à travers un extrait raw, dont l'ordre et la préparation sont fixes.
3. Trouve les mots chocs autour des moments qui méritent d'être accentués : verbe net, formule catchy,
   hyperbole expressive, contraste qui frappe. Fais sentir pourquoi le pouvoir est « complètement fou »
   ou la situation « absolument dingue » avec le fait concret qui l'accompagne. Assume cet enthousiasme.
4. Relie le tout en un récit oral continu. Les liaisons servent la cause, l'opposition ou la conséquence ;
   elles donnent envie d'entendre le moment suivant. Garde seulement le contexte nécessaire entre eux.
Cette préparation reste interne : retourne le récit, pas la liste ni le plan. En mode preserve_full_story,
hiérarchise les moments forts tout en conservant toutes les branches et étapes, y compris secondaires.
Les moments clés développent un même fil conducteur. La chronologie vérifie leur ordre réel ;
elle ne suffit pas à construire la narration. Fais comprendre ce qui attire le personnage,
ce qui le met en difficulté et la portée des bascules. Si le texte énumère ses activités,
reprends sa construction plutôt que de rajouter des connecteurs.
Lorsqu'elle est présente, l'opinion donne un regard au récit entier, elle n'est pas un avis interchangeable collé à la fin.
Écris ensuite un récit continu par macro-séquences. On doit avoir envie de savoir
ce que le personnage va faire, pas seulement constater ce que chaque plan montre.
Le début prolonge immédiatement l'accroche : pas de deuxième départ ni de répétition du même fait.
N'ouvre pas par « Ce type, c'est [nom] » ou une présentation qui n'apporte que le nom du personnage.
Si l'accroche contient déjà une information, utilise-la comme acquise. Pour un retour en arrière
nécessaire, un simple repère de temps suffit ; pas de « pour comprendre, il faut revenir au début ».
Introduis seulement le contexte nécessaire. Condense les actions secondaires, développe le choix ou
la conséquence qui donne son intérêt à la scène. Conserve les actions indispensables aux ancrages
visuels, leur chronologie et leurs relations réelles. Une simple succession reste une succession.
Si une opinion est sélectionnée, prépare une transition naturelle vers elle sans la paraphraser à l'avance.
Finis le corps sur la situation concrète atteinte, pas sur un deuxième bilan de ce qui vient d'être raconté.

La longueur dépend des faits utiles à l'histoire et du budget indicatif, jamais du nombre de mots du texte
source ou de la fiche. N'étire pas un récit pour atteindre un quota et ne compresse pas les faits au point de
perdre leurs liens. Évite la récitation exhaustive et les relances automatiques à chaque phrase.
Chaque bloc body couvre ses indices, mais sa prose ne se découpe pas plan par plan.
Le timeline fixe les frontières raw : ne déplace aucun fait à travers un extrait original ; prépare
sa compréhension avant, reprends sa conséquence après sans raconter une deuxième fois le dialogue.
Ne résume pas le dernier bloc par un faux cliffhanger si ses faits donnent déjà une résolution.

Avant de rendre le texte, fais une passe silencieuse de coupe et d'oralisation :
- Pour chaque phrase, repère sa fonction : moment clé, contexte indispensable, liaison ou accent expressif.
  Une précision peut éclairer un enjeu vague, sans obligation d'apporter un événement nouveau
  à chaque phrase. Garde le contexte, les liens et les réactions qui font comprendre la situation.
  Supprime les doubles explications ; préserve une formule courte qui fait frapper le moment.
  Regroupe les gestes secondaires dans leur résultat.
  Vérifie aussi les doublons à distance : un risque annoncé, puis reformulé juste avant sa conséquence,
  ou un contraste rappelé en fin de paragraphe. Une information donnée dans le hook n'est pas à redonner.
- Remplace les périphrases par des verbes précis et les mots écrits par des mots qu'on dirait vraiment.
  Coupe les longueurs sans neutraliser la voix : garde ou améliore les intensifs et les formulations catchy
  qui portent les temps forts. Garde les liens de cause, de temps et les nuances factuelles.
- Si une phrase accumule plusieurs subordonnées, resserre-la ou sépare la conséquence. Ne découpe pas
  artificiellement une phrase fluide en trois fragments. Lis mentalement le résultat face caméra.
- Vérifie : accroche payée, progression claire, titre absent, pas de fait inventé ni de faux vécu,
  pas de doublon hook/opinion/raw. Garde tous les blocs et les frontières des extraits originaux.
  Vérifie aussi le futur proche des actions et conséquences, sans convertir les états, règles,
  antériorités ou hypothèses qui nécessitent un autre temps.
  En mode preserve_full_story, coupe les mots inutiles, jamais les événements ni les branches.
Retourne uniquement {"blocks":[{"id":"identifiant body fourni","text":"paragraphes du récit"}]}.
Conserve exactement tous les identifiants body, dans l'ordre. Pas de markdown ni de commentaire.
