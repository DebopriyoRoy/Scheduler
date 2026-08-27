from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from integrations.square.exceptions import SquareConfigurationError
from integrations.square.services import DraftShiftRequest


@pytest.fixture
def draft_options():
    return {
        "team_member_id": "TEAM1",
        "job_id": "JOB1",
        "location_id": "LOC1",
        "start": "2026-09-10T17:00:00-02:30",
        "end": "2026-09-10T23:00:00-02:30",
    }


def test_idempotency_key_is_deterministic():
    request = DraftShiftRequest(
        team_member_id="TEAM1",
        job_id="JOB1",
        location_id="LOC1",
        start_at="2026-09-10T17:00:00-02:30",
        end_at="2026-09-10T23:00:00-02:30",
    )
    same_request = DraftShiftRequest(**request.__dict__)
    assert request.idempotency_key == same_request.idempotency_key
    assert request.idempotency_key.startswith("spirit-phase1-")


def test_invalid_shift_window_is_rejected():
    request = DraftShiftRequest(
        team_member_id="TEAM1",
        job_id="JOB1",
        location_id="LOC1",
        start_at="2026-09-10T23:00:00-02:30",
        end_at="2026-09-10T17:00:00-02:30",
    )
    with pytest.raises(SquareConfigurationError, match="end must be after"):
        request.validate()


def test_draft_command_defaults_to_dry_run(monkeypatch, draft_options):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    output = StringIO()
    with patch(
        "scheduling.management.commands.square_create_test_draft_shift.create_sandbox_draft_shift"
    ) as create_shift:
        call_command("square_create_test_draft_shift", stdout=output, **draft_options)
    assert "DRY RUN" in output.getvalue()
    assert "No Square changes made." in output.getvalue()
    create_shift.assert_not_called()


def test_confirmed_command_reports_draft_and_never_publishes(
    monkeypatch,
    draft_options,
):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("SQUARE_SANDBOX_ACCESS_TOKEN", "test-token")
    output = StringIO()
    response = {
        "id": "SHIFT1",
        "draft_shift_details": {
            "team_member_id": "TEAM1",
            "job_id": "JOB1",
            "location_id": "LOC1",
            "start_at": draft_options["start"],
            "end_at": draft_options["end"],
        },
    }
    with patch(
        "scheduling.management.commands.square_create_test_draft_shift.create_sandbox_draft_shift",
        return_value=response,
    ) as create_shift:
        call_command(
            "square_create_test_draft_shift",
            stdout=output,
            confirm=True,
            **draft_options,
        )
    assert create_shift.call_count == 1
    assert "Status: DRAFT" in output.getvalue()
    assert "Publication: NOT PUBLISHED" in output.getvalue()


def test_draft_command_rejects_production_even_without_confirm(monkeypatch, draft_options):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    with pytest.raises(CommandError, match="sandbox-only"):
        call_command("square_create_test_draft_shift", **draft_options)


def _key(**overrides):
    from scheduling.services.square_production_sync import shift_idempotency_key

    base = {
        "team_member_id": "TM1",
        "job_id": "JOB1",
        "location_id": "LOC1",
        "start_at": "2026-09-10T18:00:00-02:30",
        "end_at": "2026-09-10T22:45:00-02:30",
        "notes": "Spirit Scheduling Agent\nShow: Test\nAssignment: Confirmed",
    }
    base.update(overrides)
    return shift_idempotency_key(overrides.pop("assignment_id", 42), **base)


def test_idempotency_key_is_stable_for_an_identical_request():
    """A genuine retry of the same payload must not create a duplicate shift."""
    assert _key() == _key()


@pytest.mark.parametrize(
    "field,value",
    [
        ("notes", "Spirit Scheduling Agent\nShow: Test\nAssignment: On Call"),
        ("start_at", "2026-09-10T17:45:00-02:30"),
        ("end_at", "2026-09-10T23:00:00-02:30"),
        ("job_id", "JOB2"),
        ("team_member_id", "TM2"),
        ("location_id", "LOC2"),
    ],
)
def test_idempotency_key_changes_whenever_the_payload_changes(field, value):
    """Square replays the original response for a key it has already processed.

    A key that ignores the payload pins Square to the first version ever sent for an
    assignment: re-syncing after changing the shift notes then silently replays the
    stale create and reports success while nothing actually changed in Square.
    """
    assert _key(**{field: value}) != _key()
