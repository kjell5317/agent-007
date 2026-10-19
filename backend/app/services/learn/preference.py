"""Slot-preference model — logistic regression, MAP under a Gaussian prior
centred on the rule-derived weights, with a Laplace posterior for Thompson
sampling. Pure numpy.

Rows are either pairwise (a user move: `x = φ(to) − φ(from)`, `y = 1`) or
pointwise (a check-in outcome: `x = φ(slot)` plus a bias, `y` = done).
Features absent from every row keep their prior exactly, so the posterior is
stored only for the active features.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.services.plan import features as F

BIAS = "bias"


@dataclass(frozen=True)
class Row:
    x: dict[str, float]
    y: float
    weight: float = 1.0


@dataclass
class Posterior:
    mean: dict[str, float]
    active: list[str] = field(default_factory=list)
    cov: list[list[float]] = field(default_factory=list)
    n_samples: int = 0

    def to_params(self) -> dict:
        return {"mean": self.mean, "active": self.active, "cov": self.cov}

    @classmethod
    def from_params(cls, params: dict, n_samples: int = 0) -> Posterior:
        return cls(
            mean=dict(params.get("mean") or {}),
            active=list(params.get("active") or []),
            cov=list(params.get("cov") or []),
            n_samples=n_samples,
        )


def prior() -> Posterior:
    return Posterior(mean=F.prior_mean())


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * z))


def _design(rows: list[Row], names: list[str]) -> np.ndarray:
    index = {name: n for n, name in enumerate(names)}
    X = np.zeros((len(rows), len(names)))
    for r, row in enumerate(rows):
        for name, value in row.x.items():
            if name in index:
                X[r, index[name]] = value
    return X


def fit(rows: list[Row], *, max_iter: int = 50, tol: float = 1e-8) -> Posterior:
    """MAP weights by Newton/IRLS; covariance = inverse Hessian at the MAP."""
    mean = F.prior_mean()
    mean[BIAS] = 0.0
    if not rows:
        return Posterior(mean=mean)
    active = sorted({name for row in rows for name, value in row.x.items() if value})
    active = [name for name in active if name in mean]
    if not active:
        return Posterior(mean=mean, n_samples=len(rows))
    X = _design(rows, active)
    y = np.array([row.y for row in rows])
    s = np.array([row.weight for row in rows])
    mu0 = np.array([mean[name] for name in active])
    precision = np.diag([1.0 / F.prior_sigma(name) ** 2 for name in active])
    w = mu0.copy()
    hessian = precision
    for _ in range(max_iter):
        p = _sigmoid(X @ w)
        grad = X.T @ (s * (p - y)) + precision @ (w - mu0)
        hessian = (X.T * (s * p * (1 - p))) @ X + precision
        step = np.linalg.solve(hessian, grad)
        w -= step
        if float(np.max(np.abs(step))) < tol:
            break
    hessian = (X.T * (s * _sigmoid(X @ w) * (1 - _sigmoid(X @ w)))) @ X + precision
    cov = np.linalg.inv(hessian)
    for name, value in zip(active, w):
        mean[name] = float(value)
    return Posterior(mean=mean, active=active, cov=cov.tolist(), n_samples=len(rows))


def log_likelihood(posterior: Posterior, rows: list[Row]) -> float:
    """Mean log-likelihood of `rows` under the MAP weights."""
    if not rows:
        return 0.0
    total = 0.0
    for row in rows:
        z = sum(value * posterior.mean.get(name, 0.0) for name, value in row.x.items())
        p = float(_sigmoid(np.array([z]))[0])
        p = min(max(p, 1e-12), 1 - 1e-12)
        total += row.weight * (np.log(p) if row.y else np.log(1 - p))
    return float(total / sum(row.weight for row in rows))


def draw(posterior: Posterior, rng: np.random.Generator) -> dict[str, float]:
    """One weight vector from the posterior: correlated noise on the active
    features, independent prior noise on the rest."""
    weights = dict(posterior.mean)
    active = set(posterior.active)
    for name in weights:
        if name not in active and name != BIAS:
            weights[name] += F.prior_sigma(name) * float(rng.standard_normal())
    if posterior.active:
        cov = np.array(posterior.cov)
        # Symmetrise and jitter: the inverse Hessian can lose positive
        # definiteness to rounding.
        cov = 0.5 * (cov + cov.T) + 1e-9 * np.eye(len(cov))
        sample = rng.multivariate_normal(
            [posterior.mean[name] for name in posterior.active], cov, method="cholesky"
        )
        for name, value in zip(posterior.active, sample):
            weights[name] = float(value)
    weights.pop(BIAS, None)
    return weights
