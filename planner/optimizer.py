from decimal import Decimal, ROUND_HALF_UP
import numpy as np
from scipy.spatial import cKDTree

RANGE = 500.0
MPG = 10.0
CAPACITY = RANGE / MPG


class InfeasibleRoute(Exception):
    pass


def candidates(route, stations, corridor=5.0):
    """Project station coordinates onto nearby route segments, retaining route distance.

    Eight nearest vertices and their adjacent segments are inspected. This is a
    fast corridor approximation, not a road-network distance to a pump.
    """
    coordinates = np.asarray(route["geometry"]["coordinates"], dtype=float)
    if len(coordinates) < 2 or not stations:
        return []
    latitude = np.radians(coordinates[:, 1].mean())
    scale = np.array([69.172 * np.cos(latitude), 69.0])
    points = coordinates * scale
    locations = np.array([[s["longitude"], s["latitude"]] for s in stations]) * scale
    tree = cKDTree(points)
    _, nearest = tree.query(locations, k=min(8, len(points)))
    nearest = np.asarray(nearest).reshape(len(stations), -1)
    indices = np.clip(np.concatenate([nearest, nearest - 1], axis=1), 0, len(points) - 2)
    a, b = points[indices], points[indices + 1]
    vector = b - a
    length_sq = (vector * vector).sum(axis=2)
    fraction = np.clip(((locations[:, None, :] - a) * vector).sum(axis=2) / np.maximum(length_sq, 1e-20), 0, 1)
    projected = a + fraction[:, :, None] * vector
    offsets = np.linalg.norm(projected - locations[:, None, :], axis=2)
    best = offsets.argmin(axis=1)
    segment_miles = np.asarray(route["segment_miles"])
    cumulative = np.concatenate([[0], np.cumsum(segment_miles)])
    result = []
    for i, station in enumerate(stations):
        choice = best[i]
        offset = float(offsets[i, choice])
        segment = indices[i, choice]
        mile = float(cumulative[segment] + fraction[i, choice] * segment_miles[segment])
        if offset <= corridor and mile < route["distance_miles"] - 1e-6:
            result.append({**station, "route_mile": mile, "offset_miles": round(offset, 3)})
    # Co-located stations: retain cheapest, with deterministic tie-breaking.
    result.sort(key=lambda s: (s["route_mile"], Decimal(s["price_per_gallon"]), s["id"]))
    unique = []
    for station in result:
        if unique and abs(station["route_mile"] - unique[-1]["route_mile"]) < 1e-7:
            continue
        unique.append(station)
    return unique


def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def optimize(stations, distance, initial_gallons=50.0):
    """Exact minimum purchase cost on the fixed, zero-detour candidate model.

    Buy only enough to reach the first cheaper station within range. If none
    exists, fill to capacity or buy exactly enough to finish. Existing fuel is
    carried forward. A monotonic stack computes next cheaper stations in O(n).
    """
    nodes = [*stations, {"route_mile": distance, "price_per_gallon": "0"}]
    next_cheaper, stack = [None] * len(nodes), []
    for i in range(len(nodes) - 1, -1, -1):
        price = Decimal(nodes[i]["price_per_gallon"])
        while stack and Decimal(nodes[stack[-1]]["price_per_gallon"]) >= price:
            stack.pop()
        next_cheaper[i] = stack[-1] if stack else None
        stack.append(i)
    fuel, previous, total, stops = initial_gallons, 0.0, Decimal(0), []
    for i, station in enumerate(nodes):
        needed = (station["route_mile"] - previous) / MPG
        if needed > fuel + 1e-7:
            raise InfeasibleRoute(f'Insufficient fuel coverage near mile {previous:.1f}; next candidate is {station["route_mile"] - previous:.1f} miles away. Try a larger corridor or add verified stations.')
        fuel = max(0, fuel - needed)
        previous = station["route_mile"]
        if i == len(nodes) - 1:
            break
        cheaper = next_cheaper[i]
        target_distance = nodes[cheaper]["route_mile"] - previous if cheaper is not None else RANGE
        target = min(CAPACITY, target_distance / MPG, (distance - previous) / MPG)
        purchase = max(0.0, target - fuel)
        if purchase > 1e-7:
            cost = Decimal(str(purchase)) * Decimal(station["price_per_gallon"])
            total += cost
            stops.append({**station, "route_mile": round(previous, 3), "gallons_on_arrival": round(fuel, 6), "gallons_purchased": round(purchase, 6), "gallons_on_departure": round(fuel + purchase, 6), "cost_usd": money(cost)})
            fuel += purchase
    # Sum displayed receipt amounts so the visible receipts reconcile exactly.
    receipt_total = sum((Decimal(s["cost_usd"]) for s in stops), Decimal(0))
    return {"fuel_stops": stops, "total_cost_usd": money(receipt_total), "unrounded_total_cost_usd": str(total), "fuel_consumed_gallons": round(distance / MPG, 6), "fuel_purchased_gallons": round(sum(s["gallons_purchased"] for s in stops), 6), "remaining_gallons": round(fuel, 6)}
