"""Getting back into a locked-out application, without a Terminal.

A password cannot be looked up - Django stores a one-way hash - so the only way back
in is to set a new one. That existed only as `manage.py changepassword`, which is no
use to someone who does not use a shell, and it put the sole route into a locked-out
application outside the application.

The page is reachable while signed out, so what stands in for the old password is a
one-time code written to the app's data folder, readable by this macOS account alone.
Being on localhost is deliberately *not* the check: a bound port is reachable by
anything on the machine.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from scheduling.services import password_reset as service


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never write a reset code into the real application folder."""
    monkeypatch.setenv("SPIRIT_DATA_DIR", str(tmp_path))


@pytest.fixture
def manager(db):
    return get_user_model().objects.create_user(
        username="manager", password="the-old-one-nobody-remembers"
    )


def test_the_page_is_reachable_without_signing_in(client, manager):
    """The whole point: the person cannot sign in."""
    response = client.get(reverse("password_reset"))
    assert response.status_code == 200
    assert "Reset password" in response.content.decode()


def test_the_sign_in_page_links_to_it(client):
    html = client.get(reverse("login")).content.decode()
    assert reverse("password_reset") in html


def test_a_code_is_written_where_the_page_says(client, manager, tmp_path):
    client.post(reverse("password_reset"), {"action": "issue"}, follow=True)
    written = tmp_path / service.CODE_FILENAME
    assert written.exists()
    assert written.read_text().splitlines()[0].strip().isdigit()


def test_the_right_code_sets_the_password(client, manager, tmp_path):
    code, _ = service.issue_code()
    response = client.post(
        reverse("password_reset"),
        {
            "username": "manager", "code": code,
            "new_password": "a-properly-long-new-secret",
            "confirm_password": "a-properly-long-new-secret",
        },
        follow=True,
    )
    assert response.status_code == 200
    manager.refresh_from_db()
    assert manager.check_password("a-properly-long-new-secret")


def test_a_used_code_cannot_be_used_again(client, manager):
    """Single use, or it is a spare key rather than a recovery step."""
    code, _ = service.issue_code()
    payload = {
        "username": "manager", "code": code,
        "new_password": "a-properly-long-new-secret",
        "confirm_password": "a-properly-long-new-secret",
    }
    client.post(reverse("password_reset"), payload, follow=True)

    payload["new_password"] = payload["confirm_password"] = "another-long-secret-entirely"
    client.post(reverse("password_reset"), payload, follow=True)

    manager.refresh_from_db()
    assert manager.check_password("a-properly-long-new-secret")
    assert not manager.check_password("another-long-secret-entirely")


def test_a_wrong_code_changes_nothing(client, manager):
    service.issue_code()
    client.post(
        reverse("password_reset"),
        {
            "username": "manager", "code": "00000000",
            "new_password": "a-properly-long-new-secret",
            "confirm_password": "a-properly-long-new-secret",
        },
        follow=True,
    )
    manager.refresh_from_db()
    assert manager.check_password("the-old-one-nobody-remembers")


def test_an_expired_code_changes_nothing(client, manager, tmp_path, monkeypatch):
    code, path = service.issue_code()
    stale = (timezone.now() - service.CODE_LIFETIME - timedelta(minutes=1)).isoformat()
    path.write_text(f"{code}\n{stale}\n")

    client.post(
        reverse("password_reset"),
        {
            "username": "manager", "code": code,
            "new_password": "a-properly-long-new-secret",
            "confirm_password": "a-properly-long-new-secret",
        },
        follow=True,
    )
    manager.refresh_from_db()
    assert manager.check_password("the-old-one-nobody-remembers")


def test_mismatched_passwords_change_nothing(client, manager):
    code, _ = service.issue_code()
    client.post(
        reverse("password_reset"),
        {
            "username": "manager", "code": code,
            "new_password": "a-properly-long-new-secret",
            "confirm_password": "something-else-entirely-here",
        },
        follow=True,
    )
    manager.refresh_from_db()
    assert manager.check_password("the-old-one-nobody-remembers")


