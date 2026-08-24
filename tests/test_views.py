import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse


@pytest.mark.django_db
@pytest.mark.parametrize(
    "route_name",
    ("dashboard", "employees", "roles", "square_integration"),
)
def test_management_pages_require_login(client, route_name):
    response = client.get(reverse(route_name))
    assert response.status_code == 302
    assert reverse("login") in response.url


@pytest.mark.django_db
def test_authenticated_user_can_open_dashboard(client):
    user = get_user_model().objects.create_user(username="manager", password="safe-test-password")
    client.force_login(user)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    assert b"Spirit Scheduling Engine" in response.content


@pytest.mark.django_db
def test_square_page_is_safe_without_token(client, monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    monkeypatch.delenv("SQUARE_SANDBOX_ACCESS_TOKEN", raising=False)
    user = get_user_model().objects.create_user(username="manager", password="safe-test-password")
    client.force_login(user)
    response = client.get(reverse("square_integration"))
    assert response.status_code == 200
    assert b"Not Connected" in response.content
    assert b"No locations loaded" in response.content

