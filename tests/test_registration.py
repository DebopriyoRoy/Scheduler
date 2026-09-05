"""Self-registration from the sign-in page.

The link is public, and this application reaches real staff records and a live Square
connection while being reachable by anything on the machine. So a new account is held
inactive until a manager approves it - unless REGISTRATION_REQUIRES_APPROVAL is turned
off, which is one setting for whoever wants people in immediately.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

GOOD = "a-long-enough-chosen-secret"


@pytest.fixture
def manager(db, client):
    user = get_user_model().objects.create_user(
        username="manager", email="manager@example.com", password=GOOD
    )
    return user


def _signup(client, **overrides):
    payload = {
        "username": "deborah", "email": "deborah@example.com",
        "password": GOOD, "confirm_password": GOOD,
    }
    payload.update(overrides)
    return client.post(reverse("register"), payload, follow=True)


def test_the_sign_in_page_offers_registration(client, db):
    assert reverse("register") in client.get(reverse("login")).content.decode()


def test_the_page_is_reachable_without_signing_in(client, db):
    assert client.get(reverse("register")).status_code == 200


def test_a_new_account_waits_for_approval_by_default(client, db):
    _signup(client)
    person = get_user_model().objects.get(username="deborah")
    assert not person.is_active
    assert person.check_password(GOOD)


def test_an_unapproved_account_cannot_sign_in(client, db):
    """The whole reason for holding it: registering must not itself grant access."""
    _signup(client)
    assert not client.login(username="deborah", password=GOOD)


def test_a_manager_can_approve_it(client, db, manager):
    _signup(client)
    person = get_user_model().objects.get(username="deborah")

    client.force_login(manager)
    client.post(reverse("management_users"), {"action": "approve", "user_id": person.pk})

    person.refresh_from_db()
    assert person.is_active
    client.logout()
    assert client.login(username="deborah", password=GOOD)


def test_a_pending_request_can_be_removed(client, db, manager):
    _signup(client)
    person = get_user_model().objects.get(username="deborah")

    client.force_login(manager)
    client.post(reverse("management_users"), {"action": "reject", "user_id": person.pk})

    assert not get_user_model().objects.filter(username="deborah").exists()


def test_an_account_that_has_signed_in_is_never_deleted(client, db, manager):
    """Reject removes a request. Anything with history gets disabled instead."""
    from django.utils import timezone

    used = get_user_model().objects.create_user(
        username="old", password=GOOD, is_active=False
    )
    used.last_login = timezone.now()
    used.save()

    client.force_login(manager)
    response = client.post(
        reverse("management_users"), {"action": "reject", "user_id": used.pk}, follow=True
    )
    assert get_user_model().objects.filter(username="old").exists()
    assert "can be disabled but not deleted" in response.content.decode()


def test_pending_accounts_are_listed_for_a_manager(client, db, manager):
    _signup(client)
    client.force_login(manager)
    body = client.get(reverse("management_users")).content.decode()
    assert "Waiting for approval" in body
    assert "deborah" in body


def test_registration_can_be_opened_up(client, db, settings):
    """One setting, for whoever wants people in the moment they register."""
    settings.REGISTRATION_REQUIRES_APPROVAL = False
    _signup(client)
    assert get_user_model().objects.get(username="deborah").is_active
    assert client.login(username="deborah", password=GOOD)


def test_a_duplicate_username_is_refused(client, db, manager):
    response = _signup(client, username="manager", email="other@example.com")
    assert "already an account" in response.content.decode()


def test_a_duplicate_email_is_refused(client, db, manager):
    response = _signup(client, username="someone", email="manager@example.com")
    assert "already on another account" in response.content.decode()
    assert not get_user_model().objects.filter(username="someone").exists()


def test_mismatched_passwords_create_nothing(client, db):
    response = _signup(client, confirm_password="something-else-entirely")
    assert "do not match" in response.content.decode()
    assert not get_user_model().objects.filter(username="deborah").exists()


def test_a_weak_password_is_refused(client, db):
    _signup(client, password="12345", confirm_password="12345")
    assert not get_user_model().objects.filter(username="deborah").exists()
