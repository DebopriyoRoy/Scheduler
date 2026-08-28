from datetime import time
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

MAX_THEATRE_CAPACITY = 175

# Real operating economics of the room, per management direction.
#
# A show below MINIMUM_VIABLE_GUESTS is not run: management either cancels it or moves
# those guests onto another date. 75-80 is the working buffer management plans against,
# so an unpriced show is planned at GUEST_COUNT_PLANNING_BUFFER rather than at an
# optimistic round number - planning high would over-hire, planning below 75 would
# imply a show that does not run.
MINIMUM_VIABLE_GUESTS = 75
GUEST_COUNT_PLANNING_BUFFER = 80
DEFAULT_EXPECTED_GUESTS = GUEST_COUNT_PLANNING_BUFFER

# Because a show never runs below MINIMUM_VIABLE_GUESTS, and one server covers 25-30
# guests, three confirmed servers is a hard floor on every show that runs at all.
# Coverage ratios, per management. Each role covers a block of guests, and a block
# tolerates GUEST_RATIO_BUFFER extra before another person is added - nobody is called
# in for the sake of five more covers. So one server covers 25-30 guests, one bartender
# 75-80, one busser 100-105.
GUESTS_PER_SERVER = 25
GUESTS_PER_BARTENDER = 75
GUESTS_PER_BUSSER = 100
GUEST_RATIO_BUFFER = 5

MINIMUM_CONFIRMED_SERVERS = 3


