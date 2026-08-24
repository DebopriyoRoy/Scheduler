from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("employees/", views.employees, name="employees"),
    path("roles/", views.roles, name="roles"),
    path("integrations/square/", views.square_integration, name="square_integration"),
    path("shows/", views.show_list, name="show_list"),
    path("shows/new/", views.show_edit, name="show_create"),
    path("shows/<int:show_id>/edit/", views.show_edit, name="show_edit"),
    path("shows/<int:show_id>/deactivate/", views.show_deactivate, name="show_deactivate"),
    path("shows/import/", views.show_import, name="show_import"),
    path("availability/", views.availability, name="availability"),
    path("availability/template/", views.availability_template, name="availability_template"),
    path("configuration/rotations/", views.rotation_configuration, name="rotation_configuration"),
    path("schedules/", views.schedule_list, name="schedule_list"),
    path("schedules/generate/", views.schedule_generate, name="schedule_generate"),
    path("schedules/<int:run_id>/", views.schedule_detail, name="schedule_detail"),
    path("schedules/<int:run_id>/approve/", views.schedule_approve, name="schedule_approve"),
    path("schedules/<int:run_id>/new-draft/", views.schedule_new_draft, name="schedule_new_draft"),
    path(
        "schedules/<int:run_id>/export.xlsx",
        views.schedule_export_excel,
        name="schedule_export_excel",
    ),
    path(
        "schedules/<int:run_id>/export.csv",
        views.schedule_export_csv,
        name="schedule_export_csv",
    ),
    path(
        "schedules/<int:run_id>/export.pdf",
        views.schedule_export_pdf,
        name="schedule_export_pdf",
    ),
    path(
        "schedules/assignments/<int:assignment_id>/override/",
        views.schedule_override,
        name="schedule_override",
    ),
    path(
        "schedules/warnings/<int:warning_id>/resolve/",
        views.schedule_warning_resolve,
        name="schedule_warning_resolve",
    ),
]
