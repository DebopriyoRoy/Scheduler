from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from scheduling.importers.availability import (
    AvailabilityCSVError,
    import_availability_rows,
    parse_availability_csv,
)


class Command(BaseCommand):
    help = "Validate and atomically import employee availability from CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=Path)

    def handle(self, *args, **options):
        csv_file = options["csv_file"]
        try:
            rows = parse_availability_csv(csv_file.read_bytes())
            count = import_availability_rows(rows)
        except (OSError, AvailabilityCSVError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Imported {count} availability rows."))
