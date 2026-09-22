# ==================== launcher/core/crash_logger.py ====================
"""
CrashLogger — собирает крэш-логи Skyrim и отправляет на WebDAV.
Адаптирован из carash_logger.py под depot-систему.
"""
import os
import pathlib
from datetime import datetime
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth

import config as _config   # для DAV_PASSWORD — см. upload_files()
from config import (
    DAV_BASE_URL, DAV_USERNAME,
    CRASH_LOG_REMOTE_PATH,
)


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


# ── Upload ────────────────────────────────────────────────────────────────────

def _make_dir(session: requests.Session, url: str, log):
    r = session.request("MKCOL", url, timeout=15)
    if r.status_code not in (200, 201, 204, 405):
        raise RuntimeError(f"MKCOL {url} → {r.status_code}")


def _make_dir_recursive(session: requests.Session, base_url: str, path_segments, log):
    """
    MKCOL каждого сегмента пути по очереди, от корня вниз — не только
    последней папки. Живой баг 2026-09-22: DEBUG_LOG_REMOTE_PATH
    ("Staticfolders/DEBUG_Log") — новая папка, которую этот код никогда
    заранее не создавал (в отличие от CRASH_LOG_REMOTE_PATH, чья папка на
    сервере уже существовала до этой фичи) — старый _make_dir() вызывался
    только на `<remote_path>/<username>/`, молча ПРЕДПОЛАГАЯ, что
    <remote_path> САМ по себе уже существует. WebDAV MKCOL требует, чтобы
    ВСЕ промежуточные коллекции уже существовали (RFC 4918) — если нет,
    возвращает 409, а не создаёт их автоматически (не "mkdir -p"). Итог:
    первая попытка отправить лог отладки на новую, ещё не созданную вручную
    на сервере папку стабильно проваливалась с 409, и без ручного создания
    папки оператором никогда бы не заработала сама. MKCOL на уже
    существующую коллекцию возвращает 405 (обрабатывается как успех в
    _make_dir), так что повторные вызовы для уже существующих сегментов
    (как у CRASH_Log) безопасны и дёшевы.
    """
    url = base_url.rstrip("/")
    for seg in path_segments:
        url = f"{url}/{seg}"
        _make_dir(session, url + "/", log)


def upload_files(
    username:    str,
    files:       List[pathlib.Path],
    log:         Callable[[str], None] = print,
    server_url:  str = DAV_BASE_URL,
    remote_path: str = CRASH_LOG_REMOTE_PATH,
    dav_user:    str = DAV_USERNAME,
    dav_pass:    Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Загружает файлы на WebDAV в структуру:
      <remote_path>/<username>/<timestamp>/
    Возвращает (success, message).
    """
    session = requests.Session()
    # dav_pass=None -> config.DAV_PASSWORD живьём на момент вызова, не на
    # момент импорта (см. depot_client.py::DepotClient.__init__ — та же правка).
    effective_pass = dav_pass if dav_pass is not None else _config.DAV_PASSWORD
    session.auth = HTTPBasicAuth(dav_user, effective_pass)
    session.verify = True

    timestamp  = datetime.now().strftime("%m.%d.%Y-%H.%M.%S")
    base_url   = server_url.rstrip("/")
    rp         = remote_path.strip("/")
    user_url   = f"{base_url}/{rp}/{username}/"
    folder_url = f"{user_url}{timestamp}/"

    try:
        # Каждый сегмент по очереди (remote_path -> username -> timestamp),
        # не только последние два — см. _make_dir_recursive() за живой
        # 409-баг, который это чинит. rp может содержать несколько
        # сегментов сам по себе (напр. "1TB/TESS/Staticfolders/DEBUG_Log").
        _make_dir_recursive(session, base_url, rp.split("/") + [username, timestamp], log)
    except RuntimeError as e:
        log(f"❌ {e}")
        return False, str(e)

    uploaded = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                url = folder_url + quote(f.name)
                r   = session.put(url, data=fh, timeout=60)
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
