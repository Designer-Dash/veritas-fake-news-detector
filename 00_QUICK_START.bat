@echo off
title Veritas — Master Control Center
color 0B
:menu
cls
cd /d "%~dp0"
echo ===================================================================
echo   🛡️  VERITAS — FAKE ^& AI-GENERATED NEWS DETECTOR
echo ===================================================================
echo.
echo   Select an action to launch:
echo.
echo   [0] Run 1-Click Environment Setup  (Create venv ^& install deps)
echo   [1] Start Backend Server           (Port 8000)
echo   [2] Load Chrome Extension Guide    (1-Click Helper ^& Clipboard)
echo   [3] Run Complete Test Suite        (pytest)
echo   [4] Open Local Test Web Page
echo   [5] Open Swagger API Documentation (http://localhost:8000/docs)
echo   [6] Exit
echo.
echo ===================================================================
set /p choice="Enter choice [0-6]: "

if "%choice%"=="0" (
    call "SETUP.bat"
    goto menu
)
if "%choice%"=="1" (
    start "Veritas Backend" cmd /c "01_START_SERVER.bat"
    goto menu
)
if "%choice%"=="2" (
    call "02_LOAD_EXTENSION.bat"
    goto menu
)
if "%choice%"=="3" (
    call "03_TEST_SYSTEM.bat"
    goto menu
)
if "%choice%"=="4" (
    start "" "%~dp0webapp\privacy.html"
    goto menu
)
if "%choice%"=="5" (
    start "" "http://127.0.0.1:8000/docs"
    goto menu
)
if "%choice%"=="6" (
    exit /b 0
)

echo Invalid choice. Please try again.
timeout /t 1 >nul
goto menu
