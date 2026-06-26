# AutoType

A macOS menu-bar utility that simulates real keyboard input. Paste or type
text into it, click Start, and after a short countdown it types the text into
whatever application is focused.

It is built for apps that cannot accept a normal paste, such as VNC/RDP
viewers, virtual machine consoles, and other tools that capture raw key events.
Because AutoType posts genuine virtual-keycode events (not a pasted string),
those apps receive the keystrokes as if they came from a physical keyboard.

## Features

- Lives in the menu bar (no Dock icon); click the tray icon to show/hide the window.
- Plain-text editor; pasting strips formatting automatically.
- Configurable per-character delay and start countdown.
- Optionally hides the window when typing starts so it stays out of the way.
- Remembers the text and all settings between launches.
- Types via real key events with correct modifier (Shift/Option) presses, so
  characters like `"` and `!` arrive correctly in raw-key-capture apps.

## Requirements

- macOS (Apple Silicon or Intel).
- Accessibility permission. The first time you type, grant it under
  System Settings -> Privacy & Security -> Accessibility, then retry.

## Running from source

Uses [uv](https://docs.astral.sh/uv/) with Python 3.13.

```sh
uv sync
uv run autotype
```

## Building the macOS app

```sh
./build.sh
```

This generates the icon, builds `dist/AutoType.app` with PyInstaller, and signs
it. To install:

```sh
cp -R dist/AutoType.app /Applications/
xattr -cr /Applications/AutoType.app
```

Because the app is ad-hoc signed (no Developer ID), each fresh install is a new
identity to macOS, so you will need to re-grant Accessibility permission after
reinstalling.

## Downloading a release

Pre-built releases are available on the GitHub Releases page. After downloading
and unzipping, macOS Gatekeeper will block the app because it was not downloaded
from the App Store and is not notarized. Run the following before launching:

```sh
xattr -cr /Applications/AutoType.app
```

Or right-click the app in Finder and choose Open the first time.

You will also need to grant Accessibility permission on first launch.

## How it works

- UI and event loop: PyQt6. Typing is driven by a `QTimer` on the main thread
  (one character per tick), which avoids a native crash that occurs when the
  key-event source is created off the main thread.
- Keystroke injection: CoreGraphics, called directly through `ctypes`. Each
  character is mapped to its virtual keycode and modifier flags using the active
  keyboard layout (`UCKeyTranslate`). For characters that need a modifier, a real
  Shift/Option key-down/up is posted around the character so raw-key-capture apps
  forward the right key. Characters with no key on the current layout fall back to
  a Unicode-string event.

## Notes and limitations

- The keycode map is built from the keyboard layout active when typing starts.
  If the remote machine uses a different layout, some punctuation may differ.
- The app must NOT be signed with the hardened runtime (`codesign --options
  runtime`). Hardened runtime enables strict arm64e pointer-authentication
  enforcement that crashes Qt's static initializers at load time
  (`__CFCheckCFInfoPACSignature`). `build.sh` signs plain ad-hoc to avoid this.

## Project layout

```
src/autotype/main.py   Application code
AutoType.spec          PyInstaller build spec
scripts/make_icon.py   Generates AutoType.icns
build.sh               Build and sign the .app
```
