import json
import math
import time
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from planner.data import location, stations
from planner.routing import fetch_route, RoutingError
from planner.optimizer import candidates, optimize, InfeasibleRoute


@require_GET
def index(request):
    return render(request, "planner/index.html")


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})


@csrf_exempt
def route(request):
    if request.method != "POST":
        response = JsonResponse({"error": "Use POST with a JSON body."}, status=405)
        response["Allow"] = "POST"
        return response
    if request.content_type != "application/json":
        return JsonResponse({"error": "Content-Type must be application/json."}, status=415)
    started = time.perf_counter()
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object.")
        unknown = set(body) - {"start", "finish", "initial_fuel_gallons", "corridor_miles"}
        if unknown:
            raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
        start, finish = location(body.get("start")), location(body.get("finish"))
        if start == finish:
            raise ValueError("Start and finish must differ.")
        initial = body.get("initial_fuel_gallons", 50)
        corridor = body.get("corridor_miles", 5)
        for name, value, minimum, maximum in [("initial_fuel_gallons", initial, 0, 50), ("corridor_miles", corridor, 0.1, 25)]:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be a finite number between {minimum} and {maximum}.")
        road, hit = fetch_route(start, finish)
        inventory, skipped = stations()
        nearby = candidates(road, inventory, corridor)
        plan = optimize(nearby, road["distance_miles"], initial)
        return JsonResponse({"start": start, "finish": finish, "distance_miles": round(road["distance_miles"], 3), "duration_seconds": round(road["duration_seconds"]), "map": {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"kind": "route"}, "geometry": road["geometry"]}] + [{"type": "Feature", "properties": stop, "geometry": {"type": "Point", "coordinates": [stop["longitude"], stop["latitude"]]}} for stop in plan["fuel_stops"]]}, **plan, "assumptions": {"range_miles": 500, "mpg": 10, "capacity_gallons": 50, "initial_fuel_gallons": initial, "initial_fuel_cost_included": False, "currency": "USD", "corridor_miles": corridor, "optimization": "Minimum purchase cost along the supplied route with zero detour cost; not a global route optimization.", "station_locations": "City centroids unless verified coordinates are supplied. Offsets are straight-line estimates; road access and detour fuel are not verified.", "prices": "Static assessment CSV prices, not live prices."}, "metadata": {"routing_calls": 0 if hit else 1, "route_cache_hit": hit, "candidate_stations": len(nearby), "stations_without_coordinates": skipped, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}})
    except (ValueError, UnicodeDecodeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except InfeasibleRoute as exc:
        return JsonResponse({"error": str(exc), "code": "insufficient_station_coverage"}, status=422)
    except RoutingError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
