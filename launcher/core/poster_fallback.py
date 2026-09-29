# ==================== launcher/core/poster_fallback.py ====================
"""
Автогенерируемый постер — рисуется на лету, когда у сборки нет своего
image.png на панели (либо он не загрузился по любой причине: 404, сеть,
битые данные — core/panel_client.py::fetch_poster() уже сворачивает всё
это в один Optional[bytes], см. её же докстринг за то, почему для
постера это осознанно не разделяется на классы ошибок, в отличие от
чанков). Прямой запрос пользователя: "автогенерация постеров сборок
если постер не задан".

Алгоритм 1:1 повторяет то, что было опробовано на дизайн-макете
(Carousel.dc.html::Component.hashHue/initials) — детерминированный хеш
имени сборки -> оттенок градиента, плюс инициалы названия крупным
шрифтом по центру. Детерминированность важна: одна и та же сборка
всегда получает один и тот же цвет между запусками лаунчера, а не
случайный при каждом показе.

Специально НЕ Qt-виджет и не хранит состояние — чистая функция
QPixmap, чтобы её мог использовать и PosterWidget (главное окно,
300x420), и BuildTile (карусель, 180x240) без дублирования логики.
"""
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPixmap, QPainter, QLinearGradient, QColor, QFont, QPainterPath


def _hash_hue(name: str) -> int:
    """Тот же алгоритм, что и в макете: h = h*31 + code(ch), mod 360."""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) % 360
    return h


def _initials(name: str) -> str:
    """Первые буквы первых двух "слов" имени сборки (разделители —
    пробел, дефис, подчёркивание), в верхнем регистре. Например
    "TESVAE-2" -> "T2", "My Cool Build" -> "MC"."""
    import re
    words = [w for w in re.split(r"[\s_-]+", name) if w]
    return "".join(w[0].upper() for w in words[:2]) or "?"


def render_fallback_poster(name: str, width: int, height: int) -> QPixmap:
    """Возвращает готовый QPixmap width x height: двухцветный диагональный
    градиент (оттенок из хеша имени сборки) + крупные инициалы по центру."""
    hue = _hash_hue(name)
    hue2 = (hue + 45) % 360

    # Прозрачная подложка + скруглённый клип — в отличие от реального
    # постера (который просто setPixmap()'ится в квадратный QLabel и не
    # скругляется контейнером, см. main_window.py::PosterWidget/
    # carousel_window.py::BuildTile), тут можно закруглить сам растр —
    # приближение к "squircle" из макета (радиус ~8% меньшей стороны).
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        radius = min(width, height) * 0.08
        clip_path = QPainterPath()
        clip_path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
        painter.setClipPath(clip_path)

        gradient = QLinearGradient(0, 0, width * 0.3, height)
        gradient.setColorAt(0.0, QColor.fromHsl(hue, int(0.55 * 255), int(0.40 * 255)))
        gradient.setColorAt(1.0, QColor.fromHsl(hue2, int(0.50 * 255), int(0.18 * 255)))
        painter.fillRect(0, 0, width, height, gradient)

        painter.setPen(QColor(255, 255, 255, 210))
        font = QFont("Segoe UI", int(height * 0.16), QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(
            QRectF(0, 0, width, height),
            Qt.AlignmentFlag.AlignCenter,
            _initials(name),
        )
    finally:
        painter.end()

    return pixmap
