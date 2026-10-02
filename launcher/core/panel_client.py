# ==================== launcher/core/panel_client.py ====================
"""
Клиент чтения TESL-Panel (scp-oss/TESL-Panel) — полная замена WebDAV/
Nextcloud как источника депо сборки. Прямой запрос пользователя
(2026-09-28): "рефакторинг и адаптация лаунчера под существующую
панель", уточнено явным выбором — ПОЛНОСТЬЮ перейти на TESL-Panel, не
держать WebDAV вторым источником рядом (реальная production-сборка
TESVAE на момент этой правки физически ещё живёт на WebDAV — переход
подразумевает, что её отдельно перепубликуют на панель; это не забота
этого репозитория). WebDAV (`core/depot_client.py`) остаётся НЕТРОНУТЫМ
для крэш-/debug-логов (`core/crash_logger.py`) и патчера Skyrim
(`core/patcher.py`) — ни то, ни другое не входило в запрос.

**Все read-эндпоинты панели ПУБЛИЧНЫ** (см. TESL-Panel::panel/app.py —
`GET`/`HEAD` на `/api/builds` и `/api/depot/<build_id>/<rel_path>` не
требуют токена, её же комментарии прямо говорят "публичное чтение") —
никакого пароля/Bearer-токена здесь нет и не нужно, в отличие от
`DepotClient` (WebDAV Basic Auth).

Протокол (производящая сторона — TESL-Manager::depot_sync_manager/
chunk_manager.py + pack_writer.py):
```
GET /api/builds                               [{"id","name","created_at"}, ...]
GET /api/depot/<build_id>/depot_manifest.json  DepotManifest.to_dict():
    {"channel","build_number","created_at","description",
     "files": {path: {"size","hash","chunks":[{"id","offset","size"}]}}}
GET /api/depot/<build_id>/chunk_index.db       SQLite chunk_locations(
    chunk_id TEXT PK, pack TEXT, offset INTEGER, size INTEGER)
GET /api/depot/<build_id>/packs/<pack>         сырые байты pack-файла,
    Range-GET подтверждён (TESL-Panel's send_file(..., conditional=True))
GET /api/depot/<build_id>/chunks/<xx>/<id>     легаси-путь без упаковки
    (use_packs=False на публикации) — тот же класс фолбэка, что раньше
    был у DepotClient.fetch_manifest_db_bytes() на стороне WebDAV
GET /api/depot/<build_id>/extras_manifest.json {"documents":[...],"patch":[...]}
    см. TESL-Manager::documents_tab.py за полную схему полей
    (enabled/order/sha256/size/seq/version/date)
GET /api/depot/<build_id>/<любой другой путь>  как есть — documents/*,
    patch/*, patchs/*, poster.png
```

`PANEL_BASE_URL` — см. config.py, подтверждён реальным доменом 2026-09-28.
"""
import hashlib
import json
try:
    import sqlite3
except ImportError:
    # Живой инцидент 2026-09-29: сервер без C-расширения `_sqlite3`
    # (Python собран из исходников без libsqlite3-dev) — тот же
    # фолбэк, что уже применён в TESL-Panel::builds_db.py и во всех
    # трёх местах TESL-Manager с bare `import sqlite3`.
    import pysqlite3 as sqlite3
import tempfile
from collections import Counter
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

import config as _config


def _session() -> requests.Session:
    s = requests.Session()
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    # Тот же профиль, что DepotClient уже использует для WebDAV
    # (см. её __init__) — pool_maxsize согласован с CHUNK_MAX_WORKERS,
    # иначе пул соединений сам станет новым узким местом раньше сети.
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=48))
    s.mount("http://",  HTTPAdapter(max_retries=retry, pool_maxsize=48))
    s.headers["User-Agent"] = "TESL-Launcher/2.0"
    return s


