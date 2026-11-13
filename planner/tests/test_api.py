import json
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase

from planner import cache
from planner.routing import NoRoute, RoutingError
from planner.tests.helpers import fake_route, index_of, make_station


def post(client, body):
    return client.post("/api/route/", json.dumps(body), content_type="application/json")


class APITests(SimpleTestCase):
    def setUp(self):
        cache.clear()

        def itinerary(start, finish, stops, max_calls, budget=None):
            from planner.services import fetch_route

            base = fetch_route(start, finish)[0]
            miles = [0, *[s["route_mile"] for s in stops], base["distance_miles"]]
            return {**base, "leg_miles": list(np.diff(miles)), "station_snap_metres": [0] * len(stops)}, 0, "memory"

        self.itinerary = patch("planner.services.fetch_itinerary", side_effect=itinerary).start()
        self.addCleanup(patch.stopall)

    def test_invalid_inputs(self):
        base = {"start": "Chicago, IL", "finish": "New York, NY"}
        for body in [
            [],
            {},
            {**base, "start": "Paris, FR"},
            {**base, "initial_fuel_gallons": True},
            {**base, "corridor_miles": 0},
            {**base, "initial_fuel_gallons": 51},
            {**base, "initial_fuel_gallons": 10**400},
            {**base, "extra": 1},
            {**base, "finish": "chicago, il"},
            {**base, "start": ",,,"},
            {**base, "start": "43.6532, -79.3832"},  # Toronto
        ]:
            response = post(self.client, body)
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.json()["code"], "invalid_request")
        broken = self.client.post("/api/route/", "{", content_type="application/json")
        self.assertEqual(broken.status_code, 400)
        self.assertIn("must be valid JSON", broken.json()["error"])
        for canadian in ["Toronto, ON", "Vancouver BC", "Montreal, QC, Canada"]:
            response = post(self.client, {**base, "start": canadian})
            self.assertEqual(response.status_code, 400, canadian)
            self.assertIn("is in Canada", response.json()["error"])
        self.assertEqual(self.client.put("/api/route/").status_code, 405)
        self.assertEqual(self.client.post("/api/route/", "x", content_type="text/plain").status_code, 415)

    @patch("planner.services.station_index")
    @patch(
        "planner.services.fetch_route",
        return_value=(
            fake_route(1800, tuple(zip(np.linspace(-87.6, -118.2, 201), np.linspace(41.9, 34.0, 201), strict=True))),
            1,
            "loaded",
        ),
    )
    def test_cross_country_plan(self, fetch, station_index):
        # Synthetic stations every ~10% of the way along the mocked straight-line route.
        lons, lats = np.linspace(-87.6, -118.2, 11), np.linspace(41.9, 34.0, 11)
        stations = [
            make_station(i, lon, lat, 3 + (i % 3) / 10) for i, (lon, lat) in enumerate(zip(lons, lats, strict=True))
        ]
        station_index.return_value = index_of(stations)
        response = post(self.client, {"start": "Chicago, IL", "finish": "Los Angeles, CA"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["map_url"].startswith("http://testserver/map/?start=Chicago"))
        self.assertAlmostEqual(data["fuel_burned_gallons"], 180, delta=1)  # Road fuel plus small detours.
        kinds = [f["properties"]["kind"] for f in data["map"]["features"]]
        self.assertEqual(kinds[:3], ["route", "start", "finish"])
        self.assertEqual(kinds.count("fuel_stop"), len(data["fuel_stops"]))
        self.assertEqual(data["metadata"]["routing_calls"], 1)
        self.itinerary.assert_not_called()  # Default: the one route call is the only routing request.
        self.assertFalse(data["metadata"]["itinerary_routed"])
        # The tank never goes negative or above capacity, and detour fuel is conserved in the totals.
        for stop in data["fuel_stops"]:
            self.assertGreaterEqual(stop["gallons_on_arrival"], 0)
            self.assertLessEqual(stop["gallons_on_departure"], 50 + 1e-6)
        self.assertAlmostEqual(
            data["initial_fuel_gallons"] + data["fuel_purchased_gallons"] - data["fuel_burned_gallons"],
            data["remaining_gallons"],
            places=4,
        )
        self.assertTrue(all(stop["reason"] for stop in data["fuel_stops"]))
        self.assertIn("savings_usd", data["savings"])
        total = sum(float(s["cost_usd"]) for s in data["fuel_stops"])
        self.assertAlmostEqual(total, float(data["total_cost_usd"]), places=2)
        self.assertFalse(data["metadata"]["plan_cache_hit"])
        again = post(self.client, {"start": "Chicago, IL", "finish": "Los Angeles, CA"}).json()
        self.assertTrue(again["metadata"]["plan_cache_hit"])
        self.assertEqual(again["fuel_stops"], data["fuel_stops"])
        self.assertEqual(again["total_cost_usd"], data["total_cost_usd"])

    @patch("planner.services.fetch_route", return_value=(fake_route(80, ((-87.6, 41.9), (-87.9, 43.0))), 0, "memory"))
    def test_get_query_and_full_tank(self, fetch):
        response = self.client.get(
            "/api/route/", {"start": "Chicago, IL", "finish": "Milwaukee, WI", "initial_fuel_gallons": "50"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_cost_usd"], "0.00")
        self.assertEqual(response.json()["metadata"]["route_source"], "memory")

    @patch("planner.services.fetch_route", return_value=(fake_route(80, ((-87.6, 41.9), (-87.9, 43.0))), 0, "memory"))
    def test_verify_stops_must_be_boolean(self, fetch):
        response = post(self.client, {"start": "Chicago, IL", "finish": "Milwaukee, WI", "verify_stops": "maybe"})
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_request"))
        response = self.client.get(
            "/api/route/",
            {"start": "Chicago, IL", "finish": "Milwaukee, WI", "initial_fuel_gallons": "50", "verify_stops": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("verify_stops=true", response.json()["map_url"])

    @patch("planner.services.fetch_route", side_effect=RoutingError("Unavailable"))
    def test_upstream_failure(self, fetch):
        response = post(self.client, {"start": "Chicago, IL", "finish": "New York, NY"})
        self.assertEqual((response.status_code, response.json()["code"]), (503, "routing_unavailable"))

    @patch("planner.services.fetch_route", side_effect=NoRoute("No drivable route"))
    def test_no_route(self, fetch):
        response = post(self.client, {"start": "Honolulu, HI", "finish": "Chicago, IL"})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "no_route"))

    @patch("planner.services.station_index", return_value=index_of([]))
    @patch("planner.services.fetch_route", return_value=(fake_route(600), 1, "loaded"))
    def test_insufficient_coverage(self, fetch, station_index):
        response = post(self.client, {"start": "Chicago, IL", "finish": "New York, NY"})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "insufficient_station_coverage"))

    def test_map_page_and_health(self):
        self.assertContains(self.client.get("/map/?start=Chicago%2C+IL&finish=Denver%2C+CO"), "leaflet")
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})

    @patch("planner.views.plan_trip")
    def test_map_url_round_trips_normalized_numeric_options(self, plan):
        from urllib.parse import parse_qs, urlsplit

        plan.return_value = {"metadata": {"timing_ms": {}}}
        for value in [None, "", "0", 0, "5"]:
            with self.subTest(value=value):
                body = {
                    "start": "Chicago, IL",
                    "finish": "Milwaukee, WI",
                    "initial_fuel_gallons": value,
                    "corridor_miles": None,
                }
                first = post(self.client, body)
                self.assertEqual(first.status_code, 200)
                expected_options = plan.call_args.kwargs.copy()
                query = urlsplit(first.json()["map_url"]).query
                self.assertNotIn("None", query)
                if value is None or value == "":
                    self.assertNotIn("initial_fuel_gallons", parse_qs(query))
                second = self.client.get("/api/route/?" + query)
                self.assertEqual(second.status_code, 200)
                self.assertEqual(plan.call_args.kwargs, expected_options)

    @patch("planner.services.fetch_route", return_value=(fake_route(80), 0, "memory"))
    @patch("planner.services.station_index")
    def test_price_changes_invalidate_cached_candidates_and_plan(self, station_index, fetch):
        station_index.return_value = index_of([make_station("a", -87.6, 41.9, 3)])
        body = {"start": "Chicago, IL", "finish": "Milwaukee, WI", "initial_fuel_gallons": 0}
        first = post(self.client, body).json()
        station_index.return_value = index_of([make_station("a", -87.6, 41.9, 4)])
        second = post(self.client, body).json()
        self.assertEqual(first["total_cost_usd"], "24.00")
        self.assertEqual(second["total_cost_usd"], "32.00")
        self.assertFalse(second["metadata"]["plan_cache_hit"])

    def test_map_allows_referer_for_osm_tiles(self):
        response = self.client.get("/map/")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")

    @patch("planner.services.fetch_route", return_value=(fake_route(80), 2, "loaded"))
    @patch("planner.services.station_index", return_value=index_of([make_station("a", -87.6, 41.9, 3)]))
    def test_final_cost_uses_routed_legs_and_remaining_call_budget(self, index, fetch):
        routed = {**fake_route(84), "leg_miles": [0, 84], "station_snap_metres": [12]}
        self.itinerary.side_effect = None
        self.itinerary.return_value = (routed, 1, "loaded")
        response = post(
            self.client,
            {"start": "Chicago, IL", "finish": "Milwaukee, WI", "initial_fuel_gallons": 0, "verify_stops": True},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_cost_usd"], "25.20")
        self.assertEqual(data["distance_miles"], 84)
        self.assertEqual(data["fuel_burned_gallons"], 8.4)
        self.assertEqual(data["metadata"]["routing_calls"], 3)
        self.assertEqual(self.itinerary.call_args.kwargs["max_calls"], 1)
        self.assertEqual(data["fuel_stops"][0]["road_snap_metres"], 12)
        self.assertEqual(data["map"]["features"][0]["geometry"]["coordinates"], routed["display_coordinates"])

    @patch("planner.services.fetch_route", return_value=(fake_route(480), 1, "loaded"))
    @patch("planner.services.station_index", return_value=index_of([make_station("a", -87.6, 41.9, 3)]))
    def test_actual_road_leg_over_range_is_not_returned_as_feasible(self, index, fetch):
        self.itinerary.side_effect = None
        self.itinerary.return_value = (
            {**fake_route(510), "leg_miles": [0, 510], "station_snap_metres": [0]},
            1,
            "loaded",
        )
        response = post(
            self.client,
            {"start": "Chicago, IL", "finish": "Milwaukee, WI", "initial_fuel_gallons": 0, "verify_stops": True},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "insufficient_station_coverage")
