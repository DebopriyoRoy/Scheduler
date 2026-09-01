from django.utils import timezone

from scheduling.models import Show

POSITION_CODES = (
    # The Server Manager was missing from both exports, so a printed rota showed one
    # fewer person than the schedule on screen actually rosters.
    "server-manager",
    "lead-server",
    "server-2",
    "server-3",
    "on-call-server",
    "bartender",
    "on-call-bartender",
    "busser",
    "fifty-fifty",
)


def show_export_rows(schedule_run):
    shows = Show.objects.filter(
        active=True,
        date__range=(schedule_run.start_date, schedule_run.end_date),
    )
    rows = []
    for show in shows:
        assignments = {
            item.shift_template.code: item
            for item in schedule_run.assignments.filter(show=show).select_related(
                "employee", "shift_template"
            )
        }
        warnings = schedule_run.warnings.filter(show=show, resolved=False)
        rows.append((show, assignments, list(warnings)))
    return rows


def cell_lines(assignment) -> tuple[str, str]:
    """A person and the hours they actually work, for one cell of the rota grid.

    The exports listed names alone, which is not a rota anybody can work from: the
    whole point of the call times is that Server 1 comes in at three and Server 3 at
    half five. The hours here are the assignment's own, so a shift trimmed to somebody's
    availability prints the hours they are really in for, not the position's default.
    """
    if assignment is None:
        return ("SHORTAGE", "")
    start = timezone.localtime(assignment.start_datetime)
    end = timezone.localtime(assignment.end_datetime)
    return (assignment.employee.display_name, f"{start:%H:%M}-{end:%H:%M}")


def cell_text(assignment) -> str:
    name, hours = cell_lines(assignment)
    return f"{name}\n{hours}" if hours else name


def assumptions(schedule_run) -> list[tuple[str, str]]:
    return [
        ("Algorithm", f"{schedule_run.algorithm_version}; deterministic, never random."),
        ("Guest count", "Missing expected guest counts default to 100 and are explicitly warned."),
        ("Capacity", "Standard theatre capacity is 175; higher values require an override reason."),
        ("Availability", "Unknown availability is always ineligible and never assumed available."),
        (
            "Coverage",
            "Bartender positions are filled before ordinary servers to protect bar coverage.",
        ),
        ("Priority", "No employee receives a scheduling preference; all staff rank equally."),
        (
            "50/50",
            "Yana and Kate alternate when both are eligible; interruptions do not advance it.",
        ),
        ("On call", "On-call obligations are tracked separately and do not count as paid hours."),
        (
            "Approval",
            "Approval is local. This application does not publish or write shifts to Square.",
        ),
    ]
