# Fuel Route Planner API

A Django API that takes a start and finish anywhere in the USA and returns the driving route on a map, the cheapest places to buy fuel along it, and the total fuel cost. Vehicle: 500-mile range, 10 MPG (50-gallon tank). Prices come from the supplied CSV.

Every plan is **physically drivable**:
- detours to off-road stations are driven and paid for;
- the tank never goes below 0 or above 50 gallons;
- `total_cost_usd` is the cost of all the fuel the trip burns.

```sh
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "Chicago, IL", "finish": "Los Angeles, CA"}'
```

```jsonc
{
  "map_url": "http://127.0.0.1:8000/map/?start=Chicago%2C+IL&finish=Los+Angeles%2C+CA",
  "distance_miles": 2024.4,
  "total_cost_usd": "632.65",
  "savings": {"price_blind_cost_usd": "661.78", "savings_usd": "29.13", "savings_percent": 4.4},
  "fuel_stops": [
    {"name": "Gas N Wash", "city": "Chicago", "state": "IL", "route_mile": 0.0, "detour_miles": 0.06,
     "price_per_gallon": "3.39900000", "gallons_on_arrival": 0.606, "gallons_purchased": 1.423,
     "gallons_on_departure": 2.029, "cost_usd": "4.84",
     "reason": "Bought just enough to reach Speedway #6809 (Franklin Park, IL) at mile 14, which is cheaper at $3.354/gal."},
    {"name": "QUIKTRIP #7208", "city": "Bellwood", "state": "IL", "route_mile": 14.166, "detour_miles": 2.12,
     "price_per_gallon": "3.18100000", "gallons_on_arrival": 0.503, "gallons_purchased": 21.647,
     "gallons_on_departure": 22.15, "cost_usd": "68.86",
     "reason": "Bought just enough to reach Kwik Star #1142 (Iowa City, IA) at mile 220, which is cheaper at $3.134/gal. Also buys 0.8 gal here so the stop at mile 220 can be skipped."}
    // ... 9 stops in total
  ],
  "stop_consolidation": {"road_optimal_plan_cost_usd": "648.67", "road_optimal_plan_stops": 18, "stops_removed": 9, "cost_change_usd": "-16.02"},
  "map": {"type": "FeatureCollection", "features": ["route line, start, finish, numbered fuel stops"]},
  "metadata": {"routing_calls": 1, "geocoding_calls": 0, "route_source": "loaded",
               "timing_ms": {"routing": 755.2, "matching": 4.9, "optimize": 0.9, "total": 761.2}}
}
```

Open `map_url` in a browser to see the route with numbered stops, the savings and a "why here" note for each stop. The full response is in [`docs/example-response.json`](docs/example-response.json).

## Quick start

