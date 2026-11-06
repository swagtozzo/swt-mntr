@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --user --quiet cryptography pyyaml
python privato.py apri
if not errorlevel 1 start "" notepad privato.yaml
pause
