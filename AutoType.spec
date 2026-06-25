from PyInstaller.utils.hooks import collect_all

# Collect pynput with all its platform backends and pyobjc deps
pynput_datas, pynput_binaries, pynput_hidden = collect_all("pynput")
quartz_datas, quartz_binaries, quartz_hidden = collect_all("Quartz")
appserv_datas, appserv_binaries, appserv_hidden = collect_all("ApplicationServices")

a = Analysis(
    ["src/autotype/main.py"],
    pathex=[],
    binaries=[*pynput_binaries, *quartz_binaries, *appserv_binaries],
    datas=[*pynput_datas, *quartz_datas, *appserv_datas],
    hiddenimports=[
        *pynput_hidden,
        *quartz_hidden,
        *appserv_hidden,
        "AppKit",
        "pynput.keyboard._darwin",
        "pynput.mouse._darwin",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoType",
    debug=False,
    strip=False,
    upx=True,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="AutoType",
)

app = BUNDLE(
    coll,
    name="AutoType.app",
    icon="AutoType.icns",
    bundle_identifier="com.autotype.app",
    info_plist={
        "LSUIElement": True,             # hide from Dock
        "NSHighResolutionCapable": True,
        "CFBundleName": "AutoType",
        "CFBundleDisplayName": "AutoType",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "NSPrincipalClass": "NSApplication",
        "NSAppleEventsUsageDescription": "AutoType uses keyboard events to type text.",
    },
)
