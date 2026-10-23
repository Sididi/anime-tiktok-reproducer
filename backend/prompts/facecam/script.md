Écris le corps d'une présentation de fiction face caméra à partir de la fiche factuelle fournie.
Le créateur a choisi hook et, éventuellement, opinion : ils encadreront automatiquement le corps.
Ne les recopie pas. Si opinion est vide, aucun point de vue ne sera ajouté : n'en invente pas,
n'ajoute ni avis personnel ni bilan subjectif, et termine directement le récit avant la révélation du titre.
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
et descriptions secondaires. À faits utiles égaux, retiens la version la plus courte qui reste fluide.
Si le budget est trop faible ou nul, fournis quand même un récit complet et concis, sans supprimer de bloc.
Il ne s'applique pas au choix preserve_full_story.
Si les faits sont trop minces, reste plus court plutôt que répéter ou inventer pour remplir le temps.

Avant d'écrire, détermine la promesse exacte du hook, les faits qui y répondent et la progression qui
fait monter l'enjeu. Choisis un fil conducteur concret, éclairé aussi par l'opinion si elle est sélectionnée,
et organise les faits en quelques grands mouvements (souvent deux à quatre, selon leur richesse).
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
- Pour chaque phrase, repère ce qu'elle apporte de nouveau. Supprime le doublon ou le commentaire
  qui explique l'émotion déjà produite par les faits. Regroupe les gestes secondaires dans leur résultat.
  Vérifie aussi les doublons à distance : un risque annoncé, puis reformulé juste avant sa conséquence,
  ou un contraste rappelé en fin de paragraphe. Une information donnée dans le hook n'est pas à redonner.
- Remplace les périphrases par des verbes précis et les mots écrits par des mots qu'on dirait vraiment.
  Coupe les amorces, adjectifs et adverbes dispensables. Garde les liens de cause, de temps et les nuances.
- Si une phrase accumule plusieurs subordonnées, resserre-la ou sépare la conséquence. Ne découpe pas
  artificiellement une phrase fluide en trois fragments. Lis mentalement le résultat face caméra.
- Vérifie : accroche payée, progression claire, titre absent, pas de fait inventé ni de faux vécu,
  pas de doublon hook/opinion/raw. Garde tous les blocs et les frontières des extraits originaux.
  En mode preserve_full_story, coupe les mots inutiles, jamais les événements ni les branches.
Retourne uniquement {"blocks":[{"id":"identifiant body fourni","text":"paragraphes du récit"}]}.
Conserve exactement tous les identifiants body, dans l'ordre. Pas de markdown ni de commentaire.
