# Fuel Route Planner API

A Django API that takes a start and finish anywhere in the USA and returns the driving route on a map, the cheapest places to buy fuel along it, and the total fuel cost. Vehicle: 500-mile range, 10 MPG (50-gallon tank). Prices come from the supplied CSV.

```sh
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "Chicago, IL", "finish": "Los Angeles, CA"}'
```

```jsonc
{
  "map_url": "http://127.0.0.1:8000/map/?start=Chicago%2C+IL&finish=Los+Angeles%2C+CA",
  "distance_miles": 2024.4,
  "total_cost_usd": "621.00",
  "fuel_stops": [
    {"name": "Gulf", "city": "Bensenville", "state": "IL", "route_mile": 0.0,
     "price_per_gallon": "3.07900000", "gallons_purchased": 32.318, "cost_usd": "99.51"},
    {"name": "KWIK STAR #932", "city": "Altoona", "state": "IA", "route_mile": 323.2,
     "price_per_gallon": "2.95900000", "gallons_purchased": 16.2, "cost_usd": "47.98"}
    // ... 9 stops in total
  ],
  "map": {"type": "FeatureCollection", "features": ["route line, start, finish, numbered fuel stops"]},
  "metadata": {"routing_calls": 1, "route_source": "loaded",
               "timing_ms": {"routing": 650.2, "matching": 5.6, "optimize": 0.2, "total": 656.1}}
}
```

Open `map_url` in a browser to see the route with numbered fuel stops. The full response is in [`docs/example-response.json`](docs/example-response.json).

## Quick start

