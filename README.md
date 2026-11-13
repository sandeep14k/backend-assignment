# Fuel Route Planner API

A Django API that takes a start and finish anywhere in the USA and returns the driving route on a map, the cheapest places to buy fuel along it, and the total fuel cost. The vehicle has a **500-mile range at 10 MPG** (a 50-gallon tank). Prices come from the supplied `fuel-prices-for-be-assessment.csv`.

- **One routing call per new trip**, zero for repeated trips (cached).
- **About 1–2 s for a new cross-country trip, about 1–2 ms cached** (measured below).
- **Minimum-cost fuel plan:** an exact mixed-integer program chooses where to stop and how many gallons to buy. It never lets the tank run dry or overfill, and it includes the fuel used to detour to each station.

## Quick start

Python 3.13+. No API keys, database or migrations.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py test          # 115 tests
python manage.py runserver
```

```sh
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "New York, NY", "finish": "Miami, FL"}'
```

Open the returned `map_url` in a browser for an interactive map with numbered fuel stops and the reason for each one. The same route and stops are in the JSON as GeoJSON (`map`). A full response is in [`docs/example-response.json`](docs/example-response.json). Postman: import [`docs/postman_collection.json`](docs/postman_collection.json) (13 requests, 89 assertions, all passing: [results](docs/newman-results.json)).

## How each requirement is met

| Requirement | How |
|---|---|
| Start and finish within the USA | `"Chicago, IL"`, `"Chicago Illinois"`, `"NYC"`, `"41.88, -87.63"`, or a street address. Cities and coordinates resolve offline. Non-US points are rejected using real borders (Windsor, ON, one mile from Detroit, is rejected). Typos get suggestions (`Chicgo, IL` → "Did you mean Chicago, IL?"). |
| Map of the route | GeoJSON `FeatureCollection` (route line, start, finish, numbered stops) plus `map_url`, a Leaflet page drawn with OpenStreetMap tiles. |
| Optimal, cost-effective fuel stops | All 6,626 priced US stations within 5 miles of the route (widening to 25 miles only where there is a gap) are candidates. A mixed-integer program minimizes fuel spending. See [How the plan is computed](#how-the-plan-is-computed). |
| 500-mile range, multiple fuel-ups | The tank is modelled as 0–50 gallons at every pump, including detour fuel. Long trips get as many stops as needed (New York → Los Angeles: 10). |
| Total money spent at 10 MPG | `total_cost_usd`, with a receipt for every stop (`gallons_purchased × price_per_gallon`). |
| Use the supplied fuel prices | The CSV is the only price source. Duplicate station IDs with different prices use their mean. |
| Free map and routing API | [OSRM](https://project-osrm.org/) (free, no key) for routing; Leaflet with OpenStreetMap tiles for the map. |
| Latest stable Django | Django 6.1.3. |
| Fast | One HTTP call, compact polyline response, KD-tree station search, vectorized geometry, and memory and disk caching. |
| Routing API called as little as possible | **1 call** for a new trip, **0** when cached, **at most 2** if the primary OSRM server fails and the mirror is used. `metadata.routing_calls` reports the count on every response. |

## API

`POST /api/route/` with a JSON body, or `GET /api/route/?start=…&finish=…`.

| Field | Default | Meaning |
|---|---|---|
| `start`, `finish` | required | US city, `"lat, lon"`, or street address (max 200 characters) |
| `initial_fuel_gallons` | automatic | Fuel already in the tank (0–50). If omitted, the car starts with just enough to reach the first station, and that fuel is priced into the total. |
| `corridor_miles` | 5 | How far from the route to look for stations (0.1–25) |
| `verify_stops` | `false` | `true` routes by road through every chosen station (one or two more calls) and recomputes purchases on the real road legs. |

Main response fields:

```jsonc
{
  "map_url": "http://…/map/?start=New+York%2C+NY&finish=Miami%2C+FL…",
  "distance_miles": 1280.3,
  "total_cost_usd": "381.48",
  "fuel_stops": [{
    "name": "…", "city": "…", "state": "…", "latitude": …, "longitude": …,
    "coordinate_precision": "osm_identity_match | operator_identity_match | exit_match | city_centroid",
    "price_per_gallon": "3.099", "route_mile": 210.4, "detour_miles": 2.9,
    "gallons_on_arrival": 0.0, "gallons_purchased": 22.8, "gallons_on_departure": 22.8,
    "cost_usd": "70.72", "reason": "Bought just enough to reach … which is cheaper at $2.999/gal."
  }],
  "fuel_burned_gallons": …, "fuel_purchased_gallons": …, "remaining_gallons": …,
  "savings": {"price_blind_cost_usd": "…", "savings_usd": "…"},   // against a driver who ignores prices
  "map": {"type": "FeatureCollection", "features": […]},
  "assumptions": {…},
  "metadata": {"routing_calls": 1, "route_cache_hit": false, "timing_ms": {…}}
}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Missing, unknown or invalid fields, a non-US location, an ambiguous city name, or start = finish |
| 415 | `unsupported_media_type` | POST without `application/json` |
| 422 | `no_route` | No road connects the points (e.g. Honolulu → Chicago) |
| 422 | `insufficient_station_coverage` | The supplied CSV has a gap longer than 500 miles on this route. The message says where. |
| 503 | `routing_unavailable` / `optimization_timeout` | OSRM is down, or the solver exceeded 5 s |

