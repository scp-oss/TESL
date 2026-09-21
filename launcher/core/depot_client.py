# ==================== launcher/core/depot_client.py ====================
"""
DepotClient — читает depot.json и скачивает файлы сборки.
Заменяет старую логику JSON_SERVER + patch-list.json.

Протокол (совместим с Uploder release_manager):
  <remote_path>/depot.json        — индекс версий
  <remote_path>/manifest.json     — текущий манифест (files: {path: "sha256:hex"})
  <remote_path>/manifests/<ver>.json — манифест конкретной версии
  <remote_path>/files/<rel_path>  — файлы сборки
  <remote_path>/poster.png        — постер
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth

from config import (
    DAV_BASE_URL, DAV_USERNAME, DAV_PASSWORD,
    DEPOT_REMOTE_PATH, DEPOT_JSON_NAME, POSTER_REMOTE_PATH, POSTER_FILENAME,
    MANIFEST_CACHE, POSTER_CACHE, CHUNK_DIR,
)


def _fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"


class DepotClient:
    """
    Низкоуровневый клиент WebDAV-депо.
    Без Qt — используется из воркер-потоков.
    """

    def __init__(
        self,
        server_url:  str = DAV_BASE_URL,
        username:    str = DAV_USERNAME,
        password:    str = DAV_PASSWORD,
        remote_path: str = DEPOT_REMOTE_PATH,
        verify_ssl:  bool = True,
    ):
        self.base        = server_url.rstrip("/")
        self.remote_path = remote_path.strip("/")
        self.session     = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.verify = verify_ssl
        self.session.headers["User-Agent"] = "TESVAE-Launcher/2.0"

        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
        # pool_maxsize здесь ограничивает число держащихся живыми соединений к
        # одному хосту — должен быть >= максимального параллелизма, который
        # заведёт любой воркер (сейчас максимум — CHUNK_MAX_WORKERS в
        # chunk_installer.py, см. его комментарий) — иначе именно пул
        # соединений, а не сеть/диск, становится узким местом.
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=48))
        self.session.mount("http://",  HTTPAdapter(max_retries=retry, pool_maxsize=48))

    # ── URL helpers ───────────────────────────────────────────────────────────

    def _url(self, *parts: str) -> str:
        segs = [self.base, self.remote_path] + [p.strip("/") for p in parts if p]
        return "/".join(segs)

    def _url_encoded(self, rel_path: str) -> str:
        """URL с percent-encoding каждого сегмента пути."""
        segments = [self.base, self.remote_path] + [
            quote(seg, safe="") for seg in rel_path.strip("/").split("/")
        ]
        return "/".join(segments)

    def _chunk_url(self, chunk_id: str) -> str:
        return self._url(CHUNK_DIR, chunk_id[:2], chunk_id)

    # ── depot.json ────────────────────────────────────────────────────────────

    def fetch_depot_index(self) -> Optional[dict]:
        """Скачиваем depot.json — индекс всех версий."""
        try:
            r = self.session.get(self._url(DEPOT_JSON_NAME), timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def get_versions(self) -> List[str]:
        """
        Возвращает список версий (labels) отсортированных от новых к старым.
        Совместимо с combo_versions в UI.
        """
        index = self.fetch_depot_index()
        if not index:
            return []
        versions_dict = index.get("versions", {})
        releases = list(versions_dict.values())
        releases.sort(key=lambda r: r.get("build_number", 0), reverse=True)
        return [r.get("label", k) for k in releases]

    def get_current_version(self, channel: str = "stable") -> Optional[str]:
        """Label текущей активной версии канала."""
        index = self.fetch_depot_index()
        if not index:
            return None
        current_key = index.get("current", {}).get(channel)
        if not current_key:
            return None
        release = index.get("versions", {}).get(current_key, {})
        return release.get("label")

    def get_version_info(self, label: str) -> Optional[dict]:
        """Метаданные версии по её label."""
        index = self.fetch_depot_index()
        if not index:
            return None
        for release in index.get("versions", {}).values():
            if release.get("label") == label:
                return release
        return None

    # ── manifest.json ─────────────────────────────────────────────────────────

    def fetch_manifest(self, version_label: Optional[str] = None) -> Optional[dict]:
        """
        Скачиваем манифест.
        Если version_label=None — берём depot_manifest.json (текущий).
        Иначе ищем конкретный манифест версии через depot.json.
        """
        try:
            if version_label is None:
                r = self.session.get(self._url("manifest.json"), timeout=30)
            else:
                # Находим путь к манифесту через depot.json
                info = self.get_version_info(version_label)
                if not info or not info.get("manifest"):
                    return None
                r = self.session.get(self._url(info["manifest"]), timeout=30)

            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def cache_manifest(self, manifest: dict):
        """Сохраняем манифест локально."""
        try:
            MANIFEST_CACHE.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def load_cached_manifest(self) -> Optional[dict]:
        """Читаем кэшированный манифест."""
        try:
            if MANIFEST_CACHE.exists():
                return json.loads(MANIFEST_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    # ── manifest.db (chunk-протокол, см. config.py "Chunk-based версии") ───────

    def fetch_manifest_db_bytes(self, manifest_json_rel_path: str) -> Optional[bytes]:
        """
        Пробуем скачать компаньон обычного JSON-манифеста версии — тот же
        путь, расширение `.db` вместо `.json`. Обычный, ожидаемый исход —
        HTTP 404 (версия опубликована по старому "плоскому" протоколу, без
        чанков) — тогда просто возвращаем None, это НЕ ошибка.
        """
        if not manifest_json_rel_path.endswith(".json"):
            return None
        db_rel_path = manifest_json_rel_path[: -len(".json")] + ".db"
        try:
            r = self.session.get(self._url(db_rel_path), timeout=60)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        return None

    def download_chunk(self, chunk_id: str) -> Optional[bytes]:
        """Скачиваем один чанк по chunk_id и верифицируем его sha256."""
        try:
            r = self.session.get(self._chunk_url(chunk_id), timeout=120)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        data = r.content
        if hashlib.sha256(data).hexdigest() != chunk_id:
            return None
        return data

    # ── poster.png ────────────────────────────────────────────────────────────

    def fetch_poster(self) -> Optional[bytes]:
        """
        Скачиваем постер (PNG). Кэшируем локально.
        Постер живёт в СВОЕЙ, отдельной от depot ветке (POSTER_REMOTE_PATH —
        "Profils/<сборка>", не "Instances/<сборка>", где лежат depot.json/
        chunks/versions) — поэтому URL строится напрямую от DAV_BASE_URL,
        не через self._url() (тот всегда подставляет self.remote_path).
        """
        url = "/".join([
            self.base,
            POSTER_REMOTE_PATH.strip("/"),
            quote(POSTER_FILENAME, safe=""),
        ])
        try:
            r = self.session.get(url, timeout=20)
            if r.status_code == 200:
                POSTER_CACHE.write_bytes(r.content)
                return r.content
        except Exception:
            pass
        # Fallback на кэш
        if POSTER_CACHE.exists():
            return POSTER_CACHE.read_bytes()
        return None

    # ── File download ─────────────────────────────────────────────────────────

    def download_file(
        self,
        rel_path:       str,
        local_path:     Path,
        expected_hash:  str = "",
        on_progress=None,   # callback(downloaded_bytes, total_bytes)
        stop_fn=None,
        pause_fn=None,
    ) -> Tuple[bool, str]:
        """
        Скачиваем один файл с поддержкой resume (Range) и верификацией.
        rel_path — путь относительно remote_path/files/
        Возвращает (success, message).
        """
        url = self._url_encoded(f"files/{rel_path}")

        # Resume: если файл частично скачан
        local_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        mode = "wb"

        if local_path.exists():
            downloaded = local_path.stat().st_size
            if downloaded > 0:
                mode = "ab"

        headers = {}
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"

        for attempt in range(3):
            try:
                resp = self.session.get(
                    url, headers=headers, stream=True,
                    timeout=(30, 120)
                )

                # Сервер не поддерживает Range — качаем заново
                if "Range" in headers and resp.status_code == 200:
                    downloaded = 0
                    mode = "wb"
                    headers.pop("Range", None)

                if resp.status_code not in (200, 206):
                    return False, f"HTTP {resp.status_code}: {rel_path}"

                total = int(resp.headers.get("Content-Length", 0))
                if resp.status_code == 206:
                    total += downloaded

                with open(local_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if stop_fn and stop_fn():
                            return False, "Остановлено"
                        if pause_fn:
                            pause_fn()
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress and total:
                            on_progress(downloaded, total)

                # Верификация хэша
                if expected_hash:
                    raw_hash = expected_hash.replace("sha256:", "").strip()
                    actual = _sha256(local_path)
                    if actual != raw_hash:
                        local_path.unlink(missing_ok=True)
                        return False, f"Хэш не совпадает: {rel_path}"

                return True, "OK"

            except requests.exceptions.ConnectionError as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return False, f"Ошибка соединения: {e}"
            except Exception as e:
                return False, f"Ошибка: {e}"

        return False, "Превышено число попыток"

    # ── Diff ──────────────────────────────────────────────────────────────────

    @staticmethod
    def diff_with_local(
        manifest_files: Dict[str, str],   # {rel_path: "sha256:hex"}
        local_dir: Path,
    ) -> Tuple[List[str], List[str], List[str]]:
        """
        Сравниваем манифест с локальными файлами.
        Возвращает (to_download, to_redownload, ok).
        to_download  — файл отсутствует
        to_redownload — файл есть, но хэш не совпадает
        ok           — файл в порядке
        """
        to_download = []
        to_redownload = []
        ok = []

        for rel_path, hash_str in manifest_files.items():
            local = local_dir / Path(rel_path.replace("/", os.sep))
            if not local.exists():
                to_download.append(rel_path)
                continue
            expected = hash_str.replace("sha256:", "").strip()
            actual   = _sha256(local)
            if actual != expected:
                to_redownload.append(rel_path)
            else:
                ok.append(rel_path)

        return to_download, to_redownload, ok

    # ── Cleanup ───────────────────────────────────────────────────────────────

    @staticmethod
    def cleanup_extra_files(manifest_files: Dict[str, str], local_dir: Path) -> int:
        """Удаляем файлы которых нет в манифесте. Возвращает количество удалённых."""
        manifest_paths = {
            str(Path(p.replace("/", os.sep))) for p in manifest_files
        }
        removed = 0
        for f in local_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(local_dir))
                if rel not in manifest_paths:
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass
        return removed

    def close(self):
        self.session.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()