def test_a_weak_password_is_refused(client, manager):
    """Django's own validators apply, the same as they would in the shell."""
    code, _ = service.issue_code()
    response = client.post(
        reverse("password_reset"),
        {"username": "manager", "code": code, "new_password": "12345", "confirm_password": "12345"},
        follow=True,
    )
    manager.refresh_from_db()
    assert manager.check_password("the-old-one-nobody-remembers")
    assert response.status_code == 200


def test_an_unknown_username_changes_nothing(client, manager):
    code, _ = service.issue_code()
    client.post(
        reverse("password_reset"),
        {
            "username": "nobody", "code": code,
            "new_password": "a-properly-long-new-secret",
            "confirm_password": "a-properly-long-new-secret",
        },
        follow=True,
    )
    manager.refresh_from_db()
    assert manager.check_password("the-old-one-nobody-remembers")


# --- delivery by email -----------------------------------------------------------

@pytest.fixture
def mail_configured(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EMAIL_IS_CONFIGURED = True
    settings.DEFAULT_FROM_EMAIL = "debopriyo.inbox@gmail.com"
    settings.PASSWORD_RESET_FALLBACK_EMAIL = ""
    return settings


def test_the_code_is_emailed_when_mail_is_configured(client, manager, mail_configured):
    from django.core import mail

    manager.email = "someone@example.com"
    manager.save()

    client.post(reverse("password_reset"), {"action": "issue", "username": "manager"}, follow=True)

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["someone@example.com"]
    assert "reset code" in sent.subject.lower()


def test_the_emailed_code_actually_works(client, manager, mail_configured):
    import re

    from django.core import mail

    manager.email = "someone@example.com"
    manager.save()
    client.post(reverse("password_reset"), {"action": "issue", "username": "manager"}, follow=True)
    code = re.search(r"\b(\d{8})\b", mail.outbox[0].body).group(1)

    client.post(
        reverse("password_reset"),
        {
            "username": "manager", "code": code,
            "new_password": "a-properly-long-new-secret",
            "confirm_password": "a-properly-long-new-secret",
        },
        follow=True,
    )
    manager.refresh_from_db()
    assert manager.check_password("a-properly-long-new-secret")


def test_the_stored_code_survives_being_emailed(client, manager, mail_configured, tmp_path):
    """The file is the server's record of what was issued, not a spare key.

    Deleting it on a successful send felt tidy and made every emailed code impossible
    to verify - the reset silently did nothing. It is protected by 0600 and the macOS
    account, expires in fifteen minutes, and dies with the reset it authorises.
    """
    manager.email = "someone@example.com"
    manager.save()
    client.post(reverse("password_reset"), {"action": "issue", "username": "manager"}, follow=True)

    stored = tmp_path / service.CODE_FILENAME
    assert stored.exists()
    assert oct(stored.stat().st_mode)[-3:] == "600"


def test_the_address_is_masked_on_the_page(client, manager, mail_configured):
    """The page is reachable without signing in and must not hand out an address."""
    manager.email = "someone@example.com"
    manager.save()
    response = client.post(
        reverse("password_reset"), {"action": "issue", "username": "manager"}, follow=True
    )
    body = response.content.decode()
    assert "someone@example.com" not in body
    assert "s•••••e@example.com" in body


def test_a_mail_failure_falls_back_to_the_file(
    client, manager, mail_configured, tmp_path, monkeypatch
):
    """A mail server that is down must not take the reset page down with it."""
    manager.email = "someone@example.com"
    manager.save()

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("django.core.mail.send_mail", refuse)

    client.post(reverse("password_reset"), {"action": "issue", "username": "manager"}, follow=True)

    assert (tmp_path / service.CODE_FILENAME).exists()


def test_an_account_with_no_address_falls_back_to_the_file(
    client, manager, mail_configured, tmp_path
):
    manager.email = ""
    manager.save()
    response = client.post(
        reverse("password_reset"), {"action": "issue", "username": "manager"}, follow=True
    )
    assert (tmp_path / service.CODE_FILENAME).exists()
    assert "No email address is on file" in response.content.decode()


def test_masking_keeps_the_domain_and_hides_the_name():
    assert service.mask("debopriyo.inbox@gmail.com") == "d•••••••••••••x@gmail.com"
    assert service.mask("ab@example.com") == "a•@example.com"
    assert service.mask("not-an-address") == "the address on file"
