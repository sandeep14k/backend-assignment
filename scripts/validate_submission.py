"""Real station acceptance tests. --record captures low-volume live OSRM responses.

Default mode replays exact URL/parameter fixtures and prohibits network access.
Failures remain failures: recording never changes the expected outcome.
"""

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.test import Client, override_settings  # noqa: E402

from planner import cache, routing  # noqa: E402
from planner.data import stations  # noqa: E402

CASES = [
    ("chicago_la", "Chicago, IL", "Los Angeles, CA", None, 200),
    ("seattle_boston", "Seattle, WA", "Boston, MA", None, 200),
    ("ny_miami", "New York, NY", "Miami, FL", None, 200),
    ("north_south", "Minneapolis, MN", "Dallas, TX", None, 200),
    ("rural", "Billings, MT", "Rapid City, SD", None, 200),
    ("chicago_short", "Chicago, IL", "Milwaukee, WI", 50, 200),
    ("seattle_short", "Seattle, WA", "Portland, OR", 50, 200),
    ("ny_alias", "NYC", "Boston, MA", 50, 200),
    ("coordinates", "41.88, -87.63", "Milwaukee, WI", 50, 200),
    ("partial_tank", "Dallas, TX", "Fort Worth, TX", 10, 200),
    ("southern_short", "Miami, FL", "Fort Lauderdale, FL", 10, 200),
    ("western_short", "Los Angeles, CA", "San Diego, CA", 50, 200),
    ("chicago_houston", "Chicago, IL", "Houston, TX", None, 200),
    ("la_houston", "Los Angeles, CA", "Houston, TX", None, 200),
    ("dallas_denver", "Dallas, TX", "Denver, CO", None, 200),
    ("seattle_miami", "Seattle, WA", "Miami, FL", None, 200),
    ("bangor_san_diego", "Bangor, ME", "San Diego, CA", None, 200),
    ("verified_chicago_houston", "Chicago, IL", "Houston, TX", None, 200, {"verify_stops": True}),
    ("verified_ny_miami", "New York, NY", "Miami, FL", None, 200, {"verify_stops": True}),
    # The supplied CSV lists only 8 California stations: these routes have >500-mile gaps.
    ("gap_sf_vegas", "San Francisco, CA", "Las Vegas, NV", None, 422),
    ("gap_portland_phoenix", "Portland, OR", "Phoenix, AZ", None, 422),
    ("empty_remote", "Chicago, IL", "Los Angeles, CA", 0, 422),
    ("disconnected", "Honolulu, HI", "Chicago, IL", 50, 422),
    ("coverage_alaska", "Anchorage, AK", "Fairbanks, AK", None, 422),
    ("non_us", "Toronto, ON", "Boston, MA", None, 400),
    ("foreign_coords", "48.85, 2.35", "Boston, MA", None, 400),
    ("empty_start", "", "Boston, MA", None, 400),
    ("same_place", "Chicago, IL", "Chicago, IL", None, 400),
    ("ambiguous", "Kansas City", "Boston, MA", None, 400),
    ("bad_tank", "Chicago, IL", "Boston, MA", 51, 400),
    ("negative_tank", "Chicago, IL", "Boston, MA", -1, 400),
    ("unknown_city", "Notacity, IL", "Boston, MA", None, 400),
    ("border", "42.3149, -83.0364", "Detroit, MI", None, 400),
]


def verify(data):
    """Independently re-simulate the tank and re-add every receipt."""
    inventory = {s["id"]: s for s in stations()[0]}
    assert len(data["map"]["features"][0]["geometry"]["coordinates"]) >= 2
    assert data["map_url"].startswith("http://testserver/map/")
    kinds = [feature["properties"]["kind"] for feature in data["map"]["features"]]
    assert kinds.count("fuel_stop") == len(data["fuel_stops"])
    for stop in data["fuel_stops"]:
        source = inventory[stop["id"]]
        assert source["coordinate_precision"] == stop["coordinate_precision"]
        assert source["coordinate_source"] == stop["coordinate_source"]
        assert (source["latitude"], source["longitude"]) == (stop["latitude"], stop["longitude"])
        assert source["price_per_gallon"] == stop["price_per_gallon"]
        assert abs(float(stop["cost_usd"]) - stop["gallons_purchased"] * float(stop["price_per_gallon"])) <= 0.0051
    if data["metadata"]["itinerary_routed"]:
        verify_routed(data)
    else:
        verify_estimated(data)


