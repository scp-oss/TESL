# ==================== launcher/core/patcher.py ====================
"""
SkyrimPatcher — патчинг и откат патча Skyrim Special Edition.
MO2Configurator — настройка ModOrganizer.ini и создание ярлыка.
Извлечено из updater18-5-5-8.py без изменений логики.
"""
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from PyQt6.QtCore import pyqtSignal

import config as _config   # для DAV_PASSWORD — см. _get_session()
from config import (
    DAV_BASE_URL, DAV_USERNAME,
    PATCHER_REMOTE_PATH, APPDATA_DIR,
    MO2_EXE, MO2_SHORTCUT_NAME, MO2_SKSE_ARG,
    MO2_INI_SKYRIM_PLACEHOLDER_FWD, MO2_INI_SKYRIM_PLACEHOLDER_DBL,
)
from core.workers import ThreadSafeWorker

try:
    import winreg
except ImportError:
    winreg = None


PATCHER_MANIFEST_FILE = APPDATA_DIR / "patcher_manifest.json"


def _normalize(path: str) -> str:
    return path.replace("\\", "/")


def _win_path(path) -> str:
    if isinstance(path, Path):
        path = str(path)
    return path.replace("/", "\\") if os.name == "nt" else path


def _reset_attrs(path) -> bool:
    try:
        if os.name == "nt" and os.path.exists(path):
            attrs = os.stat(path).st_file_attributes
            if attrs & stat.FILE_ATTRIBUTE_READONLY:
                os.chmod(path, stat.S_IWRITE)
        return True
    except Exception:
        return False


# ── SkyrimPatcher ─────────────────────────────────────────────────────────────

