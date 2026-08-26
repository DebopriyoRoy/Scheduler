import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

from integrations.square.client import SquareClient
from integrations.square.config import SquareConfig, SquareEnvironment
from integrations.square.exceptions import (
    SquareAPIError,
)
from scheduling.models import (
    Employee,
    MappingStatus,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SquareEmployeeMapping,
    SquareLocationMapping,
    SquareRoleMapping,
    SquareSyncAuditAction,
    SquareSyncAuditLog,
)

logger = logging.getLogger(__name__)

EXPECTED_STAFF_NAMES = [
    "Joleen Dickson",
    "Jackie Pynn",
    "Olena",
    "Yana",
    "Kate",
    "Molly Rittwage",
    "Linda Penney",
    "Svitlana",
    "Daniel",
    "Butros",
    "Patrice",
    "Montana",
    "Neil Bobbit",
    "Brittany James",
    "Khrystyna",
    "Emily",
    "Maks Plsky",
]

EXCLUDED_STAFF_NAMES = {
    "debroah sweetapple",
    "deborah sweetapple",
    "john haris",
    "john harris",
}


def normalize_name(text: str) -> str:
    """Normalizes whitespace and case for exact string comparisons."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


class SquareProductionSyncError(Exception):
    """Base exception for Square Production synchronization errors."""


class SquareSyncValidationError(SquareProductionSyncError):
    """Raised when pre-sync validation checks fail."""


@dataclass
class ProductionSyncPreviewRow:
    assignment_id: int
    show_title: str
    show_date: str
    employee_name: str
    role_name: str
    assignment_type: str
    start_at: str
    end_at: str
    square_team_member_id: str
    square_job_id: str
    result_status: str
    reason: str


@dataclass
class ProductionSyncPreviewResult:
    schedule_run: ScheduleRun
    environment: str
    location_id: str
    location_name: str
    is_ready_for_pilot: bool
    is_ready_for_full_sync: bool
    ready_count: int = 0
    already_exists_count: int = 0
    conflict_count: int = 0
    blocked_count: int = 0
    rows: list[ProductionSyncPreviewRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def test_production_connection(client: SquareClient | None = None) -> list[dict[str, Any]]:
    """READ ONLY connection test for Production environment."""
    if client is None:
        config = SquareConfig.from_env()
        prod_config = SquareConfig(
            environment=SquareEnvironment.PRODUCTION,
            sandbox_access_token=config.sandbox_access_token,
            production_access_token=config.production_access_token,
            location_id=config.location_id,
            api_version=config.api_version,
            request_timeout_seconds=config.request_timeout_seconds,
            production_writes_enabled=config.production_writes_enabled,
            production_pilot_verified=config.production_pilot_verified,
            publishing_enabled=config.publishing_enabled,
        )
        client = SquareClient(prod_config)
    return client.test_connection()


def sync_production_team_members(
    client: SquareClient | None = None,
    user=None,
) -> dict[str, int]:
    """Retrieves Production team members and performs exact and candidate matching against staff."""
    config = SquareConfig.from_env()
    prod_config = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        sandbox_access_token=config.sandbox_access_token,
        production_access_token=config.production_access_token,
        location_id=config.location_id,
        api_version=config.api_version,
        request_timeout_seconds=config.request_timeout_seconds,
    )
    client = client or SquareClient(prod_config)

    team_members = client.search_team_members(active_only=True)

    exact_count = 0
    review_count = 0
    ambiguous_count = 0
    not_found_count = 0

    for emp in Employee.objects.filter(active=True):
        norm_emp_name = normalize_name(emp.display_name)

        if emp.excluded_from_automatic_scheduling or norm_emp_name in EXCLUDED_STAFF_NAMES:
            continue

        existing_mapping = SquareEmployeeMapping.objects.filter(
            employee=emp,
            environment=SquareEnvironment.PRODUCTION.value,
        ).first()

        # Check existing confirmed mapping
        if existing_mapping and existing_mapping.status in {
            MappingStatus.MAPPED,
            MappingStatus.MAPPED_EXACT,
        }:
            exact_count += 1
            continue

        exact_matches = []
        candidate_matches = []

        for tm in team_members:
            given = tm.get("given_name", "")
            family = tm.get("family_name", "")
            full_tm_name = f"{given} {family}".strip() or given
            norm_tm_name = normalize_name(full_tm_name)
            norm_given = normalize_name(given)

            if norm_tm_name == norm_emp_name:
                exact_matches.append(tm)
            elif norm_given == norm_emp_name or (norm_emp_name in norm_tm_name):
                candidate_matches.append(tm)

        if len(exact_matches) == 1:
            match = exact_matches[0]
            g_name = match.get("given_name", "")
            f_name = match.get("family_name", "")
            SquareEmployeeMapping.objects.update_or_create(
                employee=emp,
                environment=SquareEnvironment.PRODUCTION.value,
                defaults={
                    "square_team_member_id": match.get("id", ""),
                    "square_given_name": g_name,
                    "square_family_name": f_name,
                    "potential_square_name": f"{g_name} {f_name}".strip(),
                    "match_type": "EXACT_NAME_MATCH",
                    "confidence_reason": "Exact normalized name match.",
                    "status": MappingStatus.MAPPED_EXACT,
                    "verified_at": timezone.now(),
                },
            )
            exact_count += 1
        elif len(candidate_matches) == 1:
            match = candidate_matches[0]
            sq_full_name = f"{match.get('given_name', '')} {match.get('family_name', '')}".strip()
            reason_msg = (
                f"Matches given name of Square team member '{sq_full_name}'. "
                "Management review required."
            )
            SquareEmployeeMapping.objects.update_or_create(
                employee=emp,
                environment=SquareEnvironment.PRODUCTION.value,
                defaults={
                    "square_team_member_id": match.get("id", ""),
                    "square_given_name": match.get("given_name", ""),
                    "square_family_name": match.get("family_name", ""),
                    "potential_square_name": sq_full_name,
                    "match_type": "GIVEN_NAME_CANDIDATE",
                    "confidence_reason": reason_msg,
                    "status": MappingStatus.MANUAL_REVIEW_REQUIRED,
                },
            )

            review_count += 1
        elif len(exact_matches) > 1 or len(candidate_matches) > 1:
            SquareEmployeeMapping.objects.update_or_create(
                employee=emp,
                environment=SquareEnvironment.PRODUCTION.value,
                defaults={
                    "square_team_member_id": "",
                    "status": MappingStatus.AMBIGUOUS,
                    "confidence_reason": "Multiple Square team members match name criteria.",
                },
            )
            ambiguous_count += 1
        else:
            SquareEmployeeMapping.objects.update_or_create(
                employee=emp,
                environment=SquareEnvironment.PRODUCTION.value,
                defaults={
                    "square_team_member_id": "",
                    "status": MappingStatus.NOT_FOUND,
                    "confidence_reason": "No active Square team member matching name.",
                },
            )
            not_found_count += 1

    return {
        "mapped_exact": exact_count,
        "manual_review_required": review_count,
        "ambiguous": ambiguous_count,
        "not_found": not_found_count,
    }


def approve_manual_employee_mapping(
    employee_id: int,
    square_team_member_id: str,
    user=None,
) -> SquareEmployeeMapping:
    """Confirms and activates a manual employee mapping for Production."""
    mapping = SquareEmployeeMapping.objects.get(
        employee_id=employee_id,
        environment=SquareEnvironment.PRODUCTION.value,
    )
    mapping.square_team_member_id = square_team_member_id
    mapping.status = MappingStatus.MAPPED_EXACT
    mapping.verified_at = timezone.now()
    mapping.confidence_reason = f"Manually verified by management ({user or 'Admin'})."
    mapping.save()
    return mapping



def sync_production_jobs(
    client: SquareClient | None = None,
    user=None,
) -> dict[str, int]:
    """Retrieves Production jobs and maps local roles to Square Production job IDs."""
    config = SquareConfig.from_env()
    prod_config = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        sandbox_access_token=config.sandbox_access_token,
        production_access_token=config.production_access_token,
        location_id=config.location_id,
        api_version=config.api_version,
        request_timeout_seconds=config.request_timeout_seconds,
    )
    client = client or SquareClient(prod_config)

    jobs = client.list_jobs()
    mapped_count = 0
    unmapped_count = 0

    for role in Role.objects.all():
        norm_role_name = normalize_name(role.name)
        matches = [
            j for j in jobs
            if normalize_name(j.get("title", "")) == norm_role_name
        ]

        if len(matches) == 1:
            match = matches[0]
            SquareRoleMapping.objects.update_or_create(
                role=role,
                environment=SquareEnvironment.PRODUCTION.value,
                defaults={
                    "square_job_id": match.get("id", ""),
                    "square_job_title": match.get("title", ""),
                    "status": MappingStatus.MAPPED,
                    "verified_at": timezone.now(),
                },
            )
            mapped_count += 1
        else:
            mapping = SquareRoleMapping.objects.filter(
                role=role, environment=SquareEnvironment.PRODUCTION.value
            ).first()
            if not mapping or not mapping.square_job_id:
                SquareRoleMapping.objects.update_or_create(
                    role=role,
                    environment=SquareEnvironment.PRODUCTION.value,
                    defaults={
                        "square_job_id": "",
                        "square_job_title": "",
                        "status": MappingStatus.UNMAPPED,
                    },
                )
                unmapped_count += 1
            else:
                mapped_count += 1

    return {"mapped": mapped_count, "unmapped": unmapped_count}


def preview_production_sync(
    schedule_run: ScheduleRun,
    client: SquareClient | None = None,
    user=None,
) -> ProductionSyncPreviewResult:
    """READ ONLY Production sync preview classifying proposed schedule assignments."""
    errors: list[str] = []
    warnings: list[str] = []

    if schedule_run.status not in {ScheduleRunStatus.APPROVED, ScheduleRunStatus.SYNCED_TO_SQUARE}:
        errors.append(
            f"Schedule status is '{schedule_run.get_status_display()}'. "
            "Production synchronization requires an APPROVED schedule."
        )

    config = SquareConfig.from_env()
    config.assert_publishing_disabled()

    prod_location = SquareLocationMapping.objects.filter(
        environment=SquareEnvironment.PRODUCTION.value, active=True
    ).first()
    location_id = config.location_id or (
        prod_location.square_location_id if prod_location else ""
    )
    location_name = (
        prod_location.location_name if prod_location else "Square Production Location"
    )

    if not location_id:
        errors.append("No active Square Production location selected.")

    existing_shifts: list[dict[str, Any]] = []
    if config.token_is_configured and location_id:
        try:
            prod_config = SquareConfig(
                environment=SquareEnvironment.PRODUCTION,
                sandbox_access_token=config.sandbox_access_token,
                production_access_token=config.production_access_token,
                location_id=location_id,
                api_version=config.api_version,
                request_timeout_seconds=config.request_timeout_seconds,
            )
            client_instance = client or SquareClient(prod_config)
            start_bound = f"{schedule_run.start_date.isoformat()}T00:00:00Z"
            end_bound = f"{schedule_run.end_date.isoformat()}T23:59:59Z"
            query = {
                "filter": {
                    "start": {"start_at": start_bound, "end_at": end_bound},
                    "location_ids": [location_id],
                }
            }
            existing_shifts = client_instance.search_scheduled_shifts(query)
            if existing_shifts:
                warnings.append(
                    f"Found {len(existing_shifts)} existing shift(s) in Square Production "
                    f"during the schedule date range."
                )
        except (SquareAPIError, Exception) as exc:
            warnings.append(f"Unable to query Square Production shifts: {exc}")

    assignments = schedule_run.assignments.select_related(
        "employee", "role", "show", "shift_template"
    ).all()
    if not assignments.exists():
        errors.append("Schedule run has no assignments.")

    rows: list[ProductionSyncPreviewRow] = []
    ready_count = 0
    already_exists_count = 0
    conflict_count = 0
    blocked_count = 0

    emp_mappings = {
        m.employee_id: m
        for m in SquareEmployeeMapping.objects.filter(
            environment=SquareEnvironment.PRODUCTION.value
        )
    }
    role_mappings = {
        m.role_id: m
        for m in SquareRoleMapping.objects.filter(
            environment=SquareEnvironment.PRODUCTION.value
        )
    }

    for assignment in assignments:
        emp_map = emp_mappings.get(assignment.employee_id)
        role_map = role_mappings.get(assignment.role_id)

        sq_team_id = (
            emp_map.square_team_member_id
            if emp_map and emp_map.status in {MappingStatus.MAPPED, MappingStatus.MAPPED_EXACT}
            else ""
        )
        sq_job_id = (
            role_map.square_job_id
            if role_map and role_map.status in {MappingStatus.MAPPED, MappingStatus.MAPPED_EXACT}
            else ""
        )

        start_iso = assignment.start_datetime.isoformat()
        end_iso = assignment.end_datetime.isoformat()

        result_status = "READY_TO_CREATE"
        reason = "Ready to create unpublished draft shift in Square Production."

        if not location_id:
            result_status = "LOCATION_UNMAPPED"
            reason = "No active Square Production location selected."
            blocked_count += 1
        elif not sq_team_id:
            result_status = "TEAM_MEMBER_UNMAPPED"
            reason = (
                f"Employee '{assignment.employee.display_name}' is not mapped "
                "to a Production Square team member ID."
            )
            blocked_count += 1
        elif not sq_job_id:
            result_status = "ROLE_UNMAPPED"
            reason = f"Role '{assignment.role.name}' is not mapped to a Production Square job ID."
            blocked_count += 1
        else:
            dup_found = False
            conflict_found = False
            pilot_mismatch = False
            for ex in existing_shifts:
                shift_id = ex.get("id")
                details = ex.get("draft_shift_details") or ex.get("published_shift_details") or {}
                ex_team = details.get("team_member_id")
                ex_job = details.get("job_id")
                ex_start = details.get("start_at", "")
                ex_end = details.get("end_at", "")

                # Check pilot shift T39WJ6S3HYSSJ
                if (
                    shift_id == "T39WJ6S3HYSSJ"
                    and assignment.show.date.isoformat() == "2026-09-12"
                    and assignment.shift_template.code == "lead-server"
                ):
                    if assignment.employee.display_name == "Jackie Pynn":
                        dup_found = True
                        break
                    else:
                        pilot_mismatch = True
                        break

                if ex_team == sq_team_id:
                    start_date_match = ex_start[:10] == start_iso[:10]
                    start_time_match = ex_start[:16] == start_iso[:16]

                    if start_date_match:
                        if ex_job == sq_job_id or start_time_match:
                            dup_found = True
                            break
                        else:
                            conflict_found = True
                            break
                    elif ex_start < end_iso and ex_end > start_iso:
                        conflict_found = True
                        break

            if pilot_mismatch:
                result_status = "PILOT_SCHEDULE_MISMATCH"
                reason = (
                    f"Production pilot shift T39WJ6S3HYSSJ is for Jackie Pynn, "
                    f"but schedule assigns {assignment.employee.display_name}."
                )
                blocked_count += 1
            elif dup_found:
                result_status = "ALREADY_EXISTS"
                reason = (
                    "Equivalent shift (or verified pilot shift) "
                    "already exists in Square Production."
                )
                already_exists_count += 1

            elif conflict_found:
                result_status = "EXISTING_SHIFT_CONFLICT"
                reason = "Employee has an overlapping scheduled shift in Square Production."
                conflict_count += 1
            else:
                if not config.production_writes_enabled:
                    reason = "Ready (Note: Production writes are currently disabled in settings)."
                ready_count += 1


        rows.append(
            ProductionSyncPreviewRow(
                assignment_id=assignment.id,
                show_title=assignment.show.title,
                show_date=assignment.show.date.isoformat(),
                employee_name=assignment.employee.display_name,
                role_name=assignment.role.name,
                assignment_type=assignment.get_assignment_type_display(),
                start_at=start_iso,
                end_at=end_iso,
                square_team_member_id=sq_team_id or "UNMAPPED",
                square_job_id=sq_job_id or "UNMAPPED",
                result_status=result_status,
                reason=reason,
            )
        )

    is_ready_for_pilot = len(errors) == 0 and ready_count > 0
    is_ready_for_full_sync = (
        is_ready_for_pilot
        and config.production_writes_enabled
        and config.production_pilot_verified
        and blocked_count == 0
    )

    SquareSyncAuditLog.objects.create(
        action_type=SquareSyncAuditAction.PRODUCTION_SYNC_PREVIEWED,
        environment=SquareEnvironment.PRODUCTION.value,
        user=user if user and user.is_authenticated else None,
        schedule_run=schedule_run,
        details={
            "ready_count": ready_count,
            "already_exists_count": already_exists_count,
            "conflict_count": conflict_count,
            "blocked_count": blocked_count,
            "total_rows": len(rows),
        },
    )

    return ProductionSyncPreviewResult(
        schedule_run=schedule_run,
        environment=SquareEnvironment.PRODUCTION.value,
        location_id=location_id,
        location_name=location_name,
        is_ready_for_pilot=is_ready_for_pilot,
        is_ready_for_full_sync=is_ready_for_full_sync,
        ready_count=ready_count,
        already_exists_count=already_exists_count,
        conflict_count=conflict_count,
        blocked_count=blocked_count,
        rows=rows,
        errors=errors,
        warnings=warnings,
    )


def create_production_pilot_shift(
    schedule_run: ScheduleRun,
    assignment_id: int,
    confirmation_phrase: str,
    client: SquareClient | None = None,
    user=None,
) -> dict[str, Any]:
    """Creates EXACTLY ONE Production draft shift as a pilot test."""
    config = SquareConfig.from_env()
    config.assert_write_allowed()
    config.assert_publishing_disabled()

    if config.environment is not SquareEnvironment.PRODUCTION:
        raise SquareSyncValidationError("Pilot creation requires SQUARE_ENVIRONMENT=production.")

    if confirmation_phrase.strip() != "CREATE ONE PRODUCTION DRAFT":
        raise SquareSyncValidationError(
            "Exact typed confirmation phrase 'CREATE ONE PRODUCTION DRAFT' is required."
        )

    previous_pilot = SquareSyncAuditLog.objects.filter(
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_CREATED,
        environment=SquareEnvironment.PRODUCTION.value,
    ).exists()
    if previous_pilot and not config.production_pilot_verified:
        raise SquareSyncValidationError(
            "A Production pilot shift has already been created. "
            "Verify the existing pilot in Square Dashboard before creating another."
        )

    assignment = ScheduleAssignment.objects.select_related(
        "employee", "role", "show", "shift_template"
    ).get(pk=assignment_id, schedule_run=schedule_run)

    # MAPPED_EXACT is a successful mapping too. preview_production_sync() already
    # treats both as usable, so filtering to MAPPED alone here let an assignment
    # preview as READY_TO_CREATE and then fail mid-sync, leaving a partially
    # written roster in Square.
    emp_map = SquareEmployeeMapping.objects.filter(
        employee=assignment.employee,
        environment=SquareEnvironment.PRODUCTION.value,
        status__in=[MappingStatus.MAPPED, MappingStatus.MAPPED_EXACT],
    ).first()
    role_map = SquareRoleMapping.objects.filter(
        role=assignment.role,
        environment=SquareEnvironment.PRODUCTION.value,
        status__in=[MappingStatus.MAPPED, MappingStatus.MAPPED_EXACT],
    ).first()
    loc_map = SquareLocationMapping.objects.filter(
        environment=SquareEnvironment.PRODUCTION.value,
        active=True,
    ).first()

    if not emp_map or not emp_map.square_team_member_id:
        raise SquareSyncValidationError(
            f"Employee {assignment.employee.display_name} is not mapped in Production."
        )
    if not role_map or not role_map.square_job_id:
        raise SquareSyncValidationError(
            f"Role {assignment.role.name} is not mapped in Production."
        )

    location_id = config.location_id or (loc_map.square_location_id if loc_map else "")
    if not location_id:
        raise SquareSyncValidationError("No target Production Square location ID found.")

    timestamp = int(assignment.updated_at.timestamp())
    idempotency_key = f"spirit-shift-prod-v1-{assignment.id}-{timestamp}"

    if assignment.assignment_type == "ON_CALL":
        notes = (
            f"Spirit Scheduling Engine\n"
            f"Show: {assignment.show.title}\n"
            f"Assignment: ON CALL {assignment.role.name}\n"
            f"Management confirmation required\n"
            f"Expected Guests: {assignment.show.planning_guest_count}\n"
            f"Schedule Run: #{schedule_run.id}"
        )
    else:
        notes = (
            f"Spirit Scheduling Engine\n"
            f"Show: {assignment.show.title}\n"
            f"Assignment: {assignment.get_assignment_type_display()}\n"
            f"Expected Guests: {assignment.show.planning_guest_count}\n"
            f"Schedule Run: #{schedule_run.id}"
        )

    prod_config = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        sandbox_access_token=config.sandbox_access_token,
        production_access_token=config.production_access_token,
        location_id=location_id,
        api_version=config.api_version,
        request_timeout_seconds=config.request_timeout_seconds,
        production_writes_enabled=True,
    )
    client_instance = client or SquareClient(prod_config)

    draft_shift = client_instance.create_draft_shift(
        idempotency_key=idempotency_key,
        team_member_id=emp_map.square_team_member_id,
        job_id=role_map.square_job_id,
        location_id=location_id,
        start_at=assignment.start_datetime.isoformat(),
        end_at=assignment.end_datetime.isoformat(),
        notes=notes,
    )

    shift_id = draft_shift.get("id", "")

    verification_success = False
    if shift_id:
        try:
            verified_shift = client_instance.get_scheduled_shift(shift_id)
            v_details = verified_shift.get("draft_shift_details", {})
            if v_details.get("team_member_id") == emp_map.square_team_member_id:
                verification_success = True
        except Exception as exc:
            logger.warning("Pilot post-create verification check failed: %s", exc)

    SquareSyncAuditLog.objects.create(
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_CREATED,
        environment=SquareEnvironment.PRODUCTION.value,
        user=user if user and user.is_authenticated else None,
        schedule_run=schedule_run,
        assignment=assignment,
        square_scheduled_shift_id=shift_id,
        details={
            "idempotency_key": idempotency_key,
            "verification_success": verification_success,
            "employee": assignment.employee.display_name,
            "role": assignment.role.name,
            "show": assignment.show.title,
        },
    )

    return {
        "shift": draft_shift,
        "verification_success": verification_success,
        "square_scheduled_shift_id": shift_id,
    }


def mark_pilot_verified(user=None, square_shift_id: str = "") -> None:
    """Records administrative confirmation that Production pilot was manually verified in Square."""
    SquareSyncAuditLog.objects.create(
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_VERIFIED,
        environment=SquareEnvironment.PRODUCTION.value,
        user=user if user and user.is_authenticated else None,
        square_scheduled_shift_id=square_shift_id,
        details={"verified_by": str(user) if user else "Admin"},
    )
    logger.info("Production pilot shift verified by %s.", user)


def sync_full_production_schedule(
    schedule_run: ScheduleRun,
    confirmation_phrase: str,
    client: SquareClient | None = None,
    user=None,
) -> dict[str, Any]:
    """Publishes full approved schedule draft shifts to Square Production."""
    config = SquareConfig.from_env()
    config.assert_write_allowed()
    config.assert_pilot_verified()
    config.assert_publishing_disabled()

    if confirmation_phrase.strip() != "CREATE SQUARE DRAFTS":
        raise SquareSyncValidationError(
            "Exact typed confirmation phrase 'CREATE SQUARE DRAFTS' is required."
        )

    preview = preview_production_sync(schedule_run, client=client, user=user)
    if preview.errors:
        raise SquareSyncValidationError("; ".join(preview.errors))

    ready_rows = [r for r in preview.rows if r.result_status == "READY_TO_CREATE"]
    if not ready_rows:
        raise SquareSyncValidationError("No assignments are in READY_TO_CREATE status.")

    prod_config = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        sandbox_access_token=config.sandbox_access_token,
        production_access_token=config.production_access_token,
        location_id=preview.location_id,
        api_version=config.api_version,
        request_timeout_seconds=config.request_timeout_seconds,
        production_writes_enabled=True,
    )
    client_instance = client or SquareClient(prod_config)

    created_shifts = []
    failed_shifts = []

    SquareSyncAuditLog.objects.create(
        action_type=SquareSyncAuditAction.PRODUCTION_SYNC_STARTED,
        environment=SquareEnvironment.PRODUCTION.value,
        user=user if user and user.is_authenticated else None,
        schedule_run=schedule_run,
        details={"total_to_create": len(ready_rows)},
    )

    for row in ready_rows:
        assignment = ScheduleAssignment.objects.get(pk=row.assignment_id)
        timestamp = int(assignment.updated_at.timestamp())
        idempotency_key = f"spirit-shift-prod-v1-{assignment.id}-{timestamp}"

        notes = (
            f"Spirit Scheduling Engine\n"
            f"Show: {assignment.show.title}\n"
            f"Assignment: {assignment.get_assignment_type_display()}\n"
            f"Expected Guests: {assignment.show.planning_guest_count}\n"
            f"Schedule Run: #{schedule_run.id}"
        )

        success = False
        last_error = None
        for attempt in range(3):
            try:
                draft_shift = client_instance.create_draft_shift(
                    idempotency_key=idempotency_key,
                    team_member_id=row.square_team_member_id,
                    job_id=row.square_job_id,
                    location_id=preview.location_id,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    notes=notes,
                )
                created_shifts.append(draft_shift)
                success = True
                SquareSyncAuditLog.objects.create(
                    action_type=SquareSyncAuditAction.PRODUCTION_DRAFT_CREATED,
                    environment=SquareEnvironment.PRODUCTION.value,
                    user=user if user and user.is_authenticated else None,
                    schedule_run=schedule_run,
                    assignment=assignment,
                    square_scheduled_shift_id=draft_shift.get("id", ""),
                )
                break
            except SquareAPIError as exc:
                last_error = str(exc)
                if exc.status_code and exc.status_code in {429, 500, 502, 503, 504}:
                    time.sleep(1 * (attempt + 1))
                else:
                    break
            except Exception as exc:
                last_error = str(exc)
                break

        if not success:
            failed_shifts.append({"assignment_id": row.assignment_id, "error": last_error})
            SquareSyncAuditLog.objects.create(
                action_type=SquareSyncAuditAction.PRODUCTION_DRAFT_FAILED,
                environment=SquareEnvironment.PRODUCTION.value,
                user=user if user and user.is_authenticated else None,
                schedule_run=schedule_run,
                assignment=assignment,
                details={"error": last_error},
            )

    if not failed_shifts:
        schedule_run.status = ScheduleRunStatus.SYNCED_TO_SQUARE
        now_str = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
        cnt = len(created_shifts)
        sync_note = f"Synced {cnt} draft shift(s) to Square Production on {now_str}."


        schedule_run.notes = f"{schedule_run.notes}\n{sync_note}".strip()
        schedule_run.save()

    SquareSyncAuditLog.objects.create(
        action_type=SquareSyncAuditAction.PRODUCTION_SYNC_COMPLETED,
        environment=SquareEnvironment.PRODUCTION.value,
        user=user if user and user.is_authenticated else None,
        schedule_run=schedule_run,
        details={
            "created_count": len(created_shifts),
            "failed_count": len(failed_shifts),
        },
    )

    return {
        "schedule_run_id": schedule_run.id,
        "created_count": len(created_shifts),
        "failed_count": len(failed_shifts),
        "failed_shifts": failed_shifts,
        "status": schedule_run.status,
    }
