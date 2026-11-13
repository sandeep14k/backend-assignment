"""Compare the production dynamic program with the reference mixed-integer program.

Same inputs for both: a synthetic coast-to-coast route (2,800 miles, 350 stations
with detours, like New York -> Los Angeles). Both must reach the same optimum.
Writes docs/optimizer-benchmark.json.
"""

import json
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from planner.optimizer import STOP_WEIGHT, _solve_dp  # noqa: E402
from planner.tests.milp_oracle import solve_milp  # noqa: E402


def main():
    rng = np.random.default_rng(2026)
    distance, n = 2800.0, 350
    miles = np.sort(rng.uniform(0, distance - 1, n))
    detours = rng.choice([0.0, 0.05, 0.1, 0.3], n)  # One-way gallons (0-3 miles off the route).
    prices = np.round(rng.uniform(2.7, 4.2, n), 3)
    initial = float(miles[0] / 10 + detours[0])
    result = {"fixture": f"{n} stations with detours, {distance:.0f} miles; identical inputs", "results": {}}
    for name, solve, repeats in (("dynamic_program", _solve_dp, 30), ("reference_milp", solve_milp, 5)):
        times = []
        for _ in range(repeats):
            started = time.perf_counter()
            purchases, visits, _ = solve(miles, detours, prices, distance, initial)
            times.append((time.perf_counter() - started) * 1000)
        result["results"][name] = {
            "median_ms": round(statistics.median(times), 2),
            "samples": repeats,
            "objective": round(float(prices @ purchases + STOP_WEIGHT * visits.sum()), 6),
            "stops": int(visits.sum()),
        }
    objectives = [row["objective"] for row in result["results"].values()]
    assert abs(objectives[0] - objectives[1]) < 1e-4, objectives
    rows = result["results"]
    result["speedup"] = round(rows["reference_milp"]["median_ms"] / rows["dynamic_program"]["median_ms"], 1)
    text = json.dumps(result, indent=2)
    (ROOT / "docs/optimizer-benchmark.json").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
