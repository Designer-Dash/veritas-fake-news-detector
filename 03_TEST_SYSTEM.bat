@echo off
title Veritas — Test Suite Runner
color 0F
cd /d "%~dp0"

echo ============================================================
echo   🛡️  Veritas — Running Full Test Suite (pytest)
echo ============================================================
echo.

if exist "backend\.venv\Scripts\activate.bat" (
    call backend\.venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

cd backend
python -m pytest -v --tb=short
cd ..

echo.
echo ============================================================
echo   Test run finished.
echo ============================================================
pause
