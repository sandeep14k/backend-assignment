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

    def test_every_stop_explains_itself(self):
        stations = [station(0, 3, "a"), station(300, "2.5", "b"), station(700, 4, "c")]
        reasons = [s["reason"] for s in optimize(stations, 1100)["fuel_stops"]]
        self.assertEqual(
            reasons[0], "Bought just enough to reach station b at mile 300, which is cheaper at $2.500/gal."
        )
        self.assertTrue(reasons[1].startswith("Filled the tank to reach station c at mile 700"))
        self.assertEqual(reasons[2], "Last stop: bought just enough to finish the trip.")

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
                plan = optimize(nodes, distance, initial)
            except InfeasibleRoute:
                continue
            checked += 1
            stops = {s["id"]: s for s in plan["fuel_stops"]}
            fuel = plan["initial_fuel_gallons"]
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

    def test_skips_a_cheaper_station_whose_detour_costs_more_than_it_saves(self):
        # b is 1 cent cheaper than a, but 4 miles off the road: the detour (1.04 gal) costs more than it saves.
        stations = [station(0, "3.00", "a"), {**station(100, "2.99", "b"), "offset_miles": 4.0}]
        plan = optimize(stations, 400)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a"])
        self.assertEqual(plan["total_cost_usd"], "120.00")

    def test_matches_independent_linear_program_without_detours(self):
        """With every station on the road, the plan equals a plain LP optimum (HiGHS) on 100 random trips."""
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
            self.assertAlmostEqual(float(plan["unrounded_total_cost_usd"]), oracle.fun, delta=0.001)
            self.assertTrue(all(s["gallons_on_departure"] <= 50.001 for s in plan["fuel_stops"]))


