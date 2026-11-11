"""Routing via the free, keyless OSRM HTTP API (see README "Choosing the routing API").

One uncached route = one HTTP request to OSRM. Everything that makes that fast:

* **Warm HTTPS connections.** A new TLS connection to the public servers costs
  ~0.5-1 s; a route over a reused one ~0.2-0.5 s. One shared ``requests.Session``
  pools connections, and ``start_connection_warmer()`` keeps them open while
  the API is in use (``HEAD /``, not a routing request).
* **Compact response.** ``overview=full&geometries=polyline6`` without
  ``annotations``/``steps``/``hints``: 4-7x less data than GeoJSON with
  per-segment annotations. Segment lengths are computed locally instead.
* **Hedged request.** If the primary has not answered within
  ``OSRM_HEDGE_AFTER_SECONDS``, the same request goes to an independent mirror
  and the first answer wins. A primary failure goes to the mirror at once.
  Either way, at most two routing calls.
* **Caching.** Routes (and "no route" answers) are cached in memory and on disk
  (``planner.cache``), and concurrent requests for the same new route share one call.
"""

import hashlib
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np
import requests
from django.conf import settings
from requests.adapters import HTTPAdapter

from planner import cache
from planner.geo import METERS_PER_MILE, decode_polyline, segment_miles, simplify

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = "fuel-route-planner/1.0 (+Django; assessment project)"
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0))
_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="osrm")
_last_activity = time.monotonic()  # Startup counts as activity.


class RoutingError(Exception):
    pass


class NoRoute(RoutingError):
    """The provider answered definitively that the points are not connected by road."""


def _request(base_url, coordinates):
    response = _session.get(
        f"{base_url}/route/v1/driving/{coordinates}",
        params={
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
    return payload["routes"][0]


def _call_providers(coordinates):
    """Return ``(raw_route, calls)`` from the fastest healthy provider; at most two calls."""
    urls = list(settings.OSRM_URLS)
    pending, calls, errors = set(), 0, []

    def launch():
        nonlocal calls
        url = urls.pop(0)
        calls += 1
        future = _executor.submit(_request, url, coordinates)
        future.url = url
        pending.add(future)

    launch()
    while pending:
        hedge = settings.OSRM_HEDGE_AFTER_SECONDS if urls else None
        done, _ = wait(pending, timeout=hedge, return_when=FIRST_COMPLETED)
        if not done:  # Primary is slow: hedge with the mirror.
            logger.info("Routing provider slow after %ss; hedging", hedge)
            launch()
            continue
        for future in done:
            pending.discard(future)
            try:
                return future.result(), calls
            except NoRoute:
                raise
            except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
                logger.warning("Routing provider %s failed: %s", future.url, exc)
                errors.append(exc)
        if not pending and urls:  # Primary failed fast: fall back immediately.
            launch()
    raise RoutingError("Routing service unavailable; please retry shortly.")


def _build_route(raw):
    try:
        points = decode_polyline(raw["geometry"])
        distance_miles = raw["distance"] / METERS_PER_MILE
        duration_seconds = float(raw["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RoutingError("Routing provider returned invalid data.") from exc
    if len(points) < 2:
        raise RoutingError("Routing provider returned an empty route.")

    # Planning runs on the ~50 m Douglas-Peucker simplification: 10-20x fewer
    # vertices than OSRM's full geometry, at an error far below the precision
    # of station positions. Segment lengths are rescaled so cumulative mileage
    # matches OSRM's road distance exactly.
    simplified = simplify(points)
    segments = segment_miles(simplified)
    if segments.sum() > 0:
        segments *= distance_miles / segments.sum()
    return {
        "coordinates": simplified,
        "segment_miles": segments,
        "distance_miles": distance_miles,
        "duration_seconds": duration_seconds,
        "display_coordinates": np.round(simplified, 5).tolist(),
    }


def fetch_route(start, finish):
    """Return ``(route, routing_calls, cache_source)``.

    ``routing_calls`` counts HTTP requests made to the routing provider by this
    call: 0 when served from cache, 1 normally, 2 only if the mirror was used.
    """
    coordinates = f"{start['longitude']:.5f},{start['latitude']:.5f};{finish['longitude']:.5f},{finish['latitude']:.5f}"
    key = "route:v4:" + hashlib.sha256(coordinates.encode()).hexdigest()[:32]
    calls = 0
    global _last_activity
    _last_activity = time.monotonic()

    def load():
        nonlocal calls
        try:
            raw, calls = _call_providers(coordinates)
        except NoRoute as exc:
            return {"no_route": str(exc)}  # Cached too: the answer will not change.
        return {"cache_key": key, **_build_route(raw)}

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