def staff_for_guests(guests: int, guests_per_head: int) -> int:
    """How many people a guest count needs at a given ratio, allowing the buffer.

    One person per `guests_per_head`, except that a block stretches to cover
    GUEST_RATIO_BUFFER extra guests before the next person is added. Always at least
    one, because any show that runs needs somebody in every role.
    """
    if guests <= 0:
        return 1
    return max(1, -(-(guests - GUEST_RATIO_BUFFER) // guests_per_head))


class AvailabilityType(models.TextChoices):
    AVAILABLE_ALL_DAY = "AVAILABLE_ALL_DAY", "Available all day"
    AVAILABLE_WINDOW = "AVAILABLE_WINDOW", "Available during a window"
    UNAVAILABLE = "UNAVAILABLE", "Unavailable"
    UNKNOWN = "UNKNOWN", "Unknown"


class AssignmentType(models.TextChoices):
    CONFIRMED = "CONFIRMED", "Confirmed"
    ON_CALL = "ON_CALL", "On call"
    FIFTY_FIFTY = "FIFTY_FIFTY", "50/50"


class ScheduleRunStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    GENERATING = "GENERATING", "Generating"
    GENERATED = "GENERATED", "Generated"
    NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
    APPROVED = "APPROVED", "Approved"
    FAILED = "FAILED", "Failed"
    SYNCED_TO_SQUARE = "SYNCED_TO_SQUARE", "Synced to Square"
    SUPERSEDED_SOURCE_DATA = "SUPERSEDED_SOURCE_DATA", "Superseded Source Data"


class SourceTypeChoices(models.TextChoices):
    LIVE_SPIRIT_CALENDAR = "LIVE_SPIRIT_CALENDAR", "Live Spirit Show Calendar"
    LIVE_SQUARE_AVAILABILITY = "LIVE_SQUARE_AVAILABILITY", "Live Square Availability"
    LIVE_SQUARE_SHIFTS = "LIVE_SQUARE_SHIFTS", "Live Square Shifts"
    MANUAL = "MANUAL", "Manual Entry"
    CSV_IMPORT = "CSV_IMPORT", "CSV Import"
    DEMO = "DEMO", "Demo Seed"
    SEED = "SEED", "System Seed"


class SourceSnapshot(models.Model):
    source_type = models.CharField(max_length=40, choices=SourceTypeChoices.choices)
    source_url = models.URLField(max_length=500, blank=True)
    environment = models.CharField(max_length=20, default="production")
    retrieved_at = models.DateTimeField(auto_now_add=True)
    record_count = models.PositiveIntegerField(default=0)
    content_hash = models.CharField(max_length=64, blank=True)
    is_live = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    raw_payload_json = models.TextField(blank=True)

    class Meta:
        ordering = ["-retrieved_at"]

    def __str__(self) -> str:
        return f"{self.get_source_type_display()} ({self.retrieved_at:%Y-%m-%d %H:%M})"

    @property
    def is_stale(self) -> bool:
        from django.utils import timezone
        return (timezone.now() - self.retrieved_at).total_seconds() > 86400




class WarningSeverity(models.TextChoices):
    INFO = "INFO", "Information"
    WARNING = "WARNING", "Warning"
    ERROR = "ERROR", "Hard validation error"


class WarningType(models.TextChoices):
    SERVER_SHORTAGE = "SERVER_SHORTAGE", "Server shortage"
    BARTENDER_SHORTAGE = "BARTENDER_SHORTAGE", "Bartender shortage"
    BUSSER_SHORTAGE = "BUSSER_SHORTAGE", "Busser shortage"
    FIFTY_FIFTY_SHORTAGE = "FIFTY_FIFTY_SHORTAGE", "50/50 shortage"
    ON_CALL_SERVER_SHORTAGE = "ON_CALL_SERVER_SHORTAGE", "On-call server shortage"
    ON_CALL_BARTENDER_SHORTAGE = (
        "ON_CALL_BARTENDER_SHORTAGE",
        "On-call bartender shortage",
    )
    UNKNOWN_AVAILABILITY = "UNKNOWN_AVAILABILITY", "Unknown availability"
    GUEST_COUNT_DEFAULTED = "GUEST_COUNT_DEFAULTED", "Guest count defaulted"
    HIGH_GUEST_COUNT_REVIEW = (
        "HIGH_GUEST_COUNT_REVIEW",
        "High guest count requires review",
    )
    OFFICE_CONFLICT = "OFFICE_CONFLICT", "Office assignment conflict"
    INSUFFICIENT_FAIRNESS_HISTORY = (
        "INSUFFICIENT_FAIRNESS_HISTORY",
        "Insufficient fairness history",
    )
    SERVER_MANAGER_SHORTAGE = "SERVER_MANAGER_SHORTAGE", "Server manager shortage"
    ROLE_CONFIGURATION_ERROR = "ROLE_CONFIGURATION_ERROR", "Role configuration error"
    SQUARE_OUT_OF_DATE = (
        "SQUARE_OUT_OF_DATE",
        "Square no longer matches this schedule",
    )
    EVENT_STAFFING_REVIEW_REQUIRED = (
        "EVENT_STAFFING_REVIEW_REQUIRED",
        "Event staffing review required",
    )
    PRIVATE_EVENT_STAFFING_REVIEW_REQUIRED = (
        "PRIVATE_EVENT_STAFFING_REVIEW_REQUIRED",
        "Private event staffing review required",
    )
    OPPORTUNITY_IMBALANCE = (
        "OPPORTUNITY_IMBALANCE",
        "Opportunity distribution imbalance",
    )
    ON_CALL_IMBALANCE = (
        "ON_CALL_IMBALANCE",
        "On-call burden imbalance",
    )
    WEEKEND_IMBALANCE = (
        "WEEKEND_IMBALANCE",
        "Weekend distribution imbalance",
    )
    TARGET_HOURS_SHORTFALL = (
        "TARGET_HOURS_SHORTFALL",
        "Target hours shortfall",
    )
    EXCESSIVE_CONSECUTIVE_SHIFTS = (
        "EXCESSIVE_CONSECUTIVE_SHIFTS",
        "Excessive consecutive shifts",
    )
    ACTUAL_TIMECARD_HISTORY_NOT_AVAILABLE = (
        "ACTUAL_TIMECARD_HISTORY_NOT_AVAILABLE",
        "Actual timecard history not available",
    )


class Employee(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    display_name = models.CharField(max_length=201, unique=True)
    active = models.BooleanField(default=True)
    square_team_member_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    # Retained as an employment record only. Both fields once fed a scoring boost in
    # the allocator and the fairness score; management's decision is that all servers
    # rank equally, so nothing in scheduling reads them. Do not re-wire them into
    # selection without being asked.
    spirit_only_employment = models.BooleanField(default=False)
    employment_priority = models.PositiveSmallIntegerField(default=0)
    excluded_from_automatic_scheduling = models.BooleanField(default=False)
    opening_recent_hours = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    opening_recent_shift_count = models.PositiveIntegerField(default=0)
    fairness_history_complete = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self) -> str:
        return self.display_name


class Role(models.Model):
    name = models.CharField(max_length=100, unique=True)
    square_job_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class EmployeeRole(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="employee_roles",
    )
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="employee_roles")
    capability_level = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["employee__display_name", "role__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "role"],
                name="unique_employee_role",
            )
        ]

    def __str__(self) -> str:
        return f"{self.employee} - {self.role} (Level {self.capability_level})"


