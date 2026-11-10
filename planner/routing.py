"""Routing via the free, keyless OSRM HTTP API.

Why OSRM: no API key (reviewers can run the project immediately), no route
length cap (Valhalla's public server rejects routes over 1,500 km), and ~1 s
for a coast-to-coast route when requesting compact ``polyline6`` geometry.
Requesting per-segment ``annotations`` makes the payload ~4x larger and the
call 0.5-4 s slower, so segment lengths are computed locally instead.

Call budget: one request per uncached route. If the primary server fails, one
retry goes to an independent OSRM mirror, so never more than two calls.
"""

import hashlib
import logging

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
EARTH_RADIUS_MILES = 3958.7613
CACHE_SECONDS = 24 * 60 * 60


class RoutingError(Exception):
    pass


def decode_polyline(encoded, precision=6):
    """Decode a Google-encoded polyline into an (n, 2) array of [lon, lat]."""
    coordinates, index, lat, lon = [], 0, 0, 0
    length = len(encoded)
    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coordinates.append((lon, lat))
    return np.asarray(coordinates, dtype=float) / 10**precision


def segment_miles(coordinates):
    """Great-circle length of each consecutive segment of [lon, lat] points."""
    lon, lat = np.radians(coordinates[:, 0]), np.radians(coordinates[:, 1])
    dlat, dlon = np.diff(lat), np.diff(lon)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def simplify(coordinates, tolerance_miles=0.03):
    """Douglas-Peucker simplification for the response geometry (~50 m tolerance).

    Optimization always uses the full-resolution route; this only shrinks the
    payload the client has to download and draw.
    """
    if len(coordinates) < 3:
        return coordinates
    scale = np.array([69.172 * np.cos(np.radians(coordinates[:, 1].mean())), 69.0])
    points = coordinates * scale
    keep = np.zeros(len(points), dtype=bool)
    keep[[0, -1]] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        a, b = points[first], points[last]
        inner = points[first + 1 : last]
        direction = b - a
        length = np.hypot(*direction)
        if length == 0:
            distances = np.hypot(*(inner - a).T)
        else:
            relative = inner - a
            distances = (
                np.abs(direction[0] * relative[:, 1] - direction[1] * relative[:, 0])
                / length
            )
        farthest = int(distances.argmax())
        if distances[farthest] > tolerance_miles:
            split = first + 1 + farthest
            keep[split] = True
            stack.extend([(first, split), (split, last)])
    return coordinates[keep]


def _request(base_url, coordinates):
    response = requests.get(
        f"{base_url}/route/v1/driving/{coordinates}",
        params={
            "overview": "full",
            "geometries": "polyline6",
            "steps": "false",
            "alternatives": "false",
        },
        timeout=(3, 15),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError("No drivable route found between these locations.")
    return payload["routes"][0]


def fetch_route(start, finish):
    """Return ``(route, routing_calls)``; ``routing_calls`` is 0 on a cache hit."""
    coordinates = f"{start['longitude']:.6f},{start['latitude']:.6f};{finish['longitude']:.6f},{finish['latitude']:.6f}"
    key = "route:v2:" + hashlib.sha256(coordinates.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached, 0

    calls, raw = 0, None
    for base_url in settings.OSRM_URLS:
        calls += 1
        try:
            raw = _request(base_url, coordinates)
            break
        except RoutingError:
            raise  # A definitive "no route" answer; another mirror will agree.
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Routing provider %s failed: %s", base_url, exc)
    if raw is None:
        raise RoutingError("Routing service unavailable; please retry shortly.")

    try:
        points = decode_polyline(raw["geometry"])
        distance_miles = raw["distance"] / METERS_PER_MILE
        duration_seconds = raw["duration"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RoutingError("Routing provider returned invalid data.") from exc
    if len(points) < 2:
        raise RoutingError("Routing provider returned an empty route.")

    segments = segment_miles(points)
    # Rescale so cumulative mileage agrees exactly with the provider's road distance.
    total = segments.sum()
    if total > 0:
        segments *= distance_miles / total
    route = {
        "cache_key": key,
        "coordinates": points,
        "segment_miles": segments,
        "distance_miles": distance_miles,
        "duration_seconds": duration_seconds,
        "display_coordinates": np.round(simplify(points), 5).tolist(),
    }
    cache.set(key, route, CACHE_SECONDS)
    return route, calls
