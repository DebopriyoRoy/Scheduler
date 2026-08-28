from datetime import date
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import (
    SquarePilotNotVerifiedError,
    SquareProductionWritesDisabledError,
    SquarePublishingDisabledError,
)
from scheduling.models import (
    Employee,
    MappingStatus,
    Role,
    SquareEmployeeMapping,
    SquareLocationMapping,
    SquareRoleMapping,
    SquareSyncAuditAction,
    SquareSyncAuditLog,
)
from scheduling.services.engine import SchedulingEngine
from scheduling.services.square_production_sync import (
    SquareSyncValidationError,
    create_production_pilot_shift,
    mark_pilot_verified,
    preview_production_sync,
    sync_full_production_schedule,
    sync_production_jobs,
    sync_production_team_members,
)
from scheduling.services.square_production_sync import (
    test_production_connection as run_test_production_connection,
)
from scheduling.services.workflow import approve_schedule

User = get_user_model()


@pytest.mark.django_db
def test_production_connection_is_read_only(monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")

    mock_client = MagicMock(spec=SquareClient)
    mock_client.test_connection.return_value = [
        {"id": "LOC_PROD_1", "name": "Spirit Theatre", "status": "ACTIVE"}
    ]

    res = run_test_production_connection(client=mock_client)
    assert len(res) == 1
    assert res[0]["id"] == "LOC_PROD_1"


@pytest.mark.django_db
def test_production_team_mapping_exact_normalized_matching(monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    call_command("seed_spirit_staff")

    team_members = [
        {"id": "SQ_TM_Joleen", "given_name": "Joleen", "family_name": "Dickson"},
        {"id": "SQ_TM_Jackie", "given_name": "Jackie", "family_name": "Pynn"},
        {"id": "SQ_TM_Olena", "given_name": "Olena", "family_name": ""},
        {"id": "SQ_TM_Yana", "given_name": "Yana", "family_name": ""},
    ]
    mock_client = MagicMock(spec=SquareClient)
    mock_client.search_team_members.return_value = team_members

    res = sync_production_team_members(client=mock_client)
    assert (res["mapped_exact"] + res["manual_review_required"]) >= 4

    joleen = Employee.objects.get(display_name="Joleen Dickson")
    mapping = SquareEmployeeMapping.objects.get(employee=joleen, environment="production")
    assert mapping.status == MappingStatus.MAPPED_EXACT
    assert mapping.square_team_member_id == "SQ_TM_Joleen"



@pytest.mark.django_db
def test_production_job_mapping(monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    call_command("seed_spirit_staff")

    jobs = [
        {"id": "JOB_SERVER", "title": "Server"},
        {"id": "JOB_BARTENDER", "title": "Bartender"},
        {"id": "JOB_BUSSER", "title": "Busser"},
        {"id": "JOB_FIFTY", "title": "50/50"},
    ]
    mock_client = MagicMock(spec=SquareClient)
    mock_client.list_jobs.return_value = jobs

    res = sync_production_jobs(client=mock_client)
    assert res["mapped"] >= 4

    server_role = Role.objects.get(name="Server")
    mapping = SquareRoleMapping.objects.get(role=server_role, environment="production")
    assert mapping.status == MappingStatus.MAPPED
    assert mapping.square_job_id == "JOB_SERVER"


@pytest.mark.django_db
def test_read_only_preview_classifies_assignments(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    monkeypatch.setenv("SQUARE_LOCATION_ID", "LOC_PROD_1")
    monkeypatch.setenv("SQUARE_PRODUCTION_WRITES_ENABLED", "false")

    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    SquareLocationMapping.objects.create(
        environment="production",
        square_location_id="LOC_PROD_1",
        location_name="Spirit Theatre",
        active=True,
    )

    for idx, emp in enumerate(Employee.objects.all()):
        SquareEmployeeMapping.objects.create(
            employee=emp,
            environment="production",
            square_team_member_id=f"SQ_PROD_TM_{idx+1}",
            status=MappingStatus.MAPPED,
        )
    for idx, role in enumerate(Role.objects.all()):
        SquareRoleMapping.objects.create(
            role=role,
            environment="production",
            square_job_id=f"SQ_PROD_JOB_{idx+1}",
            status=MappingStatus.MAPPED,
        )

    run = SchedulingEngine().generate(date(2026, 9, 12), date(2026, 9, 12), allow_shortages=True)
    approve_schedule(run, user)

    mock_client = MagicMock(spec=SquareClient)
    mock_client.search_scheduled_shifts.return_value = []

    preview = preview_production_sync(run, client=mock_client)
    assert preview.environment == "production"
    assert preview.location_id == "LOC_PROD_1"
    assert preview.ready_count > 0
    assert preview.rows[0].result_status == "READY_TO_CREATE"

    audit = SquareSyncAuditLog.objects.filter(
        schedule_run=run,
        action_type=SquareSyncAuditAction.PRODUCTION_SYNC_PREVIEWED,
    ).first()
    assert audit is not None


@pytest.mark.django_db
def test_production_write_blocked_when_flag_false(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    monkeypatch.setenv("SQUARE_PRODUCTION_WRITES_ENABLED", "false")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    run = SchedulingEngine().generate(date(2026, 9, 12), date(2026, 9, 12), allow_shortages=True)
    approve_schedule(run, user)

    assignment = run.assignments.first()

    with pytest.raises(SquareProductionWritesDisabledError, match="disabled"):
        create_production_pilot_shift(
            run,
            assignment_id=assignment.id,
            confirmation_phrase="CREATE ONE PRODUCTION DRAFT",
            user=user,
        )


@pytest.mark.django_db
def test_pilot_shift_requires_exact_confirmation_phrase(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    monkeypatch.setenv("SQUARE_PRODUCTION_WRITES_ENABLED", "true")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    run = SchedulingEngine().generate(date(2026, 9, 12), date(2026, 9, 12), allow_shortages=True)
    approve_schedule(run, user)

    assignment = run.assignments.first()

    with pytest.raises(SquareSyncValidationError, match="Exact typed confirmation phrase"):
        create_production_pilot_shift(
            run,
            assignment_id=assignment.id,
            confirmation_phrase="WRONG PHRASE",
            user=user,
        )


@pytest.mark.django_db
def test_successful_one_shift_pilot_and_verification(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    monkeypatch.setenv("SQUARE_PRODUCTION_WRITES_ENABLED", "true")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    SquareLocationMapping.objects.create(
        environment="production",
        square_location_id="LOC_PROD_1",
        location_name="Spirit Theatre",
        active=True,
    )

    for idx, emp in enumerate(Employee.objects.all()):
        SquareEmployeeMapping.objects.create(
            employee=emp,
            environment="production",
            square_team_member_id=f"SQ_PROD_TM_{idx+1}",
            status=MappingStatus.MAPPED,
        )
    for idx, role in enumerate(Role.objects.all()):
        SquareRoleMapping.objects.create(
            role=role,
            environment="production",
            square_job_id=f"SQ_PROD_JOB_{idx+1}",
            status=MappingStatus.MAPPED,
        )

    run = SchedulingEngine().generate(date(2026, 9, 12), date(2026, 9, 12), allow_shortages=True)
    approve_schedule(run, user)

    assignment = run.assignments.first()
    emp_map = SquareEmployeeMapping.objects.get(
        employee=assignment.employee, environment="production"
    )

    mock_client = MagicMock(spec=SquareClient)
    mock_client.config = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        sandbox_access_token="test",
        production_access_token="prod-token",
        production_writes_enabled=True,
    )
    mock_client.create_draft_shift.return_value = {"id": "PILOT_SHIFT_999", "status": "DRAFT"}
    mock_client.get_scheduled_shift.return_value = {
        "id": "PILOT_SHIFT_999",
        "draft_shift_details": {"team_member_id": emp_map.square_team_member_id},
    }

    res = create_production_pilot_shift(
        run,
        assignment_id=assignment.id,
        confirmation_phrase="CREATE ONE PRODUCTION DRAFT",
        client=mock_client,
        user=user,
    )

    assert res["square_scheduled_shift_id"] == "PILOT_SHIFT_999"
    assert res["verification_success"] is True

    mark_pilot_verified(user=user, square_shift_id="PILOT_SHIFT_999")
    audit = SquareSyncAuditLog.objects.filter(
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_VERIFIED
    ).first()
    assert audit is not None


@pytest.mark.django_db
def test_full_sync_requires_pilot_verification(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    monkeypatch.setenv("SQUARE_PRODUCTION_WRITES_ENABLED", "true")
    monkeypatch.setenv("SQUARE_PRODUCTION_PILOT_VERIFIED", "false")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    run = SchedulingEngine().generate(date(2026, 9, 12), date(2026, 9, 12), allow_shortages=True)
    approve_schedule(run, user)

    with pytest.raises(SquarePilotNotVerifiedError, match="pilot verification"):
        sync_full_production_schedule(run, confirmation_phrase="CREATE SQUARE DRAFTS", user=user)


@pytest.mark.django_db
def test_publishing_remains_impossible():
    with pytest.raises(SquarePublishingDisabledError, match="strictly prohibited"):
        SquareConfig(
            environment=SquareEnvironment.PRODUCTION,
            publishing_enabled=True,
        ).assert_publishing_disabled()


@pytest.mark.django_db
def test_manual_review_and_candidate_approval(monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    call_command("seed_spirit_staff")

    team_members = [
        {"id": "SQ_TM_OLENA", "given_name": "Olena", "family_name": "Martynova"},
    ]
    mock_client = MagicMock(spec=SquareClient)
    mock_client.search_team_members.return_value = team_members

    res = sync_production_team_members(client=mock_client)
    assert res["manual_review_required"] >= 1

    olena = Employee.objects.get(display_name="Olena")
    mapping = SquareEmployeeMapping.objects.get(employee=olena, environment="production")
    assert mapping.status == MappingStatus.MANUAL_REVIEW_REQUIRED
    assert mapping.potential_square_name == "Olena Martynova"

    # Approve candidate
    from scheduling.services.square_production_sync import approve_manual_employee_mapping
    approved = approve_manual_employee_mapping(olena.id, "SQ_TM_OLENA")
    assert approved.status == MappingStatus.MAPPED_EXACT
    assert approved.square_team_member_id == "SQ_TM_OLENA"


@pytest.mark.django_db
def test_pilot_duplicate_detected_as_already_exists(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="admin_test")
    run = SchedulingEngine().generate(date(2026, 9, 12), date(2026, 9, 12), allow_shortages=True)
    approve_schedule(run, user)

    # Map staff & role
    jackie = Employee.objects.get(display_name="Jackie Pynn")
    # Put Jackie on the pilot slot explicitly. Which server wins it is not what this
    # test is about: all servers now rank equally, so the roster is free to change from
    # run to run, and the pilot guard only means anything while she holds that shift.
    run.assignments.filter(employee=jackie).exclude(
        shift_template__code="lead-server"
    ).delete()
    lead = run.assignments.get(shift_template__code="lead-server")
    lead.employee = jackie
    lead.save(update_fields=["employee"])
    SquareEmployeeMapping.objects.create(
        employee=jackie,
        environment="production",
        square_team_member_id="TM_JACKIE",
        status=MappingStatus.MAPPED_EXACT,
    )
    server_role = Role.objects.get(name="Server")
    SquareRoleMapping.objects.create(
        role=server_role,
        environment="production",
        square_job_id="JOB_SERVER",
        status=MappingStatus.MAPPED_EXACT,
    )
    SquareLocationMapping.objects.create(
        environment="production",
        square_location_id="LR73BX986ZKYD",
    )


    mock_client = MagicMock(spec=SquareClient)
    mock_client.search_scheduled_shifts.return_value = [
        {
            "id": "T39WJ6S3HYSSJ",
            "draft_shift_details": {
                "team_member_id": "TM_JACKIE",
                "job_id": "JOB_SERVER",
                "start_at": "2026-09-12T15:00:00-02:30",
                "end_at": "2026-09-12T21:30:00-02:30",
            },
        }
    ]

    preview = preview_production_sync(run, client=mock_client)
    assert preview.already_exists_count >= 1

    already_exists_row = next(r for r in preview.rows if r.result_status == "ALREADY_EXISTS")
    assert already_exists_row.employee_name == "Jackie Pynn"


@pytest.mark.django_db
def test_export_production_sync_csv_view(client, monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "prod-token")
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    call_command("seed_schedule_demo")

    user = User.objects.create_user(username="csv_admin")
    client.force_login(user)

    run = SchedulingEngine().generate(date(2026, 9, 12), date(2026, 9, 12), allow_shortages=True)
    approve_schedule(run, user)

    response = client.get(f"/schedules/{run.id}/sync-export.csv")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    assert b"Date,Show,Employee,Role" in response.content


