import pytest

from integrations.square.client import SquareClient, redact_secrets
from integrations.square.config import SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareAPIError, SquareProductionWriteBlocked
from tests.fakes import FakeResponse, FakeSession


@pytest.fixture
def sandbox_config():
    return SquareConfig(
        environment=SquareEnvironment.SANDBOX,
        sandbox_access_token="sandbox-test-token",
        api_version="2026-08-19",
    )


def test_location_retrieval_parsing(sandbox_config):
    session = FakeSession(
        [
            FakeResponse(
                {"locations": [{"id": "LOC1", "name": "Spirit Theatre", "status": "ACTIVE"}]}
            )
        ]
    )
    locations = SquareClient(sandbox_config, session=session).list_locations()
    assert locations == [{"id": "LOC1", "name": "Spirit Theatre", "status": "ACTIVE"}]
    assert session.calls[0]["url"] == "https://connect.squareupsandbox.com/v2/locations"
    assert session.calls[0]["headers"]["Square-Version"] == "2026-08-19"


def test_team_member_parsing_and_pagination(sandbox_config):
    session = FakeSession(
        [
            FakeResponse({"team_members": [{"id": "T1"}], "cursor": "next-page"}),
            FakeResponse({"team_members": [{"id": "T2"}]}),
        ]
    )
    members = SquareClient(sandbox_config, session=session).search_team_members()
    assert [member["id"] for member in members] == ["T1", "T2"]
    assert session.calls[0]["json"]["query"]["filter"]["status"] == "ACTIVE"
    assert session.calls[1]["json"]["cursor"] == "next-page"


def test_job_parsing_and_pagination(sandbox_config):
    session = FakeSession(
        [
            FakeResponse({"jobs": [{"id": "J1", "title": "Server"}], "cursor": "jobs-2"}),
            FakeResponse({"jobs": [{"id": "J2", "title": "Bartender"}]}),
        ]
    )
    jobs = SquareClient(sandbox_config, session=session).list_jobs()
    assert [job["title"] for job in jobs] == ["Server", "Bartender"]
    assert session.calls[1]["params"] == {"cursor": "jobs-2"}


def test_scheduled_shift_parsing(sandbox_config):
    session = FakeSession([FakeResponse({"scheduled_shifts": [{"id": "SHIFT1"}]})])
    shifts = SquareClient(sandbox_config, session=session).search_scheduled_shifts(
        {"filter": {"location_ids": ["LOC1"]}}
    )
    assert shifts == [{"id": "SHIFT1"}]
    assert session.calls[0]["json"]["query"]["filter"]["location_ids"] == ["LOC1"]


def test_secret_redaction_in_api_error(sandbox_config):
    secret = sandbox_config.sandbox_access_token
    session = FakeSession(
        [
            FakeResponse(
                {"errors": [{"code": "UNAUTHORIZED", "detail": f"Rejected Bearer {secret}"}]},
                status_code=401,
            )
        ]
    )
    with pytest.raises(SquareAPIError) as caught:
        SquareClient(sandbox_config, session=session).list_locations()
    assert secret not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_redact_secrets_handles_bearer_headers():
    result = redact_secrets("Authorization: Bearer private-token", ("private-token",))
    assert "private-token" not in result
    assert "[REDACTED]" in result


def test_client_blocks_production_draft_creation_before_request():
    config = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        production_access_token="production-secret",
    )
    session = FakeSession([])
    with pytest.raises(SquareProductionWriteBlocked):
        SquareClient(config, session=session).create_draft_shift(
            idempotency_key="key",
            team_member_id="T1",
            job_id="J1",
            location_id="L1",
            start_at="2026-09-10T17:00:00-02:30",
            end_at="2026-09-10T23:00:00-02:30",
        )
    assert session.calls == []
