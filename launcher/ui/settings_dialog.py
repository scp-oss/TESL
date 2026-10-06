# ==================== launcher/ui/settings_dialog.py ====================
"""
Диалог настроек — он же "меню сборки", прямой запрос пользователя
2026-10-06 ("меню сборки и настройки это должно быть одно и то же в
одном месте"): попытка развести эти две кнопки по разным местам в тот
же день была откачена — единственная точка входа снова ⚙
(ui/main_window.py::_open_settings()).

Состав, тем же запросом, пересобран с нуля: "🔨 Пропатчить Skyrim"/
"♻️ Очистить Skyrim"/"🔄 Перезапустить Explorer" убраны — больше не
нужны (их методы в main_window.py — `_revert_skyrim`/`_restart_explorer`
— удалены целиком; `_patch_skyrim()` остался, но вызывается только из
FirstRunDialog, не из этого диалога). Вместо них — "▶ Запустить Mod
Organizer 2" (прямой запуск игры без прохождения через умную кнопку
статуса в центре окна), "🛠 Проверить файлы" (VerifyWorker, полный
рескан+сравнение с сервером, функционал не менялся ни на строчку за
всю историю этого файла — только его UI-расположение) и "📂 Перенести
в другое место" (MoveInstallWorker — переносит уже установленную
сборку на диске, не переустанавливая её заново). Плюс переключатель
"Режим отладки" (см. core/debug_log.py, ui/main_window.py::
_set_debug_mode()) — единственный пункт, не являющийся кнопкой-
действием, поэтому остаётся отдельным чекбоксом под разделителем.

Кнопки передаются УЖЕ СОЗДАННЫМИ из main_window.py, не создаются
здесь — это те же самые self.btn_verify/self.btn_launch_mo2/
self.btn_move_install, на которые ссылается main_window.py::ALL_BTNS/
_disable_buttons() для блокировки во время параллельных операций
(install/verify/patch/перенос не должны идти одновременно). Переезд в
этот диалог меняет только их визуального родителя — Qt переставляет
parent автоматически при addWidget() в layout нового виджета, сама
Python-ссылка и вся логика enable/disable в main_window.py остаётся
рабочей без изменений.

**2026-10-06, та же "стеклянная" оболочка, что и у карусели/главного
окна** (прямой запрос "приведи интерфейс к виду как на макете") —
безрамочное окно + карточка + кастомный тайтлбар вместо системного
диалогового окна. Крестик в TitleBar закрывает модальный `exec()` так
же, как раньше это делала отдельная кнопка "Закрыть" (closeEvent
QDialog по умолчанию вызывает reject(), который этот цикл и завершает)
— отдельная кнопка "Закрыть" внизу поэтому убрана как дублирующая.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QPushButton, QLabel, QCheckBox, QFrame,
)

from ui.theme import make_frameless, wrap_in_card, TitleBar, qrgba, ACCENT


class SettingsDialog(QDialog):

    def __init__(
        self,
        parent,
        btn_verify: QPushButton,
        btn_launch_mo2: QPushButton,
        btn_move_install: QPushButton,
        btn_create_shortcut: QPushButton,
        debug_mode_getter,
        debug_mode_setter,
    ):
        super().__init__(parent)
        self.resize(420, 400)

        make_frameless(self)
        card = wrap_in_card(self, radius=22)
        card_v = QVBoxLayout(card)
        card_v.setContentsMargins(0, 0, 0, 0)
        card_v.setSpacing(0)

        title_bar = TitleBar(subtitle="Меню сборки / Настройки")
        card_v.addWidget(title_bar)

        body = QVBoxLayout()
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(10)
        card_v.addLayout(body)

        lbl_section = QLabel("Сборка")
        lbl_section.setObjectName("TeslFieldLabel")
        body.addWidget(lbl_section)

        for b in (btn_launch_mo2, btn_verify, btn_move_install, btn_create_shortcut):
            b.setObjectName("TeslGhostBtn")
            b.setFixedHeight(38)
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            body.addWidget(b)

        sep = QFrame()
        sep.setObjectName("TeslHairline")
        sep.setFixedHeight(1)
        body.addSpacing(4)
        body.addWidget(sep)
        body.addSpacing(4)

        self.chk_debug = QCheckBox("Режим отладки (отправлять лог на сервер)")
        self.chk_debug.setObjectName("TeslCheck")
        self.chk_debug.setChecked(debug_mode_getter())
        self.chk_debug.toggled.connect(debug_mode_setter)
        body.addWidget(self.chk_debug)

        hint = QLabel(
            "Если включено — полный лог установки/консоли лаунчера "
            "отправляется на сервер (после установки/проверки/патчинга и "
            "при закрытии окна) для анализа."
        )
        hint.setObjectName("TeslHint")
        hint.setWordWrap(True)
        body.addWidget(hint)

        body.addStretch()

        self.setStyleSheet(f"""
            QLabel {{ color: #F3F3F5; background: transparent; }}
            QLabel#TeslFieldLabel {{ color: #A7A7AE; font-size: 9pt; font-weight: 500; }}
            QLabel#TeslHint {{ color: #74747B; font-size: 8pt; }}
            QFrame#TeslHairline {{ background: {qrgba(255,255,255,0.09)}; border: none; }}

            QPushButton#TeslGhostBtn {{
                background: {qrgba(255,255,255,0.055)}; border: 1px solid {qrgba(255,255,255,0.14)};
                border-radius: 19px; color: #F3F3F5; font-size: 10pt; text-align: center;
            }}
            QPushButton#TeslGhostBtn:hover {{ background: {qrgba(255,255,255,0.10)}; border-color: {ACCENT}; }}
            QPushButton#TeslGhostBtn:disabled {{ color: #74747B; border-color: {qrgba(255,255,255,0.09)}; }}

            QCheckBox#TeslCheck {{ color: #F3F3F5; font-size: 9.5pt; spacing: 8px; }}
            QCheckBox#TeslCheck::indicator {{
                width: 16px; height: 16px; border-radius: 5px;
                border: 1px solid {qrgba(255,255,255,0.25)}; background: {qrgba(255,255,255,0.06)};
            }}
            QCheckBox#TeslCheck::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
        """)
