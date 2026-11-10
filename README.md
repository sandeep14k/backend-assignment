# Fuel Route Planner API

A Django API that takes a start and finish anywhere in the USA and returns the driving route on a map, the cheapest places to buy fuel along it, and the total fuel cost. Vehicle: 500-mile range, 10 MPG (50-gallon tank). Fuel prices come from the supplied CSV.

```sh
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "Chicago, IL", "finish": "Los Angeles, CA"}'
```

```jsonc
{
  "map_url": "http://127.0.0.1:8000/map/?start=Chicago%2C+IL&finish=Los+Angeles%2C+CA",
  "distance_miles": 2024.4,
  "total_cost_usd": "621.04",
  "fuel_stops": [
    {"name": "Gulf", "city": "Bensenville", "state": "IL", "route_mile": 0.0,
     "price_per_gallon": "3.07900000", "gallons_purchased": 32.318, "cost_usd": "99.51"},
    {"name": "KWIK STAR #932", "city": "Altoona", "state": "IA", "route_mile": 323.177,
     "price_per_gallon": "2.95900000", "gallons_purchased": 16.215, "cost_usd": "47.98"}
    // ... 9 stops in total
  ],
  "map": {"type": "FeatureCollection", "features": ["route line, start, finish, numbered fuel stops"]},
  "metadata": {"routing_calls": 1, "timing_ms": {"routing": 1066.6, "matching": 40.1, "optimize": 0.5, "total": 1107.2}}
}
```

Open `map_url` in a browser to see the route with numbered fuel stops. The full response is in [`docs/example-response.json`](docs/example-response.json).

## Quick start

