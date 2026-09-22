@echo off
cd /d "%~dp0"

echo === TESL: update from git + launch ===
echo.

git pull
if errorlevel 1 (
    echo.
    echo git pull failed - see output above. Launch cancelled.
    pause
    exit /b 1
)

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
