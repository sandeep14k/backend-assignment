# Recording outline (under 5 minutes)

Before recording: install dependencies, run the tests, start `python manage.py runserver`, import the Postman collection. Check the live routing service is reachable. Use a fresh server to demonstrate a cold cache. Do not describe city centroid positions as verified pumps.

0:00–0:30 — Explain the API: US start/finish cities, 500-mile range, 10 MPG, fuel prices from supplied CSV. Show the full-tank assumption; initial fuel cost is excluded.

0:30–1:30 — In Postman send Chicago to Los Angeles. Show distance, multiple fuel stops, purchases, total cost, GeoJSON, and routing_calls=1. Send again and show routing_calls=0 and elapsed_ms. Open the map in the browser and inspect a station marker.

1:30–2:10 — Send Chicago to Milwaukee: with a full tank no purchase is required. Send an invalid location and show 400. Explain the 422 response for a route without reachable station coverage and 503 for provider failure.

2:10–3:30 — Show `routing.py` (one OSRM call plus cache), `optimizer.py` (next-cheaper greedy rule, 50-gallon constraint, Decimal receipts), and `data.py` (provided CSV, offline lookup, duplicate IDs).

3:30–4:15 — Run `python manage.py test -v 2`. Point out the independent linear-programming comparison and boundary tests.

4:15–4:50 — State limitations: city centroid station locations, no detour routing, fixed-route optimality, static prices, public OSRM demo service. Show the verified-coordinate override template and explain what would be needed for navigation accuracy. Show the GitHub repository URL and finish.

After recording: share a viewable Loom URL and the GitHub repository URL in the assignment response. Check both links while signed out.
