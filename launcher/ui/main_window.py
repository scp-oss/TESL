# ==================== launcher/ui/main_window.py ====================
"""
Главное окно лаунчера.

Перед этим окном открывается ui/carousel_window.py::CarouselWindow — экран
выбора сборки (карусель плиток). Это окно открывается ПОСЛЕ выбора плитки,
уже для конкретной сборки (config.activate_build(), см. main.py).

Макет (780×500) — левая колонка "инструментов" (патч/проверка/очистка)
переехала в диалог настроек (⚙, см. _open_settings()/ui/settings_dialog.py,
прямой запрос пользователя 2026-09-22):
  ┌─────────────────────────────────────────────────────────────┐
  │ [📁 папка]  [🏷 ярлык]          Версия: ...      [⚙] [🌙] │
  ├────────────────────────────────┬──────────────────────────────┤
  │ Center (постер, шире)          │ Right 230px                  │
  │       poster.png               │ Выберите версию:             │
  │                                 │ [combo]                      │
  │ [▶ / 📥 / ⬆ / ⏹  TESVAE]      │ [Последняя]                  │
  │  (умная кнопка статуса)        │ [Обновить]                   │
  │                                 │ [Откат]                      │
  ├─────────────────────────────────┴──────────────────────────────┤
  │ [████████████████████████████████████] Пауза                 │
  ├─────────────────────────────────────────────────────────────┤
  │ [лог]                             Информация о версии        │
  ├─────────────────────────────────────────────────────────────┤
  │ Отправить репорт    ❤ Поддержать    Показать лог            │
  └─────────────────────────────────────────────────────────────┘
"""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, QSize, QMetaObject, Q_ARG, pyqtSlot
from PyQt6.QtGui import QCursor, QFontMetrics, QIcon, QPixmap, QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QProgressBar, QTextEdit, QFileDialog, QFrame, QComboBox,
    QSizePolicy, QMessageBox, QApplication,
)

from config import (
    WINDOW_TITLE, CONFIG_FILE, PROGRESS_FILE, APPDATA_DIR,
    MO2_EXE, MO2_SKSE_ARG, get_asset_path, get_launcher_commit,
)
from core.workers import (
    ThreadSafeWorker, VersionLoaderWorker, DownloadWorker,
    VerifyWorker, PosterLoader, SkyrimCheckWorker, CrashLogSender,
)
from core.patcher import SkyrimPatcher, MO2Configurator
from core.depot_client import DepotClient
from core.debug_log import maybe_upload_debug_log
from ui.settings_dialog import SettingsDialog

try:
    import winreg as _winreg
except ImportError:
    _winreg = None


DONATE_URL = "https://www.donationalerts.com/"   # замени на свой


# ── Poster widget ─────────────────────────────────────────────────────────────

