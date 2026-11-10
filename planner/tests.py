import json
import random
from unittest.mock import patch

import numpy as np
import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from scipy.optimize import linprog

from planner.data import location, station_points, stations
from planner.optimizer import InfeasibleRoute, candidates, optimize, origin_station
from planner.routing import (
    RoutingError,
    decode_polyline,
    fetch_route,
    segment_miles,
    simplify,
)


def station(mile, price, identity="1"):
    return {"id": identity, "route_mile": mile, "price_per_gallon": str(price)}


def encode(points, precision=6):
    """Reference polyline encoder used to build OSRM-like fixtures."""
    out, previous = [], (0, 0)
    for lon, lat in points:
        current = (round(lat * 10**precision), round(lon * 10**precision))
        for value, last in zip(current, previous, strict=True):
            delta = value - last
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                out.append(chr((0x20 | (delta & 0x1F)) + 63))
                delta >>= 5
            out.append(chr(delta + 63))
        previous = current
    return "".join(out)


def osrm_payload(points, meters, seconds=3600):
    return {
        "code": "Ok",
        "routes": [{"geometry": encode(points), "distance": meters, "duration": seconds}],
    }


def fake_route(miles, points=((-87.6, 41.9), (-86.6, 41.9))):
    coordinates = np.array(points, dtype=float)
    return {
        "cache_key": f"test:{miles}:{points}",
        "coordinates": coordinates,
        "segment_miles": np.array([miles / (len(points) - 1)] * (len(points) - 1)),
        "distance_miles": miles,
        "duration_seconds": 3600,
        "display_coordinates": coordinates.tolist(),
    }


