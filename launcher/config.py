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
# WINDOW_TITLE больше НЕ статическая константа (была
# "Skyrim MO2 Updater + Patcher {LAUNCHER_VERSION}") — прямой запрос
# пользователя 2026-09-22: формат заголовка окна "TESL [commit luncher
# vers]", то есть с git-хэшем ТЕКУЩЕГО чекаута, который можно узнать
# только вызовом get_launcher_commit() (subprocess) — та функция
# определена ниже по файлу, поэтому это функция, вычисляемая при
# КАЖДОМ вызове (дёшево — тот же subprocess, что уже вызывается для
# метки версии в правом верхнем углу, ui/main_window.py::
# _format_version_label()), а не константа времени импорта модуля.

# ── Локальные пути (перенесено сюда, наверх — нужны для DAV_PASSWORD_CACHE_FILE
# ниже, до того как остальной APPDATA_DIR-блок появляется дальше по файлу) ─────
APPDATA_DIR = Path(os.getenv("APPDATA") or Path.home()) / "TESVAE_Launcher"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

# Имя текущей сборки — сейчас всегда "TESVAE" (тот же смысл, что и
# MO2_SHORTCUT_NAME в секции "ModOrganizer" ниже, просто нужен раньше по
# файлу; если однажды поменяется одно — свериться со вторым). Прямой запрос
# пользователя, 2026-09-22: контентные кэши (постер/манифест/иконка ярлыка/
# chunk-манифест — то, что реально принадлежит КОНКРЕТНОЙ сборке) должны
# жить в своей подпапке, а не вперемешку в одном APPDATA_DIR — когда
# появится больше одной сборки (см. CLAUDE.md/README "Мультисборочность",
# план, ещё не начат), у каждой будут свои постер/манифест/etc., и без
# такого разделения кэш одной сборки перетирал бы кэш другой. Настройки
# самого лаунчера (CONFIG_FILE/PROGRESS_FILE/LOG_FILE/DAV_PASSWORD_CACHE_FILE
# выше) — общие для всех сборок, специально остаются в APPDATA_DIR напрямую.
BUILD_NAME     = "TESVAE"
BUILD_DATA_DIR = APPDATA_DIR / BUILD_NAME
BUILD_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── WebDAV ────────────────────────────────────────────────────────────────────
# Базовый URL WebDAV (Nextcloud). Учётка read-only, общая для всех клиентов —
# любой, кто соберёт .exe, может её извлечь (это ожидаемо для этой архитектуры,
# см. CLAUDE.md "Секреты и их модель угроз"). НЕ храним пароль в git — приоритет
# источников: ENV (сборка в CI) -> launcher/secrets_local.py (untracked, см.
# secrets_local.example.py, для разработки) -> локальный кэш-файл в APPDATA
# (пишется, когда пользователь один раз вводит пароль в самом лаунчере — см.
# main.py::_ensure_dav_password() — тот же принцип "не секрет в строгом
# смысле", что и сам факт, что пароль всё равно зашит в каждый собранный .exe).
DAV_PASSWORD_CACHE_FILE = APPDATA_DIR / "dav_password.txt"

try:
    from secrets_local import DAV_PASSWORD as _LOCAL_DAV_PASSWORD
except ImportError:
    _LOCAL_DAV_PASSWORD = ""

_CACHED_DAV_PASSWORD = ""
try:
    if DAV_PASSWORD_CACHE_FILE.exists():
        _CACHED_DAV_PASSWORD = DAV_PASSWORD_CACHE_FILE.read_text(encoding="utf-8").strip()
except Exception:
    pass

DAV_BASE_URL  = "https://nethunter.sytes.net/cloud/remote.php/dav/files/SkyrimDownloader"
DAV_USERNAME  = "SkyrimDownloader"
DAV_PASSWORD  = os.getenv("TESL_DAV_PASSWORD") or _LOCAL_DAV_PASSWORD or _CACHED_DAV_PASSWORD

