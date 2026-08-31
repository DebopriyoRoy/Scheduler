"""Management's own staff list, by department.

The structure and the order within each department are exactly as management wrote
them; both carry meaning and neither is alphabetical. People genuinely appear in
several departments - Yana is in five - and that is not duplication to be cleaned up.

Departments are not scheduling roles. Nothing here changes who the engine can roster.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from scheduling.models import Department, Employee, EmployeeDepartment

# Management writes "Surname Firstname"; the database holds "Firstname Surname", and
# a few names are spelled differently in each. Resolving by hand beats a fuzzy match
# that might quietly attach a shift to the wrong person.
# Every name management writes, mapped to the name the database holds. This is
# exhaustive on purpose: an earlier version flipped "Surname Firstname" whenever it
# could not find a match, which turned "Debopriyo Roy" - already the right way round -
# into "Roy Debopriyo". Guessing at somebody's name is not worth the convenience.
ALIASES = {
    # Written surname-first
    "AL-Lahout Svitlana": "Svitlana Al-Lahut",
    "Al-Lahout Svitlana": "Svitlana Al-Lahut",
    "Al-Deir Butros": "Butros Al-Deir",
    "Butros Al-Deir": "Butros Al-Deir",
    "Barron Charlie": "Charlie Barron",
    "Bobbit Neil": "Neil Bobbitt",
    "Bobbitt Neil": "Neil Bobbitt",
    "Broderick Blaine": "Blane Broderick",
    "Collier Melanie": "Melanie Collier",
    "Dickson Joleen": "Joleen Dickson",
    "Gordon Daniel": "Daniel Gordon",
    "Griffan Katie": "Kate Griffin",
    "Halley Patrice": "Patrice Halley",
    "Halley-Green Michael": "Michael Halley-Green",
    "Halley-Green, Lily": "Lily Halley-Green",
    "Harris John": "John Harris",
    "Hillier Bridget": "Bridget Hillier",
    "James Brittany": "Brittany James",
    "James Lukas": "Lukas James",
    "Kachensseva Mariia (Marsh)": "Mariia Kashentseva",
    "King Randy": "Randy King",
    "Lundrigan Ash": "Ash Lundrigan",
    "Lundrigan William": "William Lundrigan",
    "Martynova Olena": "Olena Martynova",
    "O'Reilly Colleen": "Colleen O'Reilly",
    "Pasechniuk Maryna": "Maryna Pasechniuk",
    "Pasechniuk Yana": "Yana Pasechniuk",
    "Penny Linda": "Linda Penney",
    "Phillpot Paul Cleaning Office": "Paul Philpot",
    "Polski Maksym": "Maks Plsky",
    "Pynn Jackie": "Jackie Pynn",
    "Pynn Montana": "Montana Pynn",
    "Pynn Morgan": "Morgan Pynn",
    "Rittwage Molly": "Molly Rittwage",
    "Samson Zachary": "Zachary Samson",
    # Ambiguous: the 50/50 list is otherwise surname-first, so this reads as
    # surname Stacey, forename Taylor. Worth confirming with management.
    "Stacey Taylor": "Taylor Stacey",
    "Stuckless Leslie": "Leslie Stuckless",
    "Talbot Emily": "Emily Talbot",
    "Tucker Colton": "Colton Tucker",
    "Wall James (Jordon)": "Jordan Wall",
    "Zavadetska Mariia": "Marila Zavadetska",
    # Written forename-first already, and must not be flipped
    "Debopriyo Roy": "Debopriyo Roy",
    "Deborah Sweetapple": "Deborah Sweetapple",
    "Keith Power- ADMIN Office": "Keith Power",
    "Khrystyna Zavadetska": "Khrystyna Zavadetska",
    "Adam Blackwood": "Adam Blackwood",
}

DEPARTMENTS = [
    ("Marketing/Screech in", ["Khrystyna Zavadetska", "Pasechniuk Yana", "Hillier Bridget"]),
    ("Bar Manager", ["Harris John"]),
    (
        "Bartenders at Spirit",
        [
            "AL-Lahout Svitlana",
            "Butros Al-Deir",
            "Dickson Joleen",
            "Gordon Daniel",
            "James Brittany",
        ],
    ),
    (
        "ACC Bartenders",
        [
            "Al-Deir Butros",
            "Al-Lahout Svitlana",
            "Bobbit Neil",
            "Halley Patrice",
            "King Randy",
            "Pynn Montana",
        ],
    ),
    ("Server Manager", ["Deborah Sweetapple"]),
    (
        "Server",
        [
            "AL-Lahout Svitlana",
            "Bobbitt Neil",
            "Collier Melanie",
            "James Lukas",
            "Khrystyna Zavadetska",
            "Martynova Olena",
            "Pasechniuk Yana",
            "Polski Maksym",
            "Pynn Jackie",
            "Penny Linda",
            "Pynn Morgan",
            "Rittwage Molly",
            "Talbot Emily",
        ],
    ),
    (
        "Flyers/Screech in Helper",
        [
            "Halley-Green Michael",
            "Khrystyna Zavadetska",
            "Pasechniuk Yana",
            "Halley-Green, Lily",
        ],
    ),
    ("50/50", ["Griffan Katie", "Pasechniuk Yana", "Stacey Taylor"]),
    (
        "Kitchen",
        [
            "Barron Charlie",
            "Kachensseva Mariia (Marsh)",
            "Lundrigan Ash",
            "Lundrigan William",
            "O'Reilly Colleen",
            "Samson Zachary",
            "Stuckless Leslie",
            "Wall James (Jordon)",
            "Zavadetska Mariia",
        ],
    ),
    (
        "Office/reservations",
        [
            "Debopriyo Roy",
            "Hillier Bridget",
            "Khrystyna Zavadetska",
            "Pasechniuk  Maryna",
            "Pasechniuk Yana",
            "Keith Power- ADMIN Office",
        ],
    ),
    (
        "Cleaners/ Maintenance",
        ["Broderick Blaine", "Phillpot Paul Cleaning Office", "Tucker Colton"],
    ),
    ("Tech", ["Adam Blackwood"]),
]


def resolve_name(written: str) -> str:
    """Turn management's spelling into the database's."""
    written = " ".join(written.split())
    if written in ALIASES:
        return ALIASES[written]
    match = Employee.objects.filter(display_name__iexact=written).first()
    if match is not None:
        return match.display_name
    # No guessing beyond the table above: an unmapped name is used exactly as written,
    # which shows up plainly on the page rather than silently becoming someone else.
    return written


class Command(BaseCommand):
    help = "Seed the department structure and its membership."

    @transaction.atomic
    def handle(self, *args, **options):
        created_people, created_links = 0, 0
        for order, (name, members) in enumerate(DEPARTMENTS, start=1):
            department, _ = Department.objects.update_or_create(
                name=name, defaults={"display_order": order}
            )
            keep = []
            for position, written in enumerate(members, start=1):
                resolved = resolve_name(written)
                employee = Employee.objects.filter(display_name__iexact=resolved).first()
                if employee is None:
                    # Kitchen, office and cleaning staff are not rostered by this
                    # application. They are created excluded so they appear on the staff
                    # list without ever entering the scheduling pool or being counted
                    # as someone whose availability is missing.
                    employee = Employee.objects.create(
                        display_name=resolved,
                        first_name=resolved.split()[0],
                        active=True,
                        excluded_from_automatic_scheduling=True,
                    )
                    created_people += 1
                    self.stdout.write(f"  created {resolved} (not schedulable)")
                _, made = EmployeeDepartment.objects.update_or_create(
                    employee=employee,
                    department=department,
                    defaults={"listing_order": position},
                )
                created_links += made
                keep.append(employee.pk)
            # Anyone dropped from the written list leaves the department.
            EmployeeDepartment.objects.filter(department=department).exclude(
                employee_id__in=keep
            ).delete()

        Department.objects.exclude(name__in=[n for n, _ in DEPARTMENTS]).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Departments ready: {Department.objects.count()} departments, "
                f"{EmployeeDepartment.objects.count()} memberships, "
                f"{created_people} new staff created, {created_links} new memberships."
            )
        )
