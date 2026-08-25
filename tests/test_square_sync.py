from datetime import date
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from scheduling.models import (
    Employee,
    Role,
    ScheduleRun,
    ScheduleRunStatus,
    SquareLocation,
)
from scheduling.services.engine import SchedulingEngine
from scheduling.services.square_sync import (
    sync_schedule_to_sandbox,
    validate_schedule_for_sync,
)
from scheduling.services.workflow import approve_schedule

User = get_user_model()


@pytest.mark.django_db
def test_sync_validation_fails_on_unapproved_schedule(monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("SQUARE_SANDBOX_ACCESS_TOKEN", "test-token")
    run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12),
        end_date=date(2026, 9, 12),
        status=ScheduleRunStatus.DRAFT,
    )
    result = validate_schedule_for_sync(run)
    assert not result.is_valid
    assert any("Only approved schedule versions can be synced" in err for err in result.errors)


@pytest.mark.django_db
def test_sync_validation_flags_unmapped_employees_and_roles(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("SQUARE_SANDBOX_ACCESS_TOKEN", "test-token")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    engine = SchedulingEngine()
    run = engine.generate(
        date(2026, 9, 12),
        date(2026, 9, 12),
        allow_shortages=True,
    )
    approve_schedule(run, user)

    # By default, seed staff do not have Square IDs
    result = validate_schedule_for_sync(run)
    assert not result.is_valid
    assert len(result.unmapped_employees) > 0
    assert len(result.unmapped_roles) > 0
    assert any("employees lack Square team member IDs" in err for err in result.errors)
    assert any("roles lack Square job IDs" in err for err in result.errors)


@pytest.mark.django_db
def test_sync_validation_and_publishing_succeeds_when_mapped(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("SQUARE_SANDBOX_ACCESS_TOKEN", "test-token")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    SquareLocation.objects.create(
        name="Main Stage",
        square_location_id="LOC_SANDBOX_1",
        active=True,
    )

    # Map all employees & roles to dummy Square IDs
    for idx, emp in enumerate(Employee.objects.all()):
        emp.square_team_member_id = f"SQ_TEAM_{idx+1}"
        emp.save()

    for idx, role in enumerate(Role.objects.all()):
        role.square_job_id = f"SQ_JOB_{idx+1}"
        role.save()

    engine = SchedulingEngine()
    run = engine.generate(
        date(2026, 9, 12),
        date(2026, 9, 12),
        allow_shortages=True,
    )
    approve_schedule(run, user)

    mock_client = MagicMock(spec=SquareClient)
    mock_client.config = SquareConfig(
        environment=SquareEnvironment.SANDBOX,
        sandbox_access_token="test-token",
        location_id="LOC_SANDBOX_1",
    )
    mock_client.search_scheduled_shifts.return_value = []
    mock_client.create_draft_shift.return_value = {
        "id": "SHIFT_123",
        "status": "DRAFT",
    }

    # Validate
    validation = validate_schedule_for_sync(run, client=mock_client)
    assert validation.is_valid
    assert len(validation.errors) == 0
    assert validation.location_id == "LOC_SANDBOX_1"
    assert len(validation.assignments_payload) > 0

    # Sync
    result = sync_schedule_to_sandbox(run, client=mock_client)
    assert result["synced_count"] == len(validation.assignments_payload)
    assert mock_client.create_draft_shift.call_count == len(validation.assignments_payload)

    # Verify schedule run status updated
    run.refresh_from_db()
    assert run.status == ScheduleRunStatus.SYNCED_TO_SQUARE
    assert "Synced" in run.notes


@pytest.mark.django_db
def test_sync_command_dry_run_and_confirm(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("SQUARE_SANDBOX_ACCESS_TOKEN", "test-token")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    # Map staff & roles
    for idx, emp in enumerate(Employee.objects.all()):
        emp.square_team_member_id = f"SQ_TEAM_{idx+1}"
        emp.save()
    for idx, role in enumerate(Role.objects.all()):
        role.square_job_id = f"SQ_JOB_{idx+1}"
        role.save()

    run = SchedulingEngine().generate(
        date(2026, 9, 12),
        date(2026, 9, 12),
        allow_shortages=True,
    )
    approve_schedule(run, user)

    output = StringIO()
    with patch("scheduling.services.square_sync.SquareClient") as MockClientClass:
        mock_client = MockClientClass.return_value
        mock_client.config = SquareConfig(
            environment=SquareEnvironment.SANDBOX,
            sandbox_access_token="test-token",
            location_id="LOC1",
        )
        mock_client.search_scheduled_shifts.return_value = []
        mock_client.create_draft_shift.return_value = {"id": "DRAFT1"}

        # Dry run
        call_command("sync_schedule_to_square_sandbox", schedule_id=run.id, stdout=output)
        assert "DRY RUN ONLY" in output.getvalue()

        # Confirmed execution
        output_confirm = StringIO()
        call_command(
            "sync_schedule_to_square_sandbox",
            schedule_id=run.id,
            confirm=True,
            stdout=output_confirm,
        )
        assert "SUCCESS: Synced" in output_confirm.getvalue()


@pytest.mark.django_db
def test_sync_refuses_production_environment(monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12),
        end_date=date(2026, 9, 12),
        status=ScheduleRunStatus.APPROVED,
    )
    result = validate_schedule_for_sync(run)
    assert not result.is_valid
    assert any("sandbox-only" in err.lower() or "disabled" in err.lower() for err in result.errors)
