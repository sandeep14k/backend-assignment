"""Fuel-stop selection: which stations lie on the route, and where to buy fuel.

Vehicle (from the brief): 500-mile range at 10 MPG, i.e. a 50-gallon tank.

Model: the route is fixed; each candidate station sits ``offset`` straight-line
miles off it, reached by a detour of ``offset * ROAD_FACTOR`` road miles each
way. Visiting a station is optional and costs its detour fuel; the tank must
never be below 0 on arriving at a pump or above 50 on leaving it. A
mixed-integer program chooses which stations to visit and how much to buy at
each, minimizing the money spent plus STOP_WEIGHT per stop. Detours are
estimates; with ``verify_stops`` the chosen stations are routed by road and,
now zero-offset, get exact greedy purchases on the fixed itinerary.
"""

from decimal import ROUND_HALF_UP, Decimal
from itertools import chain

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp  # Imported at startup: ~0.15 s.
from scipy.sparse import coo_matrix

from planner.geo import to_xyz

RANGE_MILES = 500.0
MPG = 10.0
CAPACITY_GALLONS = RANGE_MILES / MPG
ROAD_FACTOR = 1.3  # Road miles per straight-line mile for a short detour (typical US circuity is 1.2-1.4).
# A station placed at its interstate exit (planner.exits) is still a short drive off the ramp.
EXIT_ACCESS_MILES = 0.5
SOLVER_SECONDS = 5.0
# Each stop costs the driver time, so it must save at least this much fuel money to be worth making.
# Without it the cheapest plan adds stops that save a few cents (e.g. 6 stops instead of 4 from Chicago to
# Houston, for $0.53). It only steers the choice of stops; reported costs are real fuel money, and the plan
# is within STOP_WEIGHT x stops of the strict fuel-cost minimum.
STOP_WEIGHT = 0.5
EPSILON = 1e-7


class InfeasibleRoute(Exception):
    pass


class OptimizationTimeout(Exception):
    pass


def candidates(route, index, corridor_miles=5.0):
    """Stations within ``corridor_miles`` of the route, each with the route mile where it is reached.

    Exact search, no nearest-vertex shortcut: for every route segment, the
    station KD-tree (built once at startup) returns all stations within
    ``corridor + half the segment length`` of its midpoint, a superset of those
    within the corridor of the segment. Each (segment, station) pair is then
    projected onto the segment in one vectorized pass, and every station keeps
    its closest segment. Sorted by route mile; candidates with different detours are retained.
    """
    coordinates = np.asarray(route["coordinates"], dtype=float)
    if len(coordinates) < 2 or not index.stations:
        return []
    xyz = to_xyz(coordinates)
    a, direction = xyz[:-1], np.diff(xyz, axis=0)
    half_length = np.linalg.norm(direction, axis=1) / 2
    hits = index.tree.query_ball_point(a + direction / 2, r=corridor_miles + half_length)
    counts = np.fromiter((len(h) for h in hits), dtype=np.int64, count=len(hits))
    if not counts.sum():
        return []
    segment = np.repeat(np.arange(len(hits)), counts)
    station = np.fromiter(chain.from_iterable(hits), dtype=np.int64, count=int(counts.sum()))

    seg_dir, relative = direction[segment], index.xyz[station] - a[segment]
    fraction = np.clip((relative * seg_dir).sum(axis=1) / np.maximum((seg_dir**2).sum(axis=1), 1e-12), 0, 1)
    offset = np.linalg.norm(relative - fraction[:, None] * seg_dir, axis=1)

    order = np.lexsort((offset, station))  # By station, then closest segment first.
    first = np.concatenate(([True], station[order][1:] != station[order][:-1]))
    best = order[first]
    best = best[offset[best] <= corridor_miles]

    segment_miles = np.asarray(route["segment_miles"])
    cumulative = np.concatenate(([0.0], np.cumsum(segment_miles)))
    miles = cumulative[segment[best]] + fraction[best] * segment_miles[segment[best]]
    found = [
        {**index.stations[station[i]], "route_mile": float(mile), "offset_miles": round(float(offset[i]), 3)}
        for i, mile in zip(best, miles, strict=True)
        if mile < route["distance_miles"] - EPSILON
    ]
    found.sort(key=lambda s: (s["route_mile"], Decimal(s["price_per_gallon"]), s["id"]))
    return found


