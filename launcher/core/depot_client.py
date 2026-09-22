# ==================== launcher/core/depot_client.py ====================
"""
DepotClient — читает depot.json и скачивает файлы сборки.
Заменяет старую логику JSON_SERVER + patch-list.json.

Протокол (совместим с Uploder release_manager):
  <remote_path>/depot.json        — индекс версий
  <remote_path>/manifest.json     — текущий манифест (files: {path: "sha256:hex"})
  <remote_path>/manifests/<ver>.json — манифест конкретной версии
  <remote_path>/files/<rel_path>  — файлы сборки
  <remote_path>/src/image.png     — постер (подтверждено 2026-09-21)
  <remote_path>/src/icon.ico      — иконка ярлыка сборки (подтверждено 2026-09-21)
  <remote_path>/src/agr.json      — аргумент запуска ярлыка (подтверждено 2026-09-21)
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

# import config as _config — для значений, которые config.activate_build()
# может поменять ЖИВЬЁМ после импорта этого модуля (DEPOT_REMOTE_PATH,
# DAV_PASSWORD, MANIFEST_CACHE/POSTER_CACHE/SHORTCUT_ICON_CACHE). Та же
# ловушка, что уже чинилась в этом сеансе для DAV_PASSWORD (см. CLAUDE.md) —
# `from config import X` замораживает X на момент импорта модуля, а не
# читает его заново при каждом вызове. Константы НИЖЕ, которые
# activate_build() не трогает (одинаковые для всех сборок структурно —
# сам протокол, а не то, ГДЕ он развёрнут), по-прежнему импортируются по
# имени напрямую — это безопасно, они не меняются во время работы процесса.
import config as _config
from config import (
    DAV_BASE_URL, DAV_USERNAME,
    DEPOT_JSON_NAME, BUILD_ASSETS_SUBDIR,
    POSTER_FILENAME, SHORTCUT_ICON_FILENAME, SHORTCUT_ARG_FILENAME,
    CHUNK_DIR, BUILDS_REGISTRY_PATH,
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
        password:    Optional[str] = None,
        remote_path: Optional[str] = None,
        verify_ssl:  bool = True,
    ):
        self.base        = server_url.rstrip("/")
        # remote_path=None -> config.DEPOT_REMOTE_PATH ЖИВЬЁМ, на момент
        # вызова __init__, не на момент импорта модуля (раньше было
        # `remote_path: str = DEPOT_REMOTE_PATH` — значение по умолчанию у
        # функции вычисляется ОДИН РАЗ при определении функции, то есть при
        # первом импорте этого файла; после config.activate_build() при
        # переключении сборки в карусели все последующие `DepotClient()` без
        # явного remote_path продолжали бы бить в старую сборку). Тот же
        # класс бага, что уже чинился для DAV_PASSWORD.
        effective_remote_path = remote_path if remote_path is not None else _config.DEPOT_REMOTE_PATH
        self.remote_path = effective_remote_path.strip("/")
        self.session     = requests.Session()
        # password=None -> берём config.DAV_PASSWORD ЖИВЬЁМ, на момент вызова,
        # не на момент импорта этого модуля. `from config import DAV_PASSWORD`
        # (как было раньше) заморозило бы значение в default-аргументе
        # функции — если пользователь введёт пароль через диалог первого
        # запуска (main.py::_ensure_dav_password()) уже ПОСЛЕ импорта, это
        # никогда бы не подхватилось без перезапуска процесса.
        effective_password = password if password is not None else _config.DAV_PASSWORD
        self.session.auth = HTTPBasicAuth(username, effective_password)
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
        """Сохраняем манифест локально (путь — _config.MANIFEST_CACHE живьём,
        см. комментарий на импортах в начале файла — меняется activate_build())."""
        try:
            _config.MANIFEST_CACHE.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def load_cached_manifest(self) -> Optional[dict]:
        """Читаем кэшированный манифест."""
        try:
            if _config.MANIFEST_CACHE.exists():
                return json.loads(_config.MANIFEST_CACHE.read_text(encoding="utf-8"))
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

    def fetch_poster(self, on_log=None) -> Optional[bytes]:
        """
        Скачиваем постер (<remote_path>/src/image.png). Кэшируем локально.
        on_log(str), если передан, — единственное место, где реальная причина
        неудачи (HTTP-код, исключение) не глотается молча — раньше эта
        функция ловила любую ошибку через голый `except: pass`, и по UI
        было невозможно понять, 404 это, проблема авторизации или что-то ещё.
        """
        url = self._url(BUILD_ASSETS_SUBDIR, POSTER_FILENAME)
        try:
            r = self.session.get(url, timeout=20)
            if r.status_code == 200:
                _config.POSTER_CACHE.write_bytes(r.content)
                if on_log:
                    on_log(f"Постер загружен ({len(r.content)} байт): {url}")
                return r.content
            if on_log:
                on_log(f"Постер: HTTP {r.status_code} на {url}")
        except Exception as e:
            if on_log:
                on_log(f"Постер: ошибка запроса {url} — {e}")
        # Fallback на кэш
        if _config.POSTER_CACHE.exists():
            if on_log:
                on_log(f"Постер: беру из локального кэша ({_config.POSTER_CACHE})")
            return _config.POSTER_CACHE.read_bytes()
        return None

    # ── Иконка/аргумент ярлыка (<remote_path>/src/, подтверждено 2026-09-21) ───

    def fetch_shortcut_icon(self) -> Optional[Path]:
        """
        Скачиваем иконку ярлыка сборки в локальный кэш, возвращаем путь к
        файлу (ярлык .lnk ссылается на иконку файлом на диске, не байтами
        в памяти — поэтому возвращаем Path, а не bytes, как у постера).
        """
        try:
            r = self.session.get(self._url(BUILD_ASSETS_SUBDIR, SHORTCUT_ICON_FILENAME), timeout=20)
            if r.status_code == 200:
                _config.SHORTCUT_ICON_CACHE.write_bytes(r.content)
                return _config.SHORTCUT_ICON_CACHE
        except Exception:
            pass
        if _config.SHORTCUT_ICON_CACHE.exists():
            return _config.SHORTCUT_ICON_CACHE
        return None

    def fetch_shortcut_arg(self) -> Optional[dict]:
        """Скачиваем и парсим <remote_path>/src/agr.json (аргумент запуска ярлыка)."""
        try:
            r = self.session.get(self._url(BUILD_ASSETS_SUBDIR, SHORTCUT_ARG_FILENAME), timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
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


def fetch_poster_bytes(remote_path: str, on_log=None) -> Optional[bytes]:
    """
    Скачивает постер КОНКРЕТНОЙ сборки по её remote_path — для карусели,
    которая показывает миниатюры ВСЕХ сборок сразу, до того как какая-либо
    из них станет "активной" через config.activate_build(). Намеренно НЕ
    переиспользует DepotClient.fetch_poster()/_config.POSTER_CACHE — та
    кэширует на диск и читает fallback-кэш только текущей активной сборки;
    вызов её для чужой сборки перепутал бы кэши между собой. Здесь кэша на
    диск нет вообще — карусель недолговечна, каждый показ качает миниатюры
    заново; если станет проблемой при большом числе сборок — отдельная
    доработка (кэш по build.name), не сейчас.
    """
    base = DAV_BASE_URL.rstrip("/")
    rp   = remote_path.strip("/")
    url  = f"{base}/{rp}/{BUILD_ASSETS_SUBDIR}/{POSTER_FILENAME}"
    try:
        session = requests.Session()
        session.auth = HTTPBasicAuth(DAV_USERNAME, _config.DAV_PASSWORD)
        r = session.get(url, timeout=20)
        session.close()
        if r.status_code == 200:
            return r.content
        if on_log:
            on_log(f"Постер сборки: HTTP {r.status_code} на {url}")
    except Exception as e:
        if on_log:
            on_log(f"Постер сборки: ошибка запроса {url} — {e}")
    return None


def fetch_shortcut_assets(log=print) -> Tuple[Optional[str], Optional[str]]:
    """
    Качает иконку/аргумент ярлыка сборки с сервера (<remote_path>/src/
    icon.ico и /src/agr.json). Возвращает (icon_path|None, arg|None) —
    None в любом поле значит "не удалось/нет" —
    MO2Configurator.create_shortcut() сам откатывается на прежнее
    поведение (иконка из TargetPath, дефолтный MO2_SKSE_ARG). Никогда не
    бросает исключение наружу — падение этой загрузки не должно мешать
    созданию ярлыка вообще.

    Перенесена сюда из ui/main_window.py::_fetch_shortcut_assets()
    2026-09-22 — та версия делала два сетевых запроса (до 20с каждый)
    ПРЯМО на GUI-потоке, вызывалась из _post_install_configure()/
    _create_shortcut(), которые сами тоже целиком шли синхронно на
    GUI-потоке вместе с SkyrimChecker().check() и subprocess-вызовом
    MO2Configurator.create_shortcut() (до 30с) — суммарно интерфейс мог
    замирать почти на минуту. Теперь свободная функция с параметром
    log вместо self._append_log — вызывается из
    core.workers.PostInstallWorker в фоновом потоке, см. её докстринг.
    """
    icon_path = None
    arg = None
    try:
        client = DepotClient()
        icon = client.fetch_shortcut_icon()
        if icon:
            icon_path = str(icon)
        arg_data = client.fetch_shortcut_arg()
        if isinstance(arg_data, dict):
            # Схема agr.json не подтверждена с сервера — пробуем
            # несколько правдоподобных имён поля, не гадаем на одном.
            for key in ("argument", "arg", "args", "skse_arg"):
                if isinstance(arg_data.get(key), str) and arg_data[key]:
                    arg = arg_data[key]
                    break
            if arg is None:
                log(
                    f"⚠️ agr.json скачан, но не нашли ожидаемое поле "
                    f"(argument/arg/args/skse_arg) в {list(arg_data.keys())} — "
                    f"использую аргумент по умолчанию"
                )
        elif isinstance(arg_data, str) and arg_data:
            arg = arg_data
        client.close()
    except Exception as e:
        log(f"⚠️ Не удалось загрузить иконку/аргумент ярлыка с сервера: {e}")
    return icon_path, arg


def fetch_builds_registry(on_log=None) -> Optional[list]:
    """
    Скачивает реестр доступных сборок для карусели —
    <DAV_BASE_URL>/<config.BUILDS_REGISTRY_PATH>, ожидаемый JSON-массив
    [{"name": "...", "label": "...", "remote_path": "..."}, ...].

    НЕ существование этого файла на сервере СЕЙЧАС ещё не подтверждено —
    см. BUILDS_REGISTRY_PATH в config.py. HTTP 404/любая ошибка сети —
    ШТАТНЫЙ, ожидаемый исход, а не сбой: вызывающий код
    (core/builds.py::list_builds()) в этом случае откатывается на
    единственную сборку, зашитую в config.py напрямую (DEPOT_REMOTE_PATH/
    BUILD_NAME), так что карусель работает уже сегодня с одной плиткой.
    Отдельная короткоживущая requests.Session — вне remote_path какой-либо
    конкретной сборки, реестр лежит на уровень выше.
    """
    url = f"{DAV_BASE_URL.rstrip('/')}/{BUILDS_REGISTRY_PATH.strip('/')}"
    try:
        session = requests.Session()
        session.auth = HTTPBasicAuth(DAV_USERNAME, _config.DAV_PASSWORD)
        r = session.get(url, timeout=15)
        session.close()
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
            if on_log:
                on_log(f"Реестр сборок: неожиданный формат (не список JSON) — {url}")
            return None
        if on_log:
            on_log(f"Реестр сборок: HTTP {r.status_code} на {url} (ещё не выложен — используем текущую сборку)")
    except Exception as e:
        if on_log:
            on_log(f"Реестр сборок недоступен: {e} (используем текущую сборку)")
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()
