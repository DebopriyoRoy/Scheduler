"""The Staffing rules page: crew size per role, call times, and what they add up to."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.forms import modelformset_factory
from django.shortcuts import redirect, render
from django.urls import reverse

from scheduling.forms import TimeInput
from scheduling.models import (
    MAX_THEATRE_CAPACITY,
    MINIMUM_VIABLE_GUESTS,
    AssignmentType,
    CallTime,
    RoleStaffingRule,
    StaffingRuleChange,
    Timetable,
)
from scheduling.services.requirements import StaffingRequirement, templates_for_requirement
from scheduling.services.staffing_rules import (
    describe_rule,
    ensure_positions,
    install_defaults,
    log_change,
    needs_for_guests,
    rules_by_role,
)

RULE_FIELDS = (
    "minimum_confirmed",
    "guests_per_person",
    "stretch_guests",
    "maximum_confirmed",
    "on_call_base",
    "on_call_steps",
)

FIELD_LABELS = {
    "minimum_confirmed": "minimum",
    "guests_per_person": "guests per person",
    "stretch_guests": "stretch",
    "maximum_confirmed": "maximum",
    "on_call_base": "on call",
    "on_call_steps": "extra on call at",
    "start_time": "in",
    "end_time": "out",
}


class RuleForm(forms.ModelForm):
    class Meta:
        model = RoleStaffingRule
        fields = RULE_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.update(
                {"class": "form-control form-control-sm font-monospace", "aria-label": name}
            )
        self.fields["maximum_confirmed"].widget.attrs["placeholder"] = "none"
        self.fields["on_call_steps"].widget.attrs["placeholder"] = "e.g. 5, 7"


class CallTimeForm(forms.ModelForm):
    class Meta:
        model = CallTime
        fields = ("start_time", "end_time")
        widgets = {"start_time": TimeInput(format="%H:%M"), "end_time": TimeInput(format="%H:%M")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control form-control-sm font-monospace"


RuleFormSet = modelformset_factory(RoleStaffingRule, form=RuleForm, extra=0)
CallTimeFormSet = modelformset_factory(CallTime, form=CallTimeForm, extra=0)


def _rule_queryset():
    return RoleStaffingRule.objects.select_related("role").order_by("display_order", "role__name")


def _times_queryset(timetable: str):
    return (
        CallTime.objects.filter(timetable=timetable, shift_template__active=True)
        .select_related("shift_template", "shift_template__role")
        .order_by("shift_template__position_order", "shift_template__pk")
    )


def _fmt(value) -> str:
    if value is None or value == "":
        return "none"
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value)


def _changes(formset, label) -> list[str]:
    lines = []
    for form in formset.forms:
        if not form.has_changed():
            continue
        parts = [
            f"{FIELD_LABELS.get(name, name)} {_fmt(form.initial.get(name))} → "
            f"{_fmt(form.cleaned_data.get(name))}"
            for name in form.changed_data
        ]
        lines.append(f"{label(form.instance)}: " + ", ".join(parts))
    return lines


def _ladder(rules) -> list[dict]:
    """The crew the rules give, as bands of guest counts that share one crew."""
    top = MAX_THEATRE_CAPACITY
    bands: list[dict] = []
    for guests in range(MINIMUM_VIABLE_GUESTS, top + 1):
        needs = {need.role_name: need for need in needs_for_guests(guests, rules)}
        key = tuple((n.confirmed_count, n.on_call_count) for n in needs.values())
        if bands and bands[-1]["key"] == key:
            bands[-1]["high"] = guests
            continue
        bands.append(
            {
                "key": key,
                "low": guests,
                "high": guests,
                "cells": [
                    {"confirmed": n.confirmed_count, "on_call": n.on_call_count}
                    for n in needs.values()
                ],
                "people": sum(n.confirmed_count + n.on_call_count for n in needs.values()),
            }
        )
    return bands


def _crew_for(guests: int, rules) -> list[dict]:
    """Every position a show of this size gets, with its 6:30 pm call time."""
    crew = []
    times = {
        ct.shift_template_id: ct
        for ct in CallTime.objects.filter(timetable=Timetable.REGULAR)
    }
    for need in needs_for_guests(guests, rules):
        requirement = StaffingRequirement(need.role_name, need.confirmed_count, need.on_call_count)
        templates = templates_for_requirement(requirement)
        for template in templates:
            ct = times.get(template.pk)
            crew.append(
                {
                    "name": template.name,
                    "role": need.role_name,
                    "on_call": template.assignment_type == AssignmentType.ON_CALL,
                    "start": ct.start_time if ct else None,
                    "end": ct.end_time if ct else None,
                }
            )
        missing = need.confirmed_count + need.on_call_count - len(templates)
        if missing > 0:
            crew.append({"name": f"{missing} more {need.role_name} (no position yet)",
                         "role": need.role_name, "missing": True})
    return crew


@login_required
def staffing_rules(request):
    here = reverse("staffing_rules")
    rule_formset = RuleFormSet(queryset=_rule_queryset(), prefix="rules")
    time_formsets = {
        timetable: CallTimeFormSet(queryset=_times_queryset(timetable), prefix=f"t-{timetable}")
        for timetable in Timetable.values
    }

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "rules":
            rule_formset = RuleFormSet(request.POST, queryset=_rule_queryset(), prefix="rules")
            if rule_formset.is_valid():
                lines = _changes(rule_formset, lambda rule: rule.role.name)
                with transaction.atomic():
                    for form in rule_formset.forms:
                        if form.has_changed():
                            rule = form.save(commit=False)
                            rule.updated_by = request.user
                            rule.save()
                    created = ensure_positions()
                    if lines:
                        log_change(request.user, "Crew size — " + "; ".join(lines))
                note = f" New positions added: {', '.join(created)}." if created else ""
                messages.success(
                    request,
                    ("Crew size rules saved." if lines else "No changes to save.") + note,
                )
                return redirect(here + "#crew")
            messages.error(request, "Some rules need fixing — see the highlighted rows.")
        elif action == "times":
            timetable = request.POST.get("timetable")
            if timetable in Timetable.values:
                formset = CallTimeFormSet(
                    request.POST, queryset=_times_queryset(timetable), prefix=f"t-{timetable}"
                )
                time_formsets[timetable] = formset
                if formset.is_valid():
                    label = Timetable(timetable).label
                    lines = _changes(formset, lambda ct: ct.shift_template.name)
                    with transaction.atomic():
                        for form in formset.forms:
                            if form.has_changed():
                                call_time = form.save(commit=False)
                                call_time.updated_by = request.user
                                call_time.save()
                        if lines:
                            log_change(request.user, f"Call times, {label} — " + "; ".join(lines))
                    messages.success(
                        request, "Call times saved." if lines else "No changes to save."
                    )
                    return redirect(here + f"#times-{timetable}")
                messages.error(request, "Some call times need fixing — see the highlighted rows.")
        elif action == "reset":
            install_defaults(user=request.user, overwrite=True)
            log_change(request.user, "Reset every rule and call time to Spirit defaults.")
            messages.success(request, "Rules and call times reset to Spirit defaults.")
            return redirect(here)

    rules = rules_by_role()
    try:
        guests = int(request.GET.get("guests", 80))
    except ValueError:
        guests = 80
    guests = max(MINIMUM_VIABLE_GUESTS, min(guests, MAX_THEATRE_CAPACITY))

    rule_rows = []
    for form in rule_formset.forms:
        rule = form.instance
        rule_rows.append({"form": form, "role": rule.role.name, "describe": describe_rule(rule)})

    context = {
        "rule_formset": rule_formset,
        "rule_rows": rule_rows,
        "role_names": list(rules.keys()),
        "ladder": _ladder(rules),
        "guests": guests,
        "crew": _crew_for(guests, rules),
        "timetables": [
            {
                "key": timetable,
                "label": Timetable(timetable).label,
                "formset": formset,
                "rows": [(form, form.instance.shift_template) for form in formset.forms],
            }
            for timetable, formset in time_formsets.items()
        ],
        "changes": StaffingRuleChange.objects.select_related("changed_by")[:15],
        "capacity": MAX_THEATRE_CAPACITY,
        "minimum_guests": MINIMUM_VIABLE_GUESTS,
    }
    return render(request, "scheduling/staffing_rules.html", context)