def list_builds(on_log=None) -> List[dict]:
    """[{"id","name","created_at"}, ...] — пустой список (никогда None)
    на любую ошибку сети/сервера или если сборок ещё нет, вызывающему
    (core/builds.py) не нужно различать эти два случая — оба означают
    "нечего показать в карусели сейчас"."""
    url = f"{_config.PANEL_BASE_URL.rstrip('/')}/api/builds"
    try:
        s = _session()
        r = s.get(url, timeout=15)
        s.close()
        if r.status_code == 200:
            return r.json().get("builds", [])
        if on_log:
            on_log(f"Список сборок: HTTP {r.status_code} на {url}")
    except Exception as e:
        if on_log:
            on_log(f"Список сборок: ошибка запроса ({type(e).__name__}) — {e}")
    return []


def fetch_server_commit(on_log=None) -> Optional[str]:
    """GET /api/server-info — коммит панели, не привязан к сборке (тот
    же эндпоинт, что TESL-Manager's depot_tab.py уже читает для "Инфо с
    сервера"). Best-effort, только для отображения — не критично."""
    url = f"{_config.PANEL_BASE_URL.rstrip('/')}/api/server-info"
    try:
        s = _session()
        r = s.get(url, timeout=10)
        s.close()
        if r.status_code == 200:
            return r.json().get("commit")
    except Exception as e:
        if on_log:
            on_log(f"Инфо о панели: ошибка запроса — {e}")
    return None