class FuelRegressionTests(SimpleTestCase):
    def test_automatic_start_without_stations_is_not_free(self):
        with self.assertRaises(InfeasibleRoute):
            optimize([], 100)

    def test_unused_station_does_not_block_empty_start(self):
        nodes = [station(0, 3, "a"), {**station(50, 4, "b"), "offset_miles": 5}]
        plan = optimize(nodes, 100, 0)
        self.assertEqual(plan["total_cost_usd"], "30.00")
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a"])

    def test_full_tank_can_finish_without_reserve(self):
        plan = optimize([{**station(100, 4), "offset_miles": 5}], 499, 50)
        self.assertEqual(plan["fuel_stops"], [])
        self.assertEqual(plan["total_cost_usd"], "0.00")
        self.assertAlmostEqual(plan["remaining_gallons"], 0.1)

    def test_starting_fuel_and_receipts_reconcile(self):
        plan = optimize([{**station(20, 3), "offset_miles": 2}], 100)
        self.assertEqual(plan["starting_fuel"]["gallons"], 2.26)
        self.assertAlmostEqual(
            plan["initial_fuel_gallons"] + plan["fuel_purchased_gallons"],
            plan["fuel_burned_gallons"] + plan["remaining_gallons"],
            places=5,
        )
        self.assertEqual(
            Decimal(plan["total_cost_usd"]),
            Decimal(plan["starting_fuel"]["cost_usd"]) + sum(Decimal(s["cost_usd"]) for s in plan["fuel_stops"]),
        )

    def test_detour_solver_matches_enumerated_station_subsets(self):
        # Independent oracle: enumerate visit subsets, then solve continuous
        # purchase quantities on each resulting physical itinerary.
        from itertools import combinations

        nodes = [
            {**station(m, p, str(i)), "offset_miles": o}
            for i, (m, p, o) in enumerate([(0, 3.2, 0), (100, 2.9, 4), (300, 3.0, 1), (470, 4, 2)])
        ]
        distance, initial = 650, 0
        best = float("inf")
        for count in range(1, len(nodes) + 1):
            for selected in combinations(nodes, count):
                traveled, previous, positions = 0.0, 0.0, []
                for node in selected:
                    entry = node["offset_miles"] * 1.3
                    traveled += node["route_mile"] - previous + entry
                    positions.append(traveled / 10)
                    traveled += entry
                    previous = node["route_mile"]
                total = (traveled + distance - previous) / 10
                if positions[0] > initial:
                    continue
                rows, limits = [], []
                for i, arrival in enumerate(positions):
                    prefix = [int(j <= i) for j in range(count)]
                    rows.extend([prefix, [-v for v in prefix]])
                    limits.extend([50 - initial + arrival, initial - (positions[i + 1] if i + 1 < count else total)])
                result = linprog(
                    [float(s["price_per_gallon"]) for s in selected],
                    A_ub=rows,
                    b_ub=limits,
                    bounds=(0, None),
                    method="highs",
                )
                if result.success:
                    best = min(best, result.fun)
        plan = optimize(nodes, distance, initial)
        self.assertAlmostEqual(float(plan["unrounded_total_cost_usd"]), best, delta=0.001)

    def test_enough_initial_fuel_never_adds_zero_purchase_detours(self):
        nodes = [{**station(1, 3, "a"), "offset_miles": 1}, {**station(5, 2, "b"), "offset_miles": 2}]
        for distance, initial in [(10, 50), (100, 50), (400, 50), (100, 10)]:
            with self.subTest(distance=distance, initial=initial):
                plan = optimize(nodes, distance, initial)
                self.assertEqual(plan["fuel_stops"], [])
                self.assertEqual(plan["fuel_burned_gallons"], distance / 10)
                self.assertAlmostEqual(plan["remaining_gallons"], initial - distance / 10)
                self.assertEqual(plan["total_cost_usd"], "0.00")

    def test_equal_cost_plans_prefer_fewer_stops(self):
        # The on-road stations have identical prices. One fill is sufficient;
        # the off-road station forces the detour-aware solver branch.
        nodes = [station(0, 3, "a"), station(100, 3, "b"), {**station(200, 4, "c"), "offset_miles": 1}]
        plan = optimize(nodes, 400, 0)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a"])
        self.assertEqual(plan["total_cost_usd"], "120.00")

    def test_duplicate_positions_keep_only_the_cheapest(self):
        # Stations in one city share coordinates; only the cheapest of them can matter.
        base = [{**station(0, 3.0, "a"), "offset_miles": 1}, {**station(300, 3.5, "b"), "offset_miles": 2}]
        twin_dear = {**station(300, 3.9, "dear"), "offset_miles": 2}
        twin_cheap = {**station(300, 3.1, "cheap"), "offset_miles": 2}
        reference = optimize(base, 600)
        with_dear = optimize([*base, twin_dear], 600)
        self.assertEqual(with_dear["total_cost_usd"], reference["total_cost_usd"])
        self.assertNotIn("dear", [s["id"] for s in with_dear["fuel_stops"]])
        with_cheap = optimize([*base, twin_cheap], 600)
        self.assertIn("cheap", [s["id"] for s in with_cheap["fuel_stops"]])
        self.assertLess(Decimal(with_cheap["total_cost_usd"]), Decimal(reference["total_cost_usd"]))

    def test_detour_plans_explain_every_stop_and_never_lose_to_the_price_blind_driver(self):
        rng = random.Random(8)
        baselines = 0
        for _ in range(60):
            miles = sorted(rng.uniform(0, 1500) for _ in range(30))
            nodes = [
                {**station(m, round(rng.uniform(2.8, 3.6), 3), str(i)), "offset_miles": rng.uniform(0, 4)}
                for i, m in enumerate(miles)
            ]
            try:
                plan = optimize(nodes, miles[-1] + 100)
            except InfeasibleRoute:
                continue
            self.assertTrue(all(s["reason"] for s in plan["fuel_stops"]))
            self.assertTrue(plan["fuel_stops"][-1]["reason"].startswith("Last stop"))
            if plan["savings"]["savings_usd"] is not None:
                baselines += 1
                self.assertGreaterEqual(Decimal(plan["savings"]["savings_usd"]), 0)
        self.assertGreater(baselines, 30)

    def test_subcent_price_differences_and_zero_purchase_waypoint(self):
        nodes = [station(0, "3.00000001", "a"), station(200, "3.00000000", "b"), station(300, "4", "c")]
        plan = optimize(nodes, 600, 0)
        self.assertEqual([s["id"] for s in plan["fuel_stops"]], ["a", "b"])
        self.assertEqual([s["gallons_purchased"] for s in plan["fuel_stops"]], [20, 40])
        self.assertAlmostEqual(float(plan["unrounded_total_cost_usd"]), 180.0000002)
        with self.assertRaises(InfeasibleRoute):
            optimize([station(500.0001, 3)], 600, 50)


