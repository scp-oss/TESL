# ==================== launcher/core/workers.py ====================
"""
Все QThread-воркеры лаунчера в одном модуле.
"""
import concurrent.futures
import os
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

import config as _config
from config import MAX_WORKERS, CHUNK_MAX_WORKERS
from core.depot_client import _sha256
from core.chunk_manifest_db import FileEntry, ChunkInfo
from core.chunk_installer import ChunkInstaller
from core.panel_client import PanelDepotClient


# ── Манифест сборки с панели — общее для DownloadWorker и VerifyWorker ────────
# **2026-09-28: заменяет _get_version_manifest_rel_path()/_load_chunk_manifest()
# (WebDAV .db-компаньон)** — переход на TESL-Panel, см. CLAUDE.md "Переход на
# TESL-Panel". Панель отдаёт РОВНО ОДНО текущее состояние на build_id
# (`depot_manifest.json`, не набор версий с историей, как старый WebDAV
# depot.json) — поэтому "версия" в новом протоколе значит "то, что сейчас
# реально опубликовано для этой сборки", выбор версии из списка (были у
# WebDAV combo_versions) больше не имеет смысла как отдельная фича; см.
# VersionLoaderWorker ниже — синтезирует единственную псевдо-"версию"
# ("build #N") ради обратной совместимости с существующим UI main_window.py
# (комбобокс/кнопки install/update там не переписывались в этом заходе).

def _load_panel_manifest(client: PanelDepotClient):
    """Возвращает (entries, meta) — entries в ТОЧНО той же форме
    (List[FileEntry] из core.chunk_manifest_db), которую уже ожидает
    ChunkInstaller (её интерфейс не менялся вообще, см. её собственный
    докстринг класса) — сам ChunkInstaller не знает и не должен знать,
    что источник теперь панель, а не WebDAV .db-компаньон. None, если
    сборка не найдена / манифест недоступен (сеть, ещё не публиковалась)."""
    raw = client.fetch_manifest()
    if raw is None:
        return None
    entries = []
    for path, info in raw.get("files", {}).items():
        chunks = [
            ChunkInfo(chunk_id=c["id"], offset=c["offset"], size=c["size"])
            for c in info.get("chunks", [])
        ]
        # component="" — путь в depot_manifest.json уже полностью
        # префиксован ("Skyrim/Data/Skyrim.esm", см. TESL-Manager::
        # chunk_manager.py::scan_components()) — FileEntry.rel_out_path
        # возвращает path как есть, когда component пуст.
        entries.append(FileEntry(
            component="", path=path,
            size=info["size"], file_hash=info["hash"], chunks=chunks,
        ))
    meta = {
        "build_number": raw.get("build_number"),
        "channel":      raw.get("channel", ""),
        "description":  raw.get("description", ""),
    }
    return entries, meta


# ── Base ──────────────────────────────────────────────────────────────────────

class ThreadSafeWorker(QObject):
    def __init__(self):
        super().__init__()
        self._stop_event   = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._is_paused    = False

    def stop(self):
        self._stop_event.set()
        self._resume_event.set()

    def pause(self):
        if not self._is_paused:
            self._resume_event.clear()
            self._is_paused = True

    def resume(self):
        if self._is_paused:
            self._resume_event.set()
            self._is_paused = False

    def _wait_if_paused(self):
        self._resume_event.wait()

    def _should_stop(self) -> bool:
        return self._stop_event.is_set()


# ── Version loader ────────────────────────────────────────────────────────────