class OptimizerTests(SimpleTestCase):
    def test_full_tank_short_trip_buys_nothing(self):
        self.assertEqual(optimize([], 400, initial_gallons=50)["total_cost_usd"], "0.00")

    def test_empty_tank_buys_exactly_trip_fuel(self):
        plan = optimize([station(0, 3)], 400)
        self.assertEqual(plan["fuel_purchased_gallons"], 40)
        self.assertEqual(plan["total_cost_usd"], "120.00")
        self.assertEqual(plan["remaining_gallons"], 0)

    def test_partial_purchase_to_cheaper_station(self):
        plan = optimize([station(400, 5), station(600, 2)], 1000, initial_gallons=50)
        self.assertEqual([s["gallons_purchased"] for s in plan["fuel_stops"]], [10, 40])
        self.assertEqual(plan["total_cost_usd"], "130.00")

    def test_fills_up_when_nothing_cheaper_in_range(self):
        plan = optimize([station(0, 2, "a"), station(450, 4, "b")], 900)
        self.assertEqual([s["gallons_purchased"] for s in plan["fuel_stops"]], [50, 40])

    def test_range_boundary_and_unreachable(self):
        self.assertEqual(
            optimize([station(500, 3)], 1000, initial_gallons=50)["total_cost_usd"],
            "150.00",
        )
        with self.assertRaises(InfeasibleRoute):
            optimize([station(501, 3)], 1000, initial_gallons=50)
        with self.assertRaises(InfeasibleRoute):
            optimize([], 501, initial_gallons=50)
        with self.assertRaises(InfeasibleRoute):
            optimize([station(10, 3)], 100)

    def test_receipts_round_half_up_and_reconcile(self):
        plan = optimize([station(0, "3.12345678")], 100)
        self.assertEqual(plan["total_cost_usd"], "31.23")
        self.assertEqual(plan["average_price_per_gallon"], "3.12")

    def test_small_top_up_merged_into_previous_stop(self):
        stations = [
            station(0, 3, "a"),
            station(400, "2.99", "b"),
            station(420, "2.98", "c"),
        ]
        exact = optimize(stations, 900)
        self.assertEqual([s["id"] for s in exact["fuel_stops"]], ["a", "b", "c"])
        merged = optimize(stations, 900, min_stop_gallons=5)
        self.assertEqual([s["id"] for s in merged["fuel_stops"]], ["a", "c"])  # b's 2 gal bought at a
        self.assertEqual(merged["small_stops_merged"], 1)
        self.assertEqual(merged["fuel_purchased_gallons"], 90)
        self.assertEqual(merged["merge_extra_cost_usd"], "0.02")  # 2 gal x $0.01
        self.assertTrue(all(s["gallons_on_departure"] <= 50 for s in merged["fuel_stops"]))

    def test_merge_never_overfills_tank(self):
        # a fills to 50; b's 1-gallon top-up cannot move to a (tank already full), so it stays.
        plan = optimize([station(0, "2.9", "a"), station(10, 3, "b")], 510, min_stop_gallons=5)
        self.assertEqual([s["gallons_purchased"] for s in plan["fuel_stops"]], [50, 1])
        self.assertEqual(plan["small_stops_merged"], 0)

    def test_merged_plans_stay_feasible(self):
        rng = random.Random(11)
        for _ in range(200):
            miles = sorted(rng.uniform(0, 1500) for _ in range(25))
            miles[0] = 0.0
            nodes = [station(m, round(rng.uniform(2.8, 3.4), 3), str(i)) for i, m in enumerate(miles)]
            try:
                plan = optimize(nodes, miles[-1] + 200, min_stop_gallons=5)
            except InfeasibleRoute:
                continue
            fuel, position = 0.0, 0.0
            for stop in plan["fuel_stops"]:
                fuel -= (stop["route_mile"] - position) / 10
                self.assertGreaterEqual(fuel, -0.02)  # receipts are rounded to 0.001 gal
                fuel += stop["gallons_purchased"]
                self.assertLessEqual(fuel, 50.02)
                position = stop["route_mile"]
            self.assertGreaterEqual(fuel - (miles[-1] + 200 - position) / 10, -0.02)

    def test_greedy_matches_independent_linear_program(self):
        rng = random.Random(7)
        for _ in range(100):
            miles = [0.0]
            for _ in range(12):
                miles.append(miles[-1] + rng.uniform(30, 300))
            distance = miles[-1] + 150
            prices = [rng.uniform(2, 6) for _ in miles]
            initial = rng.uniform(0, 50)
            rows, bounds = [], []
            # Cumulative purchases must cover fuel to the NEXT node and never overfill 50 gallons.
            for i, mile in enumerate(miles):
                prefix = [1 if j <= i else 0 for j in range(len(miles))]
                next_mile = miles[i + 1] if i + 1 < len(miles) else distance
                rows.extend([prefix, [-x for x in prefix]])
                bounds.extend([50 - initial + mile / 10, initial - next_mile / 10])
            oracle = linprog(prices, A_ub=rows, b_ub=bounds, bounds=(0, None), method="highs")
            self.assertTrue(oracle.success)
            plan = optimize(
                [station(m, p, str(i)) for i, (m, p) in enumerate(zip(miles, prices, strict=True))],
                distance,
                initial,
            )
            self.assertAlmostEqual(float(plan["unrounded_total_cost_usd"]), oracle.fun, places=5)
            self.assertTrue(all(s["gallons_on_departure"] <= 50.001 for s in plan["fuel_stops"]))


class GeometryTests(SimpleTestCase):
    def test_projection_uses_route_mileage(self):
        route = fake_route(100, ((-100, 40), (-99, 40)))
        fuel = [
            {"id": "1", "longitude": -99.5, "latitude": 40.01, "price_per_gallon": "3"},
            {"id": "2", "longitude": -99.5, "latitude": 41.0, "price_per_gallon": "1"},
        ]
        result = candidates(route, fuel, corridor_miles=5)
        self.assertEqual([s["id"] for s in result], ["1"])
        self.assertAlmostEqual(result[0]["route_mile"], 50)

    def test_polyline_round_trip_and_haversine(self):
        points = [(-87.629798, 41.878114), (-118.243685, 34.052234)]
        decoded = decode_polyline(encode(points))
        np.testing.assert_allclose(decoded, points, atol=1e-6)
        self.assertAlmostEqual(segment_miles(decoded)[0], 1745, delta=5)  # Chicago-LA great circle

    def test_simplify_keeps_endpoints_and_drops_collinear_points(self):
        line = np.array([[-90 + i / 1000, 40.0] for i in range(1001)])
        simplified = simplify(line)
        self.assertEqual(len(simplified), 2)
        np.testing.assert_array_equal(simplified[[0, -1]], line[[0, -1]])

    def test_origin_prefers_cheapest_nearby_station(self):
        start = {"latitude": 40.0, "longitude": -90.0}
        fuel = [
            {"id": "near", "price_per_gallon": "4"},
            {"id": "cheap", "price_per_gallon": "3"},
            {"id": "far", "price_per_gallon": "1"},
        ]
        points = np.array([[-90.0, 40.0], [-90.2, 40.0], [-92.0, 40.0]])
        self.assertEqual(origin_station(start, fuel, points)["id"], "cheap")
        self.assertIsNone(origin_station(start, fuel[2:], points[2:]))  # ~106 miles away


