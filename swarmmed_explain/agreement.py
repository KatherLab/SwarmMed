"""Agreement between two attribution maps of one case.

Two methods that explain the same prediction should rank the inputs in a similar order.
These functions measure that with the Spearman rank correlation and with the overlap of
the k highest-ranked elements. Low agreement does not say which map is right; it says
that the prediction is not explained consistently and should be read with care.

Only numpy is needed.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def rankdata(values: Any) -> np.ndarray:
    """Rank values from 1 to n, giving tied values the mean of their ranks.

    Args:
        values: Any array-like; it is flattened.

    Returns:
        A float array of ranks with the flattened shape.
    """
    array = np.asarray(values, dtype=np.float64).ravel()
    if array.size == 0:
        return np.empty(0)
    order = np.argsort(array, kind="stable")
    ordered = array[order]
    boundaries = np.flatnonzero(np.diff(ordered) != 0) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [array.size]))
    mean_ranks = (starts + ends + 1) / 2.0
    ranks = np.empty(array.size)
    ranks[order] = np.repeat(mean_ranks, ends - starts)
    return ranks


def _pair(a: Any, b: Any) -> tuple[np.ndarray, np.ndarray]:
    first = _to_numpy(a)
    second = _to_numpy(b)
    if first.shape != second.shape:
        raise ValueError(
            f"the maps have different shapes {first.shape} and {second.shape}; reduce "
            "them to the same shape first (for example sum over the channel axis)"
        )
    if first.size == 0:
        raise ValueError("the maps are empty")
    if not (np.all(np.isfinite(first)) and np.all(np.isfinite(second))):
        raise ValueError("the maps contain NaN or infinite values")
    return first.ravel(), second.ravel()


def _to_numpy(value: Any) -> np.ndarray:
    detach = getattr(value, "detach", None)
    if callable(detach):
        value = detach().cpu().numpy()
    return np.squeeze(np.asarray(value, dtype=np.float64))


def resolve_k(k: int | float | None, n: int) -> int:
    """Return the number of top elements to compare.

    Args:
        k: None for 10% of the elements, a fraction in (0, 1], or a count from 1 to
            ``n``.
        n: The number of elements in a map.

    Returns:
        A count between 1 and ``n``.

    Raises:
        ValueError: If ``k`` is outside these ranges.
    """
    if k is None:
        return max(1, math.ceil(0.1 * n))
    if isinstance(k, bool):
        raise ValueError("k must be a count or a fraction, not a boolean")
    if isinstance(k, int | np.integer):
        if not 1 <= int(k) <= n:
            raise ValueError(f"k={k} is outside 1..{n}")
        return int(k)
    if isinstance(k, float) and 0 < k <= 1:
        return max(1, math.ceil(k * n))
    raise ValueError(f"k must be None, a fraction in (0, 1] or a count, not {k!r}")


def _top_indices(values: np.ndarray, k: int) -> np.ndarray:
    # Sort by value, highest first; ties keep the lower index first, so results repeat.
    return np.lexsort((np.arange(values.size), -values))[:k]


def spearman(a: Any, b: Any, *, absolute: bool = False) -> float:
    """Spearman rank correlation between two maps of the same shape.

    Args:
        a: The first map (numpy array or torch tensor).
        b: The second map.
        absolute: Compare magnitudes (|value|) instead of signed values.

    Returns:
        The correlation in [-1, 1], or NaN when either map is constant.
    """
    first, second = _pair(a, b)
    if absolute:
        first, second = np.abs(first), np.abs(second)
    if first.size < 2:
        return math.nan
    ranks_a = rankdata(first)
    ranks_b = rankdata(second)
    ranks_a -= ranks_a.mean()
    ranks_b -= ranks_b.mean()
    denominator = math.sqrt(float((ranks_a**2).sum() * (ranks_b**2).sum()))
    if denominator == 0:
        return math.nan
    return float(np.clip((ranks_a * ranks_b).sum() / denominator, -1.0, 1.0))


def top_k_overlap(
    a: Any, b: Any, k: int | float | None = None, *, absolute: bool = False
) -> float:
    """Fraction of the k highest-ranked elements that two maps share.

    Args:
        a: The first map.
        b: The second map, same shape.
        k: See :func:`resolve_k` (default: 10% of the elements).
        absolute: Rank by magnitude instead of signed value.

    Returns:
        The overlap in [0, 1]; 1 means the same k elements rank highest in both maps.
    """
    first, second = _pair(a, b)
    if absolute:
        first, second = np.abs(first), np.abs(second)
    count = resolve_k(k, first.size)
    shared = np.intersect1d(_top_indices(first, count), _top_indices(second, count))
    return shared.size / count


def rank_agreement(
    a: Any, b: Any, *, k: int | float | None = None, absolute: bool = False
) -> dict[str, Any]:
    """Spearman correlation and top-k overlap between two maps, ready for ``details``.

    Args:
        a: The first map.
        b: The second map, same shape.
        k: See :func:`resolve_k` (default: 10% of the elements).
        absolute: Compare magnitudes instead of signed values.

    Returns:
        ``{"spearman": float or None, "top_k_overlap": float, "k": int}``; ``spearman``
        is None (not NaN) when a map is constant, so the dict can go into a bundle as it
        is.
    """
    first, second = _pair(a, b)
    rho = spearman(first, second, absolute=absolute)
    count = resolve_k(k, first.size)
    return {
        "spearman": rho if math.isfinite(rho) else None,
        "top_k_overlap": top_k_overlap(first, second, count, absolute=absolute),
        "k": count,
    }