class VersionLoaderWorker(ThreadSafeWorker):
    """Загружает сведения о текущей сборке с TESL-Panel.

    **2026-09-28**: панель отдаёт РОВНО ОДНО текущее состояние на
    build_id (`depot_manifest.json`) — нет истории версий/каналов, как
    у старого WebDAV `depot.json` (`versions{}`+`current{}`). Вместо
    того чтобы выбрасывать весь UI выбора версии из main_window.py
    (большой, рискованный рефакторинг вне объёма этого захода — сам
    выбор СБОРКИ уже переехал на уровень выше, в карусель), эмитит
    список из ОДНОЙ синтетической "версии" (`"build #N"`, из
    `depot_manifest.json`'s `build_number`) — тот же сигнальный
    контракт (`versions_loaded: list[str]`, `current_version: str`),
    что и раньше, комбобокс в main_window.py работает без изменений,
    просто всегда с одним пунктом. `description` (текст релиза, см.
    CLAUDE.md "Описание релиза") эмитится отдельным сигналом —
    единственное реально НОВОЕ, что появилось здесь."""

    log             = pyqtSignal(str)
    versions_loaded = pyqtSignal(list)    # [label, ...] — сейчас всегда 0 или 1 элемент
    current_version = pyqtSignal(str)     # label текущей (единственной) версии
    description     = pyqtSignal(str)     # текст описания релиза (может быть пустым)
    finished        = pyqtSignal(bool)

    def __init__(self, channel: str = "stable"):
        super().__init__()
        self.channel = channel   # оставлен для совместимости вызова — панель
        # не различает каналы на этом уровне (публикующий контроль канала
        # остаётся за TESL-Manager, здесь просто не на что ещё выбирать).

    def run(self):
        try:
            build_id = _config.CURRENT_BUILD_ID
            if not build_id:
                self.log.emit("❌ Сборка не выбрана")
                self.versions_loaded.emit([])
                self.finished.emit(False)
                return

            self.log.emit("Загружаем сведения о сборке...")
            client = PanelDepotClient(build_id)
            try:
                raw = client.fetch_manifest()
            finally:
                client.close()

            if not raw:
                self.log.emit("Не удалось получить манифест сборки (ещё не публиковалась?)")
                self.versions_loaded.emit([])
                self.finished.emit(False)
                return

            label = f"build #{raw.get('build_number', '?')}"
            self.versions_loaded.emit([label])
            self.current_version.emit(label)
            self.description.emit(raw.get("description", "") or "")
            self.log.emit(f"Текущая версия: {label}")
            self.finished.emit(True)
        except Exception as e:
            self.log.emit(f"Ошибка загрузки сведений о сборке: {e}")
            self.versions_loaded.emit([])
            self.finished.emit(False)


# ── Download worker ───────────────────────────────────────────────────────────

class DownloadWorker(ThreadSafeWorker):
    """
    Скачивает файлы сборки с TESL-Panel (build_id = config.CURRENT_BUILD_ID,
    выбирается в карусели) через ChunkInstaller/PanelDepotClient — см.
    CLAUDE.md "Переход на TESL-Panel". Всегда устанавливает ТЕКУЩЕЕ
    опубликованное состояние сборки — панель не хранит историю версий,
    как раньше хранил WebDAV depot.json.

    task dict:
      type: "install" | "verify_and_install" | "apply_version"
      local_dir: str
      version_label: str | None  (принимается для совместимости с
        main_window.py, больше ни на что не влияет — см. run())
    """

    log                    = pyqtSignal(str)
    progress_total_setmax  = pyqtSignal(int)
    progress_total         = pyqtSignal(int)
    current_file           = pyqtSignal(str)
    finished               = pyqtSignal(bool)

    def __init__(self, task: dict):
        super().__init__()
        self.task       = task
        self.data_lock  = threading.Lock()
        self._done      = 0
        self._start_t   = 0.0
        self._client    = None
        # Скользящее окно для скорости (не средняя за всю установку с
        # самого начала) — см. тот же фикс и его причину в
        # core/chunk_installer.py::install() (комментарий у
        # last_sample_t/smoothed_dl_speed там).
        self._last_sample_t = 0.0
        self._last_sample_bytes = 0
        self._smoothed_speed = 0.0
        self._downloaded_bytes = 0

    def run(self):
        try:
            local_dir = Path(self.task.get("local_dir", ""))
            # version_label — оставлен в task dict вызывающим кодом
            # (main_window.py не переписывался в этом заходе) но больше
            # ничего не значит: у панели нет истории версий, всегда
            # устанавливается ТЕКУЩЕЕ опубликованное состояние сборки
            # (CURRENT_BUILD_ID, выбранной в карусели). См. класс-докстринг.

            if not local_dir:
                self.log.emit("❌ Локальная папка не задана")
                self.finished.emit(False)
                return
            build_id = _config.CURRENT_BUILD_ID
            if not build_id:
                self.log.emit("❌ Сборка не выбрана")
                self.finished.emit(False)
                return

            local_dir.mkdir(parents=True, exist_ok=True)

            self._client = PanelDepotClient(build_id)

            loaded = _load_panel_manifest(self._client)
            if loaded is None:
                self.log.emit("❌ Не удалось получить манифест сборки")
                self.finished.emit(False)
                return
            entries, meta = loaded
            if not entries:
                self.log.emit("⚠️ Манифест пуст")
                self.finished.emit(True)
                return

            self.log.emit(f"📋 Build #{meta.get('build_number', '?')}, файлов: {len(entries)}")

            installer = ChunkInstaller(
                client=self._client,
                local_dir=local_dir,
                max_workers=CHUNK_MAX_WORKERS,
                on_log=self.log.emit,
                on_progress_max=self.progress_total_setmax.emit,
                on_progress=self.progress_total.emit,
                on_current=self.current_file.emit,
                should_stop=self._should_stop,
                wait_if_paused=self._wait_if_paused,
            )
            ok = installer.install(entries)
            self.finished.emit(ok)

        except Exception as e:
            import traceback
            self.log.emit(f"❌ Критическая ошибка: {e}\n{traceback.format_exc()}")
            self.finished.emit(False)
        finally:
            if self._client:
                self._client.close()


