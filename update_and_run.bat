@echo off
cd /d "%~dp0"

echo === TESL: update from git + launch ===
echo.

git pull

rem Not gating on errorlevel here: git pull's exit code has been observed
rem to occasionally be nonzero even on a successful fast-forward (a known
rem quirk on some Windows git installs, e.g. pager-related). The pull's
rem own output above is the real signal - if it printed "error:"/"fatal:",
rem something's actually wrong; otherwise just proceed.

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
