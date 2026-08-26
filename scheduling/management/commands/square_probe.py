"""Find where the Square dashboard keeps availability data.

The first probe filtered network traffic too narrowly and caught only a Remix route
manifest. Square's dashboard is a Remix application, which means loader data usually
arrives either embedded in the first HTML document or through `?_data=` route requests
rather than anything that looks like a conventional JSON API. This captures everything
and then looks for staff names, which is the only reliable proof of having found the
right payload.
"""

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from scheduling.integrations.square_session import (
    SquareSessionError,
    logged_in_context,
    session_dir,
    session_status,
)
from scheduling.models import Employee


class Command(BaseCommand):
    help = "Capture everything the Square availability page loads, and find the data."

    def add_arguments(self, parser):
        parser.add_argument("--url", default="", help="Page to open.")
        parser.add_argument("--seconds", type=int, default=20, help="How long to watch.")
        parser.add_argument(
            "--headless", action="store_true", help="Do not show the browser window."
        )

    def handle(self, *args, **options):
        status = session_status()
        if not status.connected:
            raise CommandError("no stored session. Run: manage.py square_connect")

        target = options["url"] or status.availability_url
        names = [e.display_name.split()[0] for e in Employee.objects.filter(active=True)]
        out_dir = Path(session_dir())

        from playwright.sync_api import sync_playwright

        traffic = []
        bodies = {}

        try:
            with sync_playwright() as p:
                context = logged_in_context(p, headless=options["headless"])
                page = context.pages[0] if context.pages else context.new_page()

                def on_response(response):
                    entry = {
                        "url": response.url,
                        "status": response.status,
                        "type": (response.headers or {}).get("content-type", "")[:60],
                    }
                    traffic.append(entry)
                    # Keep any body that mentions a member of staff - that is the payload.
                    try:
                        if response.request.resource_type in ("xhr", "fetch", "document"):
                            body = response.text()
                            if any(n in body for n in names):
                                bodies[response.url] = body[:400000]
                                entry["HAS_STAFF_NAMES"] = True
                    except Exception:  # noqa: BLE001 - many bodies are not readable
                        pass

                page.on("response", on_response)
                self.stdout.write(f"Opening {target} and watching for {options['seconds']}s…")
                page.goto(target, wait_until="domcontentloaded")
                page.wait_for_timeout(options["seconds"] * 1000)

                final_url = page.url
                if "login" in final_url or "signin" in final_url:
                    context.close()
                    raise CommandError(
                        "Square redirected to sign-in - the session expired. "
                        "Run: manage.py square_connect"
                    )

                html = page.content()
                (out_dir / "page.html").write_text(html)

                # Remix puts loader data on the window; that is the likeliest home.
                remix = page.evaluate(
                    "() => { try { return JSON.stringify(window.__remixContext || null); }"
                    " catch (e) { return null; } }"
                )
                if remix and remix != "null":
                    (out_dir / "remix-context.json").write_text(remix)
                    found = [n for n in names if n in remix]
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  window.__remixContext captured ({len(remix):,} chars); "
                            f"staff names present: {found[:8] or 'none'}"
                        )
                    )
                else:
                    self.stdout.write("  no window.__remixContext on this page")

                visible = page.inner_text("body")[:4000]
                (out_dir / "visible-text.txt").write_text(visible)
                context.close()
        except SquareSessionError as exc:
            raise CommandError(str(exc)) from exc

        (out_dir / "traffic.json").write_text(json.dumps(traffic, indent=1))

        self.stdout.write(f"\n  requests seen: {len(traffic)}")
        interesting = [t for t in traffic if t.get("HAS_STAFF_NAMES")]
        self.stdout.write(f"  responses mentioning staff: {len(interesting)}")
        for t in interesting[:12]:
            self.stdout.write(f"    {t['status']}  {t['url'][:110]}")

        data_routes = [t for t in traffic if "_data=" in t["url"] or "/api/" in t["url"]]
        if data_routes:
            self.stdout.write(f"\n  Remix/API data routes: {len(data_routes)}")
            for t in data_routes[:12]:
                self.stdout.write(f"    {t['status']}  {t['url'][:110]}")

        # Does the rendered page itself show availability?
        text = (out_dir / "visible-text.txt").read_text()
        shown = [n for n in names if n in text]
        times = re.findall(r"\b\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?\b", text)
        self.stdout.write(f"\n  staff names visible on the page: {shown[:10] or 'none'}")
        self.stdout.write(f"  clock times visible on the page : {len(times)}")
        self.stdout.write(f"\n  everything written to {out_dir}")
        files = "page.html, traffic.json, visible-text.txt"
        if (out_dir / "remix-context.json").exists():
            files += ", remix-context.json"
        self.stdout.write(f"    {files}")
