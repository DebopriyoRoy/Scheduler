from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("employees/", views.employees, name="employees"),
    path("roles/", views.roles, name="roles"),
    path("integrations/square/", views.square_integration, name="square_integration"),
]

