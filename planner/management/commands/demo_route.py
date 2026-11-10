import json

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.test import Client


class Command(BaseCommand):
    help = "Call the live routing provider twice (cold, then cached) and save docs/example-response.json."

    def add_arguments(self, parser):
        parser.add_argument("--start", default="Chicago, IL")
        parser.add_argument("--finish", default="Los Angeles, CA")

    def handle(self, *args, **options):
        cache.clear()
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
                f"{len(data['fuel_stops'])} stops, {meta['routing_calls']} routing call(s), {meta['timing_ms']['total']} ms"
            )
        (settings.BASE_DIR / "docs/example-response.json").write_text(json.dumps(data, indent=2) + "\n")
        self.stdout.write("Saved docs/example-response.json")