Python 3.13+. No API keys, database or migrations needed.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py test               # 47 offline tests
python manage.py runserver          # http://127.0.0.1:8000
```

Postman: import [`docs/postman_collection.json`](docs/postman_collection.json). It has 8 requests with built-in checks: cold and cached runs, GET, coordinates, street addresses, a short trip, and error cases.

Other commands:
- `python manage.py demo_route`: one cold and one cached live request, with timings.
- `python manage.py audit_fuel_data`: how every CSV row is used.
- `python scripts/benchmark_routing_providers.py`: re-runs the routing API comparison below.

## How the brief is met

| Requirement | Implementation |
|---|---|
| Start and finish within the USA | Cities in any common format (`Chicago, IL`, `Chicago IL`, `Chicago, Illinois, USA`, `NYC`), coordinates, or street addresses. Cities and coordinates resolve offline; addresses use the free US Census geocoder. Typos and ambiguous names get suggestions. [Details](#locations) |
| Map of the route | `map` is GeoJSON (route, start, finish, numbered stops). `map_url` opens an interactive Leaflet map. |
| Optimal, cost-effective fuel stops | An exact minimum-cost plan. It matches a general linear-programming solver on 100 random trips. |
| 500-mile range, multiple fuel-ups | The tank never exceeds 50 gallons. Each purchase is sized for the stretch ahead. |
| Total money spent at 10 MPG | `total_cost_usd` covers all fuel for the trip from an empty tank. It is the sum of the per-stop receipts. |
| Free map and routing API | OSRM for routing; Leaflet with OpenStreetMap tiles for the map. No keys. [Why](#choosing-the-routing-api) |
| Quick | 0.2–0.8 s for a new route (almost all of it OSRM's own computation). **Under 1 ms** for a repeated route. [How](#speed) |
| 1 routing call ideal, 2–3 acceptable | Exactly 1 per new route; 0 when cached, including after restarts and across workers. Never more than 2. [Guarantees](#routing-call-budget) |
| Latest stable Django | Django 6.1.3. |

## Choosing the routing API

The brief asks for a free API that is fast and needs few calls. I tested every candidate on three long US routes (`scripts/benchmark_routing_providers.py`; each request unique so server-side caches don't flatter the numbers):

| Service | Key needed | Chicago → LA | NYC → Miami | Seattle → Boston | Verdict |
|---|---|---|---|---|---|
| **OSRM, `router.project-osrm.org`** (`polyline6`) | No | 448 ms | 213 ms | 412 ms | ✅ **Primary** |
| **OSRM, FOSSGIS mirror `routing.openstreetmap.de`** | No | 211 ms | 209 ms | 637 ms | ✅ **Fallback / hedge** |
| OSRM with GeoJSON + per-segment annotations | No | 670 ms | 638 ms | 2,138 ms | ❌ 2–5× slower, 7.5× larger (785 KB vs 105 KB) |
| Valhalla (FOSSGIS) | No | — | — | — | ❌ Rejects routes over 1,500 km |
| BRouter (`brouter.de`) | No | — | — | — | ❌ Times out on long routes ("killed by watchdog after 10 s") |
| OpenRouteService, GraphHopper, Mapbox, HERE, TomTom, Geoapify, Google | **Yes** | — | — | — | ❌ All return 401 / "API key required"; a reviewer could not run the project as-is |

Timings are medians over a reused connection (0.2–0.6 s). The first request on a new connection costs 0.6–1.1 s, because the servers are in Europe. See [Speed](#speed) for how that cost is avoided.

**Why OSRM:**
- It is the only free routing engine with **no API key, no route-length limit, and sub-second continental routes**.
- It runs on OpenStreetMap road data and is built for car routing.
- Two independent public servers run it. They use the same API, so one can stand in for the other.

**How it is called:** `overview=full&geometries=polyline6&steps=false&generate_hints=false`, with no `annotations`.
- Per-segment distances are computed locally with the haversine formula, then rescaled to OSRM's exact road distance. That's cheaper than asking OSRM for them.
- `overview=simplified` was rejected: it returns 32–41 points for a whole cross-country route. It strays **up to 10.8 miles** from the road, misses up to 39 stations and puts others up to 29 miles off.

**Map display: Leaflet 1.9.4 with OpenStreetMap raster tiles.**
- Free and keyless.
- ~42 KB of JavaScript (gzipped), pinned with SRI hashes.
- The API's `map` field is standard GeoJSON, so it also opens in QGIS, geojson.io or Mapbox GL.

## Speed

Live measurements, run in this order on a fresh server:

| Route | Miles | Cost | Stops | New route | Repeat | Response (raw / gzip) |
|---|---|---|---|---|---|---|
| Chicago, IL → Los Angeles, CA | 2,024 | $621.00 | 9 | 656 ms | 0.7 ms | 50 / 15 KB |
| New York, NY → Miami, FL | 1,280 | $377.35 | 5 | 635 ms | 0.4 ms | 27 / 8 KB |
| Seattle, WA → Boston, MA | 3,039 | $978.66 | 12 | 477 ms | 0.4 ms | 81 / 25 KB |
| Dallas, TX → Atlanta, GA | 784 | $217.52 | 3 | 421 ms | 0.3 ms | 16 / 5 KB |
| Chicago, IL → Milwaukee, WI | 89 | $27.33 | 1 | 209 ms | 0.6 ms | 4 / 1 KB |

Street addresses add a Census geocoder lookup on first use (both ends in parallel). White House → 233 S Wacker Dr, Chicago took 2.1 s cold and **4 ms** repeated. For a new route, everything except OSRM's computation takes **12–20 ms**. After a restart, a route already on disk is served in about 5 ms with no routing call. Times are server-side (`metadata.timing_ms`).

What makes it fast, with the measured effect of each:

| Technique | Effect |
|---|---|
| **Warm HTTPS connections.** One pooled `requests.Session`. While the API is in use, `HEAD /` is sent every 45 s, because OSRM drops idle connections after 1–2 min. | Saves 0.4–0.9 s of DNS/TCP/TLS setup per new route. `HEAD /` computes nothing and stops after 15 idle minutes. |
| **Compact OSRM request** (`polyline6`, no annotations, steps or hints) | 2–5× faster and 7.5× smaller than GeoJSON with annotations |
| **Vectorized polyline decoding** (NumPy, no per-character loop) | 16–27 ms → **0.9–1.7 ms** |
| **Douglas–Peucker simplification, one pass per depth level** (~22 passes, not ~2,000 calls) | 19–35 ms → **9–16 ms**. 22,700 → 1,800 points; output identical to the textbook version (tested). |
| **Station KD-tree built once at startup, in 3-D Earth coordinates.** Each route segment queries it by radius. | Station matching 35 ms → **1.5–2.7 ms**. Exact: a test compares it with brute force. |
| **O(n) optimizer** (monotonic stack) | 0.1–0.4 ms |
| **Two-level cache:** in-process LRU, then a file cache shared by all workers that survives restarts. Routes are kept 7 days and per-corridor station matches are cached too. | Repeat requests take **0.3–0.7 ms** |
| **Hedged request:** if the primary hasn't answered in 1.5 s, the mirror is asked too and the first answer wins | Caps slow responses from the public server |
| **Datasets loaded at startup** (`config/wsgi.py`) and **gzip** responses | No first-request penalty; 3–4× smaller transfer |

## Routing call budget

- **New route: exactly 1 call.** The provider is only called again if the primary fails, or is slower than 1.5 s, in which case the mirror is called too. So at most 2.
- **Repeated route: 0 calls.** This holds after a restart and in other worker processes too, because the cache is on disk. "No route" answers are cached as well.
- **Simultaneous requests for the same new route: 1 call in total.** The others wait for it and share the result. The test suite fires 8 such requests and checks there is 1 call.
- **Opening `map_url`: 0 calls.** The map page reuses the cached route.
- **No geocoding calls for cities or coordinates.** They resolve from the bundled offline dataset. A street address costs one call to the Census geocoder the first time it is seen (both ends in parallel), and is cached after that. So the worst case is 2 geocoding calls plus 1 routing call.

Every response reports `metadata.routing_calls` (0, 1 or 2), `metadata.geocoding_calls` (0–2) and `metadata.route_source` (`loaded`, `memory`, `disk` or `shared`).

## API

`POST /api/route/` with a JSON body, or `GET /api/route/?start=...&finish=...` with the same fields.

| Field | Type | Default | Notes |
|---|---|---|---|
| `start`, `finish` | string | required | A city, coordinates or a street address. See [Locations](#locations). |
| `initial_fuel_gallons` | number 0–50 | `0` | Fuel already in the tank. The default empty tank makes `total_cost_usd` the full trip cost. |
| `corridor_miles` | number 0.1–25 | `5` | How far from the route a station may be. [Why 5](#fuel-price-data) |

Response fields:
- `total_cost_usd`: total cost of the fuel bought.
- `fuel_stops`: per-stop station, price, route mile, distance from the road, gallons on arrival / bought / on departure, and cost.
- `fuel_consumed_gallons`, `fuel_purchased_gallons` and `average_price_per_gallon`.
- `small_stops_merged` and `merge_extra_cost_usd` (explained below).
- `map` (GeoJSON) and `map_url`.
- `assumptions`: the vehicle and pricing settings used.
- `metadata`: routing calls, route source, candidate count and per-step timings.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Unknown or ambiguous place (with suggestions), bad number, unknown field, or start equals finish |
| 405 | — | Method other than GET or POST |
| 415 | `unsupported_media_type` | POST without `application/json` |
| 422 | `no_route` | No road connects the points (e.g. Hawaii to the mainland) |
| 422 | `insufficient_station_coverage` | A stretch of more than 500 miles with no station |
| 503 | `routing_unavailable` | Both routing servers failed |

### Locations

| Input | Example | Resolved | Calls |
|---|---|---|---|
| City, state code or name | `Chicago, IL` · `Chicago IL` · `chicago, illinois` · `Chicago, IL 60606` · `Boston, MA, USA` | city coordinates | 0 |
| Common names and abbreviations | `NYC` · `New York City, NY` · `St. Louis MO` · `Ft. Worth, TX` | city coordinates | 0 |
| City alone, when only one state has it | `Albuquerque` | city coordinates | 0 |
| Coordinates | `41.88, -87.63` or `41.88 -87.63` (US only) | exact | 0 |
| Street address (starts with a house number) | `233 S Wacker Dr, Chicago, IL 60606` | exact, via the [US Census geocoder](https://geocoding.geo.census.gov/) (free, no key) | 1 the first time, then cached |

Every resolved location carries `matched`: `city`, `coordinates` or `address`.
- **Address fallback.** If the geocoder doesn't know an address or is down, its city is used and `matched` says so, so the request still succeeds.
- **Helpful errors.**
  - Ambiguous names list the options: *"'Kansas City' exists in 2 states (Kansas City, KS; Kansas City, MO). Please add the state."*
  - Typos get suggestions: *"Unknown US city: 'Chicgo, IL'. Did you mean Chicago, IL?"*
- **Why the Census geocoder:** it's official and keyless, and it handles US street addresses. Nominatim would need a contact email and limits to 1 request per second. Landmarks such as "Times Square" aren't in it; those resolve to their city.

## How the fuel plan is computed

```
cities (offline) ─► OSRM route (1 call, cached) ─► stations along the route ─► first fill ─► optimizer ─► merge tiny top-ups ─► JSON + GeoJSON
```

1. **Stations along the route.**
   - Each route segment asks the station KD-tree for every station within `corridor + half the segment's length` of its midpoint. That is guaranteed to include every station within the corridor.
   - Each pair is projected onto its segment in one vectorized pass, and every station keeps its closest segment.
   - That gives each station a route mile and a distance from the road. Co-located stations keep the cheapest.
