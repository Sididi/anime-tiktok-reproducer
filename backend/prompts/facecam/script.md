Écris le corps d'une présentation de fiction face caméra à partir de la fiche factuelle fournie.
Le créateur a choisi hook et, éventuellement, opinion : ils encadreront automatiquement le corps.
Ne les recopie pas. Si opinion est vide, aucun point de vue ne sera ajouté : n'en invente pas,
n'ajoute ni avis personnel ni bilan subjectif, et termine directement le récit avant la révélation du titre.
La phrase de révélation du titre sera également ajoutée automatiquement.

Si selected_story est présent, raconte uniquement cette histoire et termine sur son point d'arrivée.
La sélection des images est déjà faite : ne réintroduis pas les branches et détails écartés.
Une phrase peut couvrir plusieurs plans ; elle n'a pas à décrire chaque geste à l'écran.
Si preserve_full_story vaut true, le créateur a choisi de garder toute l'histoire : conserve toutes
les branches, étapes et conséquences établies dans la fiche, jusqu'à la fin du récit source.
La durée est libre : ne supprime aucun événement pour viser une minute. Relie les événements
en un récit fluide, sans recopier la source ni répéter l'accroche ou les dialogues originaux.

Lorsqu'il est fourni, body_budget donne le nombre de mots réservé au corps, après déduction de l'accroche, de l'opinion
éventuelle et de l'annonce du titre. Vise cette fourchette et ne dépasse jamais son maximum.
Ce budget tient compte des extraits audio conservés et de la carte finale pour viser 65 à 70 secondes.
Il ne s'applique pas au choix preserve_full_story.
Si les faits sont trop minces, reste plus court plutôt que répéter ou inventer pour remplir le temps.

Avant d'écrire, détermine la promesse exacte du hook, les faits qui y répondent et la progression qui
fait monter l'enjeu. Choisis un fil conducteur concret, éclairé aussi par l'opinion si elle est sélectionnée,
et organise les faits en quelques grands mouvements (souvent deux à quatre, selon leur richesse).
Lorsqu'elle est présente, l'opinion donne un regard au récit entier, elle n'est pas un avis interchangeable collé à la fin.
Écris ensuite un récit continu par macro-séquences. On doit avoir envie de savoir
ce que le personnage va faire, pas seulement constater ce que chaque plan montre.
Le début prolonge immédiatement l'accroche : pas de deuxième départ ni de répétition du même fait.
Introduis seulement le contexte nécessaire. Condense les actions secondaires, développe le choix ou
la conséquence qui donne son intérêt à la scène. Conserve les actions indispensables aux ancrages
visuels, leur chronologie et leurs relations réelles. Une simple succession reste une succession.
Si une opinion est sélectionnée, prépare une transition naturelle vers elle sans la paraphraser à l'avance.
Finis le corps sur la situation concrète atteinte, pas sur un deuxième bilan de ce qui vient d'être raconté.

La longueur respecte les faits disponibles et le budget lorsqu'il est fourni, jamais le nombre de mots du texte
source. N'étire pas un récit mince pour atteindre un quota et ne compresse pas les faits au point de
perdre leurs liens. Évite la récitation exhaustive et les relances automatiques à chaque phrase.
Chaque bloc body couvre ses indices, mais sa prose ne se découpe pas plan par plan.
Le timeline fixe les frontières raw : ne déplace aucun fait à travers un extrait original ; prépare
sa compréhension avant, reprends sa conséquence après sans raconter une deuxième fois le dialogue.
Ne résume pas le dernier bloc par un faux cliffhanger si ses faits donnent déjà une résolution.

Relis silencieusement : accroche payée, progression claire, titre absent, pas de fait inventé,
pas de faux vécu, pas de doublon hook/opinion/raw, phrases lisibles à voix haute et blocs préservés.
Retourne uniquement {"blocks":[{"id":"identifiant body fourni","text":"paragraphes du récit"}]}.
Conserve exactement tous les identifiants body, dans l'ordre. Pas de markdown ni de commentaire.
