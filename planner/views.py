import contextlib
import json
import math
import time
from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from planner.geocoding import resolve_pair
from planner.optimizer import CAPACITY_GALLONS, InfeasibleRoute, OptimizationTimeout
from planner.routing import NoRoute, RoutingError
from planner.services import plan_trip

FIELDS = {"start", "finish", "initial_fuel_gallons", "corridor_miles", "verify_stops"}


class UnsupportedMediaType(Exception):
    pass


NUMBER_LIMITS = {  # name: (minimum, maximum, default)
    "initial_fuel_gallons": (0.0, CAPACITY_GALLONS, None),  # None: just enough to reach the first station
    "corridor_miles": (0.1, 25.0, 5.0),
}


def _number(data, name):
    minimum, maximum, default = NUMBER_LIMITS[name]
    value = data.get(name, default)
    if value is None or value == "":
        if default is None:
            return None
        value = default
    if isinstance(value, str):  # Query-string values arrive as text.
        with contextlib.suppress(ValueError):
            value = float(value)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a number between {minimum:g} and {maximum:g}.")
    return float(value)


def _flag(data, name):
    value = data.get(name, False)
    if isinstance(value, str) and value.strip().lower() in {"", "0", "false", "no"}:
        return False
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}:
        return True
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be true or false.")


def _parse(request):
    """Read trip parameters from a JSON body (POST) or the query string (GET)."""
    if request.method == "GET":
        data = request.GET.dict()
    else:
        if request.content_type != "application/json":
            raise UnsupportedMediaType("Content-Type must be application/json.")
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
    unknown = set(data) - FIELDS
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(FIELDS))}.")
    options = {name: _number(data, name) for name in NUMBER_LIMITS}
    options["verify_stops"] = _flag(data, "verify_stops")
    start, finish, geocoding_calls = resolve_pair(data.get("start"), data.get("finish"))
    if (start["latitude"], start["longitude"]) == (finish["latitude"], finish["longitude"]):
        raise ValueError("Start and finish must be different places.")
    return data, start, finish, options, geocoding_calls


def _error(message, status, code):
    return JsonResponse({"error": message, "code": code}, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def route(request):
    started = time.perf_counter()
    try:
        data, start, finish, options, geocoding_calls = _parse(request)
        result = plan_trip(start, finish, **options)
    except UnsupportedMediaType as exc:
        return _error(str(exc), 415, "unsupported_media_type")
    except (ValueError, UnicodeDecodeError) as exc:
        return _error(str(exc), 400, "invalid_request")
    except InfeasibleRoute as exc:
        return _error(str(exc), 422, "insufficient_station_coverage")
    except NoRoute as exc:
        return _error(str(exc), 422, "no_route")
    except OptimizationTimeout as exc:
        return _error(str(exc), 503, "optimization_timeout")
    except RoutingError as exc:
        return _error(str(exc), 503, "routing_unavailable")

    query = {"start": data["start"], "finish": data["finish"]}
    query.update({key: value for key, value in options.items() if value is not None and value is not False})
    if options["verify_stops"]:
        query["verify_stops"] = "true"
    result["metadata"]["geocoding_calls"] = geocoding_calls
    result["metadata"]["timing_ms"]["total"] = round((time.perf_counter() - started) * 1000, 1)
    map_url = request.build_absolute_uri(f"{reverse('map')}?{urlencode(query)}")
    return JsonResponse({"map_url": map_url, **result})


@require_GET
def map_view(request):
    return render(request, "planner/index.html")


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})
