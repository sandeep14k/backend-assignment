"""Fuel-stop selection: which stations lie on the route, and where to buy fuel.

Vehicle model (from the brief): 500-mile range at 10 MPG, i.e. a 50-gallon tank.

Physical model (every plan must be drivable):

* Stops are off the road: reaching a station ``o`` straight-line miles away costs
  about ``o * ROAD_FACTOR`` road miles each way. That detour fuel is bought and
  paid for at the stop.
* The tank keeps a reserve ``R`` = the largest one-way detour of any candidate.
  Purchases are planned on the road route with a usable tank of ``50 - 2R``
  gallons; the real tank then holds ``planned + R`` on the road, ``planned + R - e``
  on arriving at a station with one-way detour ``e`` (never below 0) and
  ``planned + R + e`` when leaving it (never above 50).
* By default the vehicle starts with just enough fuel to reach the first station
  on the route, valued at that station's price, so ``total_cost_usd`` is the
  cost of all the fuel the trip burns. ``initial_gallons`` overrides this.
"""

from decimal import ROUND_HALF_UP, Decimal
from itertools import chain

import numpy as np

from planner.geo import to_xyz

RANGE_MILES = 500.0
MPG = 10.0
CAPACITY_GALLONS = RANGE_MILES / MPG
MIN_STOP_GALLONS = 5.0
ROAD_FACTOR = 1.3  # Road miles per straight-line mile for a short detour (typical US circuity is 1.2-1.4).
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
    its closest segment. Sorted by route mile; co-located stations keep the cheapest.
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
    unique = []
    for item in found:
        if not unique or item["route_mile"] - unique[-1]["route_mile"] > EPSILON:
            unique.append(item)
    return unique


def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _next_cheaper(prices):
    """Index of the next strictly cheaper entry for each position (monotonic stack, O(n))."""
    result, stack = [None] * len(prices), []
    for i in range(len(prices) - 1, -1, -1):
        while stack and prices[stack[-1]] >= prices[i]:
            stack.pop()
        result[i] = stack[-1] if stack else None
        stack.append(i)
    return result


def _check_reachable(gap, fuel, position):
    if gap / MPG > fuel + EPSILON:
        raise InfeasibleRoute(
            f"No fuel station within range after route mile {position:.0f} "
            f"(next one is {gap:.0f} miles away). Try a larger corridor_miles."
        )


def _cheapest_purchases(nodes, prices, initial, capacity):
    """Planned gallons bought at each node under the greedy rule in ``optimize``, and why.

    ``decisions[i]`` is ``("cheaper", j)`` (just enough to reach cheaper node j),
    ``("finish", None)`` or ``("fill", None)``.
    """
    next_cheaper = _next_cheaper(prices)
    reach = capacity * MPG
    purchases, decisions = [0.0] * len(nodes), [None] * len(nodes)
    fuel, position = initial, 0.0
    for i, node in enumerate(nodes):
        gap = node["route_mile"] - position
        _check_reachable(gap, fuel, position)
        fuel = max(0.0, fuel - gap / MPG)
        position = node["route_mile"]
        if i == len(nodes) - 1:
            break
        cheaper = next_cheaper[i]  # Always exists: the destination is a free "station".
        if nodes[cheaper]["route_mile"] - position <= reach:
            target = nodes[cheaper]["route_mile"]
            decision = ("finish", None) if cheaper == len(nodes) - 1 else ("cheaper", cheaper)
        else:
            target, decision = position + reach, ("fill", None)
        purchase = min(capacity, (target - position) / MPG) - fuel
        if purchase > EPSILON:
            purchases[i], decisions[i] = purchase, decision
            fuel += purchase
    return purchases, decisions


def _price_blind_purchases(nodes, initial, capacity):
    """Baseline: stop only when empty or the next station is out of reach, then fill up (or buy enough to finish)."""
    purchases, fuel, position = [0.0] * len(nodes), initial, 0.0
    distance = nodes[-1]["route_mile"]
    for i, node in enumerate(nodes[:-1]):
        fuel -= (node["route_mile"] - position) / MPG
        position = node["route_mile"]
        if fuel <= EPSILON or (nodes[i + 1]["route_mile"] - position) / MPG > fuel + EPSILON:
            purchases[i] = min(capacity, (distance - position) / MPG) - fuel
            fuel += purchases[i]
    return purchases


def _arrival_fuel(nodes, purchases, initial):
    """Planned gallons on arrival at each node, before buying."""
    arrivals, fuel, position = [], initial, 0.0
    for node, purchase in zip(nodes, purchases, strict=True):
        fuel -= (node["route_mile"] - position) / MPG
        arrivals.append(fuel)
        fuel += purchase
        position = node["route_mile"]
    return arrivals


