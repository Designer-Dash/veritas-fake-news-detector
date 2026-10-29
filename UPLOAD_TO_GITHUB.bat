@echo off
title Veritas — Upload / Push to GitHub
color 0B
cd /d "%~dp0"

echo ============================================================
echo   🛡️  Veritas — GitHub Upload Helper
echo ============================================================
echo.
echo Pushing local repository to GitHub...
echo Target: https://github.com/Designer-Dash/veritas-fake-news-detector.git
echo.

git branch -M main
git push -u origin main

if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo   ✅ SUCCESS! Your project is now live on GitHub:
    echo   https://github.com/Designer-Dash/veritas-fake-news-detector
    echo ============================================================
) else (
    echo.
    echo ============================================================
    echo   ⚠️ Repository not found or access denied.
    echo   If you haven't created the repository on GitHub yet,
    echo   please create it first at:
    echo   https://github.com/new?name=veritas-fake-news-detector
    echo   Then run this script again!
    echo ============================================================
)

echo.
pause
