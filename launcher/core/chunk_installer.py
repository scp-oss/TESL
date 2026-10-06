# ==================== launcher/core/chunk_installer.py ====================
"""
Устанавливает сборку из chunk-манифеста — диф по хэшу, докачка недостающих
чанков (с дедупом), сборка файлов по offset, верификация sha256 целиком,
чистка лишнего.

Переписан 2026-09-30 вокруг `_ChunkCache` — раньше refcount/эвикт чанка из
памяти были размазаны по install() тремя разными местами (успешная сборка,
"обречённый" файл при неудачной закачке, учёт байт под потолок памяти),
каждое со своей копией дедупа и своим риском разойтись при следующей
правке — именно так один раз уже случился KeyError (чанк встречается в
`e.chunks` файла дважды на разных offset, "release" звали дважды). Теперь
это одна точка: `_ChunkCache.put/get_many/release`, `release()` дедупит
сама — вызывающему коду физически негде забыть это сделать. Логика внутри
та же, что уже проверена живыми инцидентами (см. CLAUDE.md за полную
историю каждого найденного класса бага); переписан не алгоритм, а то, как
он инкапсулирован.

Намеренно БЕЗ Qt-зависимости — `core/workers.py::DownloadWorker` оборачивает
этот класс в Qt-сигналы через колбэки, сам ничего не решает.

2026-10-06, прямой запрос пользователя, три связанные правки:
- Кеш чанков (`_ChunkCache`) переехал с системного temp в
  `<local_dir>/cache/chunks` — "желательно не в темп, а в той же папке
  где находится сборка". Чистится с нуля на каждый install() (ephemeral
  в пределах одного прогона); `_cleanup_extra_files()` явно исключает
  весь `cache/` из метлы "лишних файлов", иначе собственный журнал
  резюме (см. ниже) удалялся бы на каждой установке.
- `resume_state.jsonl` (см. `_load_resume_done()`/`_ResumeStateWriter`)
  — журнал файлов, чья sha256-верификация уже ПОДТВЕРЖДЕНА прошлым
  прогоном; `diff()` пропускает дорогой полный хэш для них (только
  дешёвую проверку существование+размер) — пауза+закрытие+повторный
  запуск лаунчера больше не перехэшируют каждый уже корректный файл
  заново. Запись в журнал — СТРОГО после успешной верификации в
  `_assemble()`, никогда раньше: если процесс падает до этого момента,
  записи просто не будет, и diff() обработает файл как обычно (не
  "доверие вслепую" частично записанному файлу).
- Прогрессивная очистка кэша по чанку (`_ChunkCache.release()`) — не
  новая правка, уже существовала; подтверждена повторным разбором и
  явно задокументирована здесь по прямому вопросу пользователя,
  вырастет ли кэш до полного объёма сборки — нет, не вырастет: чанк
  удаляется с диска сразу, как только последний файл, которому он
  нужен, либо собрался (успешно или неуспешно), либо был "обречён"
  из-за неудачной закачки — см. `release()`'s собственный докстринг.
"""

import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from core.chunk_manifest_db import FileEntry


def _win_long_path(path: Path) -> str:
    """Windows MAX_PATH (260 симв.) — реальная стена для глубоко вложенных
    деревьев модов (MO2p/mods/<длинное имя>/textures/.../<длинный файл>.dds).
    `OSError(22, 'Invalid argument')` от обычного open()/mkdir() на таком
    пути — это ОС отказывается работать с путём длиннее лимита, не диск и
    не антивирус. Префикс `\\\\?\\` пускает вызов через Win32 extended-length
    API, у которого этого лимита нет. Не трогает ничего на не-Windows."""
    if os.name != "nt":
        return str(path)
    s = str(path.resolve())
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(_win_long_path(path), "rb") as f:
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


_RESUME_STATE_FILENAME = "resume_state.jsonl"


