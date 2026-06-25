a = Analysis(
    ["src/autotype/main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleName": "AutoType",
        "CFBundleDisplayName": "AutoType",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "NSPrincipalClass": "NSApplication",
        "NSAppleEventsUsageDescription": "AutoType uses keyboard events to type text.",
    },
)
