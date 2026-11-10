import hashlib
import requests
from django.conf import settings
from django.core.cache import cache


class RoutingError(Exception):
    pass


def fetch_route(start, finish):
    coordinates = f'{start["longitude"]},{start["latitude"]};{finish["longitude"]},{finish["latitude"]}'
    key = "route:" + hashlib.sha256((settings.OSRM_URL + coordinates).encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    try:
        response = requests.get(f"{settings.OSRM_URL}/route/v1/driving/{coordinates}", params={"overview": "full", "geometries": "geojson", "annotations": "distance", "steps": "false", "alternatives": "false", "radiuses": "10000;10000"}, timeout=(4, 20))
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise RoutingError("No drivable route found between these locations.")
        route = payload["routes"][0]
        distances = [d for leg in route["legs"] for d in leg["annotation"]["distance"]]
        if len(distances) != len(route["geometry"]["coordinates"]) - 1:
            raise RoutingError("Routing provider returned inconsistent geometry.")
        result = {"geometry": route["geometry"], "segment_miles": [d / 1609.344 for d in distances], "distance_miles": route["distance"] / 1609.344, "duration_seconds": route["duration"]}
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        raise RoutingError("Routing service unavailable or returned invalid data; please retry.") from exc
    cache.set(key, result, 86400)
    return result, False