class PosterWidget(QLabel):
    """300×480 px постер. Показывает заглушку пока не загружен."""

    W, H = 300, 420

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.W, self.H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: #1a1a1a; border-radius: 8px; color: #555;")
        self.setText("Загрузка постера...")

    def set_image(self, data: bytes):
        try:
            px = QPixmap()
            # loadFromData() возвращает bool, а не бросает исключение при
            # неудаче — раньше это не проверялось, и битые/нераспознанные
            # данные (200 OK, но не валидный PNG) тихо давали пустой
            # QPixmap дальше по цепочке вместо явного "Постер недоступен".
            if not px.loadFromData(data):
                self.setText("Постер\nнедоступен (битые данные)")
                return
            scaled = px.scaled(
                self.W, self.H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Crop to exact size
            x = (scaled.width()  - self.W) // 2
            y = (scaled.height() - self.H) // 2
            cropped = scaled.copy(x, y, self.W, self.H)
            self.setPixmap(cropped)
            self.setText("")
        except Exception:
            self.setText("Постер недоступен")

    def set_error(self):
        self.setText("Постер\nнедоступен")


# ── UIUpdater ─────────────────────────────────────────────────────────────────

from PyQt6.QtCore import QObject, pyqtSignal

class UIUpdater(QObject):
    update_log      = pyqtSignal(str)
    update_progress = pyqtSignal(int, int, str)
    update_progress_simple = pyqtSignal(int, int)


# ── Main window ───────────────────────────────────────────────────────────────

class UpdaterUI(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        icon_path = get_asset_path("icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(780, 500)
        self.setMinimumSize(700, 440)

        # ── State ─────────────────────────────────────────────────────────────
        self.is_installing   = False
        self.is_verifying    = False
        self.is_patching     = False
        self._worker_paused  = False
        self._is_closing     = False
        self._full_local_path = ""
        self._current_version = ""
        self._status_button_mode = "install"   # "install" | "update" | "play" — см. _refresh_status_button()
        self._pending_install_target = None    # version_label, который качает текущий install/update-запуск
        self._launcher_commit = get_launcher_commit()  # короткий git-хэш этого чекаута, "?" в собранном .exe

        # Разделитель новой сессии в LOG_FILE — файл иначе копился бы вечно
        # между запусками, вперемешку, без понятия "что было именно сейчас".
        self._write_log_file(f"===== new session, launcher {self._launcher_commit} =====")

        # ── Config ────────────────────────────────────────────────────────────
        self.data_lock    = threading.Lock()
        self.config: dict = {}
        self.progress_data: dict = {}

        # ── Workers ───────────────────────────────────────────────────────────
        self.worker          = None
        self.worker_thread   = None
        self.verify_worker   = None
        self.verify_thread   = None
        self.patcher_worker  = None
        self.patcher_thread  = None
        self.version_worker  = None
        self.version_thread  = None
        self.poster_worker   = None
        self.poster_thread   = None
        self.crash_sender    = None
        self.crash_thread    = None

        # ── UI Updater (потокобезопасный) ─────────────────────────────────────
        self.ui_updater = UIUpdater()

        # ── Build UI ──────────────────────────────────────────────────────────
        self._build_ui()
        self._connect_signals()
        self.ui_updater.update_log.connect(self._safe_append_log)
        self.ui_updater.update_progress.connect(self._safe_update_progress)
        self.ui_updater.update_progress_simple.connect(self._safe_update_progress_simple)

        # ── Load config ───────────────────────────────────────────────────────
        self._load_config()
        self._refresh_status_button()

        # ── Theme ─────────────────────────────────────────────────────────────
        if self._detect_system_theme() == "dark":
            self._apply_dark()
        else:
            self._apply_light()

        # ── Start background tasks ────────────────────────────────────────────
        QTimer.singleShot(100, self._start_version_loader)
        QTimer.singleShot(200, self._start_poster_loader)
        QTimer.singleShot(300, self._check_first_run)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        main_v = QVBoxLayout(self)
        main_v.setContentsMargins(10, 8, 10, 6)
        main_v.setSpacing(6)

        # ── Top bar ───────────────────────────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(12)

        self.lbl_folder = QLabel("📁 Выбрать папку")
        self.lbl_folder.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lbl_folder.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.lbl_shortcut = QLabel("🏷️ Создать ярлык")
        self.lbl_shortcut.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lbl_shortcut.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.lbl_version = QLabel(self._format_version_label("не установлена"))
        self.lbl_version.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # "🔄 Перезапустить Explorer" переехал в диалог настроек (см.
        # _open_settings()/ui/settings_dialog.py) — сама кнопка теперь
        # создаётся там, _restart_explorer() как метод остался прежним.
        self.btn_settings = QPushButton("⚙")
        self.btn_settings.setFixedSize(32, 32)
        self.btn_settings.setToolTip("Настройки")
        self.btn_settings.clicked.connect(self._open_settings)

        self.btn_theme = QPushButton("🌙")
        self.btn_theme.setFixedSize(32, 32)
        self.btn_theme.clicked.connect(self._toggle_theme)

        top.addWidget(self.lbl_folder)
        top.addWidget(self.lbl_shortcut)
        top.addStretch()
        top.addWidget(self.lbl_version)
        top.addWidget(self.btn_settings)
        top.addWidget(self.btn_theme)
        main_v.addLayout(top)

        # ── Content row ───────────────────────────────────────────────────────
        content = QHBoxLayout()
        content.setSpacing(10)

        # "Инструменты" (патч/проверка файлов/очистка Skyrim) переехали в
        # диалог настроек (см. _open_settings()/ui/settings_dialog.py) —
        # прямой запрос пользователя 2026-09-22 ("уберем все тулсы не
        # тулсы... в настройки"). Кнопки создаются здесь как раньше (нужны
        # main_window.py::ALL_BTNS/_disable_buttons() для блокировки во
        # время параллельных операций), просто НЕ добавляются ни в один
        # layout этого окна — SettingsDialog берёт готовые виджеты и
        # добавляет их в свой layout (см. её докстринг про реродительство).
        # Левой колонки с фиксированной шириной больше нет — освободившееся
        # место отдано постеру/центральной колонке (было 310, стало шире).
        self.btn_patch  = QPushButton("🔨 Пропатчить Skyrim")
        self.btn_verify = QPushButton("🛠 Проверить файлы")
        self.btn_revert = QPushButton("♻️ Очистить Skyrim")
        self.settings_dialog = None   # создаётся лениво, один раз — см. _open_settings()

        # Растяжка слева от центра — без своей левой колонки (190px) центр+
        # правая панель (340+230+10=580) уже не заполняют всю ширину окна
        # (780) сами по себе; без stretch'ей блок прижимался бы к левому
        # краю с некрасивой пустотой справа — с ними он по центру.
        content.addStretch(1)

        # Center panel — постер + кнопка запуска
        center = QFrame()
        center.setFixedWidth(340)
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(6)

        self.poster = PosterWidget()
        cv.addWidget(self.poster, alignment=Qt.AlignmentFlag.AlignCenter)

        self.btn_launch = QPushButton("▶  TESVAE")
        self.btn_launch.setFixedHeight(38)
        self.btn_launch.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        cv.addWidget(self.btn_launch)
        content.addWidget(center)

        # Right panel — версии
        right = QFrame()
        right.setFixedWidth(230)
        rv = QVBoxLayout(right)
        rv.setSpacing(6)

        rv.addWidget(QLabel("Выберите версию:"))
        self.combo_versions = QComboBox()
        self.combo_versions.addItem("Загрузка...")
        self.combo_versions.setFixedHeight(34)
        rv.addWidget(self.combo_versions)

        self.btn_latest   = QPushButton("🆕 Последняя")
        self.btn_update   = QPushButton("🔼 Обновить до выбранной")
        self.btn_rollback = QPushButton("↩️ Откат к выбранной")

        for b in (self.btn_latest, self.btn_update, self.btn_rollback):
            b.setFixedHeight(34)
            rv.addWidget(b)

        rv.addStretch()

        # Информация о версии
        rv.addWidget(QLabel("Информация о версии:"))
        self.lbl_version_info = QTextEdit()
        self.lbl_version_info.setReadOnly(True)
        self.lbl_version_info.setMaximumHeight(80)
        self.lbl_version_info.setPlaceholderText("Описание добавим позже")
        rv.addWidget(self.lbl_version_info)

        content.addWidget(right)
        content.addStretch(1)
        main_v.addLayout(content)

        # ── Progress bar ──────────────────────────────────────────────────────
        prog_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setFixedHeight(20)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self.progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        prog_row.addWidget(self.progress, 1)

        self.btn_pause = QPushButton("⏸ Пауза")
        self.btn_pause.setFixedSize(QSize(90, 32))
        self.btn_pause.setEnabled(False)
        prog_row.addWidget(self.btn_pause)
        main_v.addLayout(prog_row)

        # ── Log ───────────────────────────────────────────────────────────────
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        self.log.setFont(QFont("Consolas", 9))
        main_v.addWidget(self.log)

        # ── Bottom bar ────────────────────────────────────────────────────────
        bottom = QHBoxLayout()

        self.lbl_send_report = QLabel("📤 Отправить репорт")
        self.lbl_send_report.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lbl_send_report.setStyleSheet("color: #4a9eff;")

        self.lbl_donate = QLabel("❤️ Поддержать разработчиков")
        self.lbl_donate.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lbl_donate.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_donate.setStyleSheet("color: #e06c75;")

        self.lbl_show_log = QLabel("📋 Показать лог")
        self.lbl_show_log.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lbl_show_log.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_show_log.setStyleSheet("color: #4a9eff;")

        bottom.addWidget(self.lbl_send_report)
        bottom.addStretch()
        bottom.addWidget(self.lbl_donate)
        bottom.addStretch()
        bottom.addWidget(self.lbl_show_log)
        main_v.addLayout(bottom)

    # ── Signals ───────────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.lbl_folder.mousePressEvent   = lambda _: self._choose_folder()
        self.lbl_shortcut.mousePressEvent = lambda _: self._create_shortcut()

        self.btn_launch.clicked.connect(self._on_status_button_clicked)
        self.btn_patch.clicked.connect(self._patch_skyrim)
        self.btn_verify.clicked.connect(self._verify_files)
        self.btn_revert.clicked.connect(self._revert_skyrim)
        self.btn_latest.clicked.connect(self._update_to_latest)
        self.btn_update.clicked.connect(self._update_to_selected)
        self.btn_rollback.clicked.connect(self._rollback_to_selected)
        self.btn_pause.clicked.connect(self._toggle_pause)

        self.lbl_send_report.mousePressEvent = lambda _: self._send_crash_report()
        self.lbl_donate.mousePressEvent      = lambda _: self._open_donate()
        self.lbl_show_log.mousePressEvent    = lambda _: self._toggle_log_visibility()

        self.combo_versions.currentTextChanged.connect(self._on_version_selected)

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self):
        try:
            if CONFIG_FILE.exists():
                self.config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                folder = self.config.get("local_dir", "")
                if folder and Path(folder).exists():
                    self._full_local_path = folder
                    self._update_folder_label()
                    self._append_log(f"Папка: {folder}")
        except Exception as e:
            self._append_log(f"Ошибка загрузки конфига: {e}")

        try:
            if PROGRESS_FILE.exists():
                self.progress_data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _save_config(self):
        try:
            CONFIG_FILE.write_text(
                json.dumps(self.config, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as e:
            self._append_log(f"Ошибка сохранения конфига: {e}")

    def _save_progress(self):
        try:
            PROGRESS_FILE.write_text(
                json.dumps(self.progress_data, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    # ── Folder ────────────────────────────────────────────────────────────────

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Выберите папку установки MO2",
            options=QFileDialog.Option.ShowDirsOnly
        )
        if not folder:
            return
        self._full_local_path = folder
        self.config["local_dir"] = folder
        self._save_config()
        self._update_folder_label()
        self._append_log(f"Выбрана папка: {folder}")
        self._refresh_status_button()

    def _update_folder_label(self):
        if not self._full_local_path:
            self.lbl_folder.setText("📁 Выбрать папку")
            return
        fm     = QFontMetrics(self.lbl_folder.font())
        elided = fm.elidedText(f"📁 {self._full_local_path}", Qt.TextElideMode.ElideMiddle, 280)
        self.lbl_folder.setText(elided)
        self.lbl_folder.setToolTip(self._full_local_path)

    # ── Version loader ────────────────────────────────────────────────────────

    def _start_version_loader(self):
        self.version_worker = VersionLoaderWorker(channel="stable")
        self.version_thread = QThread()
        self.version_worker.moveToThread(self.version_thread)
        self.version_worker.log.connect(self._append_log)
        self.version_worker.versions_loaded.connect(self._on_versions_loaded)
        self.version_worker.current_version.connect(self._on_current_version)
        self.version_thread.started.connect(self.version_worker.run)
        self.version_thread.start()

    def _on_versions_loaded(self, versions: list):
        self.combo_versions.clear()
        if versions:
            self.combo_versions.addItems(versions)
        else:
            self.combo_versions.addItem("Нет доступных версий")
        self._refresh_status_button()

    def _format_version_label(self, version: str) -> str:
        """"Текущая версия: X (лаунчер: <git-хэш>)" — хэш нужен, чтобы при
        отладке видеть, какой код реально запущен, не гадать по выводу
        update_and_run.bat в другом окне (см. CLAUDE.md, тот же повод, что и
        у ложного "git pull failed" в этом сеансе). "?" в собранном .exe —
        не ошибка, там нет .git, чтобы его спросить."""
        return f"Текущая версия: {version}  (лаунчер: {self._launcher_commit})"

    def _on_current_version(self, label: str):
        self._current_version = label
        self.lbl_version.setText(self._format_version_label(label))
        self._refresh_status_button()

    def _on_version_selected(self, label: str):
        self._refresh_status_button()
        if not label or label in ("Загрузка...", "Нет доступных версий"):
            return
        # Показываем заметки версии
        from core.depot_client import DepotClient
        try:
            client = DepotClient()
            info   = client.get_version_info(label)
            client.close()
            if info:
                notes = info.get("notes", "Описание добавим позже")
                self.lbl_version_info.setPlainText(notes)
        except Exception:
            pass

    # ── Poster loader ─────────────────────────────────────────────────────────

    def _start_poster_loader(self):
        # worker хранится на self (self.poster_worker), не только thread —
        # раньше это была локальная переменная, и это, похоже, было причиной
        # того, что постер вообще никогда не грузился (ни успех, ни лог
        # ошибки — вообще ничего): без живой Python-ссылки PyQt6 может
        # собрать worker сборщиком мусора раньше, чем поток успеет дойти до
        # `thread.started.connect(worker.run)` и реально вызвать run().
        # Остальные воркеры в этом классе (self.worker/self.version_worker/
        # self.verify_worker/self.patcher_worker) уже хранятся на self —
        # этот был единственным исключением.
        self.poster_worker = PosterLoader()
        thread = QThread()
        self.poster_worker.moveToThread(thread)
        self.poster_worker.log.connect(self._append_log)
        self.poster_worker.loaded.connect(self.poster.set_image)
        self.poster_worker.failed.connect(self.poster.set_error)
        thread.started.connect(self.poster_worker.run)
        thread.finished.connect(thread.deleteLater)
        self.poster_thread = thread
        thread.start()

    # ── First run ─────────────────────────────────────────────────────────────

    def _check_first_run(self):
        if not self.config.get("first_run_done") and not self._full_local_path:
            from ui.first_run_dialog import FirstRunDialog
            dlg = FirstRunDialog(self)
            dlg.action_selected.connect(self._on_first_run_action)
            dlg.exec()

    def _on_first_run_action(self, action: str, skyrim_dir: str):
        from ui.first_run_dialog import FirstRunDialog
        self.config["first_run_done"] = True
        self.config["skyrim_dir"]     = skyrim_dir
        self._save_config()

        if action == FirstRunDialog.ACTION_PATCH_EXISTING:
            self._append_log("Запускаем патчинг найденного Skyrim...")
            self._patch_skyrim()
        elif action == FirstRunDialog.ACTION_DOWNLOAD_OURS:
            self._append_log("Выберите папку для установки и нажмите «Установить»")
            if not self._full_local_path:
                self._choose_folder()
        self._save_config()

    # ── Install ───────────────────────────────────────────────────────────────

    def _install(self):
        if self.is_installing:
            self._stop_worker()
            self.is_installing = False
            self._refresh_status_button()   # is_installing уже False -> пересчитает install/update/play заново
            self._enable_buttons()
            return

        if not self._full_local_path:
            self._choose_folder()
            if not self._full_local_path:
                return

        self.is_installing = True
        self._set_status_button("cancel")
        self._disable_buttons(exclude=[self.btn_launch])

        version = self.combo_versions.currentText()
        if version in ("Загрузка...", "Нет доступных версий"):
            version = None

        # Запоминаем, какую версию реально ставим — на завершении worker'а
        # запишем её в config["installed_version"] (см. _on_worker_finished).
        # version=None значит "текущая/последняя" — разрешаем в конкретную
        # метку по своему приоритету, раз сервер сам её не всегда знает
        # (у этой сборки "current" в depot.json пуст — см. CLAUDE.md).
        self._pending_install_target = version or self._current_version or self.combo_versions.currentText()

        task = {"type": "install", "local_dir": self._full_local_path, "version_label": version}
        self._start_worker(task)

    # ── Verify ────────────────────────────────────────────────────────────────

    def _verify_files(self):
        if not self._full_local_path:
            self._append_log("Сначала выберите папку установки")
            return

        if self.is_verifying:
            if self.verify_worker:
                self.verify_worker.stop()
            self.is_verifying = False
            self.btn_verify.setText("🛠 Проверить файлы")
            return

        self.is_verifying = True
        self.btn_verify.setText("⏹ Отменить")
        self._disable_buttons(exclude=[self.btn_verify])
        self.btn_pause.setEnabled(True)

        self.verify_worker = VerifyWorker(self._full_local_path)
        self.verify_thread = QThread()
        self.verify_worker.moveToThread(self.verify_thread)
        self.verify_worker.log.connect(self._append_log)
        self.verify_worker.progress.connect(self._on_verify_progress)
        self.verify_worker.finished.connect(self._on_verify_finished)
        self.verify_thread.started.connect(self.verify_worker.run)
        self.verify_thread.start()
        self._append_log("Запуск проверки файлов...")

    def _on_verify_progress(self, current, total, eta):
        pct = int(current / total * 100) if total else 0
        self.progress.setMaximum(total)
        self.progress.setValue(current)
        self.progress.setFormat(f"{pct}% — {current}/{total} — ETA: {eta}")

    def _on_verify_finished(self, ok, to_dl, to_redl, deleted):
        if self.verify_thread:
            self.verify_thread.quit()
            self.verify_thread.wait(2000)
        self.verify_worker = None
        self.verify_thread = None
        self.is_verifying  = False
        self.btn_verify.setText("🛠 Проверить файлы")
        self._enable_buttons()
        self.btn_pause.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setFormat("")

        if ok:
            total_bad = to_dl + to_redl
            self._append_log(f"Проверка: к загрузке {to_dl}, перекачать {to_redl}, удалено лишних {deleted}")
            if total_bad > 0:
                self._append_log(f"Найдено {total_bad} файлов требующих загрузки. Запускаем...")
                QTimer.singleShot(100, self._install)
        else:
            self._append_log("Проверка прервана")

        self._maybe_upload_debug_log("verify")

    # ── Patch / Revert ────────────────────────────────────────────────────────

    def _start_patcher(self, method: str, done_signal_name: str, btn, btn_text_orig: str, op_name: str):
        if self.is_patching:
            if self.patcher_worker:
                self.patcher_worker.stop()
            self.is_patching = False
            btn.setText(btn_text_orig)
            return

        self.is_patching = True
        btn.setText("⏹ Отменить")
        self._disable_buttons(exclude=[btn])
        self.btn_pause.setEnabled(True)

        self.patcher_worker = SkyrimPatcher()
        self.patcher_thread = QThread()
        self.patcher_worker.moveToThread(self.patcher_thread)
        self.patcher_worker.log_signal.connect(self._append_log)
        self.patcher_worker.progress_signal.connect(self._on_patcher_progress)
        getattr(self.patcher_worker, done_signal_name).connect(
            lambda ok, gp: self._on_patcher_done(ok, btn, btn_text_orig)
        )
        self.patcher_thread.started.connect(getattr(self.patcher_worker, method))
        self.patcher_thread.start()
        self._append_log(f"Запуск {op_name}...")

    def _patch_skyrim(self):
        self._start_patcher("run_patch", "finished_signal", self.btn_patch, "🔨 Пропатчить Skyrim", "патчинга")

    def _revert_skyrim(self):
        self._start_patcher("run_revert", "finished_signal", self.btn_revert, "♻️ Очистить Skyrim", "отката")

    def _on_patcher_progress(self, current, total, eta):
        pct = int(current / total * 100) if total else 0
        self.progress.setMaximum(total)
        self.progress.setValue(current)
        self.progress.setFormat(f"{pct}% — {current}/{total}" + (f" — ETA: {eta}" if eta else ""))

    def _on_patcher_done(self, ok: bool, btn: QPushButton, orig_text: str):
        if self.patcher_thread:
            self.patcher_thread.quit()
            self.patcher_thread.wait(1000)
        self.patcher_worker = None
        self.patcher_thread = None
        self.is_patching    = False
        btn.setText(orig_text)
        self._enable_buttons()
        self.btn_pause.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self._append_log("✅ Завершено" if ok else "❌ Завершено с ошибками")
        self._maybe_upload_debug_log("patch")

    # ── Launch ────────────────────────────────────────────────────────────────

    def _launch_game(self):
        if not self._full_local_path:
            self._append_log("Выберите папку MO2")
            return
        mo_exe = str(Path(self._full_local_path) / MO2_EXE)
        if not Path(mo_exe).exists():
            self._append_log(f"{MO2_EXE} не найден в {self._full_local_path}")
            return
        try:
            subprocess.Popen(
                [mo_exe, MO2_SKSE_ARG],
                cwd=self._full_local_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._append_log("▶ Запуск TESVAE... (подождите 10–80 сек)")
        except Exception as e:
            self._append_log(f"Ошибка запуска: {e}")

    # ── Status button (▶ TESVAE — install/update/play в одной кнопке) ──────────

    def _refresh_status_button(self):
        """
        Определяет режим большой кнопки по центру:
          - не выбрана папка ИЛИ ModOrganizer.exe там не найден -> "Установить"
          - найден, но записанная installed_version не совпадает с выбранной/
            текущей версией на сервере -> "Обновить"
          - иначе (включая случай "не знаем installed_version" — например,
            MO2 стоял ещё до этой возможности) -> "Играть"
        Вызывается при любом событии, способном поменять эту картину: выбор
        папки, загрузка списка версий, смена версии в комбобоксе, завершение
        установки/обновления.

        Во время активной установки (self.is_installing) НИЧЕГО не
        пересчитывает — кнопка должна оставаться "⏹ Отменить" (режим
        "cancel", выставленный явно в _install()) независимо от того, что
        пользователь мог успеть покрутить в комбобоксе версий, пока
        качается (combo_versions не блокируется во время install — не в
        ALL_BTNS); без этой ранней остановки смена версии в комбобоксе
        подменила бы подпись кнопки на "Обновить"/"Установить" прямо
        поверх идущей загрузки, хотя сама установка продолжала бы идти.
        """
        if self.is_installing:
            return
        mo_exe = Path(self._full_local_path) / MO2_EXE if self._full_local_path else None
        if not mo_exe or not mo_exe.exists():
            self._set_status_button("install")
            return

        installed = self.config.get("installed_version", "")
        target = self.combo_versions.currentText()
        if target in ("Загрузка...", "Нет доступных версий", ""):
            target = self._current_version

        if installed and target and installed != target:
            self._set_status_button("update")
        else:
            self._set_status_button("play")

    def _set_status_button(self, mode: str):
        """mode: "install" | "update" | "play" | "cancel". "cancel" — во
        время активной установки (см. _install()) — раньше это была
        отдельная кнопка btn_install слева ("📥 Установить" <-> "⏹
        Отменить"); та колонка переехала в настройки (см. _open_settings()),
        а её роль install/cancel полностью взяла на себя эта кнопка."""
        self._status_button_mode = mode
        if mode == "install":
            self.btn_launch.setText("📥  Установить")
        elif mode == "update":
            self.btn_launch.setText("⬆  Обновить")
        elif mode == "cancel":
            self.btn_launch.setText("⏹  Отменить")
        else:
            self.btn_launch.setText("▶  Играть")

    def _on_status_button_clicked(self):
        if self._status_button_mode in ("install", "update", "cancel"):
            self._install()
        else:
            self._launch_game()

    # ── Version actions ───────────────────────────────────────────────────────

    def _update_to_latest(self):
        if not self._full_local_path:
            self._choose_folder()
            if not self._full_local_path:
                return
        self._pending_install_target = self._current_version or self.combo_versions.currentText()
        task = {"type": "install", "local_dir": self._full_local_path, "version_label": None}
        self._start_worker(task)

    def _update_to_selected(self):
        label = self.combo_versions.currentText()
        if label in ("Загрузка...", "Нет доступных версий", ""):
            return
        if not self._full_local_path:
            self._choose_folder()
            if not self._full_local_path:
                return
        self._pending_install_target = label
        task = {"type": "install", "local_dir": self._full_local_path, "version_label": label}
        self._start_worker(task)

    def _rollback_to_selected(self):
        self._update_to_selected()  # depot хранит все файлы — откат = установка старой версии

    # ── Worker management ─────────────────────────────────────────────────────

    def _start_worker(self, task: dict):
        if self.worker:
            self._append_log("Рабочий процесс уже запущен")
            return

        self.worker = DownloadWorker(task)
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        self.worker.log.connect(self._append_log)
        self.worker.progress_total_setmax.connect(self.progress.setMaximum)
        self.worker.progress_total.connect(self.progress.setValue)
        self.worker.current_file.connect(lambda s: self.progress.setFormat(s))
        self.worker.speed_update.connect(lambda s: self._append_log(f"⬇ {s}"))
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.finished.connect(self.worker_thread.quit)

        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.start()

        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸ Пауза")
        self._disable_buttons(exclude=[self.btn_launch, self.btn_pause])

    def _on_worker_finished(self, ok: bool):
        if self._is_closing:
            return
        self.worker       = None
        self.worker_thread = None
        self._worker_paused = False
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ Пауза")
        self.progress.setValue(0)
        self.progress.setFormat("")

        if ok and self._pending_install_target:
            self.config["installed_version"] = self._pending_install_target
            self._save_config()
        self._pending_install_target = None
        self._refresh_status_button()

        if self.is_installing:
            self.is_installing = False
            # Кнопка статуса уже пересчитана выше (_refresh_status_button()) —
            # раньше здесь стояло self.btn_install.setText("📥 Установить"),
            # безусловно откатывая подпись на "Установить" независимо от
            # реального состояния; своя колонка с этой кнопкой убрана (см.
            # _open_settings()), и её текст теперь всегда только через
            # _set_status_button()/_refresh_status_button().
            self._enable_buttons()
            if ok:
                self._append_log("✅ Установка завершена")
                QTimer.singleShot(200, self._post_install_configure)
            else:
                self._append_log("❌ Установка завершена с ошибками")
        else:
            self._enable_buttons()
            self._append_log("✅ Готово" if ok else "❌ Завершено с ошибками")

        self._maybe_upload_debug_log("install")

    def _post_install_configure(self):
        """После установки: обновляем INI + создаём ярлык."""
        if not self._full_local_path:
            return
        from core.skyrim_checker import SkyrimChecker
        result = SkyrimChecker().check(log=self._append_log)
        if result.found:
            game_folder = result.skyrim_dir
            MO2Configurator.update_ini(self._full_local_path, game_folder, log=self._append_log)
        icon_path, arg = self._fetch_shortcut_assets()
        ok, msg = MO2Configurator.create_shortcut(
            self._full_local_path, log=self._append_log, icon_path=icon_path, arg=arg,
        )
        self._append_log(msg)

    def _fetch_shortcut_assets(self):
        """
        Качает иконку/аргумент ярлыка сборки с сервера (<remote_path>/src/
        icon.ico и /src/agr.json). Возвращает (icon_path|None, arg|None) —
        None в любом поле значит "не удалось/нет" — create_shortcut() сам
        откатывается на прежнее поведение (иконка из TargetPath, дефолтный
        MO2_SKSE_ARG). Никогда не бросает исключение наружу — падение этой
        загрузки не должно мешать созданию ярлыка вообще.
        """
        icon_path = None
        arg = None
        try:
            client = DepotClient()
            icon = client.fetch_shortcut_icon()
            if icon:
                icon_path = str(icon)
            arg_data = client.fetch_shortcut_arg()
            if isinstance(arg_data, dict):
                # Схема agr.json не подтверждена с сервера — пробуем
                # несколько правдоподобных имён поля, не гадаем на одном.
                for key in ("argument", "arg", "args", "skse_arg"):
                    if isinstance(arg_data.get(key), str) and arg_data[key]:
                        arg = arg_data[key]
                        break
                if arg is None:
                    self._append_log(
                        f"⚠️ agr.json скачан, но не нашли ожидаемое поле "
                        f"(argument/arg/args/skse_arg) в {list(arg_data.keys())} — "
                        f"использую аргумент по умолчанию"
                    )
            elif isinstance(arg_data, str) and arg_data:
                arg = arg_data
            client.close()
        except Exception as e:
            self._append_log(f"⚠️ Не удалось загрузить иконку/аргумент ярлыка с сервера: {e}")
        return icon_path, arg

    def _stop_worker(self):
        if self.worker:
            self.worker.stop()
            if self.worker_thread and self.worker_thread.isRunning():
                self.worker_thread.quit()
                self.worker_thread.wait(2000)

    def _toggle_pause(self):
        target = self.worker or self.verify_worker or self.patcher_worker
        if not target:
            return
        if self._worker_paused:
            target.resume()
            self._worker_paused = False
            self.btn_pause.setText("⏸ Пауза")
            self._append_log("▶ Возобновлено")
        else:
            target.pause()
            self._worker_paused = True
            self.btn_pause.setText("▶ Продолжить")
            self._append_log("⏸ Пауза")

    # ── Button state helpers ──────────────────────────────────────────────────

    ALL_BTNS = property(lambda self: [
        self.btn_launch, self.btn_patch,
        self.btn_verify, self.btn_revert, self.btn_latest,
        self.btn_update, self.btn_rollback,
    ])

    def _disable_buttons(self, exclude=None):
        excl = exclude or []
        for b in self.ALL_BTNS:
            if b not in excl:
                b.setEnabled(False)

    def _enable_buttons(self):
        for b in self.ALL_BTNS:
            b.setEnabled(True)

    # ── Shortcut & Explorer ───────────────────────────────────────────────────

    def _create_shortcut(self):
        if not self._full_local_path:
            self._append_log("Выберите папку MO2")
            return
        from core.skyrim_checker import SkyrimChecker
        result = SkyrimChecker().check(log=self._append_log)
        if result.found:
            MO2Configurator.update_ini(
                self._full_local_path, result.skyrim_dir, log=self._append_log
            )
        icon_path, arg = self._fetch_shortcut_assets()
        ok, msg = MO2Configurator.create_shortcut(
            self._full_local_path, log=self._append_log, icon_path=icon_path, arg=arg,
        )
        self._append_log(msg)

    # ── Settings dialog ───────────────────────────────────────────────────────

    def _open_settings(self):
        """
        Открывает диалог настроек (прямой запрос пользователя 2026-09-22) —
        создаётся ОДИН РАЗ (не при каждом клике): диалог реродительствует
        уже существующие self.btn_patch/self.btn_verify/self.btn_revert в
        свой layout, и повторное создание диалога заново реродительствовало
        бы их снова без вреда, но проще и дешевле просто переиспользовать
        один и тот же объект — см. SettingsDialog.__init__ докстринг.
        """
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(
                self,
                self.btn_patch, self.btn_verify, self.btn_revert,
                restart_explorer_fn=self._restart_explorer,
                debug_mode_getter=self._get_debug_mode,
                debug_mode_setter=self._set_debug_mode,
            )
        self.settings_dialog.exec()

    def _get_debug_mode(self) -> bool:
        return bool(self.config.get("debug_mode", False))

    def _set_debug_mode(self, enabled: bool):
        self.config["debug_mode"] = bool(enabled)
        self._save_config()
        self._append_log(
            "🐞 Режим отладки включён — лог будет отправляться на сервер"
            if enabled else "Режим отладки выключен"
        )

    def _maybe_upload_debug_log(self, reason: str):
        """
        Если включён режим отладки — фоново, best-effort отправляет
        LOG_FILE на сервер (core/debug_log.py, тот же WebDAV-механизм, что
        и у крэш-репортов). Вызывается после установки/проверки/патчинга и
        при закрытии окна (см. closeEvent) — НЕ блокирует UI (отдельный
        поток-демон, не QThread: приложение может закрываться прямо во
        время отправки, и это не должно держать процесс живым дольше, чем
        нужно — daemon-поток убивается вместе с процессом без вопросов).
        """
        if not self._get_debug_mode():
            return
        username = self.config.get("username", "") or "unknown_user"
        maybe_upload_debug_log(username, reason, log=self._append_log)

    def _restart_explorer(self):
        if os.name != "nt":
            return
        ans = QMessageBox.question(
            self, "Перезапустить Explorer?",
            "Это перезапустит explorer.exe. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", "Get-Process explorer | Stop-Process -Force; Start-Process explorer"],
                check=True, timeout=20,
            )
            self._append_log("Explorer перезапущен")
        except Exception as e:
            self._append_log(f"Ошибка: {e}")

    # ── Bottom bar actions ────────────────────────────────────────────────────

    def _send_crash_report(self):
        username = self.config.get("username", "").strip()
        if not username:
            from PyQt6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "Имя пользователя", "Введите ваш ник:")
            if not ok or not name.strip():
                return
            username = name.strip()
            self.config["username"] = username
            self._save_config()

        skyrim_dir = self.config.get("skyrim_dir", "")
        if not skyrim_dir:
            from core.skyrim_checker import SkyrimChecker
            result = SkyrimChecker().check()
            skyrim_dir = result.skyrim_dir or ""

        if not skyrim_dir:
            QMessageBox.warning(self, "Ошибка", "Не удалось найти папку Skyrim")
            return

        self._append_log("📤 Отправляем крэш-репорт...")
        # И sender, и t хранятся на self — та же ловушка PyQt6, что чинил у
        # PosterLoader (см. _start_poster_loader): локальные переменные без
        # живой Python-ссылки рискуют быть собраны сборщиком мусора раньше,
        # чем поток успеет реально вызвать sender.run().
        self.crash_sender = CrashLogSender(username, skyrim_dir)
        self.crash_thread = QThread()
        self.crash_sender.moveToThread(self.crash_thread)
        self.crash_sender.log.connect(self._append_log)
        self.crash_sender.finished.connect(lambda ok, msg: (
            self._append_log(f"{'✅' if ok else '❌'} {msg}"),
            QMessageBox.information(self, "Репорт", msg) if ok else
            QMessageBox.warning(self, "Ошибка", msg),
            self.crash_thread.quit(),
        ))
        self.crash_thread.started.connect(self.crash_sender.run)
        self.crash_thread.start()

    def _open_donate(self):
        import webbrowser
        webbrowser.open(DONATE_URL)

    def _toggle_log_visibility(self):
        self.log.setVisible(not self.log.isVisible())
        self.lbl_show_log.setText(
            "📋 Скрыть лог" if self.log.isVisible() else "📋 Показать лог"
        )

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        self._write_log_file(msg)
        try:
            if self._is_closing:
                return
            QMetaObject.invokeMethod(
                self.log, "append",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, str(msg))
            )
        except Exception:
            pass

    @staticmethod
    def _write_log_file(msg: str):
        """Дописывает в LOG_FILE — теперь общая config.write_log_file()
        (см. её докстринг: вынесена туда, когда появился второй вызывающий,
        ui/carousel_window.py, показывается ДО этого окна). Метод здесь
        оставлен как тонкая обёртка — вызывающий код внутри этого класса не
        трогали, чтобы не раздувать этот и без того большой diff."""
        import config as _config
        _config.write_log_file(msg)

    @pyqtSlot(str)
    def _safe_append_log(self, text: str):
        if not self._is_closing and self.log.updatesEnabled():
            self.log.append(text)

    @pyqtSlot(int, int, str)
    def _safe_update_progress(self, current, total, eta):
        if self._is_closing:
            return
        pct = int(current / total * 100) if total else 0
        self.progress.setMaximum(total)
        self.progress.setValue(current)
        self.progress.setFormat(f"{pct}% — {current}/{total} — ETA: {eta}")

    @pyqtSlot(int, int)
    def _safe_update_progress_simple(self, current, total):
        if self._is_closing:
            return
        self.progress.setMaximum(total)
        self.progress.setValue(current)

    # ── Theme ─────────────────────────────────────────────────────────────────

    _dark_ss = """
        QWidget { background: #2b2b2b; color: #ddd; font-family: "Segoe UI"; font-size: 10pt; }
        QPushButton { background: #3a3a3a; border: 1px solid #555; border-radius: 6px; padding: 6px; color: #eee; }
        QPushButton:hover { background: #505050; }
        QPushButton:disabled { background: #2e2e2e; color: #666; border-color: #444; }
        QProgressBar { border-radius: 6px; background: #444; height: 18px; color: white; text-align: center; }
        QProgressBar::chunk { border-radius: 6px; background: #16a34a; }
        QTextEdit { background: #1e1e1e; border: 1px solid #555; border-radius: 6px; color: #ddd; }
        QComboBox { background: #3a3a3a; border: 1px solid #555; border-radius: 6px; color: #ddd; padding: 4px; }
        QLabel { color: #ddd; }
    """
    _light_ss = """
        QWidget { background: #f6f7fb; color: #222; font-family: "Segoe UI"; font-size: 10pt; }
        QPushButton { background: #fff; border: 1px solid #d6dbe8; border-radius: 6px; padding: 6px; }
        QPushButton:hover { background: #eef5ff; }
        QPushButton:disabled { background: #e8e8e8; color: #999; }
        QProgressBar { border-radius: 6px; background: #e9eefb; height: 18px; text-align: center; }
        QProgressBar::chunk { border-radius: 6px; background: #4ade80; }
        QTextEdit { background: #fff; border: 1px solid #e1e6f2; border-radius: 6px; }
        QComboBox { background: #fff; border: 1px solid #d6dbe8; border-radius: 6px; padding: 4px; }
    """

    _current_theme = "light"

    def _apply_dark(self):
        self.setStyleSheet(self._dark_ss)
        self.btn_theme.setText("☀️")
        self._current_theme = "dark"

    def _apply_light(self):
        self.setStyleSheet(self._light_ss)
        self.btn_theme.setText("🌙")
        self._current_theme = "light"

    def _toggle_theme(self):
        if self._current_theme == "light":
            self._apply_dark()
        else:
            self._apply_light()

    def _detect_system_theme(self) -> str:
        if _winreg is None:
            return "light"
        try:
            key = _winreg.OpenKey(
                _winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            )
            val, _ = _winreg.QueryValueEx(key, "AppsUseLightTheme")
            _winreg.CloseKey(key)
            return "light" if val == 1 else "dark"
        except Exception:
            return "light"

    # ── Close ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._is_closing = True
        self._maybe_upload_debug_log("close")
        for attr in ("worker", "verify_worker", "patcher_worker"):
            w = getattr(self, attr, None)
            if w:
                try:
                    w.stop()
                except Exception:
                    pass
        for attr in ("worker_thread", "verify_thread", "patcher_thread",
                     "version_thread", "poster_thread"):
            t = getattr(self, attr, None)
            if t and t.isRunning():
                t.quit()
                t.wait(500)
        self._save_config()
        self._save_progress()
        event.accept()
