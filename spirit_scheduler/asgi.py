"""ASGI entry point for spirit_scheduler."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "spirit_scheduler.settings")
application = get_asgi_application()
