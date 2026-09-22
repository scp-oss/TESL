# ==================== launcher/core/workers.py ====================
"""
Все QThread-воркеры лаунчера в одном модуле.
"""
import concurrent.futures
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from PyQt6.QtCore import QObject, pyqtSignal

import config as _config   # для MANIFEST_CHUNK_DB_CACHE — см. комментарий ниже
from config import MAX_WORKERS, CHUNK_MAX_WORKERS, DAV_BASE_URL, DAV_USERNAME
from core.depot_client import DepotClient, _sha256
from core.chunk_manifest_db import read_manifest_db
from core.chunk_installer import ChunkInstaller


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
    """Загружает список версий из depot.json."""

    log             = pyqtSignal(str)
    versions_loaded = pyqtSignal(list)    # [label, ...]
    current_version = pyqtSignal(str)     # label текущей версии канала
    finished        = pyqtSignal(bool)

    def __init__(self, channel: str = "stable"):
        super().__init__()
        self.channel = channel

    def run(self):
        try:
            self.log.emit("Загружаем список версий...")
            client  = DepotClient()
            index   = client.fetch_depot_index()

            if not index:
                self.log.emit("Не удалось загрузить depot.json")
                self.versions_loaded.emit([])
                self.finished.emit(False)
                return

            versions_dict = index.get("versions", {})
            releases = sorted(
                versions_dict.values(),
                key=lambda r: r.get("build_number", 0),
                reverse=True,
            )
            labels = [r.get("label", "") for r in releases if r.get("label")]
            self.versions_loaded.emit(labels)

            # Текущая версия канала
            current_key = index.get("current", {}).get(self.channel)
            if current_key and current_key in versions_dict:
                cur_label = versions_dict[current_key].get("label", "")
                self.current_version.emit(cur_label)
                self.log.emit(f"Текущая версия ({self.channel}): {cur_label}")

            self.log.emit(f"Загружено версий: {len(labels)}")
            self.finished.emit(True)
        except Exception as e:
            self.log.emit(f"Ошибка загрузки версий: {e}")
            self.versions_loaded.emit([])
            self.finished.emit(False)


# ── Download worker ───────────────────────────────────────────────────────────

