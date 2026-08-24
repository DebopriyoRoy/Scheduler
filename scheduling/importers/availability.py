import csv
import io
from dataclasses import asdict, dataclass
from datetime import datetime

from django.db import transaction

from scheduling.models import AvailabilityType, Employee, EmployeeAvailability

REQUIRED_COLUMNS = {"employee", "date", "available", "start_time", "end_time", "notes"}
TRUE_VALUES = {"1", "true", "yes", "y", "available", "all_day"}
FALSE_VALUES = {"0", "false", "no", "n", "unavailable"}
UNKNOWN_VALUES = {"", "unknown", "?"}


class AvailabilityCSVError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class AvailabilityImportRow:
    employee_id: int
    employee: str
    date: str
    availability_type: str
    start_time: str
    end_time: str
    notes: str

    def session_dict(self) -> dict:
        return asdict(self)


def _parse_time(value: str, row_number: int, field: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"row {row_number}: {field} must use HH:MM format") from exc


def parse_availability_csv(data: bytes | str) -> list[AvailabilityImportRow]:
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AvailabilityCSVError(["The CSV must be UTF-8 encoded."]) from exc
    else:
        text = data
    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise AvailabilityCSVError([f"Missing required column(s): {', '.join(sorted(missing))}"])

    employees = {employee.display_name.casefold(): employee for employee in Employee.objects.all()}
    errors: list[str] = []
    parsed: list[AvailabilityImportRow] = []
    seen: set[tuple[int, str]] = set()
    for row_number, row in enumerate(reader, start=2):
        name = (row.get("employee") or "").strip()
        employee = employees.get(name.casefold())
        if not employee:
            errors.append(f"row {row_number}: unknown employee {name!r}")
            continue
        date_text = (row.get("date") or "").strip()
        try:
            datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            errors.append(f"row {row_number}: date must use YYYY-MM-DD format")
            continue
        key = (employee.pk, date_text)
        if key in seen:
            errors.append(f"row {row_number}: duplicate employee/date entry for {name}")
            continue
        seen.add(key)
        available = (row.get("available") or "").strip().casefold()
        start_text = (row.get("start_time") or "").strip()
        end_text = (row.get("end_time") or "").strip()
        try:
            start = _parse_time(start_text, row_number, "start_time")
            end = _parse_time(end_text, row_number, "end_time")
            if available in TRUE_VALUES:
                if bool(start) != bool(end):
                    raise ValueError(
                        f"row {row_number}: provide both start_time and end_time or neither"
                    )
                if start and end and end <= start:
                    raise ValueError(f"row {row_number}: end_time must be after start_time")
                availability_type = (
                    AvailabilityType.AVAILABLE_WINDOW
                    if start
                    else AvailabilityType.AVAILABLE_ALL_DAY
                )
            elif available in FALSE_VALUES:
                if start or end:
                    raise ValueError(f"row {row_number}: unavailable entries cannot contain times")
                availability_type = AvailabilityType.UNAVAILABLE
            elif available in UNKNOWN_VALUES:
                if start or end:
                    raise ValueError(f"row {row_number}: unknown entries cannot contain times")
                availability_type = AvailabilityType.UNKNOWN
            else:
                raise ValueError(f"row {row_number}: available must be yes, no, or unknown")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        parsed.append(
            AvailabilityImportRow(
                employee_id=employee.pk,
                employee=employee.display_name,
                date=date_text,
                availability_type=availability_type,
                start_time=start_text,
                end_time=end_text,
                notes=(row.get("notes") or "").strip(),
            )
        )
    if errors:
        raise AvailabilityCSVError(errors)
    if not parsed:
        raise AvailabilityCSVError(["The CSV contains no data rows."])
    return parsed


@transaction.atomic
def import_availability_rows(rows: list[dict] | list[AvailabilityImportRow]) -> int:
    count = 0
    for raw in rows:
        row = raw if isinstance(raw, AvailabilityImportRow) else AvailabilityImportRow(**raw)
        EmployeeAvailability.objects.update_or_create(
            employee_id=row.employee_id,
            date=row.date,
            defaults={
                "availability_type": row.availability_type,
                "start_time": row.start_time or None,
                "end_time": row.end_time or None,
                "source": "CSV",
                "notes": row.notes,
            },
        )
        count += 1
    return count
