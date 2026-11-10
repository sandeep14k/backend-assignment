import json
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase

from planner.routing import NoRoute, RoutingError
from planner.tests.helpers import fake_route, index_of, make_station


def post(client, body):
    return client.post("/api/route/", json.dumps(body), content_type="application/json")


class APITests(SimpleTestCase):
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
            response = post(self.client, body)
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.json()["code"], "invalid_request")
        self.assertEqual(self.client.post("/api/route/", "{", content_type="application/json").status_code, 400)
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
        self.assertEqual(data["fuel_purchased_gallons"], 180)  # Empty tank: buys every gallon used.
        self.assertEqual(data["fuel_stops"][0]["route_mile"], 0)
        kinds = [f["properties"]["kind"] for f in data["map"]["features"]]
        self.assertEqual(kinds[:3], ["route", "start", "finish"])
        self.assertEqual(kinds.count("fuel_stop"), len(data["fuel_stops"]))
        self.assertEqual(data["metadata"]["routing_calls"], 1)
        self.assertTrue(all(stop["reason"] for stop in data["fuel_stops"]))
        self.assertGreaterEqual(float(data["savings"]["savings_usd"]), 0)
        total = sum(float(s["cost_usd"]) for s in data["fuel_stops"])
        self.assertAlmostEqual(total, float(data["total_cost_usd"]), places=2)

    @patch("planner.services.fetch_route", return_value=(fake_route(80, ((-87.6, 41.9), (-87.9, 43.0))), 0, "memory"))
    def test_get_query_and_full_tank(self, fetch):
        response = self.client.get(
            "/api/route/", {"start": "Chicago, IL", "finish": "Milwaukee, WI", "initial_fuel_gallons": "50"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_cost_usd"], "0.00")
        self.assertEqual(response.json()["metadata"]["route_source"], "memory")

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
