import random

from django.test import SimpleTestCase
from scipy.optimize import linprog

from planner.optimizer import InfeasibleRoute, optimize
from planner.tests.helpers import station


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
        self.assertEqual(optimize([station(500, 3)], 1000, initial_gallons=50)["total_cost_usd"], "150.00")
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
        stations = [station(0, 3, "a"), station(400, "2.99", "b"), station(420, "2.98", "c")]
        exact = optimize(stations, 900)
        self.assertEqual([s["id"] for s in exact["fuel_stops"]], ["a", "b", "c"])
        merged = optimize(stations, 900, min_stop_gallons=5)
        self.assertEqual([s["id"] for s in merged["fuel_stops"]], ["a", "c"])  # b's 2 gal bought at a
        self.assertEqual(merged["small_stops_merged"], 1)
        self.assertEqual(merged["fuel_purchased_gallons"], 90)
        self.assertEqual(merged["merge_extra_cost_usd"], "0.02")  # 2 gal x $0.01

    def test_small_top_up_moved_to_next_stop_when_tank_is_full(self):
        # a fills to 50 at the cheapest price; b's 1-gallon top-up moves to c, reachable without it.
        stations = [station(0, "2.9", "a"), station(10, "3.0", "b"), station(400, "3.1", "c")]
        plan = optimize(stations, 600, min_stop_gallons=5)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a", "c"])

    def test_merge_never_overfills_or_strands(self):
        plan = optimize([station(0, "2.9", "a"), station(10, 3, "b")], 510, min_stop_gallons=5)
        self.assertEqual([s["gallons_purchased"] for s in plan["fuel_stops"]], [50, 1])  # nowhere to move it
        self.assertEqual(plan["small_stops_merged"], 0)

    def test_merged_plans_stay_feasible(self):
        rng = random.Random(11)
        for _ in range(200):
            miles = sorted(rng.uniform(0, 1500) for _ in range(25))
            miles[0] = 0.0
            nodes = [station(m, round(rng.uniform(2.8, 3.4), 3), str(i)) for i, m in enumerate(miles)]
            distance = miles[-1] + 200
            try:
                plan = optimize(nodes, distance, min_stop_gallons=5)
            except InfeasibleRoute:
                continue
            fuel, position = 0.0, 0.0
            for stop in plan["fuel_stops"]:
                fuel -= (stop["route_mile"] - position) / 10
                self.assertGreaterEqual(fuel, -0.02)  # receipts are rounded to 0.001 gal
                fuel += stop["gallons_purchased"]
                self.assertLessEqual(fuel, 50.02)
                position = stop["route_mile"]
            self.assertGreaterEqual(fuel - (distance - position) / 10, -0.02)

    def test_greedy_matches_independent_linear_program(self):
        """The O(n) greedy equals the optimum found by a general LP solver (HiGHS) on 100 random trips."""
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
            trip = [station(m, p, str(i)) for i, (m, p) in enumerate(zip(miles, prices, strict=True))]
            plan = optimize(trip, distance, initial)
            self.assertAlmostEqual(float(plan["unrounded_total_cost_usd"]), oracle.fun, places=4)
            self.assertTrue(all(s["gallons_on_departure"] <= 50.001 for s in plan["fuel_stops"]))
