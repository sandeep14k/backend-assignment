"""Routing via the free, keyless OSRM HTTP API (see README "Choosing the routing API").

One uncached route = one HTTP request to OSRM. Everything that makes that fast:

* **Warm HTTPS connections.** A new TLS connection to the public servers costs
  ~0.5-1 s; a route over a reused one ~0.2-0.5 s. One shared ``requests.Session``
  pools connections, and ``start_connection_warmer()`` keeps them open while
  the API is in use (``HEAD /``, not a routing request).
* **Compact response.** ``overview=full&geometries=polyline6`` without
  ``annotations``/``steps``/``hints``: 4-7x less data than GeoJSON with
  per-segment annotations. Segment lengths are computed locally instead.
* **Sequential fallback.** A shared request budget caps all attempts at three.
* **Caching.** Routes (and "no route" answers) are cached in memory and on disk
  (``planner.cache``), and concurrent requests for the same new route share one call.
"""

import hashlib
import json
import logging
import threading
import time
from urllib.parse import urlsplit

import numpy as np
import requests
from django.conf import settings
from requests.adapters import HTTPAdapter

from planner import cache
from planner.data import is_sourced
from planner.geo import METERS_PER_MILE, decode_polyline, segment_miles, simplify

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = "fuel-route-planner/1.0 (+Django; assessment project)"
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0))
_last_activity = time.monotonic()  # Startup counts as activity.
_public_request_lock = threading.Lock()
_public_last_request = {}


def _pace_public_provider(base_url):
    """At most one start per second per public host in this process.

    Multi-worker public deployments still need a shared gateway/rate limiter.
    Self-hosted URLs are unrestricted by this demo-service policy.
    """
    host = urlsplit(base_url).hostname
    if host not in {"router.project-osrm.org", "routing.openstreetmap.de"}:
        return
    with _public_request_lock:
        delay = 1.0 - (time.monotonic() - _public_last_request.get(host, -float("inf")))
        if delay > 0:
            time.sleep(delay)
        _public_last_request[host] = time.monotonic()


class RoutingError(Exception):
    pass


class NoRoute(RoutingError):
    """The provider answered definitively that the points are not connected by road."""


def _request(base_url, coordinates, radiuses=None, bearings=None):
    """One OSRM route request.

    ``radiuses`` lists the snap limit per point ("unlimited" or metres). ``bearings``
    lists "heading,range" per point ("" for none): it makes a waypoint on a divided
    highway snap to the carriageway going the right way, avoiding a U-turn.
    """
    _pace_public_provider(base_url)
    count = len(coordinates.split(";"))
    if radiuses is None and count > 2:
        radiuses = ["unlimited", *(["100"] * (count - 2)), "unlimited"]
    radiuses = {"radiuses": ";".join(radiuses)} if radiuses else {}
    if bearings and any(bearings):
        radiuses["bearings"] = ";".join(bearings)
    response = _session.get(
        f"{base_url}/route/v1/driving/{coordinates}",
        params={
            **radiuses,
            "overview": "full",
            "geometries": "polyline6",
            "steps": "false",
            "alternatives": "false",
            "generate_hints": "false",
        },
        timeout=(settings.OSRM_CONNECT_TIMEOUT, settings.OSRM_READ_TIMEOUT),
    )
    is_json = response.headers.get("content-type", "").startswith("application/json")
    if response.status_code == 400 and is_json and response.json().get("code") in {"NoRoute", "NoSegment"}:
        raise NoRoute("No drivable route between these locations.")
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") in {"NoRoute", "NoSegment"} or (payload.get("code") == "Ok" and not payload.get("routes")):
        raise NoRoute("No drivable route between these locations.")
    if payload.get("code") != "Ok":
        raise ValueError(f"Unexpected OSRM response code {payload.get('code')!r}")
    route = _build_route(payload["routes"][0])
    if count > 2:
        raw = payload["routes"][0]
        legs = raw.get("legs", [])
        waypoints = payload.get("waypoints", [])
        if len(legs) != count - 1 or len(waypoints) != count:
            raise RoutingError("Routing provider omitted itinerary legs or waypoints.")
        distances = [float(leg["distance"]) / METERS_PER_MILE for leg in legs]
        if not all(np.isfinite(d) and d >= 0 for d in distances):
            raise RoutingError("Invalid itinerary leg distance.")
        if abs(sum(distances) - route["distance_miles"]) > 0.01:
            raise RoutingError("Itinerary leg distances do not reconcile.")
        snaps = [float(point["distance"]) for point in waypoints[1:-1]]
        limits = radiuses["radiuses"].split(";")[1:-1]
        if not all(
            np.isfinite(d) and d >= 0 and (limit == "unlimited" or d <= float(limit))
            for d, limit in zip(snaps, limits, strict=True)
        ):
            raise NoRoute("A selected fuel station is not within 100 metres of a routable road.")
        route["leg_miles"] = distances
        route["station_snap_metres"] = snaps
    return route


class RoutingBudget:
    """Request-owned counter, charged before every HTTP attempt, including failures."""

    def __init__(self):
        self.used = 0

    @property
    def remaining(self):
        return 3 - self.used

    def consume(self):
        if not self.remaining:
            raise RoutingError("Routing budget exhausted before itinerary verification.")
        self.used += 1


def _call_providers(coordinates, max_calls=2, budget=None, radiuses=None, bearings=None):
    """Sequential fallback preserves the third attempt for itinerary repair."""
    budget = budget or RoutingBudget()
    urls = list(settings.OSRM_URLS)[: min(max_calls, budget.remaining)]
    if not urls:
        raise RoutingError("Routing budget exhausted or no provider configured.")
    before = budget.used
    for url in urls:
        budget.consume()
        try:
            return _request(url, coordinates, radiuses, bearings), budget.used - before
        except NoRoute:
            raise
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            RoutingError,
        ) as exc:
            logger.warning("Routing provider %s failed: %s", url, exc)
    raise RoutingError("Routing service unavailable; please retry shortly.")


