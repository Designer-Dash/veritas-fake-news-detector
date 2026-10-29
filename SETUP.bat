@echo off
title Veritas — 1-Click Environment Setup for Collaborators
color 0B
cd /d "%~dp0"

echo ============================================================
echo   🛡️  Veritas — 1-Click Developer Setup
echo ============================================================
echo.
echo Setting up Python environment and dependencies for editing...
echo.

REM 1. Check Python installation
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [X] Python was not found on your system PATH!
    echo Please install Python 3.10+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [1/3] Python detected:
python --version

REM 2. Create virtual environment if not already present
if not exist "backend\.venv" (
    echo.
    echo [2/3] Creating isolated Python virtual environment in backend\.venv...
    python -m venv backend\.venv
) else (
    echo.
    echo [2/3] Existing virtual environment found in backend\.venv.
)

REM 3. Activate venv and install dependencies
call backend\.venv\Scripts\activate.bat
echo.
echo [3/3] Installing and updating required dependencies...
python -m pip install --upgrade pip
pip install -r backend/requirements-dev.txt

if %ERRORLEVEL% equ 0 (
    echo.
    echo ============================================================
    echo   ✅ SETUP COMPLETE! Everything is ready for editing.
    echo ============================================================
    echo.
    echo  Quick Start Options:
    echo   • Run 01_START_SERVER.bat   -> Starts the backend engine
    echo   • Run 02_LOAD_EXTENSION.bat -> Loads Chrome extension
    echo   • Run 03_TEST_SYSTEM.bat    -> Runs all test suites
    echo.
) else (
    echo.
    echo [X] An error occurred while installing dependencies.
    echo Please check your internet connection and try running this script again.
)

echo.
pause
