"""Adding a colleague, without a public sign-up form.

There is deliberately no registration page. The application binds to 127.0.0.1, but the
accounts it hands out reach real staff records and a live Square connection, so an open
form would let anyone who can load the page mint themselves a manager. Accounts are
created by someone already signed in.

The inviter never chooses the password. Handing someone one to "change later" means it
is written down, shared over something, and usually never changed.
"""

import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse


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
def manager(db, client):
    user = get_user_model().objects.create_user(
        username="manager", email="manager@example.com", password="a-long-enough-secret"
    )
    client.force_login(user)
    return user


def test_the_page_needs_a_sign_in(client, db):
    response = client.get(reverse("management_users"))
    assert response.status_code == 302
    assert reverse("login") in response.url


def test_there_is_no_public_sign_up_route():
    """The absence is the security model, so it is worth asserting."""
    from django.urls import NoReverseMatch

    for name in ("register", "signup", "sign_up", "create_account"):
        with pytest.raises(NoReverseMatch):
            reverse(name)


def test_an_invitation_creates_an_account_that_cannot_yet_sign_in(client, manager, mail_on):
    client.post(
        reverse("management_users"),
        {"action": "invite", "username": "deborah", "email": "deborah@example.com"},
    )
    invited = get_user_model().objects.get(username="deborah")
    assert not invited.has_usable_password()
    assert invited.is_active
    assert not invited.is_superuser


def test_the_invitation_link_lets_them_set_their_own_password(client, manager, mail_on):
    client.post(
        reverse("management_users"),
        {"action": "invite", "username": "deborah", "email": "deborah@example.com"},
    )
    link = re.search(r"https?://\S+/accounts/reset/\S+", mail.outbox[0].body).group(0)

    client.logout()
    chosen = "a-password-only-they-know"
    client.post(link, {"new_password": chosen, "confirm_password": chosen})

    invited = get_user_model().objects.get(username="deborah")
    assert invited.check_password(chosen)


def test_a_duplicate_username_is_refused(client, manager, mail_on):
    payload = {"action": "invite", "username": "manager", "email": "other@example.com"}
    response = client.post(reverse("management_users"), payload, follow=True)
    assert "already an account" in response.content.decode()
    assert mail.outbox == []


def test_a_duplicate_email_is_refused(client, manager, mail_on):
    payload = {"action": "invite", "username": "someone", "email": "manager@example.com"}
    response = client.post(reverse("management_users"), payload, follow=True)
    assert "already on another account" in response.content.decode()
    assert not get_user_model().objects.filter(username="someone").exists()


def test_without_mail_the_link_is_shown_to_the_inviter(client, manager, settings):
    """The account is still created; the link just has to be passed on by hand."""
    settings.EMAIL_IS_CONFIGURED = False
    response = client.post(
        reverse("management_users"),
        {"action": "invite", "username": "deborah", "email": "deborah@example.com"},
        follow=True,
    )
    assert get_user_model().objects.filter(username="deborah").exists()
    assert "/accounts/reset/" in response.content.decode()


def test_an_account_can_be_disabled(client, manager, mail_on):
    other = get_user_model().objects.create_user(username="t", password="x-long-enough-here")
    client.post(reverse("management_users"), {"action": "deactivate", "user_id": other.pk})
    other.refresh_from_db()
    assert not other.is_active


def test_you_cannot_disable_yourself(client, manager):
    """One careless click would otherwise lock the last person out of the application."""
    response = client.post(
        reverse("management_users"),
        {"action": "deactivate", "user_id": manager.pk},
        follow=True,
    )
    manager.refresh_from_db()
    assert manager.is_active
    assert "cannot deactivate the account you are signed in with" in response.content.decode()


def test_the_last_account_cannot_be_disabled(client, manager):
    other = get_user_model().objects.create_user(username="t", password="x-long-enough-here")
    client.force_login(other)
    manager.is_active = False
    manager.save()

    client.post(
        reverse("management_users"), {"action": "deactivate", "user_id": other.pk}, follow=True
    )
    other.refresh_from_db()
    assert other.is_active
