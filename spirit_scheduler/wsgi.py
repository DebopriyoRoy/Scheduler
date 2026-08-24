"""WSGI entry point for spirit_scheduler."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "spirit_scheduler.settings")
application = get_wsgi_application()