# ── Verify worker ─────────────────────────────────────────────────────────────

class VerifyWorker(ThreadSafeWorker):
    """Проверяет файлы по манифесту. Если найдены проблемы — сообщает сколько качать."""

    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)   # current, total, eta
    finished = pyqtSignal(bool, int, int, int)  # ok, to_download, to_redownload, deleted

    def __init__(self, local_dir: str):
        super().__init__()
        self.local_dir = Path(local_dir)
        self._start_t  = 0.0

    def run(self):
        try:
            self.log.emit("Начинаем проверку файлов...")

            build_id = _config.CURRENT_BUILD_ID
            if not build_id:
                self.log.emit("❌ Сборка не выбрана")
                self.finished.emit(False, 0, 0, 0)
                return

            client = PanelDepotClient(build_id)
            try:
                loaded = _load_panel_manifest(client)
            except Exception as e:
                self.log.emit(f"❌ Не удалось прочитать манифест сборки: {e}")
                self.finished.emit(False, 0, 0, 0)
                return

            if loaded is None:
                self.log.emit("❌ Манифест недоступен")
                self.finished.emit(False, 0, 0, 0)
                return

            entries, meta = loaded
            self.log.emit(f"📦 Build #{meta.get('build_number', '?')}")
            try:
                self._run_chunk_verify(client, entries)
            finally:
                client.close()

        except Exception as e:
            import traceback
            self.log.emit(f"Ошибка проверки: {e}\n{traceback.format_exc()}")
            self.finished.emit(False, 0, 0, 0)

    def _run_chunk_verify(self, client: PanelDepotClient, entries) -> None:
        """
        Проверка chunk-based сборки — тот же цикл (ThreadPoolExecutor,
        прогресс/пауза/отмена), что у флэт-протокола выше, только по
        FileEntry из chunk-манифеста, а не {rel_path: hash} из
        manifest.json. Не переиспользует ChunkInstaller.diff() напрямую —
        тот метод однопоточный и без поддержки отмены/паузы/прогресса; для
        170К+ файлов это выглядело бы как зависание, а не как идущая
        проверка (тот же класс проблемы "тишина вместо обратной связи",
        что уже чинился в этом репозитории для отправки лога отладки/
        постера — см. CLAUDE.md).
        """
        try:
            total = len(entries)
            self.log.emit(f"Файлов в манифесте: {total}")

            self.log.emit("Поиск лишних файлов...")
            installer = ChunkInstaller(client=client, local_dir=self.local_dir, on_log=self.log.emit)
            deleted = installer._cleanup_extra_files(entries)

            self.log.emit("Проверка хэшей...")
            to_dl, to_redl = [], []
            checked = 0
            self._start_t = time.time()

            def _check_one(entry):
                if self._should_stop():
                    return None
                self._wait_if_paused()
                local = self.local_dir / Path(entry.rel_out_path.replace("/", os.sep))
                if not local.exists():
                    return ("missing", entry.rel_out_path)
                if local.stat().st_size != entry.size:
                    return ("corrupted", entry.rel_out_path)
                if _sha256(local) != entry.file_hash:
                    return ("corrupted", entry.rel_out_path)
                return ("ok", entry.rel_out_path)

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(_check_one, e): e for e in entries}
                for future in concurrent.futures.as_completed(futures):
                    if self._should_stop():
                        self.finished.emit(False, 0, 0, 0)
                        return

                    result = future.result()
                    if result is None:
                        continue

                    status, path = result
                    checked += 1

                    elapsed = time.time() - self._start_t
                    remaining = total - checked
                    eta = ""
                    if checked > 0 and elapsed > 0:
                        rate = checked / elapsed
                        eta  = _fmt_eta(remaining / rate if rate > 0 else 0)

                    self.progress.emit(checked, total, eta)

                    if status == "missing":
                        to_dl.append(path)
                    elif status == "corrupted":
                        to_redl.append(path)

            if self._should_stop():
                self.finished.emit(False, 0, 0, 0)
                return

            self.log.emit(
                f"Готово: к загрузке {len(to_dl)}, "
                f"к перекачке {len(to_redl)}, "
                f"удалено лишних {deleted}"
            )
            self.finished.emit(True, len(to_dl), len(to_redl), deleted)

        except Exception as e:
            import traceback
            self.log.emit(f"Ошибка проверки: {e}\n{traceback.format_exc()}")
            self.finished.emit(False, 0, 0, 0)


