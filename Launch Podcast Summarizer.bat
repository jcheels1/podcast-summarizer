@echo off
cd /d "%~dp0"
REM File watching is configured in .streamlit/config.toml (polling, because
REM Google Drive emits no filesystem events). Nothing to pass here.
".venv\Scripts\streamlit.exe" run app.py
