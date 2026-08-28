import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError

from scheduling.models import Employee, EmployeeRole, Role


@pytest.mark.django_db
def test_role_creation_and_employee_relationship():
    employee = Employee.objects.create(first_name="Test", display_name="Test Employee")
    role = Role.objects.create(name="Server")
    relationship = EmployeeRole.objects.create(
        employee=employee,
        role=role,
        capability_level=3,
    )
    assert relationship in employee.employee_roles.all()
    assert relationship in role.employee_roles.all()


@pytest.mark.django_db
def test_duplicate_employee_role_is_rejected():
    employee = Employee.objects.create(first_name="Test", display_name="Test Employee")
    role = Role.objects.create(name="Server")
    EmployeeRole.objects.create(employee=employee, role=role, capability_level=3)
    with pytest.raises(IntegrityError):
        EmployeeRole.objects.create(employee=employee, role=role, capability_level=4)


@pytest.mark.django_db
def test_capability_level_validation():
    employee = Employee.objects.create(first_name="Test", display_name="Test Employee")
    role = Role.objects.create(name="Server")
    assignment = EmployeeRole(employee=employee, role=role, capability_level=6)
    with pytest.raises(ValidationError):
        assignment.full_clean()


@pytest.mark.django_db
def test_staff_seed_is_idempotent_and_excludes_managers():
    call_command("seed_spirit_staff")
    call_command("seed_spirit_staff")
    # 17 floor staff plus the Server Manager.
    assert Employee.objects.count() == 18
    # Server, Bartender, Busser, 50/50, Server Manager.
    assert Role.objects.count() == 5
    # 22 floor qualifications plus Deborah's Server Manager one.
    assert EmployeeRole.objects.filter(active=True).count() == 23
    # John Harris stays off the roster entirely. Deborah is now on it, but only as the
    # Server Manager - she is still never a candidate for an ordinary floor shift.
    assert not Employee.objects.filter(
        display_name__in=("John Harris", "John Haris")
    ).exists()
    deborah = Employee.objects.get(display_name="Deborah Sweetapple")
    assert set(deborah.employee_roles.filter(active=True).values_list("role__name", flat=True)) == {
        "Server Manager"
    }
    kate = Employee.objects.get(display_name="Kate")
    assert kate.employee_roles.get(role__name="Server").capability_level == 3
    assert kate.employee_roles.get(role__name="50/50").capability_level == 3
    jackie = Employee.objects.get(display_name="Jackie Pynn")
    assert jackie.spirit_only_employment is True
    assert set(jackie.employee_roles.values_list("role__name", flat=True)) == {
        "Server",
        "Bartender",
    }
    assert jackie.employment_priority == 1
    assert Employee.objects.get(display_name="Olena").employment_priority == 1
    assert set(
        Employee.objects.get(display_name="Svitlana")
        .employee_roles.filter(active=True)
        .values_list("role__name", flat=True)
    ) == {"Server", "Bartender"}
