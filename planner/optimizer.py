"""Fuel-stop selection: which stations lie on the route, and where to buy fuel.

Vehicle model (from the brief): 500-mile range at 10 MPG, i.e. a 50-gallon tank.
"""

from decimal import ROUND_HALF_UP, Decimal
from itertools import chain

import numpy as np

from planner.geo import to_xyz

RANGE_MILES = 500.0
MPG = 10.0
CAPACITY_GALLONS = RANGE_MILES / MPG
ORIGIN_RADIUS_MILES = 25.0
ORIGIN_FALLBACK_MILES = 100.0
MIN_STOP_GALLONS = 5.0
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


def origin_station(start, index):
    """The station where the trip's first fill happens (route mile 0).

    The cheapest station within 25 miles of the start; failing that, the nearest
    station within 100 miles. ``offset_miles`` is the straight-line distance.
    """
    if not index.stations:
        return None
    point = to_xyz([[start["longitude"], start["latitude"]]])[0]
    close = index.tree.query_ball_point(point, ORIGIN_RADIUS_MILES)
    if close:
        distances = np.linalg.norm(index.xyz[close] - point, axis=1)
        choice = min(range(len(close)), key=lambda i: (index.prices[close[i]], distances[i]))
        station, distance = close[choice], distances[choice]
    else:
        distance, station = index.tree.query(point, distance_upper_bound=ORIGIN_FALLBACK_MILES)
        if not np.isfinite(distance):
            return None
    return {**index.stations[int(station)], "route_mile": 0.0, "offset_miles": round(float(distance), 3)}


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


def _cheapest_purchases(nodes, prices, distance_miles, initial_gallons):
    """Gallons bought at each node under the greedy rule in ``optimize``, and why.

    Returns ``(purchases, decisions)``; ``decisions[i]`` is ``("cheaper", j)``
    (buy just enough to reach cheaper node j), ``("finish", None)`` or ``("fill", None)``.
    """
    next_cheaper = _next_cheaper(prices)
    purchases, decisions = [0.0] * len(nodes), [None] * len(nodes)
    fuel, position = float(initial_gallons), 0.0
    for i, node in enumerate(nodes):
        gap = node["route_mile"] - position
        if gap / MPG > fuel + EPSILON:
            raise InfeasibleRoute(
                f"No fuel station within range after route mile {position:.0f} "
                f"(next one is {gap:.0f} miles away). Try a larger corridor_miles."
            )
        fuel = max(0.0, fuel - gap / MPG)
        position = node["route_mile"]
        if i == len(nodes) - 1:
            break
        cheaper = next_cheaper[i]  # Always exists: the destination is a free "station".
        if nodes[cheaper]["route_mile"] - position <= RANGE_MILES:
            target_mile, decision = (
                nodes[cheaper]["route_mile"],
                ("finish", None) if cheaper == len(nodes) - 1 else ("cheaper", cheaper),
            )
        else:
            target_mile, decision = position + RANGE_MILES, ("fill", None)
        purchase = min(CAPACITY_GALLONS, (min(target_mile, distance_miles) - position) / MPG) - fuel
        if purchase > EPSILON:
            purchases[i], decisions[i] = purchase, decision
            fuel += purchase
    return purchases, decisions


def _cost(purchases, prices):
    return sum(
        (Decimal(str(gallons)) * price for gallons, price in zip(purchases, prices, strict=True) if gallons > EPSILON),
        Decimal(0),
    )


def _arrival_fuel(nodes, purchases, initial_gallons):
    """Gallons in the tank on arrival at each node, before buying."""
    arrivals, fuel, position = [], float(initial_gallons), 0.0
    for node, purchase in zip(nodes, purchases, strict=True):
        fuel -= (node["route_mile"] - position) / MPG
        arrivals.append(fuel)
        fuel += purchase
        position = node["route_mile"]
    return arrivals


def _merge_small_purchases(nodes, purchases, prices, initial_gallons, min_gallons):
    """Move purchases below ``min_gallons`` to a neighbouring stop, so the driver skips a stop.

    Two safe moves, whichever is cheaper:
      * buy it at the previous stop instead, if the tank has room there
        (tank levels between the stops rise, peaking at that departure);
      * buy it at the next stop instead, if the tank can reach it without the fuel
        (levels between the stops fall, lowest on arrival at the next stop).
    Mutates ``purchases``; returns the moves made as ``(from_index, to_index, gallons)``.
    """
    moves = []
    for i in range(len(nodes) - 1):
        gallons = purchases[i]
        if not EPSILON < gallons < min_gallons:
            continue
        arrivals = _arrival_fuel(nodes, purchases, initial_gallons)
        stops = [j for j in range(len(nodes) - 1) if purchases[j] > EPSILON and j != i]
        previous = max((j for j in stops if j < i), default=None)
        following = min((j for j in stops if j > i), default=None)
        options = []
        if previous is not None and arrivals[previous] + purchases[previous] + gallons <= CAPACITY_GALLONS + EPSILON:
            options.append((prices[previous], previous))
        if following is not None and arrivals[following] - gallons >= -EPSILON:
            options.append((prices[following], following))
        if options:
            _, target = min(options)
            purchases[target] += gallons
            purchases[i] = 0.0
            moves.append((i, target, gallons))
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
        text = f"No cheaper station within the next {RANGE_MILES:.0f} miles, so filled the tank."
    if index == 0 and nodes[0]["route_mile"] == 0:
        text = f"First fill near the start. {text}"
    for source, _, gallons in (move for move in moves if move[1] == index):
        mile = nodes[source]["route_mile"]
        text += f" Includes {gallons:.1f} gal that would otherwise need a separate stop at mile {mile:.0f}."
    return text


