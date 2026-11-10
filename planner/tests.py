import json
import random
from decimal import Decimal
from unittest.mock import patch
from django.test import SimpleTestCase
from django.core.cache import cache
from scipy.optimize import linprog
from planner.optimizer import optimize, candidates, InfeasibleRoute
from planner.routing import fetch_route, RoutingError
from planner.data import location, stations


def station(mile, price, identity='1'):
    return {'id': identity, 'route_mile': mile, 'price_per_gallon': str(price)}


class OptimizerTests(SimpleTestCase):
    def test_short_trip_buys_nothing(self):
        self.assertEqual(optimize([], 400)['total_cost_usd'], '0.00')

    def test_partial_purchase_to_cheaper_station(self):
        plan = optimize([station(400, 5), station(600, 2)], 1000)
        self.assertEqual([s['gallons_purchased'] for s in plan['fuel_stops']], [10, 40])
        self.assertEqual(plan['total_cost_usd'], '130.00')
        self.assertEqual(plan['remaining_gallons'], 0)

    def test_range_boundary_and_unreachable(self):
        self.assertEqual(optimize([station(500, 3)], 1000)['total_cost_usd'], '150.00')
        with self.assertRaises(InfeasibleRoute):
            optimize([station(501, 3)], 1000)
        with self.assertRaises(InfeasibleRoute):
            optimize([], 501)

    def test_empty_start_and_decimal_receipts(self):
        result = optimize([station(0, '3.12345678')], 100, 0)
        self.assertEqual(result['total_cost_usd'], '31.23')

    def test_greedy_matches_independent_linear_program(self):
        random.seed(7)
        for _ in range(75):
            miles = [0]
            for i in range(12):
                miles.append(miles[-1] + random.uniform(30, 300))
            distance = miles[-1] + 150
            prices = [random.uniform(2, 6) for _ in miles]
            initial = random.uniform(0, 50)
            rows, bounds = [], []
            # Cumulative purchases must cover fuel used by the NEXT node,
            # and must never put more than 50 gallons into the tank.
            for i, mile in enumerate(miles):
                prefix = [1 if j <= i else 0 for j in range(len(miles))]
                next_mile = miles[i + 1] if i + 1 < len(miles) else distance
                rows.extend([prefix, [-x for x in prefix]])
                bounds.extend([50 - initial + mile / 10, initial - next_mile / 10])
            oracle = linprog(prices, A_ub=rows, b_ub=bounds, bounds=(0, None), method='highs')
            self.assertTrue(oracle.success)
            actual = optimize([station(m, p, str(i)) for i, (m, p) in enumerate(zip(miles, prices))], distance, initial)
            self.assertAlmostEqual(float(actual['unrounded_total_cost_usd']), oracle.fun, places=6)
            self.assertTrue(all(s['gallons_on_departure'] <= 50.000001 for s in actual['fuel_stops']))

    def test_projection_uses_segment_distances(self):
        road = {'geometry': {'coordinates': [[-100, 40], [-99, 40]]}, 'segment_miles': [100], 'distance_miles': 100}
        fuel = [{'id': '1', 'longitude': -99.5, 'latitude': 40, 'price_per_gallon': '3'}]
        result = candidates(road, fuel)
        self.assertAlmostEqual(result[0]['route_mile'], 50)


class APITests(SimpleTestCase):
    def post(self, body):
        return self.client.post('/api/route/', json.dumps(body), content_type='application/json')

    def test_invalid_inputs(self):
        for body in [[], {}, {'start': 'Paris, FR', 'finish': 'Chicago, IL'}, {'start': 'Chicago, IL', 'finish': 'New York, NY', 'initial_fuel_gallons': True}, {'start': 'Chicago, IL', 'finish': 'New York, NY', 'corridor_miles': float('nan')}]:
            self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.client.post('/api/route/', '{', content_type='application/json').status_code, 400)
        self.assertEqual(self.client.get('/api/route/').status_code, 405)
        self.assertEqual(self.client.post('/api/route/', {}, content_type='text/plain').status_code, 415)

    @patch('planner.views.fetch_route')
    def test_success_and_map(self, fetch):
        fetch.return_value = ({'geometry': {'type': 'LineString', 'coordinates': [[-87, 41], [-86, 41]]}, 'segment_miles': [50], 'distance_miles': 50, 'duration_seconds': 3600}, True)
        response = self.post({'start': 'Chicago, IL', 'finish': 'New York, NY'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['map']['type'], 'FeatureCollection')
        self.assertEqual(response.json()['total_cost_usd'], '0.00')
        self.assertEqual(response.json()['metadata']['routing_calls'], 0)
        fetch.assert_called_once()

    @patch('planner.views.fetch_route', side_effect=RoutingError('Unavailable'))
    def test_upstream_failure(self, fetch):
        self.assertEqual(self.post({'start': 'Chicago, IL', 'finish': 'New York, NY'}).status_code, 503)

    @patch('planner.views.fetch_route')
    @patch('planner.views.stations', return_value=([], 0))
    def test_infeasible(self, inventory, fetch):
        fetch.return_value = ({'geometry': {'type': 'LineString', 'coordinates': [[-87, 41], [-86, 41]]}, 'segment_miles': [600], 'distance_miles': 600, 'duration_seconds': 3600}, False)
        self.assertEqual(self.post({'start': 'Chicago, IL', 'finish': 'New York, NY'}).status_code, 422)

    def test_dataset_available(self):
        self.assertGreater(len(stations()[0]), 5000)
        self.assertAlmostEqual(location('Chicago, IL')['latitude'], 41.8, delta=.3)

    @patch('planner.routing.requests.get')
    def test_one_provider_call_then_cache(self, get):
        cache.clear()
        get.return_value.json.return_value = {'code': 'Ok', 'routes': [{'geometry': {'type': 'LineString', 'coordinates': [[-87, 41], [-86, 41]]}, 'legs': [{'annotation': {'distance': [1000]}}], 'distance': 1000, 'duration': 60}]}
        start, finish = location('Chicago, IL'), location('New York, NY')
        self.assertFalse(fetch_route(start, finish)[1])
        self.assertTrue(fetch_route(start, finish)[1])
        get.assert_called_once()