class SquareLocation(models.Model):
    name = models.CharField(max_length=255)
    square_location_id = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CalendarSyncRun(models.Model):
    class ProviderChoices(models.TextChoices):
        API_XHR = "API_XHR", "Structured API/XHR"
        PLAYWRIGHT = "PLAYWRIGHT", "Playwright Rendered Browser"
        FALLBACK_METADATA_ONLY = "FALLBACK_METADATA_ONLY", "Fallback Metadata Only"

    class SyncStatus(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial Completeness"
        FAILED = "FAILED", "Failed"

    source_url = models.URLField(default="https://spiritofnewfoundland.com/show-calendar/")
    provider = models.CharField(max_length=64, choices=ProviderChoices.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    events_received = models.PositiveIntegerField(default=0)
    events_created = models.PositiveIntegerField(default=0)
    events_updated = models.PositiveIntegerField(default=0)
    events_unchanged = models.PositiveIntegerField(default=0)
    events_flagged = models.PositiveIntegerField(default=0)

    rendered_count = models.PositiveIntegerField(default=0)
    extracted_count = models.PositiveIntegerField(default=0)
    difference = models.IntegerField(default=0)

    status = models.CharField(max_length=32, choices=SyncStatus.choices, default=SyncStatus.RUNNING)
    error_message = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"CalendarSyncRun #{self.id} ({self.provider}) - {self.status}"


class SquareAvailabilitySyncRun(models.Model):
    class EnvironmentChoices(models.TextChoices):
        SANDBOX = "sandbox", "Sandbox"
        PRODUCTION = "production", "Production"

    class SyncStatus(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial Completeness"
        FAILED = "FAILED", "Failed"

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    environment = models.CharField(
        max_length=20, choices=EnvironmentChoices.choices, default=EnvironmentChoices.PRODUCTION
    )
    provider = models.CharField(max_length=64, default="STRUCTURED_DASHBOARD_REQUEST")
    start_date = models.DateField()
    end_date = models.DateField()

    employees_requested = models.PositiveIntegerField(default=17)
    employees_found = models.PositiveIntegerField(default=0)
    records_received = models.PositiveIntegerField(default=0)
    records_created = models.PositiveIntegerField(default=0)
    records_updated = models.PositiveIntegerField(default=0)
    records_unchanged = models.PositiveIntegerField(default=0)
    unknown_count = models.PositiveIntegerField(default=0)

    total_employee_date_combinations = models.PositiveIntegerField(default=0)
    known_employee_date_combinations = models.PositiveIntegerField(default=0)
    unknown_employee_date_combinations = models.PositiveIntegerField(default=0)
    available_window_combinations = models.PositiveIntegerField(default=0)
    available_window_records = models.PositiveIntegerField(default=0)
    all_day_combinations = models.PositiveIntegerField(default=0)
    unavailable_combinations = models.PositiveIntegerField(default=0)
    completeness_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00")
    )

    status = models.CharField(max_length=32, choices=SyncStatus.choices, default=SyncStatus.RUNNING)
    error_message = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"SquareAvailabilitySyncRun #{self.id} ({self.environment}) - {self.status}"


class Show(models.Model):
    class Source(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        CALENDAR_IMPORT = "CALENDAR_IMPORT", "Calendar import"
        DEMO = "DEMO", "Development demo"

    external_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    title = models.CharField(max_length=255)
    date = models.DateField(db_index=True)
    start_time = models.TimeField(default=time(18, 30))
    end_time = models.TimeField(default=time(22, 30))
    venue = models.CharField(max_length=255, default="Theatre Gower")
    expected_guests = models.PositiveSmallIntegerField(blank=True, null=True)
    capacity = models.PositiveSmallIntegerField(default=MAX_THEATRE_CAPACITY)
    capacity_override_reason = models.TextField(blank=True)
    requires_service_staff = models.BooleanField(default=True)
    requires_50_50 = models.BooleanField(default=False)
    source = models.CharField(max_length=32, choices=Source.choices, default=Source.MANUAL)
    source_url = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "start_time", "title"]
        indexes = [models.Index(fields=["date", "active"], name="show_date_active_idx")]

    def __str__(self) -> str:
        return f"{self.date:%Y-%m-%d} - {self.title}"

    def clean(self) -> None:
        super().clean()
        if self.end_time <= self.start_time:
            raise ValidationError({"end_time": "Show end time must be after the start time."})
        if self.expected_guests is not None and self.expected_guests > self.capacity:
            if not self.capacity_override_reason.strip():
                raise ValidationError(
                    {
                        "expected_guests": (
                            "Expected guests exceed capacity. Record an administrator override "
                            "reason to continue."
                        )
                    }
                )
        if self.capacity > MAX_THEATRE_CAPACITY and not self.capacity_override_reason.strip():
            raise ValidationError(
                {
                    "capacity": (
                        f"Capacity above {MAX_THEATRE_CAPACITY} requires an administrator "
                        "override reason."
                    )
                }
            )

    @property
    def planning_guest_count(self) -> int:
        return self.expected_guests if self.expected_guests is not None else DEFAULT_EXPECTED_GUESTS

    @property
    def uses_default_guest_count(self) -> bool:
        return self.expected_guests is None


class ShiftTemplate(models.Model):
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=100)
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="shift_templates")
    assignment_type = models.CharField(max_length=20, choices=AssignmentType.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()
    scheduled_paid_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    on_call_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    position_order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["position_order", "name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.end_time <= self.start_time:
            raise ValidationError({"end_time": "Shift end time must be after start time."})
        if self.assignment_type == AssignmentType.ON_CALL and self.scheduled_paid_hours:
            raise ValidationError(
                {"scheduled_paid_hours": "On-call templates cannot include paid hours by default."}
            )


class StaffingRule(models.Model):
    minimum_guests = models.PositiveSmallIntegerField(default=1)
    maximum_guests = models.PositiveSmallIntegerField(default=DEFAULT_EXPECTED_GUESTS)
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="staffing_rules")
    confirmed_count = models.PositiveSmallIntegerField(default=0)
    on_call_count = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)
    effective_from = models.DateField(blank=True, null=True)
    effective_to = models.DateField(blank=True, null=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["minimum_guests", "maximum_guests", "role__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["minimum_guests", "maximum_guests", "role", "effective_from"],
                name="unique_staffing_rule_period",
            )
        ]

    def __str__(self) -> str:
        return (
            f"{self.role}: {self.minimum_guests}-{self.maximum_guests} guests "
            f"({self.confirmed_count} confirmed, {self.on_call_count} on call)"
        )

    def clean(self) -> None:
        super().clean()
        if self.maximum_guests < self.minimum_guests:
            raise ValidationError(
                {"maximum_guests": "Maximum guests must be at least the minimum guests."}
            )
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError(
                {"effective_to": "Effective end date must not precede the start date."}
            )


