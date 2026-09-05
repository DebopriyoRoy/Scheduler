"""Recover a forgotten sign-in without opening a Terminal.

A password cannot be looked up - Django stores a one-way hash - so the only way back
in is to set a new one. That already existed as `manage.py changepassword`, which is
no help to someone who does not use a shell, and it left the only route to a locked-out
application outside the application.

The page is reachable while signed out, so it needs to prove the person is at this
Mac rather than merely able to reach the port. It does that with a one-time code
written to the application's own data folder: readable by this macOS account and
nobody else, which is the same boundary that protects the database sitting beside it.
Being on localhost is not the check - a bound port is reachable by anything on the
machine, and treating "local" as "trusted" is how these pages become the way in.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

CODE_FILENAME = "password-reset-code.txt"
CODE_LIFETIME = timedelta(minutes=15)


def data_dir() -> Path:
    """Where the application keeps its own files, beside the database."""
    import os

    configured = os.getenv("SPIRIT_DATA_DIR")
    if configured:
        return Path(configured)
    return Path.home() / "Library" / "Application Support" / "Spirit Scheduler"


def _code_path() -> Path:
    return data_dir() / CODE_FILENAME


def issue_code() -> tuple[str, Path]:
    """Write a fresh single-use code and return it with the file it went to."""
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    code = f"{secrets.randbelow(10**8):08d}"
    path = _code_path()
    path.write_text(f"{code}\n{timezone.now().isoformat()}\n")
    path.chmod(0o600)
    return code, path


def check_code(supplied: str) -> tuple[bool, str]:
    """Whether this code is the current one and still fresh.

    Compared in constant time. The window is short because the file lives on disk:
    a code left lying around for a week is a spare key, not a recovery step.
    """
    path = _code_path()
    if not path.exists():
        return False, "No reset code has been requested yet."
    try:
        lines = path.read_text().splitlines()
        code, issued_at = lines[0].strip(), timezone.datetime.fromisoformat(lines[1].strip())
    except (OSError, IndexError, ValueError):
        return False, "The reset code could not be read. Request a new one."

    if timezone.now() - issued_at > CODE_LIFETIME:
        return False, "That code has expired. Request a new one."
    if not secrets.compare_digest(code, (supplied or "").strip()):
        return False, "That code does not match the one on file."
    return True, ""


def clear_code() -> None:
    """Single use: the code dies with the reset it authorised."""
    _code_path().unlink(missing_ok=True)