class DataTests(SimpleTestCase):
    def test_dataset_loaded(self):
        inventory, unlocated = stations()
        self.assertGreater(len(inventory), 6000)
        self.assertEqual(len(station_points()), len(inventory))
        self.assertLess(unlocated, 120)  # Canadian rows only
        by_id = {s["id"]: s for s in inventory}
        self.assertEqual(by_id["7"]["price_per_gallon"], "3.00733333")

    def test_duplicate_city_rows_resolve_to_the_city_itself(self):
        self.assertAlmostEqual(location("Orlando, FL")["longitude"], -81.38, delta=0.15)  # not Brevard County
        self.assertAlmostEqual(location("Denver, CO")["latitude"], 39.74, delta=0.1)
        self.assertEqual(location("Henrico, VA")["name"], "Richmond, VA")

    def test_location_formats(self):
        self.assertEqual(location("Chicago, IL")["name"], "Chicago, IL")
        self.assertEqual(location("chicago , illinois")["name"], "Chicago, IL")
        self.assertEqual(location("St. Louis, MO")["name"], location("st louis, mo")["name"])
        self.assertAlmostEqual(location("41.88, -87.63")["latitude"], 41.88)
        for bad in ["Paris, FR", "48.85, 2.35", "Chicago", "", None, 5]:
            with self.assertRaises(ValueError):
                location(bad)


@override_settings(OSRM_URLS=["https://primary.test", "https://mirror.test"])
class RoutingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.start, self.finish = location("Chicago, IL"), location("Milwaukee, WI")

    @patch("planner.routing.requests.get")
    def test_one_call_then_cache(self, get):
        get.return_value.json.return_value = osrm_payload([(-87.6, 41.9), (-87.9, 43.0)], 148000)
        route, calls = fetch_route(self.start, self.finish)
        self.assertEqual(calls, 1)
        self.assertAlmostEqual(route["segment_miles"].sum(), 148000 / 1609.344)
        self.assertEqual(fetch_route(self.start, self.finish)[1], 0)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"]["geometries"], "polyline6")

    @patch("planner.routing.requests.get")
    def test_falls_back_to_mirror_once(self, get):
        good = osrm_payload([(-87.6, 41.9), (-87.9, 43.0)], 148000)
        get.side_effect = [
            requests.ConnectionError("down"),
            type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: good})(),
        ]
        self.assertEqual(fetch_route(self.start, self.finish)[1], 2)
        self.assertTrue(get.call_args_list[1].args[0].startswith("https://mirror.test"))

    @patch("planner.routing.requests.get", side_effect=requests.Timeout("slow"))
    def test_all_providers_down(self, get):
        with self.assertRaises(RoutingError):
            fetch_route(self.start, self.finish)
        self.assertEqual(get.call_count, 2)

    @patch("planner.routing.requests.get")
    def test_no_route_is_not_retried(self, get):
        get.return_value.json.return_value = {"code": "NoRoute", "routes": []}
        with self.assertRaises(RoutingError):
            fetch_route(self.start, self.finish)
        get.assert_called_once()


