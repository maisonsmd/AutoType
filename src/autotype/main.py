import ctypes
import ctypes.util
import sys
from loguru import logger

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
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
#
# We post real virtual-keycode key events (with modifier flags) rather than
# attaching a Unicode string to a keycode-0 event. The Unicode-string method
# works for normal text fields but is ignored by apps that capture raw key
# events — VNC/RDP viewers, games, terminals in some modes — because there is
# no real key behind it. Mapping each character to its keycode+modifiers via
# the active keyboard layout produces events those apps recognize.
# ---------------------------------------------------------------------------

_cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")

_cg.CGEventSourceCreate.restype = ctypes.c_void_p
_cg.CGEventSourceCreate.argtypes = [ctypes.c_int]
_cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
_cg.CGEventKeyboardSetUnicodeString.restype = None
_cg.CGEventKeyboardSetUnicodeString.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint16)
]
_cg.CGEventSetFlags.restype = None
_cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_cg.CGEventPost.restype = None
_cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
_cf.CFRelease.restype = None
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_cf.CFDataGetBytePtr.restype = ctypes.c_void_p
_cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]

_CG_HID_EVENT_TAP = 0
_CG_EVENT_SOURCE_STATE_HID = 1
_cg_source = _cg.CGEventSourceCreate(_CG_EVENT_SOURCE_STATE_HID)

# CGEventFlags masks
_FLAG_SHIFT = 0x00020000
_FLAG_OPTION = 0x00080000

# Virtual keycodes for the modifier keys themselves. Apps that capture raw key
# events (VNC/RDP) need a real modifier key-down/up — a flag on the character
# event alone is ignored, so e.g. Shift+' would arrive as a plain '.
_MOD_KEYCODES = [
    (_FLAG_SHIFT, 56),   # left Shift
    (_FLAG_OPTION, 58),  # left Option/Alt
]

# Virtual key codes for keys with no printable character of their own.
_SPECIAL_KEYCODES: dict[str, int] = {
    "\n": 36,  # Return
    "\r": 36,
    "\t": 48,  # Tab
}

# Built lazily: char -> (virtual_keycode, cg_event_flags)
_CHAR_MAP: dict[str, tuple[int, int]] | None = None


def _build_char_map() -> dict[str, tuple[int, int]]:
    """Reverse-map characters to (keycode, flags) using the active layout."""
    mapping: dict[str, tuple[int, int]] = {}
    try:
        _carbon.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
        _carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
        _carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _carbon.LMGetKbdType.restype = ctypes.c_uint8
        _carbon.UCKeyTranslate.restype = ctypes.c_int32
        _carbon.UCKeyTranslate.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_uint16),
        ]
        prop = ctypes.c_void_p.in_dll(_carbon, "kTISPropertyUnicodeKeyLayoutData")

        src = _carbon.TISCopyCurrentKeyboardInputSource()
        layout_data = _carbon.TISGetInputSourceProperty(src, prop)
        if not layout_data:
            return mapping
        layout_ptr = _cf.CFDataGetBytePtr(layout_data)
        kbd_type = _carbon.LMGetKbdType()

        # UCKeyTranslate modifier state is (EventModifiers >> 8): shift=0x02, option=0x08
        mod_states = [
            (0x00, 0),
            (0x02, _FLAG_SHIFT),
            (0x08, _FLAG_OPTION),
            (0x0A, _FLAG_SHIFT | _FLAG_OPTION),
        ]
        for keycode in range(128):
            for uc_mods, cg_flags in mod_states:
                dead = ctypes.c_uint32(0)
                buf = (ctypes.c_uint16 * 8)()
                length = ctypes.c_ulong(0)
                status = _carbon.UCKeyTranslate(
                    layout_ptr, keycode, 0, uc_mods, kbd_type,
                    1,  # kUCKeyTranslateNoDeadKeysMask
                    ctypes.byref(dead), 8, ctypes.byref(length), buf,
                )
                if status != 0 or length.value != 1:
                    continue
                ch = chr(buf[0])
                if ch.isprintable() and ch not in mapping:
                    mapping[ch] = (keycode, cg_flags)
    except Exception as exc:
        logger.warning(f"could not build keycode map: {exc}")
    logger.debug(f"keycode map built: {len(mapping)} chars")
    return mapping


def _post_key(keycode: int, flags: int, down: bool) -> None:
    ev = _cg.CGEventCreateKeyboardEvent(_cg_source, keycode, down)
    # Always set flags explicitly (incl. 0) so the event doesn't inherit the
    # real hardware modifier state at post time.
    _cg.CGEventSetFlags(ev, flags)
    _cg.CGEventPost(_CG_HID_EVENT_TAP, ev)
    _cf.CFRelease(ev)


