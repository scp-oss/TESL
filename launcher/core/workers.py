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
# TESL-Panel".
#
# **2026-09-29: панель теперь ДЕЙСТВИТЕЛЬНО хранит историю версий**
# (см. TESL-Panel::builds_db.py::record_version()/list_build_versions() —
# снапшот на каждую публикацию, последние `TESL_PANEL_KEEP_VERSIONS`)
# — комментарий ниже до этой правки говорил обратное ("панель отдаёт
# РОВНО ОДНО текущее состояние"), это было верно ДО этой даты, больше не
# актуально. `VersionLoaderWorker` теперь получает РЕАЛЬНЫЙ список версий
# вместо синтетической единственной псевдо-версии — старая UI-машинерия
# main_window.py (combo_versions/btn_rollback/_rollback_to_selected(),
# которая ждала этого с самого перехода на панель) заработала по
# назначению без переписывания самой этой машинерии.

def _load_panel_manifest(client: PanelDepotClient, version_key: Optional[str] = None):
    """Возвращает (entries, meta) — entries в ТОЧНО той же форме
    (List[FileEntry] из core.chunk_manifest_db), которую уже ожидает
    ChunkInstaller (её интерфейс не менялся вообще, см. её собственный
    докстринг класса) — сам ChunkInstaller не знает и не должен знать,
    что источник теперь панель, а не WebDAV .db-компаньон, и не знает
    про версии вообще — ему всё равно, какой набор entries установить.
    None, если сборка не найдена / манифест недоступен (сеть, ещё не
    публиковалась, или `version_key` не существует — например, был
    удалён pruning'ом старше `TESL_PANEL_KEEP_VERSIONS`).

    `version_key=None` — текущая (последняя опубликованная) версия,
    как и раньше. С `version_key` — конкретная историческая версия (см.
    `PanelDepotClient.fetch_manifest()`), путь отката."""
    raw = client.fetch_manifest(version_key)
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
    """Загружает сведения о сборке с TESL-Panel, включая РЕАЛЬНУЮ историю
    версий (2026-09-29, см. TESL-Panel::builds_db.py::record_version()).

    **До 2026-09-29** панель отдавала РОВНО ОДНО текущее состояние на
    build_id — этот воркер синтезировал единственную псевдо-"версию"
    ("build #N") ради обратной совместимости с уже существующим UI
    выбора версии в main_window.py (combo_versions/btn_rollback —
    построен изначально под старый WebDAV `depot.json` с реальной
    историей, просто не имел, что показывать после перехода на панель).
    Теперь панель версионирует каждую публикацию сама — этот воркер
    просто передаёт то, что она отдаёт, без искусственного ограничения
    до одного пункта; сама UI-машинерия main_window.py не менялась,
    ей и раньше нужен был именно такой список.

    `version_meta_loaded` — новый сигнал (`{label: {"version_key",
    "description","build_number","created_at",...}}`), нужен ИМЕННО
    чтобы combo_versions мог отличаться отображаемым текстом
    (человекочитаемая метка) от реального ключа, который нужно
    передать `PanelDepotClient.fetch_manifest(version_key=...)` для
    установки/отката НЕ-последней версии, и чтобы выбор в combo мог
    показать описание ИМЕННО той версии, не только текущей — см.
    main_window.py::`_on_version_meta_loaded()`/`_on_version_selected()`/
    `DownloadWorker.run()`."""

    log                  = pyqtSignal(str)
    versions_loaded      = pyqtSignal(list)   # [label, ...], самая новая первой
    version_meta_loaded  = pyqtSignal(dict)   # {label: {version_key,description,build_number,created_at,...}} — пусто для сборок без истории (публиковались до 2026-09-29)
    current_version      = pyqtSignal(str)    # label самой новой версии
    description          = pyqtSignal(str)    # текст описания релиза текущей версии (может быть пустым)
    finished             = pyqtSignal(bool)

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
                self.version_meta_loaded.emit({})
                self.finished.emit(False)
                return

            self.log.emit("Загружаем сведения о сборке...")
            client = PanelDepotClient(build_id)
            try:
                versions = client.list_versions()
                # Текущий манифест всё равно нужен: (а) как единственный
                # источник description/build_number для сборок без
                # истории (публиковались до 2026-09-29 — их прошлые
                # версии просто никогда не были засняты), (б) как
                # запасной путь, если сама выборка версий не удалась,
                # но текущая публикация читается нормально.
                raw = client.fetch_manifest()
            finally:
                client.close()

            if not raw and not versions:
                self.log.emit("Не удалось получить манифест сборки (ещё не публиковалась?)")
                self.versions_loaded.emit([])
                self.version_meta_loaded.emit({})
                self.finished.emit(False)
                return

            if versions:
                labels = []
                meta_map = {}
                for v in versions:
                    created = (v.get("created_at") or "")[:19].replace("T", " ")
                    label = f"build #{v.get('build_number', '?')} — {created}" if created else f"build #{v.get('build_number', '?')}"
                    # Коллизия крайне маловероятна (build_number растёт
                    # монотонно, см. depot_sync_manager.py), но на всякий
                    # случай не молчим — иначе combo_versions потеряет пункт.
                    if label in meta_map:
                        label = f"{label} ({v['version_key']})"
                    labels.append(label)
                    meta_map[label] = v
                current_label = labels[0]
                description = raw.get("description", "") if raw else (versions[0].get("description") or "")
            else:
                # Сборка публиковалась ДО того, как панель начала
                # версионировать (2026-09-29) — истории для неё нет,
                # показываем ровно то единственное, что показывали до
                # этой правки. version_key=None в meta_map —
                # DownloadWorker трактует отсутствие ключа как "текущая
                # версия", что тут и есть единственно возможное значение.
                # total_size считаем сами из raw.get("files") — у сборок
                # без истории версий панель не отдаёт total_size готовым
                # (это поле есть только в записях list_versions()), а
                # предупреждение о нехватке места на диске (см.
                # main_window.py::_check_disk_space()) нужно для ЛЮБОЙ
                # сборки, не только версионированных.
                current_label = f"build #{raw.get('build_number', '?')}"
                labels = [current_label]
                total_size = sum(info.get("size", 0) for info in raw.get("files", {}).values())
                meta_map = {current_label: {
                    "version_key": None,
                    "description": raw.get("description", "") or "",
                    "build_number": raw.get("build_number"),
                    "total_size": total_size,
                }}
                description = raw.get("description", "") or ""

            self.versions_loaded.emit(labels)
            self.version_meta_loaded.emit(meta_map)
            self.current_version.emit(current_label)
            self.description.emit(description)
            self.log.emit(f"Текущая версия: {current_label}" + ("" if versions else " (истории версий пока нет)"))
            self.finished.emit(True)
        except Exception as e:
            self.log.emit(f"Ошибка загрузки сведений о сборке: {e}")
            self.versions_loaded.emit([])
            self.version_meta_loaded.emit({})
            self.finished.emit(False)


