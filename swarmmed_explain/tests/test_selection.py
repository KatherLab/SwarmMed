import numpy as np
import pytest
import torch

from swarmmed_explain.selection import binary_outcome, select_cases

LABELS = np.array([1, 1, 1, 0, 0, 0, 1, 0, 1, 0])
SCORES = np.array([0.9, 0.6, 0.2, 0.8, 0.3, 0.1, 0.4, 0.55, 0.95, 0.05])


def test_binary_selection_picks_the_most_confident_case_of_each_outcome():
    selected = select_cases(LABELS, SCORES, n_per_outcome=2)
    assert [case.index for case in selected] == [8, 0, 3, 7, 2, 6, 9, 5]
    assert [case.outcome for case in selected] == [
        "TP",
        "TP",
        "FP",
        "FP",
        "FN",
        "FN",
        "TN",
        "TN",
    ]
    first = selected[0]
    assert (first.label, first.prediction, first.score) == (1, 1, 0.95)
    fn = selected[4]
    assert (fn.label, fn.prediction, fn.score) == (1, 0, 0.2)


def test_binary_selection_options():
    one_each = select_cases(LABELS, SCORES, n_per_outcome=1)
    assert [case.index for case in one_each] == [8, 3, 2, 9]
    many = select_cases(LABELS, SCORES, n_per_outcome=5)
    assert len(many) == len(LABELS)
    two_columns = np.stack([1 - SCORES, SCORES], axis=1)
    assert select_cases(LABELS, two_columns) == select_cases(LABELS, SCORES)
    strict = select_cases(LABELS, SCORES, threshold=0.7, n_per_outcome=1)
    assert [(c.index, c.outcome) for c in strict] == [
        (8, "TP"),
        (3, "FP"),
        (2, "FN"),
        (9, "TN"),
    ]
    logits = select_cases(LABELS, np.log(SCORES / (1 - SCORES)), threshold=0.0)
    assert [c.index for c in logits] == [8, 0, 3, 7, 2, 6, 9, 5]
    tensors = select_cases(torch.tensor(LABELS), torch.tensor(SCORES))
    assert [c.index for c in tensors] == [8, 0, 3, 7, 2, 6, 9, 5]


def test_ties_keep_the_lower_index_first():
    labels = np.array([1, 1, 1, 0])
    scores = np.array([0.8, 0.9, 0.9, 0.1])
    assert [c.index for c in select_cases(labels, scores, n_per_outcome=2)] == [1, 2, 3]


def test_multiclass_selection_picks_confident_correct_and_incorrect_cases():
    labels = np.array([0, 1, 2, 2, 1, 0])
    scores = np.array(
        [
            [0.7, 0.2, 0.1],
            [0.1, 0.8, 0.1],
            [0.5, 0.3, 0.2],
            [0.1, 0.1, 0.8],
            [0.05, 0.05, 0.9],
            [0.2, 0.6, 0.2],
        ]
    )
    selected = select_cases(labels, scores, n_per_outcome=2)
    assert [(c.index, c.outcome) for c in selected] == [
        (1, "correct"),
        (3, "correct"),
        (4, "incorrect"),
        (5, "incorrect"),
    ]
    wrong = selected[2]
    assert (wrong.label, wrong.prediction, wrong.score) == (1, 2, 0.9)


def test_selection_rejects_inconsistent_inputs():
    with pytest.raises(ValueError, match="0 or 1"):
        select_cases([0, 2, 1], [0.1, 0.5, 0.9])
    with pytest.raises(ValueError, match="do not match"):
        select_cases([0, 1], [0.1, 0.5, 0.9])
    with pytest.raises(ValueError, match="NaN"):
        select_cases([0, 1], [0.1, np.nan])
    with pytest.raises(ValueError, match="positive integer"):
        select_cases([0, 1], [0.1, 0.9], n_per_outcome=0)
    with pytest.raises(ValueError, match="outside"):
        select_cases([0, 3], np.eye(3)[:2], task="multiclass")


def test_binary_outcome():
    assert binary_outcome(1, 1) == "TP"
    assert binary_outcome(0, 1) == "FP"
    assert binary_outcome(1, 0) == "FN"
    assert binary_outcome(np.int64(0), False) == "TN"
    with pytest.raises(ValueError):
        binary_outcome(2, 1)
