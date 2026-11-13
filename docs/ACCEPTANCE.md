# Acceptance checklist

Each line of `assignment.txt`, where it is implemented, and how it is checked.

| Assignment line | Implementation | Check |
|---|---|---|
| API taking start and finish within the USA | `views.py`, `data.py`, `geocoding.py` | Unit tests; acceptance cases for cities, aliases, coordinates, Toronto, Paris, Windsor ON, ambiguous and unknown cities |
| Return a map of the route | GeoJSON `map` + Leaflet page at `map_url` | Every successful acceptance case checks the route geometry, one marker per stop, and that `map_url` returns 200 |
| Optimal (cost-effective) fuel locations | `optimizer.py` exact dynamic program over all 6,626 priced US stations, placed by `data.py` / `exits.py` | 400 comparisons with an independent mixed-integer program, 100 LP-oracle comparisons and exhaustive station-subset search in unit tests; exit placement checked against 143 known positions |
| 500-mile range; multiple fuel-ups | 0–50 gallon tank constraint at every pump, including detour fuel | The acceptance runner re-simulates the tank stop by stop for all 19 successful trips |
| Total money spent at 10 MPG | Decimal prices, per-stop receipts, `total_cost_usd` | The acceptance runner re-adds every receipt and checks it against the CSV price |
| Use the supplied fuel prices | The CSV is the only price source; duplicate IDs use the mean | Each stop's price, coordinates and coordinate source are compared with the CSV inventory |
| Free map/routing API | OSRM (no key), Leaflet + OpenStreetMap tiles | Recorded real OSRM responses replayed exactly (URL and parameters) |
| Latest stable Django | Django 6.1.3 (`requirements.txt`) | `python manage.py check` in CI |
| Return quickly | One HTTP call, 5–20 ms of planning (dynamic program), startup-loaded index, two-level cache | `docs/live-api-benchmark.json` (new trip 0.4–1.2 s, 95%+ of it OSRM; cached about 1 ms) |
| One routing call ideal, 2–3 acceptable | One call per new trip, 0 when cached, 2 only on mirror fallback; `verify_stops=true` uses at most 3 | The acceptance runner asserts ≤ 1 call in default mode and ≤ 3 with `verify_stops` |

## Acceptance cases (`scripts/validate_submission.py`)

33 cases, all passing ([results](acceptance-results.json)):

- **19 successful trips**: Chicago–LA, Seattle–Boston, NY–Miami, Minneapolis–Dallas, Billings–Rapid City, Chicago–Houston, LA–Houston, Dallas–Denver, Seattle–Miami, Bangor–San Diego, two `verify_stops` trips, and seven short trips (aliases, coordinates, partial and full tanks).
- **5 expected 422s**: San Francisco → Las Vegas and Portland → Phoenix (the supplied CSV has only 8 California stations, leaving gaps over 500 miles; the error names the gap), Chicago–LA with an empty tank, Honolulu → Chicago (no road), and Anchorage → Fairbanks.
- **9 expected 400s**: non-US city, foreign coordinates, a point across the border from Detroit, empty start, identical endpoints, an ambiguous city, a tank of 51 or −1, and an unknown city.

```sh
python scripts/validate_submission.py           # offline: replays recorded OSRM responses
python scripts/validate_submission.py --record  # live: re-records them (low volume, 1 req/s)
```

## Interpretation choices

- **Starting fuel.** The brief does not specify the starting tank. If omitted, the car starts with just enough to reach the first station, and that fuel is priced at that station and included, so `total_cost_usd` covers all fuel for the trip. `initial_fuel_gallons` overrides this; fuel supplied that way is already owned and is not charged.
- **"Optimal mostly means cost effective."** The plan minimizes fuel spending, with a $0.50-per-stop cost so the planner does not add a stop to save a few cents. Reported costs are real fuel money only.
- **Station positions.** The CSV has no coordinates. 300 stations are matched to the station itself, and 3,242 are placed at the OpenStreetMap exit their address names (median error 0.26 mi against ground truth). The remaining 3,084 use their town and are labelled `city_centroid`. Detours are estimated at 1.3 × the straight-line offset each way, plus 0.5 mi of exit ramp, and charged to the tank. `verify_stops=true` replaces the estimates with real road legs; in live tests the two agree within about 0.5%.
