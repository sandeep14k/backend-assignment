"""Reference solver for tests: the fuel plan as a mixed-integer program (SciPy HiGHS).

Production uses ``planner.optimizer._solve_dp``, an exact dynamic program that is
25-100x faster; this independent formulation checks that it finds the optimum.
"""

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from planner.optimizer import CAPACITY_GALLONS, MPG, STOP_WEIGHT, InfeasibleRoute

SOLVER_SECONDS = 30.0


def solve_milp(miles, detours, prices, distance, initial):
    """Solve the visit-and-purchase MILP. Returns (purchases, visits, road fuel at each station and the end).

    Variables per station i: purchase q, binary visit z, road fuel f (before the
    station); f[n] is the fuel left at the destination. With one-way detour fuel
    e and road gap g to the next station (or the destination):
      f[0] = initial - miles[0]/MPG
      f[i+1] = f[i] + q[i] - 2 e[i] z[i] - g[i]      (fuel balance)
      q[i] <= 50 z[i]                               (buy only where you stop)
      f[i] - e[i] z[i] >= 0                         (never empty on arrival at the pump)
      f[i] - e[i] z[i] + q[i] <= 50                 (never over 50 when leaving it)
    Objective: sum price x q (+ STOP_WEIGHT per stop as a tie-break).
    """
    n = len(miles)
    q, z, f = np.arange(n), n + np.arange(n), 2 * n + np.arange(n)
    gaps = (np.append(miles[1:], distance) - miles) / MPG
    ones, inf = np.ones(n), np.full(n, np.inf)
    families = [  # (columns, coefficients, lower, upper): n rows each
        ([f + 1, f, q, z], [ones, -ones, -ones, 2 * detours], -gaps, -gaps),
        ([q, z], [ones, np.full(n, -CAPACITY_GALLONS)], -inf, np.zeros(n)),
        ([f, z], [ones, -detours], np.zeros(n), inf),
        ([f, q, z], [ones, ones, -detours], -inf, np.full(n, CAPACITY_GALLONS)),
    ]
    rows, cols, values, lower, upper = [], [], [], [], []
    for family, (columns, coefficients, lo, hi) in enumerate(families):
        for column, coefficient in zip(columns, coefficients, strict=True):
            rows.append(family * n + np.arange(n))
            cols.append(column)
            values.append(coefficient)
        lower.append(lo)
        upper.append(hi)
    start = initial - miles[0] / MPG
    rows.append(np.array([4 * n]))
    cols.append(np.array([f[0]]))
    values.append(np.ones(1))
    lower.append(np.array([start]))
    upper.append(np.array([start]))
    matrix = coo_matrix(
        (np.concatenate(values), (np.concatenate(rows), np.concatenate(cols))), shape=(4 * n + 1, 3 * n + 1)
    )

    objective = np.concatenate([prices, np.full(n, STOP_WEIGHT), np.zeros(n + 1)])
    upper_bounds = np.concatenate([np.full(n, CAPACITY_GALLONS), ones, np.full(n + 1, CAPACITY_GALLONS)])
    integrality = np.concatenate([np.zeros(n), ones, np.zeros(n + 1)])
    result = milp(
        objective,
        integrality=integrality,
        bounds=Bounds(np.zeros(3 * n + 1), upper_bounds),
        constraints=LinearConstraint(matrix.tocsr(), np.concatenate(lower), np.concatenate(upper)),
        options={"time_limit": SOLVER_SECONDS, "mip_rel_gap": 0.0},
    )
    if result.status == 2:
        raise InfeasibleRoute(
            "No drivable fuel plan: somewhere on the route the next station is beyond the 500-mile range. "
            "Try a larger corridor_miles."
        )
    if not result.success:
        raise TimeoutError("Reference MILP did not finish.")
    x = result.x
    return np.maximum(x[:n], 0.0), x[n : 2 * n] > 0.5, x[2 * n :]