class EmployeeAvailability(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="availability_entries",
    )
    date = models.DateField(db_index=True)
    available = models.BooleanField(default=False)
    start_time = models.TimeField(blank=True, null=True)
    end_time = models.TimeField(blank=True, null=True)
    availability_type = models.CharField(
        max_length=32,
        choices=AvailabilityType.choices,
        default=AvailabilityType.UNKNOWN,
    )
    source = models.CharField(max_length=50, default="LOCAL")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "employee__display_name"]

    def __str__(self) -> str:
        return f"{self.employee} - {self.date:%Y-%m-%d}: {self.get_availability_type_display()}"

    def save(self, *args, **kwargs):
        self.available = self.availability_type in {
            AvailabilityType.AVAILABLE_ALL_DAY,
            AvailabilityType.AVAILABLE_WINDOW,
        }
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if self.availability_type == AvailabilityType.AVAILABLE_WINDOW:
            if not self.start_time or not self.end_time:
                raise ValidationError(
                    "Available-window entries require both a start time and an end time."
                )
            if self.end_time <= self.start_time:
                raise ValidationError({"end_time": "Availability end must be after start."})
        elif self.start_time or self.end_time:
            raise ValidationError(
                "Start and end times are only valid for available-window entries."
            )


class ScheduleRun(models.Model):
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=35,
        choices=ScheduleRunStatus.choices,
        default=ScheduleRunStatus.DRAFT,
    )
    calendar_snapshot = models.ForeignKey(
        SourceSnapshot,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="calendar_schedule_runs",
    )
    availability_snapshot = models.ForeignKey(
        SourceSnapshot,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="availability_schedule_runs",
    )
    calendar_sync_run = models.ForeignKey(
        "CalendarSyncRun",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="schedule_runs",
    )
    availability_sync_run = models.ForeignKey(
        "SquareAvailabilitySyncRun",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="schedule_runs",
    )
    fairness_config = models.ForeignKey(
        "SchedulingFairnessConfig",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="schedule_runs",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_schedule_runs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="approved_schedule_runs",
    )
    approved_at = models.DateTimeField(blank=True, null=True)
    algorithm_version = models.CharField(max_length=50, default="phase2-deterministic-v1")
    notes = models.TextField(blank=True)


    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Schedule {self.start_date:%Y-%m-%d} to {self.end_date:%Y-%m-%d} ({self.status})"

    def clean(self) -> None:
        super().clean()
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must not precede the start date."})


