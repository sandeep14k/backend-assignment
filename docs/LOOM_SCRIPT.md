# Loom outline (about 4 minutes)

Run `python manage.py runserver`, import `docs/postman_collection.json`. Quote the numbers on screen.

1. **The request (0:00–1:00).** Send request 1 (New York → Miami). Show `total_cost_usd`, the fuel stops (price, gallons, receipt, `reason`), and `metadata.routing_calls = 1`. Mention the 500-mile range, 10 MPG and 50-gallon tank.
2. **Cache (1:00–1:20).** Send request 2: same answer, 0 routing calls, about 1 ms.
3. **Map (1:20–1:50).** Open `map_url`: route, numbered stops, and the reason for each.
4. **Coverage and errors (1:50–2:30).** Send Chicago → Houston: most stops are `exit_match`, placed at the interstate exit named in the CSV address. Then show the typo suggestion (request 7), and San Francisco → Las Vegas: a 422 that names the 537-mile gap in the supplied price list (it has only 8 California stations).
5. **Code (2:30–3:40).** `views.py` validation → `services.py` pipeline → `routing.py` (one compact OSRM call, cache) → `optimizer.py` (KD-tree corridor search, mixed-integer program with tank and detour constraints, $0.50 per-stop cost). Mention `verify_stops=true` for road-verified legs.
6. **Evidence (3:40–4:00).** README: provider comparison, benchmarks, 115 tests, 33 acceptance cases, and the GitHub link.