def _load_resume_done(cache_dir: Path) -> Dict[str, str]:
    """{rel_out_path: file_hash} уже ПОДТВЕРЖДЁННО (пост-верификация,
    см. _ResumeStateWriter.mark_done()) собранных файлов из ПРЕДЫДУЩЕГО
    прогона install() — см. diff()'s `resume_done` параметр за то, как
    это используется, чтобы не перехэшировать 170К+ файлов заново
    только потому, что пользователь поставил установку на паузу и
    закрыл лаунчер (живой запрос пользователя 2026-10-06).

    JSONL, не один JSON-блоб — добавление записи должно быть O(1)
    (append+flush), не переписыванием всего файла на каждый из 170К+
    файлов сборки. Последняя строка на путь побеждает (на случай редкой
    гонки двух процессов дописывающих один файл — не ожидается в норме,
    один install() на сборку за раз, но дешёво защититься построением
    словаря так, чтобы порядок строк сам решал конфликт предсказуемо).
    Отсутствие файла/битая строка — не ошибка, просто "истории нет",
    diff() в этом случае ведёт себя как раньше (полный хэш)."""
    path = cache_dir / _RESUME_STATE_FILENAME
    done: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    done[rec["path"]] = rec["hash"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # битая строка (обрыв записи при крэше) — просто пропускаем её
    except OSError:
        pass
    return done


class _ResumeStateWriter:
    """Append-only журнал "файл X с хэшем Y полностью собран и
    верифицирован" — ПИШЕТСЯ ТОЛЬКО когда _assemble() уже подтвердил
    sha256 всего файла (ok=True), никогда раньше. Это и есть гарантия
    безопасности против живого риска "частично записанный файл после
    крэша тихо принят как готовый": если процесс падает ДО того, как
    эта строка попала на диск (в любой момент до полной верификации),
    при следующем запуске записи просто не будет — diff() увидит файл
    как обычно (размер не совпадёт с ожидаемым, или хэш не совпадёт) и
    перекачает/пересоберёт его заново, как и должно быть. Запись только
    ПОСЛЕ верификации — единственное, что делает пропуск повторного
    хэша на следующем запуске безопасным, не "доверием на слово"."""

    def __init__(self, cache_dir: Path):
        self._lock = threading.Lock()
        self._path = cache_dir / _RESUME_STATE_FILENAME
        try:
            self._f = open(self._path, "a", encoding="utf-8")
        except OSError:
            self._f = None  # диск недоступен для записи — молча не персистим, install() всё равно работает

    def mark_done(self, rel_out_path: str, file_hash: str) -> None:
        if self._f is None:
            return
        line = json.dumps({"path": rel_out_path, "hash": file_hash}, ensure_ascii=False)
        with self._lock:
            try:
                self._f.write(line + "\n")
                self._f.flush()
            except OSError:
                pass

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass


def _reorder_for_disk_locality(chunk_order: List[str], client, max_workers: int) -> List[str]:
    """Живой инцидент 2026-09-30: 134GB сборка "виснет" на скачивании —
    chunk_order по файлам (см. _plan_chunk_order ниже) не связан с
    физическим расположением чанков на сервере, 24 параллельных Range-GET
    бьют по случайным офсетам HDD. Локально пересортировывает каждое окно
    из ~4×max_workers подряд идущих элементов по (pack, offset), если
    клиент умеет отдавать расположение (`get_chunk_location()` — только у
    PanelDepotClient); легаси/тестовые клиенты без этого метода получают
    порядок без изменений (no-op через getattr)."""
    get_loc = getattr(client, "get_chunk_location", None)
    if get_loc is None:
        return chunk_order
    window = max(64, max_workers * 4)
    result: List[str] = []
    for start in range(0, len(chunk_order), window):
        batch = chunk_order[start:start + window]
        locs = {cid: get_loc(cid) for cid in batch}
        batch.sort(key=lambda cid: (locs[cid] is None, locs[cid] or ("", 0)))
        result.extend(batch)
    return result


_BATCH_BYTES = 8 * 1024 * 1024
_BATCH_GAP_BYTES = 256 * 1024


def _group_for_batch(chunk_order: List[str], client, batch_bytes: int, gap_bytes: int) -> List[List[str]]:
    """Группирует УЖЕ реордеренный (`_reorder_for_disk_locality`)
    chunk_order в задачи на закачку — список из ≥2 chunk_id, физически
    смежных в одном pack (зазор <= gap_bytes, суммарный диапазон <=
    batch_bytes), скачивается ОДНИМ Range-GET вместо одного на чанк.
    Живой повод 2026-10-02: пользователь написал и прогнал собственный
    `batch_bench.py` против реального `tesl-panel.neth.de5.net`/
    `transport.neth.de5.net` — батч по 8МБ дал ~1.2x к скорости и втрое
    меньше запросов на ОБОИХ доменах (CF-proxied и bypass), поверх уже
    переключённого bypass-домена — независимый, аддитивный выигрыш.

    Линейный проход по ВСЕМУ chunk_order, без повторного оконного
    разбиения — `_reorder_for_disk_locality()` уже сгруппировал
    физически смежные чанки внутри каждого своего окна, так что
    последовательные элементы массива, оказавшиеся физически смежными,
    и так уже стоят рядом; группировка просто склеивает то, что
    оказалось рядом ПОСЛЕ той пересортировки. На границе окна
    пересортировки физическая смежность естественно обрывается (каждое
    окно сортируется независимо с произвольной начальной точки) —
    отдельного ограничения по размеру окна здесь не нужно, сам факт
    "не смежно" уже рвёт группу; batch_bytes остаётся отдельным
    потолком на случай, если внутри окна есть длинная смежная цепочка.

    Клиент без `get_chunk_location_full` (легаси WebDAV/тестовые
    дублёры) получает ВСЕ задачи как одиночные chunk_id (тот же
    getattr-детект, что уже использует `_reorder_for_disk_locality`) —
    нулевой риск для уже проверенного пути: каждая "пачка" из одного
    элемента ведёт себя идентично старому поведению по чанку."""
    get_loc = getattr(client, "get_chunk_location_full", None)
    if get_loc is None:
        return [[cid] for cid in chunk_order]
    tasks: List[List[str]] = []
    cur_pack: Optional[str] = None
    cur_start = cur_end = None
    cur_group: List[str] = []
    for cid in chunk_order:
        loc = get_loc(cid)
        if loc is None:
            if cur_group:
                tasks.append(cur_group)
                cur_group = []
            tasks.append([cid])
            cur_pack = cur_start = cur_end = None
            continue
        pack, off, size = loc
        if (cur_pack == pack and cur_end is not None
                and off - cur_end <= gap_bytes
                and max(cur_end, off + size) - cur_start <= batch_bytes):
            cur_group.append(cid)
            cur_end = max(cur_end, off + size)
        else:
            if cur_group:
                tasks.append(cur_group)
            cur_pack, cur_start, cur_end = pack, off, off + size
            cur_group = [cid]
    if cur_group:
        tasks.append(cur_group)
    return tasks


def _plan_chunk_order(to_process: List[FileEntry], file_chunk_ids: List[Set[str]]) -> List[str]:
    """Порядок закачки — ПО ФАЙЛАМ (файлы с меньшим числом уникальных
    чанков первыми), не по хэшу чанка. Живой инцидент 2026-09-23: порядок
    по хэшу не связан с тем, какому файлу чанк принадлежит — почти ни один
    файл не успевал собраться и освободить кэш, память росла
    неограниченно (2.5ГБ -> 9ГБ+). Идя по файлам, они начинают
    завершаться почти сразу и непрерывно. Уже виденный chunk_id не
    добавляется повторно — качается один раз, для самого раннего
    нуждающегося файла."""
    file_order = sorted(range(len(to_process)), key=lambda i: len(file_chunk_ids[i]))
    order: List[str] = []
    seen: Set[str] = set()
    for i in file_order:
        for cid in sorted(file_chunk_ids[i]):
            if cid not in seen:
                seen.add(cid)
                order.append(cid)
    return order


def _pack_layout_is_file_ordered(to_process: List[FileEntry], client, sample_files: int = 50) -> bool:
    """Эвристика: сэмплирует до `sample_files` файлов с ≥2 уникальными
    чанками и проверяет, лежат ли их чанки в ОДНОМ pack подряд по
    offset'у — тот же сигнал, что `batch_bench.py`'s `sim`-режим уже
    посчитал на реальных данных ("соседние по offset'у чанки ОДНОГО
    файла лежат в pack ВСТЫК") — 0.0% означает классическую раскладку
    `pack_writer.py` по `sorted(chunk_id)` (по хэшу публикации, см.
    "Batch-Range закачка чанков..." в CLAUDE.md), близко к 100% — более
    новую раскладку, где контент каждого файла физически смежен в
    одном pack. Порог 80% — не 100%, чтобы редкие "рваные" случаи
    (шаринг чанка между файлами, чанк на границе pack) не отключали
    оптимизацию из-за единичных исключений.

    Выбор стратегии дальше (см. `install()`) — при файловой раскладке
    чтение пачек ПОСЛЕДОВАТЕЛЬНО безопасно и для памяти (чанки файла и
    так приходят рядом по времени, раз физически смежны), и для диска
    сервера; пересортировка "маленькие файлы первыми"
    (`_plan_chunk_order`, живой OOM-фикс 2026-09-23) в этом случае не
    нужна и только мешала бы последовательному чтению pack-файлов.

    getattr-детект — клиент без `get_chunk_location_full()` (легаси
    WebDAV, тестовые дублёры) не умеет ответить, трактуется как "не
    файловая раскладка" — безопасный, уже проверенный путь ниже."""
    get_loc = getattr(client, "get_chunk_location_full", None)
    if get_loc is None:
        return False
    pairs = adjacent = 0
    checked = 0
    for e in to_process:
        if len(e.chunks) < 2:
            continue
        checked += 1
        if checked > sample_files:
            break
        chunks = sorted(e.chunks, key=lambda c: c.offset)
        for a, b in zip(chunks, chunks[1:]):
            la = get_loc(a.chunk_id)
            lb = get_loc(b.chunk_id)
            if la is None or lb is None:
                continue
            pairs += 1
            if la[0] == lb[0] and 0 <= lb[1] - (la[1] + la[2]) <= _BATCH_GAP_BYTES:
                adjacent += 1
    if pairs == 0:
        return False
    return (adjacent / pairs) >= 0.8


def _plan_chunk_order_packed(to_process: List[FileEntry], file_chunk_ids: List[Set[str]], client) -> List[str]:
    """Порядок закачки для файлово-ориентированной раскладки pack (см.
    `_pack_layout_is_file_ordered()`) — читает паки ПОСЛЕДОВАТЕЛЬНО
    (глобальная сортировка по `(pack, offset)`, без оконного реордера
    `_reorder_for_disk_locality()` — он здесь избыточен, вся очередь уже
    в физическом порядке), а не "сначала маленькие файлы"
    (`_plan_chunk_order`). При этой раскладке оба требования — дисковая
    локальность на сервере и память на клиенте — совпадают сами собой:
    раз чанки каждого файла физически смежны в одном pack,
    последовательное чтение и так приносит чанки каждого файла рядом
    друг с другом по времени, искусственно переставлять файлы по
    размеру не нужно.

    Дедуп общих chunk_id (шаринг между файлами) — тот же принцип, что в
    `_plan_chunk_order()`: чанк попадает в очередь один раз, порядок
    между файлами здесь не важен (в отличие от того фикса) — позицию в
    итоговой очереди определяет ТОЛЬКО физическое расположение.

    Клиент без `get_chunk_location_full()` сюда попасть не должен —
    вызывающий код сам решает через `_pack_layout_is_file_ordered()` —
    но если всё же попал, безопасный фолбэк на `_plan_chunk_order()`."""
    get_loc = getattr(client, "get_chunk_location_full", None)
    if get_loc is None:
        return _plan_chunk_order(to_process, file_chunk_ids)
    seen: Set[str] = set()
    all_ids: List[str] = []
    for ids in file_chunk_ids:
        for cid in sorted(ids):
            if cid not in seen:
                seen.add(cid)
                all_ids.append(cid)

    def _key(cid: str):
        loc = get_loc(cid)
        if loc is None:
            return (1, "", 0)
        pack, off, _size = loc
        return (0, pack, off)

    all_ids.sort(key=_key)
    return all_ids


class _ChunkCache:
    """Владеет чанками (на диске, не в RAM — см. ниже) и их refcount'ом
    за одним lock'ом — вся разделяемая изменяемая память установки в
    одном месте. `release()` дедупит свой аргумент сама (`set(cids)`),
    так что вызывающий код не может забыть это сделать — именно забытый
    дедуп был причиной живого KeyError 2026-09-22 (чанк на двух
    offset'ах одного файла — release() звали дважды, refcount уходил в
    минус раньше времени, чанк вытеснялся из кэша, пока другой файл на
    него ещё ссылался). Второе следствие инкапсуляции: doom-путь (чанк
    не скачался) и success-путь (файл собрался) теперь зовут ОДИН и тот
    же `release()` — `put()` для незагрузившегося чанка просто никогда
    не вызывается, так что `release()` на него — безопасный no-op (файла
    на диске нет, байтовый счётчик не трогается), а не нужна отдельная
    ветка "исключить этот cid", как было раньше.

    **Диск, не RAM, с 2026-10-06** — живой инцидент: реальная установка
    на 134ГБ-сборке держала Python-процесс на 11.7+ ГБ ОЗУ, растущих
    без остановки, несмотря на номинальный `bytes_limit=768MB`. Прямое
    предложение пользователя ("сделать папку кеш... выгружается в сыром
    виде, а потом собирается") — `put()` теперь пишет байты чанка на
    диск (в `cache_dir`, который передаёт `install()` — с 2026-10-06 это
    `<local_dir>/cache/chunks`, не системный temp, см. install()'s
    собственный комментарий за причину), не держит их в питоновском
    `dict`; `get_many()` читает их обратно с диска прямо перед сборкой
    файла. Разница в нагрузке на диск клиента —
    ничтожная (один write + один read на чанк, в среднем несколько
    сотен КБ, против самого сетевого скачивания и последующей записи в
    целевой файл, которые и так уже происходят) — тот же базовый
    компромисс, что уже многократно применялся на СЕРВЕРНОЙ стороне
    этого проекта (pack-файлы и т.п.), просто теперь и на клиенте. Это
    не чинит гипотетическую первопричину того, почему `bytes_limit`
    раньше не удерживал RAM в разумных пределах (не до конца
    локализовано статическим анализом) — но делает её неважной: сколько
    бы чанков ни оказалось одновременно "в работе", в памяти процесса
    лежат только метаданные (refcount/размер), не сами байты, так что
    рост RAM в принципе ограничен сверху независимо от этого."""

    def __init__(
        self, bytes_limit: int, cache_dir: Path,
        debug_log: Optional[Callable[[str], None]] = None,
    ):
        self._lock = threading.Lock()
        self._sizes: Dict[str, int] = {}
        self._refcount: Dict[str, int] = {}
        self.bytes_limit = bytes_limit
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._bytes = 0
        # debug_log — построчная трассировка put/release с итоговым
        # refcount'ом, по прямому запросу пользователя 2026-09-30 ("пиши
        # сразу с вшытым дебаг... так вычислим где дыра"): если та же
        # KeyError-класса гонка когда-нибудь вернётся, в LOG_FILE будет
        # видно ТОЧНУЮ последовательность put/release вокруг проблемного
        # chunk_id, а не только финальный симптом. None в обычном режиме —
        # ни единой лишней строки/проверки на горячем пути.
        self._debug_log = debug_log

    def _path(self, cid: str) -> Path:
        return self.cache_dir / cid

    def plan(self, cid: str, needed_by: int) -> None:
        """Вызывать для каждого уникального chunk_id ДО старта закачки —
        needed_by = число РАЗНЫХ файлов, которым он нужен."""
        self._refcount[cid] = needed_by

    def put(self, cid: str, data: bytes) -> None:
        # Пишем на диск ВНЕ lock'а (сам I/O не нуждается в синхронизации —
        # у каждого cid свой файл, разные потоки пишут разные файлы) —
        # под lock только обновление метаданных, тот же принцип
        # минимизации времени удержания lock'а, что был и раньше.
        self._path(cid).write_bytes(data)
        with self._lock:
            self._sizes[cid] = len(data)
            self._bytes += len(data)
            if self._debug_log:
                self._debug_log(
                    f"🐛 cache.put {cid[:12]} {len(data)}B refcount={self._refcount.get(cid)} "
                    f"cache_total={self._bytes}B"
                )

    def get_many(self, cids: List[str]) -> Optional[List[bytes]]:
        """bytes для каждого cid по порядку (читает с диска), либо None
        если хоть один отсутствует (не должно происходить, пока
        инварианты планирования держатся — отсутствие сигнализирует
        вызывающему код как ошибку). Чтение — тоже вне lock'а (сам файл
        к этому моменту уже дописан и неизменен, читать его параллельно
        с чем угодно безопасно; lock нужен только метаданным)."""
        paths = []
        with self._lock:
            for cid in cids:
                if cid not in self._sizes:
                    if self._debug_log:
                        self._debug_log(f"🐛 cache.get_many: {cid[:12]} ОТСУТСТВУЕТ (refcount={self._refcount.get(cid)})")
                    return None
                paths.append(self._path(cid))
        try:
            return [p.read_bytes() for p in paths]
        except OSError as e:
            if self._debug_log:
                self._debug_log(f"🐛 cache.get_many: ошибка чтения с диска: {e!r}")
            return None

    def release(self, cids: Iterable[str]) -> None:
        with self._lock:
            for cid in set(cids):
                self._refcount[cid] = self._refcount.get(cid, 1) - 1
                new_rc = self._refcount[cid]
                evicted = False
                if new_rc <= 0:
                    size = self._sizes.pop(cid, None)
                    if size is not None:
                        self._bytes -= size
                        evicted = True
                        try:
                            self._path(cid).unlink()
                        except OSError:
                            pass  # уже удалён/недоступен — не критично, временная папка чистится целиком в конце
                if self._debug_log:
                    self._debug_log(
                        f"🐛 cache.release {cid[:12]} refcount={new_rc}"
                        f"{' -> evicted' if evicted else ''}"
                    )

    @property
    def bytes_cached(self) -> int:
        with self._lock:
            return self._bytes

    def full(self) -> bool:
        with self._lock:
            return self._bytes >= self.bytes_limit


class ChunkInstaller:
    """
    `client` — любой объект с методом `download_chunk(chunk_id: str) ->
    Optional[bytes]` (обычно `core.panel_client.PanelDepotClient`, но для
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
        should_stop: Callable[[], bool] = lambda: False,
        wait_if_paused: Callable[[], None] = lambda: None,
        cache_bytes_limit: int = 768 * 1024 * 1024,
        debug: bool = False,
    ):
        self.client = client
        self.local_dir = Path(local_dir)
        self.max_workers = max_workers
        self.on_log = on_log
        self.on_progress_max = on_progress_max
        self.on_progress = on_progress
        self.on_current = on_current
        self.should_stop = should_stop
        self.wait_if_paused = wait_if_paused
        self.cache_bytes_limit = cache_bytes_limit
        # debug=True — построчная трассировка каждого чанка/файла через
        # on_log (и значит в LOG_FILE, и, если включена, на панель — см.
        # core/debug_log.py). Управляется настройкой "Режим отладки" в UI
        # (ui/main_window.py::_start_worker() подставляет её в task dict),
        # выключено по умолчанию — на 155К+ чанках это реально много
        # строк, включать только когда целенаправленно ищем дыру.
        self.debug = debug
        # Буфер debug-трассы — см. _dbg_emit() за живой повод (2026-09-30).
        self._dbg_lock = threading.Lock()
        self._dbg_buffer: List[str] = []
        self._dbg_last_flush = time.monotonic()

    def _dbg(self, msg: str) -> None:
        if self.debug:
            self._dbg_emit(msg)

    def _dbg_emit(self, msg: Optional[str], force: bool = False) -> None:
        """Живой инцидент 2026-09-30: на реальной установке (10772 файлов,
        16647 уникальных чанков) построчная debug-трасса — отдельный
        self.on_log() на КАЖДЫЙ `plan`/`dl`/`assemble`/`cache.put`/
        `cache.release` — производила десятки тысяч вызовов за секунды
        (один только цикл `plan` ниже отрабатывает 16647 раз подряд ДО
        старта самой закачки, синхронно, в одном потоке). `on_log` в
        реальном приложении (core/workers.py -> `self.log.emit`) доезжает
        до `ui/main_window.py::_append_log()` через auto-queued
        cross-thread соединение — КАЖДЫЙ вызов это отдельное событие в
        очереди GUI-потока плюс синхронная запись в LOG_FILE (открыть/
        записать/закрыть). GUI-поток захлёбывался этим потоком быстрее,
        чем успевал разгребать — выглядело как полное зависание всего
        приложения ("53.6 МБ и всё зависло" — прогресс-бар тоже идёт
        через тот же GUI-поток и той же живой лог показывал отставание),
        хотя фоновая загрузка, возможно, продолжала идти.

        Буферизуем и сбрасываем ОДНИМ вызовом on_log() не чаще раза в
        0.3с (или раз в 500 накопленных строк) — join'ом, ни одна строка
        не теряется, только группируются в пачки: весь смысл debug-режима
        именно в полноте трассы. Тот же класс фикса, что уже применялся
        для speed_update (см. CLAUDE.md «Дублирование строк скорости в
        логе»), здесь — впервые доведённый до самого debug-режима,
        которого раньше просто не существовало на момент того фикса."""
        with self._dbg_lock:
            if msg is not None:
                self._dbg_buffer.append(msg)
            if not self._dbg_buffer:
                return
            now = time.monotonic()
            if not force and (now - self._dbg_last_flush) < 0.3 and len(self._dbg_buffer) < 500:
                return
            batch = self._dbg_buffer
            self._dbg_buffer = []
            self._dbg_last_flush = now
        self.on_log("\n".join(batch))

    def diff(self, entries: List[FileEntry], resume_done: Optional[Dict[str, str]] = None) -> List[FileEntry]:
        """Сравнивает entries с локальными файлами (существование/размер/
        sha256). Параллелится отдельным пулом (diff_workers — диск+CPU, не
        сеть) и репортит прогресс/ETA скользящим окном — раньше шло
        однопоточно без единого признака прогресса на 173К файлах,
        выглядело как зависание (живая жалоба 2026-09-22).

        `resume_done` — {rel_out_path: file_hash} уже ПОДТВЕРЖДЁННО
        собранных файлов из прошлого прогона install() (см.
        _load_resume_done()/_ResumeStateWriter) — прямой запрос
        пользователя 2026-10-06: пауза+закрытие+повторный запуск
        лаунчера не должны заново перехэшировать каждый уже корректно
        стоящий файл сборки (дорого на 170К+ файлов). Для файла,
        найденного в этой карте С ТЕМ ЖЕ file_hash — всё ещё проверяем
        дёшево (существование+размер), но пропускаем дорогой полный
        sha256; расхождение означает, что что-то изменилось (другая
        версия поверх, файл тронули руками) — resume_done просто не
        матчится, обычная дорогая проверка срабатывает как раньше, это
        НЕ доверие вслепую."""
        to_process: List[FileEntry] = []
        total = len(entries)
        if total == 0:
            return to_process

        self.on_progress_max(total)
        done = 0
        last_sample_t = time.time()
        last_sample_done = 0
        smoothed_rate = 0.0
        resume_done = resume_done or {}

        def _check(e: FileEntry):
            local_path = self.local_dir / Path(e.rel_out_path.replace("/", os.sep))
            # os.path.*, не Path.exists()/.stat() — на длинных путях
            # (>260 симв., частые в глубоко вложенных деревьях модов)
            # Windows-версии pathlib-методов могут молча соврать/упасть
            # без \\?\-префикса; _win_long_path() ниже его добавляет.
            lp = _win_long_path(local_path)
            if not os.path.exists(lp) or os.path.getsize(lp) != e.size:
                return e, True
            if resume_done.get(e.rel_out_path) == e.file_hash:
                return e, False
            return e, _sha256_file(local_path) != e.file_hash

        diff_workers = min(8, max(4, os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=diff_workers) as pool:
            for e, needs in pool.map(_check, entries):
                if needs:
                    to_process.append(e)
                done += 1
                self.on_progress(done)

                now = time.time()
                sample_dt = now - last_sample_t
                if sample_dt >= 0.5 or done == total:
                    inst_rate = (done - last_sample_done) / sample_dt if sample_dt > 0 else 0.0
                    alpha = 0.3
                    smoothed_rate = inst_rate if smoothed_rate == 0 else (
                        alpha * inst_rate + (1 - alpha) * smoothed_rate)
                    last_sample_t = now
                    last_sample_done = done

                pct = int(done / total * 100)
                eta = _fmt_eta((total - done) / smoothed_rate) if smoothed_rate > 0 else "?"
                self.on_current(
                    f"Сравниваем с локальными файлами: {pct}% — {done}/{total} — осталось: {eta}"
                )

        return to_process

    def install(self, entries: List[FileEntry]) -> bool:
        """Скачивает чанки и СРАЗУ собирает файл, как только приходит его
        последний нужный чанк (streaming — не держит весь набор чанков в
        памяти до конца загрузки, иначе гарантированный OOM на крупной
        сборке). Сборка (запись+верификация) — в отдельном, меньшем пуле
        потоков (assemble_pool), не в том же потоке, что разбирает
        завершённые загрузки — иначе дисковая запись+хэш каждого файла
        сериализуется на одном потоке вместо N воркеров загрузки (это, а
        не сеть, было реальным узким местом — рост числа соединений
        8->24 почти не сдвинул скорость)."""
        if not entries:
            self.on_log("⚠️ Chunk-манифест пуст")
            return True

        # Папка кеша — ВНУТРИ самой сборки (<local_dir>/cache), не
        # системный TEMP: прямой запрос пользователя 2026-10-06 ("желательно
        # не в темп, а в той же папке где находится сборка"). `chunks/`
        # (сырые байты чанков "в работе", см. _ChunkCache) — чистим с нуля
        # на каждый install() (ephemeral в пределах одного прогона,
        # прошлый мусор от прерванного процесса нам не нужен и не
        # идентифицируем, какому chunk_id какой файл принадлежал бы без
        # повторного planning()). `resume_state.jsonl` (см. ниже) лежит
        # РЯДОМ, не внутри — его явно нужно переживать между прогонами,
        # ради чего всё это и делается.
        cache_dir = self.local_dir / "cache"
        chunks_dir = cache_dir / "chunks"
        shutil.rmtree(chunks_dir, ignore_errors=True)

        self.on_log(f"📋 Файлов в chunk-манифесте: {len(entries)}")
        self.on_log("🔍 Сравниваем с локальными файлами...")
        resume_done = _load_resume_done(cache_dir)
        if resume_done:
            self.on_log(f"⏩ Из прошлого прогона уже подтверждено: {len(resume_done)} файлов — хэш не пересчитываем")
        to_process = self.diff(entries, resume_done=resume_done)

        if not to_process:
            self.on_log("✅ Все файлы актуальны")
            return True

        to_process_bytes = sum(e.size for e in to_process)
        self.on_log(f"📦 Требует обновления: {len(to_process)} файлов ({_fmt_size(to_process_bytes)})")

        # Планирование: для каждого файла — уникальные chunk_id (дедуп
        # ЗДЕСЬ, один раз, за пределами любого async-кода — remaining[i]/
        # file_chunk_ids[i] строятся из этого же множества, так что вся
        # установка ниже работает с уже дедуплицированными наборами;
        # только сама СБОРКА файла (порядок записи байт на диск) читает
        # сырой e.chunks — там дубликат offset'а корректен и ожидаем.
        chunk_to_files: Dict[str, List[int]] = {}
        chunk_size: Dict[str, int] = {}
        remaining: List[int] = []
        doomed: List[bool] = []
        file_chunk_ids: List[Set[str]] = []
        for i, e in enumerate(to_process):
            unique_ids = {c.chunk_id for c in e.chunks}
            file_chunk_ids.append(unique_ids)
            remaining.append(len(unique_ids))
            doomed.append(False)
            for c in e.chunks:
                chunk_size[c.chunk_id] = c.size
            for cid in unique_ids:
                chunk_to_files.setdefault(cid, []).append(i)

        # "Сырые" байты чанков на диске — chunks_dir (см. cache_dir/
        # chunks_dir выше и _ChunkCache's докстринг за полную картину
        # живого инцидента 2026-10-06, который завёл дисковый кэш как
        # таковой) — ephemeral в пределах ЭТОГО прогона install(),
        # уже очищена с нуля выше. Чистится в конце install() (все три
        # выхода из функции ниже) — ЗДЕСЬ, а не в __init__/__del__
        # "_ChunkCache", раз именно install() владеет жизненным циклом
        # этого конкретного прогона. `resume_writer` (журнал подтверждённо
        # собранных файлов, см. _ResumeStateWriter) — отдельный от
        # chunks_dir файл, persist между прогонами, не чистится.
        cache = _ChunkCache(self.cache_bytes_limit, chunks_dir, debug_log=self._dbg_emit if self.debug else None)
        resume_writer = _ResumeStateWriter(cache_dir)
        for cid, files in chunk_to_files.items():
            cache.plan(cid, len(files))
            self._dbg(f"🐛 plan {cid[:12]} needed_by={len(files)} files={files}")

        needed_chunks: Set[str] = set(chunk_to_files.keys())
        total_bytes = sum(chunk_size[cid] for cid in needed_chunks)
        self.on_log(f"📦 Уникальных чанков к загрузке: {len(needed_chunks)} ({_fmt_size(total_bytes)})")

        failed_chunk_ids: Set[str] = set()
        done = 0
        downloaded_bytes = 0
        written_bytes = 0
        ok_files, fail_files = 0, 0
        stats_lock = threading.Lock()
        start_t = time.time()
        last_sample_t = start_t
        last_sample_downloaded = 0
        last_sample_written = 0
        smoothed_dl_speed = 0.0
        smoothed_wr_speed = 0.0
        self.on_progress_max(len(needed_chunks))

        # Сборка — диск+CPU, не сеть, отдельный меньший пул. Потолок 4
        # (не os.cpu_count() без ограничения) — живой случай 2026-09-22:
        # DRAM-less NVMe под длительной записью на маленьком SLC-кэше
        # ощутимо проседает по отклику; если это тот же физический диск,
        # что системный — тормозит вообще всю систему, не только установку.
        assemble_workers = min(4, max(2, os.cpu_count() or 4))

        def _dl_batch(cids: List[str]) -> Dict[str, Optional[bytes]]:
            if self.should_stop():
                return {cid: None for cid in cids}
            self.wait_if_paused()
            batch_fn = getattr(self.client, "download_chunks_batched", None)
            if batch_fn is not None:
                return batch_fn(cids)
            return {cid: self.client.download_chunk(cid) for cid in cids}

        def _assemble(i: int) -> None:
            nonlocal ok_files, fail_files, written_bytes
            e = to_process[i]
            self._dbg(f"🐛 assemble[{i}] start {e.rel_out_path} chunks={len(e.chunks)} unique={len(file_chunk_ids[i])}")
            out_path = self.local_dir / Path(e.rel_out_path.replace("/", os.sep))
            if os.name == "nt":
                os.makedirs(_win_long_path(out_path.parent), exist_ok=True)
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)

            chunk_bytes = cache.get_many([c.chunk_id for c in sorted(e.chunks, key=lambda c: c.offset)])
            if chunk_bytes is None:
                # Не должно происходить, пока планирование выше верно —
                # если всё же случилось, это не тихая порча файла, а
                # громкая, диагностируемая ошибка.
                self.on_log(f"❌ Внутренняя ошибка: чанк для {e.rel_out_path} отсутствует в кэше")
                with stats_lock:
                    fail_files += 1
                cache.release(file_chunk_ids[i])
                return

            ok = True
            h = hashlib.sha256()
            try:
                with open(_win_long_path(out_path), "wb") as f:
                    for data in chunk_bytes:
                        f.write(data)
                        h.update(data)
                        with stats_lock:
                            written_bytes += len(data)
            except Exception as ex:
                # repr(), не str() — голый MemoryError()/OSError без
                # текста даёт str()=="" и лог без единого намёка на
                # причину.
                self.on_log(f"❌ Ошибка записи {e.rel_out_path}: {type(ex).__name__}: {ex!r}")
                ok = False

            if ok and h.hexdigest() != e.file_hash:
                # Хэш — по записанным байтам, без повторного чтения с
                # диска. Каждый чанк уже верифицирован по sha256
                # индивидуально при скачивании — это ловит только
                # неправильную СБОРКУ, не порчу диска постфактум (для
                # этого есть отдельная "Проверить файлы").
                self.on_log(f"❌ Верификация провалена: {e.rel_out_path}")
                ok = False

            with stats_lock:
                if ok:
                    ok_files += 1
                else:
                    fail_files += 1
            if ok:
                # ТОЛЬКО здесь, ТОЛЬКО после успешной sha256-верификации
                # ВСЕГО файла — см. _ResumeStateWriter за то, почему этот
                # порядок единственно безопасный (запись раньше момента
                # подтверждения = риск принять частично записанный файл
                # за готовый после крэша).
                resume_writer.mark_done(e.rel_out_path, e.file_hash)
            self._dbg(f"🐛 assemble[{i}] {'OK' if ok else 'FAIL'} {e.rel_out_path}")
            cache.release(file_chunk_ids[i])

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool, \
             ThreadPoolExecutor(max_workers=assemble_workers) as assemble_pool:

            if _pack_layout_is_file_ordered(to_process, self.client):
                self.on_log("📐 Раскладка pack: по файлам — читаем pack последовательно")
                chunk_order = _plan_chunk_order_packed(to_process, file_chunk_ids, self.client)
            else:
                self.on_log("📐 Раскладка pack: по хэшу (старая публикация) — порядок по файлам, случайное чтение")
                chunk_order = _plan_chunk_order(to_process, file_chunk_ids)
                chunk_order = _reorder_for_disk_locality(chunk_order, self.client, self.max_workers)

            tasks = _group_for_batch(chunk_order, self.client, _BATCH_BYTES, _BATCH_GAP_BYTES)

            # Файлы БЕЗ чанков (size=0, реальные пустые файлы в манифесте)
            # никогда не станут "ready" обычным путём — remaining[i] уже 0,
            # декрементировать нечего. Подаём их в assemble_pool сразу.
            for i in range(len(to_process)):
                if remaining[i] == 0:
                    assemble_pool.submit(_assemble, i)

            next_idx = 0
            in_flight: Dict[object, List[str]] = {}

            def _submit_more():
                nonlocal next_idx
                # `or not in_flight` — предохранитель от дедлока: если
                # потолок памяти заполнен и НИ ОДИН файл ещё не готов
                # (некому освободить кэш), без этого условия закачка
                # застревала бы навсегда. Когда in_flight пуст, потолок
                # игнорируется ровно на одну задачу — гарантирует прогресс.
                # Единица отсчёта — ЗАДАЧА (пачка чанков из одного Range-
                # запроса, см. _group_for_batch()), не отдельный чанк:
                # self.max_workers по-прежнему ограничивает число
                # одновременных HTTP-соединений, каждое теперь просто
                # может нести больше одного чанка за раз.
                while (next_idx < len(tasks)
                       and len(in_flight) < self.max_workers
                       and (not cache.full() or not in_flight)):
                    task = tasks[next_idx]
                    next_idx += 1
                    in_flight[pool.submit(_dl_batch, task)] = task

            _submit_more()
            while in_flight or next_idx < len(tasks):
                if self.should_stop():
                    for f in in_flight:
                        f.cancel()
                    if self.debug:
                        self._dbg_emit(None, force=True)
                    self.on_log("⏹ Загрузка остановлена")
                    resume_writer.close()
                    shutil.rmtree(chunks_dir, ignore_errors=True)
                    return False

                if not in_flight:
                    time.sleep(1.0)
                    _submit_more()
                    continue

                done_set, _ = wait(list(in_flight.keys()), timeout=1.0, return_when=FIRST_COMPLETED)
                if not done_set:
                    _submit_more()
                    continue

                future = done_set.pop()
                task_cids = in_flight.pop(future)
                results = future.result()
                ready: List[int] = []
                with stats_lock:
                    for cid in task_cids:
                        data = results.get(cid)
                        done += 1

                        if data is None:
                            failed_chunk_ids.add(cid)
                            self._dbg(f"🐛 dl {cid[:12]} FAIL -> dooming files={chunk_to_files[cid]}")
                            for i in chunk_to_files[cid]:
                                if doomed[i]:
                                    continue
                                doomed[i] = True
                                fail_files += 1
                                cache.release(file_chunk_ids[i])
                        else:
                            cache.put(cid, data)
                            downloaded_bytes += len(data)
                            self._dbg(f"🐛 dl {cid[:12]} OK {len(data)}B")
                            for i in chunk_to_files[cid]:
                                if doomed[i]:
                                    continue
                                remaining[i] -= 1
                                if remaining[i] == 0:
                                    ready.append(i)
                    if ready:
                        self._dbg(f"🐛 ready after batch of {len(task_cids)}: files={ready}")

                    self.on_progress(done)
                    pct = int(done / len(needed_chunks) * 100) if needed_chunks else 100
                    now = time.time()
                    sample_dt = now - last_sample_t
                    if sample_dt >= 0.5:
                        inst_dl = (downloaded_bytes - last_sample_downloaded) / sample_dt
                        inst_wr = (written_bytes - last_sample_written) / sample_dt
                        alpha = 0.3
                        smoothed_dl_speed = inst_dl if smoothed_dl_speed == 0 else (
                            alpha * inst_dl + (1 - alpha) * smoothed_dl_speed)
                        smoothed_wr_speed = inst_wr if smoothed_wr_speed == 0 else (
                            alpha * inst_wr + (1 - alpha) * smoothed_wr_speed)
                        last_sample_t = now
                        last_sample_downloaded = downloaded_bytes
                        last_sample_written = written_bytes
                    bytes_remaining = total_bytes - downloaded_bytes
                    eta = _fmt_eta(bytes_remaining / smoothed_dl_speed) if smoothed_dl_speed > 0 else "?"
                    # Живая сводка причин отказов — ЖИВЬЁМ в статус-строке,
                    # не только в конце install() (как chunk_error_summary()
                    # в DownloadWorker.run()). Повод 2026-10-01: пользователь
                    # пожаловался на "смешную скорость" (0.2 МБ/с) на реальной
                    # установке, не на зависание — без видимости причины
                    # вслепую непонятно, это узкий канал сам по себе или
                    # retry-штраф (urllib3 Retry: backoff 1/2/4с на каждый
                    # timeout/5xx) умножает и без того узкий канал. getattr —
                    # не все клиенты (тестовые дублёры) обязаны иметь этот
                    # метод, ChunkInstaller по контракту работает с любым
                    # объектом, у которого есть download_chunk().
                    error_info = ""
                    get_summary = getattr(self.client, "chunk_error_summary", None)
                    if get_summary:
                        s = get_summary()
                        if s:
                            error_info = f" — ошибки: {s}"
                    self.on_current(
                        f"{pct}% — ⬇ {smoothed_dl_speed / 1024 / 1024:.1f} MB/s / "
                        f"💾 {smoothed_wr_speed / 1024 / 1024:.1f} MB/s — осталось: {eta}{error_info}"
                    )

                for i in ready:
                    assemble_pool.submit(_assemble, i)
                _submit_more()
        # Оба `with`-пула закрылись здесь — assemble_pool.__exit__ дожидается
        # ВСЕХ поставленных _assemble(), сборка файлов тоже уже завершена.
        if self.debug:
            self._dbg_emit(None, force=True)

        if failed_chunk_ids:
            self.on_log(f"❌ Не удалось скачать {len(failed_chunk_ids)} чанков — часть файлов пропущена")
            # Сводку ПРИЧИН (chunk_error_summary()) печатает вызывающий
            # код (core/workers.py::DownloadWorker.run()) сразу после
            # install() — этот класс не знает, что за client ему дали, и
            # не обязан знать про chunk_error_summary() вообще.

        resume_writer.close()
        removed = self._cleanup_extra_files(entries)
        if removed:
            self.on_log(f"🗑 Удалено лишних файлов: {removed}")

        shutil.rmtree(chunks_dir, ignore_errors=True)

        if fail_files:
            self.on_log(f"❌ Установка завершена с ошибками: {ok_files} ок, {fail_files} ошибок")
            return False
        self.on_log(f"✅ Установка завершена: {ok_files} файлов")
        return True

    def _cleanup_extra_files(self, entries: List[FileEntry]) -> int:
        manifest_paths = {
            str(Path(e.rel_out_path.replace("/", os.sep))) for e in entries
        }
        cache_dir_name = "cache"  # см. install() — <local_dir>/cache, не часть манифеста ни одной сборки
        removed = 0
        for f in self.local_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(self.local_dir)
                # Папка кеша (<local_dir>/cache/...) — свой собственный
                # жизненный цикл (chunks/ чистится в install() сама,
                # resume_state.jsonl переживает установки намеренно), не
                # часть ни одного манифеста сборки по определению. Без
                # этого исключения эта метка-сборщик удалила бы
                # resume_state.jsonl на каждом install() (манифест
                # никогда не содержит "cache/..." как настоящий путь
                # сборки) — что обесценило бы весь смысл Request A, не
                # просто тратило бы место зря.
                if rel.parts and rel.parts[0] == cache_dir_name:
                    continue
                rel_s = str(rel)
                if rel_s not in manifest_paths:
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass
        return removed
