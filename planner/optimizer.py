"""Fuel-stop selection: which stations lie on the route, and where to buy fuel.

Vehicle (from the brief): 500-mile range at 10 MPG, i.e. a 50-gallon tank.

Model: the route is fixed; each candidate station sits ``offset`` straight-line
miles off it, reached by a detour of ``offset * ROAD_FACTOR`` road miles each
way. Visiting a station is optional and costs its detour fuel; the tank must
never be below 0 on arriving at a pump or above 50 on leaving it. An exact
dynamic program (``_solve_dp``) chooses which stations to visit and how much
to buy at each, minimizing the money spent plus STOP_WEIGHT per stop, in a few
milliseconds. The test suite checks it against a mixed-integer program
(``planner/tests/milp_oracle.py``). Stations on the route itself (no detours,
as after ``verify_stops`` road routing) use an O(n) greedy instead.
"""

from decimal import ROUND_HALF_UP, Decimal
from itertools import chain

import numpy as np

from planner.geo import to_xyz

RANGE_MILES = 500.0
MPG = 10.0
CAPACITY_GALLONS = RANGE_MILES / MPG
ROAD_FACTOR = 1.3  # Road miles per straight-line mile for a short detour (typical US circuity is 1.2-1.4).
# A station placed at its interstate exit (planner.exits) is still a short drive off the ramp.
EXIT_ACCESS_MILES = 0.5
# Each stop costs the driver time, so it must save at least this much fuel money to be worth making.
# Without it the cheapest plan adds stops that save a few cents (e.g. 6 stops instead of 4 from Chicago to
# Houston, for $0.53). It only steers the choice of stops; reported costs are real fuel money, and the plan
# is within STOP_WEIGHT x stops of the strict fuel-cost minimum.
STOP_WEIGHT = 0.5
EPSILON = 1e-7


class InfeasibleRoute(Exception):
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
    problem without changing its optimum.
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


def _solve_dp(miles, detours, prices, distance, initial):
    """Exact minimum of fuel cost + STOP_WEIGHT per stop, by dynamic programming.

    In some optimal plan every stop either fills the tank or buys just enough to
    reach the next stop empty (Khuller, Malekian & Mestre, "To fill or not to
    fill", 2011): any other purchase can be shifted to a cheaper stop, and a stop
    that buys nothing would only cost its detour. So fuel on arriving at a pump is
    0, the starting fuel less the drive from the start, or 50 gallons less the
    drive from an earlier stop that filled up. The states are (stop, arrival fuel),
    and only stops within one tank of each other connect: about n x 70 x 70
    operations for a coast-to-coast route, vectorized per stop.

    Returns (purchases, visits, road fuel before each station and at the end).
    """
    n, cap = len(miles), CAPACITY_GALLONS
    to_end = (distance - miles) / MPG + detours  # Pump -> destination.
    reach = np.searchsorted(miles, miles + cap * MPG, side="right")  # Stations at most one tank ahead.
    fuel = [[] for _ in range(n)]  # Arrival fuel of each state at each pump.
    cost = [[] for _ in range(n)]
    back = [[] for _ in range(n)]  # (previous stop, its state, how it bought, this stop) or None (from the start).
    empty_cost, empty_back = np.full(n, np.inf), [None] * n  # Best way to arrive empty, merged into one state.

    from_start = initial - (miles / MPG + detours)
    for v in np.flatnonzero(from_start >= -EPSILON):
        fuel[v].append(max(0.0, from_start[v]))
        cost[v].append(0.0)
        back[v].append(None)
    best = (0.0, None) if initial + EPSILON >= distance / MPG else (np.inf, None)

    for u in range(n):
        if np.isfinite(empty_cost[u]):
            fuel[u].append(0.0)
            cost[u].append(empty_cost[u])
            back[u].append(empty_back[u])
        if not fuel[u]:
            continue
        g, c, price = np.array(fuel[u]), np.array(cost[u]), prices[u]
        fuel[u], cost[u] = g, c
        if to_end[u] <= cap + EPSILON:  # Finish from here: buy just enough.
            finish = c + price * np.maximum(0.0, to_end[u] - g) + STOP_WEIGHT
            k = int(np.argmin(finish))
            if finish[k] < best[0]:
                best = (finish[k], (u, k, "finish", None))
        targets = np.arange(u + 1, reach[u])
        need = detours[u] + (miles[targets] - miles[u]) / MPG + detours[targets]  # Pump u -> pump v.
        targets, need = targets[need <= cap + EPSILON], need[need <= cap + EPSILON]
        if not targets.size:
            continue
        fill = c + price * (cap - g) + STOP_WEIGHT  # Fill up: arrive at v with cap - need.
        k_fill = int(np.argmin(fill))
        for v, used in zip(targets.tolist(), need.tolist(), strict=True):
            fuel[v].append(max(0.0, cap - used))
            cost[v].append(fill[k_fill])
            back[v].append((u, k_fill, "fill", v))
        # Buy just enough to arrive at v empty (only from states holding no more than that).
        just = c[:, None] + price * (need[None, :] - g[:, None]) + STOP_WEIGHT
        just[g[:, None] > need[None, :] + EPSILON] = np.inf
        k_just = np.argmin(just, axis=0)
        value = just[k_just, np.arange(targets.size)]
        better = value < empty_cost[targets]
        for v, k, amount in zip(targets[better].tolist(), k_just[better].tolist(), value[better].tolist(), strict=True):
            empty_cost[v], empty_back[v] = amount, (u, k, "just", v)

    if not np.isfinite(best[0]):
        raise InfeasibleRoute(
            "No drivable fuel plan: somewhere on the route the next station is beyond the 500-mile range. "
            "Try a larger corridor_miles."
        )
    purchases, visits = np.zeros(n), np.zeros(n, dtype=bool)
    step = best[1]
    while step is not None:
        u, k, kind, v = step
        arrival = fuel[u][k]
        if kind == "finish":
            purchases[u] = max(0.0, to_end[u] - arrival)
        elif kind == "fill":
            purchases[u] = cap - arrival
        else:
            purchases[u] = max(0.0, detours[u] + (miles[v] - miles[u]) / MPG + detours[v] - arrival)
        visits[u] = True
        step = back[u][k]
    road, tank, position = np.zeros(n + 1), initial, 0.0
    for i in range(n):
        tank -= (miles[i] - position) / MPG
        road[i], position = tank, miles[i]
        if visits[i]:
            tank += purchases[i] - 2 * detours[i]
    road[n] = tank - (distance - position) / MPG
    return purchases, visits, road


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
                + (f" (the nearest needs {needed:.2f} gal)." if needed and needed >= 0.01 else "")
                + (" (the nearest needs a little fuel to reach)." if needed is not None and needed < 0.01 else "")
                + ("." if needed is None else "")
                + " Omit initial_fuel_gallons to start with just enough."
            )
        if np.any(detours):
            purchases, visits, road = _solve_dp(miles, detours, prices, distance_miles, initial)
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