class PanelDepotClient:
    """
    Один экземпляр — одна сборка (`build_id`). Инкапсулирует
    `depot_manifest.json`/`chunk_index.db`/`extras_manifest.json` +
    скачивание любого файла внутри сборки, включая `download_chunk()` в
    ТОЧНОЙ форме, которую ожидает `core.chunk_installer.ChunkInstaller`
    (см. её собственный докстринг класса: "любой объект с методом
    `download_chunk(chunk_id) -> Optional[bytes]`") — тот класс сам не
    меняется вообще, только транспорт под ним.

    **`chunk_errors` (2026-09-29)** — живой инцидент: реальная установка
    вернула "Не удалось скачать 2020 чанков" с абсолютно нулевой
    зацепкой, ПОЧЕМУ — `download_chunk()` глотал ЛЮБую причину отказа
    (таймаут, обрыв соединения, HTTP-статус, несовпадение sha256) в один
    и тот же голый `None`, тот же класс "пустое сообщение скрывает
    реальную причину", что уже чинился в этом репозитории для постера/
    записи чанка на диск (см. CLAUDE.md). Теперь каждый отказ
    инкрементирует `self.chunk_errors[reason]` (не льётся в лог на
    КАЖДЫЙ чанк — 2020 одинаковых строк были бы тем же анти-паттерном,
    что уже разбирался для `speed_update` — только счётчик, сводка
    печатается ОДИН раз в конце — см. `core/workers.py::
    DownloadWorker.run()`). Даёт наконец возможность отличить
    "сервер/Cloudflare реально не смог отдать байты" (`timeout`/
    `connection_error`/`http_5xx`) от "чанка там просто нет"
    (`http_404`) от "битые данные дошли" (`sha256_mismatch`) — без этого
    гипотеза "надо скачивать без прокси Cloudflare" остаётся ровно
    гипотезой, не диагнозом.
    """

    def __init__(self, build_id: str, base_url: str = None):
        self.build_id = build_id
        # 2026-10-01: по умолчанию — bypass-домен (PANEL_DOWNLOAD_BASE_URL,
        # см. config.py), не обычный PANEL_BASE_URL за Cloudflare Proxied.
        # Прямой запрос пользователя после диагностики "смешной скорости"
        # (0.2 МБ/с) — тот же вывод, что уже применён на стороне
        # TESL-Manager для заливки (см. её CLAUDE.md "Cloudflare Proxied
        # душит крупные аплоады"), теперь и для скачивания: тысячи мелких
        # Range-GET на чанки — именно тот паттерн запросов, который CF
        # Proxied, по опыту этого проекта, режет сильнее всего. Явный
        # base_url (например, список версий/manifest через обычный домен,
        # если это когда-нибудь понадобится отдельно) всё ещё побеждает.
        self.base = (base_url or _config.PANEL_DOWNLOAD_BASE_URL).rstrip("/")
        self.session = _session()
        self.chunk_errors: "Counter[str]" = Counter()
        self._chunk_index: Optional[Dict[str, Tuple[str, int, int]]] = None
        self._chunk_index_lock = threading.Lock()

    def close(self):
        self.session.close()

    def _url(self, rel_path: str) -> str:
        return f"{self.base}/api/depot/{self.build_id}/{rel_path.lstrip('/')}"

    def get_bytes(self, rel_path: str, timeout: int = 30) -> Optional[bytes]:
        try:
            r = self.session.get(self._url(rel_path), timeout=timeout)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        return None

    def get_json(self, rel_path: str, timeout: int = 30) -> Optional[dict]:
        data = self.get_bytes(rel_path, timeout=timeout)
        if data is None:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    # ── depot_manifest.json / версии для отката (2026-09-29) ─────────────

    def fetch_manifest(self, version_key: Optional[str] = None) -> Optional[dict]:
        """Без `version_key` — текущий (последний опубликованный)
        манифест. С `version_key` — исторический снапшот
        `versions/<key>.json` (см. TESL-Panel::builds_db.py::
        record_version(), пишется панелью автоматически на каждой
        публикации, не нужен отдельный вызов на стороне менеджера).
        Формат JSON у обоих путей идентичен (`DepotManifest.to_dict()`) —
        снапшот версии — буквальная копия того, что было в
        depot_manifest.json на момент ТОЙ публикации. `download_chunk()`
        работает одинаково для любой версии — паки/чанки cumulative
        между публикациями (см. TESL-Manager/CLAUDE.md "Критический
        баг..."), исторический манифест просто ссылается на чанки,
        которые уже физически лежат на сервере."""
        if version_key:
            return self.get_json(f"versions/{version_key}.json", timeout=30)
        return self.get_json("depot_manifest.json", timeout=30)

    def list_versions(self) -> List[dict]:
        """[{"version_key","build_number","description","file_count",
        "total_size","created_at"}, ...], самая новая первой — пусто (не
        None) на любую ошибку/если публикаций ещё не было, вызывающему
        (UI отката) не нужно различать эти два случая."""
        data = self.get_json("versions", timeout=15)
        if data is None:
            return []
        return data.get("versions", [])

    # ── extras_manifest.json (documents/patch/patchs) ────────────────────

    def fetch_extras_manifest(self) -> dict:
        """Никогда не None — сборка без документов/патчей это норма, не
        ошибка (тот же принцип, что уже применяется в TESL-Manager::
        documents_tab.py::_load_manifest())."""
        data = self.get_json("extras_manifest.json", timeout=20)
        if data is None:
            return {"documents": [], "patch": []}
        data.setdefault("documents", [])
        data.setdefault("patch", [])
        return data

    # ── chunk_index.db (packed-хранилище, см. pack_writer.py) ────────────

    def _ensure_chunk_index(self) -> Dict[str, Tuple[str, int, int]]:
        """Ленивая загрузка+парсинг chunk_index.db — один раз на клиент,
        переиспользуется на КАЖДЫЙ download_chunk() (сотни тысяч вызовов
        на крупной сборке — повторный HTTP+SQLite-парсинг на каждый был
        бы абсурдно дорог). Под замком — несколько потоков-загрузчиков
        (ChunkInstaller's ThreadPoolExecutor) могут одновременно захотеть
        chunk_index впервые; без замка это была бы просто лишняя
        повторная работа (не гонка данных — dict присваивается только
        целиком готовым), но замок делает это явным, а не "случайно
        безопасным"."""
        with self._chunk_index_lock:
            if self._chunk_index is not None:
                return self._chunk_index
            data = self.get_bytes("chunk_index.db", timeout=60)
            if data is None:
                self._chunk_index = {}
                return self._chunk_index
            # sqlite3 не читает из bytes напрямую — временный файл, тот
            # же приём, что TESL-Manager's build_manifest_db.py уже
            # использует на противоположной (пишущей) стороне.
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
                    tf.write(data)
                    tmp_path = tf.name
                conn = sqlite3.connect(tmp_path)
                try:
                    self._chunk_index = {
                        row[0]: (row[1], row[2], row[3])
                        for row in conn.execute(
                            "SELECT chunk_id, pack, offset, size FROM chunk_locations"
                        )
                    }
                finally:
                    conn.close()
            except Exception:
                self._chunk_index = {}
            finally:
                if tmp_path:
                    try:
                        Path(tmp_path).unlink()
                    except Exception:
                        pass
            return self._chunk_index

    def download_chunk(self, chunk_id: str) -> Optional[bytes]:
        """Интерфейс, ожидаемый ChunkInstaller — скачивает и верифицирует
        ОДИН чанк по content-addressed chunk_id. Пробует packed-хранилище
        (chunk_index.db + Range-GET на pack) первым; если чанка там нет
        (легаси-сборка без упаковки, use_packs=False на публикации) —
        падает обратно на прямой путь chunks/<xx>/<id>, тот же
        URL-паттерн, что WebDAV-DepotClient уже использовал.

        Каждый отказ инкрементирует `self.chunk_errors[reason]` вместо
        того чтобы молча слиться в один и тот же `None` — см. докстринг
        класса за живой повод (2020 "не удалось скачать" без единой
        зацепки, почему)."""
        index = self._ensure_chunk_index()
        loc = index.get(chunk_id)
        if loc is not None:
            pack, offset, size = loc
            try:
                headers = {"Range": f"bytes={offset}-{offset + size - 1}"}
                r = self.session.get(self._url(f"packs/{pack}"), headers=headers, timeout=60)
                if r.status_code in (200, 206):
                    data = r.content
                    if hashlib.sha256(data).hexdigest() == chunk_id:
                        return data
                    self.chunk_errors["sha256_mismatch"] += 1
                    return None
                self.chunk_errors[f"http_{r.status_code}"] += 1
                return None
            except requests.exceptions.Timeout:
                self.chunk_errors["timeout"] += 1
                return None
            except requests.exceptions.ConnectionError:
                self.chunk_errors["connection_error"] += 1
                return None
            except Exception as e:
                self.chunk_errors[f"exception_{type(e).__name__}"] += 1
                return None
        # Легаси, без упаковки — тот же путь, что и раньше на WebDAV.
        try:
            r = self.session.get(
                self._url(f"chunks/{chunk_id[:2]}/{chunk_id}"), timeout=120,
            )
        except requests.exceptions.Timeout:
            self.chunk_errors["timeout"] += 1
            return None
        except requests.exceptions.ConnectionError:
            self.chunk_errors["connection_error"] += 1
            return None
        except Exception as e:
            self.chunk_errors[f"exception_{type(e).__name__}"] += 1
            return None
        if r.status_code != 200:
            self.chunk_errors[f"http_{r.status_code}"] += 1
            return None
        data = r.content
        if hashlib.sha256(data).hexdigest() != chunk_id:
            self.chunk_errors["sha256_mismatch"] += 1
            return None
        return data

    def get_chunk_location_full(self, chunk_id: str) -> Optional[Tuple[str, int, int]]:
        """(pack, offset, size) — как get_chunk_location(), но с размером,
        нужным для склейки физически смежных чанков в один Range-запрос
        (см. chunk_installer.py::_group_for_batch()). Отдельный метод, а
        не расширение существующего get_chunk_location() — тот уже
        используется `_reorder_for_disk_locality()`, трогать его
        сигнатуру без необходимости рискованно."""
        return self._ensure_chunk_index().get(chunk_id)

    def download_chunks_batched(self, chunk_ids: List[str]) -> Dict[str, Optional[bytes]]:
        """Один Range-GET на несколько физически смежных чанков вместо
        одного запроса на чанк — живой повод 2026-10-02: пользователь
        написал и прогнал `batch_bench.py` (собственный замер, не наша
        гипотеза) против реального `tesl-panel.neth.de5.net` и bypass-
        домена `transport.neth.de5.net` — батч по 8МБ дал ~1.2x к
        скорости и втрое меньше запросов на ОБОИХ доменах (CF-proxied:
        21.56 -> 26.31 МБ/с; bypass: 84.17 -> 103.55 МБ/с), поверх уже
        переключённого bypass-домена (см. config.py::
        PANEL_DOWNLOAD_BASE_URL) — независимый, аддитивный выигрыш, не
        альтернатива ему.

        Группировку (какие chunk_id физически смежны и укладываются в
        лимит батча) делает ВЫЗЫВАЮЩИЙ код
        (`chunk_installer.py::_group_for_batch()`) — здесь только
        транспорт: один GET на объединённый диапазон, срез по каждому
        чанку, верификация КАЖДОГО по sha256 отдельно. Возвращает
        `{chunk_id: bytes|None}` — `None` для конкретного chunk_id
        означает именно его отказ (например, битый sha256 внутри иначе
        успешной пачки), остальные чанки той же пачки при этом остаются
        валидными. Полный сетевой отказ (таймаут/обрыв/HTTP-ошибка)
        помечает ВСЕ чанки пачки одной и той же причиной в
        `chunk_errors` — для пачки из одного элемента это даёт тот же
        результат, что и `download_chunk()`."""
        if not chunk_ids:
            return {}
        if len(chunk_ids) == 1:
            return {chunk_ids[0]: self.download_chunk(chunk_ids[0])}
        index = self._ensure_chunk_index()
        locs = [index.get(cid) for cid in chunk_ids]
        if any(loc is None for loc in locs):
            # Не должно происходить — _group_for_batch() уже фильтрует
            # только чанки с известной локацией. Защитный откат на
            # одиночные закачки вместо того чтобы ронять всю пачку.
            return {cid: self.download_chunk(cid) for cid in chunk_ids}
        pack = locs[0][0]
        start = min(off for _, off, _ in locs)
        end = max(off + size for _, off, size in locs)
        try:
            headers = {"Range": f"bytes={start}-{end - 1}"}
            r = self.session.get(self._url(f"packs/{pack}"), headers=headers, timeout=120)
            if r.status_code not in (200, 206):
                self.chunk_errors[f"http_{r.status_code}"] += len(chunk_ids)
                return {cid: None for cid in chunk_ids}
            data = r.content
        except requests.exceptions.Timeout:
            self.chunk_errors["timeout"] += len(chunk_ids)
            return {cid: None for cid in chunk_ids}
        except requests.exceptions.ConnectionError:
            self.chunk_errors["connection_error"] += len(chunk_ids)
            return {cid: None for cid in chunk_ids}
        except Exception as e:
            self.chunk_errors[f"exception_{type(e).__name__}"] += len(chunk_ids)
            return {cid: None for cid in chunk_ids}
        out: Dict[str, Optional[bytes]] = {}
        for cid, (_pack, off, size) in zip(chunk_ids, locs):
            piece = data[off - start: off - start + size]
            if len(piece) == size and hashlib.sha256(piece).hexdigest() == cid:
                out[cid] = piece
            else:
                self.chunk_errors["sha256_mismatch"] += 1
                out[cid] = None
        return out

    def get_chunk_location(self, chunk_id: str) -> Optional[Tuple[str, int]]:
        """Физическое расположение чанка — (pack, offset), без size.
        Используется ChunkInstaller'ом (см. его
        `_reorder_for_disk_locality()`, живой повод — 2026-09-30, диск
        сервера насыщался под логически случайным порядком чтения
        физически цельных pack-файлов) ТОЛЬКО для локальной
        пересортировки очереди закачки ради дисковой локальности на
        сервере — не меняет, ЧТО скачивается, только относительный
        порядок внутри окна. Возвращает None, если чанка нет в индексе
        (легаси-сборка без упаковки — ChunkInstaller в этом случае
        получает пустой словарь от _ensure_chunk_index() и просто не
        находит локацию, что эквивалентно отсутствию этого метода у
        клиента вообще с точки зрения результата сортировки)."""
        index = self._ensure_chunk_index()
        loc = index.get(chunk_id)
        if loc is None:
            return None
        pack, offset, _size = loc
        return (pack, offset)

    def chunk_error_summary(self) -> str:
        """Одна строка вида 'timeout: 1800, http_404: 220' для итогового
        лога — см. core/workers.py::DownloadWorker.run(). Пусто, если
        отказов не было (обычный, ожидаемый случай)."""
        if not self.chunk_errors:
            return ""
        return ", ".join(f"{reason}: {count}" for reason, count in self.chunk_errors.most_common())

    # ── Отдельные файлы (постер, documents/, patch/, patchs/) ────────────

    def fetch_poster(self) -> Optional[bytes]:
        return self.get_bytes("poster.png", timeout=20)
