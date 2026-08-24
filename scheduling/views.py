from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def dashboard(request):
    return render(request, "scheduling/dashboard.html")


@login_required
def employees(request):
    return render(request, "scheduling/employees.html", {"employees": []})


@login_required
def roles(request):
    return render(request, "scheduling/roles.html", {"roles": []})


@login_required
def square_integration(request):
    return render(
        request,
        "scheduling/square_integration.html",
        {"connection_status": "Not Connected"},
    )

