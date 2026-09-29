# ==================== launcher/ui/theme.py ====================
"""
Общая "оболочка" окна для всех трёх окон лаунчера (карусель/главное/
настройки) — прямой запрос пользователя: скрин реального приложения
показал обычный системный тайтлбар и плоские серые плитки, совсем не
похожие на дизайн-макет (Design-артефакт canvas, см. историю чата) —
до этого коммита из макета в реальный код была перенесена только
логика (автопостер, оверлей прогресса), сама "оболочка" (безрамочное
скруглённое окно, кастомный тайтлбар с бейджем/крестиком/минимизацией,
тёмная палитра, скроллбар) не трогалась вообще.

**На вопрос "может, проблема в Python — тогда другой язык?" — нет.**
Всё из макета (безрамочное окно, скруглённые углы, кастомные
свернуть/закрыть, тёмная полу-прозрачная палитра, стилизованный
скроллбар) Qt умеет нативно, тем же PyQt6, никакой другой язык/
фреймворк не нужен. Единственное, что Qt НЕ умеет кроссплатформенно и
"из коробки" — настоящий backdrop-blur (macOS Liquid Glass / Windows
Acrylic буквально размывающий то, что ПОД окном) — это платформенное
API (DWM composition на Windows), отдельная, более рискованная задача;
здесь и далее "стекло" — плоская полупрозрачная заливка + мягкая тень,
тот же практический компромисс, что макет уже принял для звания
"liquid glass" в CSS (см. Main.dc.html/Carousel.dc.html: тоже не
настоящий blur, а approximation).

Работает через готовые, проверенные offscreen-рендером (QT_QPA_PLATFORM=
offscreen, .grab().save(...) — та же методика, что уже применялась в
этой сессии для core/poster_fallback.py) кирпичи:

  - COLORS / qrgba() — палитра 1:1 из макета (--bg/--text/--accent/...).
  - card_qss(radius) — стиль "карточки"-подложки окна (сплошной фон +
    hairline-рамка + скругление) — сам скруглённый вид даёт QSS
    border-radius на QFrame со СПЛОШНЫМ фоном (это Qt рисует
    корректно на всех платформах, в отличие от border-radius на
    самом безрамочном top-level окне, который не обрезает
    содержимое одинаково всюду — поэтому оболочка = прозрачное
    безрамочное окно + одна дочерняя QFrame-"карточка" с реальным
    фоном/скруглением/тенью внутри).
  - make_frameless(window) — оба флага (FramelessWindowHint +
    WA_TranslucentBackground), больше ничего не меняет.
  - wrap_in_card(window, radius) -> QFrame — создаёт и уже
    подключает такую карточку с DropShadow-эффектом (аппроксимация
    box-shadow из макета) как единственного child-виджета window;
    возвращает саму карточку — в неё дальше кладётся обычный
    QVBoxLayout с тайтлбаром и содержимым окна.
  - TitleBar — тайтлбар: сквиркл-бейдж "T", вордмарк + подпись,
    опциональная кнопка "назад" слева, произвольные дополнительные
    виджеты окна (extra_layout) и обязательные свернуть/закрыть
    справа. Сам таскает окно за тайтлбар (frameless теряет системный
    drag) — тот же общеизвестный рецепт (mousePress/Move/Release +
    window().move()), что применяется для любого кастомного
    тайтлбара в Qt.
  - SCROLLBAR_QSS — тонкий полупрозрачный скруглённый скроллбар
    ("liquid glass" скроллбар из макета) через штатные псевдоселекторы
    QScrollBar Qt — тоже не требует ничего вне Qt Style Sheets.
"""
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor, QCursor
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QGraphicsDropShadowEffect, QSizePolicy,
)


# ── Палитра — 1:1 из макета (Main.dc.html/Carousel.dc.html, --bg/--text/...) ──
BG          = "#111114"
BG_SOFT     = "#17171B"
TEXT        = "#F3F3F5"
TEXT_2      = "#A7A7AE"
TEXT_3      = "#74747B"
ACCENT      = "#2F8FE0"
ACCENT_TEXT = "#FFFFFF"


def qrgba(r: int, g: int, b: int, alpha_float: float) -> str:
    """Qt Style Sheets: rgba() берёт alpha 0-255, НЕ 0-1 как в CSS —
    отдельная функция, чтобы не пересчитывать вручную в каждом месте
    и не унести с собой опечатку (255*0.09 не в уме считается точно)."""
    return f"rgba({r}, {g}, {b}, {round(alpha_float * 255)})"


HAIRLINE           = qrgba(255, 255, 255, 0.09)
HAIRLINE_STRONG    = qrgba(255, 255, 255, 0.16)
GLASS_FILL         = qrgba(255, 255, 255, 0.055)
ICON_HOVER         = qrgba(255, 255, 255, 0.10)
CLOSE_HOVER_BG     = qrgba(232, 71, 71, 0.22)
CLOSE_HOVER_FG     = "#FF8A8A"


def card_qss(radius: int = 20) -> str:
    return f"""
        QFrame#TeslCard {{
            background: {BG};
            border: 1px solid {HAIRLINE};
            border-radius: {radius}px;
        }}
    """


