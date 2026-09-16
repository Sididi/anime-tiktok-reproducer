# Facecam Import — Premiere Pro 2025+ / Windows

1. Fermez Premiere Pro, puis lancez `install_facecam_extension.bat` à côté du dossier `facecam-import`.
2. Ouvrez Premiere et un projet vide, puis **Fenêtre → Extensions → Facecam Import**.
3. Téléchargez le ZIP du projet depuis Discord et extrayez-le entièrement. Ne lancez pas le script depuis le ZIP.
4. Lancez `run_in_premiere.bat` dans le dossier extrait. Vous pouvez aussi choisir `import_project.jsx` dans le panneau.
5. Gardez le panneau ouvert jusqu'au message de réussite. Le projet s'importe avec tous ses médias locaux.
6. Après montage, envoyez **un seul MP4 à la racine du dossier Drive** indiqué dans Discord. Cliquez sur **Confirmer montage envoyé**.

La caméra est sur V4, les sous-titres sur V5. La voix est sur A2 et reste indépendante des coupes de caméra. Les extraits anime conservent leur audio. La couverture finale dure une seconde.

Cette extension s'installe à côté de l'extension principale. Elle ne contient ni connexion réseau, ni export automatique, ni gestion de lots. Son dossier de réception est `%APPDATA%\Adobe\FacecamImport\inbox`. Les demandes en erreur deviennent des fichiers `.failed`; corrigez le problème puis relancez le `.bat`.

Pour désinstaller, supprimez uniquement `%APPDATA%\Adobe\CEP\extensions\facecam-import` lorsque Premiere est fermé.
