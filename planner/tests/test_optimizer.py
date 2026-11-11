import random
from decimal import Decimal

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
            optimize([station(10, 3)], 100, initial_gallons=0)  # Truly empty: cannot drive 10 miles.

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
        self.assertEqual(merged["stop_consolidation"]["stops_removed"], 1)
        self.assertEqual(merged["fuel_purchased_gallons"], 90)
        self.assertEqual(merged["stop_consolidation"]["cost_change_usd"], "0.02")  # 2 gal x $0.01
        self.assertEqual(merged["stop_consolidation"]["road_optimal_plan_cost_usd"], exact["total_cost_usd"])

    def test_small_top_up_moved_to_next_stop_when_tank_is_full(self):
        # a fills to 50 at the cheapest price; b's 1-gallon top-up moves to c, reachable without it.
        stations = [station(0, "2.9", "a"), station(10, "3.0", "b"), station(400, "3.1", "c")]
        plan = optimize(stations, 600, min_stop_gallons=5)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a", "c"])

    def test_merge_never_overfills_or_strands(self):
        plan = optimize([station(0, "2.9", "a"), station(10, 3, "b")], 510, min_stop_gallons=5)
        self.assertEqual([s["gallons_purchased"] for s in plan["fuel_stops"]], [50, 1])  # nowhere to move it
        self.assertEqual(plan["stop_consolidation"]["stops_removed"], 0)

    def test_every_stop_explains_itself(self):
        stations = [station(0, 3, "a"), station(300, "2.5", "b"), station(700, 4, "c")]
        reasons = [s["reason"] for s in optimize(stations, 1100)["fuel_stops"]]
        self.assertTrue(reasons[0].startswith("Bought just enough to reach station b"))
        self.assertIn("filled the tank", reasons[1])  # Nothing cheaper than $2.50 within 500 miles.
        self.assertIn("just enough to finish", reasons[2])
        merged = optimize(
            [station(0, 3, "a"), station(400, "2.99", "b"), station(420, "2.98", "c")], 900, min_stop_gallons=5
        )
        self.assertIn("so the stop at mile 400 can be skipped", merged["fuel_stops"][0]["reason"])

    def test_savings_against_price_blind_driver(self):
        # Price-blind: stops only when forced (miles 0 and 450), paying $4 at 450. The plan buys more at $2.
        plan = optimize([station(0, 2, "a"), station(300, 3, "b"), station(450, 4, "c")], 900)
        self.assertEqual(plan["savings"]["price_blind_cost_usd"], "260.00")  # 50 x $2 + 40 x $4
        self.assertEqual(plan["total_cost_usd"], "230.00")  # 50 x $2 + 30 x $3 (fill at b) + 10 x $4 (finish)
        self.assertEqual(plan["savings"]["savings_usd"], "30.00")
        self.assertEqual(plan["savings"]["price_blind_stops"], 2)

    def test_identical_choices_save_nothing(self):
        # Two stations at the start: an empty tank must fill at the first one, as the plan does.
        plan = optimize([station(0, 3, "a"), station(0.0, 4, "b")], 80)
        self.assertEqual(plan["savings"]["savings_usd"], "0.00")

    def test_default_start_carries_just_enough_fuel_and_counts_it(self):
        plan = optimize([station(20, 3, "a"), station(300, 2, "b")], 600)
        self.assertEqual(plan["starting_fuel"]["gallons"], 2.0)  # 20 miles to the first station
        self.assertEqual(plan["starting_fuel"]["cost_usd"], "6.00")  # valued at that station's $3
        purchases = sum(Decimal(s["cost_usd"]) for s in plan["fuel_stops"])
        self.assertEqual(Decimal(plan["total_cost_usd"]), purchases + Decimal("6.00"))
        self.assertEqual(plan["fuel_burned_gallons"], 60)

    def test_detour_fuel_is_bought_and_paid_for(self):
        far = {**station(0, 3, "a"), "offset_miles": 2.0}  # 2 x 2 x 1.3 = 5.2 detour miles = 0.52 gal
        plan = optimize([far], 100, initial_gallons=5)
        stop = plan["fuel_stops"][0]
        self.assertEqual(stop["detour_miles"], 5.2)
        self.assertAlmostEqual(plan["fuel_burned_gallons"], 10.52)
        self.assertGreaterEqual(stop["gallons_on_arrival"], 0)
        self.assertLessEqual(stop["gallons_on_departure"], 50)

    def test_empty_tank_needs_a_station_on_the_spot(self):
        with self.assertRaises(InfeasibleRoute):
            optimize([{**station(0, 3), "offset_miles": 1.0}], 100, initial_gallons=0)
        self.assertEqual(optimize([station(0, 3)], 100, initial_gallons=0)["total_cost_usd"], "30.00")

    def test_real_tank_never_below_zero_or_above_capacity_with_detours(self):
        """Simulate the real vehicle, detours included, on 300 random trips."""
        rng = random.Random(21)
        checked = 0
        for _ in range(300):
            miles = sorted(rng.uniform(0, 2000) for _ in range(40))
            if rng.random() < 0.3:
                miles[0] = 0.0  # Also cover a station right at the start.
            nodes = [
                {**station(m, round(rng.uniform(2.8, 3.6), 3), str(i)), "offset_miles": rng.uniform(0, 5)}
                for i, m in enumerate(miles)
            ]
            distance = miles[-1] + rng.uniform(1, 300)
            initial = rng.choice([None, rng.uniform(1, 50)])
            try:
                plan = optimize(nodes, distance, initial, min_stop_gallons=rng.choice([0, 5]))
            except InfeasibleRoute:
                continue
            checked += 1
            stops = {s["id"]: s for s in plan["fuel_stops"]}
            reserve = max(n["offset_miles"] for n in nodes) * 1.3 / 10
            if initial is not None:
                fuel = initial
            else:  # Starts with the reserve plus just enough to reach the first station (none if it is at mile 0).
                fuel = reserve + (plan["starting_fuel"]["gallons"] if plan["starting_fuel"] else 0.0)
            position, burned = 0.0, 0.0
            for node in nodes:
                fuel -= (node["route_mile"] - position) / 10
                burned += (node["route_mile"] - position) / 10
                position = node["route_mile"]
                self.assertGreaterEqual(fuel, -0.01)
                if node["id"] in stops:
                    one_way = node["offset_miles"] * 1.3 / 10
                    fuel -= one_way
                    self.assertGreaterEqual(fuel, -0.01, "ran dry entering a station")
                    fuel += stops[node["id"]]["gallons_purchased"]
                    self.assertLessEqual(fuel, 50.01, "overfilled")
                    fuel -= one_way
                    burned += 2 * one_way
            fuel -= (distance - position) / 10
            burned += (distance - position) / 10
            self.assertGreaterEqual(fuel, -0.01, "ran dry before the destination")
            self.assertAlmostEqual(burned, plan["fuel_burned_gallons"], delta=0.05)
        self.assertGreater(checked, 100)

    def test_consolidation_removes_a_stop_when_its_detour_costs_more_than_it_saves(self):
        # b is 1 cent cheaper than a, but 4 miles off the road: the detour (1.04 gal) costs more than it saves.
        stations = [station(0, "3.00", "a"), {**station(100, "2.99", "b"), "offset_miles": 4.0}]
        plan = optimize(stations, 400, initial_gallons=None, min_stop_gallons=0)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a"])
        consolidation = plan["stop_consolidation"]
        self.assertEqual((consolidation["road_optimal_plan_stops"], consolidation["stops_removed"]), (2, 1))
        self.assertLess(Decimal(consolidation["cost_change_usd"]), 0)

    def test_consolidation_never_costs_more_without_the_convenience_rule(self):
        rng = random.Random(5)
        for _ in range(150):
            miles = sorted(rng.uniform(0, 1500) for _ in range(30))
            nodes = [
                {**station(m, round(rng.uniform(2.8, 3.4), 3), str(i)), "offset_miles": rng.uniform(0, 5)}
                for i, m in enumerate(miles)
            ]
            try:
                plan = optimize(nodes, miles[-1] + 100, min_stop_gallons=0)
            except InfeasibleRoute:
                continue
            self.assertLessEqual(Decimal(plan["stop_consolidation"]["cost_change_usd"]), 0)

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
