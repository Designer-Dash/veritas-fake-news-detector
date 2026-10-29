@echo off
title Veritas — Backend API Server (Port 8000)
color 0B
cd /d "%~dp0"

echo ============================================================
echo   🛡️  Veritas — Starting Backend API Server
echo ============================================================
echo.

REM Activate virtual environment if present
if exist "backend\.venv\Scripts\activate.bat" (
    echo [*] Activating backend\.venv virtual environment...
    call backend\.venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    echo [*] Activating .venv virtual environment...
    call .venv\Scripts\activate.bat
)

echo [*] Launching FastAPI backend on http://127.0.0.1:8000 ...
python run.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Server exited with an error code: %ERRORLEVEL%
    pause
)
