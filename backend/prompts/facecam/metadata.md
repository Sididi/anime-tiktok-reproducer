# Métadonnées Facecam

À partir de l'anime et de la narration fournis, propose huit titres, UNE seule description
commune à Facebook, Instagram et YouTube, ainsi que les tags de recherche YouTube.
Les hashtags et le texte TikTok sont ajoutés par l'application : ne les génère pas.

Écris en français naturel. Ne révèle ni le titre de l'œuvre ni les noms des personnages
dans les titres ou la description. Les tags YouTube peuvent nommer l'œuvre.
N'invente aucun événement absent du script.

# Bloc 1 : les 8 titres (le cœur de ta mission)

Ces règles sortent de l'analyse statistique des 200 dernières vidéos de la chaîne. Elles ne sont pas des suggestions.

## Règles dures

1. Longueur cible : 36 à 48 caractères, espaces comprises (le sweet spot mesuré : ×1,27). Maximum absolu : 62 — au-delà, l'application tronque le titre en plein mot et le titre est mort.
2. AUCUNE question générique : les titres en «?» font ×0,54 vs la médiane. Seule exception autorisée : la construction «Et si … toi ?» du slot 1.
3. AUCUN point d'exclamation (×0,78). AUCUN chiffre (×0,81).
4. AUCUN jargon isekai/RPG : mage, niveau, guilde, build, boss, stats, level (×0,91 — ce vocabulaire rétrécit l'audience aux gamers).
5. Par défaut, ouvre sur un pronom-personnage : Il / Elle / Son / Sa / Cette (×1,31) — sauf si la formule du slot impose autre chose.
6. Le vague tue : «pas comme les autres», «très particulier», «incroyable» sont interdits. Toujours un détail concret à la place. Exception : le slot 2 (secret découvert) reste volontairement sans détail.
7. 0 à 1 emoji maximum, seulement s'il ajoute une émotion (😭 par exemple). Aucun hashtag dans les titres.
8. Chaque titre doit fonctionner tel quel sur YouTube, Facebook, Instagram et TikTok.

## Les 8 slots (dans cet ordre, exactement un titre chacun)

Chaque slot exploite un levier psychologique prouvé sur CETTE chaîne. Les ancres sont de vrais titres à succès de la chaîne : imite leur mécanique, pas leurs mots, et adapte au script.

1. **Menace / hypothèse en 2ᵉ personne** — implique directement le spectateur avec «te», «toi», «ton». Levier n°1 mesuré : ×3,07. Prends la menace ou le pouvoir central du script et retourne-le vers le spectateur.
   Ancre : «Et si quelqu'un pouvait prendre le contrôle de toi ?» (1,9M de vues)
2. **Secret découvert** — une vérité cachée vient d'éclater, sans donner AUCUN détail (lexique secret/caché/découvert : ×1,62). Le manque d'information force le clic.
   Ancre : «Son lourd secret vient d'être découvert» (732k)
3. **Concept absurde assumé** — énonce la prémisse la plus bizarre du script au premier degré, sans la commenter. Plus c'est étrange dit sobrement, plus ça clique.
   Ancres : «Son rencard virtuel était un vieux daron 😭» (1,3M) · «Il fuit sa femme extraterrestre partout» (711k)
4. **Prix sombre en deux temps** — désir énoncé en phrase courte. Point. Conséquence sombre. Le contraste désir/prix crée la tension.
   Ancre : «Elle voulait être belle. Le prix était monstrueux» (729k)
5. **«Quand …» relatable** — format meme situationnel (×1,77) : le spectateur se reconnaît, ou reconnaît quelqu'un.
   Ancre : «Quand ton voisin défonce ton mur pour défendre son anime» (729k)
6. **Interdit / tabou** — la transgression sociale ou relationnelle du script : intrusion, relation interdite, mensonge de trop, place volée.
   Ancre : «Elle était l'intruse de sa propre maison» (709k)
7. **Full-send choc** — l'affirmation la plus agressive que le script peut vaguement soutenir. Sur ce slot uniquement, l'exagération est autorisée : promets l'extrême.
8. **Factuel affûté** — description honnête de la scène, mais avec un verbe d'action fort et un détail concret. C'est le choix de sécurité, pas le choix mou.

Interdiction de paraphrase : les 8 titres doivent exploiter des éléments DIFFÉRENTS du script, pas 8 variantes de la même idée.

# Description commune

`description` : une ou deux phrases simples qui présentent la situation racontée.
Aucun appel à l'action (CTA), aucune invitation à commenter, s'abonner, liker, partager
ou regarder la suite, aucune question adressée au public. Aucun hashtag ni liste de tags.
Ne génère jamais de description différente selon la plateforme.

`youtube_tags` : titre de l'œuvre, variantes usuelles de son nom, genre, anime, manga.

# Sortie JSON exclusivement

{"title_candidates": ["Titre 1", "Titre 2", "... huit titres au total"],
 "description": "Une ou deux phrases simples, sans CTA ni hashtags.",
 "youtube_tags": ["anime", "manga", "nom de l'œuvre"]}

# Données d'entrée

Œuvre : [OEUVRE]
Narration complète : [SCRIPT]
