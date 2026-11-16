import math

import numpy as np
import pytest
import torch

from swarmmed_explain.agreement import (
    rank_agreement,
    rankdata,
    resolve_k,
    spearman,
    top_k_overlap,
)


def test_identical_maps_agree_fully():
    a = np.random.default_rng(0).normal(size=(4, 25))
    assert spearman(a, a) == pytest.approx(1.0)
    assert top_k_overlap(a, a) == 1.0
    assert rank_agreement(a, a.copy(), k=7) == {
        "spearman": pytest.approx(1.0),
        "top_k_overlap": 1.0,
        "k": 7,
    }


def test_anti_correlated_maps():
    a = np.random.default_rng(1).normal(size=100)
    b = -a
    assert spearman(a, b) == pytest.approx(-1.0)
    assert top_k_overlap(a, b, k=10) == 0.0
    # By magnitude the two maps rank the elements identically.
    assert spearman(a, b, absolute=True) == pytest.approx(1.0)
    assert top_k_overlap(a, b, k=10, absolute=True) == 1.0
    result = rank_agreement(a, b, k=0.1)
    assert result["spearman"] == pytest.approx(-1.0)
    assert result["top_k_overlap"] == 0.0
    assert result["k"] == 10


def test_known_spearman_value_and_ties():
    # Textbook example without ties: sum of squared rank differences 4, n = 5.
    assert spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) == pytest.approx(0.8)
    np.testing.assert_allclose(rankdata([10, 20, 20, 30]), [1, 2.5, 2.5, 4])
    np.testing.assert_allclose(rankdata([3, 3, 3]), [2, 2, 2])
    assert spearman([1, 2, 2, 3], [1, 2, 2, 3]) == pytest.approx(1.0)


def test_constant_maps_and_inputs():
    constant = np.zeros(10)
    other = np.arange(10.0)
    assert math.isnan(spearman(constant, other))
    assert rank_agreement(constant, other)["spearman"] is None
    assert rank_agreement(torch.arange(6.0), np.arange(6.0))[
        "spearman"
    ] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="shapes"):
        spearman(np.zeros((2, 3)), np.zeros((3, 2)))
    with pytest.raises(ValueError, match="NaN"):
        spearman([1.0, np.nan], [1.0, 2.0])


def test_resolve_k():
    assert resolve_k(None, 95) == 10
    assert resolve_k(None, 3) == 1
    assert resolve_k(0.25, 10) == 3
    assert resolve_k(4, 10) == 4
    with pytest.raises(ValueError):
        resolve_k(0, 10)
    with pytest.raises(ValueError):
        resolve_k(11, 10)
    with pytest.raises(ValueError):
        resolve_k(1.5, 10)
