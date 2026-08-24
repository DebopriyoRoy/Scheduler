from datetime import date, time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scheduling.models import (
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    FiftyFiftyRotationConfig,
    OfficeRotationConfig,
    Show,
)

DEMO_SHOWS = (
    (date(2026, 9, 12), "Forever Country… In the Key of Spirit"),
    (date(2026, 9, 18), "Forever Country… In the Key of Spirit"),
    (date(2026, 9, 19), "Shift Happens"),
    (date(2026, 9, 25), "Forever Country… In the Key of Spirit"),
    (date(2026, 10, 2), "Forever Country… In the Key of Spirit"),
    (date(2026, 10, 3), "Shift Happens"),
)


class Command(BaseCommand):
    help = "Populate isolated development-only sample shows, availability, and rotation seeds."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo seeding is disabled unless DEBUG=True.")
        target_dates = [show_date for show_date, _ in DEMO_SHOWS]
        if Show.objects.exclude(source=Show.Source.DEMO).filter(date__in=target_dates).exists():
            raise CommandError(
                "Non-demo shows already exist on target dates. Refusing to mix demo and real data."
            )
        non_demo_availability = EmployeeAvailability.objects.exclude(source="DEMO").filter(
            date__in=target_dates
        )
        if non_demo_availability.exists():
            raise CommandError(
                "Non-demo availability already exists on target dates. Refusing to overwrite it."
            )
        call_command("seed_spirit_staff", verbosity=0)
        call_command("seed_scheduling_config", verbosity=0)
        for show_date, title in DEMO_SHOWS:
            Show.objects.update_or_create(
                external_id=f"DEMO-{show_date.isoformat()}",
                defaults={
                    "title": title,
                    "date": show_date,
                    "start_time": time(18, 30),
                    "end_time": time(22, 30),
                    "expected_guests": 100,
                    "requires_service_staff": True,
                    "requires_50_50": True,
                    "source": Show.Source.DEMO,
                    "notes": "DEMO DATA — safe to remove; not imported from Production.",
                    "active": True,
                },
            )
        for employee in Employee.objects.filter(active=True):
            for show_date in target_dates:
                EmployeeAvailability.objects.update_or_create(
                    employee=employee,
                    date=show_date,
                    defaults={
                        "availability_type": AvailabilityType.AVAILABLE_ALL_DAY,
                        "start_time": None,
                        "end_time": None,
                        "source": "DEMO",
                        "notes": "DEMO DATA — all-day sample availability.",
                    },
                )
        yana = Employee.objects.get(display_name="Yana")
        office = OfficeRotationConfig.objects.first()
        OfficeRotationConfig.objects.update_or_create(
            pk=office.pk if office else None,
            defaults={
                "seed_date": date(2026, 9, 12),
                "seed_saturday_employee": yana,
                "office_start_time": time(9),
                "office_end_time": time(17),
            },
        )
        fifty = FiftyFiftyRotationConfig.objects.first()
        FiftyFiftyRotationConfig.objects.update_or_create(
            pk=fifty.pk if fifty else None,
            defaults={"seed_employee": yana},
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"DEMO ONLY: seeded {len(DEMO_SHOWS)} shows, "
                f"{Employee.objects.filter(active=True).count() * len(DEMO_SHOWS)} availability "
                "rows, and both rotation seeds."
            )
        )