class ScheduleAssignment(models.Model):
    schedule_run = models.ForeignKey(
        ScheduleRun,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    show = models.ForeignKey(Show, on_delete=models.PROTECT, related_name="schedule_assignments")
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="schedule_assignments",
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="schedule_assignments")
    assignment_type = models.CharField(max_length=20, choices=AssignmentType.choices)
    shift_template = models.ForeignKey(
        ShiftTemplate,
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    scheduled_paid_hours = models.DecimalField(max_digits=6, decimal_places=2)
    on_call_hours = models.DecimalField(max_digits=6, decimal_places=2)
    selection_reason = models.TextField()
    manually_overridden = models.BooleanField(default=False)
    override_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["show__date", "shift_template__position_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule_run", "show", "employee"],
                name="unique_employee_per_show_run",
            ),
            models.UniqueConstraint(
                fields=["schedule_run", "show", "shift_template"],
                name="unique_position_per_show_run",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.show} - {self.shift_template}: {self.employee}"

    def clean(self) -> None:
        super().clean()
        if self.end_datetime <= self.start_datetime:
            raise ValidationError({"end_datetime": "Assignment end must be after start."})
        if self.assignment_type == AssignmentType.ON_CALL and self.scheduled_paid_hours:
            raise ValidationError(
                {"scheduled_paid_hours": "On-call obligations cannot count as paid hours."}
            )
        if self.manually_overridden and not self.override_reason.strip():
            raise ValidationError({"override_reason": "Manual overrides require a reason."})


class SchedulingWarning(models.Model):
    schedule_run = models.ForeignKey(
        ScheduleRun,
        on_delete=models.CASCADE,
        related_name="warnings",
    )
    show = models.ForeignKey(
        Show,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="scheduling_warnings",
    )
    warning_type = models.CharField(max_length=50, choices=WarningType.choices)
    severity = models.CharField(
        max_length=12,
        choices=WarningSeverity.choices,
        default=WarningSeverity.WARNING,
    )
    message = models.TextField()
    resolved = models.BooleanField(default=False)
    resolution_note = models.TextField(blank=True)

    class Meta:
        ordering = ["show__date", "severity", "warning_type"]

    def __str__(self) -> str:
        return f"{self.warning_type}: {self.message}"


class OfficeRotationConfig(models.Model):
    seed_date = models.DateField(help_text="A Saturday that begins the two-week rotation.")
    seed_saturday_employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="office_rotation_seeds",
    )
    office_start_time = models.TimeField(default=time(9, 0))
    office_end_time = models.TimeField(default=time(17, 0))
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Office rotation from {self.seed_date} ({self.seed_saturday_employee})"

    def clean(self) -> None:
        super().clean()
        if self.seed_date.weekday() != 5:
            raise ValidationError({"seed_date": "Office rotation seed date must be a Saturday."})
        if self.seed_saturday_employee.first_name not in {"Yana", "Khrystyna"}:
            raise ValidationError({"seed_saturday_employee": "Choose either Yana or Khrystyna."})
        if self.office_end_time <= self.office_start_time:
            raise ValidationError({"office_end_time": "Office end must be after start."})


class OfficeAssignment(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="office_assignments",
    )
    date = models.DateField(db_index=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    source = models.CharField(max_length=50, default="ROTATION")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["date", "employee__display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "date"],
                name="unique_office_employee_date",
            )
        ]

    def __str__(self) -> str:
        return f"{self.employee} office - {self.date:%Y-%m-%d}"

    def clean(self) -> None:
        super().clean()
        if self.end_time <= self.start_time:
            raise ValidationError({"end_time": "Office end must be after start."})


class FiftyFiftyRotationConfig(models.Model):
    seed_employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="fifty_fifty_rotation_seeds",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"50/50 rotation starts with {self.seed_employee}"

    def clean(self) -> None:
        super().clean()
        if self.seed_employee.first_name not in {"Yana", "Kate"}:
            raise ValidationError({"seed_employee": "Choose either Yana or Kate."})


