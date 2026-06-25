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

# ---------------------------------------------------------------------------
# Keyboard injection via CoreGraphics (no pyobjc / pynput needed)
# ---------------------------------------------------------------------------

_cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

_cg.CGEventSourceCreate.restype = ctypes.c_void_p
_cg.CGEventSourceCreate.argtypes = [ctypes.c_int]
_cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
_cg.CGEventKeyboardSetUnicodeString.restype = None
_cg.CGEventKeyboardSetUnicodeString.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint16)
]
_cg.CGEventPost.restype = None
_cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
_cf.CFRelease.restype = None
_cf.CFRelease.argtypes = [ctypes.c_void_p]

_CG_HID_EVENT_TAP = 0
_CG_EVENT_SOURCE_STATE_HID = 1
_cg_source = _cg.CGEventSourceCreate(_CG_EVENT_SOURCE_STATE_HID)

# Virtual key codes for characters that need explicit keycodes
_KEYCODES: dict[str, int] = {
    "\n": 36,  # Return
    "\r": 36,
    "\t": 48,  # Tab
}


def _post_key(keycode: int, down: bool) -> None:
    ev = _cg.CGEventCreateKeyboardEvent(_cg_source, keycode, down)
    _cg.CGEventPost(_CG_HID_EVENT_TAP, ev)
    _cf.CFRelease(ev)


def _type_char(ch: str) -> None:
    if ch in _KEYCODES:
        kc = _KEYCODES[ch]
        _post_key(kc, True)
        _post_key(kc, False)
    else:
        buf = ch.encode("utf-16-le")
        arr = (ctypes.c_uint16 * (len(buf) // 2)).from_buffer_copy(buf)
        n = len(buf) // 2
        for down in (True, False):
            ev = _cg.CGEventCreateKeyboardEvent(_cg_source, 0, down)
            _cg.CGEventKeyboardSetUnicodeString(ev, n, arr)
            _cg.CGEventPost(_CG_HID_EVENT_TAP, ev)
            _cf.CFRelease(ev)


# ---------------------------------------------------------------------------
# macOS ObjC helpers via ctypes (no pyobjc import, no event-loop conflict)
# ---------------------------------------------------------------------------

def _nsapp():
    lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    lib.objc_getClass.restype = ctypes.c_void_p
    lib.objc_getClass.argtypes = [ctypes.c_char_p]
    lib.sel_registerName.restype = ctypes.c_void_p
    lib.sel_registerName.argtypes = [ctypes.c_char_p]
    lib.objc_msgSend.restype = ctypes.c_void_p
    lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cls = lib.objc_getClass(b"NSApplication")
    sel = lib.sel_registerName(b"sharedApplication")
    return lib, lib.objc_msgSend(cls, sel)


def _hide_from_dock() -> None:
    try:
        lib, app = _nsapp()
        lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        lib.objc_msgSend(app, lib.sel_registerName(b"setActivationPolicy:"), 1)
    except Exception as exc:
        logger.warning(f"dock-hide failed: {exc}")


def _activate_app() -> None:
    try:
        lib, app = _nsapp()
        lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        lib.objc_msgSend(app, lib.sel_registerName(b"activateIgnoringOtherApps:"), True)
    except Exception as exc:
        logger.warning(f"activate failed: {exc}")


def _is_accessibility_trusted() -> bool:
    try:
        lib = ctypes.CDLL(
            ctypes.util.find_library("ApplicationServices")
            or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        trusted = bool(lib.AXIsProcessTrusted())
        logger.debug(f"AXIsProcessTrusted: {trusted}")
        return trusted
    except Exception as exc:
        logger.warning(f"accessibility check failed: {exc}")
        return True


# ---------------------------------------------------------------------------

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
    progress = pyqtSignal(int, int)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._type_next)
        self._chars: list[str] = []
        self._index = 0
        self._total = 0

    def start(self, text: str, delay_ms: int) -> None:
        if not _is_accessibility_trusted():
            self.error.emit(
                "Accessibility permission is required.\n\n"
                "Open System Settings → Privacy & Security → Accessibility\n"
                "and add AutoType (or Terminal) to the list, then retry."
            )
            return
        self._chars = list(text)
        self._index = 0
        self._total = len(self._chars)
        logger.debug(f"Typing {self._total} chars at {delay_ms} ms/char")
        self._timer.start(delay_ms)

    def stop(self) -> None:
        self._timer.stop()

    def _type_next(self) -> None:
        if self._index >= self._total:
            self._timer.stop()
            self.finished.emit()
            return
        ch = self._chars[self._index]
        try:
            _type_char(ch)
        except Exception as exc:
            self._timer.stop()
            logger.error(f"_type_char failed at {self._index}: {exc}")
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
        self.text_edit.setPlainText(self._settings.value("text", ""))
        self.delay_spin.setValue(int(self._settings.value("char_delay_ms", 50)))
        self.start_delay_spin.setValue(int(self._settings.value("start_delay_s", 3)))

    def _save_settings(self):
        self._settings.setValue("text", self.text_edit.toPlainText())
        self._settings.setValue("char_delay_ms", self.delay_spin.value())
        self._settings.setValue("start_delay_s", self.start_delay_spin.value())

    def closeEvent(self, event):
        self._save_settings()
        event.ignore()
        self.hide()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    _hide_from_dock()

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
