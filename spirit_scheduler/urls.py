"""URL routes for the Spirit scheduling application."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from scheduling import views as scheduling_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Outside the login wall on purpose: it is the way back in for someone locked out.
    path("accounts/register/", scheduling_views.register, name="register"),
    path("accounts/reset/", scheduling_views.password_reset, name="password_reset"),
    path(
        "accounts/reset/<uidb64>/<token>/",
        scheduling_views.password_reset_confirm,
        name="password_reset_confirm",
    ),
    path("", include("scheduling.urls")),
]
