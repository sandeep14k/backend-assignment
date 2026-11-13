from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from planner import cache
from planner.optimizer import InfeasibleRoute
from planner.routing import RoutingBudget, RoutingError, _call_providers
from planner.services import plan_trip, select_adaptively
from planner.tests.helpers import fake_route, index_of, make_station


class PlanningTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_gap_expansion_needs_no_routing(self):
        route = fake_route(1200)
        first = {**make_station("a", -87.6, 41.9), "route_mile": 0, "offset_miles": 0}
        bridge = {**make_station("b", -87.1, 42.0), "route_mile": 700, "offset_miles": 7}
        second = {**first, "id": "second", "route_mile": 300}
        last = {**first, "id": "last", "route_mile": 1100}
        unrelated = {**make_station("c", -87.5, 42.0), "route_mile": 100, "offset_miles": 7}
        with (
            patch(
                "planner.services.candidates",
                side_effect=lambda r, i, radius: [first, second, last]
                if radius == 5
                else [first, second, bridge, last, unrelated],
            ),
            patch("planner.routing._request", side_effect=AssertionError("Network")),
        ):
            plan, nodes, radius = select_adaptively(route, index_of([]), 0, 5)
        self.assertEqual(radius, 10)
        self.assertNotIn("c", [s["id"] for s in nodes])
        self.assertIn("b", [s["id"] for s in plan["fuel_stops"]])
        self.assertGreaterEqual(plan["remaining_gallons"], 0)

    @override_settings(OSRM_URLS=["https://a.test", "https://b.test"])
    def test_failures_charge_shared_budget_and_fourth_call_is_impossible(self):
        budget = RoutingBudget()
        with patch("planner.routing._request", side_effect=requests.Timeout()) as call:
            for _ in range(4):
                with self.assertRaises(RoutingError):
                    _call_providers("a;b", budget=budget)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(budget.used, 3)

    def run_repair(self, final_distance):
        first = {**make_station("a", -87.6, 41.9), "route_mile": 0, "offset_miles": 0}
        bridge = {**make_station("b", -87.1, 41.9), "route_mile": 250, "offset_miles": 0}
        base = fake_route(480)
        long = {**fake_route(510), "leg_miles": [0, 510], "station_snap_metres": [0]}
        repaired = {
            **fake_route(final_distance),
            "leg_miles": [0, 250, final_distance - 250],
            "station_snap_metres": [0, 0],
        }
        with (
            patch("planner.services.fetch_route", return_value=(base, 1, "loaded")),
            patch("planner.services.station_index", return_value=index_of([first, bridge])),
            patch("planner.services.select_adaptively", return_value=({"fuel_stops": [first]}, [first], 5)),
            patch("planner.services.candidates", return_value=[first, bridge]),
            patch(
                "planner.services.fetch_itinerary", side_effect=[(long, 1, "loaded"), (repaired, 1, "loaded")]
            ) as fetch,
        ):
            result = plan_trip(first, bridge, 0, verify_stops=True)
            self.assertEqual(fetch.call_count, 2)
            return result

    def test_successful_repair(self):
        result = self.run_repair(520)
        self.assertEqual(result["metadata"]["routing_calls"], 3)
        self.assertEqual(result["fuel_burned_gallons"], 52)
        self.assertEqual(len(result["routed_waypoints"]), 2)

    def test_failed_repair_is_not_returned_as_success(self):
        with self.assertRaises(InfeasibleRoute):
            self.run_repair(800)

    def test_coverage_error_names_the_gap(self):
        far = {**make_station("far", -87.0, 41.9), "city": "Jean", "state": "NV", "route_mile": 537, "offset_miles": 9}
        with patch("planner.services.candidates", return_value=[far]), self.assertRaises(InfeasibleRoute) as error:
            select_adaptively(fake_route(569), index_of([]), None, 5)
        self.assertIn("between mile 0 (the start) and mile 537 (Jean, NV): a 537-mile gap", str(error.exception))

    def test_starting_fuel_reason_is_kept_when_no_gap_explains_it(self):
        near = {**make_station("a", -87.6, 41.9), "route_mile": 20, "offset_miles": 0}
        with patch("planner.services.candidates", return_value=[near]), self.assertRaises(InfeasibleRoute) as error:
            select_adaptively(fake_route(300), index_of([]), 0, 5)
        self.assertIn("Starting with 0 gal is not enough to reach a station", str(error.exception))
