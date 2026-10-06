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
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QPushButton, QLabel,
    QCheckBox, QFrame,
)


class SettingsDialog(QDialog):

    def __init__(
        self,
        parent,
        btn_verify: QPushButton,
        btn_launch_mo2: QPushButton,
        btn_move_install: QPushButton,
        debug_mode_getter,
        debug_mode_setter,
    ):
        super().__init__(parent)
        self.setWindowTitle("Меню сборки / Настройки")
        self.resize(360, 300)

        v = QVBoxLayout(self)
        v.setSpacing(10)

        v.addWidget(QLabel("Сборка:"))
        for b in (btn_launch_mo2, btn_verify, btn_move_install):
            b.setFixedHeight(36)
            v.addWidget(b)

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
