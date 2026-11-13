"""Small live API smoke benchmark; isolated cache, no background warm-up traffic.

Run: python scripts/benchmark_live_api.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

from planner import cache  # noqa: E402
from planner.data import warm  # noqa: E402


def main():
    warm()
    client, results = Client(), []
    with tempfile.TemporaryDirectory() as directory:
        caches = {"routes": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache", "LOCATION": directory}}
        with override_settings(CACHES=caches):
            cache.clear()
            for start, finish in [
                ("Chicago, IL", "Los Angeles, CA"),
                ("New York, NY", "Miami, FL"),
                ("Seattle, WA", "Boston, MA"),
            ]:
                time.sleep(1.1)
                for mode in ("new_route", "cached"):
                    started = time.perf_counter()
                    response = client.post(
                        "/api/route/", json.dumps({"start": start, "finish": finish}), content_type="application/json"
                    )
                    elapsed = (time.perf_counter() - started) * 1000
                    data = response.json()
                    results.append(
                        {
                            "start": start,
                            "finish": finish,
                            "mode": mode,
                            "status": response.status_code,
                            "http_stack_ms": round(elapsed, 3),
                            "metadata": data.get("metadata"),
                            "cost_usd": data.get("total_cost_usd"),
                            "error": data.get("error"),
                        }
                    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
