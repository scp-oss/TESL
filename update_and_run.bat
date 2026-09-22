@echo off
cd /d "%~dp0"

echo === TESL: update from git + launch ===
echo.

rem Hard-sync to origin/main instead of `git pull` - guarantees this is
rem EXACTLY what's on GitHub, no ambiguity from fast-forward/errorlevel
rem quirks. This discards any local changes to tracked files (not
rem untracked ones like launcher/secrets_local.py) - fine for a checkout
rem that's only ever meant to run the app, not to develop on.
git fetch origin main
git reset --hard origin/main

echo.
echo Now running commit:
git log -1 --oneline
echo.

cd launcher

rem WebDAV password: env var / secrets_local.py / cached APPDATA file are all
rem checked automatically by the launcher itself. If none of them has it, the
rem launcher shows its own password dialog (hidden input) on first run and
rem saves it - nothing to do here.

echo.
echo Starting launcher...
echo.
python main.py

echo.
pause