class StopCostTests(SimpleTestCase):
    def plan(self, second_price):
        first = {**station(0, "3.00", "a"), "offset_miles": 0.1}
        second = {**station(100, second_price, "b"), "offset_miles": 0.1}
        return optimize([first, second], 300, initial_gallons=1)

    def test_stop_saving_cents_is_skipped(self):
        # Buying ~20 gal at the second pump would save ~20 cents: not worth a stop.
        self.assertEqual([s["id"] for s in self.plan("2.99")["fuel_stops"]], ["a"])

    def test_stop_saving_dollars_is_made(self):
        self.assertEqual([s["id"] for s in self.plan("2.50")["fuel_stops"]], ["a", "b"])

    def test_exit_placed_station_charges_ramp_access(self):
        on_exit = {**station(100, "3.00", "x"), "offset_miles": 0.0, "coordinate_precision": "exit_match"}
        plan = optimize([on_exit], 300, initial_gallons=20)
        stop = plan["fuel_stops"][0]
        self.assertEqual(stop["detour_miles"], 1.0)  # 0.5 mile each way between the exit and the pumps.
        self.assertAlmostEqual(stop["gallons_on_arrival"], 20 - 10 - 0.05, places=6)
        self.assertAlmostEqual(plan["fuel_burned_gallons"], 30.1, places=6)


class DynamicProgramTests(SimpleTestCase):
    """The production dynamic program must match an independent mixed-integer program exactly."""

    def test_matches_the_reference_mixed_integer_program(self):
        import numpy as np

        from planner.optimizer import STOP_WEIGHT, _solve_dp
        from planner.tests.milp_oracle import solve_milp

        rng, solved = np.random.default_rng(11), 0
        for _ in range(400):
            n, distance = int(rng.integers(1, 30)), float(rng.uniform(50, 2500))
            miles = np.sort(rng.uniform(0, distance * 0.999, n))
            detours = rng.choice([0.0, 0.05, 0.3, 1.5], n) * rng.uniform(0, 1, n)
            prices = np.round(rng.uniform(2.5, 4.5, n), 3)
            initial = float(rng.choice([miles[0] / 10 + detours[0], rng.uniform(0, 50)]))
            results = []
            for solve in (_solve_dp, solve_milp):
                try:
                    results.append(solve(miles, detours, prices, distance, initial))
                except InfeasibleRoute:
                    results.append(None)
            dp, reference = results
            self.assertEqual(dp is None, reference is None)  # Same feasibility.
            if dp is None:
                continue
            solved += 1
            value = [float(prices @ q + STOP_WEIGHT * z.sum()) for q, z, _ in (dp, reference)]
            self.assertAlmostEqual(value[0], value[1], delta=1e-5)  # Same optimum (MILP tolerance ~1e-6).
            purchases, visits, _ = dp
            tank, position = initial, 0.0  # Re-simulate: never empty, never over 50 gallons.
            for i in range(n):
                tank -= (miles[i] - position) / 10
                position = miles[i]
                if visits[i]:
                    tank -= detours[i]
                    self.assertGreaterEqual(tank, -1e-6)
                    tank += purchases[i]
                    self.assertLessEqual(tank, 50 + 1e-6)
                    tank -= detours[i]
                else:
                    self.assertEqual(purchases[i], 0)
                self.assertGreaterEqual(tank, -1e-6)
            self.assertGreaterEqual(tank - (distance - position) / 10, -1e-6)
        self.assertGreater(solved, 250)
