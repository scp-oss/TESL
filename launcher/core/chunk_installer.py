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
        """
        Скачивает чанки и СРАЗУ собирает файл, как только приходит его
        последний нужный чанк — не держит весь набор чанков в памяти до
        конца загрузки. Иначе для крупной сборки (эта — 171GB) память
        росла бы неограниченно, пока не скачается вообще всё, прежде чем
        на диск попадёт хоть один байт — на обычной машине гарантированный
        OOM задолго до завершения. Чанк держится в кэше, только пока хотя
        бы один ещё не собранный файл на него ссылается (refcount),
        освобождается сразу после сборки последнего такого файла.
        """
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

        # Для каждого чанка — какие файлы (по индексу в to_process) его ждут,
        # и refcount = сколько из них ещё не собрано (=не освободили чанк).
        chunk_to_files: Dict[str, List[int]] = {}
        remaining: List[int] = []          # remaining[i] = сколько чанков файла i ещё не пришло
        doomed: List[bool] = []            # файл никогда не соберётся (пропал чанк)
        for i, e in enumerate(to_process):
            unique_ids = {c.chunk_id for c in e.chunks}
            remaining.append(len(unique_ids))
            doomed.append(False)
            for cid in unique_ids:
                chunk_to_files.setdefault(cid, []).append(i)

        chunk_refcount: Dict[str, int] = {cid: len(files) for cid, files in chunk_to_files.items()}
        needed_chunks: Set[str] = set(chunk_to_files.keys())
        self.on_log(f"📦 Уникальных чанков к загрузке: {len(needed_chunks)}")

        chunk_cache: Dict[str, bytes] = {}
        failed_chunk_ids: Set[str] = set()
        done = 0
        downloaded_bytes = 0
        ok_files, fail_files = 0, 0
        lock = threading.Lock()
        import time
        start_t = time.time()
        self.on_progress_max(len(needed_chunks))

        def _dl(cid: str):
            if self.should_stop():
                return cid, None
            self.wait_if_paused()
            return cid, self.client.download_chunk(cid)

        def _release_chunk(cid: str):
            chunk_refcount[cid] -= 1
            if chunk_refcount[cid] <= 0:
                chunk_cache.pop(cid, None)

        def _assemble(i: int):
            nonlocal ok_files, fail_files
            e = to_process[i]
            out_path = self.local_dir / Path(e.rel_out_path.replace("/", os.sep))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            ok = True
            try:
                with open(out_path, "wb") as f:
                    for c in sorted(e.chunks, key=lambda c: c.offset):
                        f.write(chunk_cache[c.chunk_id])
            except Exception as ex:
                self.on_log(f"❌ Ошибка записи {e.rel_out_path}: {ex}")
                ok = False

            if ok and _sha256_file(out_path) != e.file_hash:
                self.on_log(f"❌ Верификация провалена: {e.rel_out_path}")
                ok = False

            if ok:
                ok_files += 1
            else:
                fail_files += 1

            for c in e.chunks:
                _release_chunk(c.chunk_id)

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

                    if data is None:
                        failed_chunk_ids.add(cid)
                        # Все файлы, ждавшие этот чанк, обречены — не будут
                        # собраны. Освобождаем их прочие чанки сразу, не
                        # дожидаясь конца (иначе они держали бы память зря).
                        for i in chunk_to_files[cid]:
                            if doomed[i]:
                                continue
                            doomed[i] = True
                            fail_files += 1
                            for other in to_process[i].chunks:
                                if other.chunk_id != cid and other.chunk_id in chunk_refcount:
                                    _release_chunk(other.chunk_id)
                    else:
                        chunk_cache[cid] = data
                        downloaded_bytes += len(data)
                        elapsed = time.time() - start_t
                        if elapsed > 0:
                            self.on_speed(f"{downloaded_bytes / elapsed / 1024 / 1024:.1f} MB/s")

                        for i in chunk_to_files[cid]:
                            if doomed[i]:
                                continue
                            remaining[i] -= 1
                            if remaining[i] == 0:
                                _assemble(i)

        if failed_chunk_ids:
            self.on_log(f"❌ Не удалось скачать {len(failed_chunk_ids)} чанков — часть файлов пропущена")

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
