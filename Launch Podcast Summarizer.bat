@echo off
cd /d "%~dp0"
REM --server.fileWatcherType poll is local-only on purpose.
REM
REM This project lives on a Google Drive virtual drive, which does not emit
REM the filesystem events Streamlit's default watcher listens for. Without
REM polling, an edited module stays cached in sys.modules and the server has
REM to be killed by hand after every change to ui.py, stores.py and friends.
REM
REM It is NOT in .streamlit/config.toml, because that file ships to the
REM deployed app, where module hot-reloading crashes Python 3.14's
REM dataclasses (see the comment in that file).
".venv\Scripts\streamlit.exe" run app.py --server.fileWatcherType poll