def _consolidate_stops(nodes, purchases, prices, detours, initial, capacity, min_gallons):
    """Remove stops by moving their whole purchase to a neighbouring stop, when feasible.

    A stop's purchase moves to the previous stop (if the tank has room there:
    levels between the stops rise, peaking at that departure) or to the next
    stop (if the tank reaches it without that fuel: levels fall, lowest on
    arrival there). A move is made when it lowers the total cost, because the
    skipped stop's detour fuel is no longer needed, or, for convenience, when
    the purchase is below ``min_gallons``. Repeats until nothing changes.
    Mutates ``purchases``; returns the moves as ``(from_index, to_index, gallons)``.
    """
    moves, changed = [], True
    while changed:
        changed = False
        for i in range(len(nodes) - 1):
            gallons = purchases[i]
            if gallons <= EPSILON:
                continue
            arrivals = _arrival_fuel(nodes, purchases, initial)
            stops = [j for j in range(len(nodes) - 1) if purchases[j] > EPSILON and j != i]
            previous = max((j for j in stops if j < i), default=None)
            following = min((j for j in stops if j > i), default=None)
            options = []
            if previous is not None and arrivals[previous] + purchases[previous] + gallons <= capacity + EPSILON:
                options.append(previous)
            if following is not None and arrivals[following] - gallons >= -EPSILON:
                options.append(following)
            if not options:
                continue
            # Cost change: the fuel at the new price, minus the skipped stop's detour fuel.
            delta, target = min(
                (
                    (Decimal(str(gallons)) * (prices[j] - prices[i]) - Decimal(str(detours[i])) * prices[i], j)
                    for j in options
                ),
                key=lambda option: option[0],
            )
            if delta < 0 or gallons < min_gallons:
                purchases[target] += gallons
                purchases[i] = 0.0
                moves.append((i, target, gallons))
                changed = True
    return moves


def _reason(index, nodes, decision, moves):
    """One sentence explaining a stop, e.g. 'Bought just enough to reach X at mile 578, cheaper at $2.899.'"""
    kind, target = decision
    if kind == "cheaper":
        node = nodes[target]
        place = f" ({node['city']}, {node['state']})" if "city" in node else ""
        text = (
            f"Bought just enough to reach {node.get('name', 'station ' + str(node.get('id')))}{place} at mile "
            f"{node['route_mile']:.0f}, which is cheaper at ${Decimal(node['price_per_gallon']):.3f}/gal."
        )
    elif kind == "finish":
        text = "No cheaper station before the destination, so bought just enough to finish."
    else:
        text = "No cheaper station within range, so filled the tank."
    replaced = [
        (nodes[source]["route_mile"], gallons)
        for source, target, gallons in moves
        if target == index and gallons >= 0.05  # Fractions of a gallon are not worth a sentence.
    ]
    if replaced:
        miles = " and ".join(f"{mile:.0f}" for mile, _ in sorted(replaced))
        where = f"stop at mile {miles}" if len(replaced) == 1 else f"stops at miles {miles}"
        text += f" Also buys {sum(g for _, g in replaced):.1f} gal here so the {where} can be skipped."
    return text


def _realize(nodes, prices, purchases, detours, initial, reserve, decisions=None, moves=()):
    """Turn planned road purchases into real stops: add each stop's detour fuel and real tank levels."""
    stops, total, fuel, position = [], Decimal(0), initial, 0.0
    for index, (node, purchase) in enumerate(zip(nodes[:-1], purchases[:-1], strict=True)):
        fuel -= (node["route_mile"] - position) / MPG
        position = node["route_mile"]
        if purchase <= EPSILON:
            continue
        detour = detours[index]
        bought = purchase + detour
        cost = Decimal(str(bought)) * prices[index]
        total += cost
        stop = {
            **node,
            "route_mile": round(node["route_mile"], 3),
            "detour_miles": round(detour * MPG, 2),
            "gallons_on_arrival": round(fuel + reserve - detour / 2, 3),
            "gallons_purchased": round(bought, 3),
            "gallons_on_departure": round(fuel + purchase + reserve + detour / 2, 3),
            "cost_usd": money(cost),
        }
        if decisions is not None:
            stop["reason"] = _reason(index, nodes, decisions[index], moves)
        stops.append(stop)
        fuel += purchase
    end_fuel = fuel - (nodes[-1]["route_mile"] - position) / MPG + reserve
    return stops, total, end_fuel


def _receipts(stops):
    return sum((Decimal(stop["cost_usd"]) for stop in stops), Decimal(0))


