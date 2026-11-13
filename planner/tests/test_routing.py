import threading
import time
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from planner import cache
from planner.data import location
from planner.routing import NoRoute, RoutingError, fetch_route
from planner.tests.helpers import osrm_response

LINE = [(-87.6, 41.9), (-87.75, 42.4), (-87.9, 43.0)]

SOURCED = {"coordinate_precision": "osm_identity_match", "coordinate_source": "https://www.openstreetmap.org/node/1"}


@override_settings(OSRM_URLS=["https://primary.test", "https://mirror.test"], OSRM_HEDGE_AFTER_SECONDS=0.2)
@patch("planner.routing._session.get")
class RoutingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.start, self.finish = location("Chicago, IL"), location("Milwaukee, WI")

    def test_one_compact_call_then_memory_cache(self, get):
        get.return_value = osrm_response(LINE, 148000)
        route, calls, source = fetch_route(self.start, self.finish)
        self.assertEqual((calls, source), (1, "loaded"))
        self.assertAlmostEqual(route["segment_miles"].sum(), 148000 / 1609.344)
        self.assertEqual(fetch_route(self.start, self.finish)[1:], (0, "memory"))
        get.assert_called_once()
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["geometries"], params["overview"]), ("polyline6", "full"))
        self.assertNotIn("annotations", params)

    def test_second_level_cache_survives_process_memory_loss(self, get):
        get.return_value = osrm_response(LINE, 148000)
        fetch_route(self.start, self.finish)
        cache._l1.clear()  # Simulate a restart / another worker.
        self.assertEqual(fetch_route(self.start, self.finish)[1:], (0, "disk"))
        get.assert_called_once()

    def test_fast_failure_falls_back_to_mirror(self, get):
        get.side_effect = [requests.ConnectionError("down"), osrm_response(LINE, 148000)]
        self.assertEqual(fetch_route(self.start, self.finish)[1], 2)
        self.assertTrue(get.call_args_list[1].args[0].startswith("https://mirror.test"))

    def test_slow_primary_preserves_repair_allowance(self, get):
        def respond(url, **kwargs):
            if url.startswith("https://primary.test"):
                time.sleep(1.0)
            return osrm_response(LINE, 148000)

        get.side_effect = respond
        started = time.perf_counter()
        route, calls, _ = fetch_route(self.start, self.finish)
        self.assertGreaterEqual(time.perf_counter() - started, 1.0)
        self.assertEqual(calls, 1)

    def test_all_providers_down(self, get):
        get.side_effect = requests.Timeout("slow")
        with self.assertRaises(RoutingError):
            fetch_route(self.start, self.finish)
        self.assertEqual(get.call_count, 2)

    def test_no_route_is_final_and_cached(self, get):
        response = osrm_response(LINE, 1)
        response.json.return_value = {"code": "NoRoute", "routes": []}
        get.return_value = response
        for _ in range(2):
            with self.assertRaises(NoRoute):
                fetch_route(self.start, self.finish)
        get.assert_called_once()  # Not retried on the mirror, and not asked again.

    def test_concurrent_requests_share_one_call(self, get):
        def respond(url, **kwargs):
            time.sleep(0.1)
            return osrm_response(LINE, 148000)

        get.side_effect = respond
        results = []

        def request():
            results.append(fetch_route(self.start, self.finish))

        threads = [threading.Thread(target=request) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(sum(calls for _, calls, _ in results), 1)

    def test_invalid_geometry_falls_back(self, get):
        invalid = osrm_response(LINE, 148000)
        invalid.json.return_value["routes"][0]["geometry"] = "_"
        get.side_effect = [invalid, osrm_response(LINE, 148000)]
        self.assertEqual(fetch_route(self.start, self.finish)[1], 2)

    def test_invalid_distance_fails_as_provider_error(self, get):
        for distance in [float("nan"), float("inf"), -1]:
            get.return_value = osrm_response(LINE, distance)
            with self.assertRaises(RoutingError):
                fetch_route(self.start, self.finish)

    @override_settings(OSRM_URLS=[])
    def test_empty_configuration_is_controlled_error(self, get):
        with self.assertRaises(RoutingError):
            fetch_route(self.start, self.finish)
        get.assert_not_called()

    @override_settings(OSRM_URLS=["https://one.test", "https://two.test", "https://three.test"])
    def test_call_budget_even_with_extra_urls(self, get):
        get.side_effect = requests.Timeout("down")
        with self.assertRaises(RoutingError):
            fetch_route(self.start, self.finish)
        self.assertEqual(get.call_count, 2)

    def test_changed_route_gets_new_derived_cache_key(self, get):
        get.return_value = osrm_response(LINE, 148000)
        first = fetch_route(self.start, self.finish)[0]
        cache.clear()
        get.return_value = osrm_response(LINE, 149000)
        second = fetch_route(self.start, self.finish)[0]
        self.assertNotEqual(first["cache_key"], second["cache_key"])

    def test_itinerary_requires_leg_and_snap_evidence(self, get):
        from planner.routing import fetch_itinerary

        sourced = {**self.start, **SOURCED}

        response = osrm_response(LINE, 148000)
        get.return_value = response
        with self.assertRaises(RoutingError):
            fetch_itinerary(self.start, self.finish, [sourced])
        self.assertEqual(get.call_count, 1)
        payload = response.json.return_value
        payload["routes"][0]["legs"] = [{"distance": 0}, {"distance": 148000}]
        payload["waypoints"] = [{"distance": 0}, {"distance": 10}, {"distance": 0}]
        route, calls, _ = fetch_itinerary(self.start, self.finish, [sourced])
        self.assertEqual(calls, 1)
        self.assertEqual(route["station_snap_metres"], [10])
        self.assertEqual(fetch_itinerary(self.start, self.finish, [sourced])[1], 0)
        self.assertEqual(get.call_args.kwargs["params"]["radiuses"], "unlimited;100;unlimited")

    def test_itinerary_rejects_remote_road_snap(self, get):
        from planner.routing import fetch_itinerary

        response = osrm_response(LINE, 148000)
        response.json.return_value["routes"][0]["legs"] = [{"distance": 0}, {"distance": 148000}]
        response.json.return_value["waypoints"] = [{"distance": 0}, {"distance": 200}, {"distance": 0}]
        get.return_value = response
        with self.assertRaises(NoRoute):
            fetch_itinerary(self.start, self.finish, [{**self.start, **SOURCED}])

    def test_exit_stops_snap_to_the_carriageway_in_the_direction_of_travel(self, get):
        from planner.routing import fetch_itinerary

        response = osrm_response(LINE, 148000)
        response.json.return_value["routes"][0]["legs"] = [{"distance": 0}, {"distance": 148000}]
        response.json.return_value["waypoints"] = [{"distance": 0}, {"distance": 5}, {"distance": 0}]
        get.return_value = response
        on_exit = {**self.start, "coordinate_precision": "exit_match", "coordinate_source": "", "heading": 271.6}
        fetch_itinerary(self.start, self.finish, [on_exit])
        self.assertEqual(get.call_args.kwargs["params"]["bearings"], ";272,60;")
        fetch_itinerary(self.start, self.finish, [{**on_exit, "heading": None}])
        self.assertNotIn("bearings", get.call_args.kwargs["params"])

    def test_city_centroid_stations_snap_to_nearest_road_at_any_distance(self, get):
        from planner.routing import fetch_itinerary

        response = osrm_response(LINE, 148000)
        response.json.return_value["routes"][0]["legs"] = [{"distance": 0}, {"distance": 148000}]
        response.json.return_value["waypoints"] = [{"distance": 0}, {"distance": 900}, {"distance": 0}]
        get.return_value = response
        centroid = {**self.start, "coordinate_precision": "city_centroid", "coordinate_source": ""}
        route, _, _ = fetch_itinerary(self.start, self.finish, [centroid])
        self.assertEqual(route["station_snap_metres"], [900])
        self.assertEqual(get.call_args.kwargs["params"]["radiuses"], "unlimited;unlimited;unlimited")

    def test_public_requests_are_paced_without_delaying_private_servers(self, get):
        from planner.routing import _pace_public_provider

        with (
            patch("planner.routing._public_last_request", {}),
            patch("planner.routing.time.monotonic", return_value=100),
            patch("planner.routing.time.sleep") as sleep,
        ):
            _pace_public_provider("https://router.project-osrm.org")
            sleep.assert_not_called()
            _pace_public_provider("https://router.project-osrm.org")
            sleep.assert_called_once_with(1.0)
            _pace_public_provider("https://private.test")
            sleep.assert_called_once()
