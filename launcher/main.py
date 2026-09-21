# ==================== launcher/main.py ====================
import sys
import os

# Добавляем корень launcher/ в sys.path чтобы работали импорты
sys.path.insert(0, os.path.dirname(__file__))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

from config import get_asset_path
from ui.main_window import UpdaterUI


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("TESVAE Launcher")
    app.setStyle("Fusion")

    icon_path = get_asset_path("icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = UpdaterUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