def _price_blind_plan(nodes, prices, initial_gallons):
    """Baseline for comparison: a driver who ignores prices.

    Same route, same stations, same start. They stop only when the next station
    (or the destination) is out of reach, and then fill the tank, or buy just
    enough to finish if the destination is within range.
    """
    cost, stops, fuel, position = Decimal(0), 0, float(initial_gallons), 0.0
    distance = nodes[-1]["route_mile"]
    for i, node in enumerate(nodes[:-1]):
        fuel -= (node["route_mile"] - position) / MPG
        position = node["route_mile"]
        gap = nodes[i + 1]["route_mile"] - position
        if fuel <= EPSILON or gap / MPG > fuel + EPSILON:  # Empty, or cannot reach the next station.
            purchase = min(CAPACITY_GALLONS, (distance - position) / MPG) - fuel
            cost += Decimal(money(Decimal(str(purchase)) * prices[i]))
            stops += 1
            fuel += purchase
    return cost, stops


def optimize(stations, distance_miles, initial_gallons=0.0, min_stop_gallons=0.0):
    """Minimum-cost fuel purchases along a fixed route (exact, O(n)).

    Rule at each station: if a cheaper station is within range, buy just enough
    to reach it; otherwise fill the tank (or buy just enough to finish).
    Why it is optimal: fuel bought here before a reachable cheaper station could
    have been bought there for less; when nothing cheaper is reachable, every
    gallon needed before the next refill is cheapest right here.
    The destination is modelled as a free "station" so we never overbuy.

    ``min_stop_gallons`` then folds tiny top-ups into a neighbouring stop (see
    ``_merge_small_purchases``); the extra cost is reported, typically cents.
    Each stop carries a one-sentence ``reason``, and ``savings`` compares the
    plan with a price-blind driver on the same route.
    """
    nodes = [*stations, {"route_mile": distance_miles, "price_per_gallon": "0"}]
    prices = [Decimal(node["price_per_gallon"]) for node in nodes]
    purchases, decisions = _cheapest_purchases(nodes, prices, distance_miles, initial_gallons)
    optimal_cost = _cost(purchases, prices)
    moves = (
        _merge_small_purchases(nodes, purchases, prices, initial_gallons, min_stop_gallons) if min_stop_gallons else []
    )

    stops, fuel, total = [], float(initial_gallons), Decimal(0)
    for index, (node, previous, purchase, price) in enumerate(
        zip(nodes, [None, *nodes[:-1]], purchases, prices, strict=True)
    ):
        fuel -= (node["route_mile"] - (previous["route_mile"] if previous else 0.0)) / MPG
        fuel = max(fuel, 0.0)
        if purchase <= EPSILON:
            continue
        cost = Decimal(str(purchase)) * price
        total += cost
        stops.append(
            {
                **node,
                "route_mile": round(node["route_mile"], 3),
                "gallons_on_arrival": round(fuel, 3),
                "gallons_purchased": round(purchase, 3),
                "gallons_on_departure": round(fuel + purchase, 3),
                "cost_usd": money(cost),
                "reason": _reason(index, nodes, decisions[index], moves),
            }
        )
        fuel += purchase

    # Total = sum of the rounded receipts, so the numbers shown always add up.
    receipts = sum((Decimal(stop["cost_usd"]) for stop in stops), Decimal(0))
    purchased = sum(stop["gallons_purchased"] for stop in stops)
    blind_cost, blind_stops = _price_blind_plan(nodes, prices, initial_gallons)
    return {
        "total_cost_usd": money(receipts),
        "savings": {
            "compared_to": "A price-blind driver on the same route: stops only when the next station is out "
            "of reach, then fills the tank.",
            "price_blind_cost_usd": money(blind_cost),
            "price_blind_stops": blind_stops,
            "savings_usd": money(blind_cost - receipts),
            "savings_percent": float(round((blind_cost - receipts) / blind_cost * 100, 1)) if blind_cost else 0.0,
        },
        "unrounded_total_cost_usd": str(total.quantize(Decimal("0.000001"))),
        "small_stops_merged": len(moves),
        "merge_extra_cost_usd": money(total - optimal_cost),
        "fuel_stops": stops,
        "fuel_consumed_gallons": round(distance_miles / MPG, 3),
        "fuel_purchased_gallons": round(purchased, 3),
        "average_price_per_gallon": money(receipts / Decimal(str(purchased))) if purchased else None,
        "remaining_gallons": round(fuel, 3),
    }
