import json
import math
import time
from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from planner.data import location
from planner.optimizer import CAPACITY_GALLONS, InfeasibleRoute
from planner.routing import RoutingError
from planner.services import plan_trip

FIELDS = {"start", "finish", "initial_fuel_gallons", "corridor_miles"}


class UnsupportedMediaType(Exception):
    pass


NUMBER_LIMITS = {
    "initial_fuel_gallons": (0.0, CAPACITY_GALLONS, 0.0),
    "corridor_miles": (0.1, 25.0, 5.0),
}


def _number(data, name):
    minimum, maximum, default = NUMBER_LIMITS[name]
    value = data.get(name, default)
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(
            f"{name} must be a number between {minimum:g} and {maximum:g}."
        )
    return float(value)


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
        raise ValueError(
            f"Unknown fields: {', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(FIELDS))}."
        )
    start, finish = location(data.get("start")), location(data.get("finish"))
    if (start["latitude"], start["longitude"]) == (
        finish["latitude"],
        finish["longitude"],
    ):
        raise ValueError("Start and finish must be different places.")
    return (
        data,
        start,
        finish,
        _number(data, "initial_fuel_gallons"),
        _number(data, "corridor_miles"),
    )


def _error(message, status, code):
    return JsonResponse({"error": message, "code": code}, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def route(request):
    started = time.perf_counter()
    try:
        data, start, finish, initial, corridor = _parse(request)
        result = plan_trip(start, finish, initial, corridor)
    except UnsupportedMediaType as exc:
        return _error(str(exc), 415, "unsupported_media_type")
    except (ValueError, UnicodeDecodeError) as exc:
        return _error(str(exc), 400, "invalid_request")
    except InfeasibleRoute as exc:
        return _error(str(exc), 422, "insufficient_station_coverage")
    except RoutingError as exc:
        return _error(str(exc), 503, "routing_unavailable")

    query = {
        k: data[k]
        for k in ("start", "finish", "initial_fuel_gallons", "corridor_miles")
        if k in data
    }
    result["metadata"]["timing_ms"]["total"] = round(
        (time.perf_counter() - started) * 1000, 1
    )
    map_url = request.build_absolute_uri(f"{reverse('map')}?{urlencode(query)}")
    return JsonResponse({"map_url": map_url, **result})


@require_GET
def map_view(request):
    return render(request, "planner/index.html")


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})
