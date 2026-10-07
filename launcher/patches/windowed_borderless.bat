@echo off
setlocal

:: ============================================================================
:: windowed_borderless.bat
::
:: Переводит уже записанный SkyrimPrefs.ini (Documents\My Games\Skyrim
:: Special Edition\) в безрамочный оконный режим: bBorderless=1,
:: bFull Screen=0.
::
:: Прямой запрос пользователя 2026-10-07: "пути мо можно оставить в
:: лаунчере а вот оконный режим в патч" — продолжение более раннего "фиксы
:: ты писал под эту сборку, возможно я залью другую, и они применятся к
:: ней". В отличие от MO2-путей/отключения диалога смены игры (те
:: одинаково верны для ЛЮБОЙ MO2+Skyrim сборки и остаются в
:: core/patcher.py лаунчера без изменений), безрамочный оконный режим —
:: НЕ универсальное решение: он нужен только там, где это явно требуется
:: (DLSS5 и другие апскейлеры конфликтуют с эксклюзивным fullscreen-flip,
:: см. core/ini_profile.py), обычный игрок без DLSS5 предпочтёт обычный
:: fullscreen. Поэтому это отдельный патч под КОНКРЕТНУЮ сборку,
:: прикрепляемый через TESL-Manager -> "📄 Документы и патчи" -> "Патчи"
:: (см. его собственный докстринг в depot_sync_manager/documents_tab.py
:: за формат extras_manifest.json/enabled/order), а не жёстко зашитое
:: значение в generic launcher'е.
::
:: core/ini_profile.py::COMMON_VALUES держит обычный fullscreen по
:: умолчанию (bFull Screen=1 / bBorderless=0) — любая сборка без этого
:: патча получает стандартный vanilla-режим, не оконный.
::
:: Запускается лаунчером (core/patch_runner.py, раздел "Патчи") ПОСЛЕ
:: того, как Skyrim.ini/SkyrimPrefs.ini уже записаны
:: (core/ini_profile.py::apply_ini_profile()) — правит уже готовый файл,
:: не создаёт его с нуля. core/patch_runner.py сам отслеживает
:: применённые патчи по (путь, sha256) и не станет перезапускать этот
:: скрипт повторно для одной и той же установки, пока его содержимое не
:: изменится — но сам скрипт ТОЖЕ идемпотентен (безопасно запускать
:: вручную много раз подряд: если значения уже верные, файл не трогается
:: и бэкап не плодится заново).
:: ============================================================================

set "PREFS_INI=%USERPROFILE%\Documents\My Games\Skyrim Special Edition\SkyrimPrefs.ini"

if not exist "%PREFS_INI%" (
    echo [windowed_borderless] SkyrimPrefs.ini не найден: "%PREFS_INI%" - пропуск.
    exit /b 0
)

if not exist "%PREFS_INI%.bak_windowed" (
    copy /y "%PREFS_INI%" "%PREFS_INI%.bak_windowed" >nul 2>&1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = $env:PREFS_INI; $enc = New-Object System.Text.UTF8Encoding($false); $lines = [System.IO.File]::ReadAllLines($p, [System.Text.Encoding]::UTF8); $changed = $false; for ($i = 0; $i -lt $lines.Length; $i++) { if ($lines[$i] -match '(?i)^bFull\s*Screen\s*=') { if ($lines[$i] -ne 'bFull Screen=0') { $lines[$i] = 'bFull Screen=0'; $changed = $true } } elseif ($lines[$i] -match '(?i)^bBorderless\s*=') { if ($lines[$i] -ne 'bBorderless=1') { $lines[$i] = 'bBorderless=1'; $changed = $true } } }; if ($changed) { [System.IO.File]::WriteAllLines($p, $lines, $enc); Write-Output 'changed' } else { Write-Output 'already-applied' }; exit 0 } catch { Write-Error $_; exit 1 }"

if errorlevel 1 (
    echo [windowed_borderless] Ошибка PowerShell-шага - SkyrimPrefs.ini не тронут.
    exit /b 1
)

echo [windowed_borderless] bFull Screen=0 / bBorderless=1 применены к SkyrimPrefs.ini.
exit /b 0
