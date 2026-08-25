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
    path("shows/calendar-sync/", views.calendar_sync, name="calendar_sync"),
    path("availability/", views.availability, name="availability"),
    path(
        "availability/square-sync/",
        views.square_availability_sync,
        name="square_availability_sync",
    ),
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
    path(
        "schedules/<int:run_id>/sync-preview/",
        views.schedule_sync_preview,
        name="schedule_sync_preview",
    ),
    path(
        "schedules/<int:run_id>/sync-confirm/",
        views.schedule_sync_confirm,
        name="schedule_sync_confirm",
    ),
    path(
        "integrations/square/team-mapping/",

        views.square_team_mapping,
        name="square_team_mapping",
    ),
    path(
        "integrations/square/job-mapping/",
        views.square_job_mapping,
        name="square_job_mapping",
    ),
    path(
        "schedules/<int:run_id>/square-sync/",
        views.square_production_sync_hub,
        name="square_production_sync_hub",
    ),
    path(
        "schedules/<int:run_id>/pilot-confirm/",
        views.square_production_pilot_confirm,
        name="square_production_pilot_confirm",
    ),
    path(
        "schedules/<int:run_id>/pilot-verify/",
        views.square_production_pilot_verify,
        name="square_production_pilot_verify",
    ),
    path(
        "schedules/<int:run_id>/full-sync/",
        views.square_production_full_sync,
        name="square_production_full_sync",
    ),
    path(
        "schedules/<int:run_id>/sync-export.csv",
        views.export_production_sync_csv,
        name="export_production_sync_csv",
    ),
]




