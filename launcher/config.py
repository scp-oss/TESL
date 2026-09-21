# ==================== launcher/config.py ====================
"""
Конфигурация лаунчера.
Все константы, пути, WebDAV-параметры в одном месте.
"""
import os
import sys
from pathlib import Path

# ── Версия лаунчера ───────────────────────────────────────────────────────────
LAUNCHER_VERSION = "19.0.8"
WINDOW_TITLE     = f"Skyrim MO2 Updater + Patcher {LAUNCHER_VERSION}"

# ── WebDAV ────────────────────────────────────────────────────────────────────
# Базовый URL WebDAV (Nextcloud). Учётка read-only, общая для всех клиентов —
# любой, кто соберёт .exe, может её извлечь (это ожидаемо для этой архитектуры,
# см. CLAUDE.md "Секреты и их модель угроз"). НЕ храним пароль в git — он либо
# приходит из ENV (сборка в CI), либо из launcher/secrets_local.py (untracked,
# см. secrets_local.example.py), либо оставляем пустым для разработки без сети.
try:
    from secrets_local import DAV_PASSWORD as _LOCAL_DAV_PASSWORD
except ImportError:
    _LOCAL_DAV_PASSWORD = ""

DAV_BASE_URL  = "https://nethunter.sytes.net/cloud/remote.php/dav/files/SkyrimDownloader"
DAV_USERNAME  = "SkyrimDownloader"
DAV_PASSWORD  = os.getenv("TESL_DAV_PASSWORD", _LOCAL_DAV_PASSWORD)

# Пути на сервере (относительно DAV_BASE_URL/1TB/)
DEPOT_REMOTE_PATH   = "1TB/ModOrganizer"   # основные файлы MO2-сборки
PATCHER_REMOTE_PATH = "1TB/Patcher"        # файлы патчера
SKYRIM_REMOTE_PATH  = "1TB/TESV1.6.1170.0" # лицензионная версия Skyrim
MO2_REMOTE_PATH     = "1TB/MO2p"           # отдельный Mod Organizer

# depot.json — индекс версий сборки (лежит в DEPOT_REMOTE_PATH)
DEPOT_JSON_NAME = "depot.json"

# Постер — берётся из корня DEPOT_REMOTE_PATH
POSTER_FILENAME = "poster.png"

# ── Chunk-based версии (см. core/chunk_manifest_db.py) ────────────────────────
# Некоторые версии физически хранятся как content-addressed чанки
# (chunks/<xx>/<id>) вместо плоских files/<rel_path> — так был опубликован
# минимум один реальный билд (см. TESL-Manager::CLAUDE.md "Восстановление
# сборки из чанков"). Для таких версий рядом с обычным JSON-манифестом лежит
# компаньон manifest.db (SQLite, тот же путь, расширение .db вместо .json —
# генерируется TESL-Manager'ом, depot_sync_manager/build_manifest_db.py).
# DownloadWorker сначала пробует его найти и, если он есть, качает сборку
# напрямую из chunks/, не трогая files/<rel_path> вообще; если компаньона
# нет (обычный HTTP 404) — молча работает по старому, "плоскому" протоколу.
# (путь MANIFEST_CHUNK_DB_CACHE определён ниже, после APPDATA_DIR)
CHUNK_DIR = "chunks"

# ── Старый JSON-сервер (для совместимости bootstrap'а) ────────────────────────
JSON_SERVER = "https://nethunter.sytes.net/sky/"

# ── Crash logger ──────────────────────────────────────────────────────────────
CRASH_LOG_REMOTE_PATH = "1TB/CrashLogs"   # куда шлём репорты (тот же WebDAV)

# ── Локальные пути ────────────────────────────────────────────────────────────
APPDATA_DIR      = Path(os.getenv("APPDATA") or Path.home()) / "TESVAE_Launcher"
CONFIG_FILE      = APPDATA_DIR / "config.json"
PROGRESS_FILE    = APPDATA_DIR / "progress.json"
LOG_FILE         = APPDATA_DIR / "launcher.log"
MANIFEST_CACHE   = APPDATA_DIR / "manifest.json"   # кэш манифеста с сервера
POSTER_CACHE     = APPDATA_DIR / "poster.png"
MANIFEST_CHUNK_DB_CACHE = APPDATA_DIR / "manifest_chunk_cache.db"   # см. CHUNK_DIR выше

APPDATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Skyrim AE ─────────────────────────────────────────────────────────────────
SKYRIM_TARGET_VERSION = "1.6.1170.0"
SKYRIM_EXE_NAME       = "SkyrimSE.exe"

# Обязательные файлы дополнений (AE DLC)
SKYRIM_AE_DLC_FILES = [
    "ccafdsse001-dwesanctuary.bsa",
    "ccafdsse001-dwesanctuary.esm",
    "ccasvsse001-almsivi.bsa",
    "ccasvsse001-almsivi.esm",
    "ccbgssse001-fish.bsa",
    "ccbgssse001-fish.esm",
    "ccbgssse002-exoticarrows.bsa",
    "ccbgssse002-exoticarrows.esl",
    "ccbgssse003-zombies.bsa",
    "ccbgssse003-zombies.esl",
    "ccbgssse004-ruinsedge.bsa",
    "ccbgssse004-ruinsedge.esl",
    "ccbgssse005-goldbrand.bsa",
    "ccbgssse005-goldbrand.esl",
    "ccbgssse006-stendarshammer.bsa",
    "ccbgssse006-stendarshammer.esl",
    "ccbgssse007-chrysamere.bsa",
    "ccbgssse007-chrysamere.esl",
    "ccbgssse008-wraithguard.bsa",
    "ccbgssse008-wraithguard.esl",
    "ccbgssse010-petdwarvenarmoredmudcrab.bsa",
    "ccbgssse010-petdwarvenarmoredmudcrab.esl",
    "ccbgssse016-umbra.bsa",
    "ccbgssse016-umbra.esm",
    "ccbgssse025-advdsgs.bsa",
    "ccbgssse025-advdsgs.esm",
    "ccbgssse031-advcyrus.bsa",
    "ccbgssse031-advcyrus.esm",
    "ccbgssse037-curios.bsa",
    "ccbgssse037-curios.esl",
    "ccbgssse067-daedinv.bsa",
    "ccbgssse067-daedinv.esm",
    "cceejsse001-hstead.bsa",
    "cceejsse001-hstead.esm",
    "ccmtysse001-knightsofthenine.bsa",
    "ccmtysse001-knightsofthenine.esl",
    "ccqdrsse001-survivalmode.bsa",
    "ccqdrsse001-survivalmode.esl",
    "cctwbsse001-puzzledungeon.bsa",
    "cctwbsse001-puzzledungeon.esm",
    "ccvsvsse001-winter.bsa",
    "ccvsvsse001-winter.esl",
]
# Неполный список — достаточно для проверки AE-версии.
# Полный список из 100+ файлов можно добавить при необходимости.

# ── ModOrganizer ──────────────────────────────────────────────────────────────
MO2_EXE           = "ModOrganizer.exe"
MO2_INI           = "ModOrganizer.ini"
MO2_SHORTCUT_NAME = "TESVAE"
MO2_SKSE_ARG      = "moshortcut://:SKSE"

# Путь в Skyrim который прописан в шаблоне ModOrganizer.ini (заменяем при установке)
MO2_INI_SKYRIM_PLACEHOLDER_FWD = "D:/SteamLibrary/steamapps/common/Skyrim Special Edition"
MO2_INI_SKYRIM_PLACEHOLDER_DBL = "D:\\\\SteamLibrary\\\\steamapps\\\\common\\\\Skyrim Special Edition"

# ── Worker ────────────────────────────────────────────────────────────────────
MAX_WORKERS = min(32, max(8, (os.cpu_count() or 4) * 2))
BATCH_SIZE  = 500

# ── Assets ────────────────────────────────────────────────────────────────────
def get_asset_path(name: str) -> Path:
    """Путь к bundled-ассету (работает и в .py и в PyInstaller exe)"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name