class SkyrimPatcher(ThreadSafeWorker):
    """
    Скачивает патчер с сервера и применяет к Skyrim.
    Поддерживает run_patch() и run_revert().
    """

    log_signal      = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int, str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.temp_dir      = None
        self.patcher_manifest: dict = {}
        self.start_time    = None
        self._session      = None

    # ── Session ───────────────────────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        if not self._session:
            self._session = requests.Session()
            # config.DAV_PASSWORD живьём на момент вызова, не на момент
            # импорта этого модуля (см. depot_client.py::DepotClient.__init__
            # — та же правка, тот же повод: пароль может появиться позже,
            # через диалог первого запуска в main.py).
            self._session.auth = (DAV_USERNAME, _config.DAV_PASSWORD)
        return self._session

    # ── Skyrim detection (делегируем checker'у) ───────────────────────────────

    def find_skyrim_steam(self) -> Optional[str]:
        from core.skyrim_checker import SkyrimChecker
        result = SkyrimChecker().check(log=self.log_signal.emit)
        return str(Path(result.skyrim_dir) / "SkyrimSE.exe") if result.found else None

    # ── Patcher manifest ──────────────────────────────────────────────────────

    def download_patcher_manifest(self) -> bool:
        import json
        try:
            self.log_signal.emit("Загружаем манифест патчера...")
            url = f"{DAV_BASE_URL}/{PATCHER_REMOTE_PATH}/manifest.json"
            r   = self._get_session().get(url, timeout=30)
            r.raise_for_status()
            self.patcher_manifest = r.json()
            with open(PATCHER_MANIFEST_FILE, "w", encoding="utf-8") as f:
                json.dump(self.patcher_manifest, f, indent=2)
            self.log_signal.emit(f"Манифест патчера загружен: {len(self.patcher_manifest.get('files', {}))} файлов")
            return True
        except Exception as e:
            self.log_signal.emit(f"Ошибка загрузки манифеста патчера: {e}")
            return False

    # ── Download patcher files ────────────────────────────────────────────────

    def download_patcher_files(self, game_path: str) -> bool:
        if not self.patcher_manifest or "files" not in self.patcher_manifest:
            self.log_signal.emit("Манифест патчера не загружен")
            return False

        self.temp_dir = tempfile.mkdtemp()
        self.log_signal.emit(f"Временная папка: {self.temp_dir}")

        file_items  = list(self.patcher_manifest["files"].items())
        total_files = len(file_items)
        self.start_time = time.time()
        downloaded_count = 0
        lock = threading.Lock()

        def _eta(current, total, start):
            if current == 0:
                return "оценивается..."
            elapsed = time.time() - start
            rate    = current / elapsed if elapsed > 0 else 0
            remaining = total - current
            secs = int(remaining / rate) if rate > 0 else 0
            m, s = divmod(secs, 60)
            h, m = divmod(m, 60)
            return f"{h}h{m:02d}m{s:02d}s" if h else (f"{m}m{s:02d}s" if m else f"{s}s")

        def _download_one(item):
            nonlocal downloaded_count
            file_name, expected_hash = item
            if self._should_stop():
                return False
            self._wait_if_paused()

            local_path = os.path.join(self.temp_dir, *file_name.split("/"))
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            url = f"{DAV_BASE_URL}/{PATCHER_REMOTE_PATH}/{'/'.join(quote(p) for p in file_name.split('/'))}"

            for attempt in range(3):
                try:
                    r = self._get_session().get(url, stream=True, timeout=60)
                    r.raise_for_status()
                    with open(local_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)

                    # Verify
                    h = hashlib.sha256()
                    with open(local_path, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    actual = h.hexdigest()
                    expected_clean = expected_hash.replace("sha256:", "").strip()
                    if actual != expected_clean:
                        self.log_signal.emit(f"Хэш не совпадает: {file_name}, попытка {attempt+1}")
                        if os.path.exists(local_path):
                            os.remove(local_path)
                        continue

                    with lock:
                        downloaded_count += 1
                        eta = _eta(downloaded_count, total_files, self.start_time)
                        self.progress_signal.emit(downloaded_count, total_files, eta)
                    return True

                except Exception as e:
                    self.log_signal.emit(f"Ошибка {file_name} (попытка {attempt+1}): {e}")
                    time.sleep(1)

            return False

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_download_one, file_items))

        if self._should_stop():
            return False

        if all(results):
            self.log_signal.emit(f"Скачаны все {total_files} файлов патчера")
            return True
        else:
            failed = sum(1 for r in results if not r)
            self.log_signal.emit(f"Не удалось скачать {failed}/{total_files} файлов патчера")
            return False

    # ── Apply patch ───────────────────────────────────────────────────────────

    def apply_patch(self, game_path: str) -> bool:
        try:
            if not self.temp_dir or not os.path.exists(self.temp_dir):
                self.log_signal.emit("Файлы патчера не скачаны")
                return False

            game_folder = os.path.dirname(game_path)
            self.log_signal.emit(f"Применяем патч к: {game_folder}")

            total_files = 0
            for _, _, files in os.walk(self.temp_dir):
                total_files += len(files)

            self.start_time = time.time()
            copied = 0

            for root, _, files in os.walk(self.temp_dir):
                for file in files:
                    if self._should_stop():
                        return False
                    self._wait_if_paused()

                    src = os.path.join(root, file)
                    rel = os.path.relpath(src, self.temp_dir)
                    dst = _win_path(os.path.join(game_folder, rel))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)

                    try:
                        with open(src, "rb") as fs, open(dst, "wb") as fd:
                            while True:
                                chunk = fs.read(65536)
                                if not chunk:
                                    break
                                fd.write(chunk)
                        shutil.copystat(src, dst)
                        copied += 1
                        self.progress_signal.emit(copied, total_files, "")
                    except Exception as e:
                        self.log_signal.emit(f"Ошибка копирования {rel}: {e}")
                        return False

            self.log_signal.emit(f"Скопировано {copied} файлов")
            return True
        except Exception as e:
            self.log_signal.emit(f"Ошибка применения патча: {e}")
            return False

    # ── Revert patch ──────────────────────────────────────────────────────────

    def revert_patch(self, game_path: str) -> bool:
        import json
        try:
            if not PATCHER_MANIFEST_FILE.exists():
                self.log_signal.emit("Манифест патчера не найден")
                return False

            with open(PATCHER_MANIFEST_FILE, "r", encoding="utf-8") as f:
                manifest = json.load(f)

            files = manifest.get("files", {})
            game_folder = os.path.dirname(game_path)
            self.log_signal.emit(f"Откат из: {game_folder}")

            total    = len(files)
            removed  = 0
            self.start_time = time.time()

            for file_name in files:
                if self._should_stop():
                    return False
                self._wait_if_paused()

                file_path = _win_path(os.path.join(game_folder, *file_name.split("/")))
                if os.path.exists(file_path):
                    for attempt in range(3):
                        try:
                            _reset_attrs(file_path)
                            os.remove(file_path)
                            removed += 1
                            break
                        except PermissionError:
                            time.sleep(1)
                        except Exception as e:
                            self.log_signal.emit(f"Ошибка удаления {file_name}: {e}")
                            break
                    self.progress_signal.emit(removed, total, "")

            # Удаляем пустые папки
            for root, dirs, files_left in os.walk(game_folder, topdown=False):
                for d in dirs:
                    dp = os.path.join(root, d)
                    try:
                        if not os.listdir(dp):
                            os.rmdir(dp)
                    except Exception:
                        pass

            try:
                PATCHER_MANIFEST_FILE.unlink()
            except Exception:
                pass

            self.log_signal.emit(f"Откат завершён. Удалено {removed} файлов")
            return True
        except Exception as e:
            self.log_signal.emit(f"Ошибка отката: {e}")
            return False

    # ── Run methods ───────────────────────────────────────────────────────────

    def run_patch(self):
        try:
            game_path = self.find_skyrim_steam()
            if not game_path:
                self.log_signal.emit("Skyrim Special Edition не найден")
                self.finished_signal.emit(False, "")
                return
            if not self.download_patcher_manifest():
                self.finished_signal.emit(False, game_path)
                return
            if not self.download_patcher_files(game_path):
                self.finished_signal.emit(False, game_path)
                return
            if not self.apply_patch(game_path):
                self.finished_signal.emit(False, game_path)
                return
            if self.temp_dir and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.log_signal.emit("Патчинг завершён успешно!")
            self.finished_signal.emit(True, game_path)
        except Exception as e:
            self.log_signal.emit(f"Критическая ошибка патчинга: {e}")
            if self.temp_dir:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.finished_signal.emit(False, "")

    def run_revert(self):
        try:
            game_path = self.find_skyrim_steam()
            if not game_path:
                self.log_signal.emit("Skyrim Special Edition не найден")
                self.finished_signal.emit(False, "")
                return
            if not self.revert_patch(game_path):
                self.finished_signal.emit(False, game_path)
                return
            self.log_signal.emit("Откат патча завершён успешно!")
            self.finished_signal.emit(True, game_path)
        except Exception as e:
            self.log_signal.emit(f"Критическая ошибка отката: {e}")
            self.finished_signal.emit(False, "")


