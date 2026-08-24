from django.contrib import admin

from .models import Employee, EmployeeRole, Role, SquareLocation


class EmployeeRoleInline(admin.TabularInline):
    model = EmployeeRole
    extra = 0


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "active",
        "spirit_only_employment",
        "has_square_mapping",
    )
    list_filter = ("active", "spirit_only_employment")
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

