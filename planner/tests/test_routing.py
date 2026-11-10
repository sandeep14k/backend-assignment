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

    def test_slow_primary_is_hedged_and_fastest_answer_wins(self, get):
        def respond(url, **kwargs):
            if url.startswith("https://primary.test"):
                time.sleep(1.0)
            return osrm_response(LINE, 148000)

        get.side_effect = respond
        started = time.perf_counter()
        route, calls, _ = fetch_route(self.start, self.finish)
        self.assertLess(time.perf_counter() - started, 0.8)  # Did not wait for the slow primary.
        self.assertEqual(calls, 2)

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
