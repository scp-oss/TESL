# ==================== launcher/ui/carousel_window.py ====================
"""
Стартовый экран — карусель доступных сборок (прямой запрос пользователя
2026-09-22, по образцу стороннего лаунчера: экран со сборками, клик по
плитке -> открывается конкретная сборка). Показывается ПЕРЕД главным окном
(ui/main_window.py::UpdaterUI) — main.py сначала открывает это окно, и
только после выбора плитки открывает UpdaterUI для выбранной сборки.

**2026-09-28: список сборок — с TESL-Panel** (GET /api/builds, см.
core/builds.py/core/panel_client.py), не WebDAV builds.json — карусель
показывает РЕАЛЬНО существующие на панели сборки, ни одной "зашитой"
по умолчанию не осталось; при пустом ответе панели показывает
понятный статус вместо пустого экрана без объяснений (см.
_on_builds_loaded()).

**Безрамочная "стеклянная" оболочка (см. ui/theme.py) — прямой запрос
пользователя** после того, как реальный скрин показал обычное системное
окно и плоские серые плитки, совсем не похожие на дизайн-макет: не
проблема Python/Qt (там же спрашивалось, не взять ли другой язык под
это) — просто до этого коммита из макета в код была перенесена только
логика (автопостер/оверлей прогресса), не "оболочка" (безрамочное окно,
кастомный тайтлбар со свернуть/закрыть, скруглённые плитки, скроллбар).
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
from ui.theme import (
    make_frameless, wrap_in_card, TitleBar, SCROLLBAR_QSS,
    TEXT, TEXT_2, TEXT_3, HAIRLINE,
)


TILE_W, TILE_H = 220, 300
POSTER_W, POSTER_H = 200, 260


class BuildTile(QFrame):
    """Одна плитка карусели — постер + название, кликабельная."""

    def __init__(self, build, on_click, parent=None):
        super().__init__(parent)
        self.build = build
        self._on_click = on_click
        self.setFixedSize(TILE_W, TILE_H)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(f"""
            BuildTile {{
                background: qlineargradient(x1:0, y1:0, x2:0.4, y2:1, stop:0 #26262b, stop:1 #1a1a1e);
                border: 1px solid {HAIRLINE}; border-radius: 18px;
            }}
            BuildTile:hover {{ border-color: rgba(47, 143, 224, 140); }}
        """)

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        self.poster = QLabel()
        self.poster.setFixedSize(POSTER_W, POSTER_H)
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setStyleSheet(f"background: #1a1a1a; border-radius: 14px; color: {TEXT_3};")
        # Автогенерированный постер сразу, а не текст-заглушка "..." — эта
        # же картинка и останется, если BuildsLoaderWorker для этой сборки
        # реального постера так и не пришлёт (poster_loaded просто не
        # эмитится, см. её докстринг) — то есть "если постер не задан",
        # прямой запрос пользователя. set_poster() ниже перекрывает её,
        # если/когда реальный постер всё же загрузится.
        from core.poster_fallback import render_fallback_poster
        self.poster.setPixmap(render_fallback_poster(build.label or build.name, POSTER_W, POSTER_H))
        v.addWidget(self.poster, alignment=Qt.AlignmentFlag.AlignCenter)

        name = QLabel(build.label)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        name.setStyleSheet(f"color: {TEXT}; background: transparent; border: none;")
        name.setWordWrap(True)
        v.addWidget(name)

        # Оверлей прогресса загрузки — поднимается снизу поверх постера,
        # тот же приём, что у плиток Microsoft Store при установке
        # приложения ("мини прогресбар цветом, винда так умеет", прямой
        # запрос пользователя). Дети self.poster (не самой плитки) —
        # позиционируются вручную через setGeometry() внутри его
        # координат, а не через layout. Скрыт по умолчанию (set_progress
        # ещё не вызывался ни разу — см. её докстринг за то, что сейчас
        # реально driving-сигнала для этого нет).
        self._progress_overlay = QFrame(self.poster)
        self._progress_overlay.setStyleSheet(
            "background: rgba(47, 143, 224, 150); border: none; border-radius: 0;"
        )
        self._progress_overlay.setGeometry(0, POSTER_H, POSTER_W, 0)
        self._progress_overlay.hide()

        self._progress_label = QLabel(self.poster)
        self._progress_label.setStyleSheet(
            "color: white; font-weight: 600; font-size: 11px; background: transparent; border: none;"
        )
        self._progress_label.setFixedSize(60, 18)
        self._progress_label.hide()

    def set_progress(self, percent):
        """percent: int (0-100) — доля загрузки, показать/обновить оверлей;
        None — скрыть (сборка сейчас не скачивается).

        ПРИМЕЧАНИЕ ДЛЯ СЛЕДУЮЩЕГО, КТО ЭТО ПОДКЛЮЧАЕТ: сейчас этот метод
        никто не вызывает. В текущей архитектуре карусель ЗАКРЫВАЕТСЯ в
        момент клика по плитке (main.py::_open_main_window — открывает
        главное окно и сразу же `_carousel_window.close()`), а сама
        загрузка идёт уже внутри него (core/workers.py::DownloadWorker) —
        то есть плитка, для которой был бы виден этот оверлей, к началу
        реальной загрузки уже не существует. Подключить его к живому
        прогрессу значит держать карусель открытой (или воссоздавать её
        состояние) во время установки — решение об архитектуре окон, не
        только визуальный перенос макета, и здесь оно не принято
        самостоятельно. Метод оставлен готовым и статически проверенным."""
        if percent is None:
            self._progress_overlay.hide()
            self._progress_label.hide()
            return
        percent = max(0, min(100, int(percent)))
        h = round(POSTER_H * percent / 100)
        self._progress_overlay.setGeometry(0, POSTER_H - h, POSTER_W, h)
        self._progress_overlay.show()
        self._progress_overlay.raise_()
        self._progress_label.setText(f"{percent}%")
        self._progress_label.move(8, POSTER_H - 22)
        self._progress_label.show()
        self._progress_label.raise_()

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
        # Тот же формат заголовка, что и у главного окна (см.
        # config.get_window_title(), прямой запрос пользователя
        # 2026-09-22, "TESL [commit vers]") — эта карусель тоже реальное
        # окно приложения (первое, что видит пользователь при запуске),
        # не отдельный from-scratch бренд.
        self.setWindowTitle(f"{_config.get_window_title()} — выбор сборки")
        icon_path = get_asset_path("icon.ico")
        from PyQt6.QtGui import QIcon
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(768, 468)
        self.setMinimumSize(528, 408)

        # Безрамочное окно + одна "карточка"-подложка (реальный фон/
        # скругление/тень) внутри — см. ui/theme.py за то, почему НЕ
        # border-radius прямо на самом top-level окне.
        make_frameless(self)
        card = wrap_in_card(self, radius=22)

        self._tiles: dict = {}   # build.name -> BuildTile
        self.builds_worker = None   # хранится на self — та же ловушка PyQt6,
        self.builds_thread = None   # что уже чинилась у PosterLoader/CrashLogSender
        # (см. ui/main_window.py::_start_poster_loader — локальная переменная
        # без живой Python-ссылки рискует быть собрана сборщиком мусора до
        # того, как поток успеет вызвать run()).

        card_v = QVBoxLayout(card)
        card_v.setContentsMargins(0, 0, 0, 0)
        card_v.setSpacing(0)

        title_bar = TitleBar(subtitle="Выберите сборку, чтобы продолжить")
        card_v.addWidget(title_bar)

        root = QVBoxLayout()
        root.setContentsMargins(24, 16, 24, 20)
        root.setSpacing(12)
        card_v.addLayout(root)

        self.status_label = QLabel("Загрузка списка сборок...")
        self.status_label.setStyleSheet(f"color: {TEXT_2};")
        root.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }" + SCROLLBAR_QSS)

        self.strip = QWidget()
        self.strip.setStyleSheet("background: transparent;")
        self.strip_layout = QHBoxLayout(self.strip)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setSpacing(16)
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
            self.status_label.setText(
                "Сборок пока нет, либо не удалось связаться с панелью — "
                "проверьте соединение и повторите запуск"
            )
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
