@echo off
title Veritas Fake News Detector Server
echo Starting Veritas Backend Server...

REM Check for virtual environment in backend\.venv or .venv
if exist "backend\.venv\Scripts\activate.bat" (
    call backend\.venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

python run.py
pause
