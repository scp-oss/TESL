@echo off
setlocal enabledelayedexpansion
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

if defined TESL_DAV_PASSWORD (
    echo WebDAV password already set via environment variable.
) else if exist "secrets_local.py" (
    echo Using launcher\secrets_local.py.
) else (
    echo.
    echo WebDAV password not found ^(no env var, no secrets_local.py^).
    set /p TESL_DAV_PASSWORD="Enter WebDAV password: "
    setx TESL_DAV_PASSWORD "!TESL_DAV_PASSWORD!" >nul
    echo Saved for this Windows user - won't ask again next time.
)

echo.
echo Starting launcher...
echo.
python main.py

echo.
pause