# ── Download worker ───────────────────────────────────────────────────────────

class DownloadWorker(ThreadSafeWorker):
    """
    Скачивает файлы сборки с TESL-Panel (build_id = config.CURRENT_BUILD_ID,
    выбирается в карусели) через ChunkInstaller/PanelDepotClient — см.
    CLAUDE.md "Переход на TESL-Panel".

    **2026-09-29**: `version_key` в task dict — если задан, устанавливает
    ИМЕННО эту историческую версию (откат), не обязательно последнюю
    опубликованную. `None`/отсутствует — как и раньше, текущая
    (последняя) версия. main_window.py резолвит `version_label`
    (человекочитаемый текст combo_versions) → `version_key` через карту,
    полученную от `VersionLoaderWorker.version_keys_loaded` (см. её
    докстринг) — ПЕРЕД созданием task dict, сам DownloadWorker не знает
    ничего про метки, только про реальный ключ.

    task dict:
      type: "install" | "verify_and_install" | "apply_version"
      local_dir: str
      version_label: str | None  (только для отображения/возобновления
        паузы — см. main_window.py::_resume_paused_install())
      version_key: str | None  (реальный ключ версии для установки —
        None значит "текущая", см. выше)
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
            version_key = self.task.get("version_key") or None

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

            loaded = _load_panel_manifest(self._client, version_key)
            if loaded is None:
                if version_key:
                    self.log.emit(f"❌ Не удалось получить манифест версии {version_key} (устарела и вычищена? см. TESL_PANEL_KEEP_VERSIONS)")
                else:
                    self.log.emit("❌ Не удалось получить манифест сборки")
                self.finished.emit(False)
                return
            entries, meta = loaded
            if not entries:
                self.log.emit("⚠️ Манифест пуст")
                self.finished.emit(True)
                return

            if version_key:
                self.log.emit(f"⏪ Откат на версию {version_key}: build #{meta.get('build_number', '?')}, файлов: {len(entries)}")
            else:
                self.log.emit(f"📋 Build #{meta.get('build_number', '?')}, файлов: {len(entries)}")

            debug_mode = bool(self.task.get("debug_mode", False))
            if debug_mode:
                self.log.emit("🐛 Режим отладки включён — пишу построчную трассировку установки")
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
                debug=debug_mode,
            )
            ok = installer.install(entries)
            # Сводка ПРИЧИН отказа скачивания чанков (не одна строка на
            # каждый — 2020 одинаковых строк были бы тем же анти-
            # паттерном, что уже чинился для speed_update) — см.
            # PanelDepotClient.chunk_errors за живой повод 2026-09-29
            # ("не удалось скачать 2020 чанков" без единой зацепки).
            error_summary = self._client.chunk_error_summary()
            if error_summary:
                self.log.emit(f"🔎 Причины отказов скачивания чанков: {error_summary}")
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


# ── Manual ini profile apply ────────────────────────────────────────────────────

class ApplyIniProfileWorker(ThreadSafeWorker):
    """Явная кнопка "Применить профиль Skyrim.ini" — прямой запрос
    пользователя 2026-10-06: он удалил свои Skyrim.ini/SkyrimPrefs.ini
    руками, чтобы проверить новые шаблоны (см. core/ini_profile.py), и
    они не появились — `apply_ini_profile()` до этого вызывалась ТОЛЬКО
    изнутри `PostInstallWorker` (после установки/через "Создать ярлык"),
    без отдельного способа прогнать её по требованию. Этот воркер —
    тонкая обёртка, ничего больше не делает (не трогает SkyrimChecker/
    MO2Configurator/ярлык — та логика осталась в PostInstallWorker,
    смешивать их в одной кнопке только путало бы намерение клика)."""
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, local_dir: str = ""):
        super().__init__()
        self.local_dir = local_dir

    def run(self):
        from core.ini_profile import apply_ini_profile, resolve_skyrim_exe_path
        try:
            exe_path = resolve_skyrim_exe_path(self.local_dir)
            ok = apply_ini_profile(log=self.log.emit, skyrim_exe_path=exe_path)
            self.finished.emit(ok, "Профиль применён" if ok else "Не удалось применить профиль")
        except Exception as e:
            import traceback
            self.log.emit(f"❌ Ошибка применения профиля Skyrim.ini: {e}\n{traceback.format_exc()}")
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
        from core.ini_profile import apply_ini_profile
        from core.patch_runner import run_patches

        try:
            # Приоритет 1: Skyrim как компонент ЭТОЙ ЖЕ сборки
            # (<local_dir>/Skyrim, см. config.SKYRIM_COMPONENT_DIR) —
            # детерминированно, без похода в реестр/по дискам. Живой
            # инцидент 2026-10-06: на ПК без отдельно установленного
            # через Steam Skyrim `SkyrimChecker` ничего не находит
            # (result.found=False), и update_ini() раньше вообще не
            # вызывалась, хотя реальная игра лежала прямо тут же, в
            # составе этой сборки — путь в ModOrganizer.ini навсегда
            # оставался тем, что был записан при публикации/на прошлой
            # машине. SkyrimChecker (внешний поиск) — фолбэк только для
            # сборок, которые НЕ возят Skyrim как компонент.
            skyrim_dir = None
            bundled_skyrim_dir = os.path.join(self.local_dir, _config.SKYRIM_COMPONENT_DIR)
            if os.path.isdir(bundled_skyrim_dir):
                self.log.emit(f"Skyrim найден в составе сборки: {bundled_skyrim_dir}")
                MO2Configurator.update_ini(self.local_dir, bundled_skyrim_dir, log=self.log.emit)
                skyrim_dir = bundled_skyrim_dir
            else:
                result = SkyrimChecker().check(log=self.log.emit)
                if result.found:
                    MO2Configurator.update_ini(self.local_dir, result.skyrim_dir, log=self.log.emit)
                    skyrim_dir = result.skyrim_dir

            # Skyrim.ini/SkyrimPrefs.ini (Documents\My Games\...) — не
            # зависит от того, нашёлся ли Skyrim выше: папка должна быть
            # готова до первого запуска игры, best-effort (см.
            # core/ini_profile.py — никогда не роняет post-install).
            # skyrim_exe_path (если найден выше) заодно чинит DPI-
            # виртуализацию самого SkyrimSE.exe — см.
            # core/ini_profile.py::ensure_dpi_compat_override().
            skyrim_exe_path = (
                os.path.join(skyrim_dir, _config.SKYRIM_EXE_NAME) if skyrim_dir else None
            )
            apply_ini_profile(log=self.log.emit, skyrim_exe_path=skyrim_exe_path)

            # Патчи сборки (patch/, .bat) — прямой запрос пользователя
            # 2026-10-07, после явного подтверждения ("да, подключить
            # сейчас") — см. core/patch_runner.py за полный контракт
            # (очередь/идемпотентность/окружение, передаваемое патчу).
            # После ini-профиля (патч может зависеть от уже правильно
            # настроенной игры), до создания ярлыка (ярлык — финальный
            # шаг "всё готово").
            run_patches(self.local_dir, _config.CURRENT_BUILD_ID, log=self.log.emit)

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


# ── Move install location ─────────────────────────────────────────────────────

class MoveInstallWorker(ThreadSafeWorker):
    """Переносит уже установленную сборку на диске в другую папку
    (`shutil.move`) — прямой запрос пользователя 2026-10-06: "Перенести
    в другое место" в едином меню сборки/настроек, вместо повторной
    полной переустановки/перекачки.

    На фоновом потоке, НЕ на GUI — `shutil.move()` на 100+ ГБ, особенно
    между разными физическими дисками (где это полное копирование +
    удаление исходника, не атомарный rename), может идти долго; тот же
    класс урока про синхронную дисковую работу на GUI-потоке, что уже
    неоднократно фиксировался в CLAUDE.md для этого репозитория
    (постер/лог отладки/настройка после установки — всегда одна и та
    же причина живых "зависаний").

    Ни `should_stop()`, ни пауза не поддерживаются — `shutil.move()`
    атомарен на уровне одного вызова (прервать его на середине значило
    бы рисковать получить ни источник, ни назначение в согласованном
    виде); если пользователь закроет лаунчер посреди переноса, перенос
    либо успеет завершиться сам (daemon этому не мешает — closeEvent()
    не зовёт `.stop()` на этот воркер намеренно), либо прервётся вместе
    с процессом — в последнем случае источник/назначение могут остаться
    в промежуточном состоянии, тот же риск, что у любого прерванного
    `shutil.move`/`cp`/`mv` в принципе, ничего специфичного для этого
    кода."""

    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)   # ok, новый путь (пусто, если не ok)

    def __init__(self, src: str, dst: str):
        super().__init__()
        self.src = src
        self.dst = dst

    def run(self):
        import shutil
        try:
            self.log.emit(f"📂 Переносим сборку: {self.src} → {self.dst} (может занять долго)...")
            shutil.move(self.src, self.dst)
            self.log.emit("✅ Перенос завершён")
            self.finished.emit(True, self.dst)
        except Exception as e:
            self.log.emit(f"❌ Ошибка переноса: {type(e).__name__}: {e!r}")
            self.finished.emit(False, "")


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
