"""Trip planning pipeline: route -> stations on the route -> cheapest purchases."""

import copy
import hashlib
import time
from collections import Counter

from planner import cache
from planner.data import is_sourced, station_index
from planner.geo import heading_at
from planner.optimizer import (
    CAPACITY_GALLONS,
    MPG,
    RANGE_MILES,
    InfeasibleRoute,
    candidates,
    detour_miles,
    optimize,
)
from planner.routing import NoRoute, RoutingBudget, fetch_itinerary, fetch_route

STOP_FIELDS = (
    "id",
    "name",
    "address",
    "city",
    "state",
    "latitude",
    "longitude",
    "coordinate_precision",
    "coordinate_source",
    "road_snap_metres",
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


def coverage_gaps(nodes, distance, initial):
    """Unreachable intervals in the forward estimated-detour reachability graph."""
    reachable = [(0.0, (50 if initial is None else initial) * MPG)]
    gaps = []
    for node in nodes:
        mile, detour = node["route_mile"], detour_miles(node)
        if any(mile - pos + detour <= fuel + 1e-6 for pos, fuel in reachable):
            reachable.append((mile, RANGE_MILES - detour))
    frontier = max(pos + fuel for pos, fuel in reachable)
    if frontier < distance - 1e-6:
        left = max(pos for pos, _ in reachable)
        right = next((s["route_mile"] for s in nodes if s["route_mile"] > frontier), distance)
        gaps.append((left, right))
    return gaps


def select_adaptively(route, index, initial, corridor):
    nodes, last_error = candidates(route, index, corridor), None
    for radius in [corridor, *[r for r in (10, 15, 25) if r > corridor]]:
        expanded = candidates(route, index, radius) if radius > corridor else []
        while True:
            try:
                return optimize(nodes, route["distance_miles"], initial), nodes, radius
            except InfeasibleRoute as error:
                last_error = error
                if not expanded:
                    break
                gaps = coverage_gaps(nodes, route["distance_miles"], initial)
                if not gaps:
                    gaps = [(0, nodes[0]["route_mile"] if nodes else route["distance_miles"])]
                by_id = {s["id"]: s for s in nodes}
                by_id.update({s["id"]: s for s in expanded if any(a <= s["route_mile"] <= b for a, b in gaps)})
                if len(by_id) == len(nodes):
                    break
                nodes = sorted(by_id.values(), key=lambda s: s["route_mile"])
    # A gap in the price list explains it best; otherwise keep the optimizer's own reason
    # (e.g. the starting fuel cannot reach any station).
    raise InfeasibleRoute(_coverage_message(route, index) or str(last_error))


def _coverage_message(route, index):
    """Say where the supplied price list leaves the route without fuel, e.g. 'mile 0 to 537', or None."""
    wide = candidates(route, index, 25)
    ends = [(0.0, "the start"), *((s["route_mile"], f"{s['city']}, {s['state']}") for s in wide)]
    ends.append((route["distance_miles"], "the finish"))
    size, (left, left_name), (right, right_name) = max(
        ((b[0] - a[0], a, b) for a, b in zip(ends, ends[1:], strict=False)), key=lambda gap: gap[0]
    )
    if size > RANGE_MILES:
        return (
            f"The supplied fuel-price list has no station within 25 miles of this route between mile {left:.0f} "
            f"({left_name}) and mile {right:.0f} ({right_name}): a {size:.0f}-mile gap, longer than the "
            f"{RANGE_MILES:.0f}-mile range."
        )
    return None


def _with_heading(route, stops):
    """Exit positions sit on the motorway: give OSRM the direction of travel so it picks the right carriageway."""
    return [
        {**stop, "heading": heading_at(route["coordinates"], route["segment_miles"], stop["route_mile"])}
        if stop.get("coordinate_precision") == "exit_match"
        else stop
        for stop in stops
    ]


def locate_itinerary(route, selected):
    mile, located = 0.0, []
    for stop, leg, snap in zip(selected, route["leg_miles"][:-1], route["station_snap_metres"], strict=True):
        mile += leg
        located.append({**stop, "route_mile": mile, "offset_miles": 0.0, "road_snap_metres": snap})
    return located


def repair_selection(route, selected, index, initial):
    """Insert one estimated-reachable bridge in the first infeasible road leg."""
    miles = [0.0]
    for leg in route["leg_miles"]:
        miles.append(miles[-1] + leg)
    for i, leg in enumerate(route["leg_miles"]):
        limit = (initial * MPG if initial is not None else RANGE_MILES) if i == 0 else RANGE_MILES
        if leg <= limit + 1e-6:
            continue
        choices = [
            s
            for s in candidates(route, index, 25)
            if s["id"] not in {n["id"] for n in selected}
            and miles[i] <= s["route_mile"] <= miles[i + 1]
            and s["route_mile"] - miles[i] + detour_miles(s) <= limit
            and miles[i + 1] - s["route_mile"] + detour_miles(s) <= RANGE_MILES
        ]
        if choices:
            bridge = min(choices, key=lambda s: (s["offset_miles"], float(s["price_per_gallon"])))
            return [*selected[:i], bridge, *selected[i:]]
        break
    raise InfeasibleRoute("Verified itinerary exceeds range; no reachable bridging station found.")


def plan_trip(start, finish, initial_fuel_gallons=None, corridor_miles=5.0, verify_stops=False):
    """Plan the cheapest fuel purchases between two resolved locations.

    Default: one routing call. Stations are matched to the route geometry and the
    optimizer charges each stop's estimated detour (``detour_miles``, both
    ways) against the tank. Feasibility is relative to this location/access model.
    ``verify_stops`` spends one or two more calls to route through the chosen
    coordinates and re-solve purchases. Exit and town positions remain estimates.
    """
    started = time.perf_counter()
    budget = RoutingBudget()
    route, routing_calls, route_source = fetch_route(start, finish, budget=budget)
    budget.used = max(budget.used, routing_calls)
    routing_ms = _elapsed_ms(started)

    step = time.perf_counter()
    index = station_index()
    derived_key = f"{route['cache_key']}:data:{index.fingerprint}"
    matching_ms = _elapsed_ms(step)
    step = time.perf_counter()
    estimated_selection, selection_source = cache.get_or_load(
        f"{derived_key}:adaptive:v3:{corridor_miles!r}:{initial_fuel_gallons!r}",
        lambda: select_adaptively(route, index, initial_fuel_gallons, corridor_miles),
    )
    estimated, on_route, search_radius = estimated_selection
    selection_ms = _elapsed_ms(step)
    step = time.perf_counter()
    selected, located = estimated["fuel_stops"], []
    if not verify_stops or not selected:
        plan = copy.deepcopy(estimated)
        for stop in plan["fuel_stops"]:
            stop["road_snap_metres"] = None
        itinerary_ms = optimize_ms = 0.0
        plan_source = selection_source
        return _response(
            route,
            start,
            finish,
            plan,
            [],
            on_route,
            search_radius,
            routing_calls,
            route_source,
            plan_source,
            initial_fuel_gallons,
            corridor_miles,
            False,
            [routing_ms, matching_ms, selection_ms, 0.0, 0.0],
            verification_requested=verify_stops,
        )
    if selected:
        selected = _with_heading(route, selected)
        before = budget.used
        try:
            route, verification_calls, _ = fetch_itinerary(
                start, finish, selected, max_calls=min(2, budget.remaining), budget=budget
            )
        except NoRoute as exc:
            if not budget.remaining:
                raise InfeasibleRoute("Stop coordinates could not be routed within the routing budget.") from exc
            # Provider does not identify the bad waypoint: replace the nearest
            # eligible alternative once, then require complete road verification.
            alternatives = [
                (abs(other["route_mile"] - stop["route_mile"]), i, other)
                for i, stop in enumerate(selected)
                for other in on_route
                if other["id"] not in {s["id"] for s in selected}
                and abs(other["route_mile"] - stop["route_mile"]) <= 25
            ]
            if not alternatives:
                raise InfeasibleRoute("Station access failed; no alternative station available.") from exc
            _, position, replacement = min(alternatives, key=lambda row: row[0])
            selected = sorted(
                [*selected[:position], *_with_heading(route, [replacement]), *selected[position + 1 :]],
                key=lambda stop: stop["route_mile"],
            )
            try:
                route, _, _ = fetch_itinerary(start, finish, selected, max_calls=1, budget=budget)
            except NoRoute as retry:
                raise InfeasibleRoute("Alternative stop coordinates could not be routed.") from retry
            verification_calls = budget.used - before
        routing_calls += verification_calls
        budget.used = max(budget.used, routing_calls)
        located = locate_itinerary(route, selected)
        try:
            optimize(located, route["distance_miles"], initial_fuel_gallons)
        except InfeasibleRoute:
            if not budget.remaining:
                raise InfeasibleRoute("Verified itinerary exceeds range and routing budget is exhausted.") from None
            selected = repair_selection(route, selected, index, initial_fuel_gallons)
            # Only the new bridge lacks a heading; it was measured on this itinerary route.
            selected = [stop if "heading" in stop else _with_heading(route, [stop])[0] for stop in selected]
            route, repair_calls, _ = fetch_itinerary(start, finish, selected, max_calls=1, budget=budget)
            routing_calls += repair_calls
            located = locate_itinerary(route, selected)
    itinerary_ms = _elapsed_ms(step)
    step = time.perf_counter()
    # Separate cache stages: never hold one cross-process cache lock while
    # acquiring another. The final plan uses routed road legs, not offset * 1.3.
    identities = hashlib.sha256(",".join(stop["id"] for stop in located).encode()).hexdigest()[:24]
    final_key = f"{route['cache_key']}:final:v4:{index.fingerprint}:{initial_fuel_gallons!r}:{identities}"
    plan, plan_source = cache.get_or_load(
        final_key, lambda: optimize(located, route["distance_miles"], initial_fuel_gallons)
    )
    plan = copy.deepcopy(plan)
    optimize_ms = _elapsed_ms(step)
    return _response(
        route,
        start,
        finish,
        plan,
        located,
        on_route,
        search_radius,
        routing_calls,
        route_source,
        plan_source,
        initial_fuel_gallons,
        corridor_miles,
        bool(selected),
        [routing_ms, matching_ms, selection_ms, itinerary_ms, optimize_ms],
        verification_requested=verify_stops,
    )


def _response(
    route, start, finish, plan, located, on_route, search_radius, routing_calls, route_source, plan_source,
    initial_fuel_gallons, corridor_miles, itinerary_routed, timings, verification_requested=False,
):  # fmt: skip
    plan["fuel_stops"] = [{field: stop[field] for field in STOP_FIELDS} for stop in plan["fuel_stops"]]
    approximate = [stop for stop in plan["fuel_stops"] if not is_sourced(stop)]
    warnings = []
    if approximate:
        warnings.append(
            {
                "code": "approximate_station_locations",
                "message": f"{len(approximate)} fuel stop(s) use an exit or town location. "
                "Confirm the listed station address and access before driving; fuel and detour estimates may change.",
                "station_ids": [stop["id"] for stop in approximate],
            }
        )
    for stop in plan["fuel_stops"]:
        if is_sourced(stop):
            stop["location_note"] = "Station coordinates matched to a cited source."
        elif stop["coordinate_precision"] == "exit_match":
            stop["location_note"] = "Approximate: highway exit; pump access assumes 0.5 mile each way."
        else:
            stop["location_note"] = "Approximate: town centre; actual station position and access are unverified."
    routing_ms, matching_ms, selection_ms, itinerary_ms, optimize_ms = timings
    precisions = Counter(stop["coordinate_precision"] for stop in plan["fuel_stops"])
    verification_status = "not_requested"
    if itinerary_routed:
        verification_status = "waypoints_routed"
    elif verification_requested and not plan["fuel_stops"]:
        verification_status = "not_needed"
    return {
        "start": start,
        "finish": finish,
        "distance_miles": round(route["distance_miles"], 1),
        "duration_hours": round(route["duration_seconds"] / 3600, 2),
        **plan,
        "warnings": warnings,
        "map": _geojson(route, start, finish, plan["fuel_stops"]),
        "routed_leg_miles": route.get("leg_miles", [route["distance_miles"]]),
        "routed_waypoints": [
            {
                "id": stop["id"],
                "name": stop["name"],
                "latitude": stop["latitude"],
                "longitude": stop["longitude"],
                "road_snap_metres": stop["road_snap_metres"],
                "route_mile": stop["route_mile"],
            }
            for stop in located
        ],
        "assumptions": {
            "range_miles": RANGE_MILES,
            "mpg": MPG,
            "tank_capacity_gallons": CAPACITY_GALLONS,
            "initial_fuel_gallons": initial_fuel_gallons,
            "corridor_miles": corridor_miles,
            "starting_fuel": "Automatic mode starts with enough fuel for the first reachable candidate, "
            "including modeled access, and values it at that station's price. "
            "Explicit starting fuel is already owned and is not charged again.",
            "detours": (
                "Mileage uses OSRM road legs through the chosen coordinates. Fuel also includes the "
                "estimated 0.5-mile access each way for exit locations. Town locations remain approximate."
                if itinerary_routed
                else "Each stop's detour is estimated as 1.3 x its straight-line offset from the route, each way, "
                "plus 0.5 mile each way for exit locations. Detour fuel is included in fuel_burned_gallons."
            ),
            "verification_scope": "verify_stops routes through the available coordinates, not the actual pumps "
            "when coordinates represent an exit or town centre. A road snap does not establish station access.",
            "station_locations": f"All {len(station_index().stations):,} priced US stations are candidates. "
            "coordinate_precision says where each position comes from: osm_identity_match / "
            "operator_identity_match (the station itself), exit_match (the OpenStreetMap interstate exit named in "
            "its CSV address, plus 0.5 mile each way to reach the pumps), or city_centroid (its listed town).",
            "optimization_scope": "Exact minimum-cost choice of stops and gallons for the fixed route and the "
            "modeled detours using a dynamic program. The objective is fuel spending plus $0.50 per stop "
            "in both estimated and waypoint-routed modes; reported costs are fuel money only. "
            "In waypoint-routed mode, purchases are optimized over the selected waypoints on that itinerary.",
            "prices": "Retail prices from the supplied CSV.",
        },
        "metadata": {
            "routing_calls": routing_calls,
            "route_cache_hit": routing_calls == 0,
            "plan_cache_hit": plan_source != "loaded",
            "route_source": route_source,
            "candidate_stations": len(on_route),
            "search_corridor_miles": search_radius,
            "candidate_pool_stations": len(station_index().stations),
            "stop_coordinate_precision": dict(precisions),
            "itinerary_routed": itinerary_routed,
            "approximate_stop_count": len(approximate),
            "verification_status": verification_status,
            "timing_ms": {
                "routing": routing_ms,
                "matching": matching_ms,
                "selection": selection_ms,
                "itinerary_routing": itinerary_ms,
                "optimize": optimize_ms,
            },
        },
    }
