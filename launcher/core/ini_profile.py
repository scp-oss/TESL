# ==================== launcher/core/ini_profile.py ====================
"""
Применение Skyrim.ini/SkyrimPrefs.ini для текущей сборки на клиенте —
прямой запрос пользователя 2026-10-06 (прислал оба файла как шаблоны,
попросил разложить по `C:\\Users\\<ник>\\Documents\\My Games\\Skyrim Special
Edition\\`, создав папку, если её нет, и "заодно проверить" файлы).

**Это НЕ готовые ini — это шаблоны.** Оба присланных файла (сохранены как
`launcher/assets/ini_profile/{Skyrim,SkyrimPrefs}.ini`) содержат 65
плейсхолдеров вида `{{SHADOW_DISTANCE}}` вместо реальных значений — если
записать их на диск как есть, Skyrim получит мусорные строки вроде
`fShadowDistance={{SHADOW_DISTANCE}}` и либо проигнорирует их, либо не
стартует. Перед записью все плейсхолдеры должны быть заменены.

**Откуда берутся значения** — прямой выбор пользователя (а не моя
догадка): автоподбор по железу клиента, тем же принципом, что
"Low/Medium/High/Ultra" в BethINI — определяется объём видеопамяти
(`_detect_gpu()`), выбирается один из четырёх тиров (`_pick_tier()`), и
для каждого тира уже есть готовая таблица значений (`TIER_VALUES`).
Разрешение экрана (`RES_W`/`RES_H`) и имя GPU (`GPU_NAME`) всегда
определяются живьём, не зависят от тира.

**Важная оговорка, честно, не для галочки**: `TIER_VALUES` — это МОЙ
собственный эвристический набор чисел (аналог того, как BethINI сама
масштабирует LOD/тени/дистанции между своими пресетами), не выгрузка из
авторитетного источника конкретно под эту сборку — ни один файл в этом
репозитории не содержал "правильных" значений для 65 ключей этого
конкретного профиля. Если после живого теста на Windows какой-то тир
выглядит явно неверно (слишком тяжело/слишком плоско) — править нужно
именно `TIER_VALUES` ниже, не сам механизм подстановки.

**Обнаружение GPU/VRAM — только Windows** (`wmic`, с откатом на
PowerShell `Get-CimInstance`, если `wmic` недоступен — он официально
deprecated в новых сборках Windows, но пока ещё обычно присутствует) —
при любой ошибке (Linux-песочница разработки в т.ч.) тихо откатывается
на `UNKNOWN_GPU`/тир "medium", без исключения наружу.

Используется из `core/workers.py::PostInstallWorker.run()` — тот же шаг,
что уже докачивает иконку/аргумент ярлыка и создаёт .lnk после установки
(см. его собственный докстринг про перенос с GUI-потока) — best-effort,
никогда не валит весь post-install, если что-то пошло не так.
"""
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple

import config

try:
    import winreg
except ImportError:
    winreg = None

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

_ENUM_CURRENT_SETTINGS = -1


class _DEVMODEW(ctypes.Structure):
    """Подмножество полей WinAPI `DEVMODEW`, нужное для чтения текущего
    видеорежима через `EnumDisplaySettingsW` — байтовые смещения до
    `dmPelsWidth`/`dmPelsHeight` совпадают с реальной структурой
    независимо от того, что именно объявлено в union-блоке между
    `dmDriverExtra` и `dmColor` (printer-specific поля здесь занимают
    ровно те же 16 байт, что display-specific `dmPosition`/
    `dmDisplayOrientation`/`dmDisplayFixedOutput` — нужные нам поля идут
    ПОСЛЕ этого union, значения самого union не читаются)."""
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]

_TEMPLATE_FILES = ("Skyrim.ini", "SkyrimPrefs.ini")

UNKNOWN_GPU = "Unknown GPU"
DEFAULT_RESOLUTION = (1920, 1080)
DEFAULT_TIER = "medium"

# ── Значения, не зависящие от тира (не про графику/производительность) ────────
COMMON_VALUES: Dict[str, str] = {
    "LANGUAGE": "russian",           # sFontConfigFile=...FontConfig_ru.txt, Voices_ru0.bsa — сборка русская
    "DIFFICULTY": "2",               # vanilla "Новичок+1" (Adept), не тюнится по железу
    "GAMEPAD": "0",                  # по умолчанию клавиатура+мышь
    "AUTOSAVE_MINS": "15",           # игровое удобство, не связано с производительностью
    "EXTERIOR_CELL_BUFFER": "36",    # vanilla default (uGridsToLoad=5-related), не тюнится по железу
    "BORDERLESS": "1",               # borderless windowed — меньше проблем с alt-tab/оверлеями
    "FULLSCREEN": "0",
    "VSYNC": "0",                    # большинство гайдов рекомендуют off + внешний FPS-кап
}

