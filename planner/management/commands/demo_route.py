import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from planner import cache
from planner.data import warm
from planner.routing import _warm_connections


class Command(BaseCommand):
    help = "Clear the route cache, call the live API twice (cold, then cached), save docs/example-response.json."

    def add_arguments(self, parser):
        parser.add_argument("--start", default="Chicago, IL")
        parser.add_argument("--finish", default="Los Angeles, CA")

    def handle(self, *args, **options):
        cache.clear()
        warm()  # Same startup work the server does (config/wsgi.py),
        _warm_connections()  # including an open connection to the routing servers.
        client = Client()
        body = json.dumps({"start": options["start"], "finish": options["finish"]})
        for label in ("cold", "cached"):
            response = client.post("/api/route/", body, content_type="application/json")
            data = response.json()
            if response.status_code != 200:
                raise CommandError(f"{response.status_code}: {data}")
            meta = data["metadata"]
            self.stdout.write(
                f"{label:>6}: {data['distance_miles']} mi, ${data['total_cost_usd']}, "
                f"{len(data['fuel_stops'])} stops, {meta['routing_calls']} routing call(s), "
                f"{meta['timing_ms']['total']} ms"
            )
        (settings.BASE_DIR / "docs/example-response.json").write_text(json.dumps(data, indent=2) + "\n")
        self.stdout.write("Saved docs/example-response.json")
