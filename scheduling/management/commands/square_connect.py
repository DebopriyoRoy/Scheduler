"""Sign in to the Square dashboard once, then record where availability comes from.

Square publishes no availability API, so it has to be read from the dashboard. This
opens a real browser for the sign-in - the password goes to Square's own page and is
never seen here - and then watches which requests the availability screen makes, so the
data endpoint is discovered rather than guessed. Square moves these paths between
dashboard revisions; a hardcoded URL would rot.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from scheduling.integrations.square_session import (
    DEFAULT_AVAILABILITY_URL,
    SquareSessionError,
    logged_in_context,
    open_dashboard_for_login,
    record_session,
    session_dir,
    session_status,
)


class Command(BaseCommand):
    help = "Sign in to Square and discover the availability data endpoint."

    def add_arguments(self, parser):
        parser.add_argument(
            "--probe-only",
            action="store_true",
            help="Skip sign-in and reuse the stored session to inspect the page.",
        )
        parser.add_argument(
            "--url",
            default="",
            help="Availability page to open. Defaults to the stored or built-in URL.",
        )

    def handle(self, *args, **options):
        status = session_status()

        if not options["probe_only"]:
            if status.connected:
                self.stdout.write(f"Already connected: {status.detail}")
            self.stdout.write(
                "Opening a browser window. Sign in to Square there - including any "
                "two-factor step - and leave it open once the dashboard appears."
            )
            try:
                status = open_dashboard_for_login()
            except SquareSessionError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS(f"Signed in. {status.detail}"))

        if not status.connected:
            raise CommandError("no stored session; run without --probe-only first.")

        target = options["url"] or status.availability_url or DEFAULT_AVAILABILITY_URL
        self.stdout.write(f"\nOpening {target} to see what it returns…")

        from playwright.sync_api import sync_playwright

        captured = []
        with sync_playwright() as p:
            context = logged_in_context(p, headless=False)
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response):
                url = response.url
                if any(w in url.lower() for w in ("avail", "schedule", "team", "shift")):
                    ctype = (response.headers or {}).get("content-type", "")
                    if "json" in ctype:
                        captured.append({"url": url, "status": response.status})

            page.on("response", on_response)
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            final_url = page.url
            title = page.title()
            context.close()

        self.stdout.write(f"  landed on : {final_url}")
        self.stdout.write(f"  page title: {title}")

        if "login" in final_url or "signin" in final_url:
            raise CommandError(
                "Square redirected to sign-in, so the stored session has expired. "
                "Run this command again without --probe-only."
            )

        out = Path(session_dir()) / "endpoint-probe.json"
        out.write_text(json.dumps(captured, indent=1))
        self.stdout.write(f"\n  JSON requests the page made: {len(captured)}")
        for item in captured[:20]:
            self.stdout.write(f"    {item['status']}  {item['url'][:118]}")
        self.stdout.write(f"\n  full list written to {out}")

        account = session_status().detail.split(" on ")[0].replace("Connected as ", "")
        record_session(account, final_url)
        self.stdout.write(
            self.style.SUCCESS(
                "\nSession stored. Send the list above on and the availability reader "
                "can be written against the real endpoint."
            )
        )
