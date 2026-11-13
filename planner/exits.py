"""Place stations at the interstate exit their CSV address names, using OpenStreetMap exits.

Most CSV addresses are exit descriptions ("I-44, EXIT 283 & US-69"), not
street addresses. OSM maps numbered exits as ``highway=motorway_junction``
nodes whose ``ref`` is the exit number, on motorway ways whose ``ref`` names
the highway ("I 44;OK 66"). A station is placed at an exit only when **all**
of these hold:

* a highway listed before "EXIT" matches a motorway the junction lies on;
* the exit number matches (letters too, when both sides have one);
* the junction is in the station's state, within ``MAX_TOWN_MILES`` of its town;
* every matching junction (usually one node per direction of travel) lies
  within ``MAX_SPREAD_MILES`` of the others; their mean is used.

Anything else (no exit number, no match, ambiguity) keeps the town position.
"""

import re

import numpy as np

from planner.geo import EARTH_RADIUS_MILES

MAX_TOWN_MILES = 20.0
MAX_SPREAD_MILES = 2.0
INTERSTATE, US_ROUTE, STATE_ROUTE = "I", "US", "S"

_ROUTE = re.compile(r"\b(I|US\s+HWY|US|SR|ST|HWY|CR)[- ]?(\d+)([NSEW]?)((?:/\d+[NSEW]?)*)", re.IGNORECASE)
_EXIT = re.compile(r"\bEXIT\s*#?\s*(\d+)\s*([A-Z])?\b", re.IGNORECASE)
_OSM_ROUTE = re.compile(r"^\s*([A-Z]+)[- ]?(\d+)", re.IGNORECASE)


def _kind(prefix):
    prefix = prefix.upper().replace(" ", "")
    if prefix == "I":
        return INTERSTATE
    if prefix in {"US", "USHWY"}:
        return US_ROUTE
    return STATE_ROUTE  # SR, ST, HWY, or a state's own prefix in OSM ("OK 66", "WI 29").


def parse_exit_address(address):
    """'I-55/57, EXIT 66A & US-60' -> ({('I', '55'), ('I', '57')}, ('66', 'A')), or None without an exit."""
    found = _EXIT.search(address)
    if not found:
        return None
    routes = set()
    for prefix, number, _suffix, more in _ROUTE.findall(address[: found.start()]):
        kind = _kind(prefix)
        routes.add((kind, number))
        routes.update((kind, re.match(r"\d+", extra)[0]) for extra in more.split("/") if extra)
    if not routes:
        return None
    return routes, (found[1], (found[2] or "").upper())


def osm_routes(ref):
    """'I 44;OK 66' -> {('I', '44'), ('S', '66')}."""
    routes = set()
    for part in (ref or "").split(";"):
        found = _OSM_ROUTE.match(part)
        if found:
            routes.add((_kind(found[1]), found[2]))
    return routes


def osm_exits(ref):
    """'66A;66B' -> {('66', 'A'), ('66', 'B')}; '283' -> {('283', '')}."""
    exits = set()
    for part in re.split(r"[;,/]", ref or ""):
        found = re.match(r"\s*(\d+)\s*-?\s*([A-Z])?", part.upper())
        if found:
            exits.add((found[1], found[2] or ""))
    return exits


def junctions_from_overpass(elements):
    """Overpass nodes + ways -> list of {id, lat, lon, exits, routes}."""
    routes_by_node = {}
    for element in elements:
        if element["type"] == "way":
            for node in element.get("nodes", []):
                routes_by_node.setdefault(node, set()).update(osm_routes(element.get("tags", {}).get("ref")))
    return [
        {
            "id": element["id"],
            "lat": element["lat"],
            "lon": element["lon"],
            "exits": osm_exits(element["tags"].get("ref")),
            "routes": routes_by_node.get(element["id"], set()),
        }
        for element in elements
        if element["type"] == "node" and routes_by_node.get(element["id"])
    ]


def miles_between(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def match_exit(address, town_lat, town_lon, junctions):
    """Return {latitude, longitude, nodes, town_miles, exit, routes} or None (unparsed, unmatched or ambiguous)."""
    parsed = parse_exit_address(address)
    if parsed is None:
        return None
    routes, (number, letter) = parsed
    nearby = [
        j
        for j in junctions
        if j["routes"] & routes and miles_between(town_lat, town_lon, j["lat"], j["lon"]) <= MAX_TOWN_MILES
    ]
    exact = [j for j in nearby if (number, letter) in j["exits"]]
    loose = [j for j in nearby if any(n == number and (not letter or not ltr) for n, ltr in j["exits"])]
    chosen = exact or loose
    if not chosen:
        return None
    lats, lons = np.array([j["lat"] for j in chosen]), np.array([j["lon"] for j in chosen])
    spread = max(
        (
            miles_between(lats[i], lons[i], lats[k], lons[k])
            for i in range(len(chosen))
            for k in range(i + 1, len(chosen))
        ),
        default=0.0,
    )
    if spread > MAX_SPREAD_MILES:
        return None
    latitude, longitude = float(lats.mean()), float(lons.mean())
    return {
        "latitude": round(latitude, 7),
        "longitude": round(longitude, 7),
        "nodes": sorted(j["id"] for j in chosen),
        "town_miles": round(float(miles_between(town_lat, town_lon, latitude, longitude)), 2),
        "exit": number + letter,
        "routes": sorted(f"{kind}-{num}" for kind, num in routes),
    }
