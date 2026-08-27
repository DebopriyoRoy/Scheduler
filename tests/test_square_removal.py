"""Removing a synced roster back out of Square.

Every test here drives a fake client. The real one would delete shifts from the
live production account, which is not something a test suite may do.
"""

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from integrations.square.exceptions import (
    SquareAPIError,
    SquarePublishedShiftError,
)
from scheduling.models import (
    ScheduleRun,
    ScheduleRunStatus,
    SquareSyncAuditAction,
    SquareSyncAuditLog,
)
from scheduling.services import square_production_sync as sync


@pytest.fixture
def writes_enabled(monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("SQUARE_LOCATION_ID", "TESTLOCATION")
    monkeypatch.setenv("SQUARE_PRODUCTION_WRITES_ENABLED", "true")
    monkeypatch.setenv("SQUARE_PUBLISHING_ENABLED", "false")


@pytest.fixture
def synced_run(db):
    start = date.today() + timedelta(days=10)
    run = ScheduleRun.objects.create(
        start_date=start,
        end_date=start + timedelta(days=7),
        status=ScheduleRunStatus.SYNCED_TO_SQUARE,
    )
    for shift_id in ("SHIFT-A", "SHIFT-B", "SHIFT-C"):
        SquareSyncAuditLog.objects.create(
            schedule_run=run,
            action_type=SquareSyncAuditAction.PRODUCTION_DRAFT_CREATED,
            square_scheduled_shift_id=shift_id,
        )
    return run


class FakeClient:
    """Records what it was asked to delete; raises for ids it is told to fail."""

    def __init__(self, config, *, published=(), api_errors=None):
        self.config = config
        self.deleted = []
        self._published = set(published)
        self._api_errors = api_errors or {}

    def delete_draft_shift(self, shift_id):
        if shift_id in self._published:
            raise SquarePublishedShiftError(f"{shift_id} is published")
        if shift_id in self._api_errors:
            raise self._api_errors[shift_id]
        self.deleted.append(shift_id)
        return {"id": shift_id}


def _install(monkeypatch, **kwargs):
    made = {}

    def factory(config):
        client = FakeClient(config, **kwargs)
        made["client"] = client
        return client

    monkeypatch.setattr(sync, "SquareClient", factory)
    return made


@pytest.mark.django_db
def test_every_recorded_shift_is_deleted_and_the_run_stops_claiming_to_be_in_square(
    monkeypatch, writes_enabled, synced_run
):
    made = _install(monkeypatch)
    result = sync.remove_run_from_square(synced_run)

    assert sorted(made["client"].deleted) == ["SHIFT-A", "SHIFT-B", "SHIFT-C"]
    assert result.deleted == 3
    assert result.clean
    synced_run.refresh_from_db()
    assert synced_run.status == ScheduleRunStatus.NEEDS_REVIEW


@pytest.mark.django_db
def test_only_ids_this_app_recorded_creating_are_touched(monkeypatch, writes_enabled, synced_run):
    """A shift a manager entered by hand must never be swept up."""
    SquareSyncAuditLog.objects.create(
        schedule_run=synced_run,
        action_type=SquareSyncAuditAction.PRODUCTION_SYNC_PREVIEWED,
        square_scheduled_shift_id="NOT-OURS",
    )
    made = _install(monkeypatch)
    sync.remove_run_from_square(synced_run)

    assert "NOT-OURS" not in made["client"].deleted


@pytest.mark.django_db
def test_a_published_shift_is_reported_rather_than_left_half_deleted(
    monkeypatch, writes_enabled, synced_run
):
    _install(monkeypatch, published=("SHIFT-B",))
    result = sync.remove_run_from_square(synced_run)

    assert result.deleted == 2
    assert result.published == ("SHIFT-B",)
    assert not result.clean
    # It still has a shift in Square, so it must keep saying so.
    synced_run.refresh_from_db()
    assert synced_run.status == ScheduleRunStatus.SYNCED_TO_SQUARE


@pytest.mark.django_db
def test_a_shift_already_gone_from_square_counts_as_done(monkeypatch, writes_enabled, synced_run):
    _install(monkeypatch, api_errors={"SHIFT-C": SquareAPIError("gone", status_code=404)})
    result = sync.remove_run_from_square(synced_run)

    assert result.deleted == 2
    assert result.already_gone == 1
    assert result.clean


@pytest.mark.django_db
def test_a_real_api_failure_is_surfaced_and_the_run_stays_marked_as_synced(
    monkeypatch, writes_enabled, synced_run
):
    _install(monkeypatch, api_errors={"SHIFT-A": SquareAPIError("boom", status_code=500)})
    result = sync.remove_run_from_square(synced_run)

    assert result.failed == ("SHIFT-A",)
    assert not result.clean
    synced_run.refresh_from_db()
    assert synced_run.status == ScheduleRunStatus.SYNCED_TO_SQUARE


@pytest.mark.django_db
def test_each_deletion_is_written_to_the_audit_log(monkeypatch, writes_enabled, synced_run):
    _install(monkeypatch, published=("SHIFT-B",))
    sync.remove_run_from_square(synced_run)

    def count(action):
        return synced_run.square_sync_audit_logs.filter(action_type=action).count()

    assert count(SquareSyncAuditAction.PRODUCTION_DRAFT_DELETED) == 2
    assert count(SquareSyncAuditAction.PRODUCTION_DRAFT_DELETE_FAILED) == 1
    assert count(SquareSyncAuditAction.PRODUCTION_REMOVED_FROM_SQUARE) == 1


@pytest.mark.django_db
def test_nothing_is_touched_while_production_writes_are_disabled(
    monkeypatch, writes_enabled, synced_run
):
    monkeypatch.setenv("SQUARE_PRODUCTION_WRITES_ENABLED", "false")
    made = _install(monkeypatch)
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)
    response = client.post(
        reverse("schedule_square_remove", args=[synced_run.pk]), follow=True
    )

    assert response.status_code == 200
    assert "client" not in made or not made["client"].deleted
    synced_run.refresh_from_db()
    assert synced_run.status == ScheduleRunStatus.SYNCED_TO_SQUARE


