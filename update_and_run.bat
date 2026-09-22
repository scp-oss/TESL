@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo === TESL: обновление из git + запуск ===
echo.

git pull
if errorlevel 1 (
    echo.
    echo Ошибка git pull — смотри вывод выше. Запуск отменён.
    pause
    exit /b 1
)

cd launcher

if defined TESL_DAV_PASSWORD (
    echo Пароль WebDAV уже задан в переменной окружения.
) else if exist "secrets_local.py" (
    echo Использую launcher\secrets_local.py.
) else (
    echo.
    echo Пароль WebDAV не найден ^(ни в переменной окружения, ни в secrets_local.py^).
    set /p TESL_DAV_PASSWORD="Введи пароль WebDAV: "
    setx TESL_DAV_PASSWORD "!TESL_DAV_PASSWORD!" >nul
    echo Сохранено для этого пользователя Windows — в следующий раз спрашивать не будет.
)

echo.
echo Запускаем лаунчер...
echo.
python main.py

echo.
pause
