import ctypes
import ctypes.util
import sys
from loguru import logger

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# pynput is imported once at module load to initialize Quartz on the main thread
# before the Qt event loop starts — avoids a native crash on macOS when the
# import happens later in a background thread.
try:
    from pynput.keyboard import Controller as _KeyboardController
    logger.debug("pynput loaded on main thread")
except Exception as _e:
    _KeyboardController = None  # type: ignore[assignment,misc]
    logger.warning(f"pynput unavailable: {_e}")

def _is_accessibility_trusted() -> bool:
    try:
        appserv = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("ApplicationServices")
            or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        appserv.AXIsProcessTrusted.restype = ctypes.c_bool
        trusted = bool(appserv.AXIsProcessTrusted())
        logger.debug(f"AXIsProcessTrusted: {trusted}")
        return trusted
    except Exception as exc:
        logger.warning(f"Could not check accessibility: {exc}")
        return True  # assume OK and let pynput fail with its own error


def _activate_app():
    try:
        from AppKit import NSApplication  # type: ignore[import]
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass

def _make_tray_icon(size: int = 64) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#439425"))
    p.setPen(Qt.PenStyle.NoPen)
    r = size // 8
    p.drawRoundedRect(2, 2, size - 4, size - 4, r, r)
    p.setPen(QColor("white"))
    f = QFont("Arial", size // 2, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "AT")
    p.end()
    return QIcon(pix)


class PlainTextEdit(QTextEdit):
    def insertFromMimeData(self, source):
        if source:
            self.insertPlainText(source.text())


class TypingController(QObject):
    """Types characters one-per-timer-tick on the main thread (avoids macOS
    native crash when pynput's CGEventSource is created in a background thread)."""

    progress = pyqtSignal(int, int)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._type_next)
        self._kb = None
        self._chars: list[str] = []
        self._index = 0
        self._total = 0

    def start(self, text: str, delay_ms: int):
        if not _is_accessibility_trusted():
            self.error.emit(
                "Accessibility permission is required.\n\n"
                "Open System Settings → Privacy & Security → Accessibility\n"
                "and add your Terminal (or this app) to the list, then retry."
            )
            return

        if _KeyboardController is None:
            self.error.emit("pynput failed to load — see logs for details.")
            return

        try:
            logger.debug("Creating keyboard controller on main thread")
            self._kb = _KeyboardController()
            logger.debug("Keyboard controller created")
        except Exception as exc:
            logger.error(f"Controller() failed: {exc}")
            self.error.emit(str(exc))
            return

        self._chars = list(text)
        self._index = 0
        self._total = len(self._chars)
        logger.debug(f"Starting typing {self._total} chars at {delay_ms}ms/char")
        self._timer.start(delay_ms)

    def stop(self):
        self._timer.stop()
        self._kb = None

    def _type_next(self):
        assert self._kb is not None
        if self._index >= self._total:
            self._timer.stop()
            self.finished.emit()
            return
        ch = self._chars[self._index]
        try:
            self._kb.type(ch)
        except Exception as exc:
            self._timer.stop()
            logger.error(f"type() failed at char {self._index}: {exc}")
            self.error.emit(str(exc))
            return
        self._index += 1
        self.progress.emit(self._index, self._total)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._typer = TypingController(self)
        self._typer.progress.connect(self._on_progress)
        self._typer.finished.connect(self._on_done)
        self._typer.error.connect(self._on_error)
        self._countdown_timer: QTimer | None = None
        self._countdown_val = 3
        self._settings = QSettings("autotype", "AutoType")
        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        self.setWindowTitle("AutoType")
        self.setMinimumWidth(440)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("AutoType")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        root.addWidget(title)

        sub = QLabel("Simulates keyboard input for apps that block paste (VNC, etc.)")
        sub.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #ddd;")
        root.addWidget(sep)

        root.addWidget(QLabel("Text to type:"))
        self.text_edit = PlainTextEdit()
        self.text_edit.setPlaceholderText("Paste or type your text here…")
        self.text_edit.setMinimumHeight(160)
        self.text_edit.setStyleSheet("font-family: 'Menlo'; font-size: 13px;")
        root.addWidget(self.text_edit)

        row = QHBoxLayout()
        row.addWidget(QLabel("Char delay:"))
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(10, 2000)
        self.delay_spin.setValue(50)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setFixedWidth(90)
        row.addWidget(self.delay_spin)
        row.addStretch()
        row.addWidget(QLabel("Start in:"))
        self.start_delay_spin = QSpinBox()
        self.start_delay_spin.setRange(1, 10)
        self.start_delay_spin.setValue(3)
        self.start_delay_spin.setSuffix(" s")
        self.start_delay_spin.setFixedWidth(65)
        row.addWidget(self.start_delay_spin)
        root.addLayout(row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #555; font-size: 13px;")
        root.addWidget(self.status_label)

        btn_row = QHBoxLayout()

        self.start_btn = QPushButton("Start")
        self.start_btn.setStyleSheet(
            "QPushButton { background:#4A90E2; color:white; border:none;"
            " padding:8px 24px; border-radius:5px; font-size:14px; font-weight:bold; }"
            "QPushButton:hover { background:#357ABD; }"
            "QPushButton:disabled { background:#aaa; }"
        )
        self.start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self.start_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet(
            "QPushButton { background:#E74C3C; color:white; border:none;"
            " padding:8px 24px; border-radius:5px; font-size:14px; }"
            "QPushButton:hover { background:#C0392B; }"
        )
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)

        root.addLayout(btn_row)

    def _on_start(self):
        text = self.text_edit.toPlainText()
        if not text:
            self.status_label.setText("Enter some text first.")
            return

        self.start_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self._countdown_val = self.start_delay_spin.value()
        self._tick()

    def _tick(self):
        if self._countdown_val > 0:
            self.status_label.setText(
                f"Starting in {self._countdown_val}s — switch to target app now!"
            )
            self._countdown_val -= 1
            self._countdown_timer = QTimer()
            self._countdown_timer.setSingleShot(True)
            self._countdown_timer.timeout.connect(self._tick)
            self._countdown_timer.start(1000)
        else:
            self._begin_typing()

    def _begin_typing(self):
        text = self.text_edit.toPlainText()
        self.progress_bar.setMaximum(len(text))
        self.status_label.setText("Typing…")
        self._typer.start(text, self.delay_spin.value())

    def _on_progress(self, current: int, total: int):
        self.progress_bar.setValue(current)
        self.status_label.setText(f"Typing… {current} / {total} chars")

    def _on_done(self):
        self.status_label.setText("Done!")
        self._reset()

    def _on_error(self, msg: str):
        self.status_label.setText(f"Error: {msg}")
        self._reset()

    def _on_cancel(self):
        if self._countdown_timer:
            self._countdown_timer.stop()
        self._typer.stop()
        self.status_label.setText("Cancelled.")
        self._reset()

    def _reset(self):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)

    def _load_settings(self):
        logger.info("Loading settings")
        self.text_edit.setPlainText(self._settings.value("text", ""))
        self.delay_spin.setValue(int(self._settings.value("char_delay_ms", 50)))
        self.start_delay_spin.setValue(int(self._settings.value("start_delay_s", 3)))

    def _save_settings(self):
        logger.info("Saving settings")
        self._settings.setValue("text", self.text_edit.toPlainText())
        self._settings.setValue("char_delay_ms", self.delay_spin.value())
        self._settings.setValue("start_delay_s", self.start_delay_spin.value())

    def closeEvent(self, a0):
        self._save_settings()
        if a0:
            a0.ignore()  # hide instead of close so tray keeps working
        self.hide()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore[import]
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception as e:
        print(f"[AppKit] skipped: {e}", flush=True)

    window = MainWindow()

    tray = QSystemTrayIcon()
    tray.setIcon(_make_tray_icon())
    tray.setToolTip("AutoType")

    menu = QMenu()

    show_action = QAction("Show / Hide")

    def toggle_window():
        if window.isVisible():
            window.hide()
        else:
            _activate_app()
            window.show()
            window.raise_()
            window.activateWindow()

    show_action.triggered.connect(toggle_window)
    menu.addAction(show_action)
    menu.addSeparator()

    quit_action = QAction("Quit AutoType")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: toggle_window()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
