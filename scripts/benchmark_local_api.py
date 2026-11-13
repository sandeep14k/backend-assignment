"""Network-free HTTP replay benchmark: cached routing with fresh/cached planning.

Run validate_submission.py --record first when station choices change. Public
service timings are measured separately by benchmark_live_api.py.
"""

import json
import os
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.test import Client, override_settings  # noqa: E402

from planner import cache, routing  # noqa: E402
from planner.data import warm  # noqa: E402
from scripts.validate_submission import verify  # noqa: E402


def main():
    records = json.loads((ROOT / "planner/tests/fixtures/routing/ny_miami.json").read_text())

    def replay(url, **kwargs):
        row = next(r for r in records if r["url"] == url and r["params"] == kwargs["params"])
        response = requests.Response()
        response.status_code = row["status"]
        response._content = row["body"].encode()
        response.headers.update(row["headers"])
        return response

    warm()
    client = Client()
    body = json.dumps({"start": "New York, NY", "finish": "Miami, FL"})
    result = {"network": False, "fixture": "ny_miami.json", "measurements": {}}
    original_cache = cache.get_or_load

    def fresh_planning(key, loader):
        return (
            original_cache(key, loader)
            if not any(stage in key for stage in (":adaptive:", ":final:"))
            else (loader(), "loaded")
        )

    isolated = {"routes": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bench"}}
    with (
        override_settings(CACHES=isolated),
        patch.object(routing._session, "get", side_effect=replay),
        patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")),
    ):
        cache.clear()
        response = client.post("/api/route/", body, content_type="application/json")
        assert response.status_code == 200, response.content
        for mode, loader in [("cached_route_fresh_plan", fresh_planning), ("cached_response", original_cache)]:
            samples, calls = [], []
            with patch("planner.cache.get_or_load", side_effect=loader):
                for _ in range(30):
                    started = time.perf_counter()
                    response = client.post("/api/route/", body, content_type="application/json")
                    samples.append((time.perf_counter() - started) * 1000)
                    assert response.status_code == 200, response.content
                    data = response.json()
                    verify(data)
                    calls.append(data["metadata"]["routing_calls"])
            samples.sort()
            result["measurements"][mode] = {
                "samples": len(samples),
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(samples[int(len(samples) * 0.95) - 1], 3),
                "max_ms": round(max(samples), 3),
                "response_bytes": len(response.content),
                "routing_calls": sum(calls),
                "failures": 0,
            }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
