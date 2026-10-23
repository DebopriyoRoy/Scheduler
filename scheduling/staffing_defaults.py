"""Spirit's standard staffing rules and call times, as management set them.

These are only the starting values. The live rules are rows in the database that
management edit on the Staffing rules page; this module is what a fresh install is
given and what "Reset to Spirit defaults" puts back. Plain data, no model imports, so
migrations can use it too.

Crew size per role, for a guest count G:

    confirmed = max(minimum, ceil((G - stretch) / guests_per_person)), capped at maximum
    on call   = on_call_base + one more for each step the confirmed count has reached

guests_per_person of 0 means a fixed crew of `minimum`, whatever the house.
At 75-80 guests this gives the standard crew: 3 servers + 1 on call, 1 bartender + 1
on call, 1 busser, 1 50/50 and the Server Manager.
"""

# role name: (minimum, guests_per_person, stretch, maximum, on_call_base, on_call_steps)
ROLE_RULES = {
    "Server Manager": (1, 0, 5, None, 0, ""),
    "Server": (3, 25, 5, None, 1, "5, 7"),
    "Bartender": (1, 75, 5, None, 1, "3"),
    "Busser": (1, 100, 5, None, 0, ""),
    "50/50": (1, 0, 5, None, 0, ""),
}

ROLE_ORDER = ("Server Manager", "Server", "Bartender", "Busser", "50/50")

REGULAR = "REGULAR"
DWIGHTS = "DWIGHTS"

# Shows with doors at 6:30 pm. Management's own clock times, October 2026.
REGULAR_CALL_TIMES = {
    "server-manager": ("14:00", "21:00"),
    "lead-server": ("15:00", "21:00"),
    "server-2": ("16:00", "21:30"),
    "server-3": ("17:30", "23:00"),
    "server-4": ("17:30", "23:00"),
    "server-5": ("17:30", "23:00"),
    "server-6": ("17:30", "23:00"),
    "server-7": ("17:30", "23:00"),
    "on-call-server": ("18:15", "23:00"),
    "on-call-server-2": ("18:15", "23:00"),
    "on-call-server-3": ("18:15", "23:00"),
    "bartender": ("16:00", "21:30"),
    "bartender-2": ("16:00", "21:30"),
    "bartender-3": ("16:00", "21:30"),
    "on-call-bartender": ("17:30", "23:00"),
    "on-call-bartender-2": ("17:30", "23:00"),
    "busser": ("18:30", "23:00"),
    "busser-2": ("18:30", "23:00"),
    "fifty-fifty": ("17:45", "20:30"),
}

# Dwight's Wedding: doors 6:00 pm, its own timetable.
DWIGHTS_CALL_TIMES = {
    "server-manager": ("14:00", "21:00"),
    "lead-server": ("15:00", "21:00"),
    "server-2": ("15:30", "21:30"),
    "server-3": ("17:00", "22:30"),
    "server-4": ("17:00", "22:30"),
    "server-5": ("17:00", "22:30"),
    "server-6": ("17:00", "22:30"),
    "server-7": ("17:00", "22:30"),
    "on-call-server": ("19:00", "22:30"),
    "on-call-server-2": ("19:00", "22:30"),
    "on-call-server-3": ("19:00", "22:30"),
    "bartender": ("16:00", "22:30"),
    "bartender-2": ("16:00", "22:30"),
    "bartender-3": ("16:00", "22:30"),
    "on-call-bartender": ("17:00", "22:30"),
    "on-call-bartender-2": ("17:00", "22:30"),
    "busser": ("19:15", "23:00"),
    "busser-2": ("19:15", "23:00"),
    "fifty-fifty": ("17:30", "20:30"),
}

CALL_TIMES = {REGULAR: REGULAR_CALL_TIMES, DWIGHTS: DWIGHTS_CALL_TIMES}
