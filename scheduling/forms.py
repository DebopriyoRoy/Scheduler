from datetime import date

from django import forms
from django.utils import timezone

from scheduling.models import (
    Employee,
    EmployeeTimeOff,
    FiftyFiftyRotationConfig,
    OfficeRotationConfig,
    Show,
)


class DateInput(forms.DateInput):
    input_type = "date"


class TimeInput(forms.TimeInput):
    input_type = "time"


class ShowForm(forms.ModelForm):
    class Meta:
        model = Show
        fields = (
            "title",
            "date",
            "start_time",
            "end_time",
            "venue",
            "expected_guests",
            "capacity",
            "capacity_override_reason",
            # requires_50_50 is deliberately absent: the 50/50 is now part of the
            # standard crew in staffing_requirements_for(), so a per-show toggle here
            # would look like it controlled something and control nothing.
            "requires_service_staff",
            "notes",
            "active",
        )
        widgets = {
            "date": DateInput(),
            "start_time": TimeInput(),
            "end_time": TimeInput(),
        }


class CalendarImportForm(forms.Form):
    start_date = forms.DateField(widget=DateInput(), initial=date(2026, 9, 7))
    end_date = forms.DateField(widget=DateInput(), initial=date(2026, 10, 3))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start_date") and cleaned.get("end_date"):
            if cleaned["end_date"] < cleaned["start_date"]:
                self.add_error("end_date", "End date must not precede the start date.")
        return cleaned


class ScheduleGenerateForm(CalendarImportForm):
    generate_with_shortages = forms.BooleanField(
        required=False,
        help_text=(
            "Required when availability is incomplete. Unknown availability remains ineligible."
        ),
    )
    schedule_run_id = forms.IntegerField(required=False, widget=forms.HiddenInput())


class OverrideAssignmentForm(forms.Form):
    employee = forms.ModelChoiceField(queryset=Employee.objects.none())
    # Optional: an override that only swaps the person keeps the generated window, so
    # omitting these is a valid request rather than an incomplete one.
    start_time = forms.TimeField(
        widget=TimeInput(),
        required=False,
        help_text="Local time. Leave as-is to keep the generated shift window.",
    )
    end_time = forms.TimeField(widget=TimeInput(), required=False)
    swap = forms.BooleanField(
        required=False,
        label="Swap positions",
        help_text=(
            "Only when the person you pick is already working this show. They take this "
            "shift and the person currently on it takes theirs, each on the other's hours. "
            "Leave unticked to move them here and leave their old position unfilled."
        ),
    )
    override_reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), min_length=5)

    def __init__(self, *args, assignment=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Who is already on this show, so the page can say what a swap would do.
        self.swappable = {}
        if assignment is not None:
            from scheduling.models import ScheduleAssignment

            self.swappable = {
                row.employee_id: row.shift_template.name
                for row in ScheduleAssignment.objects.filter(
                    schedule_run=assignment.schedule_run, show=assignment.show
                )
                .exclude(pk=assignment.pk)
                .select_related("shift_template")
            }
        if assignment is not None:
            self.fields["employee"].queryset = (
                Employee.objects.filter(
                    active=True,
                    employee_roles__role=assignment.role,
                    employee_roles__active=True,
                )
                .distinct()
                .order_by("display_name")
            )
            # Prefilled with the window the engine worked out, so a manager replacing
            # somebody without touching the times keeps exactly what was generated.
            self.fields["start_time"].initial = timezone.localtime(
                assignment.start_datetime
            ).time()
            self.fields["end_time"].initial = timezone.localtime(assignment.end_datetime).time()

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end and start == end:
            raise forms.ValidationError("The shift start and end times cannot be the same.")
        return cleaned


class OfficeRotationForm(forms.ModelForm):
    class Meta:
        model = OfficeRotationConfig
        fields = (
            "seed_date",
            "seed_saturday_employee",
            "office_start_time",
            "office_end_time",
        )
        widgets = {
            "seed_date": DateInput(),
            "office_start_time": TimeInput(),
            "office_end_time": TimeInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["seed_saturday_employee"].queryset = Employee.objects.filter(
            active=True,
            first_name__in=("Yana", "Khrystyna"),
        )


class FiftyFiftyRotationForm(forms.ModelForm):
    class Meta:
        model = FiftyFiftyRotationConfig
        fields = ("seed_employee",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["seed_employee"].queryset = Employee.objects.filter(
            active=True,
            first_name__in=("Yana", "Kate"),
        )


class AvailabilityUploadForm(forms.Form):
    csv_file = forms.FileField(
        help_text="CSV columns: employee, date, available, start_time, end_time, notes"
    )


class FillAssignmentForm(forms.Form):
    """Staff a slot the generator left short.

    Mirrors OverrideAssignmentForm, but the times start from the window the generator
    would have used rather than from an assignment that does not exist yet.
    """

    employee = forms.ModelChoiceField(queryset=Employee.objects.none())
    start_time = forms.TimeField(
        widget=TimeInput(),
        required=False,
        help_text="Local time. Leave as-is to use the standard window for this position.",
    )
    end_time = forms.TimeField(widget=TimeInput(), required=False)
    override_reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        min_length=5,
        label="Reason",
    )

    def __init__(self, *args, template=None, window=None, **kwargs):
        super().__init__(*args, **kwargs)
        if template is not None:
            self.fields["employee"].queryset = (
                Employee.objects.filter(
                    active=True,
                    employee_roles__role=template.role,
                    employee_roles__active=True,
                )
                .distinct()
                .order_by("display_name")
            )
        if window is not None:
            start, end = window
            self.fields["start_time"].initial = timezone.localtime(start).time()
            self.fields["end_time"].initial = timezone.localtime(end).time()

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end and start == end:
            raise forms.ValidationError("The shift start and end times cannot be the same.")
        return cleaned


class TimeOffForm(forms.ModelForm):
    class Meta:
        model = EmployeeTimeOff
        fields = ("employee", "start_date", "end_date", "status", "reason")
        widgets = {"start_date": DateInput(), "end_date": DateInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.filter(active=True).order_by(
            "display_name"
        )
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control form-control-sm")