Python 3.13+. No API keys, database or migrations needed.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py test               # 61 offline tests
python manage.py runserver          # http://127.0.0.1:8000
```

Postman: import [`docs/postman_collection.json`](docs/postman_collection.json). It has 8 requests with built-in checks: cold and cached runs, GET, coordinates, street addresses, a short trip, and error cases.

Other commands:
- `python manage.py demo_route`: one cold and one cached live request, with timings.
- `python manage.py audit_fuel_data`: how every CSV row is used.
- `python scripts/benchmark_routing_providers.py`: re-runs the routing API comparison.

## How the brief is met

| Requirement | Implementation |
|---|---|
| Start and finish within the USA | Cities in any common format, coordinates, or street addresses. Coordinates are checked against real US borders, not a bounding box. [Details](#locations) |
| Map of the route | `map` is GeoJSON (route, start, finish, numbered stops). `map_url` opens an interactive Leaflet map. |
| Optimal, cost-effective fuel stops | Purchases are exactly optimal for road fuel, verified against a linear-programming solver. Stops are then consolidated wherever that saves their detour fuel. [How](#how-the-fuel-plan-is-computed) |
| 500-mile range, multiple fuel-ups | Simulated against the real tank, detours included: it never goes below 0 or above 50 gallons (tested on 300 random trips). |
| Total money spent at 10 MPG | `total_cost_usd` is the cost of all the fuel the trip burns: road miles, detours to stations, and the fuel used to reach the first station. |
| Free map and routing API | OSRM for routing; Leaflet with OpenStreetMap tiles for the map. No keys. [Why](#choosing-the-routing-api) |
| Quick | 0.2–0.8 s for a new route (almost all of it OSRM's own computation). About **1 ms** for a repeated route. [How](#speed) |
| 1 routing call ideal, 2–3 acceptable | Exactly 1 per new route; 0 when cached, including after restarts and across workers on the host. Never more than 2. [Guarantees](#routing-call-budget) |
| Latest stable Django | Django 6.1.3. |

## Choosing the routing API

The brief asks for a free API that is fast and needs few calls. I tested every candidate on three long US routes (`scripts/benchmark_routing_providers.py`; each request unique so server-side caches don't flatter the numbers):

| Service | Key needed | Chicago → LA | NYC → Miami | Seattle → Boston | Verdict |
|---|---|---|---|---|---|
| **OSRM, `router.project-osrm.org`** (`polyline6`) | No | 448 ms | 213 ms | 412 ms | ✅ **Primary** |
| **OSRM, FOSSGIS mirror `routing.openstreetmap.de`** | No | 211 ms | 209 ms | 637 ms | ✅ **Fallback / hedge** |
| OSRM with GeoJSON + per-segment annotations | No | 670 ms | 638 ms | 2,138 ms | ❌ 2–5× slower, 7.5× larger |
| Valhalla (FOSSGIS) | No | — | — | — | ❌ Rejects routes over 1,500 km |
| BRouter (`brouter.de`) | No | — | — | — | ❌ Times out on long routes |
| OpenRouteService, GraphHopper, Mapbox, HERE, TomTom, Geoapify, Google | **Yes** | — | — | — | ❌ All return 401 / "API key required"; a reviewer could not run the project as-is |

Medians over a reused connection. A new connection adds 0.4–0.9 s, because the servers are in Europe; [Speed](#speed) explains how that is avoided.

**Why OSRM:** it's the only free engine with **no key, no route-length limit and sub-second continental routes**. It runs on OpenStreetMap road data, and two independent public servers share the same API, so one can stand in for the other.

**How it's called:** `overview=full&geometries=polyline6&steps=false&generate_hints=false`, with no `annotations`.
- Segment lengths are computed locally with the haversine formula, then rescaled to OSRM's exact road distance.
- `overview=simplified` was rejected: 32–41 points for a cross-country route, up to 10.8 miles off the road.

**Map display:** Leaflet 1.9.4 with OpenStreetMap tiles. Free, keyless, ~42 KB gzipped, pinned with SRI hashes. The `map` field is standard GeoJSON, so it also opens in QGIS or geojson.io.

## Speed

Live, on a fresh server (cache cleared, connections warm). Times are server-side (`metadata.timing_ms`):

| Route | Miles | Cost | Stops | New route | Repeat | Response (raw / gzip) |
|---|---|---|---|---|---|---|
| Chicago, IL → Los Angeles, CA | 2,024 | $632.65 | 9 | 761 ms | 1.0 ms | 54 / 16 KB |
| New York, NY → Miami, FL | 1,280 | $389.25 | 5 | 243 ms | 0.8 ms | 30 / 9 KB |
| Seattle, WA → Boston, MA | 3,039 | $990.64 | 10 | 470 ms | 1.1 ms | 84 / 26 KB |
| Dallas, TX → Atlanta, GA | 784 | $223.91 | 3 | 227 ms | 0.4 ms | 18 / 5 KB |
| Chicago, IL → Milwaukee, WI | 89 | $30.19 | 1 | 213 ms | 0.4 ms | 5 / 2 KB |

- **For a new route, everything except OSRM takes 3–7 ms:** station matching about 5 ms, the optimizer under 1 ms.
- **After a restart,** a route already on disk is served in about 5 ms with no routing call.
- **Street addresses** add a Census geocoder lookup the first time (both ends in parallel). For example, 1600 Pennsylvania Ave NW → 233 S Wacker Dr took 2.1 s cold and 4 ms repeated.

| Technique | Effect |
|---|---|
| **Warm HTTPS connections.** One pooled `requests.Session`. While the API is in use, `HEAD /` is sent every 45 s, because OSRM drops idle connections after 1–2 min. | Saves 0.4–0.9 s of DNS/TCP/TLS setup per new route. `HEAD /` computes nothing and stops after 15 idle minutes. |
| **Compact OSRM request** (`polyline6`, no annotations, steps or hints) | 2–5× faster and 7.5× smaller than GeoJSON with annotations |
| **Vectorized polyline decoding** (NumPy) | 16–27 ms → **0.9–1.7 ms** |
| **Douglas–Peucker simplification, one pass per depth level** | 19–35 ms → **9–16 ms**; output identical to the textbook version (tested) |
| **Station KD-tree built once at startup, in 3-D Earth coordinates** | Station matching 35 ms → **2–5 ms**; exact (tested against brute force) |
| **O(n) optimizer** (monotonic stack) plus consolidation | Under 1 ms |
| **Two-level cache** (in-process LRU, then a file cache shared by workers and kept across restarts) | Repeat requests take **about 1 ms** |
| **Hedged request:** the mirror is also asked if the primary hasn't answered in 1.5 s | Caps slow responses from the public server |
| **Data loaded at startup** (`config/wsgi.py`) and **gzip** responses | No first-request penalty; 3–4× smaller transfer |

## Routing call budget

- **New route: exactly 1 call.** The mirror is called only if the primary fails or takes longer than 1.5 s, so at most 2.
- **Repeated route: 0 calls,** including after a restart, because the cache is on disk. "No route" answers are cached too.
- **Simultaneous requests for the same new route: 1 call in total, across every worker process on the host.**
  - Threads in one process wait on a shared future.
  - Separate processes take a file lock next to the cache, then re-check it.
  - Tests check both: 8 threads, and 4 separate OS processes, each produce exactly one load.
  - With several hosts, point the `routes` cache at Redis and this holds per host.
- **Opening `map_url`: 0 calls.**
- **Cities and coordinates make no geocoding calls.** A street address makes one Census geocoder call the first time it's seen. So the worst case is 2 geocoding calls plus 1 routing call.
- **Cache lifetime:** both cache levels share one absolute expiry, 7 days after the route was fetched.

Every response reports `metadata.routing_calls` (0, 1 or 2), `metadata.geocoding_calls` (0–2) and `metadata.route_source` (`loaded`, `memory`, `disk` or `shared`).

## API

`POST /api/route/` with a JSON body, or `GET /api/route/?start=...&finish=...` with the same fields.

| Field | Type | Default | Notes |
|---|---|---|---|
| `start`, `finish` | string | required | A city, coordinates or a street address. See [Locations](#locations). |
| `initial_fuel_gallons` | number 0–50 | omitted | Omitted: the vehicle starts with just enough fuel to reach the first station, and that fuel is counted. Given: the tank holds this much at the start. |
| `corridor_miles` | number 0.1–25 | `5` | How far from the route a station may be. [Why 5](#fuel-price-data) |
| `min_stop_gallons` | number 0–20 | `5` | Convenience rule: purchases below this are folded into a neighbouring stop. `0` turns it off. |

Response fields:
- `total_cost_usd`: total cost of all fuel burned.
- `starting_fuel`: the fuel used to reach the first station, if any, priced at that station.
- `savings`: comparison with a price-blind driver.
- `stop_consolidation`: the road-optimal plan versus the final plan.
- `fuel_stops`: per stop, the station, price, route mile, `offset_miles`, `detour_miles`, real tank gallons on arrival / bought / on departure, cost, and `reason`.
- `fuel_burned_gallons`, `fuel_purchased_gallons`, `average_price_per_gallon` and `remaining_gallons`.
- `map` and `map_url`.
- `assumptions` and `metadata`.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Unknown, ambiguous or non-US place (with suggestions), bad number, unknown field, or start equals finish |
| 405 | — | Method other than GET or POST |
| 415 | `unsupported_media_type` | POST without `application/json` |
| 422 | `no_route` | No road connects the points (e.g. Hawaii to the mainland) |
| 422 | `insufficient_station_coverage` | No station within range somewhere on the route, or an explicitly given tank too empty to reach one |
| 503 | `routing_unavailable` | Both routing servers failed |

### Locations

| Input | Example | Resolved | Calls |
|---|---|---|---|
| City, state code or name | `Chicago, IL` · `Chicago IL` · `chicago, illinois` · `Chicago, IL 60606` · `Boston, MA, USA` | city coordinates | 0 |
| Common names and abbreviations | `NYC` · `New York City, NY` · `St. Louis MO` · `Ft. Worth, TX` | city coordinates | 0 |
| City alone, when only one state has it | `Albuquerque` | city coordinates | 0 |
| Coordinates | `41.88, -87.63` or `41.88 -87.63` | exact | 0 |
| Street address (starts with a house number) | `233 S Wacker Dr, Chicago, IL 60606` | exact, via the [US Census geocoder](https://geocoding.geo.census.gov/) (free, no key) | 1 the first time, then cached |

- **US check for coordinates.** Coordinates are tested against [Natural Earth](https://www.naturalearthdata.com/) 1:10m borders (public domain; `data/us_boundary.json`), not a bounding box.
  - Accepted: US land and territories, or the sea within 25 miles of them.
  - Rejected: any point inside Canada, Mexico, Cuba, the Bahamas, Russia or the British Virgin Islands. That includes Windsor, 1 mile from Detroit, and Toronto.
  - Of the 29,880 bundled US city coordinates, all but 8 border hamlets pass. Those 8 still resolve by name.
- **Matching.** Every resolved location carries `matched` (`city`, `coordinates` or `address`). If the geocoder doesn't know an address or is down, its city is used instead.
- **Errors give suggestions.** For example *"Did you mean Chicago, IL?"*, or *"'Kansas City' exists in 2 states (Kansas City, KS; Kansas City, MO)"*.

## How the fuel plan is computed

```
locations ─► OSRM route (1 call, cached) ─► stations along the route ─► road-optimal purchases ─► detours + reserve ─► consolidate stops ─► JSON + GeoJSON
```

1. **Stations along the route.**
   - Each route segment asks the station KD-tree for every station within `corridor + half the segment's length` of its midpoint. That is guaranteed to include all of them.
   - One vectorized projection then gives each station its route mile and its straight-line `offset_miles` from the road.
