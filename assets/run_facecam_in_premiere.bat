@echo off
setlocal DisableDelayedExpansion
set "FACECAM_INBOX=%APPDATA%\Adobe\FacecamImport\inbox"
set "FACECAM_JSX=%~dp0import_project.jsx"
if not exist "%FACECAM_INBOX%" (
  echo Installez Facecam Import avec install_facecam_extension.bat, puis ouvrez son panneau dans Premiere Pro.
  pause
  exit /b 1
)
if not exist "%FACECAM_JSX%" (
  echo Extrayez entierement le ZIP avant de lancer ce fichier.
  pause
  exit /b 1
)
powershell -NoProfile -NonInteractive -Command "$name = 'run_' + [guid]::NewGuid().ToString('N'); $temp = Join-Path $env:FACECAM_INBOX ($name + '.tmp'); $dest = Join-Path $env:FACECAM_INBOX ($name + '.trigger'); [IO.File]::WriteAllText($temp, $env:FACECAM_JSX, (New-Object Text.UTF8Encoding($false))); [IO.File]::Move($temp, $dest)"
if errorlevel 1 (
  echo Impossible d'envoyer le script. Choisissez import_project.jsx directement dans Facecam Import.
  pause
  exit /b 1
)
echo Script envoye. Premiere Pro et le panneau Facecam Import doivent etre ouverts.
timeout /t 3 >nul
