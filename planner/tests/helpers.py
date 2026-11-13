"""Shared fixtures: an independent polyline encoder and OSRM-shaped payloads."""

import hashlib
from unittest.mock import Mock

import numpy as np

from planner.data import StationIndex


def station(mile, price, identity="1"):
    return {"id": identity, "route_mile": mile, "price_per_gallon": str(price)}


def make_station(identity, lon, lat, price="3.0"):
    return {
        "id": str(identity),
        "name": f"Stop {identity}",
        "address": "",
        "city": "X",
        "state": "ZZ",
        "latitude": lat,
        "longitude": lon,
        "price_per_gallon": str(price),
        "coordinate_precision": "osm_identity_match",
        "coordinate_source": "https://www.openstreetmap.org/node/123",
    }


def index_of(stations):
    return StationIndex.build(stations)


def encode(points, precision=6):
    """Reference polyline encoder (straightforward loop) for building fixtures."""
    out, previous = [], (0, 0)
    for lon, lat in points:
        current = (round(lat * 10**precision), round(lon * 10**precision))
        for value, last in zip(current, previous, strict=True):
            delta = value - last
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                out.append(chr((0x20 | (delta & 0x1F)) + 63))
                delta >>= 5
            out.append(chr(delta + 63))
        previous = current
    return "".join(out)


def osrm_response(points, meters, seconds=3600, code="Ok"):
    """A Mock shaped like a successful ``requests`` response from OSRM."""
    payload = {"code": code, "routes": [{"geometry": encode(points), "distance": meters, "duration": seconds}]}
    response = Mock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def fake_route(miles, points=((-87.6, 41.9), (-86.6, 41.9))):
    coordinates = np.array(points, dtype=float)
    return {
        "cache_key": "test:" + hashlib.sha256(repr((miles, points)).encode()).hexdigest()[:16],
        "coordinates": coordinates,
        "segment_miles": np.array([miles / (len(points) - 1)] * (len(points) - 1)),
        "distance_miles": miles,
        "duration_seconds": 3600,
        "display_coordinates": coordinates.tolist(),
    }