2. **The physical model.** These rules make every plan drivable:
   - **Detours.** Reaching a station costs `offset × 1.3` road miles each way. (1.3 is the typical ratio of road distance to straight-line distance in the US, which runs 1.2–1.4.) That fuel is bought at the stop and included in the cost.
   - **Reserve.** The tank keeps a reserve *R*, the largest one-way detour among the candidate stations. Purchases are planned with a usable tank of 50 − 2R gallons. The real tank then holds *planned + R* on the road, never less than 0 when arriving at a station, and never more than 50 when leaving it.
   - **Start.** By default the vehicle starts with just enough fuel to reach the first station on the route. That fuel is valued at that station's price and included in the total. On most routes the first station is in the start city itself.
3. **Road-optimal purchases**: exact and O(n).
   - *If a cheaper station is within range, buy just enough to reach it. Otherwise fill the tank, or buy just enough to finish.*
   - Why this is optimal: fuel bought before a reachable cheaper station could have been bought there for less. When nothing cheaper is reachable, every gallon needed before the next fill is cheapest here.
   - A monotonic stack finds each station's next cheaper station.
   - Tested against SciPy's HiGHS linear-programming solver on 100 random trips.
4. **Stop consolidation.** Every stop now costs detour fuel, so a stop's whole purchase moves to the previous stop (if the tank has room) or the next (if the tank can reach it without that fuel) **whenever that's cheaper overall**. Purchases under `min_stop_gallons` are also moved, for convenience. This repeats until nothing changes.
   - Without the convenience rule the result never costs more than the road-optimal plan (tested on 150 random trips).
   - Chicago → LA goes from 18 stops ($648.67) to **9 stops ($632.65)**.
   - The response reports both plans.
   - Including detours, finding the true optimum is a harder problem (each stop has a fixed cost). This step is a local improvement on an exact base, not a proof of global optimality.