def detour_miles(station):
    """Estimated road miles from the route to the station (one way)."""
    access = EXIT_ACCESS_MILES if station.get("coordinate_precision") == "exit_match" else 0.0
    return station.get("offset_miles", 0) * ROAD_FACTOR + access


def money(value):
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _prune_dominated(stations):
    """Keep only the cheapest of stations sharing a route mile and detour.

    Such stations (typically several in one city, which share its coordinates)
    are identical in the model except for price, so a plan using a dearer one
    can always switch to the cheapest at no extra cost. This roughly halves the
    integer program without changing its optimum.
    """
    best = {}
    for node in stations:
        key = (node["route_mile"], node.get("offset_miles", 0))
        if key not in best or Decimal(node["price_per_gallon"]) < Decimal(best[key]["price_per_gallon"]):
            best[key] = node
    return sorted(best.values(), key=lambda node: (node["route_mile"], node.get("offset_miles", 0)))


def _name(node):
    place = f" ({node['city']}, {node['state']})" if "city" in node else ""
    return f"{node.get('name', 'station ' + str(node.get('id')))}{place}"


def _reason(stop, following, remaining):
    """One factual sentence explaining a stop, from what the solved plan does next."""
    if following is None:
        if remaining < 1e-3:
            return "Last stop: bought just enough to finish the trip."
        return f"Last stop: bought {stop['gallons_purchased']:.1f} gal to finish the trip."
    if stop["gallons_on_departure"] >= CAPACITY_GALLONS - 1e-3:
        text = "Filled the tank to reach"
    elif following["gallons_on_arrival"] < 1e-3:
        text = "Bought just enough to reach"
    else:
        text = f"Bought {stop['gallons_purchased']:.1f} gal to reach"
    price, next_price = Decimal(stop["price_per_gallon"]), Decimal(following["price_per_gallon"])
    target = f"{_name(following)} at mile {following['route_mile']:.0f}"
    if next_price < price:
        return f"{text} {target}, which is cheaper at ${next_price:.3f}/gal."
    return f"{text} {target} (${next_price:.3f}/gal); the next selected station is no cheaper."


def _price_blind(stations, detours, distance, initial):
    """A driver who ignores prices: stops only when the next pump (or the end) is out of reach, then fills up,
    or buys just enough to finish. Same stations, detours and start. Returns (cost, stops) or (None, None)."""
    total, stops, position = Decimal(0), 0, 0.0
    fuel = initial  # Road fuel at ``position``.
    for i, node in enumerate(stations):
        fuel -= (node["route_mile"] - position) / MPG
        position = node["route_mile"]
        to_finish = (distance - position) / MPG
        needed = to_finish
        if i + 1 < len(stations):
            needed = min(to_finish, (stations[i + 1]["route_mile"] - position) / MPG + detours[i + 1])
        if fuel + EPSILON >= needed:
            continue
        if fuel - detours[i] < -EPSILON:
            return None, None  # The simple rule strands this driver.
        purchase = min(CAPACITY_GALLONS - (fuel - detours[i]), to_finish - fuel + 2 * detours[i])
        fuel += purchase - 2 * detours[i]
        total += Decimal(str(purchase)) * Decimal(node["price_per_gallon"])
        stops += 1
    if fuel + EPSILON < (distance - position) / MPG:
        return None, None
    return total, stops


def _solve_on_route(miles, prices, distance, initial):
    """Linear-time minimum-cost purchases when every pump is on the itinerary.

    Buy enough to reach the next strictly cheaper pump, or fill up if none is
    reachable. Never buy more than the destination needs. A monotone stack
    finds the next cheaper pump in O(n); equal prices favour buying earlier.
    """
    n = len(miles)
    cheaper, stack = np.full(n, n, dtype=int), []
    for i in range(n - 1, -1, -1):
        while stack and prices[stack[-1]] >= prices[i]:
            stack.pop()
        if stack:
            cheaper[i] = stack[-1]
        stack.append(i)
    purchases, road = np.zeros(n), np.zeros(n + 1)
    fuel, position = initial, 0.0
    for i, mile in enumerate(miles):
        fuel -= (mile - position) / MPG
        if fuel < -EPSILON:
            raise InfeasibleRoute("A routed leg exceeds the available fuel or 500-mile range.")
        road[i] = max(0.0, fuel)
        target = miles[cheaper[i]] if cheaper[i] < n else distance
        desired = min(CAPACITY_GALLONS, (target - mile) / MPG, (distance - mile) / MPG)
        purchases[i] = max(0.0, desired - fuel)
        fuel += purchases[i]
        position = mile
    fuel -= (distance - position) / MPG
    if fuel < -EPSILON:
        raise InfeasibleRoute("A routed leg exceeds the available fuel or 500-mile range.")
    road[-1] = max(0.0, fuel)
    return purchases, purchases > EPSILON, road


