# Loom outline (target 4:40, hard limit 5:00)

**Before recording**
- Run `python manage.py shell -c "from planner import cache; cache.clear()"` so request 1 is a genuinely new route.
- Start `python manage.py runserver`. At startup it loads the data and opens connections to OSRM.
- Import `docs/postman_collection.json` into Postman.
- Open in an editor: `services.py`, `routing.py`, `optimizer.py`, `README.md`.
- Hide notifications.

---

**0:00–0:20 · The problem**
> "Start and finish anywhere in the US. It returns the route on a map, the cheapest places to fuel with a 500-mile range, and the total cost at 10 MPG. Django 6.1, with OSRM for routing: free and no key."

**0:20–1:45 · Postman**
1. **Request 1, Chicago → LA.** Point at:
   - `total_cost_usd` (~$633) and the 9 `fuel_stops`, each with price, detour, real tank levels, cost and a `reason` (read one aloud);
   - `savings`: "$29 cheaper than a price-blind driver on the same route, with the same detours";
   - `routing_calls: 1` and `timing_ms` (about 0.2–0.8 s, nearly all OSRM; our own work is about 5 ms).
2. **Click `map_url`.** The browser shows the route with numbered stops. Click one.
3. **Request 2, same trip.** Point at `routing_calls: 0`, `route_source: memory` and under 1 ms.
4. **Requests 3–5.** Full state names, coordinates and real street addresses. Cities resolve offline; an address costs one cached Census geocoder call (`geocoding_calls`).
5. **Request 7, typo → 400 "Did you mean Chicago, IL?".** Mention 422 for no road / no stations and 503 when routing is down.
6. **Postman Test Results tab:** receipts add up to the total and the tank never goes over 50 gallons.

**1:45–2:30 · Why OSRM, and how few calls (README)**
- Show the "Choosing the routing API" table:
  > "I benchmarked every free option. Valhalla caps routes at 1,500 km, BRouter times out, and the rest need keys. OSRM is keyless, unlimited and sub-second, with a second public server as a fallback."
- Show "Routing call budget":
  > "Exactly 1 call for a new route and 0 for repeats, even after a restart, because the cache is on disk. Eight simultaneous identical requests share one call, and the tests check that."

**2:30–3:45 · Code**
1. `services.plan_trip`: "Route, stations along it, optimizer, response."
2. `routing.py`:
   - "One pooled HTTPS session that's kept warm, which saves almost a second of TLS setup to Europe."
   - "Compact polyline6, decoded with vectorized NumPy in 1 ms."
   - "If OSRM is slow, a hedged request goes to the mirror, so never more than 2 calls."
3. `optimizer.candidates`: "A station KD-tree built once at startup. Matching takes 2 ms and gives exactly the same result as brute force."
4. `optimizer.optimize`:
   - "If a cheaper station is within range, buy just enough to reach it; otherwise fill up. That's provably optimal, O(n), and checked against a linear-programming solver."
   - "Every plan is drivable: detours to stations are driven and paid for, a small reserve keeps the tank between 0 and 50, and a simulation test checks that on 300 random trips."
   - "Because each stop costs detour fuel, stops are consolidated when that's cheaper: Chicago–LA goes from 18 road-optimal stops to 9, saving $16."

**3:45–4:15 · Data and tests**
- Run `python manage.py audit_fuel_data`:
  > "All 6,626 US stations are used. The 112 skipped are Canadian. Duplicate listings get their mean price."
- Run `python manage.py test`: 61 offline tests, including the LP check, the tank simulation, brute-force checks, a multi-process cache test and the US border check.

**4:15–4:40 · Limitations and close**
> "Stations sit at their city's coordinates because the CSV has no coordinates, so detours aren't counted. Pump coordinates plus one extra routing call would fix that. Thanks — the repo link is below."

---

**After recording:** check that the Loom and GitHub links both open while signed out.
