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


def recipient_for(user) -> str:
    """Where this account's reset code should be sent, or "" if nowhere is known."""
    from django.conf import settings

    return (user.email or "").strip() or getattr(
        settings, "PASSWORD_RESET_FALLBACK_EMAIL", ""
    ).strip()


def mask(address: str) -> str:
    """A recognisable but non-disclosing form of an address.

    The reset page is reachable without signing in, so it must not hand a visitor a
    working address - but it does have to tell the person which inbox to open.
    """
    name, _, domain = address.partition("@")
    if not domain:
        return "the address on file"
    if len(name) <= 2:
        shown = name[:1] + "•"
    else:
        shown = f"{name[0]}{'•' * (len(name) - 2)}{name[-1]}"
    return f"{shown}@{domain}"


def email_code(user, code: str) -> tuple[bool, str]:
    """Send the code. Returns (sent, detail) - detail is safe to show a user.

    Never raises. A mail server that is down, misconfigured or refusing the password
    must not take the reset page with it: the caller falls back to writing the file,
    which is the whole reason that path still exists.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    if not getattr(settings, "EMAIL_IS_CONFIGURED", False):
        return False, "No mail account is configured."

    address = recipient_for(user)
    if not address:
        return False, f"No email address is on file for “{user.username}”."

    minutes = int(CODE_LIFETIME.total_seconds() // 60)
    try:
        send_mail(
            subject="Spirit Scheduling Agent - password reset code",
            message=(
                f"Your reset code is {code}\n\n"
                f"It works once and expires in {minutes} minutes.\n\n"
                f"Enter it on the reset page along with a new password for "
                f"“{user.username}”.\n\n"
                f"If you did not ask for this, someone with access to this Mac did. "
                f"The code alone cannot be used from anywhere else.\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[address],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 - any mail failure falls back to the file
        return False, f"Mail could not be sent ({type(exc).__name__}: {exc})."
    return True, mask(address)
