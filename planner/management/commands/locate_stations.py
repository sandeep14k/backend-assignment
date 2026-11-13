"""Build source-backed coordinates from a locally saved OpenStreetMap extract."""

import csv
import json
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.data import stations
from planner.enrichment import match_stations


class Command(BaseCommand):
    help = "Match CSV station identities to OSM fuel objects; write coordinates and an unresolved report."

    def add_arguments(self, parser):
        parser.add_argument("extract", type=Path, nargs="+")

    def handle(self, *args, **options):
        elements = {}
        dates = []
        for path in options["extract"]:
            payload = json.loads(path.read_text())
            if payload.get("remark") or "elements" not in payload:
                raise CommandError("OSM extract is incomplete or invalid; existing coordinates were not changed.")
            date = payload.get("osm3s", {}).get("timestamp_osm_base", "unknown")
            dates.append(date)
            for element in payload["elements"]:
                elements[(element["type"], element["id"])] = {**element, "source_date": date}
        # Matching always starts from the original city's location, not a previous override.
        from planner.data import find_city

        inventory = []
        for station in stations()[0]:
            city = find_city(station["city"], station["state"])
            if city:
                inventory.append(
                    {**station, "latitude": float(city["LATITUDE"]), "longitude": float(city["LONGITUDE"])}
                )
        matches, report = match_stations(inventory, list(elements.values()))
        operator_path = settings.BASE_DIR / "data/operator-evidence.json"
        if operator_path.exists():
            evidence = json.loads(operator_path.read_text())
            from planner.enrichment import brand, distance, store_ref

            for station in inventory:
                row = evidence.get(station["id"])
                if row is None:
                    continue
                if (
                    brand(station["name"]) != "loves"
                    or store_ref(station["name"]) != row["store_reference"]
                    or row["id"] != station["id"]
                    or row["source_state"] != station["state"]
                    or not row["source"].startswith("https://www.loves.com/")
                    or distance(station, row) > 25
                ):
                    raise CommandError("Invalid operator identity evidence: " + station["id"])
                matches = [m for m in matches if m["id"] != station["id"]]
                matches.append({**row, "city_distance_miles": round(distance(station, row), 3)})
                next(r for r in report if r["id"] == station["id"])["status"] = "matched"
        if not matches:
            raise CommandError("No unambiguous identity matches; existing coordinates were not changed.")
        destination = settings.BASE_DIR / "data/station_coordinates.csv"
        temporary = destination.with_suffix(".tmp")
        with temporary.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=sorted(set().union(*(row.keys() for row in matches))), lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(matches)
        temporary.replace(destination)
        details = {
            "osm_timestamp": dates,
            "source": "OpenStreetMap contributors, ODbL 1.0; "
            "exact operator identity evidence in operator-evidence.json",
            "coverage_fraction": len(matches) / len(report),
            "total_us_stations": len(report),
            "summary": dict(Counter(r["status"] for r in report)),
            "stations": report,
        }
        (settings.BASE_DIR / "data/enrichment-report.json").write_text(json.dumps(details, indent=2) + "\n")
        self.stdout.write(json.dumps(details["summary"]))