def _solve_milp(miles, detours, prices, distance, initial):
    """Solve the visit-and-purchase MILP. Returns (purchases, visits, road fuel at each station and the end).

    Variables per station i: purchase q, binary visit z, road fuel f (before the
    station); f[n] is the fuel left at the destination. With one-way detour fuel
    e and road gap g to the next station (or the destination):
      f[0] = initial - miles[0]/MPG
      f[i+1] = f[i] + q[i] - 2 e[i] z[i] - g[i]      (fuel balance)
      q[i] <= 50 z[i]                               (buy only where you stop)
      f[i] - e[i] z[i] >= 0                         (never empty on arrival at the pump)
      f[i] - e[i] z[i] + q[i] <= 50                 (never over 50 when leaving it)
    Objective: sum price x q (+ STOP_WEIGHT per stop as a tie-break).
    """
    n = len(miles)
    q, z, f = np.arange(n), n + np.arange(n), 2 * n + np.arange(n)
    gaps = (np.append(miles[1:], distance) - miles) / MPG
    ones, inf = np.ones(n), np.full(n, np.inf)
    families = [  # (columns, coefficients, lower, upper): n rows each
        ([f + 1, f, q, z], [ones, -ones, -ones, 2 * detours], -gaps, -gaps),
        ([q, z], [ones, np.full(n, -CAPACITY_GALLONS)], -inf, np.zeros(n)),
        ([f, z], [ones, -detours], np.zeros(n), inf),
        ([f, q, z], [ones, ones, -detours], -inf, np.full(n, CAPACITY_GALLONS)),
    ]
    rows, cols, values, lower, upper = [], [], [], [], []
    for family, (columns, coefficients, lo, hi) in enumerate(families):
        for column, coefficient in zip(columns, coefficients, strict=True):
            rows.append(family * n + np.arange(n))
            cols.append(column)
            values.append(coefficient)
        lower.append(lo)
        upper.append(hi)
    start = initial - miles[0] / MPG
    rows.append(np.array([4 * n]))
    cols.append(np.array([f[0]]))
    values.append(np.ones(1))
    lower.append(np.array([start]))
    upper.append(np.array([start]))
    matrix = coo_matrix(
        (np.concatenate(values), (np.concatenate(rows), np.concatenate(cols))), shape=(4 * n + 1, 3 * n + 1)
    )

    objective = np.concatenate([prices, np.full(n, STOP_WEIGHT), np.zeros(n + 1)])
    upper_bounds = np.concatenate([np.full(n, CAPACITY_GALLONS), ones, np.full(n + 1, CAPACITY_GALLONS)])
    integrality = np.concatenate([np.zeros(n), ones, np.zeros(n + 1)])
    result = milp(
        objective,
        integrality=integrality,
        bounds=Bounds(np.zeros(3 * n + 1), upper_bounds),
        constraints=LinearConstraint(matrix.tocsr(), np.concatenate(lower), np.concatenate(upper)),
        options={"time_limit": SOLVER_SECONDS, "mip_rel_gap": 0.0},
    )
    if result.status == 2:
        raise InfeasibleRoute(
            "No drivable fuel plan: somewhere on the route the next station is beyond the 500-mile range. "
            "Try a larger corridor_miles."
        )
    if not result.success:
        raise OptimizationTimeout("Fuel optimization did not finish in time; please retry with a smaller corridor.")
    x = result.x
    return np.maximum(x[:n], 0.0), x[n : 2 * n] > 0.5, x[2 * n :]