# ── Poster loader ─────────────────────────────────────────────────────────────

class PosterLoader(ThreadSafeWorker):
    """Фоново скачивает постер ТЕКУЩЕЙ (выбранной в карусели) сборки —
    см. BuildsLoaderWorker выше за отдельный, недолговечный путь загрузки
    постеров ВСЕХ сборок для самой карусели; здесь — тот же файл, но для
    уже открытого главного окна одной конкретной сборки."""
    log     = pyqtSignal(str)
    loaded  = pyqtSignal(bytes)
    failed  = pyqtSignal()

    def run(self):
        try:
            build_id = _config.CURRENT_BUILD_ID
            if not build_id:
                self.failed.emit()
                return
            client = PanelDepotClient(build_id)
            data   = client.fetch_poster()
            client.close()
            if data:
                self.loaded.emit(data)
            else:
                self.log.emit("Постер: не найден на панели")
                self.failed.emit()
        except Exception as e:
            self.log.emit(f"Постер: неожиданная ошибка — {e}")
            self.failed.emit()


# ── Builds loader (карусель) ──────────────────────────────────────────────────

class BuildsLoaderWorker(ThreadSafeWorker):
    """
    Загружает список сборок для карусели на старте (core/builds.py) и
    постер-миниатюру для каждой — в фоне, чтобы карусель не подвисала на
    сетевых запросах. loaded emit'ится один раз со всем списком (карусель
    сразу рисует плитки с текстом), poster_loaded — по одной на сборку,
    по мере скачивания (та же логика "показывай что уже готово", что и у
    funnel-результатов в z0r-panel, только тут это постеры, а не стратегии).
    """
    log           = pyqtSignal(str)
    loaded        = pyqtSignal(list)         # List[core.builds.Build]
    poster_loaded = pyqtSignal(str, bytes)   # build.name, poster bytes

    def run(self):
        from core.builds import list_builds
        try:
            builds = list_builds(log=self.log.emit)
        except Exception as e:
            self.log.emit(f"Ошибка загрузки списка сборок: {e}")
            builds = []
        self.loaded.emit(builds)

        # Постер каждой сборки — свой недолговечный PanelDepotClient (та
        # же логика, что раньше была у fetch_poster_bytes(): карусель
        # показывает миниатюры ВСЕХ сборок сразу, до того как какая-либо
        # из них станет "активной" через config.activate_build() — нет
        # диска-кэша здесь вообще, каждый показ карусели качает заново;
        # если станет проблемой при большом числе сборок — отдельная
        # доработка (кэш по build_id), не сейчас).
        for b in builds:
            if self._should_stop():
                return
            client = PanelDepotClient(b.build_id)
            try:
                data = client.fetch_poster()
            finally:
                client.close()
            if data:
                self.poster_loaded.emit(b.name, data)