# ── Таблица значений по тирам (low/medium/high/ultra) ──────────────────────────
# Эвристика, см. докстринг модуля — не авторитетный источник, подбор по
# аналогии с BethINI-подобным масштабированием между пресетами качества.
TIER_VALUES: Dict[str, Dict[str, str]] = {
    "low": {
        "SHADOW_LOD_MAX_START_FADE": "2400", "SPECULAR_LOD_MAX_START_FADE": "1500",
        "LIGHT_LOD_MAX_START_FADE": "1500", "LIGHT_LOD_START_FADE": "1200",
        "SHADOW_MAP_RESOLUTION_PRIMARY": "1024", "TREES_MID_LOD": "1000",
        "FIRST_SLICE_DISTANCE": "100", "DYNAMIC_DOF_FAR_BLUR": "4",
        "DOF_MAX_DEPTH": "1", "GRASS_MAX_FADE": "2000",
        "MAX_SKIN_DECAL_PER_ACTOR": "3", "RADIAL_BLUR_LEVEL": "0",
        "SHADOW_DISTANCE": "2000", "NUM_FOCUS_SHADOW": "1",
        "INTERIOR_SHADOW_DISTANCE": "1500", "SHADOW_MAP_RESOLUTION": "512",
        "TAA": "0", "SAO": "0", "SSR": "0", "IBLF": "0",
        "SHADOW_MASK_QUARTER": "0", "VOLUMETRIC_LIGHTING": "0",
        "VOLUMETRIC_QUALITY": "0", "IMPROVED_SNOW": "0", "FXAA": "1",
        "LEAF_DAMPEN_END": "2500", "LEAF_DAMPEN_START": "1200",
        "MESH_LOD_LEVEL2_TREE": "140", "MESH_LOD_LEVEL1_TREE": "2000",
        "MESH_LOD_LEVEL2_FADE": "2500", "MESH_LOD_LEVEL1_FADE": "1000",
        "TREES_RECEIVE_SHADOWS": "0", "DRAW_LAND_SHADOWS": "0",
        "MAX_SKIN_DECALS_PER_FRAME": "3", "MAX_DECALS_PER_FRAME": "10",
        "NUM_SPLITS": "1", "REFLECTION_RES_DIVIDER": "8",
        "DOF": "0", "LENS_FLARE": "0",
        "U_LARGE_REF_LOD_GRID_SIZE": "5", "SKY_CELL_REF_FADE": "20000",
        "MAX_PARTICLES": "150", "TREE_LOAD_DISTANCE": "20000",
        "BLOCK_MAX_DISTANCE": "70000", "BLOCK_LEVEL1_DISTANCE": "35000",
        "BLOCK_LEVEL0_DISTANCE": "17000", "SPLIT_DISTANCE_MULT": "1.5",
        "MAX_SKINNED_TREES": "10", "GRASS_START_FADE": "1000",
        "MAX_SKIN_DECALS": "50", "MAX_DECALS": "100",
        "LOD_FADE_OBJECTS": "0.5", "LOD_FADE_ITEMS": "0.5", "LOD_FADE_ACTORS": "0.5",
    },
    "medium": {
        "SHADOW_LOD_MAX_START_FADE": "4096", "SPECULAR_LOD_MAX_START_FADE": "2800",
        "LIGHT_LOD_MAX_START_FADE": "2800", "LIGHT_LOD_START_FADE": "2400",
        "SHADOW_MAP_RESOLUTION_PRIMARY": "2048", "TREES_MID_LOD": "2500",
        "FIRST_SLICE_DISTANCE": "200", "DYNAMIC_DOF_FAR_BLUR": "5",
        "DOF_MAX_DEPTH": "1.25", "GRASS_MAX_FADE": "4000",
        "MAX_SKIN_DECAL_PER_ACTOR": "5", "RADIAL_BLUR_LEVEL": "1",
        "SHADOW_DISTANCE": "4000", "NUM_FOCUS_SHADOW": "2",
        "INTERIOR_SHADOW_DISTANCE": "3000", "SHADOW_MAP_RESOLUTION": "1024",
        "TAA": "0", "SAO": "1", "SSR": "0", "IBLF": "0",
        "SHADOW_MASK_QUARTER": "1", "VOLUMETRIC_LIGHTING": "0",
        "VOLUMETRIC_QUALITY": "1", "IMPROVED_SNOW": "1", "FXAA": "1",
        "LEAF_DAMPEN_END": "4000", "LEAF_DAMPEN_START": "2000",
        "MESH_LOD_LEVEL2_TREE": "281", "MESH_LOD_LEVEL1_TREE": "4000",
        "MESH_LOD_LEVEL2_FADE": "5000", "MESH_LOD_LEVEL1_FADE": "2000",
        "TREES_RECEIVE_SHADOWS": "1", "DRAW_LAND_SHADOWS": "1",
        "MAX_SKIN_DECALS_PER_FRAME": "8", "MAX_DECALS_PER_FRAME": "25",
        "NUM_SPLITS": "2", "REFLECTION_RES_DIVIDER": "4",
        "DOF": "1", "LENS_FLARE": "1",
        "U_LARGE_REF_LOD_GRID_SIZE": "7", "SKY_CELL_REF_FADE": "40000",
        "MAX_PARTICLES": "300", "TREE_LOAD_DISTANCE": "40000",
        "BLOCK_MAX_DISTANCE": "140000", "BLOCK_LEVEL1_DISTANCE": "60000",
        "BLOCK_LEVEL0_DISTANCE": "30000", "SPLIT_DISTANCE_MULT": "2",
        "MAX_SKINNED_TREES": "20", "GRASS_START_FADE": "2000",
        "MAX_SKIN_DECALS": "100", "MAX_DECALS": "250",
        "LOD_FADE_OBJECTS": "1.0", "LOD_FADE_ITEMS": "1.0", "LOD_FADE_ACTORS": "1.0",
    },
    "high": {
        "SHADOW_LOD_MAX_START_FADE": "6144", "SPECULAR_LOD_MAX_START_FADE": "4096",
        "LIGHT_LOD_MAX_START_FADE": "4096", "LIGHT_LOD_START_FADE": "3377",
        "SHADOW_MAP_RESOLUTION_PRIMARY": "4096", "TREES_MID_LOD": "5500",
        "FIRST_SLICE_DISTANCE": "300", "DYNAMIC_DOF_FAR_BLUR": "6",
        "DOF_MAX_DEPTH": "1.5", "GRASS_MAX_FADE": "7000",
        "MAX_SKIN_DECAL_PER_ACTOR": "10", "RADIAL_BLUR_LEVEL": "1",
        "SHADOW_DISTANCE": "8000", "NUM_FOCUS_SHADOW": "3",
        "INTERIOR_SHADOW_DISTANCE": "4000", "SHADOW_MAP_RESOLUTION": "2048",
        "TAA": "1", "SAO": "1", "SSR": "1", "IBLF": "1",
        "SHADOW_MASK_QUARTER": "2", "VOLUMETRIC_LIGHTING": "1",
        "VOLUMETRIC_QUALITY": "2", "IMPROVED_SNOW": "1", "FXAA": "0",
        "LEAF_DAMPEN_END": "5500", "LEAF_DAMPEN_START": "3000",
        "MESH_LOD_LEVEL2_TREE": "420", "MESH_LOD_LEVEL1_TREE": "6000",
        "MESH_LOD_LEVEL2_FADE": "7500", "MESH_LOD_LEVEL1_FADE": "3000",
        "TREES_RECEIVE_SHADOWS": "1", "DRAW_LAND_SHADOWS": "1",
        "MAX_SKIN_DECALS_PER_FRAME": "15", "MAX_DECALS_PER_FRAME": "50",
        "NUM_SPLITS": "2", "REFLECTION_RES_DIVIDER": "2",
        "DOF": "1", "LENS_FLARE": "1",
        "U_LARGE_REF_LOD_GRID_SIZE": "11", "SKY_CELL_REF_FADE": "70000",
        "MAX_PARTICLES": "450", "TREE_LOAD_DISTANCE": "65536",
        "BLOCK_MAX_DISTANCE": "250000", "BLOCK_LEVEL1_DISTANCE": "100000",
        "BLOCK_LEVEL0_DISTANCE": "50000", "SPLIT_DISTANCE_MULT": "2.5",
        "MAX_SKINNED_TREES": "25", "GRASS_START_FADE": "3000",
        "MAX_SKIN_DECALS": "200", "MAX_DECALS": "500",
        "LOD_FADE_OBJECTS": "1.0", "LOD_FADE_ITEMS": "1.0", "LOD_FADE_ACTORS": "1.0",
    },
    "ultra": {
        "SHADOW_LOD_MAX_START_FADE": "8192", "SPECULAR_LOD_MAX_START_FADE": "6000",
        "LIGHT_LOD_MAX_START_FADE": "6000", "LIGHT_LOD_START_FADE": "4800",
        "SHADOW_MAP_RESOLUTION_PRIMARY": "8192", "TREES_MID_LOD": "8500",
        "FIRST_SLICE_DISTANCE": "400", "DYNAMIC_DOF_FAR_BLUR": "7",
        "DOF_MAX_DEPTH": "2", "GRASS_MAX_FADE": "10000",
        "MAX_SKIN_DECAL_PER_ACTOR": "20", "RADIAL_BLUR_LEVEL": "2",
        "SHADOW_DISTANCE": "12000", "NUM_FOCUS_SHADOW": "4",
        "INTERIOR_SHADOW_DISTANCE": "6000", "SHADOW_MAP_RESOLUTION": "4096",
        "TAA": "1", "SAO": "1", "SSR": "1", "IBLF": "1",
        "SHADOW_MASK_QUARTER": "3", "VOLUMETRIC_LIGHTING": "1",
        "VOLUMETRIC_QUALITY": "4", "IMPROVED_SNOW": "1", "FXAA": "0",
        "LEAF_DAMPEN_END": "7000", "LEAF_DAMPEN_START": "4000",
        "MESH_LOD_LEVEL2_TREE": "560", "MESH_LOD_LEVEL1_TREE": "8000",
        "MESH_LOD_LEVEL2_FADE": "10000", "MESH_LOD_LEVEL1_FADE": "4000",
        "TREES_RECEIVE_SHADOWS": "1", "DRAW_LAND_SHADOWS": "1",
        "MAX_SKIN_DECALS_PER_FRAME": "25", "MAX_DECALS_PER_FRAME": "100",
        "NUM_SPLITS": "3", "REFLECTION_RES_DIVIDER": "1",
        "DOF": "1", "LENS_FLARE": "1",
        "U_LARGE_REF_LOD_GRID_SIZE": "13", "SKY_CELL_REF_FADE": "100000",
        "MAX_PARTICLES": "700", "TREE_LOAD_DISTANCE": "90000",
        "BLOCK_MAX_DISTANCE": "350000", "BLOCK_LEVEL1_DISTANCE": "140000",
        "BLOCK_LEVEL0_DISTANCE": "70000", "SPLIT_DISTANCE_MULT": "3",
        "MAX_SKINNED_TREES": "40", "GRASS_START_FADE": "4500",
        "MAX_SKIN_DECALS": "400", "MAX_DECALS": "1000",
        "LOD_FADE_OBJECTS": "1.0", "LOD_FADE_ITEMS": "1.0", "LOD_FADE_ACTORS": "1.0",
    },
}