2. **First fill.** The tank starts empty, so the trip begins at the cheapest station within 25 miles of the start (or the nearest within 100 miles if there is none).
3. **Optimizer**: exact and O(n).
   - *If a cheaper station is within range, buy just enough to reach it. Otherwise fill the tank, or buy just enough to finish.*
   - Why this is optimal: fuel bought before a reachable cheaper station could have been bought there for less. When nothing cheaper is reachable, every gallon needed before the next fill is cheapest here.
   - A monotonic stack finds each station's next cheaper station.
   - Tested against SciPy's HiGHS linear-programming solver on 100 random trips.
4. **Small top-ups.** The exact optimum sometimes buys 0.4 gallons at a station 10 miles after the previous one, to save a fraction of a cent.
   - Any purchase under 5 gallons moves to the previous stop (if the tank has room) or the next one (if the tank can reach it without that fuel), whichever is cheaper.
   - Chicago → LA goes from 13 stops to 9 for $0.67 more. The extra cost is reported as `merge_extra_cost_usd`.
5. **Money.** Prices and receipts use `Decimal`, and each receipt is rounded half-up to cents. The total is the sum of the receipts, so the numbers always add up.

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
  - Names match regardless of case or punctuation, and "St.", "Ft." and "Mt." match "Saint", "Fort" and "Mount".
  - Cities listed under several counties resolve to the right county. For example, Orlando is not placed 54 miles away in Brevard County.
  - Five station towns missing from the city file map to the adjacent city (e.g. Port Wentworth → Savannah, GA).
  - Verified pump coordinates can be added to `data/station_coordinates.csv` (`id,latitude,longitude`) and override city positions.
