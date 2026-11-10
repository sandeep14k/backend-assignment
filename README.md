# Fuel Route API

Django 6.1.3 API for US city-to-city driving routes, fuel purchase planning, and an interactive route map. Python 3.13+ required. The supplied assessment fuel-price CSV is included unchanged.

## Run

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py check
python manage.py test
python manage.py runserver
```

Open http://127.0.0.1:8000 for the map. No API key or database migration is required. Internet access is required for routing, Leaflet assets, and map tiles. Tests run offline.

## API

```sh
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"Chicago, IL","finish":"Los Angeles, CA"}'
```

Inputs use `City, ST`; lookup is offline and restricted to the US city dataset. Optional `initial_fuel_gallons` (0–50, default 50) and `corridor_miles` (0.1–25, default 5). City lookup ignores punctuation and case. Unknown fields are rejected. Identical start and finish is rejected.

The response contains distance, duration, GeoJSON route and fuel-stop markers, station identities and CSV prices, route mileage, purchase quantities, arrival/departure tank levels, per-stop receipts, total USD, and cache/timing metadata. The root page renders this GeoJSON using Leaflet/OpenStreetMap. JSON itself is usable directly in other GIS clients.

Status codes: 400 invalid input; 405 unsupported method; 415 wrong content type; 422 insufficient fuel/station coverage; 503 routing provider unavailable. Health check: `GET /health/`.

## Cost model and important accuracy limits

* Range: 500 miles; efficiency: 10 MPG; capacity: 50 US gallons.
* **The default starting tank is full. `total_cost_usd` is money spent on fuel purchased during the trip, excluding the unknown cost of starting fuel.** A trip shorter than 500 miles can therefore cost $0 in new purchases. Consumption is reported separately. Lower the initial fuel input to model a partially full tank; an empty tank requires a candidate station at the start.
* **The source CSV has no coordinates. The included fallback places stations at city centroids, not actual pumps. Recommendations are estimates, not a verified navigable itinerary.** Station address and coordinate precision are returned. Unmatched cities are excluded and counted. The city file itself can contain imperfect coordinates.
* To replace estimates, create `data/station_coordinates.csv` with columns `id,latitude,longitude`, using the CSV's OPIS Truckstop ID and independently verified WGS84 pump coordinates. Restart the server after updates. A header-only template is included. No fabricated exact station positions are supplied.
* Candidate stations lie within the configured straight-line corridor of the route. Projection examines eight nearest geometry vertices and adjacent segments; complex loops or sparse geometry can cause approximate placement. Coordinates are projected in a local planar approximation. Straight-line proximity does not guarantee road access, travel direction compatibility, or a short driving detour.
* Optimality applies to purchase cost on the **fixed candidate sequence with zero detour fuel**. This does not optimize over alternative roads or account for detour costs. Verified pump coordinates and routing through selected pumps are necessary for navigation-grade results. That extension requires an additional routing call and rechecking range after detours.
* Routes between US endpoints may cross Canada; no US-only-route constraint is claimed. Unconnected regions return a provider error or infeasibility. Static CSV prices are USD per US gallon and are not live prices.
* Duplicate OPIS IDs retain the first row. Co-located candidates retain the lowest price. Prices and monetary arithmetic use `Decimal`; optimization distances use floating point. Each receipt is rounded half up to cents; the total sums those receipts so displayed amounts reconcile.

## Routing and speed

One OSRM request provides the complete route geometry and per-segment road distances. There are no per-station network calls or request-time geocoding calls. OSRM route data is cached for 24 hours, so repeated endpoint pairs make zero routing calls even if initial fuel or corridor changes. The city and station inventories are loaded once per process. Spatial matching uses NumPy and a SciPy KD-tree; the fuel optimizer is linear in the number of ordered candidates, after sorting.

OSRM's public demo server is suitable for a small assessment demonstration; it has no availability guarantee and is not a production hosting service. Set `OSRM_URL` to a self-hosted compatible OSRM instance for sustained use. Connect/read timeouts are 4/20 seconds; retries are deliberately omitted to bound external calls. Cache is in-process, bounded to 128 entries: separate workers have separate caches, and simultaneous cold requests can duplicate upstream work. Use a shared cache and distributed per-route lock for larger deployments. No public exposure protections such as application rate limiting are included.

Map tiles and Leaflet assets are browser requests, separate from the API's single routing request. The public OSM tile service should only be used in accordance with its usage policy.

## Verification and demo

```sh
python manage.py test -v 2
python manage.py demo_route
```

Observed live Chicago–Los Angeles result: 2,024.379 route miles, 96 candidate stations, 10 fuel purchases, and $468.62 purchased during the trip. Cold request: 5,386 ms and one routing call; cached request: 151 ms and zero routing calls. These are local observations, not latency guarantees. The station loader excluded 123 unique IDs whose cities could not be matched. See the saved response for the complete assumptions.

The live command makes two requests in the same process, prints timing and cache metadata, and saves `docs/example-response.json`. The second request should report zero routing calls. Tests cover range boundaries, partial fuel purchases, insufficient coverage, monetary rounding, route projection, malformed input, API errors, provider caching, and dataset loading. Seventy-five generated optimizer cases are compared to an independent linear-programming solution.

Import `docs/postman_collection.json` into Postman; its `base_url` variable defaults to localhost. The recording outline is in `docs/LOOM_SCRIPT.md`.

## Structure

* `planner/data.py`: offline city lookup, station loading/deduplication, verified-coordinate overrides.
* `planner/routing.py`: OSRM adapter, timeouts, geometry validation and route cache.
* `planner/optimizer.py`: corridor matching and minimum-cost fuel purchase algorithm.
* `planner/views.py`: request validation, JSON API, map page.
* `planner/tests.py`: offline regression tests and independent optimization oracle.

The greedy rule is: at each station, retain enough fuel to reach the first strictly cheaper station within 500 miles. If none is reachable, fill up to capacity or buy only enough to finish. Buying extra before a reachable cheaper station cannot improve cost; when no cheaper station is reachable, shifting a later expensive purchase to the current station cannot increase cost. A monotonic stack finds the next cheaper station in O(n). Consecutive reachability is checked and infeasible trips fail rather than invent stops or exceed tank capacity.

## Configuration and submission

Environment variables: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (default 0), `DJANGO_ALLOWED_HOSTS` (comma-separated), `OSRM_URL`. For deployment set a secret key, restrict hosts, use HTTPS and a production WSGI server; Django's development server is for local demonstration.

The workspace initially inherited a Git repository rooted outside this project. No changes are made to that parent repository. To publish, create a dedicated repository from this directory, review the files, and push it to your own GitHub account. Record the walkthrough in Loom and submit the repository URL and video URL. Those external deliverables are not created by this project.

## Sources

* [Django official releases](https://www.djangoproject.com/download/) — 6.1.3 was listed as stable when built on November 11, 2026.
* [OSRM HTTP API](https://project-osrm.org/docs/v5.24.0/api/) and [demo server usage policy](https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy).
* [US Cities Database](https://github.com/kelvins/US-Cities-Database) — bundled offline city coordinates under the MIT license; see `data/CITIES-LICENSE`.
* [OpenStreetMap attribution](https://www.openstreetmap.org/copyright) and [tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
