# ==================== launcher/main.py ====================
import sys
import os

# Добавляем корень launcher/ в sys.path чтобы работали импорты
sys.path.insert(0, os.path.dirname(__file__))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from ui.main_window import UpdaterUI


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("TESVAE Launcher")
    app.setStyle("Fusion")

    window = UpdaterUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