def optimize(stations, distance_miles, initial_gallons=None):
    """Cheapest drivable fuel plan along a fixed route.

    ``initial_gallons`` is fuel already in the tank (free). Omitted, the vehicle
    starts with just enough to reach the first candidate station, including its
    detour, and that fuel is valued at that station's price and included.
    """
    stations = _prune_dominated(stations)
    burned_on_road = distance_miles / MPG
    automatic = initial_gallons is None
    if automatic and not stations:
        raise InfeasibleRoute("No fuel station near the route to price the starting fuel. Supply initial_fuel_gallons.")
    miles = np.array([s["route_mile"] for s in stations], dtype=float)
    detours = np.array([detour_miles(s) / MPG for s in stations], dtype=float)
    prices = np.array([float(s["price_per_gallon"]) for s in stations], dtype=float)
    initial = float(miles[0] / MPG + detours[0]) if automatic else float(initial_gallons)
    if initial > CAPACITY_GALLONS + EPSILON:
        raise InfeasibleRoute("No fuel station within range of the start. Try a larger corridor_miles.")

    if not automatic and initial + EPSILON >= burned_on_road:
        purchases, visits, road = np.zeros(len(stations)), np.zeros(len(stations), bool), [initial - burned_on_road]
    else:
        if not automatic and not np.any(miles / MPG + detours <= initial + EPSILON):
            needed = float(np.min(miles / MPG + detours)) if len(stations) else None
            raise InfeasibleRoute(
                f"Starting with {initial:g} gal is not enough to reach a station"
                + (f" (the nearest needs {needed:.2f} gal)." if needed is not None else ".")
                + " Omit initial_fuel_gallons to start with just enough."
            )
        if np.any(detours):
            purchases, visits, road = _solve_milp(miles, detours, prices, distance_miles, initial)
        else:
            purchases, visits, road = _solve_on_route(miles, prices, distance_miles, initial)

    stops, total, burned = [], Decimal(0), burned_on_road
    for i in np.flatnonzero(visits):
        node, purchase = stations[i], float(purchases[i])
        arrival = max(0.0, float(road[i] - detours[i]))
        cost = Decimal(str(purchase)) * Decimal(node["price_per_gallon"])
        total += cost
        burned += 2 * float(detours[i])
        stops.append(
            {
                **node,
                "route_mile": round(node["route_mile"], 3),
                "detour_miles": round(2 * float(detours[i]) * MPG, 3),
                "gallons_on_arrival": round(arrival, 6),
                "gallons_purchased": round(purchase, 6),
                "gallons_on_departure": round(min(CAPACITY_GALLONS, arrival + purchase), 6),
                "cost_usd": money(cost),
            }
        )
    remaining = max(0.0, float(road[-1]))
    for k, stop in enumerate(stops):
        stop["reason"] = _reason(stop, stops[k + 1] if k + 1 < len(stops) else None, remaining)

    starting_cost, starting = Decimal(0), None
    if automatic and initial > EPSILON:
        starting_cost = Decimal(money(Decimal(str(initial)) * Decimal(stations[0]["price_per_gallon"])))
        starting = {
            "gallons": round(initial, 6),
            "valued_at_price_per_gallon": stations[0]["price_per_gallon"],
            "cost_usd": money(starting_cost),
            "note": f"Fuel to reach the first candidate station ({_name(stations[0])}), including its detour, "
            "valued at its price and included in the total.",
        }
    receipts = sum((Decimal(stop["cost_usd"]) for stop in stops), Decimal(0))
    total_cost = receipts + starting_cost
    purchased = sum(stop["gallons_purchased"] for stop in stops)

    blind, blind_stops = _price_blind(stations, detours, distance_miles, initial) if stations else (Decimal(0), 0)
    if blind is None:
        savings = {
            "compared_to": "No price-blind baseline: that simple rule cannot complete this trip.",
            "price_blind_cost_usd": None,
            "price_blind_stops": None,
            "savings_usd": None,
            "savings_percent": None,
        }
    else:
        blind_cost = Decimal(money(blind)) + starting_cost
        savings = {
            "compared_to": "A price-blind driver on the same route with the same detours: stops only when the "
            "next station is out of reach, then fills the tank.",
            "price_blind_cost_usd": money(blind_cost),
            "price_blind_stops": blind_stops,
            "savings_usd": money(blind_cost - total_cost),
            "savings_percent": float(round((blind_cost - total_cost) / blind_cost * 100, 1)) if blind_cost else 0.0,
        }
    return {
        "total_cost_usd": money(total_cost),
        "starting_fuel": starting,
        "initial_fuel_gallons": round(initial, 6),
        "savings": savings,
        "unrounded_total_cost_usd": str(total),
        "fuel_stops": stops,
        "fuel_burned_gallons": round(burned, 6),
        "fuel_purchased_gallons": round(purchased, 6),
        "remaining_gallons": round(remaining, 6),
        "average_price_per_gallon": money(receipts / Decimal(str(purchased))) if purchased else None,
    }
