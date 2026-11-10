"""Fuel-stop selection: which stations lie on the route, and where to buy fuel.

Vehicle model (from the brief): 500-mile range at 10 MPG, i.e. a 50-gallon tank.
"""
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from scipy.spatial import cKDTree

RANGE_MILES = 500.0
MPG = 10.0
CAPACITY_GALLONS = RANGE_MILES / MPG
ORIGIN_RADIUS_MILES = 25.0
ORIGIN_FALLBACK_MILES = 100.0
MIN_STOP_GALLONS = 5.0
EPSILON = 1e-7


class InfeasibleRoute(Exception):
    pass


def _planar_scale(latitudes):
    """Miles per degree of [lon, lat] near the given latitudes (local flat-earth approximation)."""
    return np.array([69.172 * np.cos(np.radians(np.mean(latitudes))), 69.0])


def candidates(route, stations, corridor_miles=5.0, points=None):
    """Stations within ``corridor_miles`` of the route, with the route mile where each is reached.

    Each station is projected onto the closest of the segments adjacent to its
    eight nearest route vertices (KD-tree lookup, fully vectorized). Result is
    ordered by route mile; stations at the same mile keep only the cheapest.
    """
    coordinates = np.asarray(route["coordinates"], dtype=float)
    if len(coordinates) < 2 or not stations:
        return []
    if points is None:
        points = np.array([[s["longitude"], s["latitude"]] for s in stations], dtype=float)
    scale = _planar_scale(coordinates[:, 1])
    route_xy, station_xy = coordinates * scale, points * scale

    # Cheap pre-filter: ignore stations outside the route's padded bounding box.
    low, high = route_xy.min(axis=0) - corridor_miles, route_xy.max(axis=0) + corridor_miles
    nearby = np.flatnonzero(((station_xy >= low) & (station_xy <= high)).all(axis=1))
    if not len(nearby):
        return []
    station_xy = station_xy[nearby]

    _, nearest = cKDTree(route_xy).query(station_xy, k=min(8, len(route_xy)))
    nearest = np.asarray(nearest).reshape(len(station_xy), -1)
    segment = np.clip(np.concatenate([nearest, nearest - 1], axis=1), 0, len(route_xy) - 2)
    a, b = route_xy[segment], route_xy[segment + 1]
    direction = b - a
    length_sq = np.maximum((direction ** 2).sum(axis=2), 1e-20)
    fraction = np.clip(((station_xy[:, None, :] - a) * direction).sum(axis=2) / length_sq, 0, 1)
    offset = np.linalg.norm(a + fraction[:, :, None] * direction - station_xy[:, None, :], axis=2)
    best = offset.argmin(axis=1)

    rows = np.arange(len(station_xy))
    best_offset = offset[rows, best]
    best_segment = segment[rows, best]
    cumulative = np.concatenate([[0.0], np.cumsum(route["segment_miles"])])
    mile = cumulative[best_segment] + fraction[rows, best] * np.asarray(route["segment_miles"])[best_segment]

    found = [
        {**stations[nearby[i]], "route_mile": float(mile[i]), "offset_miles": round(float(best_offset[i]), 3)}
        for i in np.flatnonzero((best_offset <= corridor_miles) & (mile < route["distance_miles"] - EPSILON))
    ]
    found.sort(key=lambda s: (s["route_mile"], Decimal(s["price_per_gallon"]), s["id"]))
    unique = []
    for station in found:
        if not unique or station["route_mile"] - unique[-1]["route_mile"] > EPSILON:
            unique.append(station)
    return unique