5. **Why each stop.** Every stop has a one-sentence `reason` from the optimizer's own decision, for example *"Bought just enough to reach X at mile 578, which is cheaper at $2.899/gal"* or *"No cheaper station within range, so filled the tank."*
6. **Savings.** The plan is compared with a **price-blind driver** under the same model: same route, stations, detours and start. That driver stops only when the next station is out of reach, then fills up.

   | Route | Plan | Price-blind | Saved |
   |---|---|---|---|
   | Chicago → LA | $632.65 (9 stops) | $661.78 (5 stops) | **$29.13 (4.4%)** |
   | New York → Miami | $389.25 (5 stops) | $415.44 (3 stops) | **$26.19 (6.3%)** |
   | Seattle → Boston | $990.64 (10 stops) | $1,066.52 (7 stops) | **$75.88 (7.1%)** |
   | Dallas → Atlanta | $223.91 (3 stops) | $236.60 (2 stops) | **$12.69 (5.4%)** |

7. **Money.** Prices and receipts use `Decimal`, and each receipt is rounded half-up to cents. The total is the sum of the receipts (plus the starting fuel), so the numbers always add up.

## Fuel price data

`python manage.py audit_fuel_data` reports how the supplied CSV is used, and `planner/tests/test_data.py` checks it:

| | |
|---|---|
| Rows / unique OPIS station IDs | 8,151 / 6,738 |
| US stations located and loaded | **6,626 of 6,626 (100%)** |
| Skipped | 112 Canadian stations (AB, BC, MB, NB, NS, ON, QC, SK, YT): no US route reaches them |
| Stations listed more than once | 678; 597 with different prices (median spread $0.09, max $0.90). The **mean** of each station's listed prices is used. |
| Price range used | $2.687 – $6.399 per gallon (median $3.399), 48 states |
| Columns used | `OPIS Truckstop ID`, `Truckstop Name`, `Address`, `City`, `State`, `Retail Price`. (`Rack ID` is a wholesale supply reference, not needed.) |

