@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "sync_docs_index.py" --once
timeout /t 2 >nul
