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

## Шаблоны — теперь per-build через `documents/` на панели, не только bundled (2026-10-07)

Прямой запрос пользователя: "фиксы ты писал под эту сборку, возможно я
залью другую, и они применятся к ней" — справедливо про САМИ ШАБЛОНЫ
(`TIER_VALUES`/`COMMON_VALUES` ниже подобраны под конкретный модлист
TESVAE, не универсальны), НЕ про саму машинерию подстановки/детекта
железа (она одинаково корректна для любой MO2+Skyrim сборки, трогать
её ради этого не нужно).

`fetch_panel_ini_templates()` — читает `documents/` АКТИВНОЙ сборки
(`config.CURRENT_BUILD_ID`) через уже существующий, ничем не
изменённый механизм TESL-Manager/TESL-Panel (`documents_tab.py`
публикует произвольные файлы под `documents/<имя>` + `extras_manifest.
json`, `PanelDepotClient.fetch_extras_manifest()`/`get_bytes()` в этом
репозитории уже умели их читать — ни строчки на стороне панели/
менеджера менять не пришлось). Ищет документы с именами РОВНО
`Skyrim.ini`/`SkyrimPrefs.ini` (`enabled` истинно или отсутствует) —
это те же самые файлы-С-ПЛЕЙСХОЛДЕРАМИ (`{{SHADOW_DISTANCE}}` и т.п.),
просто загруженные оператором как обычный "шаблон настроек" через
вкладку "📄 Документы и патчи", без какой-либо специальной обработки
на стороне менеджера/панели — они хранят байты как есть.

Требует ОБА файла найденными разом — половинчатый профиль (один шаблон
с панели, другой bundled) рисковал бы смешать несогласованные друг с
другом значения. Если НЕ оба найдены (сборка ещё не публиковала свои
documents-шаблоны, панель недоступна, сборка не выбрана) —
`apply_ini_profile()` откатывается на `assets/ini_profile/*.ini`
(bundled, те же, что и раньше) с явным предупреждением в лог — другими
словами, пока оператор не прикрепит `Skyrim.ini`/`SkyrimPrefs.ini`
через панель к НОВОЙ сборке, та сборка продолжит получать
TESVAE-калиброванные значения по умолчанию (то же поведение, что было
до этого фикса) — НЕ полную тишину/отказ — сознательный выбор не
ломать уже работающую фичу ради сборки, которая ещё не существует,
но явно предупреждает в логе о риске, который пользователь описал.

## Оконный режим вынесен в отдельный патч, не в generic launcher (2026-10-07)

Прямое продолжение того же разговора: при уточнении, какие именно
"фиксы" привязаны к конкретной сборке, пользователь разделил их сам —
"пути мо можно оставить в лаунчере а вот оконный режим в патч".
Перепись пути MO2/отключение диалога смены игры (`core/patcher.py`)
одинаково верны для ЛЮБОЙ MO2+Skyrim сборки — остаются в лаунчере без
изменений. А вот `BORDERLESS`/`FULLSCREEN` в `COMMON_VALUES` ниже —
НЕ универсальное решение: безрамочный оконный режим нужен конкретно
для DLSS5/апскейлеров (эксклюзивный fullscreen-flip им мешает), обычный
игрок без DLSS5 предпочтёт обычный vanilla fullscreen. Раньше
`COMMON_VALUES` жёстко задавал `BORDERLESS=1`/`FULLSCREEN=0` для ВСЕХ
сборок через этот generic Python-код — тот же класс проблемы, от
которого уже ушли ini-шаблоны через `documents/` (см. раздел выше):
фикс, написанный под одну сборку, молча применился бы к другой.

