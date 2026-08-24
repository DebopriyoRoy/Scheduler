from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareIntegrationError

from .models import Employee, Role


def square_connection_context() -> dict[str, object]:
    context: dict[str, object] = {
        "connection_status": "Not Connected",
        "environment": "Not configured",
        "locations": [],
        "error_message": "",
    }
    try:
        config = SquareConfig.from_env()
        context["environment"] = config.environment.value.title()
        if config.environment is SquareEnvironment.SANDBOX and config.token_is_configured:
            context["locations"] = SquareClient(config).test_connection()
            context["connection_status"] = "Connected to Sandbox"
    except SquareIntegrationError as exc:
        context["error_message"] = str(exc)
    return context


@login_required
def dashboard(request):
    square_context = square_connection_context()
    return render(
        request,
        "scheduling/dashboard.html",
        {
            "employee_count": Employee.objects.filter(active=True).count(),
            "role_count": Role.objects.count(),
            "square_connection_status": square_context["connection_status"],
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
    return render(request, "scheduling/square_integration.html", square_connection_context())
