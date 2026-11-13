"""Compare exact greedy purchases to the generic MILP on identical on-route inputs."""

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from planner.optimizer import _solve_milp, _solve_on_route  # noqa: E402


def main():
    miles = np.arange(0, 2400, 200, dtype=float)
    prices = np.array([3.1, 3.0, 3.3, 2.9, 3.2, 3.0, 3.4, 3.1, 3.0, 3.2, 3.3, 3.1])
    cases = {
        "generic_milp": lambda: _solve_milp(miles, np.zeros(len(miles)), prices, 2500, 0),
        "linear_greedy": lambda: _solve_on_route(miles, prices, 2500, 0),
    }
    result = {"fixture": "12 on-route stations, 2500 miles; identical inputs", "results": {}}
    for name, solve in cases.items():
        solve()
        times = []
        for _ in range(100):
            started = time.perf_counter()
            purchases, _, _ = solve()
            times.append((time.perf_counter() - started) * 1000)
        result["results"][name] = {
            "median_ms": round(statistics.median(times), 4),
            "samples": len(times),
            "purchase_cost_usd": round(float(purchases @ prices), 6),
        }
    assert len({row["purchase_cost_usd"] for row in result["results"].values()}) == 1
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