- **Why the default corridor is 5 miles.** Measured on the real routes:

  | Corridor | Chicago → LA | Seattle → Boston | Farthest stop from the road |
  |---|---|---|---|
  | 2 mi | $624.46 | $992.67 | 1.9 mi |
  | 5 mi | **$621.00** | **$978.66** | 4.1 mi |
  | 8 mi | $614.80 | $974.44 | 7.5 mi |

  Widening to 8 miles saves $4–6, but those stations are up to 7.5 miles off the road. The 15-mile round trip burns about 1.5 gallons (~$4.50), which cancels the saving. So 5 miles is the break-even point. `corridor_miles` lets callers choose otherwise.

## Limitations

- **Approximate station positions.** A stop can be a few miles from the highway, and the detour fuel is not counted. Pump coordinates plus one extra routing call through the chosen stops would fix this.
- **Fixed route.** Stops are chosen along OSRM's fastest route; other roads with cheaper fuel are not considered.
- **Static prices** from the supplied CSV.
- **Public OSRM servers** have no uptime guarantee. Set `OSRM_URLS` to a self-hosted OSRM in production, which also removes the network round trip to Europe.
- **The file cache is per host.** For several hosts, point the `routes` cache at Redis (a one-line settings change).

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
  data.py               offline city lookup, CSV loading and cleaning, station KD-tree
  geo.py                vectorized polyline decoding, haversine, 3-D coordinates, Douglas-Peucker
  routing.py            OSRM client: pooled connections, hedging, fallback, keep-warm
  geocoding.py          street addresses via the US Census geocoder (cached, with city fallback)
  cache.py              two-level cache (memory + disk) with single-flight loading
  optimizer.py          stations along the route, first fill, optimal purchases, small-stop merge
  services.py           plan_trip(): runs the steps above and builds the response
  views.py              input validation, HTTP API, map page
  templates/            Leaflet map page
  management/commands/  demo_route, audit_fuel_data
  tests/                47 offline tests: LP check, brute-force and reference-implementation checks, concurrency
scripts/                routing provider benchmark
docs/                   Postman collection, example response, Loom outline
```

Development: `pip install -r requirements-dev.txt`, then `ruff check . && ruff format --check . && python manage.py test`. GitHub Actions runs the same checks on every push.
