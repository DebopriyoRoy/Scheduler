from django.contrib import admin

from .models import (
    Employee,
    EmployeeAvailability,
    EmployeeRole,
    FiftyFiftyRotationConfig,
    OfficeAssignment,
    OfficeRotationConfig,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    SchedulingWarning,
    ShiftTemplate,
    Show,
    SquareLocation,
    StaffingRule,
)


class EmployeeRoleInline(admin.TabularInline):
    model = EmployeeRole
    extra = 0


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "active",
        "spirit_only_employment",
        "employment_priority",
        "excluded_from_automatic_scheduling",
        "has_square_mapping",
    )
    list_filter = ("active", "spirit_only_employment", "excluded_from_automatic_scheduling")
    search_fields = ("display_name", "first_name", "last_name", "square_team_member_id")
    inlines = (EmployeeRoleInline,)

    @admin.display(boolean=True, description="Square mapped")
    def has_square_mapping(self, employee: Employee) -> bool:
        return bool(employee.square_team_member_id)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "has_square_mapping", "updated_at")
    search_fields = ("name", "square_job_id")

    @admin.display(boolean=True, description="Square mapped")
    def has_square_mapping(self, role: Role) -> bool:
        return bool(role.square_job_id)


@admin.register(SquareLocation)
class SquareLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "square_location_id", "active")
    list_filter = ("active",)
    search_fields = ("name", "square_location_id")


@admin.register(Show)
class ShowAdmin(admin.ModelAdmin):
    list_display = ("title", "date", "start_time", "expected_guests", "active")
    list_filter = ("active", "date", "source")
    search_fields = ("title", "venue", "external_id")


@admin.register(ShiftTemplate)
class ShiftTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "role", "assignment_type", "start_time", "end_time", "active")
    list_filter = ("role", "assignment_type", "active")


@admin.register(StaffingRule)
class StaffingRuleAdmin(admin.ModelAdmin):
    list_display = ("role", "minimum_guests", "maximum_guests", "confirmed_count", "on_call_count")
    list_filter = ("role", "active")


@admin.register(EmployeeAvailability)
class EmployeeAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("employee", "date", "availability_type", "start_time", "end_time", "source")
    list_filter = ("availability_type", "date", "source")
    search_fields = ("employee__display_name",)


@admin.register(ScheduleRun)
class ScheduleRunAdmin(admin.ModelAdmin):
    list_display = ("id", "start_date", "end_date", "status", "created_at", "approved_at")
    list_filter = ("status",)


@admin.register(ScheduleAssignment)
class ScheduleAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "schedule_run",
        "show",
        "shift_template",
        "employee",
        "assignment_type",
        "manually_overridden",
    )
    list_filter = ("assignment_type", "manually_overridden")
    search_fields = ("employee__display_name", "show__title")


@admin.register(SchedulingWarning)
class SchedulingWarningAdmin(admin.ModelAdmin):
    list_display = ("schedule_run", "show", "warning_type", "severity", "resolved")
    list_filter = ("severity", "warning_type", "resolved")


admin.site.register(OfficeRotationConfig)
admin.site.register(OfficeAssignment)
admin.site.register(FiftyFiftyRotationConfig)
