# ==================== launcher/core/builds.py ====================
"""
Build — одна доступная для установки сборка, показывается плиткой в
карусели на старте лаунчера (см. ui/carousel_window.py).

**2026-09-28: источник — TESL-Panel, не WebDAV builds.json.** Прямой
запрос пользователя ("рефакторинг и адаптация лаунчера под
существующую панель"), уточнено выбором — полностью перейти на
TESL-Panel, не держать WebDAV builds.json вторым источником. Реестр
теперь — GET /api/builds (публичный, см. core/panel_client.py), реальный
ключ сборки — build_id (UUID из TESL-Panel::builds_db.py), а не путь на
WebDAV. Нет фолбэка на единственную "зашитую" сборку, как раньше
(_default_build()) — WebDAV убран как источник совсем, если панель
недоступна/пуста, список пуст, это честно отражает реальность (см.
ui/carousel_window.py за то, как это показывается пользователю).
"""
from dataclasses import dataclass
from typing import List

from core import panel_client


@dataclass
class Build:
    build_id: str            # реальный ключ на TESL-Panel (builds_db.py)
    name:     str             # техническое имя (= TESL-Panel's build.name, папка кэша в APPDATA)
    label:    str             # отображаемое имя на плитке карусели (пока = name — панель
                               # не отдаёт отдельного "label" сейчас, в отличие от старого
                               # WebDAV builds.json реестра)


def list_builds(log=print) -> List[Build]:
    """
    Возвращает список сборок для карусели — пустой список (не None),
    если панель недоступна или сборок ещё нет; panel_client.list_builds()
    уже сама трактует сеть/HTTP-ошибки как штатный исход и логирует
    причину через `log`.
    """
    raw = panel_client.list_builds(on_log=log)
    if not raw:
        log("Панель не вернула ни одной сборки (сеть недоступна или сборок пока нет)")
        return []

    builds: List[Build] = []
    for entry in raw:
        try:
            build_id = entry["id"]
            name     = entry["name"]
        except (KeyError, TypeError):
            log(f"⚠️ Пропускаю некорректную запись реестра сборок (нет id/name): {entry}")
            continue
        builds.append(Build(build_id=build_id, name=name, label=name))

    log(f"Загружено сборок с панели: {len(builds)}")
    return builds
