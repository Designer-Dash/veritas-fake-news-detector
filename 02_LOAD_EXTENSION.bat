@echo off
title Veritas — Chrome Extension Setup & Loader
color 0A
cd /d "%~dp0"

set "EXT_DIR=%~dp0extension"
REM Remove trailing backslash if present
if "%EXT_DIR:~-1%"=="\" set "EXT_DIR=%EXT_DIR:~0,-1%"

REM Copy extension directory path to Windows clipboard
echo %EXT_DIR%| clip

echo ============================================================
echo   🛡️  Veritas Chrome Extension — Quick Loader
echo ============================================================
echo.
echo  [+] Extension folder path copied to your clipboard:
echo      "%EXT_DIR%"
echo.
echo  [+] Opening Google Chrome to the Extensions Manager...
echo.
start "" chrome "chrome://extensions" 2>nul || start "" "chrome://extensions" 2>nul

echo ============================================================
echo   📋 QUICK 3-STEP SETUP IN CHROME:
echo ============================================================
echo   1. In Chrome, switch [Developer mode] ON (top-right toggle).
echo   2. Click the [Load unpacked] button (top-left).
echo   3. Press Ctrl + V in the folder dialog to paste the path,
echo      then click [Select Folder].
echo ============================================================
echo.
echo  ✅ Veritas Extension will appear with icon and live badge!
echo     Now browse any news site (or open webapp/privacy.html)
echo     to see the real-time AI verification banner in action.
echo.
echo Press any key to open the local Veritas test web page...
pause >nul

start "" "%~dp0webapp\privacy.html"
exit /b 0
