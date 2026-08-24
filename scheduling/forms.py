from datetime import date

from django import forms

from scheduling.models import (
    Employee,
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
            "requires_service_staff",
            "requires_50_50",
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
    override_reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), min_length=5)

    def __init__(self, *args, assignment=None, **kwargs):
        super().__init__(*args, **kwargs)
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
            display_name__in=("Yana", "Khrystyna"),
        )


class FiftyFiftyRotationForm(forms.ModelForm):
    class Meta:
        model = FiftyFiftyRotationConfig
        fields = ("seed_employee",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["seed_employee"].queryset = Employee.objects.filter(
            active=True,
            display_name__in=("Yana", "Kate"),
        )


class AvailabilityUploadForm(forms.Form):
    csv_file = forms.FileField(
        help_text="CSV columns: employee, date, available, start_time, end_time, notes"
    )
