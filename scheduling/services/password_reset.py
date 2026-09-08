"""Recover a forgotten sign-in without opening a Terminal.

A password cannot be looked up - Django stores a one-way hash - so the only way back
in is to set a new one. That existed only as `manage.py changepassword`, which is no
use to someone who does not use a shell.

The link carries a signed token from Django's own generator rather than anything
hand-rolled: it is tied to the account's current password hash and last-login time, so
it stops working the moment the password changes or the link is used, and it expires on
its own. Nothing about the code is stored, which is the point - there is no secret
sitting on disk to leak.

When no mail account is configured the link is written to a file in the application's
data folder instead, readable by this macOS account alone. A machine with no mail set
up must not be a machine nobody can get back into.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

LINK_FILENAME = "password-reset-link.txt"


def data_dir() -> Path:
    """Where the application keeps its own files, beside the database."""
    configured = os.getenv("SPIRIT_DATA_DIR")
    if configured:
        return Path(configured)
    return Path.home() / "Library" / "Application Support" / "Spirit Scheduler"


def find_user(identifier: str):
    """Match on username or the email the account was registered with.

    People remember one or the other, rarely which. Email is matched
    case-insensitively because nobody types their own address consistently.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    users = get_user_model().objects
    return (
        users.filter(username__iexact=identifier, is_active=True).first()
        or users.filter(email__iexact=identifier, is_active=True).first()
    )


def build_link(user, request) -> str:
    token = default_token_generator.make_token(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    path = reverse("password_reset_confirm", kwargs={"uidb64": uid, "token": token})
    return request.build_absolute_uri(path)


def mask(address: str) -> str:
    """Recognisable but not usable.

    The page is reachable without signing in, so it has to say which inbox to open
    without handing a visitor a working address.
    """
    name, _, domain = address.partition("@")
    if not domain:
        return "the address on file"
    shown = name[:1] + "•" if len(name) <= 2 else f"{name[0]}{'•' * (len(name) - 2)}{name[-1]}"
    return f"{shown}@{domain}"


def write_link_to_disk(link: str) -> Path:
    """The fallback when there is no mail account. Returns the file written."""
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LINK_FILENAME
    path.write_text(f"{link}\n")
    path.chmod(0o600)
    return path


def email_link(user, link: str) -> tuple[bool, str]:
    """Send the link. Returns (sent, detail); detail is safe to show a user.

    Never raises. A mail server that is down, misconfigured or refusing the password
    must not take the reset page with it - the caller falls back to the file.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    if not getattr(settings, "EMAIL_IS_CONFIGURED", False):
        return False, "No mail account is configured."
    address = (user.email or "").strip()
    if not address:
        return False, f"No email address is on file for “{user.username}”."

    try:
        send_mail(
            subject="Reset your Spirit Scheduling Agent password",
            message=(
                f"Open this link to set a new password for “{user.username}”:\n\n"
                f"{link}\n\n"
                f"It can be used once and stops working as soon as the password "
                f"changes.\n\n"
                f"If you did not ask for this, you can ignore it - the link only works "
                f"on the Mac the application runs on.\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[address],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the file
        return False, f"Mail could not be sent ({type(exc).__name__}: {exc})."
    return True, mask(address)


def email_invitation(user, link: str, invited_by: str) -> tuple[bool, str]:
    """Tell a new colleague their account exists and let them choose the password.

    The inviter never sets it. Handing someone a password to "change later" means it
    is written down somewhere, shared over something, and usually never changed - the
    link lets them pick their own and means nobody else ever knew it.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    if not getattr(settings, "EMAIL_IS_CONFIGURED", False):
        return False, "No mail account is configured."
    address = (user.email or "").strip()
    if not address:
        return False, f"No email address was given for “{user.username}”."

    try:
        send_mail(
            subject="Your Spirit Scheduling Agent account",
            message=(
                f"{invited_by} has set up an account for you on the Spirit Scheduling "
                f"Agent.\n\n"
                f"Your username is “{user.username}”. Open this link to choose a "
                f"password:\n\n{link}\n\n"
                f"The link can be used once. If it has expired by the time you get to "
                f"it, use “Forgotten your password?” on the sign-in page.\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[address],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the link on screen
        return False, f"Mail could not be sent ({type(exc).__name__}: {exc})."
    return True, mask(address)
