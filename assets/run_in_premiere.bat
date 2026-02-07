@echo off
setlocal

set "INBOX=%APPDATA%\Adobe\JSXRunner\inbox"

:: Check if the JSX Runner extension is installed
if not exist "%INBOX%" (
    echo.
    echo ERROR: JSX Runner extension is not installed.
    echo.
    echo Please install it first:
    echo   1. Get the premiere-extension folder from the project repository
    echo   2. Double-click install_extension.bat
    echo   3. Restart Premiere Pro 2025
    echo.
    pause
    exit /b 1
)

:: Write the absolute path of import_project.jsx to a trigger file
echo %~dp0import_project.jsx> "%INBOX%\run_%RANDOM%.trigger"

echo.
echo Script sent to Premiere Pro.
echo Check the JSX Runner panel (Window ^> Extensions ^> JSX Runner) for status.
echo.
echo NOTE: Premiere Pro must be running with the JSX Runner panel loaded.
echo.
timeout /t 3
