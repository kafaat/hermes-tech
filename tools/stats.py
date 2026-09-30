"""Exact binomial bounds for acceptance (method named in policies/acceptance_policy.json).

upper_bound(k, n, confidence) is the ONE-SIDED exact (Clopper-Pearson) upper confidence bound on a binomial
proportion: the largest p with P(X <= k; n, p) >= 1 - confidence. For k = 0 it has the closed form
1 - (1 - confidence) ** (1 / n); "0 misses in 50" gives 5.8% at one-sided 95%.
It is NOT the upper end of a two-sided 95% interval; that end equals the one-sided 97.5% bound (7.1% for 0/50).
The acceptance policy runs every attempt at 1 - 0.05 / max_attempts so that 95% holds for the whole path.
"""
from math import comb

METHOD = "one-sided exact Clopper-Pearson upper bound"


def binom_cdf(k: int, n: int, p: float) -> float:
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def upper_bound(k: int, n: int, confidence: float = 0.95) -> float:
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    alpha = 1 - confidence
    if k == 0:
        return 1 - alpha ** (1 / n)
    lo, hi = k / n, 1.0
    for _ in range(80):                       # P(X <= k; p) is decreasing in p
        mid = (lo + hi) / 2
        if binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def min_n_for_zero_misses(target: float, confidence: float = 0.95) -> int:
    n = 1
    while upper_bound(0, n, confidence) > target:
        n += 1
    return n


def acceptance_probability_zero_misses(true_rate: float, n: int, attempts: int) -> float:
    """Chance that a system missing `true_rate` of complaints passes a zero-miss attempt at least once in
    `attempts` independent tries: the path-level error the policy must keep at or below 1 - path confidence."""
    one = (1 - true_rate) ** n
    return 1 - (1 - one) ** attempts
