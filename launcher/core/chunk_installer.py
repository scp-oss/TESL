# ==================== launcher/core/chunk_installer.py ====================
"""
Устанавливает сборку из chunk-манифеста (`manifest.db` + `chunks/<xx>/<id>`
на WebDAV) — диф по хэшу, докачка недостающих чанков (с дедупом), сборка
файлов по offset, верификация sha256 целиком, чистка лишнего.

Намеренно БЕЗ Qt-зависимости (в отличие от `core/workers.py`, где живут
все QThread-воркеры) — тот же принцип, что у `TESL-Manager`'а
`recover_from_chunks.py::ChunkRecoverer` (тот же алгоритм, тот же повод:
проверяемость без GUI-окружения). `core/workers.py::DownloadWorker`
оборачивает этот класс в Qt-сигналы через колбэки, сам ничего не решает.
"""

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from core.chunk_manifest_db import FileEntry


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


class ChunkInstaller:
    """
    `client` — любой объект с методом `download_chunk(chunk_id: str) ->
    Optional[bytes]` (обычно `core.depot_client.DepotClient`, но для
    тестов подходит любой дублёр с этим методом).
    """

    def __init__(
        self,
        client,
        local_dir: Path,
        max_workers: int = 8,
        on_log: Callable[[str], None] = print,
        on_progress_max: Callable[[int], None] = lambda n: None,
        on_progress: Callable[[int], None] = lambda n: None,
        on_current: Callable[[str], None] = lambda s: None,
        on_speed: Callable[[str], None] = lambda s: None,
        should_stop: Callable[[], bool] = lambda: False,
        wait_if_paused: Callable[[], None] = lambda: None,
    ):
        self.client = client
        self.local_dir = Path(local_dir)
        self.max_workers = max_workers
        self.on_log = on_log
        self.on_progress_max = on_progress_max
        self.on_progress = on_progress
        self.on_current = on_current
        self.on_speed = on_speed
        self.should_stop = should_stop
        self.wait_if_paused = wait_if_paused

    def diff(self, entries: List[FileEntry]) -> List[FileEntry]:
        to_process: List[FileEntry] = []
        for e in entries:
            local_path = self.local_dir / Path(e.rel_out_path.replace("/", os.sep))
            if not local_path.exists() or local_path.stat().st_size != e.size:
                to_process.append(e)
                continue
            if _sha256_file(local_path) != e.file_hash:
                to_process.append(e)
        return to_process

    def install(self, entries: List[FileEntry]) -> bool:
        if not entries:
            self.on_log("⚠️ Chunk-манифест пуст")
            return True

        self.on_log(f"📋 Файлов в chunk-манифесте: {len(entries)}")
        self.on_log("🔍 Сравниваем с локальными файлами...")
        to_process = self.diff(entries)

        if not to_process:
            self.on_log("✅ Все файлы актуальны")
            return True

        self.on_log(f"📦 Требует обновления: {len(to_process)} файлов")

        needed_chunks: Set[str] = {c.chunk_id for e in to_process for c in e.chunks}
        self.on_log(f"📦 Уникальных чанков к загрузке: {len(needed_chunks)}")

        chunk_cache: Dict[str, bytes] = {}
        failed_chunks: Set[str] = set()
        done = 0
        lock = threading.Lock()
        import time
        start_t = time.time()
        self.on_progress_max(len(needed_chunks))

        def _dl(cid: str):
            if self.should_stop():
                return cid, None
            self.wait_if_paused()
            return cid, self.client.download_chunk(cid)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_dl, cid): cid for cid in needed_chunks}
            for future in as_completed(futures):
                if self.should_stop():
                    for f in futures:
                        f.cancel()
                    self.on_log("⏹ Загрузка остановлена")
                    return False
                cid, data = future.result()
                with lock:
                    done += 1
                    self.on_progress(done)
                    self.on_current(f"чанк {cid[:8]}…")
                    elapsed = time.time() - start_t
                    if elapsed > 0:
                        total_bytes = sum(len(v) for v in chunk_cache.values())
                        self.on_speed(f"{total_bytes / elapsed / 1024 / 1024:.1f} MB/s")
                if data is not None:
                    chunk_cache[cid] = data
                else:
                    failed_chunks.add(cid)

        if failed_chunks:
            self.on_log(f"❌ Не удалось скачать {len(failed_chunks)} чанков")

        self.on_log("🔧 Собираем файлы из чанков...")
        ok_files, fail_files = 0, 0
        for e in to_process:
            if self.should_stop():
                return False
            missing = [c.chunk_id for c in e.chunks if c.chunk_id not in chunk_cache]
            if missing:
                self.on_log(f"⚠️ Пропускаем {e.rel_out_path} — недостаёт {len(missing)} чанков")
                fail_files += 1
                continue

            out_path = self.local_dir / Path(e.rel_out_path.replace("/", os.sep))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(out_path, "wb") as f:
                    for c in sorted(e.chunks, key=lambda c: c.offset):
                        f.write(chunk_cache[c.chunk_id])
            except Exception as ex:
                self.on_log(f"❌ Ошибка записи {e.rel_out_path}: {ex}")
                fail_files += 1
                continue

            if _sha256_file(out_path) != e.file_hash:
                self.on_log(f"❌ Верификация провалена: {e.rel_out_path}")
                fail_files += 1
                continue
            ok_files += 1

        removed = self._cleanup_extra_files(entries)
        if removed:
            self.on_log(f"🗑 Удалено лишних файлов: {removed}")

        if fail_files:
            self.on_log(f"❌ Установка завершена с ошибками: {ok_files} ок, {fail_files} ошибок")
            return False
        self.on_log(f"✅ Установка завершена: {ok_files} файлов")
        return True

    def _cleanup_extra_files(self, entries: List[FileEntry]) -> int:
        manifest_paths = {
            str(Path(e.rel_out_path.replace("/", os.sep))) for e in entries
        }
        removed = 0
        for f in self.local_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(self.local_dir))
                if rel not in manifest_paths:
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass
        return removed
