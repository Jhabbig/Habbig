from PyInstaller.utils.hooks import collect_submodules
block_cipher = None

a = Analysis(
    ["app/desktop/app_entry.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("app/templates", "app/templates"),
        ("app/config.yaml", "app"),
        ("app/desktop/assets", "app/desktop/assets"),
    ],
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
        "sqlmodel", "sqlalchemy", "sqlalchemy.dialects.sqlite", "aiosqlite",
        "httpx", "httpx._transports", "httpx._transports.default", "httpcore",
        "httpcore._async", "httpcore._sync",
        "apscheduler", "apscheduler.schedulers.asyncio", "apscheduler.triggers.interval",
        "jinja2", "yaml", "dotenv",
        *collect_submodules("app"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas", "webview", "rumps",
              "pyobjc-core", "pyobjc-framework-Cocoa", "pyobjc-framework-WebKit",
              "pyobjc-framework-Quartz", "pyobjc-framework-security"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PolymarketDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
)

coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="PolymarketDashboard")

app = BUNDLE(
    coll,
    name="PolymarketDashboard.app",
    icon="app/desktop/assets/icon.icns",
    bundle_identifier="com.polymarket.signal-dashboard",
    info_plist={
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
        "NSRequiresAquaSystemAppearance": False,
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleName": "Polymarket Signal",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