# ── Skyrim check worker ───────────────────────────────────────────────────────

class SkyrimCheckWorker(ThreadSafeWorker):
    """Запускает SkyrimChecker в фоне."""
    log      = pyqtSignal(str)
    finished = pyqtSignal(object)   # SkyrimCheckResult

    def run(self):
        from core.skyrim_checker import SkyrimChecker
        checker = SkyrimChecker()
        result  = checker.check(log=self.log.emit)
        self.finished.emit(result)


# ── Crash log sender ──────────────────────────────────────────────────────────

class CrashLogSender(ThreadSafeWorker):
    """Собирает и отправляет крэш-логи на WebDAV."""
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, username: str, skyrim_dir: str):
        super().__init__()
        self.username   = username.lower().strip()
        self.skyrim_dir = Path(skyrim_dir)

    def run(self):
        try:
            from core.crash_logger import collect_files, upload_files
            files = collect_files(str(self.skyrim_dir), log=self.log.emit)
            if not files:
                self.finished.emit(False, "Нет файлов для отправки")
                return
            ok, msg = upload_files(self.username, files, log=self.log.emit)
            self.finished.emit(ok, msg)
        except Exception as e:
            self.finished.emit(False, str(e))


# ── Post-install configurator ───────────────────────────────────────────────────

class PostInstallWorker(ThreadSafeWorker):
    """
    Настройка после установки/по кнопке "Создать ярлык": проверка Skyrim,
    правка ModOrganizer.ini под реальный путь игры, докачка иконки/
    аргумента ярлыка с сервера, создание самого .lnk.

    Раньше (до 2026-09-22) весь этот блок шёл СИНХРОННО на GUI-потоке —
    ui/main_window.py::_post_install_configure()/_create_shortcut() дублировали
    один и тот же код напрямую в обработчике. Реальная цена: SkyrimChecker
    (диск/реестр) + два сетевых запроса (fetch_shortcut_assets, до 20с
    каждый) + subprocess для .lnk (MO2Configurator.create_shortcut(),
    timeout=30с) — суммарно интерфейс мог замереть почти на минуту,
    полностью не отвечая ни на что. Живой репорт 2026-09-22: пользователь
    сообщил, что лаунчер "завис" примерно в районе запуска игры — этот
    синхронный блок точно совпадает по классу симптома (не подтверждено
    как именно ЭТОТ конкретный случай, но сама дыра была реальной и
    стоила исправления независимо от того, она ли была причиной в тот раз).
    """
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, local_dir: str):
        super().__init__()
        self.local_dir = local_dir

    def run(self):
        from core.skyrim_checker import SkyrimChecker
        from core.patcher import MO2Configurator
        from core.depot_client import fetch_shortcut_assets

        try:
            result = SkyrimChecker().check(log=self.log.emit)
            if result.found:
                MO2Configurator.update_ini(self.local_dir, result.skyrim_dir, log=self.log.emit)

            icon_path, arg = fetch_shortcut_assets(log=self.log.emit)

            ok, msg = MO2Configurator.create_shortcut(
                self.local_dir, log=self.log.emit, icon_path=icon_path, arg=arg,
            )
            self.log.emit(msg)
            self.finished.emit(ok, msg)
        except Exception as e:
            import traceback
            self.log.emit(f"❌ Ошибка настройки после установки: {e}\n{traceback.format_exc()}")
            self.finished.emit(False, str(e))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_eta(seconds: float) -> str:
    if seconds < 0:
        return "0s"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
