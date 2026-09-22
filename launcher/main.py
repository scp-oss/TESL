# ==================== launcher/main.py ====================
import sys
import os

# Добавляем корень launcher/ в sys.path чтобы работали импорты
sys.path.insert(0, os.path.dirname(__file__))

from PyQt6.QtWidgets import QApplication, QInputDialog, QLineEdit
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

import config
from config import get_asset_path
from ui.main_window import UpdaterUI


def _ensure_dav_password():
    """
    Если пароль WebDAV нигде не нашёлся (ни ENV, ни secrets_local.py, ни
    кэш-файл в APPDATA — см. config.py "WebDAV"), спрашиваем один раз через
    диалог и сохраняем в APPDATA, чтобы больше не спрашивать. Не более
    "секретно", чем то, что этот же пароль в любом случае зашит в каждый
    собранный .exe (см. CLAUDE.md "Секреты и их модель угроз") — поэтому
    хранение обычным текстом в APPDATA не добавляет нового риска.

    Пишем `config.DAV_PASSWORD` напрямую (не просто локальную переменную) —
    все места, которые его читают, делают это ЖИВЬЁМ через `import config`
    (см. depot_client.py/patcher.py/crash_logger.py), а не через
    `from config import DAV_PASSWORD`, специально чтобы это сработало без
    перезапуска процесса.
    """
    if config.DAV_PASSWORD:
        return
    password, ok = QInputDialog.getText(
        None, "WebDAV пароль",
        "Пароль WebDAV-аккаунта не найден.\nВведи его один раз — дальше сохранится:",
        QLineEdit.EchoMode.Password,
    )
    if not ok or not password:
        return
    config.DAV_PASSWORD = password
    try:
        config.DAV_PASSWORD_CACHE_FILE.write_text(password, encoding="utf-8")
    except Exception:
        pass  # не критично — просто спросит ещё раз в следующий запуск


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("TESVAE Launcher")
    app.setStyle("Fusion")

    icon_path = get_asset_path("icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    _ensure_dav_password()

    window = UpdaterUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