def origin_station(start, stations, points):
    """The station where the trip's first fill happens (route mile 0).

    The cheapest station within 25 miles of the start; failing that, the nearest
    station within 100 miles. ``offset_miles`` reports the straight-line distance.
    """
    if not stations:
        return None
    scale = _planar_scale([start["latitude"]])
    distance = np.linalg.norm((points - [start["longitude"], start["latitude"]]) * scale, axis=1)
    close = np.flatnonzero(distance <= ORIGIN_RADIUS_MILES)
    if len(close):
        index = min(close, key=lambda i: (Decimal(stations[i]["price_per_gallon"]), distance[i]))
    else:
        index = int(distance.argmin())
        if distance[index] > ORIGIN_FALLBACK_MILES:
            return None
    return {**stations[index], "route_mile": 0.0, "offset_miles": round(float(distance[index]), 3)}


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
    """Gallons bought at each node under the greedy rule described in ``optimize``."""
    next_cheaper = _next_cheaper(prices)
    purchases = [0.0] * len(nodes)
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
        target_mile = nodes[next_cheaper[i]]["route_mile"] if next_cheaper[i] is not None else position + RANGE_MILES
        purchase = min(CAPACITY_GALLONS, (min(target_mile, distance_miles) - position) / MPG) - fuel
        if purchase > EPSILON:
            purchases[i] = purchase
            fuel += purchase
    return purchases


def _cost(purchases, prices):
    return sum((Decimal(str(gallons)) * price for gallons, price in zip(purchases, prices) if gallons > EPSILON), Decimal(0))


def _arrival_fuel(nodes, purchases, initial_gallons):
    """Gallons in the tank on arrival at each node, before buying."""
    arrivals, fuel, position = [], float(initial_gallons), 0.0
    for node, purchase in zip(nodes, purchases):
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
    Mutates ``purchases``; returns how many stops were removed.
    """
    merged = 0
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
            merged += 1
    return merged


def optimize(stations, distance_miles, initial_gallons=0.0, min_stop_gallons=0.0):
    """Minimum-cost fuel purchases along a fixed route (exact, O(n)).

    Rule at each station: if a cheaper station is within range, buy just enough
    to reach it; otherwise fill the tank (or buy just enough to finish).
    Why it is optimal: fuel bought here before a reachable cheaper station could
    have been bought there for less; when nothing cheaper is reachable, every
    gallon needed before the next refill is cheapest right here.
    The destination is modelled as a free "station" so we never overbuy.

    ``min_stop_gallons`` then folds tiny top-ups into the previous stop (see
    ``_merge_small_purchases``); the extra cost is reported, typically cents.
    """
    nodes = [*stations, {"route_mile": distance_miles, "price_per_gallon": "0"}]
    prices = [Decimal(node["price_per_gallon"]) for node in nodes]
    purchases = _cheapest_purchases(nodes, prices, distance_miles, initial_gallons)
    optimal_cost = _cost(purchases, prices)
    merged = _merge_small_purchases(nodes, purchases, prices, initial_gallons, min_stop_gallons) if min_stop_gallons else 0

    stops, fuel, total = [], float(initial_gallons), Decimal(0)
    for node, previous, purchase, price in zip(nodes, [None, *nodes], purchases, prices):
        fuel -= (node["route_mile"] - (previous["route_mile"] if previous else 0.0)) / MPG
        fuel = max(fuel, 0.0)
        if purchase <= EPSILON:
            continue
        cost = Decimal(str(purchase)) * price
        total += cost
        stops.append({
            **node,
            "route_mile": round(node["route_mile"], 3),
            "gallons_on_arrival": round(fuel, 3),
            "gallons_purchased": round(purchase, 3),
            "gallons_on_departure": round(fuel + purchase, 3),
            "cost_usd": money(cost),
        })
        fuel += purchase

    # Total = sum of the rounded receipts, so the numbers shown always add up.
    receipts = sum((Decimal(stop["cost_usd"]) for stop in stops), Decimal(0))
    purchased = sum(stop["gallons_purchased"] for stop in stops)
    return {
        "total_cost_usd": money(receipts),
        "unrounded_total_cost_usd": str(total),
        "small_stops_merged": merged,
        "merge_extra_cost_usd": money(total - optimal_cost),
        "fuel_stops": stops,
        "fuel_consumed_gallons": round(distance_miles / MPG, 3),
        "fuel_purchased_gallons": round(purchased, 3),
        "average_price_per_gallon": money(receipts / Decimal(str(purchased))) if purchased else None,
        "remaining_gallons": round(fuel, 3),
    }