def verify_estimated(data):
    """Default mode: one routing call; each stop's detour (both ways) is drawn from the tank."""
    assert data["metadata"]["routing_calls"] in (0, 1)  # 0 when cached.
    receipts = Decimal(data["starting_fuel"]["cost_usd"]) if data["starting_fuel"] else Decimal(0)
    fuel, position, burned = data["initial_fuel_gallons"], 0.0, data["distance_miles"] / 10
    for stop in data["fuel_stops"]:
        fuel -= (stop["route_mile"] - position) / 10 + stop["detour_miles"] / 20
        position = stop["route_mile"]
        assert fuel >= -0.001, "Empty before pump"
        assert abs(fuel - stop["gallons_on_arrival"]) < 0.001
        fuel += stop["gallons_purchased"]
        assert fuel <= 50.001, "Over tank capacity"
        fuel -= stop["detour_miles"] / 20
        burned += stop["detour_miles"] / 10
        receipts += Decimal(stop["cost_usd"])
    fuel -= (data["distance_miles"] - position) / 10
    assert fuel >= -0.01, "Empty before finish"
    assert abs(fuel - data["remaining_gallons"]) < 0.01
    assert abs(burned - data["fuel_burned_gallons"]) < 0.01
    assert receipts == Decimal(data["total_cost_usd"])


def verify_routed(data):
    """verify_stops mode: OSRM road legs through every selected station."""
    assert 0 <= data["metadata"]["routing_calls"] <= 3
    receipts = Decimal(data["starting_fuel"]["cost_usd"]) if data["starting_fuel"] else Decimal(0)
    purchases = {s["id"]: s for s in data["fuel_stops"]}
    fuel, burned = data["initial_fuel_gallons"], sum(data["routed_leg_miles"]) / 10
    for leg, waypoint in zip(data["routed_leg_miles"][:-1], data["routed_waypoints"], strict=True):
        fuel -= leg / 10
        stop = purchases.get(waypoint["id"])
        if stop:
            # Exit waypoints lie on the motorway; the pumps are a ramp away (detour_miles, both ways).
            fuel -= stop["detour_miles"] / 20
            assert fuel >= -0.0001, "Empty before pump"
            assert abs(fuel - stop["gallons_on_arrival"]) < 0.0001
            fuel += stop["gallons_purchased"]
            assert fuel <= 50.0001, "Over tank capacity"
            fuel -= stop["detour_miles"] / 20
            burned += stop["detour_miles"] / 10
            receipts += Decimal(stop["cost_usd"])
        assert fuel >= -0.0001, "Empty on the road"
    fuel -= data["routed_leg_miles"][-1] / 10
    assert fuel >= -0.0001
    assert abs(fuel - data["remaining_gallons"]) < 0.0001
    assert abs(burned - data["fuel_burned_gallons"]) < 0.0001
    assert receipts == Decimal(data["total_cost_usd"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    directory = ROOT / "planner/tests/fixtures/routing"
    directory.mkdir(parents=True, exist_ok=True)
    client, results = Client(), []
    real_get = routing._session.get
    isolated = {"routes": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "acceptance"}}
    for name, start, finish, initial, expected, *extra in CASES:
        if args.only and name not in args.only:
            continue
        path = directory / f"{name}.json"
        records = [] if args.record else json.loads(path.read_text()) if path.exists() else []
        cursor = 0

        def transport(url, records=records, name=name, **kwargs):
            nonlocal cursor
            key = {"url": url, "params": kwargs["params"]}
            if args.record:
                response = real_get(url, **kwargs)
                records.append(
                    {**key, "status": response.status_code, "body": response.text, "headers": dict(response.headers)}
                )
                return response
            assert cursor < len(records), f"Missing routing fixture: {name}"
            row = records[cursor]
            cursor += 1
            assert key == {k: row[k] for k in key}, f"Stale routing fixture: {name}"
            response = requests.Response()
            response.status_code = row["status"]
            response._content = row["body"].encode()
            response.headers.update(row["headers"])
            return response

        body = {"start": start, "finish": finish}
        if initial is not None:
            body["initial_fuel_gallons"] = initial
        body.update(*extra)
        started = time.perf_counter()
        try:
            with override_settings(CACHES=isolated), patch.object(routing._session, "get", side_effect=transport):
                cache.clear()
                response = client.post("/api/route/", json.dumps(body), content_type="application/json")
                data = response.json()
                assert len(records) <= 3
                if not args.record:
                    assert cursor == len(records), "Unused fixture calls"
                assert response.status_code == expected, data
                if expected == 200:
                    verify(data)
                    assert client.get(data["map_url"]).status_code == 200
                else:
                    assert data["code"] == (
                        "invalid_request"
                        if expected == 400
                        else "no_route"
                        if name == "disconnected"
                        else "insufficient_station_coverage"
                    )
                result = {
                    "name": name,
                    "passed": True,
                    "status": response.status_code,
                    "routing_calls": data.get("metadata", {}).get("routing_calls"),
                    "response_bytes": len(response.content),
                }
        except Exception as exc:
            result = {"name": name, "passed": False, "error": str(exc)}
        if args.record:
            path.write_text(json.dumps(records, indent=2) + "\n")
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        results.append(result)
        print(json.dumps(result), flush=True)
    report = {"mode": "live" if args.record else "recorded HTTP replay", "results": results}
    (ROOT / "docs/acceptance-results.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