# Границы VRAM (МБ) для выбора тира — ultra требует >=10GB, high >=6GB,
# medium >=3GB, иначе low. Эвристика, не калибровка под конкретные модели.
_TIER_VRAM_THRESHOLDS_MB = (
    (10 * 1024, "ultra"),
    (6 * 1024, "high"),
    (3 * 1024, "medium"),
)


def get_skyrim_documents_dir() -> Path:
    """`~/Documents/My Games/Skyrim Special Edition` — реальный путь
    (учитывает `newab`/любой другой профиль Windows через Path.home()),
    не хардкод конкретного пользователя из примера."""
    return Path.home() / "Documents" / "My Games" / "Skyrim Special Edition"


def _ensure_dpi_aware() -> None:
    """Без этого `GetSystemMetrics` честно ЛЖЁТ про реальное разрешение —
    для DPI-неосведомлённого процесса Windows отдаёт МАСШТАБИРОВАННЫЕ под
    текущий DPI значения, не настоящие физические пиксели (пример: честные
    3840x2160 при масштабе экрана 150% превращаются в 2560x1440). Прямой
    отчёт пользователя: "разрешение не моё" — именно этот класс искажения.
    Безопасно звать повторно — если DPI-awareness уже выставлен (например,
    самим PyQt6/Qt до создания этого окна), повторный вызов просто падает
    с `OSError` (`E_ACCESSDENIED`), тихо проглатывается."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _detect_resolution() -> Tuple[int, int]:
    """Живой отчёт пользователя: даже после `_ensure_dpi_aware()` +
    `GetSystemMetrics` разрешение в применённом `SkyrimPrefs.ini`
    по-прежнему не совпадало с реальным экраном ("всё ещё разрешение не
    на весь экран") — вероятная причина: PyQt6/Qt уже выставляет
    СВОЙ DPI-awareness context ДО того, как этот код вообще успевает
    вызваться (кнопка кликается уже внутри запущенного GUI) — повторный
    `SetProcessDpiAwareness()` в этом случае либо no-op, либо не тот
    уровень awareness, и `GetSystemMetrics` всё ещё может отдавать не
    то, что нужно.

    **`EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ...)` — надёжнее
    в принципе**: эта функция читает ТЕКУЩИЙ РЕЖИМ ДРАЙВЕРА ДИСПЛЕЯ
    напрямую (`dmPelsWidth`/`dmPelsHeight`) — она в принципе не проходит
    через DPI-виртуализацию GetSystemMetrics и не зависит от
    DPI-awareness вызывающего процесса вообще, именно поэтому теперь
    основной путь, а не DPI-awareness+GetSystemMetrics (тот остаётся
    вторым фолбэком на случай, если EnumDisplaySettingsW почему-то
    недоступна/отказала)."""
    if sys.platform == "win32":
        try:
            devmode = _DEVMODEW()
            devmode.dmSize = ctypes.sizeof(_DEVMODEW)
            if ctypes.windll.user32.EnumDisplaySettingsW(
                None, _ENUM_CURRENT_SETTINGS, ctypes.byref(devmode)
            ):
                w, h = int(devmode.dmPelsWidth), int(devmode.dmPelsHeight)
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
        try:
            _ensure_dpi_aware()
            user32 = ctypes.windll.user32
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            if w > 0 and h > 0:
                return int(w), int(h)
        except Exception:
            pass
    return DEFAULT_RESOLUTION


_DISPLAY_CLASS_GUID = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def _detect_vram_registry_mb() -> int:
    """Максимальный `HardwareInformation.qwMemorySize` (QWORD, байты) из
    реестра среди всех подключённых видеоадаптеров — обходит живой баг
    WMI/`Win32_VideoController.AdapterRAM`: то поле — 32-битный DWORD,
    физически не может представить ≥4GB VRAM и либо переполняется, либо
    молча обрезается драйвером при публикации в WMI. Живой отчёт
    пользователя: AMD Radeon RX 7700 XT (реально 12GB VRAM) определился
    как 4095 МБ — ровно эта картина (упёрлись в потолок чуть ниже 4096).
    `qwMemorySize` — 64-битное поле, этого ограничения не имеет, тот же
    путь, что используют GPU-Z и аналогичные утилиты. Возвращает 0 при
    любой проблеме (не Windows, ключа нет, старый драйвер без этого
    поля) — тогда вызывающий код остаётся на значении от wmic/
    PowerShell."""
    if winreg is None:
        return 0
    best_mb = 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_GUID) as class_key:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(class_key, i)
                except OSError:
                    break
                i += 1
                if not subkey_name.isdigit():
                    continue  # "Properties" и другие служебные подключи, не индекс адаптера
                try:
                    with winreg.OpenKey(class_key, subkey_name) as sub:
                        value, _ = winreg.QueryValueEx(sub, "HardwareInformation.qwMemorySize")
                        mb = int(value) // (1024 * 1024)
                        if mb > best_mb:
                            best_mb = mb
                except OSError:
                    continue
    except Exception:
        return 0
    return best_mb


def _detect_gpu() -> Tuple[str, int]:
    """(имя GPU, VRAM в МБ). Только Windows — `wmic`, с откатом на
    PowerShell `Get-CimInstance`, если wmic отсутствует (deprecated в
    новых сборках). Любая ошибка → (UNKNOWN_GPU, 0), не бросает наружу.
    VRAM дополнительно сверяется с `_detect_vram_registry_mb()` — если
    реестр отдаёт БОЛЬШЕЕ значение, используется оно (см. его докстринг
    про 32-битный потолок `AdapterRAM`); если реестр недоступен/меньше —
    остаётся значение от wmic/PowerShell, регрессии для карт <4GB нет."""
    if sys.platform != "win32":
        return UNKNOWN_GPU, 0

    def _run(cmd) -> str:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).decode("utf-8", errors="replace")

    name = UNKNOWN_GPU
    vram_mb = 0
    try:
        out = _run(["wmic", "path", "win32_VideoController", "get", "Name,AdapterRAM", "/format:list"])
        for block in out.split("\n\n"):
            block_name, block_ram = None, 0
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("Name="):
                    block_name = line.split("=", 1)[1].strip()
                elif line.startswith("AdapterRAM="):
                    try:
                        block_ram = int(line.split("=", 1)[1].strip() or 0)
                    except ValueError:
                        block_ram = 0
            if block_name and block_ram > vram_mb * 1024 * 1024:
                name, vram_mb = block_name, block_ram // (1024 * 1024)
    except Exception:
        try:
            out = _run([
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | "
                "Select-Object Name,AdapterRAM | ConvertTo-Json",
            ])
            import json
            data = json.loads(out)
            items = data if isinstance(data, list) else [data]
            for item in items:
                ram = int(item.get("AdapterRAM") or 0)
                if ram > vram_mb * 1024 * 1024:
                    name, vram_mb = item.get("Name") or UNKNOWN_GPU, ram // (1024 * 1024)
        except Exception:
            return UNKNOWN_GPU, 0

    reg_vram_mb = _detect_vram_registry_mb()
    if reg_vram_mb > vram_mb:
        vram_mb = reg_vram_mb

    return name or UNKNOWN_GPU, vram_mb


def _pick_tier(vram_mb: int) -> str:
    for threshold, tier in _TIER_VRAM_THRESHOLDS_MB:
        if vram_mb >= threshold:
            return tier
    return "low" if vram_mb > 0 else DEFAULT_TIER


def build_placeholder_values(tier: str, res_w: int, res_h: int, gpu_name: str) -> Dict[str, str]:
    if tier not in TIER_VALUES:
        tier = DEFAULT_TIER
    values = dict(COMMON_VALUES)
    values.update(TIER_VALUES[tier])
    values["RES_W"] = str(res_w)
    values["RES_H"] = str(res_h)
    values["GPU_NAME"] = gpu_name
    return values


def render_template(text: str, values: Dict[str, str]) -> Tuple[str, list]:
    """Подставляет `{{KEY}}` из `values`. Возвращает (результат,
    список_незаполненных_ключей) — пустой список = все плейсхолдеры
    закрыты, нет риска записать мусорную строку в INI."""
    missing = []

    def _sub(m: "re.Match") -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        missing.append(key)
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, text), missing


def check_templates() -> Dict[str, list]:
    """Офлайн-проверка ("заодно проверь их"): для каждого шаблонного
    файла и каждого тира — какие плейсхолдеры остались бы незаполненными.
    Пустой словарь = все шаблоны полностью покрыты таблицей значений для
    всех тиров. Не требует Windows/реального железа."""
    problems: Dict[str, list] = {}
    for fname in _TEMPLATE_FILES:
        text = config.get_asset_path(f"assets/ini_profile/{fname}").read_text(encoding="utf-8")
        for tier in TIER_VALUES:
            values = build_placeholder_values(tier, *DEFAULT_RESOLUTION, UNKNOWN_GPU)
            _, missing = render_template(text, values)
            if missing:
                problems[f"{fname} [{tier}]"] = missing
    return problems


def apply_ini_profile(log=print) -> bool:
    """Главная точка входа — детект железа → рендер обоих шаблонов →
    запись в `Documents/My Games/Skyrim Special Edition` (создаёт папку,
    если её нет). Best-effort: любая ошибка логируется и возвращает
    False, не бросает исключение (вызывается из PostInstallWorker —
    не должно ронять остальной post-install)."""
    try:
        res_w, res_h = _detect_resolution()
        gpu_name, vram_mb = _detect_gpu()
        tier = _pick_tier(vram_mb)
        log(
            f"🖴 Профиль графики: GPU={gpu_name} ({vram_mb} МБ VRAM) → "
            f"тир «{tier}», разрешение {res_w}x{res_h}"
        )

        values = build_placeholder_values(tier, res_w, res_h, gpu_name)

        dest_dir = get_skyrim_documents_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)

        for fname in _TEMPLATE_FILES:
            src = config.get_asset_path(f"assets/ini_profile/{fname}")
            text = src.read_text(encoding="utf-8")
            rendered, missing = render_template(text, values)
            if missing:
                log(f"⚠️ {fname}: не заполнены плейсхолдеры {missing} — файл не записан")
                return False
            (dest_dir / fname).write_text(rendered, encoding="utf-8")

        log(f"✅ Skyrim.ini/SkyrimPrefs.ini применены в {dest_dir}")
        return True
    except Exception as e:
        log(f"❌ Не удалось применить профиль графики: {e}")
        return False