class APITests(SimpleTestCase):
    def post(self, body):
        return self.client.post("/api/route/", json.dumps(body), content_type="application/json")

    def test_invalid_inputs(self):
        base = {"start": "Chicago, IL", "finish": "New York, NY"}
        for body in [
            [],
            {},
            {**base, "start": "Paris, FR"},
            {**base, "initial_fuel_gallons": True},
            {**base, "corridor_miles": 0},
            {**base, "initial_fuel_gallons": 51},
            {**base, "extra": 1},
            {**base, "finish": "chicago, il"},
        ]:
            response = self.post(body)
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.json()["code"], "invalid_request")
        self.assertEqual(
            self.client.post("/api/route/", "{", content_type="application/json").status_code,
            400,
        )
        self.assertEqual(self.client.put("/api/route/").status_code, 405)
        self.assertEqual(
            self.client.post("/api/route/", "x", content_type="text/plain").status_code,
            415,
        )

    @patch(
        "planner.services.fetch_route",
        return_value=(fake_route(1800, ((-87.6, 41.9), (-118.2, 34.0))), 1),
    )
    @patch("planner.services.stations")
    @patch("planner.services.station_points")
    def test_cross_country_plan(self, points, inventory, fetch):
        # Synthetic stations every ~10% of the way along the mocked straight-line route.
        lons, lats = np.linspace(-87.6, -118.2, 11), np.linspace(41.9, 34.0, 11)
        fuel = [
            {
                "id": str(i),
                "name": f"Stop {i}",
                "address": "",
                "city": "X",
                "state": "ZZ",
                "latitude": lat,
                "longitude": lon,
                "price_per_gallon": str(3 + (i % 3) / 10),
                "coordinate_precision": "city_centroid",
            }
            for i, (lon, lat) in enumerate(zip(lons, lats, strict=True))
        ]
        inventory.return_value, points.return_value = (
            (fuel, 0),
            np.column_stack([lons, lats]),
        )
        response = self.post({"start": "Chicago, IL", "finish": "Los Angeles, CA"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["map_url"].startswith("http://testserver/map/?start=Chicago"))
        self.assertEqual(data["fuel_purchased_gallons"], 180)  # empty tank: buys every gallon used
        self.assertEqual(data["fuel_stops"][0]["route_mile"], 0)
        self.assertGreaterEqual(len(data["fuel_stops"]), 4)
        kinds = [f["properties"]["kind"] for f in data["map"]["features"]]
        self.assertEqual(kinds[:3], ["route", "start", "finish"])
        self.assertEqual(kinds.count("fuel_stop"), len(data["fuel_stops"]))
        self.assertEqual(data["metadata"]["routing_calls"], 1)
        total = sum(float(s["cost_usd"]) for s in data["fuel_stops"])
        self.assertAlmostEqual(total, float(data["total_cost_usd"]), places=2)

    @patch(
        "planner.services.fetch_route",
        return_value=(fake_route(80, ((-87.6, 41.9), (-87.9, 43.0))), 0),
    )
    def test_get_query_and_full_tank(self, fetch):
        response = self.client.get(
            "/api/route/",
            {
                "start": "Chicago, IL",
                "finish": "Milwaukee, WI",
                "initial_fuel_gallons": "50",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_cost_usd"], "0.00")
        self.assertTrue(response.json()["metadata"]["route_cache_hit"])

    @patch("planner.services.fetch_route", side_effect=RoutingError("Unavailable"))
    def test_upstream_failure(self, fetch):
        response = self.post({"start": "Chicago, IL", "finish": "New York, NY"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "routing_unavailable")

    @patch("planner.services.fetch_route", return_value=(fake_route(600), 1))
    @patch("planner.services.stations", return_value=([], 0))
    @patch("planner.services.station_points", return_value=np.empty((0, 2)))
    def test_infeasible(self, points, inventory, fetch):
        response = self.post({"start": "Chicago, IL", "finish": "New York, NY"})
        self.assertEqual(response.status_code, 422)

    def test_map_page_and_health(self):
        self.assertContains(self.client.get("/map/?start=Chicago%2C+IL&finish=Denver%2C+CO"), "leaflet")
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})
