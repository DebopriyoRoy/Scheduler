"""Entry point for the packaged Spirit Scheduler Mac application.

Double-clicking the app runs this. It prepares a writable data directory, brings the
database up to date, starts the web server on a free port, and opens the browser. The
window it opens is the application; quitting the app stops the server.

Everything the app writes lives in ~/Library/Application Support/Spirit Scheduler,
because the code itself sits inside a read-only .app bundle.
"""

import os
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "Spirit Scheduler"
DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
DB_PATH = DATA_DIR / "db.sqlite3"
KEY_PATH = DATA_DIR / "secret_key"
ENV_PATH = DATA_DIR / "settings.env"


def bundle_dir() -> Path:
    """Where the bundled resources live, frozen or running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # noqa: SLF001 - PyInstaller's documented attribute
    return Path(__file__).resolve().parent.parent


def free_port(preferred: int = 8765) -> int:
    """Prefer a stable port so bookmarks keep working; fall back if it is taken."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    return 0


def already_running(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def prepare_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # A per-installation signing key, generated once and kept.
    if not KEY_PATH.exists():
        KEY_PATH.write_text(secrets.token_urlsafe(50))
        KEY_PATH.chmod(0o600)

    # Seed the database from the copy shipped inside the app, but only the first
    # time. A later app update must never overwrite live rosters.
    if not DB_PATH.exists():
        seed = bundle_dir() / "db.sqlite3"
        if seed.exists():
            shutil.copy2(seed, DB_PATH)
        else:
            DB_PATH.touch()
    DB_PATH.chmod(0o600)

    if not ENV_PATH.exists():
        ENV_PATH.write_text(
            "# Spirit Scheduler settings. Edit with any text editor, then restart.\n"
            "# Paste your Square access token after the = sign to enable Square sync.\n"
            "SQUARE_PRODUCTION_ACCESS_TOKEN=\n"
            "SQUARE_LOCATION_ID=LR73BX986ZKYD\n"
            "SQUARE_ENVIRONMENT=production\n"
            "SQUARE_API_VERSION=2025-06-18\n"
            "SQUARE_REQUEST_TIMEOUT_SECONDS=30\n"
            "SQUARE_PRODUCTION_WRITES_ENABLED=false\n"
            "SQUARE_PRODUCTION_PILOT_VERIFIED=true\n"
            "SQUARE_PUBLISHING_ENABLED=false\n"
        )
        ENV_PATH.chmod(0o600)


def configure_django(port: int) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "spirit_scheduler.settings")
    os.environ["DJANGO_SECRET_KEY"] = KEY_PATH.read_text().strip()
    os.environ["DJANGO_DEBUG"] = "false"
    os.environ["DJANGO_HTTPS"] = "false"
    os.environ["DJANGO_ALLOWED_HOSTS"] = "127.0.0.1,localhost"
    os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"  # four slashes total: absolute
    os.environ.setdefault("SPIRIT_APP_PORT", str(port))

    # User-editable settings file wins for anything it defines.
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            os.environ[name.strip()] = value.strip()

    if getattr(sys, "frozen", False):
        os.environ["SPIRIT_STATIC_ROOT"] = str(bundle_dir() / "staticfiles")


def open_when_ready(url: str, port: int) -> None:
    for _ in range(80):
        if already_running(port):
            webbrowser.open(url)
            return
        time.sleep(0.25)


def main() -> int:
    prepare_data_dir()

    port = free_port()
    url = f"http://127.0.0.1:{port}/"

    # Second launch: just surface the window that already exists.
    if already_running(8765) and port != 8765:
        webbrowser.open("http://127.0.0.1:8765/")
        return 0

    configure_django(port)

    import django

    django.setup()

    from django.core.management import call_command

    call_command("migrate", interactive=False, verbosity=0)

    # Every install needs an account to sign in with, so the app is usable straight
    # after a double-click with no terminal step. Keyed to this specific username
    # rather than "are there any users at all": a seeded database may already carry
    # other accounts, and the documented manager login has to work regardless.
    # Existing accounts and passwords are never touched.
    from django.contrib.auth import get_user_model

    users = get_user_model()
    if not users.objects.filter(username="manager").exists():
        users.objects.create_superuser("manager", "", "spirit")

    threading.Thread(target=open_when_ready, args=(url, port), daemon=True).start()

    from django.core.management.commands.runserver import Command as RunServer

    RunServer().run_from_argv(
        ["spirit-scheduler", "runserver", f"127.0.0.1:{port}", "--noreload"]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