# ── MO2 Configurator ─────────────────────────────────────────────────────────

class MO2Configurator:
    """Настройка ModOrganizer.ini и создание ярлыка."""

    @staticmethod
    def update_ini(mo2_dir: str, game_folder: str, log=print) -> bool:
        """Заменяет placeholder-пути в ModOrganizer.ini на реальные."""
        ini_path = _win_path(os.path.join(mo2_dir, "ModOrganizer.ini"))
        if not os.path.exists(ini_path):
            log(f"ModOrganizer.ini не найден: {ini_path}")
            return False
        try:
            with open(ini_path, "r", encoding="utf-8") as f:
                content = f.read()

            fwd = _normalize(game_folder)
            dbl = game_folder.replace("\\", "\\\\")

            content = content.replace(MO2_INI_SKYRIM_PLACEHOLDER_FWD, fwd)
            content = content.replace(MO2_INI_SKYRIM_PLACEHOLDER_DBL, dbl)

            with open(ini_path, "w", encoding="utf-8") as f:
                f.write(content)

            log("ModOrganizer.ini обновлён ✓")
            return True
        except Exception as e:
            log(f"Ошибка обновления INI: {e}")
            return False

    @staticmethod
    def create_shortcut(
        mo2_dir: str,
        log=print,
        icon_path: Optional[str] = None,
        arg: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Создаёт ярлык TESVAE на рабочем столе.

        icon_path — путь к .ico для ярлыка (см. DepotClient.fetch_shortcut_icon()
        — качается с <remote_path>/src/icon.ico); None/файл не существует —
        ярлык остаётся без явной IconLocation (Windows берёт иконку из
        TargetPath, как было раньше этой правки).
        arg — аргумент запуска (см. DepotClient.fetch_shortcut_arg() —
        <remote_path>/src/agr.json); None — используется дефолт MO2_SKSE_ARG
        (было единственным поведением раньше этой правки).
        """
        if os.name != "nt":
            return False, "Только Windows"

        mo_exe = _win_path(os.path.join(mo2_dir, MO2_EXE))
        if not os.path.exists(mo_exe):
            return False, f"{MO2_EXE} не найден в {mo2_dir}"

        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            shortcut_path = os.path.join(desktop, f"{MO2_SHORTCUT_NAME}.lnk")
            workdir = os.path.dirname(mo_exe)

            fd, vbs_path = tempfile.mkstemp(suffix=".vbs")
            os.close(fd)

            effective_arg = arg if arg else MO2_SKSE_ARG
            safe_args = effective_arg.replace('"', '""')

            icon_line = ""
            if icon_path and os.path.exists(icon_path):
                safe_icon = _win_path(icon_path).replace('"', '""')
                icon_line = f'oLink.IconLocation = "{safe_icon}"\n'

            script = (
                f'Set oWS = WScript.CreateObject("WScript.Shell")\n'
                f'sLinkFile = "{shortcut_path}"\n'
                f'Set oLink = oWS.CreateShortcut(sLinkFile)\n'
                f'oLink.TargetPath = "{mo_exe}"\n'
                f'oLink.Arguments = """{safe_args}"""\n'
                f'oLink.WorkingDirectory = "{workdir}"\n'
                f'{icon_line}'
                f'oLink.Save\n'
            )
            with open(vbs_path, "w", encoding="utf-8") as f:
                f.write(script)

            subprocess.run(
                ["cscript", "//nologo", vbs_path],
                check=True, timeout=30
            )
            os.remove(vbs_path)
            return True, f"Ярлык создан: {shortcut_path}"
        except Exception as e:
            return False, f"Ошибка создания ярлыка: {e}"