# Пути на сервере (относительно DAV_BASE_URL).
# DEPOT_REMOTE_PATH подтверждён живым тестом 2026-09-21 (curl -I на
# .../1TB/TESS/Instances/TESVAE/versions/v1_20eb01df.db -> 200 OK) — старое
# значение "1TB/ModOrganizer" было устаревшей заглушкой, из-за которой
# fetch_depot_index() стабильно бил в несуществующий путь (404 -> None ->
# "Не удалось загрузить depot.json"). PATCHER_REMOTE_PATH/SKYRIM_REMOTE_PATH/
# MO2_REMOTE_PATH пока НЕ подтверждены тем же способом — см. CLAUDE.md
# "Известные несостыковки", не полагаться на патчер/крэш-репортер в проде,
# пока их тоже не сверят с реальным сервером.
DEPOT_REMOTE_PATH   = "1TB/TESS/Instances/TESVAE"  # основные файлы MO2-сборки
PATCHER_REMOTE_PATH = "1TB/Patcher"        # файлы патчера (НЕ подтверждено)
SKYRIM_REMOTE_PATH  = "1TB/TESV1.6.1170.0" # лицензионная версия Skyrim (НЕ подтверждено)
MO2_REMOTE_PATH     = "1TB/MO2p"           # отдельный Mod Organizer (НЕ подтверждено)

# Реестр доступных сборок для карусели на старте (см. ui/carousel_window.py,
# core/builds.py) — [{"name","label","remote_path",...}, ...] в JSON, лежит
# на уровень выше DEPOT_REMOTE_PATH (рядом с TESVAE, а не внутри неё).
# НЕ ПОДТВЕРЖДЕНО существование такого файла на реальном сервере — тот же
# класс "путь ещё не сверен с реальным листингом", что раньше был у
# DEPOT_REMOTE_PATH/CRASH_LOG_REMOTE_PATH до их подтверждения (см. CLAUDE.md).
# depot_client.py::fetch_builds_registry() трактует 404/любую ошибку как
# штатный случай (реестр ещё не выложен) и откатывается на единственную
# сборку, зашитую константами этого файла — карусель поэтому работает уже
# сегодня, с одной плиткой, и подхватит реальный список сам, как только
# builds.json появится на сервере по этому пути.
BUILDS_REGISTRY_PATH = "1TB/TESS/Instances/builds.json"

# depot.json — индекс версий сборки (лежит в DEPOT_REMOTE_PATH)
DEPOT_JSON_NAME = "depot.json"

# Постер/иконка ярлыка/аргумент ярлыка — подтверждено 2026-09-21 (пользователь
# прислал реальный листинг): все трое лежат в ОДНОЙ подпапке "src/" внутри
# DEPOT_REMOTE_PATH (т.е. <DEPOT_REMOTE_PATH>/src/...), а не в отдельной ветке
# "Profils" — более раннее предположение (POSTER_REMOTE_PATH отдельно от
# remote_path) было неверным, убрано.
BUILD_ASSETS_SUBDIR    = "src"
POSTER_FILENAME        = "image.png"
SHORTCUT_ICON_FILENAME = "icon.ico"
SHORTCUT_ARG_FILENAME  = "agr.json"

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
# (путь MANIFEST_CHUNK_DB_CACHE — см. блок APPDATA_DIR-путей ниже)
CHUNK_DIR = "chunks"

# ── Старый JSON-сервер (для совместимости bootstrap'а) ────────────────────────
JSON_SERVER = "https://nethunter.sytes.net/sky/"

# ── Crash logger ──────────────────────────────────────────────────────────────
# Подтверждено пользователем 2026-09-22 (реальный листинг Nextcloud, живой
# крэш-репорт от старой, ДОustановочной версии инструмента под ником "xcode",
# .../CRASH_Log/xcode/10.03.2025-18.59.33/) — старое значение "1TB/CrashLogs"
# было такой же устаревшей заглушкой, как раньше DEPOT_REMOTE_PATH. Финальная
# структура (собирается в crash_logger.py::upload_files(), не трогал) —
# <CRASH_LOG_REMOTE_PATH>/<username>/<ММ.ДД.ГГГГ-ЧЧ.ММ.СС>/ — уже совпадает
# с реальным примером, чинить нужно было только сам путь.
CRASH_LOG_REMOTE_PATH = "1TB/TESS/Staticfolders/CRASH_Log"

