from datetime import date, time, timedelta

import pytest
from django.utils import timezone

from scheduling.importers.calendar import events_from_html
from scheduling.models import (
    Employee,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SourceSnapshot,
    SourceTypeChoices,
    SquareEmployeeMapping,
    SquareRoleMapping,
)
from scheduling.services.square_production_sync import preview_production_sync


@pytest.mark.django_db
def test_supersede_previous_schedule_run():
    run5 = ScheduleRun.objects.create(
        start_date=date(2026, 9, 7),
        end_date=date(2026, 10, 3),
        status=ScheduleRunStatus.SUPERSEDED_SOURCE_DATA,
        notes=(
            "Superseded because show calendar and staff availability were not "
            "sourced from live authoritative systems."
        ),
    )
    assert run5.status == ScheduleRunStatus.SUPERSEDED_SOURCE_DATA
    assert "Superseded" in run5.notes


@pytest.mark.django_db
def test_source_snapshot_creation_and_stale_flag():
    snapshot = SourceSnapshot.objects.create(
        source_type=SourceTypeChoices.LIVE_SPIRIT_CALENDAR,
        source_url="https://spiritofnewfoundland.com/show-calendar/",
        environment="production",
        record_count=6,
        is_live=True,
    )
    assert snapshot.is_live is True
    assert snapshot.is_stale is False

    # Simulate old snapshot
    snapshot.retrieved_at = timezone.now() - timedelta(hours=25)
    snapshot.save()
    assert snapshot.is_stale is True


@pytest.mark.django_db
def test_events_from_html_parser():
    sample_html = """
    <html>
      <head>
        <title>Forever Country – Fall 2026</title>
        <script type="application/ld+json">
        {
          "@type": "Event",
          "name": "Forever Country… In the Key of Spirit",
          "startDate": "2026-09-12T18:30:00-02:30",
          "endDate": "2026-09-12T22:30:00-02:30",
          "location": {"name": "Theatre Gower"}
        }
        </script>
      </head>
      <body>
        <h1>Forever Country… In the Key of Spirit</h1>
        <p>September 12, 2026</p>
      </body>
    </html>
    """
    events = events_from_html(sample_html, "https://spiritofnewfoundland.com/shows/test")
    assert len(events) >= 1
    assert events[0].date == date(2026, 9, 12)
    assert events[0].venue == "Theatre Gower"


@pytest.mark.django_db
def test_sync_preview_detects_pilot_shift_match():
    from unittest.mock import MagicMock
    run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 7),
        end_date=date(2026, 10, 3),
        status=ScheduleRunStatus.NEEDS_REVIEW,
    )
    emp = Employee.objects.create(first_name="Jackie", last_name="Pynn", display_name="Jackie Pynn")
    role = Role.objects.create(name="Server")
    from scheduling.models import ShiftTemplate, Show
    show = Show.objects.create(
        title="Test Show",
        date=date(2026, 9, 12),
        start_time=time(18, 30),
        end_time=time(22, 30),
    )
    tmpl = ShiftTemplate.objects.create(
        name="Server Shift",
        role=role,
        start_time=time(17, 0),
        end_time=time(23, 0),
    )

    from decimal import Decimal
    asgn = ScheduleAssignment.objects.create(
        schedule_run=run,
        show=show,
        employee=emp,
        role=role,
        shift_template=tmpl,
        start_datetime=timezone.now(),
        end_datetime=timezone.now() + timedelta(hours=6),
        scheduled_paid_hours=Decimal("6.0"),
        on_call_hours=Decimal("0.0"),
    )

    SquareEmployeeMapping.objects.create(
        employee=emp,
        environment="production",
        square_team_member_id="TMw6AlQQaftVDYWM",
        status="MAPPED",
    )
    SquareRoleMapping.objects.create(
        role=role,
        environment="production",
        square_job_id="a3j9wktGzEkSf3CJ171bxWLD",
        status="MAPPED",
    )

    # Mock search_scheduled_shifts to return pilot shift
    mock_client = MagicMock()
    mock_client.search_scheduled_shifts.return_value = [
        {
            "id": "T39WJ6S3HYSSJ",
            "draft_shift_details": {
                "team_member_id": "TMw6AlQQaftVDYWM",
                "job_id": "a3j9wktGzEkSf3CJ171bxWLD",
                "start_at": asgn.start_datetime.isoformat(),
                "end_at": asgn.end_datetime.isoformat(),
            },
        }
    ]

    preview = preview_production_sync(run, client=mock_client)
    assert preview.already_exists_count == 1
    assert preview.rows[0].result_status == "ALREADY_EXISTS"