class MappingStatus(models.TextChoices):
    MAPPED_EXACT = "MAPPED_EXACT", "Mapped (Exact)"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED", "Manual Review Required"
    NOT_FOUND = "NOT_FOUND", "Not Found"
    INACTIVE = "INACTIVE", "Inactive"
    AMBIGUOUS = "AMBIGUOUS", "Ambiguous"
    MAPPED = "MAPPED", "Mapped"
    UNMAPPED = "UNMAPPED", "Unmapped"


class SquareEnvironmentChoices(models.TextChoices):
    SANDBOX = "sandbox", "Sandbox"
    PRODUCTION = "production", "Production"


class SquareSyncAuditAction(models.TextChoices):
    PRODUCTION_SYNC_PREVIEWED = "PRODUCTION_SYNC_PREVIEWED", "Production sync previewed"
    PRODUCTION_PILOT_STARTED = "PRODUCTION_PILOT_STARTED", "Production pilot started"
    PRODUCTION_PILOT_CREATED = "PRODUCTION_PILOT_CREATED", "Production pilot created"
    PRODUCTION_PILOT_VERIFIED = "PRODUCTION_PILOT_VERIFIED", "Production pilot verified"
    PRODUCTION_SYNC_STARTED = "PRODUCTION_SYNC_STARTED", "Production sync started"
    PRODUCTION_DRAFT_CREATED = "PRODUCTION_DRAFT_CREATED", "Production draft created"
    PRODUCTION_DRAFT_ALREADY_EXISTS = (
        "PRODUCTION_DRAFT_ALREADY_EXISTS",
        "Production draft already exists",
    )
    PRODUCTION_CONFLICT = "PRODUCTION_CONFLICT", "Production conflict detected"
    PRODUCTION_DRAFT_FAILED = "PRODUCTION_DRAFT_FAILED", "Production draft failed"
    PRODUCTION_DRAFT_VERIFIED = "PRODUCTION_DRAFT_VERIFIED", "Production draft verified"
    PRODUCTION_SYNC_COMPLETED = "PRODUCTION_SYNC_COMPLETED", "Production sync completed"
    PRODUCTION_DRAFT_DELETED = "PRODUCTION_DRAFT_DELETED", "Production draft deleted"
    PRODUCTION_DRAFT_DELETE_FAILED = (
        "PRODUCTION_DRAFT_DELETE_FAILED",
        "Production draft delete failed",
    )
    PRODUCTION_REMOVED_FROM_SQUARE = (
        "PRODUCTION_REMOVED_FROM_SQUARE",
        "Production roster removed from Square",
    )


class SquareEmployeeMapping(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="square_mappings",
    )
    environment = models.CharField(
        max_length=20,
        choices=SquareEnvironmentChoices.choices,
        default=SquareEnvironmentChoices.SANDBOX,
    )
    square_team_member_id = models.CharField(max_length=100, blank=True)
    square_given_name = models.CharField(max_length=100, blank=True)
    square_family_name = models.CharField(max_length=100, blank=True)
    potential_square_name = models.CharField(max_length=255, blank=True)
    match_type = models.CharField(max_length=50, blank=True)
    confidence_reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=30,
        choices=MappingStatus.choices,
        default=MappingStatus.UNMAPPED,
    )
    verified_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        ordering = ["environment", "employee__display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "environment"],
                name="unique_employee_environment_mapping",
            )
        ]

    def __str__(self) -> str:
        return f"{self.employee} ({self.environment}): {self.square_team_member_id or 'UNMAPPED'}"


class SquareRoleMapping(models.Model):
    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="square_mappings",
    )
    environment = models.CharField(
        max_length=20,
        choices=SquareEnvironmentChoices.choices,
        default=SquareEnvironmentChoices.SANDBOX,
    )
    square_job_id = models.CharField(max_length=100, blank=True)
    square_job_title = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=30,
        choices=MappingStatus.choices,
        default=MappingStatus.UNMAPPED,
    )

    verified_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["environment", "role__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["role", "environment"],
                name="unique_role_environment_mapping",
            )
        ]

    def __str__(self) -> str:
        job_label = self.square_job_title or self.square_job_id or "UNMAPPED"
        return f"{self.role} ({self.environment}): {job_label}"