**Фикс**: `COMMON_VALUES` теперь держит обычный vanilla
`FULLSCREEN="1"`/`BORDERLESS="0"` — то, что подходит ЛЮБОЙ сборке по
умолчанию. Переключение в безрамочный оконный режим — отдельный
`.bat`-патч, `launcher/patches/windowed_borderless.bat` (правит уже
записанный `SkyrimPrefs.ini` через PowerShell: `bFull Screen=0`/
`bBorderless=1`, с бэкапом и собственной идемпотентностью), который
оператор прикрепляет через TESL-Manager → "📄 Документы и патчи" →
"Патчи" только тем сборкам, где он реально нужен (прогоняется
`core/patch_runner.py` — см. его докстринг за контракт
очереди/идемпотентность на уровне лаунчера). Для сборки без этого
патча ничего не меняется — просто обычный fullscreen вместо
безрамочного окна.
"""
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

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
    "BORDERLESS": "0",               # обычный vanilla fullscreen по умолчанию — см. "Оконный режим
    "FULLSCREEN": "1",               # вынесен в отдельный патч" в докстринге модуля; не для всех
                                      # сборок нужен безрамочный режим (только DLSS5/апскейлеры)
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


_DPI_LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
_DPI_OVERRIDE_FLAG = "~ HIGHDPIAWARE"


def resolve_skyrim_exe_path(local_dir: Optional[str]) -> Optional[str]:
    """Best-effort поиск реального `SkyrimSE.exe` для DPI-фикса ниже —
    тот же приоритет, что `PostInstallWorker.run()` уже использует для
    `update_ini()` (бандл-компонент ЭТОЙ сборки, `<local_dir>/Skyrim/`,
    первым; внешний `SkyrimChecker` — фолбэк, только если компонента
    нет). Используется отдельно от `PostInstallWorker` в кнопке
    "Применить профиль" (`ApplyIniProfileWorker`), у которой нет уже
    готового результата детекта под рукой. Никогда не бросает исключение
    — `None`, если ничего не нашлось (DPI-шаг в этом случае просто
    пропускается, запись самих ini-файлов не зависит от этого)."""
    if not local_dir:
        return None
    try:
        bundled = Path(local_dir) / config.SKYRIM_COMPONENT_DIR / config.SKYRIM_EXE_NAME
        if bundled.is_file():
            return str(bundled)
    except Exception:
        pass
    try:
        from core.skyrim_checker import SkyrimChecker
        result = SkyrimChecker().check()
        if result.found and result.skyrim_dir:
            exe = Path(result.skyrim_dir) / config.SKYRIM_EXE_NAME
            if exe.is_file():
                return str(exe)
    except Exception:
        pass
    return None


def ensure_dpi_compat_override(exe_path: str, log=print) -> bool:
    """Отключает DPI-виртуализацию Windows ИМЕННО для процесса
    `SkyrimSE.exe` — регистровый эквивалент галочки на вкладке
    "Совместимость" exe-файла: "Переопределение масштабирования
    высокого разрешения экрана, выполняемое: Приложение". Это
    задокументированное поведение самой Windows (та же галочка пишет
    РОВНО это значение в реестр — не наш собственный/изобретённый
    механизм), доступное через реестр без похода в UI вручную на
    каждой машине.

    **Зачем, живой повод 2026-10-06**: ноутбук с физическим 2560x1440
    и масштабом ОС 120% — игра открывалась "как 4K", на экране было
    видно только 1/4 картинки (симптом согласуется: если движок
    рендерит в 2x больше пикселей по каждому измерению, видимая без
    компенсации область — ровно 1/4 по площади). `_detect_resolution()`
    уже пишет в `SkyrimPrefs.ini` правильное ФИЗИЧЕСКОЕ разрешение
    (через `EnumDisplaySettingsW`, не зависящий от DPI-awareness
    читающего процесса, см. его докстринг) — но это не делает DPI-
    осведомлённым сам процесс SkyrimSE.exe. Если движок (в отличие от
    НАШЕГО launcher.exe, которому `_ensure_dpi_aware()` ставит
    awareness напрямую в коде) сам DPI-неосведомлён, Windows
    виртуализирует ЕГО окно независимо от цифр в ini — тот же класс
    искажения, что `_detect_resolution()`'s докстринг уже объясняет
    для `GetSystemMetrics`, просто здесь он бьёт по чужому процессу,
    код которого мы не контролируем, значит чинится только снаружи,
    через реестр.

    Правит только СВОЙ флаг — если у `exe_path` уже есть другие
    записанные Windows/пользователем флаги совместимости в этом же
    значении (разделяются пробелом), они не трогаются, `~
    HIGHDPIAWARE` дописывается в конец, только если его там ещё нет
    (идемпотентно — повторный вызов на уже пропатченном пути не
    дублирует флаг). `winreg is None` (не Windows) или любая ошибка
    реестра — тихий `False`, не бросает исключение (вызывается из
    best-effort `apply_ini_profile()`)."""
    if winreg is None:
        return False
    try:
        resolved = str(Path(exe_path).resolve())
    except Exception:
        resolved = exe_path
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _DPI_LAYERS_KEY, 0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            try:
                current, _ = winreg.QueryValueEx(key, resolved)
            except FileNotFoundError:
                current = ""
            if _DPI_OVERRIDE_FLAG in current:
                return True
            new_value = f"{current} {_DPI_OVERRIDE_FLAG}".strip()
            winreg.SetValueEx(key, resolved, 0, winreg.REG_SZ, new_value)
            log(f"🖴 DPI-переопределение (Приложение) включено для {resolved}")
            return True
    except Exception as e:
        log(f"⚠️ Не удалось выставить DPI-переопределение для {resolved}: {e}")
        return False


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


def fetch_panel_ini_templates(log=print) -> Optional[Dict[str, str]]:
    """См. раздел "Шаблоны — теперь per-build через documents/..." в
    докстринге модуля. Возвращает `{"Skyrim.ini": text, "SkyrimPrefs.
    ini": text}` ТОЛЬКО если оба найдены и включены — иначе `None`
    (вызывающий код откатывается на bundled). Никогда не бросает
    исключение — любая сетевая/парсинг ошибка тихо даёт `None`, тот же
    принцип best-effort, что и у всего остального в этом модуле."""
    build_id = config.CURRENT_BUILD_ID
    if not build_id:
        return None
    client = None
    try:
        from core.panel_client import PanelDepotClient
        client = PanelDepotClient(build_id)
        manifest = client.fetch_extras_manifest()
        wanted = {name.lower(): name for name in _TEMPLATE_FILES}
        found: Dict[str, str] = {}
        for entry in manifest.get("documents", []):
            if entry.get("enabled", True) is False:
                continue
            path = entry.get("path", "")
            basename = path.rsplit("/", 1)[-1]
            canon = wanted.get(basename.lower())
            if not canon or canon in found:
                continue
            data = client.get_bytes(path)
            if data is None:
                continue
            try:
                found[canon] = data.decode("utf-8")
            except Exception:
                continue
        if len(found) == len(_TEMPLATE_FILES):
            log("📄 Найдены собственные шаблоны Skyrim.ini/SkyrimPrefs.ini "
                "для этой сборки на панели (documents/) — используются они.")
            return found
        return None
    except Exception:
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


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


def apply_ini_profile(log=print, skyrim_exe_path: Optional[str] = None) -> bool:
    """Главная точка входа — детект железа → рендер обоих шаблонов →
    запись в `Documents/My Games/Skyrim Special Edition` (создаёт папку,
    если её нет). Best-effort: любая ошибка логируется и возвращает
    False, не бросает исключение (вызывается из PostInstallWorker —
    не должно ронять остальной post-install).

    `skyrim_exe_path` (опционально) — если известен реальный путь к
    `SkyrimSE.exe` (см. `resolve_skyrim_exe_path()`), заодно выставляет
    DPI-переопределение для него (`ensure_dpi_compat_override()`) —
    чинит рассинхрон разрешения на экранах с масштабом ОС ≠100%, см. её
    докстринг. Без пути (не передан/не найден) этот шаг просто
    пропускается — запись самих ini-файлов от него не зависит."""
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

        panel_templates = fetch_panel_ini_templates(log=log)
        if panel_templates is None:
            log(
                "ℹ️ Для этой сборки нет собственных шаблонов Skyrim.ini/"
                "SkyrimPrefs.ini на панели (documents/) — используется "
                "встроенный профиль, подобранный под TESVAE; для ДРУГОЙ "
                "сборки он может быть неверным."
            )

        for fname in _TEMPLATE_FILES:
            if panel_templates is not None:
                text = panel_templates[fname]
            else:
                src = config.get_asset_path(f"assets/ini_profile/{fname}")
                text = src.read_text(encoding="utf-8")
            rendered, missing = render_template(text, values)
            if missing:
                log(f"⚠️ {fname}: не заполнены плейсхолдеры {missing} — файл не записан")
                return False
            (dest_dir / fname).write_text(rendered, encoding="utf-8")

        log(f"✅ Skyrim.ini/SkyrimPrefs.ini применены в {dest_dir}")

        if skyrim_exe_path:
            ensure_dpi_compat_override(skyrim_exe_path, log=log)

        return True
    except Exception as e:
        log(f"❌ Не удалось применить профиль графики: {e}")
        return False
