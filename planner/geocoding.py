"""Street-address geocoding with the US Census Bureau geocoder (free, keyless, official).

Only inputs that start with a house number ("233 S Wacker Dr, Chicago, IL") are
sent; cities and coordinates resolve offline (``planner.data.location``). Results,
including "not found", are cached like routes, so a given address costs one call
ever. If the geocoder is down or does not know the address, the address's city
is used instead and ``matched`` says so.
"""

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

from planner import cache
from planner.data import _point, location, state_codes

logger = logging.getLogger(__name__)

STREET_ADDRESS = re.compile(r"^\s*\d+[A-Za-z]?(-\d+)?\s+[A-Za-z]")
_session = requests.Session()
_session.headers["User-Agent"] = "fuel-route-planner/1.0 (+Django; assessment project)"
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="geocode")


class GeocoderUnavailable(Exception):
    pass


def _display(address):
    """'1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500' -> '1600 Pennsylvania Ave NW, Washington, DC, 20500'."""
    upper = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", *state_codes().values()}
    return re.sub(r"[A-Za-z]+", lambda m: m[0].upper() if m[0].upper() in upper else m[0].capitalize(), address)


def _census(address):
    try:
        response = _session.get(
            settings.GEOCODER_URL,
            params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        matches = response.json()["result"]["addressMatches"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise GeocoderUnavailable(str(exc)) from exc
    if not matches:
        return {"not_found": True}
    best = matches[0]
    return {"address": best["matchedAddress"], "x": best["coordinates"]["x"], "y": best["coordinates"]["y"]}


def resolve(value):
    """Return ``(location, geocoding_calls)`` for one user-supplied location."""
    if not (isinstance(value, str) and STREET_ADDRESS.match(value)):
        return location(value), 0
    key = "geocode:v1:" + hashlib.sha256(" ".join(value.lower().split()).encode()).hexdigest()[:32]
    try:
        found, source = cache.get_or_load(key, lambda: _census(value))
    except GeocoderUnavailable as exc:
        logger.warning("Geocoder unavailable, using the address's city: %s", exc)
        return {**location(value), "matched": "city (geocoder unavailable)"}, 1
    calls = 1 if source == "loaded" else 0
    if found.get("not_found"):
        return {**location(value), "matched": "city (address not found)"}, calls
    point = _point(found["y"], found["x"], "address")
    return {**point, "name": _display(found["address"])}, calls


def resolve_pair(start, finish):
    """Resolve both ends, geocoding in parallel when both are street addresses."""
    first, second = _executor.submit(resolve, start), _executor.submit(resolve, finish)
    (start_point, start_calls), (finish_point, finish_calls) = first.result(), second.result()
    return start_point, finish_point, start_calls + finish_calls