# ── Debug-режим (настройки -> "Режим отладки") ────────────────────────────────
# Прямой запрос пользователя 2026-09-22: если включено, весь лог консоли
# лаунчера (LOG_FILE, см. блок ниже) отправляется на сервер тем же
# WebDAV-механизмом, что и крэш-репорты (core/crash_logger.py::upload_files(),
# уже проверенный и подтверждённый в этом сеансе выше). ПУТЬ НЕ ПОДТВЕРЖДЁН —
# по аналогии с CRASH_LOG_REMOTE_PATH, соседняя папка того же
# "Staticfolders" — сверить с реальным листингом, когда появится первая
# реальная отправка, тем же способом, каким сверяли путь крэш-логов.
DEBUG_LOG_REMOTE_PATH = "1TB/TESS/Staticfolders/DEBUG_Log"

# ── Настройки лаунчера — общие для всех сборок, прямо в APPDATA_DIR ───────────
CONFIG_FILE      = APPDATA_DIR / "config.json"
PROGRESS_FILE    = APPDATA_DIR / "progress.json"
LOG_FILE         = APPDATA_DIR / "launcher.log"

# ── Контент конкретной сборки — под BUILD_DATA_DIR (см. её собственный
# комментарий выше про мультисборочность) ──────────────────────────────────────
MANIFEST_CACHE   = BUILD_DATA_DIR / "manifest.json"   # кэш манифеста с сервера
POSTER_CACHE     = BUILD_DATA_DIR / "poster.png"
MANIFEST_CHUNK_DB_CACHE = BUILD_DATA_DIR / "manifest_chunk_cache.db"   # см. CHUNK_DIR выше
# .lnk-ярлык ссылается на иконку ФАЙЛОМ на диске (не встроенными байтами) —
# поэтому иконку ярлыка нужно один раз сохранить локально, не только скачать.
SHORTCUT_ICON_CACHE = BUILD_DATA_DIR / "shortcut_icon.ico"

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

# Параллелизм закачки чанков (core/chunk_installer.py) — отдельно от
# MAX_WORKERS (тот исторически зажат до 8 для плоского протокола, см.
# core/workers.py::DownloadWorker). Чанки в среднем МЕЛЬЧЕ целых файлов
# (для реальной сборки 2026-09-21 — ~1.1MB средний чанк при максимум 4MB),
# так что при 8 параллельных соединениях каждое успевает передать немного
# байт за раунд-трип — узкое место оказывается в числе соединений, а не в
# реальной пропускной способности сети/диска (живой симптом: гигабитная
# сеть, SSD, а факт — ~10.7 MB/s). CHUNK_MAX_WORKERS выше, и pool_maxsize
# в core/depot_client.py::DepotClient.__init__ поднят до 48, чтобы сам пул
# соединений не стал новым потолком раньше времени.
CHUNK_MAX_WORKERS = 24

