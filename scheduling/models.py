from datetime import time
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

MAX_THEATRE_CAPACITY = 175
DEFAULT_EXPECTED_GUESTS = 100


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
    ROLE_CONFIGURATION_ERROR = "ROLE_CONFIGURATION_ERROR", "Role configuration error"
    EVENT_STAFFING_REVIEW_REQUIRED = (
        "EVENT_STAFFING_REVIEW_REQUIRED",
        "Event staffing review required",
    )
    PRIVATE_EVENT_STAFFING_REVIEW_REQUIRED = (
        "PRIVATE_EVENT_STAFFING_REVIEW_REQUIRED",
        "Private event staffing review required",
    )


class Employee(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    display_name = models.CharField(max_length=201, unique=True)
    active = models.BooleanField(default=True)
    square_team_member_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
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
        if self.seed_saturday_employee.display_name not in {"Yana", "Khrystyna"}:
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
        if self.seed_employee.display_name not in {"Yana", "Kate"}:
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

