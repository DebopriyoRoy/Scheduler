"""Give an existing installation its editable staffing rules and call times.

Values are the ones the engine was already using, with management's October 2026
call-time changes (busser in at 18:30, bartender off at 21:30, on-call bartender off
at 23:00). Only fills gaps: nothing management has already set is touched.
"""

from datetime import datetime

from django.db import migrations

from scheduling import staffing_defaults as defaults


def _t(value):
    return datetime.strptime(value, "%H:%M").time()


def install(apps, schema_editor):
    Role = apps.get_model("scheduling", "Role")
    RoleStaffingRule = apps.get_model("scheduling", "RoleStaffingRule")
    ShiftTemplate = apps.get_model("scheduling", "ShiftTemplate")
    CallTime = apps.get_model("scheduling", "CallTime")

    for order, name in enumerate(defaults.ROLE_ORDER):
        role, _ = Role.objects.get_or_create(name=name)
        minimum, per, stretch, maximum, base, steps = defaults.ROLE_RULES[name]
        RoleStaffingRule.objects.get_or_create(
            role=role,
            defaults={
                "minimum_confirmed": minimum,
                "guests_per_person": per,
                "stretch_guests": stretch,
                "maximum_confirmed": maximum,
                "on_call_base": base,
                "on_call_steps": steps,
                "display_order": order,
            },
        )
    for timetable, times in defaults.CALL_TIMES.items():
        for code, (start, end) in times.items():
            template = ShiftTemplate.objects.filter(code=code).first()
            if template is not None:
                CallTime.objects.get_or_create(
                    timetable=timetable,
                    shift_template=template,
                    defaults={"start_time": _t(start), "end_time": _t(end)},
                )


class Migration(migrations.Migration):
    dependencies = [("scheduling", "0028_editable_staffing_rules")]

    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
