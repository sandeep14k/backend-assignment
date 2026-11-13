"""Write data/exit_coordinates.csv: stations placed at the OSM exit their CSV address names."""

import csv
import json
from collections import Counter

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.data import find_city, stations
from planner.exits import junctions_from_overpass, match_exit, miles_between, parse_exit_address

CONFLICT_MILES = 3.0
FIELDS = ["id", "latitude", "longitude", "precision", "source", "exit", "routes", "osm_nodes", "town_miles", "osm_date"]


class Command(BaseCommand):
    help = "Match 'I-44, EXIT 283' style CSV addresses to OSM motorway exits (run scripts/download_exit_junctions.py)."

    def handle(self, *args, **options):
        directory = settings.BASE_DIR / "data/enrichment/junctions"
        files = sorted(directory.glob("*.json"))
        if not files:
            raise CommandError("No exit data. Run: python scripts/download_exit_junctions.py")
        junctions, dates = {}, {}
        for path in files:
            payload = json.loads(path.read_text())
            if payload.get("remark") or "elements" not in payload:
                raise CommandError(f"{path.name} is incomplete; re-download it.")
            junctions[path.stem] = junctions_from_overpass(payload["elements"])
            dates[path.stem] = payload.get("osm3s", {}).get("timestamp_osm_base", "")

        def place(station):
            town = find_city(station["city"], station["state"])
            if town is None or station["state"] not in junctions:
                return None
            lat, lon = float(town["LATITUDE"]), float(town["LONGITUDE"])
            return match_exit(station["address"], lat, lon, junctions[station["state"]])

        identity = _identity_positions()
        rows, outcome, errors, conflicts = [], Counter(), [], []
        for station in stations()[0]:
            if station["id"] in identity:
                # Ground truth: place this identity-matched station by its exit address too and compare.
                check = place(station)
                if check is None:
                    outcome["already exact"] += 1
                    continue
                error = float(miles_between(*identity[station["id"]], check["latitude"], check["longitude"]))
                errors.append(error)
                if error <= CONFLICT_MILES:
                    outcome["already exact"] += 1
                    continue
                # The address names an exit far from the matched store: the identity match is wrong.
                outcome["identity match contradicted by its exit, replaced"] += 1
                conflicts.append({"id": station["id"], "name": station["name"], "miles": round(error, 2)})
                found = check
            else:
                if parse_exit_address(station["address"]) is None:
                    outcome["no exit number in address"] += 1
                    continue
                found = place(station)
                if found is None:
                    outcome["exit not matched or ambiguous"] += 1
                    continue
                outcome["placed at exit"] += 1
            rows.append(
                {
                    "id": station["id"],
                    "latitude": found["latitude"],
                    "longitude": found["longitude"],
                    "precision": "exit_match",
                    "source": f"https://www.openstreetmap.org/node/{found['nodes'][0]}",
                    "exit": found["exit"],
                    "routes": " ".join(found["routes"]),
                    "osm_nodes": " ".join(map(str, found["nodes"])),
                    "town_miles": found["town_miles"],
                    "osm_date": dates[station["state"]],
                }
            )

        with (settings.BASE_DIR / "data/exit_coordinates.csv").open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, FIELDS)
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: int(row["id"])))
        report = {
            "states": sorted(junctions),
            "outcome": dict(outcome.most_common()),
            "accuracy_against_exactly_located_stations": _accuracy(errors),
            "identity_matches_replaced": conflicts,
        }
        (settings.BASE_DIR / "data/exit-report.json").write_text(json.dumps(report, indent=2) + "\n")
        for name, count in outcome.most_common():
            self.stdout.write(f"{count:>6}  {name}")
        self.stdout.write(
            f"Accuracy against exactly located stations: {report['accuracy_against_exactly_located_stations']}"
        )
        self.stdout.write(f"Wrote data/exit_coordinates.csv ({len(rows)} stations)")


def _identity_positions():
    path = settings.BASE_DIR / "data/station_coordinates.csv"
    with path.open(encoding="utf-8") as source:
        return {row["id"]: (float(row["latitude"]), float(row["longitude"])) for row in csv.DictReader(source)}


def _accuracy(errors):
    """Distance (miles) from the exit position to the station's known position."""
    if not errors:
        return {"stations": 0}
    values = np.sort(np.array(errors))
    return {
        "stations": len(values),
        "median_miles": round(float(np.median(values)), 2),
        "p90_miles": round(float(np.percentile(values, 90)), 2),
        "max_miles": round(float(values[-1]), 2),
        "within_1_mile": round(float((values <= 1).mean()), 3),
    }