class DownloadWorker(ThreadSafeWorker):
    """
    Скачивает файлы сборки с WebDAV depot.
    Поддерживает resume, параллельную загрузку, верификацию.

    task dict:
      type: "install" | "verify_and_install" | "apply_version"
      local_dir: str
      version_label: str | None  (None = текущая stable)
    """

    log                    = pyqtSignal(str)
    progress_total_setmax  = pyqtSignal(int)
    progress_total         = pyqtSignal(int)
    current_file           = pyqtSignal(str)
    speed_update           = pyqtSignal(str)   # "12.3 MB/s"
    finished               = pyqtSignal(bool)

    def __init__(self, task: dict):
        super().__init__()
        self.task       = task
        self.data_lock  = threading.Lock()
        self._done      = 0
        self._start_t   = 0.0
        self._client    = None

    def run(self):
        try:
            local_dir     = Path(self.task.get("local_dir", ""))
            version_label = self.task.get("version_label")

            if not local_dir:
                self.log.emit("❌ Локальная папка не задана")
                self.finished.emit(False)
                return

            local_dir.mkdir(parents=True, exist_ok=True)

            self._client = DepotClient()

            # Пробуем chunk-протокол (см. config.py "Chunk-based версии") —
            # для версий, опубликованных как content-addressed чанки, а не
            # плоские files/<rel_path>. Не найден компаньон manifest.db —
            # обычный, ожидаемый случай, просто работаем по старому пути.
            chunk_result = self._try_chunk_install(version_label, local_dir)
            if chunk_result is not None:
                self.finished.emit(chunk_result)
                return

            # Скачиваем манифест
            self.log.emit("📥 Загружаем манифест...")
            manifest = self._client.fetch_manifest(version_label)
            if not manifest:
                self.log.emit("❌ Не удалось получить манифест")
                self.finished.emit(False)
                return

            files: Dict[str, str] = manifest.get("files", {})
            if not files:
                self.log.emit("⚠️ Манифест пуст")
                self.finished.emit(True)
                return

            self._client.cache_manifest(manifest)
            self.log.emit(f"📋 Файлов в манифесте: {len(files)}")

            # Определяем что нужно скачать
            self.log.emit("🔍 Сравниваем с локальными файлами...")
            to_dl, to_redl, ok = self._client.diff_with_local(files, local_dir)
            total_download = len(to_dl) + len(to_redl)

            self.log.emit(
                f"К загрузке: {len(to_dl)}, "
                f"к перекачке: {len(to_redl)}, "
                f"в порядке: {len(ok)}"
            )

            if total_download == 0:
                self.log.emit("✅ Все файлы актуальны")
                self.finished.emit(True)
                return

            to_download_list = to_dl + to_redl
            self.progress_total_setmax.emit(total_download)
            self._done   = 0
            self._start_t = time.time()

            failed: List[str] = []
            lock = threading.Lock()

            def _download_one(rel_path: str) -> bool:
                if self._should_stop():
                    return False
                self._wait_if_paused()

                local_file = local_dir / Path(rel_path.replace("/", os.sep))
                expected   = files.get(rel_path, "")

                ok_flag, msg = self._client.download_file(
                    rel_path   = rel_path,
                    local_path = local_file,
                    expected_hash = expected,
                    stop_fn    = self._should_stop,
                    pause_fn   = self._wait_if_paused,
                )

                with lock:
                    self._done += 1
                    self.progress_total.emit(self._done)
                    self.current_file.emit(rel_path)

                    # Скорость
                    elapsed = time.time() - self._start_t
                    if elapsed > 0 and local_file.exists():
                        total_bytes = sum(
                            (local_dir / Path(p.replace("/", os.sep))).stat().st_size
                            for p in (to_dl + to_redl)[:self._done]
                            if (local_dir / Path(p.replace("/", os.sep))).exists()
                        )
                        speed = total_bytes / elapsed / 1024 / 1024
                        self.speed_update.emit(f"{speed:.1f} MB/s")

                if not ok_flag:
                    self.log.emit(f"❌ {msg}")
                    with lock:
                        failed.append(rel_path)
                return ok_flag

            optimal = min(MAX_WORKERS, 8)
            self.log.emit(f"⬇️ Скачиваем {total_download} файлов ({optimal} потоков)...")

            with concurrent.futures.ThreadPoolExecutor(max_workers=optimal) as pool:
                futures = {pool.submit(_download_one, p): p for p in to_download_list}
                for future in concurrent.futures.as_completed(futures):
                    if self._should_stop():
                        for f in futures:
                            f.cancel()
                        self.log.emit("⏹ Загрузка остановлена")
                        self.finished.emit(False)
                        return

            if failed:
                self.log.emit(f"❌ Не загружено {len(failed)} файлов")
                self.finished.emit(False)
            else:
                self.log.emit(f"✅ Загрузка завершена: {total_download} файлов")
                self.finished.emit(True)

        except Exception as e:
            import traceback
            self.log.emit(f"❌ Критическая ошибка: {e}\n{traceback.format_exc()}")
            self.finished.emit(False)
        finally:
            if self._client:
                self._client.close()

    # ── Chunk-протокол (см. config.py "Chunk-based версии") ─────────────────────

    def _get_version_manifest_rel_path(self, version_label: Optional[str]) -> Optional[str]:
        """Находим путь к JSON-манифесту версии (info['manifest']) — нужен
        только чтобы вычислить путь к её .db-компаньону, сам JSON тут не
        скачивается (обычный путь скачает его сам через fetch_manifest())."""
        index = self._client.fetch_depot_index()
        if not index:
            return None
        if version_label is None:
            current_key = index.get("current", {}).get("stable")
            if not current_key:
                return None
            info = index.get("versions", {}).get(current_key)
        else:
            info = None
            for release in index.get("versions", {}).values():
                if release.get("label") == version_label:
                    info = release
                    break
        return info.get("manifest") if info else None

    def _try_chunk_install(self, version_label: Optional[str], local_dir: Path) -> Optional[bool]:
        """
        Возвращает None, если для этой версии нет chunk-манифеста (компаньона
        .db) — значит нужно работать по обычному "плоскому" пути (files/<rel>).
        Возвращает True/False, если реально запустили chunk-установку.

        Сама загрузка/сборка/верификация — в `core.chunk_installer.ChunkInstaller`
        (без Qt-зависимости, юнит-тестируется отдельно) — этот метод только
        находит манифест и прокидывает Qt-сигналы в его колбэки.
        """
        manifest_rel = self._get_version_manifest_rel_path(version_label)
        if not manifest_rel:
            return None

        db_bytes = self._client.fetch_manifest_db_bytes(manifest_rel)
        if db_bytes is None:
            return None

        self.log.emit("📦 Обнаружен chunk-манифест — устанавливаем напрямую из chunks/ на сервере")
        try:
            # _config.MANIFEST_CHUNK_DB_CACHE живьём, не через `from config
            # import MANIFEST_CHUNK_DB_CACHE` — та заморозила бы путь на
            # момент импорта этого модуля, раньше переключения сборки в
            # карусели через config.activate_build() (см. её докстринг).
            cache_path = _config.MANIFEST_CHUNK_DB_CACHE
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(db_bytes)
            entries, meta = read_manifest_db(cache_path)
        except Exception as e:
            self.log.emit(f"❌ Не удалось прочитать chunk-манифест: {e}")
            return False

        self.log.emit(f"📋 Версия по chunk-манифесту: {meta.get('version_key', '?')}")

        installer = ChunkInstaller(
            client=self._client,
            local_dir=local_dir,
            max_workers=CHUNK_MAX_WORKERS,
            on_log=self.log.emit,
            on_progress_max=self.progress_total_setmax.emit,
            on_progress=self.progress_total.emit,
            on_current=self.current_file.emit,
            on_speed=self.speed_update.emit,
            should_stop=self._should_stop,
            wait_if_paused=self._wait_if_paused,
        )
        return installer.install(entries)


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

            client   = DepotClient()
            manifest = client.fetch_manifest()

            if not manifest:
                # Пробуем кэш
                manifest = client.load_cached_manifest()
                if not manifest:
                    self.log.emit("❌ Манифест недоступен")
                    self.finished.emit(False, 0, 0, 0)
                    return

            client.cache_manifest(manifest)
            files = manifest.get("files", {})
            total = len(files)
            self.log.emit(f"Файлов в манифесте: {total}")

            # Удаляем лишние
            self.log.emit("Поиск лишних файлов...")
            deleted = 0
            manifest_paths = {
                str(Path(p.replace("/", os.sep))) for p in files
            }
            for f in self.local_dir.rglob("*"):
                if self._should_stop():
                    self.finished.emit(False, 0, 0, 0)
                    return
                if f.is_file():
                    rel = str(f.relative_to(self.local_dir))
                    if rel not in manifest_paths:
                        try:
                            f.unlink()
                            deleted += 1
                        except Exception:
                            pass

            # Проверяем хэши
            self.log.emit("Проверка хэшей...")
            to_dl, to_redl = [], []
            checked = 0
            self._start_t = time.time()

            file_items = list(files.items())

            def _check_one(item):
                rel_path, hash_str = item
                if self._should_stop():
                    return None
                self._wait_if_paused()
                local = self.local_dir / Path(rel_path.replace("/", os.sep))
                if not local.exists():
                    return ("missing", rel_path)
                expected = hash_str.replace("sha256:", "").strip()
                actual   = _sha256(local)
                if actual != expected:
                    return ("corrupted", rel_path)
                return ("ok", rel_path)

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(_check_one, item): item for item in file_items}
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
    """Фоново скачивает постер."""
    log     = pyqtSignal(str)
    loaded  = pyqtSignal(bytes)
    failed  = pyqtSignal()

    def run(self):
        try:
            client = DepotClient()
            data   = client.fetch_poster(on_log=self.log.emit)
            client.close()
            if data:
                self.loaded.emit(data)
            else:
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
        from core.depot_client import fetch_poster_bytes
        try:
            builds = list_builds(log=self.log.emit)
        except Exception as e:
            self.log.emit(f"Ошибка загрузки списка сборок: {e}")
            builds = []
        self.loaded.emit(builds)

        for b in builds:
            if self._should_stop():
                return
            data = fetch_poster_bytes(b.remote_path, on_log=self.log.emit)
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
