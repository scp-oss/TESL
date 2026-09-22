# ==================== launcher/ui/settings_dialog.py ====================
"""
Диалог настроек — прямой запрос пользователя 2026-09-22 ("на последнем
скрине раздел настроек давай туда уберем все тулсы не тулсы плюс режим
дебаг"): инструментальные кнопки (патч/проверка файлов/очистка Skyrim,
перезапуск Explorer), раньше жившие отдельной колонкой слева в главном
окне, перенесены сюда — плюс новый переключатель "Режим отладки" (см.
core/debug_log.py, ui/main_window.py::_set_debug_mode()).

Кнопки patch/verify/revert передаются УЖЕ СОЗДАННЫМИ из main_window.py, не
создаются здесь — это те же самые self.btn_patch/self.btn_verify/
self.btn_revert, на которые ссылается main_window.py::ALL_BTNS/
_disable_buttons() для блокировки во время параллельных операций (install/
verify/patch не должны идти одновременно). Переезд в этот диалог меняет
только их визуального родителя — Qt переставляет parent автоматически при
addWidget() в layout нового виджета, сама Python-ссылка и вся логика
enable/disable в main_window.py остаётся рабочей без изменений.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QCheckBox, QFrame,
)


class SettingsDialog(QDialog):

    def __init__(
        self,
        parent,
        btn_patch: QPushButton,
        btn_verify: QPushButton,
        btn_revert: QPushButton,
        restart_explorer_fn,
        debug_mode_getter,
        debug_mode_setter,
    ):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.resize(360, 340)

        v = QVBoxLayout(self)
        v.setSpacing(10)

        v.addWidget(QLabel("Инструменты:"))
        for b in (btn_patch, btn_verify, btn_revert):
            b.setFixedHeight(36)
            v.addWidget(b)

        self.btn_explorer = QPushButton("🔄 Перезапустить Explorer")
        self.btn_explorer.setFixedHeight(36)
        self.btn_explorer.clicked.connect(restart_explorer_fn)
        v.addWidget(self.btn_explorer)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        v.addWidget(sep)

        self.chk_debug = QCheckBox("Режим отладки (отправлять лог на сервер)")
        self.chk_debug.setChecked(debug_mode_getter())
        self.chk_debug.toggled.connect(debug_mode_setter)
        v.addWidget(self.chk_debug)

        hint = QLabel(
            "Если включено — полный лог установки/консоли лаунчера "
            "отправляется на сервер (после установки/проверки/патчинга и "
            "при закрытии окна) для анализа."
        )
        hint.setStyleSheet("color: #888; font-size: 8pt;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        v.addStretch()

        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.accept)
        v.addWidget(btn_close)
