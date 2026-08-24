#!/usr/bin/env python3
"""Django command-line utility for the Spirit scheduling application."""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "spirit_scheduler.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not installed. Activate the virtual environment and install requirements."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

