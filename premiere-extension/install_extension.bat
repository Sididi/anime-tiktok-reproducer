@echo off
setlocal

echo ============================================
echo   JSX Runner - CEP Extension Installer
echo   For Adobe Premiere Pro 2025
echo ============================================
echo.

:: Target directories
set "EXT_DIR=%APPDATA%\Adobe\CEP\extensions\jsx-runner"
set "INBOX_DIR=%APPDATA%\Adobe\JSXRunner\inbox"
set "SOURCE_DIR=%~dp0jsx-runner"

:: Check source exists
if not exist "%SOURCE_DIR%\CSXS\manifest.xml" (
    echo ERROR: Cannot find jsx-runner folder next to this script.
    echo Make sure install_extension.bat is in the premiere-extension folder.
    pause
    exit /b 1
)

:: Step 1: Copy extension files
echo [1/3] Installing extension to:
echo       %EXT_DIR%
if exist "%EXT_DIR%" (
    echo       Removing previous installation...
    rmdir /s /q "%EXT_DIR%"
)
xcopy "%SOURCE_DIR%" "%EXT_DIR%" /e /i /q >nul
if errorlevel 1 (
    echo ERROR: Failed to copy extension files.
    pause
    exit /b 1
)
echo       Done.
echo.

:: Step 2: Enable unsigned extensions (PlayerDebugMode)
echo [2/3] Enabling unsigned extensions...
reg add "HKCU\Software\Adobe\CSXS.12" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
if errorlevel 1 (
    echo WARNING: Could not set registry key. You may need to run as administrator.
) else (
    echo       Done.
)
echo.

:: Step 3: Create hot folder
echo [3/3] Creating hot folder:
echo       %INBOX_DIR%
if not exist "%INBOX_DIR%" mkdir "%INBOX_DIR%"
echo       Done.
echo.

echo ============================================
echo   Installation complete!
echo.
echo   Next steps:
echo   1. Restart Adobe Premiere Pro 2025
echo   2. Go to Window ^> Extensions ^> JSX Runner
echo   3. The panel should appear with a green dot
echo ============================================
echo.
pause