# ── Assets ────────────────────────────────────────────────────────────────────
def get_asset_path(name: str) -> Path:
    """Путь к bundled-ассету (работает и в .py и в PyInstaller exe)"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


def activate_build(build) -> None:
    """
    Переключает "текущую" сборку на выбранную в карусели (см.
    ui/carousel_window.py, core/builds.py::Build) — живьём меняет модульные
    константы этого файла. `build` — любой объект с атрибутами
    `.name`/`.remote_path` (core.builds.Build).

    ВАЖНО, та же ловушка, что уже чинилась этим сеансом для DAV_PASSWORD
    (см. CLAUDE.md): любой код, который читает эти константы через
    `from config import DEPOT_REMOTE_PATH`/`MANIFEST_CACHE`/etc. (имя
    попадает в локальную область модуля при импорте), НЕ увидит смену
    сборки — такой импорт замораживает значение на момент импорта. Только
    код, который делает `import config as _config` и читает
    `_config.DEPOT_REMOTE_PATH` и т.п. ЖИВЬЁМ на момент вызова, подхватит
    переключение. core/depot_client.py и core/workers.py уже переведены на
    этот паттерн для всех констант, которые меняет эта функция — если
    добавляется новый потребитель DEPOT_REMOTE_PATH/BUILD_NAME/
    BUILD_DATA_DIR/MANIFEST_CACHE/POSTER_CACHE/MANIFEST_CHUNK_DB_CACHE/
    SHORTCUT_ICON_CACHE, та же дисциплина обязательна.

    Вызывается один раз, до создания UpdaterUI — карусель переключает
    сборку, ПОТОМ открывает главное окно (см. main.py). Переключение "на
    лету" при уже открытом главном окне не поддерживается и не нужно —
    сменить сборку можно только перезапуском карусели.
    """
    global BUILD_NAME, DEPOT_REMOTE_PATH, BUILD_DATA_DIR
    global MANIFEST_CACHE, POSTER_CACHE, MANIFEST_CHUNK_DB_CACHE, SHORTCUT_ICON_CACHE

    BUILD_NAME        = build.name
    DEPOT_REMOTE_PATH = build.remote_path
    BUILD_DATA_DIR     = APPDATA_DIR / BUILD_NAME
    BUILD_DATA_DIR.mkdir(parents=True, exist_ok=True)

    MANIFEST_CACHE          = BUILD_DATA_DIR / "manifest.json"
    POSTER_CACHE             = BUILD_DATA_DIR / "poster.png"
    MANIFEST_CHUNK_DB_CACHE = BUILD_DATA_DIR / "manifest_chunk_cache.db"
    SHORTCUT_ICON_CACHE     = BUILD_DATA_DIR / "shortcut_icon.ico"


def write_log_file(msg: str) -> None:
    """
    Дописывает строку в LOG_FILE (%APPDATA%\\TESVAE_Launcher\\launcher.log).
    Единственное место с этой логикой — раньше жила только внутри
    ui/main_window.py::UpdaterUI._write_log_file() как статический метод;
    вынесена сюда, когда появился второй вызывающий (ui/carousel_window.py,
    показывается ДО UpdaterUI и логирует в тот же файл до его создания) —
    тянуть UpdaterUI только ради одного статического метода было лишней
    связью между модулями. main_window.py теперь тоже зовёт эту функцию.
    Best-effort — ошибка записи на диск никогда не должна ломать лог в UI,
    только сама запись молча пропускается.
    """
    try:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def get_launcher_commit() -> str:
    """
    Короткий git-хэш текущего чекаута лаунчера — показывается в UI рядом с
    версией сборки (см. ui/main_window.py::lbl_version), чтобы при отладке
    было видно, какой именно код реально запущен, а не предполагать это по
    выводу `update_and_run.bat` в отдельном окне терминала. Прямой запрос
    пользователя — тот же класс путаницы, что уже был в этом сеансе с
    `git pull` (ложный "failed" на успешном fast-forward, неясно было, та
    ли версия вообще запущена).

    Работает только при запуске из git-чекаута (`launcher/` внутри клонированного
    репозитория) — git сам находит `.git`, поднимаясь от cwd вверх по дереву,
    так что можно звать из `launcher/`, не из корня репо. В собранном
    PyInstaller .exe `.git` не бандлится вообще — возвращает "?", это НЕ
    ошибка, просто нет откуда взять.
    """
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "?"


def get_window_title() -> str:
    """Заголовок окна — прямой запрос пользователя 2026-09-22, формат
    "TESL [<git-хэш> <версия лаунчера>]", например "TESL [e6f7eb9 19.0.8]".
    Заменил прежнюю статическую константу WINDOW_TITLE
    ("Skyrim MO2 Updater + Patcher {LAUNCHER_VERSION}") — теперь функция,
    вызывается при создании каждого окна (main_window.py), не константа
    времени импорта, ради живого git-хэша (см. get_launcher_commit())."""
    return f"TESL [{get_launcher_commit()} {LAUNCHER_VERSION}]"
