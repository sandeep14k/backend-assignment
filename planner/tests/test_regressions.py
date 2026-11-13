"""Reachability and verification regressions found during submission review."""

from unittest.mock import patch

from django.test import SimpleTestCase

from planner import cache
from planner.optimizer import optimize
from planner.services import plan_trip
from planner.tests.helpers import fake_route, index_of, make_station, station


class ReachabilityRegressionTests(SimpleTestCase):
    def test_same_position_with_different_access_cost_keeps_reachable_pump(self):
        exit_stop = {**station(500, "3.00", "exit"), "coordinate_precision": "exit_match"}
        exact_stop = {**station(500, "3.01", "pump"), "coordinate_precision": "osm_identity_match"}
        plan = optimize([exit_stop, exact_stop], 600, 50)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["pump"])
        self.assertEqual(plan["total_cost_usd"], "30.10")
        self.assertEqual(plan["fuel_stops"][0]["gallons_on_arrival"], 0)

    def test_automatic_start_skips_unreachable_first_candidate(self):
        far = {**station(499, "3.00", "far"), "offset_miles": 2}
        reachable = station(499.5, "3.10", "reachable")
        plan = optimize([far, reachable], 600)
        self.assertEqual(plan["initial_fuel_gallons"], 49.95)
        self.assertEqual(plan["starting_fuel"]["valued_at_price_per_gallon"], "3.10")
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["reachable"])
        self.assertEqual(plan["fuel_burned_gallons"], 60)
        self.assertEqual(plan["remaining_gallons"], 0)

    def test_stop_penalty_also_applies_without_detours(self):
        plan = optimize([station(0, "3.00", "a"), station(100, "2.99", "b")], 300, 0)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a"])
        self.assertEqual(plan["total_cost_usd"], "90.00")


class VerificationRegressionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_automatic_trip_without_purchases_keeps_valuation_when_verified(self):
        node = {**make_station("s", -87.6, 41.91), "route_mile": 0, "offset_miles": 1}
        with (
            patch("planner.services.fetch_route", return_value=(fake_route(1), 1, "loaded")),
            patch("planner.services.station_index", return_value=index_of([node])),
            patch("planner.services.candidates", return_value=[node]),
            patch("planner.services.fetch_itinerary") as itinerary,
        ):
            plain = plan_trip(node, node)
            verified = plan_trip(node, node, verify_stops=True)
        itinerary.assert_not_called()
        self.assertEqual(verified["fuel_stops"], [])
        for field in ("total_cost_usd", "starting_fuel", "initial_fuel_gallons", "remaining_gallons"):
            self.assertEqual(verified[field], plain[field])
        self.assertEqual(verified["metadata"]["verification_status"], "not_needed")

    def test_routed_town_waypoint_remains_explicitly_approximate(self):
        node = {
            **make_station("town", -87.6, 41.9),
            "coordinate_precision": "city_centroid",
            "coordinate_source": "",
            "route_mile": 0,
            "offset_miles": 0,
        }
        routed = {**fake_route(100), "leg_miles": [0, 100], "station_snap_metres": [900]}
        with (
            patch("planner.services.fetch_route", return_value=(fake_route(100), 1, "loaded")),
            patch("planner.services.station_index", return_value=index_of([node])),
            patch("planner.services.candidates", return_value=[node]),
            patch("planner.services.fetch_itinerary", return_value=(routed, 1, "loaded")),
        ):
            result = plan_trip(node, node, 0, verify_stops=True)
        self.assertTrue(result["metadata"]["itinerary_routed"])
        self.assertEqual(result["metadata"]["approximate_stop_count"], 1)
        self.assertEqual(result["metadata"]["verification_status"], "waypoints_routed")
        self.assertEqual(result["warnings"][0]["code"], "approximate_station_locations")
        self.assertEqual(result["warnings"][0]["station_ids"], ["town"])
        self.assertIn("town centre", result["fuel_stops"][0]["location_note"])
        self.assertIn("not the actual pumps", result["assumptions"]["verification_scope"])

    def test_exit_waypoint_keeps_ramp_allowance_and_location_warning(self):
        node = {
            **make_station("exit", -87.6, 41.9),
            "coordinate_precision": "exit_match",
            "route_mile": 0,
            "offset_miles": 0,
        }
        routed = {**fake_route(100), "leg_miles": [0, 100], "station_snap_metres": [5]}
        with (
            patch("planner.services.fetch_route", return_value=(fake_route(100), 1, "loaded")),
            patch("planner.services.station_index", return_value=index_of([node])),
            patch("planner.services.candidates", return_value=[node]),
            patch("planner.services.fetch_itinerary", return_value=(routed, 1, "loaded")),
        ):
            result = plan_trip(node, node, 1, verify_stops=True)
        self.assertEqual(result["fuel_stops"][0]["detour_miles"], 1)
        self.assertAlmostEqual(result["fuel_burned_gallons"], 10.1)
        self.assertEqual(result["metadata"]["approximate_stop_count"], 1)
        self.assertIn("exit", result["fuel_stops"][0]["location_note"])

    def test_sourced_pump_does_not_get_an_approximate_location_warning(self):
        node = {**make_station("pump", -87.6, 41.9), "route_mile": 0, "offset_miles": 0}
        with (
            patch("planner.services.fetch_route", return_value=(fake_route(100), 1, "loaded")),
            patch("planner.services.station_index", return_value=index_of([node])),
            patch("planner.services.candidates", return_value=[node]),
        ):
            result = plan_trip(node, node, 0)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["metadata"]["approximate_stop_count"], 0)
        self.assertEqual(result["metadata"]["verification_status"], "not_requested")
