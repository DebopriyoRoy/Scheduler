"""Django settings for the Spirit scheduling application."""

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def database_config() -> dict[str, object]:
    database_url = os.getenv("DATABASE_URL", "sqlite:///db.sqlite3")
    parsed = urlparse(database_url)
    if parsed.scheme in {"postgres", "postgresql"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "localhost",
            "PORT": parsed.port or 5432,
            "CONN_MAX_AGE": 60,
        }
    if parsed.scheme != "sqlite":
        raise ValueError("DATABASE_URL must use sqlite, postgres, or postgresql")
    # sqlite:///name.db  -> relative to the project directory (the usual case)
    # sqlite:////abs/path -> an absolute location. The packaged Mac app needs this:
    # its code lives inside a read-only .app bundle, so the database has to sit in
    # Application Support instead.
    raw = unquote(parsed.path) or "/db.sqlite3"
    if raw.startswith("//"):
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": Path(raw[1:])}
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / raw.lstrip("/")}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-only-secret-key")
DEBUG = env_bool("DJANGO_DEBUG", default=True)
if not DEBUG and SECRET_KEY == "unsafe-development-only-secret-key":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be configured when DJANGO_DEBUG is false.")
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "scheduling",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the collected static files directly from Django. Without it the app has
    # no styling at all once DEBUG is off, because Django stops serving static files
    # and the single-machine install has no separate web server in front of it.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "spirit_scheduler.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "spirit_scheduler.wsgi.application"
ASGI_APPLICATION = "spirit_scheduler.asgi.application"
DATABASES = {"default": database_config()}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-ca"
TIME_ZONE = "America/St_Johns"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# The packaged Mac app collects static files at build time and ships them inside the
# read-only .app bundle, so it points STATIC_ROOT at that location instead.
STATIC_ROOT = Path(os.getenv("SPIRIT_STATIC_ROOT") or (BASE_DIR / "staticfiles"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Compressed but deliberately not the *Manifest* variant: manifest storage refuses
    # to resolve a static file unless collectstatic has already produced its manifest,
    # which breaks the test suite and would turn a forgotten collectstatic into a hard
    # 500 on the desktop install. Hashed filenames buy little here - one machine, one
    # user, no CDN in front.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# HTTPS-dependent hardening is keyed to whether the site is actually served over TLS,
# not to DEBUG. Those are different questions: the single-machine desktop install runs
# with DEBUG off (so errors are not exposed) but over plain http://127.0.0.1, where
# SECURE_SSL_REDIRECT sends every request to an https:// address that does not exist
# and the Secure cookie flags stop anyone logging in. Set DJANGO_HTTPS=true for a real
# deployment behind TLS; leave it false for local desktop use.
SERVE_OVER_HTTPS = env_bool("DJANGO_HTTPS", default=False)

SESSION_COOKIE_SECURE = SERVE_OVER_HTTPS
CSRF_COOKIE_SECURE = SERVE_OVER_HTTPS
SECURE_SSL_REDIRECT = SERVE_OVER_HTTPS
SECURE_HSTS_SECONDS = 31_536_000 if SERVE_OVER_HTTPS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = SERVE_OVER_HTTPS
SECURE_HSTS_PRELOAD = SERVE_OVER_HTTPS
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"


# --- Outgoing email -------------------------------------------------------------
# Used to send a password-reset code. Gmail refuses an ordinary account password over
# SMTP, so EMAIL_HOST_PASSWORD must be a Google App Password: myaccount.google.com >
# Security > 2-Step Verification > App passwords. It is a credential and belongs in
# .env, which is git-ignored - never in this file.
#
# With nothing configured the reset falls back to writing the code to a file in the
# application's data folder, so a machine with no mail set up is not locked out.
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "20"))
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "no-reply@localhost")

# Where a reset code goes when the account being reset has no address of its own.
PASSWORD_RESET_FALLBACK_EMAIL = os.getenv("PASSWORD_RESET_FALLBACK_EMAIL", "")

# Mail is only attempted when there is something to authenticate with.
EMAIL_IS_CONFIGURED = bool(EMAIL_HOST_USER and EMAIL_HOST_PASSWORD)

# --- Self-registration ----------------------------------------------------------
# The sign-in page carries a "Create an account" link. New accounts are held inactive
# until an existing manager approves them on the Access page, because this application
# reaches real staff records and a live Square connection and is reachable by anything
# on the machine.
#
# Set REGISTRATION_REQUIRES_APPROVAL=false to let people in the moment they register.
REGISTRATION_REQUIRES_APPROVAL = env_bool("REGISTRATION_REQUIRES_APPROVAL", default=True)
