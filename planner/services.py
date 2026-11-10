"""Trip planning pipeline: route -> stations on the route -> cheapest purchases."""

import time

from django.core.cache import cache

from planner.data import station_points, stations
from planner.optimizer import (
    CAPACITY_GALLONS,
    MIN_STOP_GALLONS,
    MPG,
    RANGE_MILES,
    candidates,
    optimize,
    origin_station,
)
from planner.routing import CACHE_SECONDS, fetch_route

STOP_FIELDS = (
    "id",
    "name",
    "address",
    "city",
    "state",
    "latitude",
    "longitude",
    "coordinate_precision",
    "price_per_gallon",
    "route_mile",
    "offset_miles",
    "gallons_on_arrival",
    "gallons_purchased",
    "gallons_on_departure",
    "cost_usd",
)


def _elapsed_ms(since):
    return round((time.perf_counter() - since) * 1000, 1)


def _point(lon, lat, **properties):
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def _geojson(route, start, finish, stops):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "route"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": route["display_coordinates"],
                },
            },
            _point(start["longitude"], start["latitude"], kind="start", name=start["name"]),
            _point(
                finish["longitude"],
                finish["latitude"],
                kind="finish",
                name=finish["name"],
            ),
            *(
                _point(
                    stop["longitude"],
                    stop["latitude"],
                    kind="fuel_stop",
                    stop_number=n,
                    **stop,
                )
                for n, stop in enumerate(stops, 1)
            ),
        ],
    }


def plan_trip(start, finish, initial_gallons, corridor_miles):
    """Plan the cheapest fuel purchases between two resolved locations."""
    started = time.perf_counter()
    route, routing_calls = fetch_route(start, finish)
    routing_ms = _elapsed_ms(started)

    step = time.perf_counter()
    inventory, unlocated = stations()
    points = station_points()
    key = f"{route['cache_key']}:corridor:{corridor_miles:g}"
    on_route = cache.get(key)
    if on_route is None:
        on_route = candidates(route, inventory, corridor_miles, points)
        cache.set(key, on_route, CACHE_SECONDS)
    origin = origin_station(start, inventory, points) if initial_gallons < CAPACITY_GALLONS else None
    if origin:
        on_route = [origin] + [s for s in on_route if s["id"] != origin["id"]]
    matching_ms = _elapsed_ms(step)

    step = time.perf_counter()
    plan = optimize(on_route, route["distance_miles"], initial_gallons, MIN_STOP_GALLONS)
    optimize_ms = _elapsed_ms(step)

    plan["fuel_stops"] = [{field: stop[field] for field in STOP_FIELDS} for stop in plan["fuel_stops"]]
    return {
        "start": start,
        "finish": finish,
        "distance_miles": round(route["distance_miles"], 1),
        "duration_hours": round(route["duration_seconds"] / 3600, 2),
        **plan,
        "map": _geojson(route, start, finish, plan["fuel_stops"]),
        "assumptions": {
            "range_miles": RANGE_MILES,
            "mpg": MPG,
            "tank_capacity_gallons": CAPACITY_GALLONS,
            "initial_fuel_gallons": initial_gallons,
            "corridor_miles": corridor_miles,
            "first_fill": "A non-full tank is first filled at the cheapest station within 25 miles of the start.",
            "small_stops": (
                f"Top-ups under {MIN_STOP_GALLONS:g} gallons move to the previous or next stop when feasible; "
                "the extra cost is reported as merge_extra_cost_usd."
            ),
            "station_locations": "Station positions are their city's coordinates (the price file has no coordinates).",
            "prices": "Retail prices from the supplied CSV.",
        },
        "metadata": {
            "routing_calls": routing_calls,
            "route_cache_hit": routing_calls == 0,
            "candidate_stations": len(on_route),
            "stations_without_coordinates": unlocated,
            "timing_ms": {
                "routing": routing_ms,
                "matching": matching_ms,
                "optimize": optimize_ms,
            },
        },
    }