## How the plan is computed

1. **Route (1 HTTP call).** OSRM returns the route as a compact polyline. It is decoded with NumPy and simplified to about 50 m tolerance, and segment lengths are scaled to match OSRM's road distance exactly.
2. **Stations on the route.** A KD-tree over all stations, built once at startup, finds every station within `corridor_miles` of any route segment. Each station gets the route mile where it is reached and its straight-line distance from the route. If some stretch has no reachable station, only that stretch is searched again at 10, 15 and then 25 miles. This needs no extra routing calls.
3. **Optimize.** A sparse mixed-integer program (SciPy HiGHS) decides which stations to visit and how many gallons to buy at each. The constraints:
   - fuel balance between consecutive stations;
   - you can only buy where you stop;
   - you never reach a pump empty;
   - you never leave with more than 50 gallons.

   A detour to a station costs `1.3 × offset` road miles each way, a typical US road-to-straight-line ratio, plus 0.5 mile each way from an exit to its pumps for exit-placed stations. That fuel is drawn from the tank and counted in `fuel_burned_gallons`. The objective is fuel cost plus **$0.50 per stop**, so a stop must save at least 50¢ to be made. Without that, the strict minimum adds stops that each save only cents. Reported costs are real fuel money only. Stations at the same position keep only the cheapest, which halves the program without changing the answer.
4. **Respond.** The response includes receipts, tank levels on arrival and departure, a one-sentence reason for each stop, and the saving against a driver who ignores prices on the same stations (Chicago → Houston saves $23.77).

With `verify_stops=true`, step 4 first routes through the chosen stations and re-solves purchases on the real road legs. Exit waypoints are sent with the route's heading (OSRM `bearings`), so they snap to the carriageway going the right way; without that, a waypoint on the opposite carriageway forced U-turns that added 40 miles to Chicago → Houston. If a leg turns out longer than the range, one bridging station is inserted, still within 3 calls. In live tests the one-call estimate matched the road-verified result closely:

| Trip | One call (default) | Road-verified |
|---|---:|---:|
| Chicago → Houston | 1,086.9 mi, $330.01 | 1,088.3 mi, $330.04 |
| Dallas → Denver | 794.8 mi, $221.35 | 798.8 mi, $221.39 |
| New York → Miami | 1,280.3 mi, $381.48 | 1,281.6 mi, $379.02 |
| Seattle → Miami | 3,301.1 mi, $1,036.76 | 3,304.1 mi, $1,037.24 |
| Atlanta → Minneapolis | 1,132.2 mi, $330.70 | 1,133.5 mi, $331.00 |

## Station locations

The CSV gives names, prices and towns but **no coordinates**. Its addresses are mostly exit descriptions such as `I-44, EXIT 283 & US-69`. Each station gets the best position its evidence supports, and `coordinate_precision` says which:

