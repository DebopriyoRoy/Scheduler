from scheduling.models import Show

POSITION_CODES = (
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
