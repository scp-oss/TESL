# ==================== launcher/core/skyrim_checker.py ====================
"""
SkyrimChecker — обнаружение и верификация Skyrim Special Edition AE.

Проверяет:
  1. Наличие SkyrimSE.exe версии 1.6.1170.0
  2. Наличие файлов AE DLC (Creation Club)
  3. Определяет: лицензия / пиратка (по наличию steam_api64.dll + Steam)
"""
import os
import re
import sys
from pathlib import Path
from typing import Optional

from config import SKYRIM_TARGET_VERSION, SKYRIM_EXE_NAME, SKYRIM_AE_DLC_FILES

try:
    import winreg
except ImportError:
    winreg = None

# Импорты для работы с версией через WinAPI
import ctypes
import ctypes.wintypes


# ── Dataclass-like result ─────────────────────────────────────────────────────

class SkyrimCheckResult:
    def __init__(self):
        self.found:          bool          = False
        self.skyrim_dir:     Optional[str] = None   # папка с SkyrimSE.exe
        self.exe_version:    Optional[str] = None   # "1.6.1170.0"
        self.version_ok:     bool          = False  # == 1.6.1170.0
        self.dlc_ok:         bool          = False  # все AE DLC на месте
        self.missing_dlc:    list          = []
        self.is_licensed:    bool          = False  # определяем по steam_api + реестру
        self.via_steam:      bool          = False  # найдено через Steam libraryfolders

    @property
    def is_ae_complete(self) -> bool:
        """Версия правильная + все DLC"""
        return self.found and self.version_ok and self.dlc_ok

    def __str__(self):
        if not self.found:
            return "Skyrim Special Edition не найден"
        parts = [f"Skyrim: {self.skyrim_dir}"]
        parts.append(f"Версия: {self.exe_version or '?'} {'✓' if self.version_ok else '✗ (нужна 1.6.1170.0)'}")
        parts.append(f"AE DLC: {'✓ все' if self.dlc_ok else f'✗ отсутствует {len(self.missing_dlc)} файлов'}")
        parts.append(f"Тип: {'Лицензия' if self.is_licensed else 'Нелицензионная'}")
        return "  ".join(parts)


# ── Main checker ──────────────────────────────────────────────────────────────

