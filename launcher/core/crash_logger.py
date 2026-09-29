# ==================== launcher/core/crash_logger.py ====================
"""
CrashLogger — собирает крэш-логи Skyrim и лог отладки лаунчера, отправляет
на TESL-Panel (`PUT /api/reports/<report_type>/<username>/<timestamp>/
<filename>`, см. её CLAUDE.md "Разделы Отчёты и Дашборд"/
"поправь отправку логов в панель").

**2026-09-29: переезд с WebDAV на панель** (было — WebDAV MKCOL+PUT,
`CRASH_LOG_REMOTE_PATH`/`DEBUG_LOG_REMOTE_PATH` в config.py, обе убраны).
Живой повод: `MKCOL .../Staticfolders/DEBUG_Log/ → 403` — общий
read-ориентированный WebDAV-аккаунт лаунчера (`DAV_USERNAME`) не имеет
прав создавать НОВЫЕ папки в этой ветке Nextcloud (в отличие от
`CRASH_Log`, созданной вручную задолго до этого движка) — серверные
права, не чинится в клиентском коде. Панель уже держала готовый,
специально под это ждущий эндпоинт (`"crash"`/`"debug_log"` — публичен,
без Bearer, см. TESL-Panel::app.py::api_reports_upload за обоснование:
токен депо в публичном .exe был бы катастрофой, отчёт — нет), так что
переезд заодно СИЛЬНО упростил сам код — `storage.put_bytes()` на
панели сам создаёт родительские папки при каждом PUT, никакого
MKCOL-танца (`_make_dir`/`_make_dir_recursive`, обе удалены) больше не
нужно.
"""
import os
import pathlib
from datetime import datetime
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

import requests

import config as _config


# ── File collection ───────────────────────────────────────────────────────────

def _latest_file(folder: str, pattern: str) -> Optional[pathlib.Path]:
    """Самый свежий файл по паттерну glob."""
    p = pathlib.Path(os.path.expandvars(folder))
    if not p.exists():
        return None
    files = list(p.glob(pattern))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def collect_files(skyrim_dir: str, log: Callable[[str], None] = print) -> List[pathlib.Path]:
    """
    Собирает файлы для отправки:
      • SKSE/crash-*.log
      • Logs/Script/Papyrus.0.log
      • Saves/*.ess  (последнее сохранение)
      • Saves/*.skse* (SKSE co-save)
    """
    base = pathlib.Path(skyrim_dir)
    if not base.exists():
        log(f"❌ Папка игры не найдена: {skyrim_dir}")
        return []

    # Skyrim хранит логи в Documents/My Games/Skyrim Special Edition
    # если base указывает на папку игры — ищем рядом
    docs_skyrim = (
        pathlib.Path(os.path.expanduser("~"))
        / "Documents" / "My Games" / "Skyrim Special Edition"
    )

    candidates = [
        _latest_file(str(base / "SKSE"),       "crash-*.log"),
        _latest_file(str(docs_skyrim / "SKSE"), "crash-*.log"),
        _latest_file(str(docs_skyrim / "Logs" / "Script"), "Papyrus.0.log"),
        _latest_file(str(docs_skyrim / "Saves"), "*.ess"),
        _latest_file(str(docs_skyrim / "Saves"), "*.skse*"),
    ]

    result = [f for f in candidates if f is not None]
    if not result:
        log("⚠️ Файлы логов/сохранений не найдены")
    else:
        log(f"📁 Найдено файлов для отправки: {len(result)}")
    return result


# ── Upload (TESL-Panel) ──────────────────────────────────────────────────────

def upload_files(
    username:    str,
    files:       List[pathlib.Path],
    log:         Callable[[str], None] = print,
    report_type: str = "crash",
) -> Tuple[bool, str]:
    """
    PUT каждого файла по отдельности на
      {PANEL_BASE_URL}/api/reports/<report_type>/<username>/<timestamp>/<filename>
    (публичный эндпоинт, без токена — см. модульный докстринг). Один общий
    `timestamp` на весь вызов (не на файл) — та же структура
    `<username>/<timestamp>/` с несколькими файлами внутри, что была на
    WebDAV, сохранена намеренно: `/admin/reports` на панели уже рассчитан
    ровно на неё.
    """
    session = requests.Session()
    timestamp = datetime.now().strftime("%m.%d.%Y-%H.%M.%S")
    base = _config.PANEL_BASE_URL.rstrip("/")
    folder_url = f"{base}/api/reports/{report_type}/{quote(username, safe='')}/{timestamp}/"

    uploaded = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                url = folder_url + quote(f.name)
                r = session.put(url, data=fh, timeout=60)
            if r.status_code in (200, 201, 204):
                log(f"✅ Загружен: {f.name}")
                uploaded.append(f.name)
            else:
                log(f"❌ Ошибка {r.status_code} при загрузке {f.name}")
        except Exception as e:
            log(f"❌ {f.name}: {e}")
    session.close()

    if uploaded:
        return True, f"Отправлено {len(uploaded)} файлов:\n" + "\n".join(uploaded)
    return False, "Ни один файл не был загружен"
