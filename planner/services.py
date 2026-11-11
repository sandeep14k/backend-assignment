"""Trip planning pipeline: route -> stations on the route -> cheapest purchases."""

import time

from planner import cache
from planner.data import station_index, stations
from planner.optimizer import CAPACITY_GALLONS, MPG, RANGE_MILES, ROAD_FACTOR, candidates, optimize
from planner.routing import fetch_route

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
    "detour_miles",
    "cost_usd",
    "reason",
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


def plan_trip(start, finish, initial_fuel_gallons=None, corridor_miles=5.0, min_stop_gallons=5.0):
    """Plan the cheapest fuel purchases between two resolved locations."""
    started = time.perf_counter()
    route, routing_calls, route_source = fetch_route(start, finish)
    routing_ms = _elapsed_ms(started)

    step = time.perf_counter()
    index = station_index()
    on_route, _ = cache.get_or_load(
        f"{route['cache_key']}:corridor:{corridor_miles:g}",
        lambda: candidates(route, index, corridor_miles),
    )
    matching_ms = _elapsed_ms(step)

    step = time.perf_counter()
    plan = optimize(on_route, route["distance_miles"], initial_fuel_gallons, min_stop_gallons)
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
            "initial_fuel_gallons": initial_fuel_gallons,
            "corridor_miles": corridor_miles,
            "min_stop_gallons": min_stop_gallons,
            "starting_fuel": "Omitted initial_fuel_gallons: the vehicle starts with just enough fuel to reach the "
            "first station on the route, and that fuel is valued at the station's price and included in the total.",
            "detours": f"Reaching an off-route station costs {ROAD_FACTOR:g}x its straight-line distance each way; "
            "that fuel is bought at the stop and included. A small reserve keeps the tank between 0 and 50 gallons.",
            "station_locations": "Station positions are their city's coordinates (the price file has no coordinates).",
            "prices": "Retail prices from the supplied CSV.",
        },
        "metadata": {
            "routing_calls": routing_calls,
            "route_cache_hit": routing_calls == 0,
            "route_source": route_source,
            "candidate_stations": len(on_route),
            "stations_without_coordinates": stations()[1],
            "timing_ms": {
                "routing": routing_ms,
                "matching": matching_ms,
                "optimize": optimize_ms,
            },
        },
    }