# Тонкий полупрозрачный скруглённый скроллбар — тот же вид, что и
# "liquid glass" скроллбар в макете (см. Main.dc.html `::-webkit-scrollbar`).
SCROLLBAR_QSS = f"""
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 0; }}
    QScrollBar::handle:vertical {{
        background: {qrgba(255, 255, 255, 0.16)}; border-radius: 5px; min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {qrgba(255, 255, 255, 0.32)}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; border: none; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 2px; }}
    QScrollBar::handle:horizontal {{
        background: {qrgba(255, 255, 255, 0.16)}; border-radius: 5px; min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {qrgba(255, 255, 255, 0.32)}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; border: none; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
"""


def circle_btn_qss(hover_bg: str = ICON_HOVER, hover_fg: str = TEXT) -> str:
    """Круглая icon-button 30x30 — свернуть/закрыть/назад в TitleBar и
    любая другая круглая кнопка-иконка окна (тот же .icon-btn из макета)."""
    return f"""
        QPushButton {{
            background: transparent; border: none; border-radius: 15px;
            color: {TEXT_2}; font-size: 14pt; font-weight: 600;
        }}
        QPushButton:hover {{ background: {hover_bg}; color: {hover_fg}; }}
        QPushButton:pressed {{ background: {qrgba(255, 255, 255, 0.16)}; }}
    """


def make_frameless(window: QWidget) -> None:
    """Безрамочное + прозрачный фон окна — сама видимая "рамка" рисуется
    карточкой внутри (см. wrap_in_card), не системным decorations."""
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def wrap_in_card(window: QWidget, radius: int = 20, shadow_margin: int = 24) -> QFrame:
    """Кладёт в window ОДНУ дочернюю QFrame-"карточку" (реальный фон +
    скругление + мягкая тень, аппроксимация box-shadow из макета) с
    отступом shadow_margin со всех сторон — под тень нужно место, иначе
    DropShadowEffect обрежется границей самого (прозрачного) окна.
    Возвращает карточку — в неё кладётся обычный QVBoxLayout(card) с
    TitleBar() первым виджетом и содержимым окна дальше."""
    outer = QVBoxLayout(window)
    outer.setContentsMargins(shadow_margin, shadow_margin, shadow_margin, shadow_margin)

    card = QFrame(window)
    card.setObjectName("TeslCard")
    card.setStyleSheet(card_qss(radius))

    shadow = QGraphicsDropShadowEffect(card)
    shadow.setBlurRadius(56)
    shadow.setOffset(0, 18)
    shadow.setColor(QColor(0, 0, 0, 150))
    card.setGraphicsEffect(shadow)

    outer.addWidget(card)
    return card


class TitleBar(QWidget):
    """Кастомный тайтлбар — сквиркл-бейдж "T" + вордмарк "TESL" + подпись,
    опциональная кнопка "назад" (стрелка, слева от бейджа), место для
    доп. виджетов конкретного окна (self.extra_layout — например,
    папка/ярлык/настройки/тема в главном окне) и свернуть/закрыть
    (всегда справа, крайние). Сам таскает окно (frameless теряет
    системный drag системной рамки)."""

    HEIGHT = 56

    def __init__(self, subtitle: str = "", show_back: bool = False, on_back=None, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setStyleSheet(
            f"background: {GLASS_FILL}; border-top-left-radius: 20px; "
            f"border-top-right-radius: 20px; border-bottom: 1px solid {HAIRLINE};"
        )
        self._drag_pos = None

        h = QHBoxLayout(self)
        h.setContentsMargins(16, 0, 12, 0)
        h.setSpacing(10)

        if show_back:
            back_btn = QPushButton("‹")
            back_btn.setFixedSize(30, 30)
            back_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            back_btn.setStyleSheet(circle_btn_qss())
            if on_back:
                back_btn.clicked.connect(on_back)
            h.addWidget(back_btn)

        badge = QLabel("T")
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background: {ACCENT}; color: {ACCENT_TEXT}; font-weight: 700; "
            f"font-size: 12pt; border-radius: 8px;"
        )
        h.addWidget(badge)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title_lbl = QLabel("TESL")
        title_lbl.setStyleSheet(f"color: {TEXT}; font-weight: 700; font-size: 12pt; background: transparent;")
        text_col.addWidget(title_lbl)
        if subtitle:
            sub_lbl = QLabel(subtitle)
            sub_lbl.setStyleSheet(f"color: {TEXT_2}; font-size: 9pt; background: transparent;")
            text_col.addWidget(sub_lbl)
        h.addLayout(text_col)

        h.addStretch(1)

        # Доп. виджеты конкретного окна (папка/ярлык/⚙/🌙 в главном
        # окне) — добавляются вызывающим через self.extra_layout,
        # ДО минимизировать/закрыть, которые всегда последние.
        self.extra_layout = QHBoxLayout()
        self.extra_layout.setSpacing(10)
        h.addLayout(self.extra_layout)

        min_btn = QPushButton("–")
        min_btn.setFixedSize(30, 30)
        min_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        min_btn.setToolTip("Свернуть лаунчер")
        min_btn.setStyleSheet(circle_btn_qss())
        min_btn.clicked.connect(self._minimize)
        h.addWidget(min_btn)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(30, 30)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.setToolTip("Закрыть лаунчер")
        close_btn.setStyleSheet(circle_btn_qss(hover_bg=CLOSE_HOVER_BG, hover_fg=CLOSE_HOVER_FG))
        close_btn.clicked.connect(self._close)
        h.addWidget(close_btn)

    def _minimize(self):
        self.window().showMinimized()

    def _close(self):
        self.window().close()

    # ── Перетаскивание окна за тайтлбар ──────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)
