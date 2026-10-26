"""Duration correction — `log(actual) = a[label, work_type] + b · log(estimate)`,
fitted with the log-estimate centred on one hour so the shared slope `b`
doesn't soak up one group's offset and leak it into every other group.

Exact observations come from "still working → done" check-ins; upper bounds
from "done" check-ins and closes inside the slot (the task took at most
that long), handled as censored: only a prediction above the bound is
penalised. A Gaussian prior shrinks `a` toward 0 and `b` toward 1, so with
little data the correction is the identity. Pure numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# One observation moves a group about a third of the way: (0.3² / (0.3² + 0.4²)).
SIGMA_A = 0.3
SIGMA_B = 0.2
# Residual spread in log-minutes; a ×1.5 error is about one sigma.
SIGMA_RESIDUAL = 0.4
MAX_MINUTES = 24 * 60
CENTRE = math.log(60)


@dataclass(frozen=True)
class Observation:
    estimate: float
    actual: float
    group: str
    upper_bound: bool = False
    weight: float = 1.0


def group_key(label: str | None, work_type: str | None, estimator: str | None = None) -> str:
    key = f"{label or '-'}|{work_type or '-'}"
    return f"{key}|{estimator}" if estimator else key


def fit(observations: list[Observation], *, max_iter: int = 50) -> dict:
    obs = [o for o in observations if o.estimate > 0 and o.actual > 0]
    groups = sorted({o.group for o in obs})
    params = {"a": {}, "b": 1.0, "centre": CENTRE, "n_exact": 0, "n_upper": 0}
    if not obs:
        return params
    index = {g: n for n, g in enumerate(groups)}
    k = len(groups) + 1
    X = np.zeros((len(obs), k))
    for r, o in enumerate(obs):
        X[r, index[o.group]] = 1.0
        X[r, -1] = math.log(o.estimate) - CENTRE
    y = np.array([math.log(o.actual) - CENTRE for o in obs])
    upper = np.array([o.upper_bound for o in obs])
    s = np.array([o.weight for o in obs]) / SIGMA_RESIDUAL**2
    mu0 = np.zeros(k)
    mu0[-1] = 1.0
    precision = np.diag([1 / SIGMA_A**2] * (k - 1) + [1 / SIGMA_B**2])
    theta = mu0.copy()
    for _ in range(max_iter):
        residual = X @ theta - y
        # Censored rows only count while the prediction exceeds the bound.
        live = ~upper | (residual > 0)
        Xl, sl = X[live], s[live]
        A = (Xl.T * sl) @ Xl + precision
        rhs = (Xl.T * sl) @ y[live] + precision @ mu0
        new = np.linalg.solve(A, rhs)
        if np.allclose(new, theta, atol=1e-10):
            theta = new
            break
        theta = new
    params["a"] = {g: float(theta[index[g]]) for g in groups}
    params["b"] = float(theta[-1])
    params["n_exact"] = int((~upper).sum())
    params["n_upper"] = int(upper.sum())
    return params


def corrected_minutes(params: dict | None, estimate: float, group: str) -> float:
    if not params or estimate <= 0 or not (params.get("n_exact") or params.get("n_upper")):
        return estimate
    a = float((params.get("a") or {}).get(group, 0.0))
    b = float(params.get("b", 1.0))
    centre = float(params.get("centre", CENTRE))
    return min(MAX_MINUTES, math.exp(centre + a + b * (math.log(estimate) - centre)))
