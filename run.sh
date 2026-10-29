#!/usr/bin/env bash
# Veritas Fake News Detector Server Runner

# Activate virtualenv if present
if [ -f "backend/.venv/bin/activate" ]; then
    source backend/.venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

python run.py
