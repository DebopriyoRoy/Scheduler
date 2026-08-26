# PyInstaller build definition for the Spirit Scheduler Mac application.
#
# Build with:  ./desktop/build_app.sh
#
# Django resolves a great deal at runtime through strings, so the modules it reaches
# that way have to be declared here - PyInstaller's static analysis cannot see them.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO = Path(SPECPATH).parent

hiddenimports = [
    *collect_submodules("django"),
    *collect_submodules("spirit_scheduler"),
    *collect_submodules("scheduling"),
    *collect_submodules("integrations"),
    "playwright",
    "playwright.sync_api",
    "playwright.async_api",
    "greenlet",
    "pyee",
    "whitenoise",
    "whitenoise.middleware",
    "whitenoise.storage",
    "openpyxl",
    "reportlab",
    "bs4",
    "dotenv",
]

datas = [
    (str(REPO / "scheduling" / "templates"), "scheduling/templates"),
    (str(REPO / "scheduling" / "static"), "scheduling/static"),
    (str(REPO / "scheduling" / "migrations"), "scheduling/migrations"),
    (str(REPO / "staticfiles"), "staticfiles"),
    # Playwright ships a Node driver alongside the Python package; without it the
    # library imports but cannot launch anything.
    *collect_data_files("playwright"),
]

# The starter database ships inside the bundle and is copied out on first launch.
seed = REPO / "db.sqlite3"
if seed.exists():
    datas.append((str(seed), "."))

a = Analysis(
    [str(REPO / "desktop" / "app_main.py")],
    pathex=[str(REPO)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Playwright is required, not optional: the show calendar is rendered entirely in
    # the browser, so importing shows cannot work without a real browser engine. Only
    # the Python package is bundled - Chromium itself lives in the user's cache
    # directory and is fetched on first use by ensure_browser() in app_main.
    excludes=["pytest", "ruff", "PyInstaller", "psycopg", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Spirit Scheduler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Spirit Scheduler",
)

app = BUNDLE(
    coll,
    name="Spirit Scheduler.app",
    icon=None,
    bundle_identifier="com.spiritofnewfoundland.scheduler",
    info_plist={
        "CFBundleName": "Spirit Scheduler",
        "CFBundleDisplayName": "Spirit Scheduler",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        # No dock icon or menu bar: the interface is the browser window it opens.
        "LSBackgroundOnly": False,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Spirit of Newfoundland Productions",
    },
)