- **Locations.** The CSV has no coordinates, so each station is placed at its city's coordinates from the bundled [US Cities Database](https://github.com/kelvins/US-Cities-Database) (MIT; `data/CITIES-LICENSE`).
  - Names match regardless of case or punctuation, and "St." / "Ft." / "Mt." match "Saint" / "Fort" / "Mount".
  - Cities listed under several counties resolve to the right county.
  - Five towns missing from the city file map to the adjacent city.
  - Verified pump coordinates can be added to `data/station_coordinates.csv`, and they override the city position.
- **Why the default corridor is 5 miles.** With detours priced in, the total barely changes between 3 and 10 miles, then rises: wider corridors bring longer detours and a bigger reserve.

  | Corridor | 2 mi | 3 mi | **5 mi** | 8 mi | 10 mi | 15 mi |
  |---|---|---|---|---|---|---|
  | Chicago → LA | $633.36 | $632.47 | **$632.65** | $632.02 | $632.21 | $632.90 |
  | Seattle → Boston | $997.96 | $991.89 | **$990.64** | $993.70 | $994.03 | $1,006.10 |

## Limitations

- **Station positions are city coordinates,** because the CSV has none. Detours are estimated from those positions with a 1.3 road factor, not routed. Pump coordinates plus one routing call through the chosen stops would make them exact.
- **Fixed route.** Stops are chosen along OSRM's fastest route.
- **Optimality.** Road-fuel purchases are exactly optimal. Once detours are included, the result is a feasible, locally improved plan rather than a proven global optimum.
- **Static prices** from the supplied CSV.
- **Public OSRM servers** have no uptime guarantee. Set `OSRM_URLS` to a self-hosted OSRM for production.

## Configuration

| Variable | Default |
|---|---|
| `OSRM_URLS` | `https://router.project-osrm.org,https://routing.openstreetmap.de/routed-car` (primary, then mirror) |
| `OSRM_HEDGE_AFTER_SECONDS` | `1.5` |
| `OSRM_KEEP_WARM_SECONDS` | `900` (keep connections warm for 15 min after the last request; `0` turns it off) |
| `ROUTE_CACHE_DIR` / `ROUTE_CACHE_SECONDS` | `.cache/routes` / 7 days (routes and geocoded addresses) |
| `GEOCODER_URL` | US Census one-line address geocoder |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` | development key (set it in production), `0`, `localhost,127.0.0.1,testserver` |

## Project layout

```
config/                 settings, URLs, WSGI (startup: load data, build KD-tree, warm connections)
planner/
  data.py               location parsing, US border check, CSV loading and cleaning, station KD-tree
  geo.py                polyline decoding, haversine, 3-D coordinates, Douglas-Peucker, point-in-polygon
  routing.py            OSRM client: pooled connections, hedging, fallback, keep-warm
  geocoding.py          street addresses via the US Census geocoder (cached, with city fallback)
  cache.py              two-level cache (memory + disk), single-flight across threads and processes
  optimizer.py          stations along the route, physical fuel model, optimal purchases, consolidation
  services.py           plan_trip(): runs the steps above and builds the response
  views.py              input validation, HTTP API, map page
  templates/            Leaflet map page
  management/commands/  demo_route, audit_fuel_data
  tests/                61 offline tests: LP check, tank simulation, brute-force and reference checks,
                        multi-process cache, border check
data/                   US cities (MIT), US borders (Natural Earth, public domain), station overrides
scripts/                routing provider benchmark
docs/                   Postman collection, example response, Loom outline
```

Development: `pip install -r requirements-dev.txt`, then `ruff check . && ruff format --check . && python manage.py test`. GitHub Actions runs the same checks on every push.