class SquareLocationMapping(models.Model):
    environment = models.CharField(
        max_length=20,
        choices=SquareEnvironmentChoices.choices,
        default=SquareEnvironmentChoices.SANDBOX,
    )
    square_location_id = models.CharField(max_length=100)
    location_name = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    verified_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["environment", "location_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["environment", "square_location_id"],
                name="unique_environment_square_location",
            )
        ]

    def __str__(self) -> str:
        return f"{self.location_name} ({self.environment}): {self.square_location_id}"


class SquareSyncAuditLog(models.Model):
    action_type = models.CharField(max_length=50, choices=SquareSyncAuditAction.choices)
    environment = models.CharField(
        max_length=20,
        choices=SquareEnvironmentChoices.choices,
        default=SquareEnvironmentChoices.PRODUCTION,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="square_sync_audit_logs",
    )
    schedule_run = models.ForeignKey(
        ScheduleRun,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="square_sync_audit_logs",
    )
    assignment = models.ForeignKey(
        ScheduleAssignment,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="square_sync_audit_logs",
    )
    square_scheduled_shift_id = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} - {self.action_type} ({self.environment})"


class SchedulingFairnessConfig(models.Model):
    name = models.CharField(max_length=100, default="Default Spirit Fairness Config")
    active = models.BooleanField(default=True)

    # Confirmed Ranking Weights (must sum to 1.00)
    opportunity_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.30")
    )
    hours_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.25")
    )
    role_opportunity_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.10")
    )
    confirmed_shift_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.10")
    )
    weekend_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.08")
    )
    rest_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.05")
    )
    reliability_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.05")
    )
    performance_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.04")
    )
    role_fit_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.03")
    )

    # On-Call Ranking Weights (must sum to 1.00)
    on_call_count_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.40")
    )
    on_call_hours_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.20")
    )
    on_call_opportunity_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.15")
    )
    on_call_confirmed_workload_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.10")
    )
    on_call_weekend_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.05")
    )
    on_call_reliability_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.05")
    )
    on_call_role_fit_weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.05")
    )

    # History Windows (Days)
    recent_hours_window_days = models.PositiveIntegerField(default=28)
    confirmed_shift_window_days = models.PositiveIntegerField(default=28)
    on_call_window_days = models.PositiveIntegerField(default=28)
    weekend_window_days = models.PositiveIntegerField(default=56)

    # Defaults for missing data
    default_reliability = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.50")
    )
    default_performance = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.50")
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-active", "-updated_at"]

    def __str__(self) -> str:
        return f"{self.name} ({'Active' if self.active else 'Inactive'})"

    def clean(self) -> None:
        super().clean()
        confirmed_total = (
            self.opportunity_weight
            + self.hours_weight
            + self.role_opportunity_weight
            + self.confirmed_shift_weight
            + self.weekend_weight
            + self.rest_weight
            + self.reliability_weight
            + self.performance_weight
            + self.role_fit_weight
        )
        if abs(confirmed_total - Decimal("1.00")) > Decimal("0.001"):
            raise ValidationError(
                {
                    "opportunity_weight": (
                        f"Confirmed ranking weights must sum to 1.00 (currently {confirmed_total})."
                    )
                }
            )
        on_call_total = (
            self.on_call_count_weight
            + self.on_call_hours_weight
            + self.on_call_opportunity_weight
            + self.on_call_confirmed_workload_weight
            + self.on_call_weekend_weight
            + self.on_call_reliability_weight
            + self.on_call_role_fit_weight
        )
        if abs(on_call_total - Decimal("1.00")) > Decimal("0.001"):
            raise ValidationError(
                {
                    "on_call_count_weight": (
                        f"On-call ranking weights must sum to 1.00 (currently {on_call_total})."
                    )
                }
            )

    @classmethod
    def get_active_config(cls) -> "SchedulingFairnessConfig":
        config = cls.objects.filter(active=True).first()
        if not config:
            config = cls.objects.create(name="Default Spirit Fairness Config", active=True)
        return config


class EmployeeSchedulingPreference(models.Model):
    employee = models.OneToOneField(
        Employee,
        on_delete=models.CASCADE,
        related_name="scheduling_preference",
    )
    target_hours = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Monthly target confirmed paid hours.",
    )
    priority_enabled = models.BooleanField(default=True)
    preferred_role = models.ForeignKey(
        "Role",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="preferring_employees",
        help_text=(
            "Where a cross-trained employee would rather work. Applied as a ranking "
            "nudge only: it never makes anyone eligible or ineligible, and scarce-skill "
            "cover still comes first."
        ),
    )
    effective_from = models.DateField(blank=True, null=True)
    effective_to = models.DateField(blank=True, null=True)
    notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"{self.employee}: Target {self.target_hours or 0} hrs/mo"


