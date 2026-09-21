# ==================== launcher/ui/first_run_dialog.py ====================
"""
Диалог первого запуска — определяет что делать со Skyrim.

Раздача "нашей версии" (патченой/даунгрейженой) — ТОЛЬКО для подтверждённой
Steam-лицензии (SkyrimChecker.is_licensed, см. core/skyrim_checker.py). Без
подтверждённой лицензии предлагается только Steam — никакой раздачи без
проверки владения (см. CLAUDE.md, "Легитимность — не факультативно").

Дерево решений:
  Skyrim найден, БЕЗ лицензии (нет steam_api64.dll / Steam не найден)
    → только "Купить/установить в Steam"

  Skyrim AE найден + лицензия + версия OK + DLC OK
    → "Пропатчить" или "Скачать нашу версию поверх"

  Skyrim найден + лицензия, версия НЕ та или DLC неполные
    → "Скачать нашу версию" + "через Steam"

  Skyrim не найден:
    → только "Купить/установить в Steam" (владение до установки не проверить —
      значит и раздавать нечего)
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QRadioButton, QButtonGroup, QTextEdit, QProgressBar,
    QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from core.workers import SkyrimCheckWorker
from core.skyrim_checker import SkyrimCheckResult


STEAM_STORE_URL = "https://store.steampowered.com/app/489830/The_Elder_Scrolls_V_Skyrim_Special_Edition/"


class FirstRunDialog(QDialog):
    """
    Показывается при первом запуске (или если папка игры не задана).
    Эмитит решение пользователя через сигналы.
    """

    # Варианты решения
    ACTION_PATCH_EXISTING   = "patch"       # патчим найденную копию
    ACTION_DOWNLOAD_OURS    = "download"    # скачиваем нашу версию (только при is_licensed)
    ACTION_STEAM            = "steam"       # открываем Steam store
    ACTION_CANCEL           = "cancel"

    action_selected = pyqtSignal(str, str)  # action, skyrim_dir (если найден)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Первый запуск — настройка Skyrim")
        self.setFixedWidth(540)
        self.setModal(True)

        self._check_result: SkyrimCheckResult = None
        self._worker  = None
        self._thread  = None

        self._init_ui()
        self._start_check()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Заголовок
        title = QLabel("Настройка Skyrim Special Edition")
        title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # Статус поиска
        self.status_label = QLabel("🔍 Ищем Skyrim на вашем компьютере...")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setFixedHeight(10)
        layout.addWidget(self.progress)

        # Лог поиска
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(120)
        self.log_box.setFont(QFont("Consolas", 8))
        layout.addWidget(self.log_box)

        # Опции (заполняются после проверки)
        self.options_box = QGroupBox("Что делаем?")
        self.options_layout = QVBoxLayout(self.options_box)
        self.btn_group = QButtonGroup(self)
        layout.addWidget(self.options_box)
        self.options_box.hide()

        # Кнопки
        btn_row = QHBoxLayout()
        self.btn_ok = QPushButton("Продолжить")
        self.btn_ok.setEnabled(False)
        self.btn_ok.setMinimumHeight(38)
        self.btn_ok.clicked.connect(self._on_ok)

        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setMinimumHeight(38)
        self.btn_cancel.clicked.connect(self.reject)

        btn_row.addWidget(self.btn_ok)
        btn_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_row)

    # ── Skyrim check ──────────────────────────────────────────────────────────

    def _start_check(self):
        self._worker = SkyrimCheckWorker()
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.log.connect(self._on_log)
        self._worker.finished.connect(self._on_check_done)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_log(self, msg: str):
        self.log_box.append(msg)

    def _on_check_done(self, result: SkyrimCheckResult):
        self._thread.quit()
        self._thread.wait()
        self._check_result = result

        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._build_options(result)
        self.options_box.show()
        self.btn_ok.setEnabled(True)
        self.adjustSize()

    # ── Build option radios ───────────────────────────────────────────────────

    def _build_options(self, r: SkyrimCheckResult):
        # Очищаем
        for btn in self.btn_group.buttons():
            self.btn_group.removeButton(btn)
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if r.found:
            # Показываем что нашли
            found_info = QLabel(
                f"<b>Найдено:</b> {r.skyrim_dir}<br>"
                f"Версия: <b>{r.exe_version or '?'}</b> "
                f"{'✅' if r.version_ok else '❌ (нужна 1.6.1170.0)'}<br>"
                f"AE DLC: {'✅ все на месте' if r.dlc_ok else f'❌ отсутствует {len(r.missing_dlc)} файлов'}<br>"
                f"Тип: {'Лицензия Steam' if r.is_licensed else '⚠️ без Steam-лицензии'}"
            )
            found_info.setWordWrap(True)
            self.options_layout.addWidget(found_info)

            if not r.is_licensed:
                # Найденная копия не проходит проверку лицензии Steam — раздачи
                # без подтверждённого владения не бывает, см. докстринг файла.
                self.status_label.setText(
                    "⚠️ Найденная копия Skyrim не проходит проверку лицензии Steam. "
                    "Лаунчер работает только с копией, купленной и запускаемой через Steam."
                )
                self._add_radio(
                    self.ACTION_STEAM,
                    "🎮 Купить/установить Skyrim SE в Steam",
                    checked=True,
                )
            elif r.is_ae_complete:
                # Идеальный случай — патчим
                self.status_label.setText(
                    "✅ Skyrim AE найден и готов к патчингу!"
                )
                self._add_radio(
                    self.ACTION_PATCH_EXISTING,
                    "🔨 Пропатчить найденную копию Skyrim",
                    checked=True,
                )
                self._add_radio(
                    self.ACTION_DOWNLOAD_OURS,
                    "📥 Скачать нашу версию Skyrim с сервера (заменит найденную)",
                )
            elif not r.version_ok:
                # Версия не та
                self.status_label.setText(
                    f"⚠️ Найден Skyrim, но версия {r.exe_version or '?'} ≠ 1.6.1170.0"
                )
                self._add_radio(
                    self.ACTION_DOWNLOAD_OURS,
                    "📥 Скачать правильную версию Skyrim с нашего сервера",
                    checked=True,
                )
                self._add_radio(
                    self.ACTION_STEAM,
                    "🎮 Обновить до 1.6.1170.0 через Steam",
                )
            else:
                # Версия ок но DLC неполные
                self.status_label.setText(
                    f"⚠️ Skyrim {r.exe_version} найден, но AE DLC неполные ({len(r.missing_dlc)} файлов)"
                )
                self._add_radio(
                    self.ACTION_DOWNLOAD_OURS,
                    "📥 Скачать полную версию с нашего сервера",
                    checked=True,
                )
                self._add_radio(
                    self.ACTION_STEAM,
                    "🎮 Установить AE DLC через Steam",
                )
        else:
            # Не найден — владение Steam подтвердить нечем (проверка лицензии
            # требует уже установленной копии), поэтому единственный вариант —
            # отправить в Steam. Раздачи "вслепую" не бывает.
            self.status_label.setText("❌ Skyrim Special Edition не найден")
            self._add_radio(
                self.ACTION_STEAM,
                "🎮 Купить и установить Skyrim SE в Steam",
                checked=True,
            )

    def _add_radio(self, action: str, text: str, checked: bool = False):
        rb = QRadioButton(text)
        rb.setChecked(checked)
        rb.setProperty("action", action)
        self.btn_group.addButton(rb)
        self.options_layout.addWidget(rb)

    # ── OK ───────────────────────────────────────────────────────────────────

    def _on_ok(self):
        selected = None
        for btn in self.btn_group.buttons():
            if btn.isChecked():
                selected = btn.property("action")
                break

        if not selected:
            return

        if selected == self.ACTION_STEAM:
            import webbrowser
            webbrowser.open(STEAM_STORE_URL)
            QMessageBox.information(
                self, "Steam",
                "Открыли страницу Steam.\n\n"
                "После установки/обновления Skyrim перезапустите лаунчер."
            )
            self.reject()
            return

        skyrim_dir = self._check_result.skyrim_dir if self._check_result else ""
        self.action_selected.emit(selected, skyrim_dir or "")
        self.accept()

    def closeEvent(self, event):
        if self._thread and self._thread.isRunning():
            if self._worker:
                self._worker.stop()
            self._thread.quit()
            self._thread.wait(1000)
        event.accept()
