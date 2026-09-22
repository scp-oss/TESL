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


def _fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:   # NaN check без импорта math
        return "?"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}ч{m:02d}м"
    if m:
        return f"{m}м{s:02d}с"
    return f"{s}с"


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

        Сборка файла (запись на диск + верификация хэша) идёт в ОТДЕЛЬНОМ,
        меньшем пуле потоков (`assemble_pool`), не в том же потоке, что
        разбирает `as_completed()` для загрузок — см. CLAUDE.md "Сборка
        файлов блокировала параллельную загрузку чанков" за полную историю
        находки. Раньше `_assemble()` вызывался синхронно ВНУТРИ `with
        lock:` в потоке, читающем `as_completed()` — дисковая запись +
        повторное чтение+хэш КАЖДОГО файла (на этой сборке — 173К файлов)
        сериализовались на ОДНОМ потоке, а не на 24 воркерах загрузки, и
        именно это, не сеть, было реальным узким местом (эмпирически: рост
        `CHUNK_MAX_WORKERS`/`pool_maxsize` 8->24/16->48 почти не сдвинул
        скорость с ~10-13 MB/s на гигабитном SSD — то, что должно было
        сильно помочь, не помогло, это и есть сигнал, что бутылочное
        горлышко было не в числе соединений).
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
        # chunk_size — нужен для точного total_bytes/ETA по байтам, не по
        # числу чанков (размер чанка варьируется, до 4MB — см. config.py).
        chunk_to_files: Dict[str, List[int]] = {}
        chunk_size:     Dict[str, int] = {}
        remaining: List[int] = []          # remaining[i] = сколько чанков файла i ещё не пришло
        doomed: List[bool] = []            # файл никогда не соберётся (пропал чанк)
        for i, e in enumerate(to_process):
            unique_ids = {c.chunk_id for c in e.chunks}
            remaining.append(len(unique_ids))
            doomed.append(False)
            for c in e.chunks:
                chunk_to_files.setdefault(c.chunk_id, [])
                chunk_size[c.chunk_id] = c.size
            for cid in unique_ids:
                chunk_to_files[cid].append(i)

        chunk_refcount: Dict[str, int] = {cid: len(files) for cid, files in chunk_to_files.items()}
        needed_chunks: Set[str] = set(chunk_to_files.keys())
        total_bytes = sum(chunk_size[cid] for cid in needed_chunks)
        self.on_log(f"📦 Уникальных чанков к загрузке: {len(needed_chunks)} ({_fmt_size(total_bytes)})")

        chunk_cache: Dict[str, bytes] = {}
        failed_chunk_ids: Set[str] = set()
        done = 0
        downloaded_bytes = 0
        written_bytes = 0
        ok_files, fail_files = 0, 0
        lock = threading.Lock()
        import time
        start_t = time.time()
        last_speed_log_t = 0.0   # для троттлинга on_speed() — см. комментарий ниже
        self.on_progress_max(len(needed_chunks))

        # Пул для сборки файлов — отдельный от пула загрузки (self.max_workers,
        # обычно 24, см. config.CHUNK_MAX_WORKERS), поменьше: сборка — это
        # диск+CPU (запись + sha256), не сеть, и на SSD должна успевать за
        # 24 сетевых воркера с большим запасом даже на 4-8 потоках.
        assemble_workers = min(8, max(4, os.cpu_count() or 4))

        def _dl(cid: str):
            if self.should_stop():
                return cid, None
            self.wait_if_paused()
            return cid, self.client.download_chunk(cid)

        def _release_chunk(cid: str):
            # Вызывается ТОЛЬКО под `lock` — либо изнутри _assemble() (свой
            # `with lock:` в конце), либо из ветки "чанк не скачался" в
            # главном цикле (уже под lock там).
            chunk_refcount[cid] -= 1
            if chunk_refcount[cid] <= 0:
                chunk_cache.pop(cid, None)

        def _assemble(i: int):
            """Выполняется в assemble_pool, НЕ в главном потоке. Чтение
            chunk_cache[...] ниже безопасно без lock — чанки этого файла
            остаются в кэше, пока их refcount не понизит _release_chunk()
            НИЖЕ, в этой же функции, ПОСЛЕ чтения; ни один другой поток не
            может вытеснить их раньше (никто другой не звонит _release_chunk
            для чанков, которые всё ещё нужны именно этому файлу)."""
            nonlocal ok_files, fail_files, written_bytes
            e = to_process[i]
            out_path = self.local_dir / Path(e.rel_out_path.replace("/", os.sep))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            ok = True
            h = hashlib.sha256()
            file_bytes = 0
            try:
                with open(out_path, "wb") as f:
                    for c in sorted(e.chunks, key=lambda c: c.offset):
                        data = chunk_cache[c.chunk_id]
                        f.write(data)
                        h.update(data)
                        file_bytes += len(data)
            except Exception as ex:
                # repr(), не str() — живой случай 2026-09-22: str(ex) может
                # быть ПУСТОЙ строкой (напр. голый `MemoryError()` без
                # аргументов даёт str() == "") — тогда лог показывал
                # "Ошибка записи <путь>: " без единого намёка, что
                # случилось. repr() всегда включает имя класса исключения
                # даже при пустом сообщении.
                self.on_log(f"❌ Ошибка записи {e.rel_out_path}: {type(ex).__name__}: {ex!r}")
                ok = False

            if ok and h.hexdigest() != e.file_hash:
                # Хэш считается ПРЯМО ПО ЗАПИСАННЫМ байтам, без повторного
                # чтения с диска (раньше — _sha256_file(out_path), лишний
                # проход диска на все 171GB суммарно). Каждый чанк уже
                # верифицирован по sha256 индивидуально в
                # DepotClient.download_chunk() — эта проверка ловит только
                # неправильную СБОРКУ (порядок/пропуск чанка), не повреждение
                # диска постфактум; для этого есть отдельная "Проверить
                # файлы" (VerifyWorker) по требованию.
                self.on_log(f"❌ Верификация провалена: {e.rel_out_path}")
                ok = False

            with lock:
                if ok:
                    ok_files += 1
                    written_bytes += file_bytes
                else:
                    fail_files += 1
                for c in e.chunks:
                    _release_chunk(c.chunk_id)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool, \
             ThreadPoolExecutor(max_workers=assemble_workers) as assemble_pool:
            futures = {pool.submit(_dl, cid): cid for cid in needed_chunks}
            for future in as_completed(futures):
                if self.should_stop():
                    for f in futures:
                        f.cancel()
                    self.on_log("⏹ Загрузка остановлена")
                    return False
                cid, data = future.result()
                ready: List[int] = []   # файлы, готовые к сборке — submit'ятся В assemble_pool ПОСЛЕ lock, не внутри него
                with lock:
                    done += 1
                    self.on_progress(done)

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

                        for i in chunk_to_files[cid]:
                            if doomed[i]:
                                continue
                            remaining[i] -= 1
                            if remaining[i] == 0:
                                ready.append(i)

                    # ── Статус-бар: процент + скорость загрузки/записи + ETA
                    # (прямой запрос пользователя — раньше здесь была строка
                    # "чанк <хэш>", бесполезная для оценки прогресса).
                    # Считается на каждый чанк (дёшево — только форматирование
                    # строки), но пишется в ЛОГ (on_speed) не чаще раза в
                    # секунду — раньше on_speed слался на КАЖДЫЙ чанк (до
                    # 155К записей в лог на одну установку — видно в скрине
                    # пользователя, стена из "⬇ X MB/s"), теперь только
                    # статус-бар обновляется так часто, лог — раз в секунду.
                    elapsed = time.time() - start_t
                    pct = int(done / len(needed_chunks) * 100) if needed_chunks else 100
                    dl_speed_bps = downloaded_bytes / elapsed if elapsed > 0 else 0
                    wr_speed_bps = written_bytes / elapsed if elapsed > 0 else 0
                    bytes_remaining = total_bytes - downloaded_bytes
                    eta = _fmt_eta(bytes_remaining / dl_speed_bps) if dl_speed_bps > 0 else "?"
                    self.on_current(
                        f"{pct}% — ⬇ {dl_speed_bps / 1024 / 1024:.1f} MB/s / "
                        f"💾 {wr_speed_bps / 1024 / 1024:.1f} MB/s — осталось: {eta}"
                    )
                    if elapsed - last_speed_log_t >= 1.0:
                        last_speed_log_t = elapsed
                        self.on_speed(f"{dl_speed_bps / 1024 / 1024:.1f} MB/s")

                for i in ready:
                    assemble_pool.submit(_assemble, i)
        # Оба `with`-пула закрылись здесь — assemble_pool.__exit__ дожидается
        # ВСЕХ поставленных _assemble(), так что к этому месту сборка файлов
        # тоже реально завершена, не только скачивание чанков.

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