@pytest.mark.django_db
def test_a_get_cannot_remove_anything(monkeypatch, writes_enabled, synced_run):
    made = _install(monkeypatch)
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)
    response = client.get(reverse("schedule_square_remove", args=[synced_run.pk]))

    assert response.status_code == 302
    assert "client" not in made
    synced_run.refresh_from_db()
    assert synced_run.status == ScheduleRunStatus.SYNCED_TO_SQUARE


@pytest.mark.django_db
def test_the_button_is_offered_only_for_runs_actually_in_square(
    monkeypatch, writes_enabled, synced_run
):
    start = date.today() + timedelta(days=40)
    ScheduleRun.objects.create(
        start_date=start,
        end_date=start + timedelta(days=5),
        status=ScheduleRunStatus.NEEDS_REVIEW,
    )
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)
    body = client.get(reverse("schedule_list")).content.decode()

    assert body.count("Remove from Square") == 1


@pytest.mark.django_db
def test_still_in_square_is_created_minus_removed(synced_run):
    """The created rows stay in the audit log forever; they are not the question."""
    assert sync.shifts_still_in_square(synced_run) == {"SHIFT-A", "SHIFT-B", "SHIFT-C"}

    SquareSyncAuditLog.objects.create(
        schedule_run=synced_run,
        action_type=SquareSyncAuditAction.PRODUCTION_DRAFT_DELETED,
        square_scheduled_shift_id="SHIFT-B",
    )
    assert sync.shifts_still_in_square(synced_run) == {"SHIFT-A", "SHIFT-C"}


@pytest.mark.django_db
def test_a_run_becomes_deletable_once_its_shifts_are_out_of_square(monkeypatch, writes_enabled):
    """The round trip that was previously a dead end.

    An Approved run holding one pilot shift: Delete refused because the shift was in
    Square, and Remove was not offered because the run was not in the "In Square"
    section. Gating Delete on "has ever created" would also have kept it undeletable
    after the shift was removed, since that audit row never goes away.
    """
    start = date.today() + timedelta(days=10)
    run = ScheduleRun.objects.create(
        start_date=start, end_date=start, status=ScheduleRunStatus.APPROVED
    )
    SquareSyncAuditLog.objects.create(
        schedule_run=run,
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_CREATED,
        square_scheduled_shift_id="PILOT-1",
    )
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)

    refused = client.post(reverse("schedule_delete", args=[run.pk]), follow=True)
    assert b"still has 1 shift in Square" in refused.content
    assert ScheduleRun.objects.filter(pk=run.pk).exists()

    _install(monkeypatch)
    client.post(reverse("schedule_square_remove", args=[run.pk]), follow=True)
    assert sync.shifts_still_in_square(run) == set()

    client.post(reverse("schedule_delete", args=[run.pk]), follow=True)
    assert not ScheduleRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_a_pilot_shift_on_an_approved_run_still_offers_removal(writes_enabled):
    """A pilot puts one shift in Square while the run is only Approved.

    It never reaches the "In Square" section, so keying the button off the section
    stranded it: shifts in Square, and no way to take them out.
    """
    start = date.today() + timedelta(days=10)
    run = ScheduleRun.objects.create(
        start_date=start, end_date=start, status=ScheduleRunStatus.APPROVED
    )
    SquareSyncAuditLog.objects.create(
        schedule_run=run,
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_CREATED,
        square_scheduled_shift_id="PILOT-1",
    )
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)
    body = client.get(reverse("schedule_list")).content.decode()

    assert "Remove from Square (1)" in body


@pytest.mark.django_db
def test_a_superseded_run_holding_shifts_in_square_also_offers_removal(writes_enabled):
    start = date.today() - timedelta(days=60)
    run = ScheduleRun.objects.create(
        start_date=start,
        end_date=start + timedelta(days=5),
        status=ScheduleRunStatus.SUPERSEDED_SOURCE_DATA,
    )
    for shift_id in ("OLD-1", "OLD-2"):
        SquareSyncAuditLog.objects.create(
            schedule_run=run,
            action_type=SquareSyncAuditAction.PRODUCTION_DRAFT_CREATED,
            square_scheduled_shift_id=shift_id,
        )
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)
    body = client.get(reverse("schedule_list")).content.decode()

    assert "Remove from Square (2)" in body
