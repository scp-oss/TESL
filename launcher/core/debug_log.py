# ==================== launcher/core/debug_log.py ====================
"""
Отправка LOG_FILE на сервер, когда включён "Режим отладки" (настройки
лаунчера -> чекбокс, см. ui/settings_dialog.py) — прямой запрос
пользователя 2026-09-22: "если его включить будет лог от ланчере о
установке весь лог консоли отправляться на сервер для нашего анализа".

Переиспользует уже проверенный механизм крэш-репортов
(core/crash_logger.py::upload_files()) — та же структура
`<username>/<timestamp>/`, только `report_type="debug_log"` вместо
`"crash"`. **2026-09-29: переехало вместе с крэш-репортами на
TESL-Panel** — см. crash_logger.py's докстринг за живой повод
(WebDAV `MKCOL DEBUG_Log/ → 403`, серверные права, не чинится тут).
"""
import threading
from typing import Callable

import config as _config


def _do_upload(username: str, reason: str, log: Callable[[str], None]):
    from core.crash_logger import upload_files
    try:
        log(f"🐞 Отправляем лог отладки на сервер (причина: {reason})...")
        ok, msg = upload_files(
            username, [_config.LOG_FILE],
            log=log,
            report_type="debug_log",
        )
        log(f"🐞 {'Лог отладки отправлен' if ok else 'Не удалось отправить лог отладки'}: {msg}")
    except Exception as e:
        log(f"🐞 Ошибка отправки лога отладки: {e}")


def maybe_upload_debug_log(username: str, reason: str, log: Callable[[str], None] = print):
    """
    Запускает отправку в фоновом daemon-потоке (НЕ QThread) — вызывается
    в том числе из closeEvent() главного окна, где приложение уже
    закрывается: обычный QThread пережил бы владельца-виджет и рисковал
    бы повиснуть/не быть корректно остановленным при выходе, а
    daemon-поток убивается вместе с процессом без дополнительной уборки —
    отправка best-effort, потеря последнего лога при закрытии не критична.
    Не бросает исключения наружу — сбой отправки лога отладки никогда не
    должен ронять сам лаунчер.
    """
    t = threading.Thread(
        target=_do_upload, args=(username, reason, log),
        daemon=True,
    )
    t.start()