Python 3.13+. No API keys, database or migrations needed.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py test          # 27 offline tests
python manage.py runserver     # http://127.0.0.1:8000
```

Import [`docs/postman_collection.json`](docs/postman_collection.json) into Postman. It has 7 requests with built-in checks: cold and cached runs, GET, coordinates, a short trip, and error cases. `python manage.py demo_route` calls the live routing service twice and prints the timings.

## How the brief is met

| Requirement | Implementation |
|---|---|
| Start and finish within the USA | `"City, ST"`, `"City, State"` or `"lat, lon"`, resolved offline from a bundled city list of 29,880 rows. Coordinates outside the US are rejected. |
| Map of the route | `map` is a GeoJSON FeatureCollection (route, start, finish, stops). `map_url` opens an interactive Leaflet map. |
| Optimal, cost-effective fuel stops | An exact minimum-cost purchase plan along the route. It is checked against an independent linear-programming solver on 100 random cases. |
| 500-mile range, multiple fuel-ups | The tank never exceeds 50 gallons. Each purchase is sized for the next stretch, not just "fill up". |
| Total money spent at 10 MPG | `total_cost_usd` covers all fuel for the trip, starting from an empty tank. It is the sum of the per-stop receipts. |
| Free map/routing API | [OSRM](https://project-osrm.org/) (OpenStreetMap data) for routing; Leaflet and OpenStreetMap tiles for the map. No keys. |
| Quick | About 1 s for a cross-country request, almost all of it the routing call. Under 1 ms when cached. |
| Few routing calls (1 ideal) | One call per new route; 0 for repeated routes. A mirror is tried only if the primary fails, so at most 2. |
| Latest stable Django | Django 6.1.3. |

## API

`POST /api/route/` with a JSON body, or `GET /api/route/?start=...&finish=...` with the same fields.

| Field | Type | Default | Notes |
|---|---|---|---|
| `start`, `finish` | string | required | `"Chicago, IL"`, `"St. Louis, Missouri"` or `"41.88, -87.63"` |
| `initial_fuel_gallons` | number 0–50 | `0` | Fuel already in the tank. The default empty tank makes `total_cost_usd` the full trip cost. |
| `corridor_miles` | number 0.1–25 | `5` | How far from the route a station may be. |

Response fields:
- `total_cost_usd`: total cost of the fuel bought.
- `fuel_stops`: per-stop station, price, route mile, gallons on arrival / bought / on departure, and cost.
- `fuel_consumed_gallons`, `fuel_purchased_gallons` and `average_price_per_gallon`.
- `small_stops_merged` and `merge_extra_cost_usd` (explained below).
- `map`: GeoJSON of the route, start, finish and stops.
- `map_url`: link to the interactive map.
- `assumptions`: the vehicle and pricing settings used.
- `metadata`: routing calls, whether the route came from the cache, and per-step timings.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Unknown city, bad number, unknown field, or start equals finish |
| 405 | — | Method other than GET or POST |
| 415 | `unsupported_media_type` | POST without `application/json` |
| 422 | `insufficient_station_coverage` | A stretch of more than 500 miles with no station |
| 503 | `routing_unavailable` | Both routing servers failed |

## How it works

```
request ─► resolve cities offline ─► 1 OSRM call (cached 24 h) ─► stations along the route ─► optimizer ─► JSON + GeoJSON
```

1. **Routing** (`planner/routing.py`): one OSRM request with compact `polyline6` geometry. I benchmarked the options:
   - Asking OSRM for per-segment distances made the call 0.5–4 s slower and the payload 4× bigger. Segment lengths are computed locally with the haversine formula instead, then scaled to OSRM's road distance.
   - Valhalla's public server rejects routes over 1,500 km.
   - OpenRouteService, Mapbox and GraphHopper need API keys.

   The geometry sent to clients is simplified with Douglas–Peucker (2,000 km route: 22,700 → 1,800 points, ~50 KB). The optimizer always uses the full route.
2. **Stations on the route** (`planner/optimizer.py: candidates`): every station in the route's bounding box is snapped to its nearest route segment. A KD-tree finds the nearby points and the projection is vectorized with NumPy. Each station gets a route mile and a distance from the road. The result is cached per route.
3. **First fill**: the tank starts empty, so the trip begins at the cheapest station within 25 miles of the start (or the nearest one within 100 miles if there is none).
4. **Optimizer** (`optimize`): an exact greedy algorithm that runs in O(n):
   - *If a cheaper station is within range, buy just enough to reach it. Otherwise fill the tank, or buy just enough to finish.*
   - Why this is optimal: fuel bought before a reachable cheaper station could have been bought there for less. When nothing cheaper is reachable, every gallon needed before the next fill is cheapest here.
   - A monotonic stack finds each station's next cheaper station.
   - The tests compare the result with SciPy's linear-programming solver on 100 random routes.
5. **Small top-ups**: the exact optimum sometimes buys 0.4 gallons at a station 10 miles past the last one, to save a fraction of a cent. Any purchase under 5 gallons is moved to the previous stop (if the tank has room) or the next stop (if the tank can reach it without that fuel), whichever is cheaper. Chicago → LA goes from 14 stops to 9 for $0.67 more. The extra cost is reported as `merge_extra_cost_usd`.
6. **Money**: prices and receipts use `Decimal`, and each receipt is rounded half-up to cents. `total_cost_usd` is the sum of the receipts, so the numbers always add up.

## Performance

Measured locally against the public OSRM server:

| Route | Miles | Fuel cost | Stops | Cold | Cached |
|---|---|---|---|---|---|
| Chicago, IL → Los Angeles, CA | 2,024 | $621.04 | 9 | 1.1–2.2 s | 0.9 ms |
| New York, NY → Miami, FL | 1,280 | $377.35 | 5 | 1.1–1.3 s | < 1 ms |
| Seattle, WA → Boston, MA | 3,039 | $978.70 | 12 | 1.4–1.5 s | < 1 ms |
| Dallas, TX → Atlanta, GA | 784 | $217.52 | 3 | 0.9 s | < 1 ms |
| Chicago, IL → Milwaukee, WI | 89 | $27.33 | 1 | 0.6 s | < 1 ms |

When not cached, over 95% of the time is the OSRM round trip. The rest (station matching and the optimizer) takes under 45 ms. Datasets load once when the server starts (~70 ms). Responses are gzip-compressed (Chicago → LA is about 17 KB on the wire).

## Data handling

- **Station locations:** the price CSV has addresses but no coordinates. Each station is placed at its city's coordinates from the bundled [US Cities Database](https://github.com/kelvins/US-Cities-Database) (MIT, see `data/CITIES-LICENSE`). Verified pump coordinates can be added to `data/station_coordinates.csv` (`id,latitude,longitude`), and they override the city position.
- **Unmatched stations:** 112 stations can't be placed. They are all in Canada, which no US route reaches.
- **Duplicate listings:** 597 truck stops appear several times with different prices. They get the mean of their listed prices.
- **Cities in several counties:** these resolve to the county the city is actually in. For example, Orlando is not placed in Brevard County.
- **Abbreviations:** "St.", "Ft." and "Mt." match "Saint", "Fort" and "Mount".

## Limitations

- **Approximate station positions.** A station may be up to `corridor_miles` from the road. The detour to reach it is not routed and its fuel is not counted. Pump coordinates plus one extra routing call through the chosen stops would fix this.
- **Fixed route.** The optimizer picks stops along OSRM's fastest route; it doesn't look at other roads with cheaper fuel.
- **Static prices** from the supplied CSV.
- **Public OSRM server.** It has no uptime guarantee; set `OSRM_URLS` to a self-hosted OSRM for production.
- **Per-process cache.** The cache is in memory; use Redis to share it across workers.

## Configuration

| Variable | Default |
|---|---|
| `OSRM_URLS` | `https://router.project-osrm.org,https://routing.openstreetmap.de/routed-car` (primary, fallback) |
| `DJANGO_SECRET_KEY` | development key: set it in production |
| `DJANGO_DEBUG` | `0` |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,testserver` |

## Project layout

```
config/            Django settings, URLs, WSGI (loads datasets at startup)
planner/
  data.py          offline city lookup, station loading and cleaning
  routing.py       OSRM client, polyline decoding, simplification, cache, mirror fallback
  optimizer.py     stations along the route, first fill, optimal purchases, small-stop merge
  services.py      plan_trip(): runs the steps above and builds the response
  views.py         input validation, HTTP API, map page
  templates/       Leaflet map page
  tests.py         27 offline tests, including the linear-programming check
docs/              Postman collection, example response, Loom outline
```

Development: `pip install -r requirements-dev.txt`, then `ruff check . && ruff format --check . && python manage.py test`. GitHub Actions runs the same checks on every push.
