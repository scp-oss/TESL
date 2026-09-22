# ==================== launcher/core/debug_log.py ====================
"""
Отправка LOG_FILE на сервер, когда включён "Режим отладки" (настройки
лаунчера -> чекбокс, см. ui/settings_dialog.py) — прямой запрос
пользователя 2026-09-22: "если его включить будет лог от ланчере о
установке весь лог консоли отправляться на сервер для нашего анализа".

Переиспользует УЖЕ проверенный WebDAV-механизм крэш-репортов
(core/crash_logger.py::upload_files()) вместо новой логики — та же
структура <remote_path>/<username>/<timestamp>/, только путь другой
(config.DEBUG_LOG_REMOTE_PATH, НЕ ПОДТВЕРЖДЁН реальным листингом — см. её
докстринг в config.py, тот же класс "нужно сверить, когда появится первая
реальная отправка", что раньше был у CRASH_LOG_REMOTE_PATH/DEPOT_REMOTE_PATH
до их подтверждения).
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
            remote_path=_config.DEBUG_LOG_REMOTE_PATH,
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