class SkyrimChecker:
    """Поиск и проверка Skyrim AE. Синхронный, запускать из QThread."""

    TARGET_VERSION = SKYRIM_TARGET_VERSION

    def check(self, log=None) -> SkyrimCheckResult:
        """Полная проверка. log — callable(str) для прогресса."""
        result = SkyrimCheckResult()

        def _log(msg):
            if log:
                log(msg)

        _log("Ищем Skyrim Special Edition...")

        # 1. Собираем кандидатов
        skyrim_exe = self._find_skyrim_exe(_log)

        if not skyrim_exe:
            _log("Skyrim Special Edition не найден")
            return result

        skyrim_dir   = str(Path(skyrim_exe).parent)
        result.found      = True
        result.skyrim_dir = skyrim_dir
        _log(f"Найден: {skyrim_exe}")

        # 2. Проверяем версию exe
        result.exe_version = self._get_exe_version(skyrim_exe)
        result.version_ok  = result.exe_version == self.TARGET_VERSION
        _log(f"Версия: {result.exe_version or 'неизвестна'}"
             + (" ✓" if result.version_ok else f" ✗ (нужна {self.TARGET_VERSION})"))

        # 3. Проверяем AE DLC
        missing = []
        for dlc_file in SKYRIM_AE_DLC_FILES:
            if not Path(skyrim_dir, "Data", dlc_file).exists():
                missing.append(dlc_file)
        result.missing_dlc = missing
        result.dlc_ok      = len(missing) == 0
        if result.dlc_ok:
            _log("AE DLC: все файлы на месте ✓")
        else:
            _log(f"AE DLC: отсутствует {len(missing)} файлов ✗")

        # 4. Лицензия / пиратка
        result.is_licensed = self._is_licensed(skyrim_dir)
        result.via_steam   = self._is_via_steam(skyrim_dir)
        _log(f"Тип копии: {'Лицензия' if result.is_licensed else 'Нелицензионная'}")

        return result

    # ── Поиск exe ─────────────────────────────────────────────────────────────

    def _find_skyrim_exe(self, log) -> Optional[str]:
        candidates = []

        # 1. Steam registry + libraryfolders
        for path in self._steam_library_paths(log):
            exe = Path(path) / "steamapps" / "common" / "Skyrim Special Edition" / SKYRIM_EXE_NAME
            if exe.exists():
                candidates.append(str(exe))

        # 2. Стандартные пути Steam
        for steam_root in self._standard_steam_paths():
            exe = Path(steam_root) / "steamapps" / "common" / "Skyrim Special Edition" / SKYRIM_EXE_NAME
            if exe.exists() and str(exe) not in candidates:
                candidates.append(str(exe))

        # 3. Быстрый поиск по дискам (глубина 4)
        if not candidates:
            log("Расширенный поиск по дискам...")
            for drive in self._available_drives():
                found = self._search_drive(drive, depth=4)
                if found:
                    candidates.append(found)
                    break

        # Приоритет: версия 1.6.1170.0
        for c in candidates:
            if self._get_exe_version(c) == self.TARGET_VERSION:
                return c
        return candidates[0] if candidates else None

    def _steam_library_paths(self, log) -> list:
        paths = []
        if winreg is None:
            return paths
        registry_keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Valve\Steam"),
        ]
        steam_roots = []
        for hive, key_path in registry_keys:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                    if install_path and Path(install_path).exists():
                        steam_roots.append(install_path)
            except Exception:
                continue

        for root in steam_roots:
            lf = Path(root) / "steamapps" / "libraryfolders.vdf"
            if not lf.exists():
                continue
            try:
                content = lf.read_text(encoding="utf-8", errors="ignore")
                # Парсим путь из VDF
                try:
                    import vdf
                    data = vdf.loads(content)
                    lib  = data.get("libraryfolders", {})
                    for v in lib.values():
                        if isinstance(v, dict) and "path" in v:
                            paths.append(v["path"].replace("\\\\", "\\"))
                except Exception:
                    # Fallback regex
                    for m in re.finditer(r'"path"\s+"([^"]+)"', content):
                        paths.append(m.group(1).replace("\\\\", "\\"))
            except Exception:
                continue
        return paths

    def _standard_steam_paths(self) -> list:
        result = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            for sub in (r"\Steam", r"\Program Files\Steam", r"\Program Files (x86)\Steam"):
                p = f"{letter}:{sub}"
                if Path(p).exists():
                    result.append(p)
        return result

    def _available_drives(self) -> list:
        if sys.platform != "win32":
            return ["/"]
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            d = f"{letter}:\\"
            if Path(d).exists():
                drives.append(d)
        return drives

    def _search_drive(self, drive: str, depth: int = 4) -> Optional[str]:
        try:
            for root, dirs, files in os.walk(drive, topdown=True):
                current_depth = root.replace(drive, "").count(os.sep)
                if current_depth >= depth:
                    dirs.clear()
                    continue
                if SKYRIM_EXE_NAME in files:
                    candidate = Path(root) / SKYRIM_EXE_NAME
                    # Быстрая проверка — имя папки
                    if "Skyrim" in root:
                        return str(candidate)
        except Exception:
            pass
        return None

    # ── Версия PE (исправлено через WinAPI) ───────────────────────────────────

    @staticmethod
    def _get_exe_version(exe_path: str) -> Optional[str]:
        """
        Возвращает версию файла в формате major.minor.build.patch
        используя Windows API GetFileVersionInfo.
        """
        try:
            ver_api = ctypes.windll.version

            size = ver_api.GetFileVersionInfoSizeW(exe_path, None)
            if size == 0:
                return None

            buffer = ctypes.create_string_buffer(size)
            if not ver_api.GetFileVersionInfoW(exe_path, 0, size, buffer):
                return None

            fixed_info_ptr = ctypes.c_void_p()
            fixed_info_len = ctypes.c_uint32()
            if not ver_api.VerQueryValueW(buffer, "\\", ctypes.byref(fixed_info_ptr), ctypes.byref(fixed_info_len)):
                return None

            class VS_FIXEDFILEINFO(ctypes.Structure):
                _fields_ = [
                    ("dwSignature", ctypes.c_uint32),
                    ("dwStrucVersion", ctypes.c_uint32),
                    ("dwFileVersionMS", ctypes.c_uint32),
                    ("dwFileVersionLS", ctypes.c_uint32),
                    ("dwProductVersionMS", ctypes.c_uint32),
                    ("dwProductVersionLS", ctypes.c_uint32),
                    ("dwFileFlagsMask", ctypes.c_uint32),
                    ("dwFileFlags", ctypes.c_uint32),
                    ("dwFileOS", ctypes.c_uint32),
                    ("dwFileType", ctypes.c_uint32),
                    ("dwFileSubtype", ctypes.c_uint32),
                    ("dwFileDateMS", ctypes.c_uint32),
                    ("dwFileDateLS", ctypes.c_uint32),
                ]

            info = ctypes.cast(fixed_info_ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
            if info.dwSignature != 0xFEEF04BD:
                return None

            major = info.dwFileVersionMS >> 16
            minor = info.dwFileVersionMS & 0xFFFF
            build = info.dwFileVersionLS >> 16
            patch = info.dwFileVersionLS & 0xFFFF

            return f"{major}.{minor}.{build}.{patch}"
        except Exception:
            return None

    # ── Лицензия ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_licensed(skyrim_dir: str) -> bool:
        """
        Считаем лицензионной если:
          • есть steam_api64.dll
          • Steam запущен или зарегистрирован в реестре
        """
        if not Path(skyrim_dir, "steam_api64.dll").exists():
            return False  # пиратка всегда убирает steamapi
        if winreg is None:
            return True  # нет winreg — просто верим steam_api64.dll
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"):
                return True
        except Exception:
            return False

    @staticmethod
    def _is_via_steam(skyrim_dir: str) -> bool:
        """Путь содержит steamapps — значит установлена через Steam."""
        return "steamapps" in skyrim_dir.replace("\\", "/").lower()