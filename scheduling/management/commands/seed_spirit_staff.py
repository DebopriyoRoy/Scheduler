from dataclasses import dataclass

from django.core.management.base import BaseCommand
from django.db import transaction

from scheduling.models import Employee, EmployeeRole, Role


@dataclass(frozen=True)
class StaffMember:
    display_name: str
    roles: tuple[tuple[str, int], ...]
    spirit_only_employment: bool = False
    employment_priority: int = 0


STAFF = (
    StaffMember("Joleen Dickson", (("Bartender", 5), ("Server", 5))),
    StaffMember("Jackie Pynn", (("Bartender", 4), ("Server", 4)), True, 1),
    StaffMember("Olena", (("Server", 3),), True, 1),
    StaffMember("Yana", (("Server", 3), ("50/50", 3))),
    StaffMember("Kate", (("Server", 3), ("50/50", 3))),
    StaffMember("Molly Rittwage", (("Server", 3),)),
    StaffMember("Linda Penney", (("Server", 3),)),
    StaffMember("Daniel", (("Bartender", 3),)),
    StaffMember("Butros", (("Bartender", 3),)),
    StaffMember("Svitlana", (("Bartender", 3), ("Server", 3))),
    StaffMember("Patrice", (("Bartender", 3),)),
    StaffMember("Montana", (("Bartender", 3),)),
    StaffMember("Neil Bobbit", (("Bartender", 3),)),
    StaffMember("Brittany James", (("Bartender", 3),)),
    StaffMember("Khrystyna", (("Busser", 3),)),
    StaffMember("Emily", (("Busser", 3),)),
    StaffMember("Maks Plsky", (("Busser", 3),)),
)


class Command(BaseCommand):
    help = "Seed the approved Phase 1 Spirit staffing roster and capability levels."

    @transaction.atomic
    def handle(self, *args, **options):
        role_names = ("Server", "Bartender", "Busser", "50/50")
        roles = {name: Role.objects.get_or_create(name=name)[0] for name in role_names}
        created_count = 0
        updated_count = 0

        for member in STAFF:
            name_parts = member.display_name.split(" ", 1)
            defaults = {
                "first_name": name_parts[0],
                "last_name": name_parts[1] if len(name_parts) > 1 else "",
                "active": True,
                "spirit_only_employment": member.spirit_only_employment,
                "employment_priority": member.employment_priority,
                "excluded_from_automatic_scheduling": False,
            }
            employee, created = Employee.objects.update_or_create(
                display_name=member.display_name,
                defaults=defaults,
            )
            created_count += int(created)
            updated_count += int(not created)
            for role_name, level in member.roles:
                EmployeeRole.objects.update_or_create(
                    employee=employee,
                    role=roles[role_name],
                    defaults={"capability_level": level, "active": True},
                )
            EmployeeRole.objects.filter(employee=employee).exclude(
                role__name__in=[role_name for role_name, _ in member.roles]
            ).update(active=False)

        assignment_count = EmployeeRole.objects.filter(
            employee__display_name__in=[member.display_name for member in STAFF]
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Spirit staff seed complete: {created_count} created, {updated_count} updated, "
                f"{assignment_count} role assignments."
            )
        )
