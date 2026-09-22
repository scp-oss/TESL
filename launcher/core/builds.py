# ==================== launcher/core/builds.py ====================
"""
Build — одна доступная для установки сборка, показывается плиткой в
карусели на старте лаунчера (см. ui/carousel_window.py).

Сейчас реально существует ровно одна сборка (TESVAE, зашита константами
в config.py). Реестр на сервере (config.BUILDS_REGISTRY_PATH) ещё НЕ
подтверждён — list_builds() откатывается на эту единственную сборку, если
реестра нет (обычный, ожидаемый на сегодня случай, не ошибка). Как только
реестр появится на сервере — подхватится сам, без изменений в коде здесь.
"""
from dataclasses import dataclass
from typing import List

import config as _config
from core.depot_client import fetch_builds_registry


@dataclass
class Build:
    name:        str            # техническое имя (= config.BUILD_NAME для неё, папка кэша в APPDATA)
    label:       str            # отображаемое имя на плитке карусели
    remote_path: str            # путь на WebDAV относительно DAV_BASE_URL (= config.DEPOT_REMOTE_PATH для неё)
    version:     str = ""       # опционально — короткая подпись под названием (напр. текущая версия), если реестр её даёт


def _default_build() -> Build:
    """Единственная сборка, зашитая в config.py сейчас — фолбэк, пока
    реестра builds.json на сервере нет (см. модульный докстринг)."""
    return Build(
        name=_config.BUILD_NAME,
        label=_config.BUILD_NAME,
        remote_path=_config.DEPOT_REMOTE_PATH,
    )


def list_builds(log=print) -> List[Build]:
    """
    Возвращает список сборок для карусели. Всегда возвращает хотя бы одну
    (см. _default_build()) — карусель никогда не остаётся пустой экраном,
    даже если реестр на сервере отсутствует или битый.
    """
    registry = fetch_builds_registry(on_log=log)
    if not registry:
        log(f"Реестр сборок (builds.json) не найден на сервере — показываем текущую сборку ({_config.BUILD_NAME})")
        return [_default_build()]

    builds: List[Build] = []
    for entry in registry:
        try:
            name        = entry["name"]
            remote_path = entry["remote_path"]
        except (KeyError, TypeError):
            log(f"⚠️ Пропускаю некорректную запись в реестре сборок (нет name/remote_path): {entry}")
            continue
        builds.append(Build(
            name=name,
            label=entry.get("label", name),
            remote_path=remote_path,
            version=entry.get("version", ""),
        ))

    if not builds:
        log("Реестр сборок пуст или все записи битые — показываем текущую сборку")
        return [_default_build()]

    log(f"Загружено сборок из реестра: {len(builds)}")
    return builds
