@echo off
setlocal DisableDelayedExpansion
set "FACECAM_SOURCE=%~dp0facecam-import"
set "FACECAM_DEST=%APPDATA%\Adobe\CEP\extensions\facecam-import"
if not exist "%FACECAM_SOURCE%\CSXS\manifest.xml" (
  echo Extension Facecam Import introuvable a cote de cet installateur.
  pause
  exit /b 1
)
if exist "%FACECAM_DEST%" rmdir /s /q "%FACECAM_DEST%"
if exist "%FACECAM_DEST%" (
  echo Fermez Premiere Pro et Media Encoder, puis relancez cet installateur.
  pause
  exit /b 1
)
xcopy "%FACECAM_SOURCE%" "%FACECAM_DEST%" /e /i /q /y >nul
if errorlevel 1 exit /b 1
reg add "HKCU\Software\Adobe\CSXS.12" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul
if not exist "%APPDATA%\Adobe\FacecamImport\inbox" mkdir "%APPDATA%\Adobe\FacecamImport\inbox"
echo Installation terminee. Ouvrez Premiere Pro, puis Fenetre - Extensions - Facecam Import.
pause
