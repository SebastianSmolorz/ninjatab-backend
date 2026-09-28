"""Freeze public tabs into markdown files the Nuxt site renders at build time.

These tabs are finished trips — the numbers never move — so hitting the API on
every page view buys nothing and couples a marketing page to the app's uptime.
Run this whenever a tab is published or its bills change, then commit the output.

Only the numbers and the tab's own description are exported. Titles, meta
descriptions, images and the author link live in content/trips/<slug>.md and are
hand-written, so re-running this can never clobber copy.

Two sources, same payload: the ORM (needs DB access, exports every public tab),
or a running API via --from-api (needs slugs, but works from any machine).
"""
import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from django.core.management.base import BaseCommand, CommandError

from ninjatab.tabs.api.tabs import _public_tab_payload
from ninjatab.tabs.models import Tab

# backend/ninjatab/tabs/management/commands/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_OUT = REPO_ROOT / "frontend" / "ninjatab" / "content" / "tripdata"

PREFETCH = (
    "people",
    "bills__creator",
    "bills__paid_by",
    "bills__line_items__person_claims__person",
    "settlements__from_person",
    "settlements__to_person",
)


def _ssl_context():
    """A stock python.org install has no CA bundle wired up; certifi ships one."""
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def _plain(value):
    """Dates and Decimals through JSON so yaml only ever sees primitives."""
    return json.loads(json.dumps(value, default=str))


class Command(BaseCommand):
    help = "Export public tabs to content/tripdata/<slug>.md for the Nuxt site"

    def add_arguments(self, parser):
        parser.add_argument("slugs", nargs="*", help="public_slug values (default: every public tab)")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
        parser.add_argument(
            "--from-api",
            metavar="BASE_URL",
            help="Read tabs from a running API (e.g. https://api.tab.ninja/api) instead of the "
                 "database. Requires slugs — the public API has no index to enumerate.",
        )

    def handle(self, *args, **options):
        slugs = options["slugs"]
        base_url = options["from_api"]

        if base_url and not slugs:
            raise CommandError("--from-api needs one or more slugs")

        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)

        sources = self._from_api(base_url, slugs) if base_url else self._from_db(slugs)

        count = 0
        for slug, payload in sources:
            self._write(out, slug, payload)
            count += 1

        self.stdout.write(self.style.SUCCESS(f"Exported {count} tab(s) to {out}"))

    def _from_db(self, slugs):
        tabs = Tab.objects.filter(is_public=True).exclude(public_slug="").prefetch_related(*PREFETCH)
        if slugs:
            tabs = tabs.filter(public_slug__in=slugs)
            missing = set(slugs) - {t.public_slug for t in tabs}
            if missing:
                raise CommandError(f"No public tab with slug: {', '.join(sorted(missing))}")
        for tab in tabs:
            yield tab.public_slug, _public_tab_payload(tab, presign=False)

    def _from_api(self, base_url, slugs):
        for slug in slugs:
            url = f"{base_url.rstrip('/')}/tabs/public/{slug}"
            try:
                with urllib.request.urlopen(url, timeout=30, context=_ssl_context()) as response:
                    yield slug, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                raise CommandError(f"{url} returned {exc.code}")
            except urllib.error.URLError as exc:
                raise CommandError(f"{url} unreachable: {exc.reason}")

    def _write(self, out, slug, payload):
        # `id` is reserved by Nuxt Content, and a uuid has no business in a
        # public static file. The description becomes the markdown body.
        payload.pop("id", None)
        body = (payload.pop("description", "") or "").strip()

        for bill in payload["bills"]:
            # A presigned S3 URL expires, so it must never be frozen into a
            # static file — the page links to the redirect endpoint instead.
            # An older API build won't send has_receipt, so derive it.
            receipt_url = bill.pop("receipt_image_url", "")
            bill.setdefault("has_receipt", bool(receipt_url))

        # Per-bill line items are the bulk of the payload and are only read on
        # the drill-down, so they go in their own top-level key the overview
        # query can leave out.
        details = [
            {
                "id": bill["id"],
                "person_totals": bill.pop("person_totals"),
                "line_items": bill.pop("line_items"),
            }
            for bill in payload["bills"]
        ]

        front = _plain({"slug": slug, **payload, "bill_details": details})
        text = (
            "---\n"
            + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000)
            + "---\n\n"
            + body
            + "\n"
        )
        (out / f"{slug}.md").write_text(text, encoding="utf-8")
        self.stdout.write(f"  {slug}.md  ({len(payload['bills'])} bills)")