class SchedulingFairnessSnapshot(models.Model):
    schedule_run = models.ForeignKey(
        ScheduleRun,
        on_delete=models.CASCADE,
        related_name="fairness_snapshots",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="fairness_snapshots",
    )
    role = models.ForeignKey(
        Role,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="fairness_snapshots",
    )
    eligible_opportunities = models.PositiveIntegerField(default=0)
    confirmed_opportunities = models.PositiveIntegerField(default=0)
    opportunity_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00")
    )
    recent_actual_hours = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00")
    )
    recent_confirmed_shifts = models.PositiveIntegerField(default=0)
    recent_on_call_assignments = models.PositiveIntegerField(default=0)
    recent_on_call_hours = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00")
    )
    recent_weekend_shifts = models.PositiveIntegerField(default=0)
    consecutive_nights = models.PositiveIntegerField(default=0)
    role_opportunities = models.PositiveIntegerField(default=0)
    target_hours = models.DecimalField(
        max_digits=6, decimal_places=2, blank=True, null=True
    )
    projected_hours = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00")
    )
    reliability_score = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.50")
    )
    performance_score = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.50")
    )
    role_fit_score = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.50")
    )
    target_hours_adjustment = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0.00")
    )
    confirmed_fair_score = models.DecimalField(
        max_digits=5, decimal_places=3, default=Decimal("0.000")
    )
    on_call_fair_score = models.DecimalField(
        max_digits=5, decimal_places=3, default=Decimal("0.000")
    )
    selected = models.BooleanField(default=False)
    selection_reason = models.TextField(blank=True)
    score_breakdown = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["schedule_run", "employee__display_name"]

    def __str__(self) -> str:
        return f"{self.employee} - {self.schedule_run}: Score {self.confirmed_fair_score}"



class TimeOffStatus(models.TextChoices):
    """Square's own states. Only APPROVED keeps somebody off the schedule.

    A pending request is a question, not a decision - blocking on it would silently
    over-rule a manager who has not answered yet, and declining it later would leave
    a roster that was built around a refusal.
    """

    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    DECLINED = "DECLINED", "Declined"
    CANCELLED = "CANCELLED", "Cancelled"


class TimeOffSource(models.TextChoices):
    SQUARE = "SQUARE", "Square"
    MANUAL = "MANUAL", "Entered here"


class EmployeeTimeOff(models.Model):
    """An approved or pending absence, mirroring Square's Time off page.

    Stored as a date range rather than per-date rows: that is how Square holds it and
    how a request is made ("the 12th to the 15th"), and expanding it into rows would
    have to be re-expanded every time the range is edited.
    """

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="time_off")
    start_date = models.DateField()
    end_date = models.DateField()
    # Null for a whole-day absence, which is the common case.
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=TimeOffStatus.choices, default=TimeOffStatus.PENDING
    )
    reason = models.CharField(max_length=200, blank=True)
    source = models.CharField(
        max_length=20, choices=TimeOffSource.choices, default=TimeOffSource.MANUAL
    )
    # Square's own id where one is available, so a re-sync updates rather than duplicates.
    square_time_off_id = models.CharField(max_length=64, blank=True)
    # Square shows these three as their own columns and management reads them, so they
    # are mirrored verbatim rather than recomputed - "7d" approved against a "6d"
    # request is a discrepancy worth seeing, and deriving it here would hide it.
    requested_time = models.CharField(max_length=20, blank=True)
    approved_all_day = models.CharField(max_length=20, blank=True)
    approved_partial = models.CharField(max_length=20, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-start_date", "employee__display_name")
        indexes = [models.Index(fields=["start_date", "end_date", "status"])]

    def __str__(self) -> str:
        return f"{self.employee.display_name} {self.start_date}..{self.end_date} ({self.status})"

    def clean(self):
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must not precede the start date."})

    @property
    def is_whole_day(self) -> bool:
        return self.start_time is None and self.end_time is None

    def covers(self, day, shift_start=None, shift_end=None) -> bool:
        """Whether this absence blocks a shift on `day`.

        A whole-day absence blocks anything that day. A partial one only blocks a
        shift it actually overlaps, so somebody off for a morning appointment can
        still work that evening's show.
        """
        if not (self.start_date <= day <= self.end_date):
            return False
        if self.is_whole_day or shift_start is None or shift_end is None:
            return True
        return self.start_time < shift_end and self.end_time > shift_start