| Precision | Stations | Source |
|---|---:|---|
| `osm_identity_match` / `operator_identity_match` | 300 | The station itself: an OpenStreetMap fuel station matched by brand plus store number or street address, or a Love's store page whose OPIS ID matches. |
| `exit_match` | 3,242 | The OpenStreetMap interstate exit named in the station's address. |
| `city_centroid` | 3,084 | The listed town ([US Cities Database](https://github.com/kelvins/US-Cities-Database), MIT). These addresses name no exit (`US-69`, `SR-29 & SR-55`) or their exit couldn't be matched unambiguously. |

**How exits are matched** (`planner/exits.py`). OpenStreetMap maps numbered exits as `motorway_junction` nodes on motorways whose `ref` names the highway (`I 44;OK 66`). A station is placed at an exit only if all of these hold:
- a highway in its address matches the exit's motorway;
- the exit number matches;
- the exit is in the station's state, within 20 miles of its town;
- all matching nodes, one per direction of travel, lie within 2 miles of each other.

Otherwise it stays at its town. 91% of exit-described addresses match.

**Accuracy, measured against ground truth.** 143 identity-matched stations also have exit addresses. Placed by their exit instead, the median error is **0.26 miles** and 89% fall within 1 mile ([`data/exit-report.json`](data/exit-report.json)). The one large disagreement exposed a wrong identity match: "AMPM, I-10, EXIT 140", Phoenix, had been name-matched to an ampm store 15 miles from I-10. When an identity match contradicts the exit its own address names by more than 3 miles, the exit now wins.

Using every station, with honest labels, matters. With only the 300 exactly located stations, most trips were impossible (Chicago → Houston, Los Angeles → Houston and Dallas → Denver all failed).

**Real gaps in the supplied data:** the CSV lists only **8 stations in all of California**. San Francisco → Las Vegas has no station in the first 537 miles, and Portland → Phoenix has a 1,005-mile gap through California. These return a 422 that names the gap; no fuel plan exists with this price list.

Other data facts: 8,151 rows; 6,738 unique IDs (6,626 US, 112 Canadian, which are excluded); 678 IDs listed more than once, 597 of them with conflicting prices, so the mean is used. Choosing the cheapest would understate costs. `python manage.py audit_fuel_data` reproduces these numbers.

The coordinate files are bundled, so running the app needs no downloads. To rebuild them, which is optional:

```sh
python scripts/download_exit_junctions.py   # OSM exits for 51 states via Overpass (resumable, about 40 min)
python manage.py locate_exits               # writes data/exit_coordinates.csv and data/exit-report.json
```

The identity matches come from `python manage.py locate_stations` (`planner/enrichment.py`, `scripts/download_station_extract.py`). OSM-derived positions are © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright); each row keeps its OSM node URL.

## Performance

Measured on 2026-11-13 against the public OSRM server with an empty cache ([`docs/live-api-benchmark.json`](docs/live-api-benchmark.json), `python scripts/benchmark_live_api.py`):

| Trip | Fuel cost | New trip | Cached | Routing calls |
|---|---:|---:|---:|---:|
| Chicago → Los Angeles | $625.68 | 1,470 ms | 1.2 ms | 1 |
| New York → Miami | $381.48 | 845 ms | 0.8 ms | 1 |
| Seattle → Boston | $979.95 | 1,177 ms | 1.7 ms | 1 |

Most of a new trip's time is the OSRM request. On a cached route, the offline stages (station matching and optimization) take about 140 ms median with no network ([`docs/local-api-benchmark.json`](docs/local-api-benchmark.json)).

What makes it fast:
- One shared HTTPS session.
- `polyline6` without steps or annotations: 4–7× less data than GeoJSON.
- Datasets, the KD-tree and SciPy loaded at startup (`config/wsgi.py`), not on the first request.
- Vectorized polyline decoding, simplification and corridor projection.
- Gzip responses (82 KB → 25 KB for New York → Los Angeles).
- A two-level cache: an in-process LRU plus a file cache shared by workers and kept across restarts. Concurrent requests for the same new trip share one OSRM call (thread futures plus a host file lock).

## Choosing the routing API

OSRM was chosen because it is free, needs no key, handles cross-country trips in one request, and returns the polyline and leg distances needed. Measured on 2026-11-13 ([`docs/provider-benchmark.txt`](docs/provider-benchmark.txt), `python scripts/benchmark_routing_providers.py`; median of 3 warm requests):

| Provider | Chicago → LA | NYC → Miami | Seattle → Boston | Result |
|---|---:|---:|---:|---|
| OSRM project, polyline6 | 657 ms | 655 ms | 1,285 ms | **Primary** |
| OSRM FOSSGIS, polyline6 | 1,001 ms | 618 ms | 1,058 ms | Automatic fallback |
| OSRM GeoJSON + annotations | 1,909 ms | 1,630 ms | 2,705 ms | 521–1,393 KB against 71–185 KB |
| Valhalla FOSSGIS demo | HTTP 400 | HTTP 400 | HTTP 400 | Public demo limits routes to 1,500 km |
| BRouter public | HTTP 400 | HTTP 400 | HTTP 403 | Failed |

OpenRouteService, GraphHopper, Mapbox, HERE, TomTom, Geoapify and Google all need an account and key; unauthenticated probes returned 401. They would work with a key, but would add sign-up steps for a reviewer. For production traffic, use a self-hosted OSRM or a paid provider: the public servers ask for at most one request per second, which the client enforces per process.

## Verification

```sh
ruff check . && ruff format --check .
python manage.py check
python manage.py test                       # 115 offline tests
python scripts/validate_submission.py       # 33 end-to-end cases, replaying recorded OSRM responses
npx newman run docs/postman_collection.json # 13 requests, 89 assertions (server running)
```

- **Unit tests** check the optimizer against an independent LP solver (100 random cases) and an exhaustive search over station subsets. They also run 300 random tank simulations and cover range boundaries, the per-stop cost, coverage-gap messages, malformed OSRM responses, the call budget, cache deduplication across threads and processes, input parsing and data provenance.
- **End-to-end acceptance** ([`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md), [results](docs/acceptance-results.json)) sends 33 real requests through Django, replaying recorded OSRM responses: 19 trips that should succeed (including Texas, California and the Southeast, and both modes), 5 expected 422s and 9 invalid inputs. For every successful trip, an independent checker re-simulates the tank stop by stop and re-adds every receipt.
- CI (`.github/workflows/ci.yml`) runs lint, formatting, Django checks and the tests.

## Configuration

| Variable | Default |
|---|---|
| `OSRM_URLS` | `https://router.project-osrm.org,https://routing.openstreetmap.de/routed-car` |
| `ROUTE_CACHE_DIR` / `ROUTE_CACHE_SECONDS` | `.cache/routes` / 604800 (7 days) |
| `GEOCODER_URL` | US Census one-line address geocoder (street addresses only) |
| `OSRM_KEEP_WARM_SECONDS` | `0` (off; enabling sends `HEAD` keep-alives) |
| `DJANGO_SECRET_KEY` / `DJANGO_DEBUG` / `DJANGO_ALLOWED_HOSTS` | development key / `0` / `localhost,127.0.0.1,testserver` |

## Code map

| File | Role |
|---|---|
| `planner/views.py` | Request parsing, validation, error codes |
| `planner/services.py` | Pipeline: route → stations → plan → response |
| `planner/routing.py` | OSRM client, call budget, fallback |
| `planner/optimizer.py` | Corridor search and the fuel optimizer |
| `planner/data.py` | CSV loading, city lookup, US borders, station positions and index |
| `planner/exits.py` | Places stations at the OSM exit their address names |
| `planner/geocoding.py` | Street addresses via the Census geocoder |
| `planner/geo.py` | Polyline decoding, simplification, distances |
| `planner/cache.py` | Two-level cache with single-flight loading |
| `planner/templates/planner/index.html` | Leaflet map page |

Street addresses add at most one Census geocoder call each, cached and reported in `metadata.geocoding_calls`. Map tiles are loaded by the browser and are not routing calls.
