"""Choose which cases to explain.

A review that shows only correct predictions says little about a model. These functions
pick the most confident cases of every outcome, so failures are reviewed next to
successes:

- binary tasks: true positives and false positives with the highest positive-class
  score, false negatives and true negatives with the lowest;
- multiclass tasks: correct and incorrect predictions with the highest score for the
  predicted class.

Ties are broken by the lower index, so the choice repeats exactly. Only numpy is needed.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import numpy as np

BINARY_OUTCOMES = ("TP", "FP", "FN", "TN")
MULTICLASS_OUTCOMES = ("correct", "incorrect")


class SelectedCase(NamedTuple):
    """One selected case.

    Attributes:
        index: Row index into the arrays passed to :func:`select_cases`.
        outcome: ``"TP"``, ``"FP"``, ``"FN"``, ``"TN"``, ``"correct"`` or
            ``"incorrect"``.
        label: The reference label (0 or 1 for binary tasks, the class index otherwise).
        prediction: The predicted label.
        score: Binary: the positive-class score. Multiclass: the predicted class's
            score.
    """

    index: int
    outcome: str
    label: int
    prediction: int
    score: float


def binary_outcome(label: Any, prediction: Any) -> str:
    """Return the outcome (TP, FP, FN or TN) of a binary label and prediction.

    Raises:
        ValueError: If a value is not 0, 1 or a boolean.
    """
    truth = _binary_value(label, "label")
    predicted = _binary_value(prediction, "prediction")
    if predicted:
        return "TP" if truth else "FP"
    return "FN" if truth else "TN"


def _binary_value(value: Any, field: str) -> bool:
    item = value.item() if hasattr(value, "item") else value
    if item in (0, 1):
        return bool(item)
    raise ValueError(f"{field} must be 0 or 1, not {value!r}")


def _sorted(indices: np.ndarray, keys: np.ndarray, *, descending: bool) -> np.ndarray:
    order = np.lexsort((indices, -keys if descending else keys))
    return indices[order]


def select_cases(
    y_true: Any,
    scores: Any,
    *,
    n_per_outcome: int = 2,
    threshold: float = 0.5,
    task: str = "auto",
) -> list[SelectedCase]:
    """Select the most confident cases per outcome.

    Args:
        y_true: Reference labels, shape ``(n,)``: 0/1 for binary tasks, class indices
            for multiclass tasks.
        scores: Binary: positive-class scores ``(n,)``, or ``(n, 2)`` class scores
            (column 1 is used). Multiclass: ``(n, classes)`` scores; pass probabilities
            so the confidence ranking means something.
        n_per_outcome: Cases to pick per outcome; fewer when an outcome has fewer cases.
        threshold: Binary only: a case is predicted positive when its score is at least
            this value. Use 0.0 for logits.
        task: ``"binary"``, ``"multiclass"`` or ``"auto"`` (1-D or 2-column scores mean
            binary, more columns mean multiclass).

    Returns:
        The selected cases, grouped by outcome in the order TP, FP, FN, TN (binary) or
        correct, incorrect (multiclass), most confident first within each group.

    Raises:
        ValueError: If the inputs do not fit the task.
    """
    if isinstance(n_per_outcome, bool) or int(n_per_outcome) != n_per_outcome:
        raise ValueError("n_per_outcome must be a positive integer")
    if n_per_outcome < 1:
        raise ValueError("n_per_outcome must be a positive integer")
    labels = np.asarray(_numpy(y_true)).ravel()
    values = np.asarray(_numpy(scores), dtype=np.float64)
    if values.ndim not in (1, 2) or values.shape[0] != labels.size:
        raise ValueError(
            f"scores of shape {values.shape} do not match {labels.size} labels"
        )
    if not np.all(np.isfinite(values)):
        raise ValueError("scores contain NaN or infinite values")
    if task == "auto":
        task = "binary" if values.ndim == 1 or values.shape[1] <= 2 else "multiclass"
    if task == "binary":
        return _select_binary(labels, values, int(n_per_outcome), threshold)
    if task == "multiclass":
        return _select_multiclass(labels, values, int(n_per_outcome))
    raise ValueError(f"task must be 'auto', 'binary' or 'multiclass', not {task!r}")


def _numpy(value: Any) -> Any:
    detach = getattr(value, "detach", None)
    return detach().cpu().numpy() if callable(detach) else value


def _select_binary(
    labels: np.ndarray, values: np.ndarray, n: int, threshold: float
) -> list[SelectedCase]:
    if values.ndim == 2:
        if values.shape[1] == 1:
            values = values[:, 0]
        elif values.shape[1] == 2:
            values = values[:, 1]
        else:
            raise ValueError("binary scores must have shape (n,) or (n, 2)")
    if not np.all(np.isin(labels, (0, 1))):
        raise ValueError("binary labels must be 0 or 1")
    truth = labels.astype(bool)
    predicted = values >= threshold
    indices = np.arange(labels.size)
    groups = {
        "TP": (truth & predicted, True),
        "FP": (~truth & predicted, True),
        "FN": (truth & ~predicted, False),
        "TN": (~truth & ~predicted, False),
    }
    selected = []
    for outcome in BINARY_OUTCOMES:
        mask, descending = groups[outcome]
        chosen = _sorted(indices[mask], values[mask], descending=descending)[:n]
        selected.extend(
            SelectedCase(
                int(i), outcome, int(labels[i]), int(predicted[i]), float(values[i])
            )
            for i in chosen
        )
    return selected


def _select_multiclass(
    labels: np.ndarray, values: np.ndarray, n: int
) -> list[SelectedCase]:
    if values.ndim != 2 or values.shape[1] < 2:
        raise ValueError("multiclass scores must have shape (n, classes)")
    if not np.all(np.equal(np.mod(labels, 1), 0)):
        raise ValueError("multiclass labels must be class indices")
    classes = labels.astype(np.int64)
    if classes.size and (classes.min() < 0 or classes.max() >= values.shape[1]):
        raise ValueError("a label is outside the score columns")
    predicted = values.argmax(axis=1)
    confidence = values[np.arange(values.shape[0]), predicted]
    indices = np.arange(labels.size)
    correct = predicted == classes
    selected = []
    for outcome, mask in (("correct", correct), ("incorrect", ~correct)):
        chosen = _sorted(indices[mask], confidence[mask], descending=True)[:n]
        selected.extend(
            SelectedCase(
                int(i),
                outcome,
                int(classes[i]),
                int(predicted[i]),
                float(confidence[i]),
            )
            for i in chosen
        )
    return selected
