# Loom outline (target 4:30, hard limit 5:00)

**Before recording**
- Restart `python manage.py runserver` so the cache is empty.
- Import `docs/postman_collection.json` into Postman.
- Open in an editor: `routing.py`, `optimizer.py`, `services.py`.
- Hide bookmarks and notifications.

---

**0:00–0:25 · The problem**
> "The API takes a start and finish in the USA and returns the route on a map, the cheapest places to fuel up with a 500-mile range, and the total fuel cost at 10 MPG. It's Django 6.1 and uses OSRM for routing, which is free and needs no key."

**0:25–1:50 · Postman demo**
1. **Request 1, Chicago → LA.** Point at:
   - `total_cost_usd` (~$621) and the 9 `fuel_stops`, each with price, gallons and cost;
   - `routing_calls: 1` and `timing_ms` (about 1 s, nearly all of it OSRM).
2. **Click `map_url`.** The browser shows the route with numbered stops. Click a marker.
3. **Request 2, same trip.** Point at `routing_calls: 0` and under 1 ms: the route is cached.
4. **Request 3 (GET, "St. Louis, Missouri") and request 4 (coordinates).** Inputs are resolved offline, so they cost no extra API calls.
5. **Request 6, Paris → 400.** Mention 422 (no station within 500 miles) and 503 (routing down).
6. Show the Postman **Test Results** tab: receipts add up to the total, and the tank never goes over 50 gallons.

**1:50–3:40 · Code tour**
1. `services.plan_trip`: "Four steps: route, stations on the route, optimizer, response."
2. `routing.py`:
   - "One OSRM call with compact polyline6 geometry. I benchmarked it: asking OSRM for segment distances was up to 4 s slower, so I compute them locally."
   - "The result is cached for 24 h. If OSRM fails, one retry goes to a mirror, so never more than 2 calls."
3. `optimizer.candidates`: "Every station is snapped onto the route with a KD-tree and vectorized NumPy, which gives each station a route mile."
4. `optimizer.optimize`:
   - "The rule: if a cheaper station is within range, buy just enough to reach it; otherwise fill up. That's provably optimal and O(n) using a monotonic stack."
   - "Purchases under 5 gallons are moved to a neighbouring stop. That cut Chicago–LA from 14 to 9 stops for 67 cents, and the response reports that cost."
5. `data.py`, briefly:
   - "The CSV has no coordinates, so each station is placed at its city's coordinates. Duplicate listings get their average price."
   - "Cities listed under several counties resolve to the right county."

**3:40–4:10 · Tests**
- Run `python manage.py test`: 27 tests, all offline.
- "The optimizer is checked against SciPy's linear-programming solver on 100 random routes. CI runs ruff and the tests on every push."

**4:10–4:30 · Limitations and close**
> "Stations sit at their city's coordinates, so a stop can be a few miles off the road, and detours aren't counted. Real pump coordinates plus one more routing call through the chosen stops would fix that. Thanks — the repo link is in the description."

---

**After recording:** check that the Loom and GitHub links both open while signed out.
