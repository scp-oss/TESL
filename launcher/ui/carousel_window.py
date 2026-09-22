# ==================== launcher/ui/carousel_window.py ====================
"""
Стартовый экран — карусель доступных сборок (прямой запрос пользователя
2026-09-22, по образцу стороннего лаунчера: экран со сборками, клик по
плитке -> открывается конкретная сборка). Показывается ПЕРЕД главным окном
(ui/main_window.py::UpdaterUI) — main.py сначала открывает это окно, и
только после выбора плитки открывает UpdaterUI для выбранной сборки.

Сегодня реально существует одна сборка (TESVAE) — реестр builds.json на
сервере ещё не подтверждён (см. config.py::BUILDS_REGISTRY_PATH,
core/builds.py). Карусель поэтому всегда показывает хотя бы одну плитку и
работает уже сейчас, а когда реестр появится на сервере — подхватит
остальные сборки сама, без изменений здесь.
"""
from PyQt6.QtCore import Qt, QThread, QSize
from PyQt6.QtGui import QCursor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QSizePolicy,
)

import config as _config
from config import get_asset_path
from core.workers import BuildsLoaderWorker


TILE_W, TILE_H = 200, 280
POSTER_W, POSTER_H = 180, 240


class BuildTile(QFrame):
    """Одна плитка карусели — постер + название, кликабельная."""

    def __init__(self, build, on_click, parent=None):
        super().__init__(parent)
        self.build = build
        self._on_click = on_click
        self.setFixedSize(TILE_W, TILE_H)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet("""
            BuildTile { background: #232323; border: 1px solid #3a3a3a; border-radius: 10px; }
            BuildTile:hover { background: #2e2e2e; border-color: #6aa9ff; }
        """)

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        self.poster = QLabel()
        self.poster.setFixedSize(POSTER_W, POSTER_H)
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setStyleSheet("background: #1a1a1a; border-radius: 6px; color: #666;")
        self.poster.setText("...")
        v.addWidget(self.poster, alignment=Qt.AlignmentFlag.AlignCenter)

        name = QLabel(build.label)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        name.setStyleSheet("color: #eee; background: transparent; border: none;")
        name.setWordWrap(True)
        v.addWidget(name)

        if build.version:
            ver = QLabel(build.version)
            ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ver.setStyleSheet("color: #888; background: transparent; border: none; font-size: 9pt;")
            v.addWidget(ver)

    def set_poster(self, data: bytes):
        try:
            px = QPixmap()
            if not px.loadFromData(data):
                return
            scaled = px.scaled(
                POSTER_W, POSTER_H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (scaled.width() - POSTER_W) // 2
            y = (scaled.height() - POSTER_H) // 2
            cropped = scaled.copy(x, y, POSTER_W, POSTER_H)
            self.poster.setPixmap(cropped)
            self.poster.setText("")
        except Exception:
            pass

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click(self.build)
        super().mousePressEvent(event)


class CarouselWindow(QWidget):
    """Стартовый экран выбора сборки. build_selected(build) — сигнал
    отсутствует намеренно: колбэк передаётся напрямую в конструктор
    (main.py создаёт это окно и само решает, что делать при выборе),
    тот же простой паттерн, что и у FirstRunDialog."""

    def __init__(self, on_build_selected):
        super().__init__()
        self._on_build_selected = on_build_selected
        self.setWindowTitle("Выбор сборки")
        icon_path = get_asset_path("icon.ico")
        from PyQt6.QtGui import QIcon
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(720, 420)
        self.setMinimumSize(480, 360)
        self.setStyleSheet("background: #1c1c1c;")

        self._tiles: dict = {}   # build.name -> BuildTile
        self.builds_worker = None   # хранится на self — та же ловушка PyQt6,
        self.builds_thread = None   # что уже чинилась у PosterLoader/CrashLogSender
        # (см. ui/main_window.py::_start_poster_loader — локальная переменная
        # без живой Python-ссылки рискует быть собрана сборщиком мусора до
        # того, как поток успеет вызвать run()).

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("Выберите сборку")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: #eee;")
        root.addWidget(title)

        self.status_label = QLabel("Загрузка списка сборок...")
        self.status_label.setStyleSheet("color: #888;")
        root.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.strip = QWidget()
        self.strip.setStyleSheet("background: transparent;")
        self.strip_layout = QHBoxLayout(self.strip)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setSpacing(14)
        self.strip_layout.addStretch()
        scroll.setWidget(self.strip)
        root.addWidget(scroll, 1)

        self._start_builds_loader()

    def _start_builds_loader(self):
        self.builds_worker = BuildsLoaderWorker()
        thread = QThread()
        self.builds_worker.moveToThread(thread)
        self.builds_worker.log.connect(self._log)
        self.builds_worker.loaded.connect(self._on_builds_loaded)
        self.builds_worker.poster_loaded.connect(self._on_poster_loaded)
        thread.started.connect(self.builds_worker.run)
        thread.finished.connect(thread.deleteLater)
        self.builds_thread = thread
        thread.start()

    def _log(self, msg: str):
        _config.write_log_file(f"[карусель] {msg}")

    def _on_builds_loaded(self, builds: list):
        if not builds:
            self.status_label.setText("Не удалось загрузить список сборок — проверьте соединение")
            return
        self.status_label.setText(f"Доступно сборок: {len(builds)}")

        # Убираем финальный stretch, добавляем плитки, возвращаем stretch —
        # чтобы плитки были прижаты к левому краю при малом количестве.
        self.strip_layout.takeAt(self.strip_layout.count() - 1)
        for b in builds:
            tile = BuildTile(b, self._on_tile_clicked, self.strip)
            self._tiles[b.name] = tile
            self.strip_layout.addWidget(tile)
        self.strip_layout.addStretch()

    def _on_poster_loaded(self, build_name: str, data: bytes):
        tile = self._tiles.get(build_name)
        if tile:
            tile.set_poster(data)

    def _on_tile_clicked(self, build):
        if self.builds_thread and self.builds_thread.isRunning():
            self.builds_worker.stop()
            self.builds_thread.quit()
            self.builds_thread.wait(500)
        self._on_build_selected(build)