def _build_route(raw):
    try:
        points = decode_polyline(raw["geometry"])
        distance_miles = float(raw["distance"]) / METERS_PER_MILE
        duration_seconds = float(raw["duration"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RoutingError("Routing provider returned invalid data.") from exc
    if (
        not np.isfinite(distance_miles)
        or distance_miles <= 0
        or not np.isfinite(duration_seconds)
        or duration_seconds < 0
        or not np.isfinite(points).all()
        or (len(points) and (np.abs(points[:, 0]).max() > 180 or np.abs(points[:, 1]).max() > 90))
    ):
        raise RoutingError("Routing provider returned invalid distances or coordinates.")
    if len(points) < 2:
        raise RoutingError("Routing provider returned an empty route.")

    # Planning runs on the ~50 m Douglas-Peucker simplification: 10-20x fewer
    # vertices than OSRM's full geometry, at an error far below the precision
    # of station positions. Segment lengths are rescaled so cumulative mileage
    # matches OSRM's road distance exactly.
    simplified = simplify(points)
    segments = segment_miles(simplified)
    if segments.sum() <= 0:
        raise RoutingError("Routing provider returned a zero-length geometry.")
    segments *= distance_miles / segments.sum()
    return {
        "coordinates": simplified,
        "segment_miles": segments,
        "distance_miles": distance_miles,
        "duration_seconds": duration_seconds,
        "display_coordinates": np.round(simplified, 5).tolist(),
    }


def fetch_route(start, finish, budget=None):
    """Return ``(route, routing_calls, cache_source)``.

    ``routing_calls`` counts HTTP requests made to the routing provider by this
    call: 0 when served from cache, 1 normally, 2 only if the mirror was used.
    """
    coordinates = f"{start['longitude']:.5f},{start['latitude']:.5f};{finish['longitude']:.5f},{finish['latitude']:.5f}"
    identity = json.dumps([settings.OSRM_URLS[:2], coordinates])
    key = "route:v5:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    calls = 0
    global _last_activity
    _last_activity = time.monotonic()

    def load():
        nonlocal calls
        try:
            route, calls = _call_providers(coordinates, budget=budget)
        except NoRoute as exc:
            return {"no_route": str(exc)}  # Cached too: the answer will not change.
        digest = hashlib.sha256(route["coordinates"].tobytes() + route["segment_miles"].tobytes()).hexdigest()[:24]
        return {"cache_key": f"{key}:{digest}", **route}

    route, source = cache.get_or_load(key, load)
    if "no_route" in route:
        raise NoRoute(route["no_route"])
    return route, calls, source


def _warm_connections():
    for url in settings.OSRM_URLS:
        try:
            _session.head(f"{url}/", timeout=(settings.OSRM_CONNECT_TIMEOUT, 5))
        except requests.RequestException as exc:
            logger.info("Connection warm-up to %s failed: %s", url, exc)


def start_connection_warmer():
    """Keep HTTPS connections to the routing servers open while the API is in use.

    A new TLS connection to the public servers costs ~0.5-1 s, and they close
    idle connections after 1-2 minutes. While the API has been used in the last
    ``OSRM_KEEP_WARM_SECONDS`` (and right after startup), this sends ``HEAD /``
    to each server every ``OSRM_KEEPALIVE_INTERVAL`` seconds. That is not a
    routing request: OSRM answers it immediately without computing anything.
    When the API is idle it stops, so an unused server generates no traffic.
    """
    if settings.OSRM_KEEP_WARM_SECONDS <= 0:
        return

    def run():
        while True:
            if time.monotonic() - _last_activity <= settings.OSRM_KEEP_WARM_SECONDS:
                _warm_connections()
            time.sleep(settings.OSRM_KEEPALIVE_INTERVAL)

    threading.Thread(target=run, name="osrm-keep-warm", daemon=True).start()


def fetch_itinerary(start, finish, stops, max_calls=1, budget=None):
    """Route through selected station coordinates, with at most the remaining call budget.

    Cache includes waypoint order and providers. Stations with sourced
    coordinates must snap to a road within 100 metres; city-centre estimates
    snap to the nearest road at any distance (the snap is reported). A stop with
    a ``heading`` (exit positions, which lie on the motorway) snaps only to roads
    heading that way within 60 degrees.
    """

    points = [start, *stops, finish]
    if not stops or len(points) > 100:
        raise RoutingError("Itinerary must contain 1–98 fuel waypoints.")
    coordinates = ";".join(f"{p['longitude']:.6f},{p['latitude']:.6f}" for p in points)
    radiuses = ["unlimited", *("100" if is_sourced(stop) else "unlimited" for stop in stops), "unlimited"]
    bearings = ["", *(f"{round(s['heading']) % 360},60" if s.get("heading") is not None else "" for s in stops), ""]
    identity = json.dumps([settings.OSRM_URLS, coordinates, radiuses, bearings])
    key = "itinerary:v3:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    calls = 0

    def load():
        nonlocal calls
        route, calls = _call_providers(
            coordinates, max_calls=max_calls, budget=budget, radiuses=radiuses, bearings=bearings
        )
        digest = hashlib.sha256(json.dumps([route["leg_miles"], route["display_coordinates"]]).encode()).hexdigest()[
            :24
        ]
        return {"cache_key": f"{key}:{digest}", **route}

    route, source = cache.get_or_load(key, load)
    return route, calls, source
