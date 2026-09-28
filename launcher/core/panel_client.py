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
import sqlite3
import tempfile
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
    """

    def __init__(self, build_id: str, base_url: str = None):
        self.build_id = build_id
        self.base = (base_url or _config.PANEL_BASE_URL).rstrip("/")
        self.session = _session()
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

    # ── depot_manifest.json ──────────────────────────────────────────────

    def fetch_manifest(self) -> Optional[dict]:
        return self.get_json("depot_manifest.json", timeout=30)

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
        URL-паттерн, что WebDAV-DepotClient уже использовал."""
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
            except Exception:
                pass
            return None
        # Легаси, без упаковки — тот же путь, что и раньше на WebDAV.
        data = self.get_bytes(f"chunks/{chunk_id[:2]}/{chunk_id}", timeout=120)
        if data is None:
            return None
        if hashlib.sha256(data).hexdigest() != chunk_id:
            return None
        return data

    # ── Отдельные файлы (постер, documents/, patch/, patchs/) ────────────

    def fetch_poster(self) -> Optional[bytes]:
        return self.get_bytes("poster.png", timeout=20)