def _type_unicode(ch: str) -> None:
    """Fallback for characters with no key on the current layout (emoji, etc.)."""
    buf = ch.encode("utf-16-le")
    n = len(buf) // 2
    arr = (ctypes.c_uint16 * n).from_buffer_copy(buf)
    for down in (True, False):
        ev = _cg.CGEventCreateKeyboardEvent(_cg_source, 0, down)
        _cg.CGEventKeyboardSetUnicodeString(ev, n, arr)
        _cg.CGEventPost(_CG_HID_EVENT_TAP, ev)
        _cf.CFRelease(ev)


def _type_char(ch: str) -> None:
    global _CHAR_MAP
    if ch in _SPECIAL_KEYCODES:
        _post_key(_SPECIAL_KEYCODES[ch], 0, True)
        _post_key(_SPECIAL_KEYCODES[ch], 0, False)
        return
    if _CHAR_MAP is None:
        _CHAR_MAP = _build_char_map()
    entry = _CHAR_MAP.get(ch)
    if entry is not None:
        keycode, flags = entry
        # Press the required modifier keys first, accumulating their flags, so
        # raw-key-capture apps (VNC) see a genuine Shift/Option key-down.
        held = 0
        mods = [(bit, kc) for bit, kc in _MOD_KEYCODES if flags & bit]
        for bit, kc in mods:
            held |= bit
            _post_key(kc, held, True)
        _post_key(keycode, held, True)
        _post_key(keycode, held, False)
        for bit, kc in reversed(mods):
            held &= ~bit
            _post_key(kc, held, False)
    else:
        _type_unicode(ch)


# ---------------------------------------------------------------------------
# macOS ObjC helpers via ctypes (no pyobjc import, no event-loop conflict)
# ---------------------------------------------------------------------------

def _nsapp():
    lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc")) # type: ignore
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


def _appservices():
    return ctypes.CDLL(
        ctypes.util.find_library("ApplicationServices")
        or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )


def _is_accessibility_trusted() -> bool:
    try:
        lib = _appservices()
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        trusted = bool(lib.AXIsProcessTrusted())
        logger.debug(f"AXIsProcessTrusted: {trusted}")
        return trusted
    except Exception as exc:
        logger.warning(f"accessibility check failed: {exc}")
        return True


def _request_accessibility() -> bool:
    """Check trust, asking macOS to show its native permission dialog if not.

    Uses AXIsProcessTrustedWithOptions with kAXTrustedCheckOptionPrompt=true,
    which presents the system prompt with an "Open System Settings" button.
    """
    try:
        lib = _appservices()
        lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]

        prompt_key = ctypes.c_void_p.in_dll(lib, "kAXTrustedCheckOptionPrompt")
        true_val = ctypes.c_void_p.in_dll(_cf, "kCFBooleanTrue")

        _cf.CFDictionaryCreate.restype = ctypes.c_void_p
        _cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        keys = (ctypes.c_void_p * 1)(prompt_key)
        vals = (ctypes.c_void_p * 1)(true_val)
        opts = _cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
        trusted = bool(lib.AXIsProcessTrustedWithOptions(opts))
        if opts:
            _cf.CFRelease(opts)
        logger.debug(f"AXIsProcessTrustedWithOptions(prompt): {trusted}")
        return trusted
    except Exception as exc:
        logger.warning(f"accessibility prompt failed: {exc}")
        return _is_accessibility_trusted()


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

        self.hide_on_start_check = QCheckBox("Hide window when typing starts")
        self.hide_on_start_check.setChecked(True)
        root.addWidget(self.hide_on_start_check)

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
        self._save_settings()
        if self.hide_on_start_check.isChecked():
            self.hide()  # get out of the way; the countdown runs in the background
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
        self.hide_on_start_check.setChecked(
            self._settings.value("hide_on_start", True, type=bool)
        )

    def _save_settings(self):
        self._settings.setValue("text", self.text_edit.toPlainText())
        self._settings.setValue("char_delay_ms", self.delay_spin.value())
        self._settings.setValue("start_delay_s", self.start_delay_spin.value())
        self._settings.setValue("hide_on_start", self.hide_on_start_check.isChecked())

    def closeEvent(self, a0):
        self._save_settings()
        if a0:
            a0.ignore()
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

    # On launch, make sure we have Accessibility permission. If not, trigger the
    # native macOS prompt and explain in a dialog (the permission only takes
    # effect after the app is relaunched).
    if not _request_accessibility():
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Accessibility permission required")
        box.setText("AutoType needs Accessibility permission to type for you.")
        box.setInformativeText(
            "macOS should have just shown a permission prompt. Open\n"
            "System Settings > Privacy & Security > Accessibility,\n"
            "enable AutoType, then quit and reopen the app.\n\n"
            "Until then, typing will not work."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        _activate_app()
        box.exec()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
