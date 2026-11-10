"""Compare free routing APIs on long US routes (the evidence behind README "Choosing the routing API").

Usage: python scripts/benchmark_routing_providers.py
Uses one keep-alive session per provider; "first" includes connection setup, "warm" is the median after it.
Each run nudges the start point ~50 m so no request repeats: public servers cache identical URLs,
which would otherwise make repeated requests look faster than real route computation.
"""

import json
import statistics
import time

import requests

ROUTES = {
    "Chicago-Los Angeles": ((41.885847, -87.618123), (33.973093, -118.247896)),
    "New York-Miami": ((40.7128, -74.0060), (25.7617, -80.1918)),
    "Seattle-Boston": ((47.6062, -122.3321), (42.3601, -71.0589)),
}
RUNS = 4
OSRM_PARAMS = "steps=false&alternatives=false&overview=full&geometries=polyline6&generate_hints=false"


def osrm(base, params=OSRM_PARAMS):
    def call(session, a, b):
        return session.get(f"{base}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}?{params}", timeout=30)

    return call


def valhalla(session, a, b):
    body = {"locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}], "costing": "auto"}
    return session.post("https://valhalla1.openstreetmap.de/route", json=body, timeout=30)


def brouter(session, a, b):
    params = {"lonlats": f"{a[1]},{a[0]}|{b[1]},{b[0]}", "profile": "car-fast", "format": "geojson"}
    return session.get("https://brouter.de/brouter", params=params, timeout=60)


KEYLESS = {
    "OSRM (router.project-osrm.org), polyline6": osrm("https://router.project-osrm.org"),
    "OSRM (FOSSGIS routing.openstreetmap.de), polyline6": osrm("https://routing.openstreetmap.de/routed-car"),
    "OSRM, GeoJSON + per-segment annotations": osrm(
        "https://router.project-osrm.org", "steps=false&overview=full&geometries=geojson&annotations=distance"
    ),
    "Valhalla (FOSSGIS)": valhalla,
    "BRouter (brouter.de)": brouter,
}
KEYED = {
    "OpenRouteService": "https://api.openrouteservice.org/v2/directions/driving-car?start=-87.6,41.9&end=-118.2,34.0",
    "GraphHopper": "https://graphhopper.com/api/1/route?point=41.9,-87.6&point=34.0,-118.2&profile=car",
    "Mapbox": "https://api.mapbox.com/directions/v5/mapbox/driving/-87.6,41.9;-118.2,34.0",
    "HERE": "https://router.hereapi.com/v8/routes?transportMode=car&origin=41.9,-87.6&destination=34.0,-118.2",
    "TomTom": "https://api.tomtom.com/routing/1/calculateRoute/41.9,-87.6:34.0,-118.2/json",
    "Geoapify": "https://api.geoapify.com/v1/routing?waypoints=41.9,-87.6|34.0,-118.2&mode=drive",
    "Google": "https://maps.googleapis.com/maps/api/directions/json?origin=41.9,-87.6&destination=34.0,-118.2",
}


def main():
    for name, call in KEYLESS.items():
        session = requests.Session()
        print(name)
        for route, (a, b) in ROUTES.items():
            times, response = [], None
            for run in range(RUNS):
                start = (round(a[0] + 0.0005 * run, 6), a[1])
                started = time.perf_counter()
                try:
                    response = call(session, start, b)
                except requests.RequestException as exc:
                    print(f"  {route:<20} error: {exc}")
                    break
                times.append((time.perf_counter() - started) * 1000)
                if response.status_code != 200:
                    break
            if response is None:
                continue
            if response.status_code != 200:
                print(f"  {route:<20} HTTP {response.status_code}: {response.text[:90].strip()}")
                continue
            warm = statistics.median(times[1:])
            print(f"  {route:<20} first {times[0]:6.0f} ms  warm {warm:5.0f} ms  {len(response.content) // 1024:5d} KB")
    print("Key-required services (called without a key):")
    for name, url in KEYED.items():
        try:
            response = requests.get(url, timeout=15)
            detail = json.dumps(response.json())[:80] if response.content else ""
        except (requests.RequestException, ValueError) as exc:
            print(f"  {name:<18} {exc}")
            continue
        print(f"  {name:<18} HTTP {response.status_code} {detail}")


if __name__ == "__main__":
    main()
