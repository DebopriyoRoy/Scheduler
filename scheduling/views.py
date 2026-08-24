from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render

from .models import Employee, Role


@login_required
def dashboard(request):
    return render(
        request,
        "scheduling/dashboard.html",
        {
            "employee_count": Employee.objects.filter(active=True).count(),
            "role_count": Role.objects.count(),
        },
    )


@login_required
def employees(request):
    employee_list = Employee.objects.prefetch_related("employee_roles__role")
    return render(request, "scheduling/employees.html", {"employees": employee_list})


@login_required
def roles(request):
    role_list = Role.objects.annotate(
        active_employee_count=Count(
            "employee_roles",
            filter=Q(employee_roles__active=True, employee_roles__employee__active=True),
        )
    )
    return render(request, "scheduling/roles.html", {"roles": role_list})


@login_required
def square_integration(request):
    return render(
        request,
        "scheduling/square_integration.html",
        {"connection_status": "Not Connected"},
    )
