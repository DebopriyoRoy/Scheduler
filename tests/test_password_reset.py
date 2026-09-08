"""Getting back into a locked-out application, without a Terminal.

A password cannot be looked up - Django stores a one-way hash - so the only way back
in is to set a new one. That existed only as `manage.py changepassword`, which is no
use to someone who does not use a shell.

The link carries Django's own signed token: tied to the account's current password
hash and last login, so it stops working the moment the password changes, and it
expires on its own. Nothing is stored, so there is no secret on disk to leak.
"""

import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse

from scheduling.services import password_reset as service

NEW = "a-long-enough-new-secret"
NEW_AGAIN = {"new_password": NEW, "confirm_password": NEW}


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIRIT_DATA_DIR", str(tmp_path))


@pytest.fixture
def mail_on(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EMAIL_IS_CONFIGURED = True
    settings.DEFAULT_FROM_EMAIL = "spirit@example.com"
    return settings


@pytest.fixture
def manager(db):
    return get_user_model().objects.create_user(
        username="manager", email="someone@example.com", password="the-old-one"
    )


def _link_from_outbox() -> str:
    return re.search(r"https?://\S+/accounts/reset/\S+", mail.outbox[0].body).group(0)


# --- asking for the link ----------------------------------------------------------

def test_the_page_is_reachable_without_signing_in(client, db):
    assert client.get(reverse("password_reset")).status_code == 200


def test_the_sign_in_page_links_to_it(client, db):
    assert reverse("password_reset") in client.get(reverse("login")).content.decode()


def test_the_username_field_accepts_either(client, manager, mail_on):
    """People remember one or the other, rarely which."""
    for identifier in ("manager", "someone@example.com"):
        mail.outbox.clear()
        client.post(reverse("password_reset"), {"identifier": identifier})
        assert len(mail.outbox) == 1, f"no link sent for {identifier}"


def test_the_email_is_matched_regardless_of_case(client, manager, mail_on):
    client.post(reverse("password_reset"), {"identifier": "SOMEONE@Example.COM"})
    assert len(mail.outbox) == 1


def test_a_typo_is_told_it_is_a_typo(client, manager, mail_on):
    """This app is bound to 127.0.0.1 on one Mac, so enumeration is not the threat.

    Anyone who can load this page can already read the database beside it. Telling a
    person their username is wrong is worth more here than hiding it from an attacker
    who is by definition already inside.
    """
    response = client.post(reverse("password_reset"), {"identifier": "nobdy"}, follow=True)
    assert "No active account matches" in response.content.decode()
    assert mail.outbox == []


def test_the_recipient_is_masked_on_screen(client, manager, mail_on):
    body = client.post(
        reverse("password_reset"), {"identifier": "manager"}, follow=True
    ).content.decode()
    assert "someone@example.com" not in body
    assert "s•••••e@example.com" in body


# --- the link itself --------------------------------------------------------------

def test_the_link_sets_the_password(client, manager, mail_on):
    client.post(reverse("password_reset"), {"identifier": "manager"})
    link = _link_from_outbox()

    assert client.get(link).status_code == 200
    client.post(link, NEW_AGAIN)

    manager.refresh_from_db()
    assert manager.check_password(NEW)


def test_a_link_cannot_be_used_twice(client, manager, mail_on):
    """Django's token is tied to the password hash, so changing it burns the link."""
    client.post(reverse("password_reset"), {"identifier": "manager"})
    link = _link_from_outbox()
    client.post(link, NEW_AGAIN)

    other = "another-entirely-new-one"
    second = {"new_password": other, "confirm_password": other}
    assert client.post(link, second).status_code == 400

    manager.refresh_from_db()
    assert manager.check_password(NEW)


def test_a_tampered_link_is_refused(client, manager, mail_on):
    client.post(reverse("password_reset"), {"identifier": "manager"})
    link = _link_from_outbox()
    assert client.get(link[:-4] + "aaa/").status_code == 400


def test_an_expired_link_is_refused(client, manager, mail_on, settings, monkeypatch):
    """Issued now, opened after the window has passed."""
    from datetime import datetime, timedelta

    from django.contrib.auth.tokens import PasswordResetTokenGenerator

    client.post(reverse("password_reset"), {"identifier": "manager"})
    link = _link_from_outbox()

    # _now() is naive by Django's own convention here, so match it.
    later = datetime.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 60)
    monkeypatch.setattr(PasswordResetTokenGenerator, "_now", lambda self: later)

    assert client.get(link).status_code == 400


def test_mismatched_passwords_change_nothing(client, manager, mail_on):
    client.post(reverse("password_reset"), {"identifier": "manager"})
    link = _link_from_outbox()
    client.post(link, {"new_password": NEW, "confirm_password": "different-one"})
    manager.refresh_from_db()
    assert manager.check_password("the-old-one")


def test_a_weak_password_is_refused(client, manager, mail_on):
    """Django's own validators apply, exactly as they would in the shell."""
    client.post(reverse("password_reset"), {"identifier": "manager"})
    link = _link_from_outbox()
    client.post(link, {"new_password": "12345", "confirm_password": "12345"})
    manager.refresh_from_db()
    assert manager.check_password("the-old-one")


# --- when there is no mail account -------------------------------------------------

def test_without_mail_the_link_is_written_to_disk(client, manager, tmp_path, settings):
    settings.EMAIL_IS_CONFIGURED = False
    response = client.post(reverse("password_reset"), {"identifier": "manager"}, follow=True)

    written = tmp_path / service.LINK_FILENAME
    assert written.exists()
    assert "/accounts/reset/" in written.read_text()
    assert oct(written.stat().st_mode)[-3:] == "600"
    assert "saved to" in response.content.decode().lower()


def test_a_mail_failure_falls_back_to_disk(client, manager, mail_on, tmp_path, monkeypatch):
    """A mail server that is down must not take the reset page with it."""
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("django.core.mail.send_mail", refuse)
    client.post(reverse("password_reset"), {"identifier": "manager"}, follow=True)
    assert (tmp_path / service.LINK_FILENAME).exists()


def test_masking_keeps_the_domain_and_hides_the_name():
    assert service.mask("debopriyo.inbox@gmail.com") == "d•••••••••••••x@gmail.com"
    assert service.mask("ab@example.com") == "a•@example.com"
    assert service.mask("not-an-address") == "the address on file"