def optimize(stations, distance_miles, initial_gallons=None, min_stop_gallons=0.0):
    """Minimum-cost fuel purchases along a fixed route, made physically drivable.

    Rule at each station (on the road model): if a cheaper station is within
    range, buy just enough to reach it; otherwise fill the tank (or buy just
    enough to finish). Why it is optimal: fuel bought here before a reachable
    cheaper station could have been bought there for less; when nothing cheaper
    is reachable, every gallon needed before the next refill is cheapest here.
    The destination is modelled as a free "station" so we never overbuy. This
    is exact for road fuel (tested against an LP solver).

    Each chosen stop then adds its detour fuel (see the module docstring), so
    every extra stop has a cost. ``_consolidate_stops`` removes stops whose
    purchase can move to a neighbour more cheaply, and optionally (for
    convenience) any under ``min_stop_gallons``. The final cost is therefore
    never above the road-optimal plan's unless the convenience rule asks for it;
    both are reported. Each stop has a ``reason``; ``savings`` compares with a
    price-blind driver.
    """
    stations = list(stations)
    nodes = [*stations, {"route_mile": distance_miles, "price_per_gallon": "0"}]
    prices = [Decimal(node["price_per_gallon"]) for node in nodes]
    detours = [2 * node.get("offset_miles", 0.0) * ROAD_FACTOR / MPG for node in stations] + [0.0]
    reserve = max(detours, default=0.0) / 2
    capacity = CAPACITY_GALLONS - 2 * reserve

    starting = None
    if initial_gallons is None:  # Just enough to reach the first station; its cost is counted.
        first_mile = stations[0]["route_mile"] if stations else distance_miles
        planned_initial = first_mile / MPG
        if stations:
            starting = {"gallons": planned_initial, "price": prices[0], "station": stations[0].get("name")}
    else:
        planned_initial = float(initial_gallons) - reserve
        if planned_initial < -EPSILON:
            raise InfeasibleRoute(
                f"Starting with {initial_gallons:g} gal is not enough to reach a station; at least "
                f"{reserve:.2f} gal is needed for the detour. Omit initial_fuel_gallons to start with just enough."
            )
        planned_initial = max(planned_initial, 0.0)
    if initial_gallons is None and planned_initial > capacity + EPSILON:
        raise InfeasibleRoute("No fuel station within range of the start. Try a larger corridor_miles.")

    purchases, decisions = _cheapest_purchases(nodes, prices, planned_initial, capacity)
    road_optimal_stops, _, _ = _realize(nodes, prices, purchases, detours, planned_initial, reserve)
    moves = _consolidate_stops(nodes, purchases, prices, detours, planned_initial, capacity, min_stop_gallons)
    stops, total, end_fuel = _realize(nodes, prices, purchases, detours, planned_initial, reserve, decisions, moves)
    blind_stops, _, _ = _realize(
        nodes, prices, _price_blind_purchases(nodes, planned_initial, capacity), detours, planned_initial, reserve
    )

    starting_cost = Decimal(0)
    starting_fuel = None
    if starting and starting["gallons"] > EPSILON:
        starting_cost = Decimal(money(Decimal(str(starting["gallons"])) * starting["price"]))
        starting_fuel = {
            "gallons": round(starting["gallons"], 3),
            "valued_at_price_per_gallon": str(starting["price"]),
            "cost_usd": money(starting_cost),
            "note": f"Fuel to reach the first station ({starting['station']}), priced there and included in the total.",
        }
    receipts = _receipts(stops)
    total_cost = receipts + starting_cost
    blind_cost = _receipts(blind_stops) + starting_cost
    road_optimal_cost = _receipts(road_optimal_stops) + starting_cost
    purchased = sum(stop["gallons_purchased"] for stop in stops)
    detour_gallons = sum(stop["detour_miles"] for stop in stops) / MPG
    return {
        "total_cost_usd": money(total_cost),
        "starting_fuel": starting_fuel,
        "savings": {
            "compared_to": "A price-blind driver on the same route: stops only when the next station is out "
            "of reach, then fills the tank.",
            "price_blind_cost_usd": money(blind_cost),
            "price_blind_stops": len(blind_stops),
            "savings_usd": money(blind_cost - total_cost),
            "savings_percent": float(round((blind_cost - total_cost) / blind_cost * 100, 1)) if blind_cost else 0.0,
        },
        "stop_consolidation": {
            "road_optimal_plan_cost_usd": money(road_optimal_cost),
            "road_optimal_plan_stops": len(road_optimal_stops),
            "stops_removed": len(moves),
            "min_stop_gallons": min_stop_gallons,
            "cost_change_usd": money(total_cost - road_optimal_cost),
            "note": "The road-optimal plan is the exact minimum for road fuel (LP-verified). Removing stops saves "
            "their detour fuel; tiny top-ups under min_stop_gallons are also folded in for convenience.",
        },
        "unrounded_total_cost_usd": str(total.quantize(Decimal("0.000001"))),
        "fuel_stops": stops,
        "fuel_burned_gallons": round(distance_miles / MPG + detour_gallons, 3),
        "fuel_purchased_gallons": round(purchased, 3),
        "average_price_per_gallon": money(receipts / Decimal(str(purchased))) if purchased else None,
        "remaining_gallons": round(end_fuel, 3),
    }
