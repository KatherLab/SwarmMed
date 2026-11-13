"""Attribution methods for one case, and permutation importance over a data set.

Conventions shared by every function:

- ``x`` is one case without a batch axis, as a numpy array or torch tensor:
  ``(features,)`` for a tabular row, ``(channels, samples)`` for a signal,
  ``(channels, height, width)`` for an image or spectrogram,
  ``(channels, depth, height, width)`` for a volume.
- ``model`` is a ``torch.nn.Module`` or a function that maps a batch to scores of shape
  ``(batch,)`` or ``(batch, outputs)``. Perturbation methods (:func:`occlusion`,
  :func:`feature_ablation`, :func:`permutation_importance`) call a function with a numpy
  batch. Gradient methods (:func:`integrated_gradients`, :func:`gradient_x_input`,
  :func:`saliency`, :func:`grad_cam`) call it with a torch batch and need a
  differentiable result. A module is always called with a torch batch on its own device
  and dtype.
- ``target`` is the output column to explain. It may be left as None only for a model
  with one output. ``output="probability"`` explains the sigmoid (one output) or softmax
  (several outputs) of the scores instead of the raw scores.
- A module is switched to evaluation mode for the call and switched back afterwards, so
  dropout and batch statistics cannot make results vary. Nothing is random except the
  seeded permutations of :func:`permutation_importance`; results repeat exactly on CPU.
- Attributions are float64 numpy arrays. Positive values support the target output.
"""

from __future__ import annotations

import contextlib
import itertools
import math
import operator
from collections.abc import Callable, Iterator, Sequence
from typing import Any, NamedTuple

import numpy as np
import torch
import torch.nn.functional as F

from .agreement import rankdata

OUTPUTS = ("raw", "probability")
IG_RULES = ("gausslegendre", "trapezoid", "midpoint")
MAX_BATCH_ELEMENTS = 1 << 24
MAX_EVALUATIONS = 100_000


class IntegratedGradients(NamedTuple):
    """Result of :func:`integrated_gradients`.

    Attributes:
        attributions: An array shaped like the input, or a tuple of arrays for a tuple
            of inputs.
        completeness_gap: ``sum(attributions) - (f(x) - f(baseline))`` for the explained
            output. It is close to zero when the path integral is accurate; increase
            ``steps`` when it is not small compared with ``f(x) - f(baseline)``, which
            equals ``sum(attributions) - completeness_gap``.
    """

    attributions: Any
    completeness_gap: float


class PermutationImportance(NamedTuple):
    """Result of :func:`permutation_importance`.

    Attributes:
        mean: Mean drop of the metric per feature (or group) when it is permuted.
        std: Standard deviation of the drop over the repeats.
        values: All drops, shape ``(features, repeats)``.
        baseline_score: The metric on the unpermuted data.
    """

    mean: np.ndarray
    std: np.ndarray
    values: np.ndarray
    baseline_score: float


# --------------------------------------------------------------------------- helpers


def _is_module(model: Any) -> bool:
    return isinstance(model, torch.nn.Module)


def _device_dtype(model: Any) -> tuple[torch.device, torch.dtype]:
    if _is_module(model):
        for tensor in itertools.chain(model.parameters(), model.buffers()):
            if tensor.is_floating_point():
                return tensor.device, tensor.dtype
    return torch.device("cpu"), torch.float32


@contextlib.contextmanager
def _eval_mode(model: Any) -> Iterator[None]:
    if not _is_module(model):
        yield
        return
    modes = [(module, module.training) for module in model.modules()]
    model.eval()
    try:
        yield
    finally:
        for module, training in modes:
            module.training = training


def _check_output(output: str) -> None:
    if output not in OUTPUTS:
        raise ValueError(f"output must be one of {OUTPUTS}, not {output!r}")


def _target_index(target: Any, n_outputs: int) -> int:
    if target is None:
        if n_outputs != 1:
            raise ValueError(
                f"the model has {n_outputs} outputs; pass target=<output index>"
            )
        return 0
    if isinstance(target, bool):
        raise TypeError("target must be an integer index, not a boolean")
    try:
        index = operator.index(target)
    except TypeError:
        raise TypeError(f"target must be an integer index, not {target!r}") from None
    if not 0 <= index < n_outputs:
        if n_outputs == 1:
            raise ValueError(
                f"the model has a single output; use target=None (or 0), not {index}"
            )
        raise ValueError(f"target {index} is outside 0..{n_outputs - 1}")
    return index


def _scores_2d(out: Any, output: str) -> torch.Tensor:
    if isinstance(out, tuple | list):
        raise TypeError(
            "the model returned a tuple or list; wrap it so that it returns one tensor "
            "of scores"
        )
    if not torch.is_tensor(out):
        out = torch.as_tensor(np.asarray(out, dtype=np.float64))
    if out.ndim == 1:
        out = out.unsqueeze(1)
    if out.ndim != 2:
        raise ValueError(
            f"the model returned shape {tuple(out.shape)}; expected (batch,) or "
            "(batch, outputs)"
        )
    if output == "probability":
        out = torch.sigmoid(out) if out.shape[1] == 1 else torch.softmax(out, dim=1)
    return out


def _target_column(out: Any, target: Any, output: str) -> torch.Tensor:
    scores = _scores_2d(out, output)
    return scores[:, _target_index(target, scores.shape[1])]


def _as_array(x: Any, name: str = "x") -> np.ndarray:
    if torch.is_tensor(x):
        x = x.detach().cpu().numpy()
    array = np.asarray(x)
    if not np.issubdtype(array.dtype, np.floating):
        array = array.astype(np.float64)
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains NaN or infinite values")
    return array


def _as_tensor(x: Any, dtype: torch.dtype, device: torch.device) -> torch.Tensor:
    if torch.is_tensor(x):
        tensor = x.detach().to(device=device, dtype=dtype)
    else:
        tensor = torch.as_tensor(np.asarray(x), dtype=dtype, device=device)
    if not torch.all(torch.isfinite(tensor)):
        raise ValueError("the input contains NaN or infinite values")
    return tensor


def _baseline_array(baseline: Any, x: np.ndarray) -> np.ndarray:
    if torch.is_tensor(baseline):
        baseline = baseline.detach().cpu().numpy()
    values = np.asarray(baseline, dtype=x.dtype)
    try:
        values = np.broadcast_to(values, x.shape)
    except ValueError:
        raise ValueError(
            f"baseline of shape {values.shape} does not broadcast to the input shape "
            f"{x.shape}"
        ) from None
    if not np.all(np.isfinite(values)):
        raise ValueError("baseline contains NaN or infinite values")
    return values


def _batch_len(batch_size: int, elements_per_item: int) -> int:
    if isinstance(batch_size, bool) or int(batch_size) < 1:
        raise ValueError("batch_size must be a positive integer")
    return max(1, min(int(batch_size), MAX_BATCH_ELEMENTS // max(1, elements_per_item)))


def _numpy_scorer(
    model: Any, target: Any, output: str
) -> Callable[[np.ndarray], np.ndarray]:
    """Return a function from a numpy batch to float64 scores of the target output."""
    if _is_module(model):
        device, dtype = _device_dtype(model)

        def score_module(batch: np.ndarray) -> np.ndarray:
            with torch.no_grad():
                out = model(torch.as_tensor(batch, dtype=dtype, device=device))
                column = _target_column(out, target, output)
                return column.detach().to("cpu", torch.float64).numpy()

        return score_module
    if not callable(model):
        raise TypeError("model must be a torch.nn.Module or a function")

    def score_function(batch: np.ndarray) -> np.ndarray:
        out = model(batch)
        if torch.is_tensor(out):
            out = out.detach().to("cpu", torch.float64)
        return _target_column(out, target, output).to(torch.float64).numpy()

    return score_function


def as_predict_fn(
    model: Any,
    *,
    target: Any = None,
    output: str = "raw",
    batch_size: int = 256,
) -> Callable[[Any], np.ndarray]:
    """Wrap a model as a function from a numpy batch to numpy scores.

    Args:
        model: A ``torch.nn.Module``, or a function taking a batch (numpy, or torch when
            it is a torch function: the batch is passed as numpy, so wrap torch
            functions in a module or convert inside).
        target: Return only this output column, shape ``(n,)``. None returns every
            column, or shape ``(n,)`` for a model with one output.
        output: ``"raw"`` or ``"probability"`` (sigmoid for one output, else softmax).
        batch_size: Rows per forward pass.

    Returns:
        ``predict(X) -> np.ndarray`` (float64).
    """
    _check_output(output)
    device, dtype = _device_dtype(model)

    def predict(batch: Any) -> np.ndarray:
        data = _as_array(batch, "the batch")
        step = _batch_len(batch_size, int(np.prod(data.shape[1:], dtype=np.int64)))
        parts = []
        with _eval_mode(model), torch.no_grad():
            for start in range(0, data.shape[0], step):
                chunk = data[start : start + step]
                if _is_module(model):
                    out = model(torch.as_tensor(chunk, dtype=dtype, device=device))
                else:
                    out = model(chunk)
                if torch.is_tensor(out):
                    out = out.detach().to("cpu", torch.float64)
                parts.append(_scores_2d(out, "raw").to(torch.float64))
        if not parts:
            return np.empty((0,))
        scores = torch.cat(parts)
        if output == "probability":
            scores = _scores_2d(scores, output)
        if target is not None:
            return scores[:, _target_index(target, scores.shape[1])].numpy()
        if scores.shape[1] == 1:
            return scores[:, 0].numpy()
        return scores.numpy()

    return predict


# --------------------------------------------------------------------------- occlusion


def _int_value(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | np.integer):
        raise TypeError(f"{name} must hold integers, not {value!r}")
    if value < 1:
        raise ValueError(f"{name} must hold positive integers, not {value}")
    return int(value)


def _window(window: Any, shape: tuple[int, ...]) -> tuple[int, ...]:
    if isinstance(window, int | np.integer) and not isinstance(window, bool):
        size = _int_value(window, "window")
        return tuple(min(size, length) for length in shape)
    if not isinstance(window, Sequence):
        raise TypeError("window must be an int or a tuple of ints")
    values = tuple(_int_value(value, "window") for value in window)
    if not values or len(values) > len(shape):
        raise ValueError(
            f"window {values} needs between 1 and {len(shape)} axes for an input of "
            f"shape {shape}"
        )
    full = tuple(shape[: len(shape) - len(values)]) + values
    for size, length in zip(full, shape, strict=True):
        if size > length:
            raise ValueError(f"window {values} does not fit the input shape {shape}")
    return full


def _stride(
    stride: Any, window: tuple[int, ...], shape: tuple[int, ...]
) -> tuple[int, ...]:
    if stride is None:
        return window
    if isinstance(stride, int | np.integer) and not isinstance(stride, bool):
        step = _int_value(stride, "stride")
        return tuple(min(step, size) for size in window)
    if not isinstance(stride, Sequence):
        raise TypeError("stride must be None, an int or a tuple of ints")
    values = tuple(_int_value(value, "stride") for value in stride)
    if not values or len(values) > len(shape):
        raise ValueError(f"stride {values} has the wrong number of axes")
    full = tuple(window[: len(shape) - len(values)]) + values
    for step, size in zip(full, window, strict=True):
        if step > size:
            raise ValueError(
                f"stride {values} is larger than the window {window}; parts of the "
                "input would never be occluded"
            )
    return full


def _starts(length: int, size: int, step: int) -> list[int]:
    starts = list(range(0, length - size + 1, step))
    if starts[-1] + size < length:
        starts.append(length - size)
    return starts


def occlusion(
    model: Any,
    x: Any,
    target: Any = None,
    *,
    window: Any,
    stride: Any = None,
    baseline: Any = 0.0,
    batch_size: int = 32,
    output: str = "raw",
    max_evaluations: int = MAX_EVALUATIONS,
) -> np.ndarray:
    """Occlusion: how much the target score drops when an input window is replaced.

    A window slides over the input. At each position the window is filled with the
    baseline and the model is evaluated; every element covered by the window receives
    ``f(x) - f(x_occluded)``, averaged over all positions that cover it. With
    ``window=1`` a linear model ``w . x`` gets exactly ``w * (x - baseline)``.

    Args:
        model: A module or a function of a numpy batch (see the module docstring).
        x: One case without a batch axis.
        target: Output index to explain (None for a model with one output).
        window: Window size. An int applies to every axis (clipped to the axis length).
            A tuple with one entry per axis is used as given; a shorter tuple applies to
            the last axes, and the leading axes are occluded whole. Examples: ECG
            ``(12, 1000)`` per lead ``(1, 50)``, all leads together ``(50,)``; image
            ``(3, 224, 224)`` patches ``(16, 16)``; tabular row ``1``.
        stride: Step between windows, same forms as ``window``; default ``window`` (no
            overlap). It may not exceed the window. A last window is added at the end of
            an axis when the steps do not reach it, so every element is covered.
        baseline: Fill value: a number, an array broadcastable to ``x`` (for example the
            training mean), or ``"mean"`` for the mean of the occluded window itself.
        batch_size: Occluded copies per forward pass (capped so a batch holds at most
            ``MAX_BATCH_ELEMENTS`` values).
        output: ``"raw"`` or ``"probability"``.
        max_evaluations: Refuse windows that need more forward passes than this.

    Returns:
        An attribution map shaped like ``x``.
    """
    _check_output(output)
    data = _as_array(x)
    shape = data.shape
    if data.ndim == 0:
        raise ValueError("x must have at least one axis")
    sizes = _window(window, shape)
    steps = _stride(stride, sizes, shape)
    fill_mean = isinstance(baseline, str)
    if fill_mean and baseline != "mean":
        raise ValueError("baseline must be a number, an array or 'mean'")
    fill = None if fill_mean else _baseline_array(baseline, data)
    starts = [
        _starts(length, size, step)
        for length, size, step in zip(shape, sizes, steps, strict=True)
    ]
    count = math.prod(len(axis) for axis in starts)
    if count > max_evaluations:
        raise ValueError(
            f"occlusion would need {count} model evaluations (limit "
            f"{max_evaluations}); use a larger window or stride"
        )
    score = _numpy_scorer(model, target, output)
    total = np.zeros(shape)
    covered = np.zeros(shape)
    step = _batch_len(batch_size, data.size)
    with _eval_mode(model):
        reference = float(score(data[None])[0])
        for group in itertools.batched(itertools.product(*starts), step):
            regions = [
                tuple(
                    slice(begin, begin + size)
                    for begin, size in zip(start, sizes, strict=True)
                )
                for start in group
            ]
            batch = np.repeat(data[None], len(regions), axis=0)
            for row, region in enumerate(regions):
                if fill is None:
                    batch[(row, *region)] = data[region].mean()
                else:
                    batch[(row, *region)] = fill[region]
            drops = reference - score(batch)
            for drop, region in zip(drops, regions, strict=True):
                total[region] += drop
                covered[region] += 1
    return total / covered


def feature_ablation(
    model: Any,
    x: Any,
    target: Any = None,
    *,
    baseline: Any,
    groups: Sequence[Sequence[int]] | None = None,
    batch_size: int = 32,
    output: str = "raw",
) -> np.ndarray:
    """Per-case feature ablation against a reference.

    Each feature (or group of features) is set to its reference value in turn and the
    drop of the target score is recorded:
    ``f(x) - f(x with the feature from the reference)``. Use it for tabular rows, with
    the training mean or median as the reference.

    Args:
        model: A module or a function of a numpy batch.
        x: One case, usually ``(features,)``; other shapes are treated element-wise.
        target: Output index to explain (None for a model with one output).
        baseline: The reference: a number or an array broadcastable to ``x``.
        groups: Optional lists of flat feature indices that are ablated together, for
            example a value and its missing-indicator, or the columns of a one-hot code.
        batch_size: Ablated copies per forward pass.
        output: ``"raw"`` or ``"probability"``.

    Returns:
        An array shaped like ``x``, or one value per group when ``groups`` is given.
    """
    _check_output(output)
    data = _as_array(x)
    reference_values = _baseline_array(baseline, data).reshape(-1)
    flat = data.reshape(-1)
    if groups is None:
        index_groups = [np.array([index]) for index in range(flat.size)]
    else:
        index_groups = []
        for number, group in enumerate(groups):
            indices = np.asarray(group, dtype=np.int64).ravel()
            if indices.size == 0:
                raise ValueError(f"group {number} is empty")
            if indices.min() < 0 or indices.max() >= flat.size:
                raise ValueError(
                    f"group {number} has an index outside 0..{flat.size - 1}"
                )
            index_groups.append(indices)
    score = _numpy_scorer(model, target, output)
    result = np.empty(len(index_groups))
    step = _batch_len(batch_size, data.size)
    with _eval_mode(model):
        reference = float(score(data[None])[0])
        for start in range(0, len(index_groups), step):
            part = index_groups[start : start + step]
            batch = np.repeat(flat[None], len(part), axis=0)
            for row, indices in enumerate(part):
                batch[row, indices] = reference_values[indices]
            batch = batch.reshape((len(part), *data.shape))
            result[start : start + len(part)] = reference - score(batch)
    return result.reshape(data.shape) if groups is None else result


# --------------------------------------------------------------------------- gradients


def _tensors(
    model: Any, x: Any
) -> tuple[list[torch.Tensor], bool, torch.device, torch.dtype]:
    if not (_is_module(model) or callable(model)):
        raise TypeError("model must be a torch.nn.Module or a function")
    device, dtype = _device_dtype(model)
    multiple = isinstance(x, tuple)
    inputs = list(x) if multiple else [x]
    if not inputs:
        raise ValueError("x is an empty tuple")
    return [_as_tensor(item, dtype, device) for item in inputs], multiple, device, dtype


def _grad(score: torch.Tensor, inputs: list[torch.Tensor]) -> list[torch.Tensor]:
    if not score.requires_grad:
        raise TypeError(
            "the model output does not depend on the input through torch operations; "
            "gradient methods need a torch module or a differentiable torch function"
        )
    grads = torch.autograd.grad(score.sum(), inputs, allow_unused=True)
    return [
        torch.zeros_like(item) if grad is None else grad
        for grad, item in zip(grads, inputs, strict=True)
    ]


def _input_gradients(
    model: Any, tensors: list[torch.Tensor], target: Any, output: str
) -> list[torch.Tensor]:
    with torch.enable_grad():
        leaves = [item.unsqueeze(0).clone().requires_grad_(True) for item in tensors]
        score = _target_column(model(*leaves), target, output)
        grads = _grad(score, leaves)
    return [grad[0].detach() for grad in grads]


def _result(arrays: list[torch.Tensor], multiple: bool) -> Any:
    converted = tuple(item.detach().to("cpu", torch.float64).numpy() for item in arrays)
    return converted if multiple else converted[0]


def saliency(
    model: Any,
    x: Any,
    target: Any = None,
    *,
    output: str = "raw",
    absolute: bool = True,
) -> Any:
    """Saliency: the gradient of the target score with respect to the input.

    Args:
        model: A module or a differentiable torch function.
        x: One case, or a tuple of inputs for a model with several inputs.
        target: Output index to explain (None for a model with one output).
        output: ``"raw"`` or ``"probability"``.
        absolute: Return ``|gradient|`` (the usual saliency map) instead of the signed
            gradient.

    Returns:
        An array shaped like ``x`` (a tuple of arrays for a tuple of inputs).
    """
    _check_output(output)
    tensors, multiple, _, _ = _tensors(model, x)
    with _eval_mode(model):
        grads = _input_gradients(model, tensors, target, output)
    return _result([grad.abs() if absolute else grad for grad in grads], multiple)


def gradient_x_input(
    model: Any, x: Any, target: Any = None, *, output: str = "raw"
) -> Any:
    """Gradient x input: ``x * d score / d x``, a fast first-order attribution.

    Args:
        model: A module or a differentiable torch function.
        x: One case, or a tuple of inputs for a model with several inputs.
        target: Output index to explain (None for a model with one output).
        output: ``"raw"`` or ``"probability"``.

    Returns:
        An array shaped like ``x`` (a tuple of arrays for a tuple of inputs).
    """
    _check_output(output)
    tensors, multiple, _, _ = _tensors(model, x)
    with _eval_mode(model):
        grads = _input_gradients(model, tensors, target, output)
    return _result(
        [item * grad for item, grad in zip(tensors, grads, strict=True)], multiple
    )


def _path(steps: int, rule: str) -> tuple[np.ndarray, np.ndarray]:
    if isinstance(steps, bool) or int(steps) != steps or steps < 1:
        raise ValueError("steps must be a positive integer")
    steps = int(steps)
    if rule == "gausslegendre":
        nodes, weights = np.polynomial.legendre.leggauss(steps)
        return (nodes + 1.0) / 2.0, weights / 2.0
    if rule == "trapezoid":
        if steps < 2:
            raise ValueError("the trapezoid rule needs at least 2 steps")
        alphas = np.linspace(0.0, 1.0, steps)
        weights = np.full(steps, 1.0 / (steps - 1))
        weights[[0, -1]] /= 2.0
        return alphas, weights
    if rule == "midpoint":
        return (np.arange(steps) + 0.5) / steps, np.full(steps, 1.0 / steps)
    raise ValueError(f"rule must be one of {IG_RULES}, not {rule!r}")


def integrated_gradients(
    model: Any,
    x: Any,
    target: Any = None,
    *,
    baseline: Any = 0.0,
    steps: int = 64,
    batch_size: int = 32,
    output: str = "raw",
    rule: str = "gausslegendre",
) -> IntegratedGradients:
    """Integrated gradients along the straight path from a baseline to the input.

    For input element ``i``::

        attribution_i = (x_i - b_i) * integral over a in [0, 1] of
                        d f(b + a (x - b)) / d x_i

    computed with ``steps`` points of the quadrature ``rule``. The attributions add up
    to ``f(x) - f(baseline)`` up to the returned completeness gap.

    Args:
        model: A module or a differentiable torch function.
        x: One case, or a tuple of inputs for a model with several inputs (for example
            a signal and a metadata vector); the model is then called as
            ``model(*inputs)`` and every input moves along the path together.
        target: Output index to explain (None for a model with one output).
        baseline: A number or array broadcastable to ``x`` (a tuple with one entry per
            input for tuple inputs; a single number applies to all). The baseline should
            mean "no signal": zeros for standardised data, the training mean for tabular
            rows.
        steps: Number of path points (Gauss-Legendre needs fewer than the trapezoid
            rule).
        batch_size: Path points per forward and backward pass.
        output: ``"raw"`` or ``"probability"``.
        rule: ``"gausslegendre"``, ``"trapezoid"`` or ``"midpoint"``.

    Returns:
        :class:`IntegratedGradients` ``(attributions, completeness_gap)``.
    """
    _check_output(output)
    alphas, weights = _path(steps, rule)
    tensors, multiple, device, dtype = _tensors(model, x)
    if multiple and isinstance(baseline, tuple):
        if len(baseline) != len(tensors):
            raise ValueError("give one baseline per input")
        baselines_raw = list(baseline)
    else:
        baselines_raw = [baseline] * len(tensors)
    baselines = []
    for item, base in zip(tensors, baselines_raw, strict=True):
        values = _as_tensor(base, dtype, device)
        try:
            baselines.append(torch.broadcast_to(values, item.shape).clone())
        except RuntimeError:
            raise ValueError(
                f"baseline of shape {tuple(values.shape)} does not broadcast to the "
                f"input shape {tuple(item.shape)}"
            ) from None
    totals = [
        torch.zeros(item.shape, dtype=torch.float64, device=device) for item in tensors
    ]
    step = _batch_len(batch_size, sum(item.numel() for item in tensors))
    with _eval_mode(model):
        for start in range(0, len(alphas), step):
            alpha = torch.as_tensor(
                alphas[start : start + step], dtype=dtype, device=device
            )
            weight = torch.as_tensor(
                weights[start : start + step], dtype=torch.float64, device=device
            )
            with torch.enable_grad():
                path = []
                for item, base in zip(tensors, baselines, strict=True):
                    view = (-1,) + (1,) * item.ndim
                    point = base.unsqueeze(0) + alpha.view(view) * (
                        item - base
                    ).unsqueeze(0)
                    path.append(point.detach().requires_grad_(True))
                score = _target_column(model(*path), target, output)
                grads = _grad(score, path)
            for index, grad in enumerate(grads):
                view = (-1,) + (1,) * tensors[index].ndim
                totals[index] += (grad.to(torch.float64) * weight.view(view)).sum(0)
        with torch.no_grad():
            at_input = _target_column(
                model(*[item.unsqueeze(0) for item in tensors]), target, output
            )
            at_baseline = _target_column(
                model(*[base.unsqueeze(0) for base in baselines]), target, output
            )
    attributions = [
        (item - base).to(torch.float64) * total
        for item, base, total in zip(tensors, baselines, totals, strict=True)
    ]
    delta = float(at_input[0]) - float(at_baseline[0])
    gap = float(sum(float(item.sum()) for item in attributions)) - delta
    return IntegratedGradients(_result(attributions, multiple), gap)


def _resolve_layer(model: Any, layer: Any) -> torch.nn.Module:
    if isinstance(layer, torch.nn.Module):
        return layer
    if isinstance(layer, str):
        if not _is_module(model):
            raise TypeError("a layer name needs a torch module; pass the layer itself")
        try:
            return model.get_submodule(layer)
        except AttributeError:
            raise ValueError(f"the model has no layer named {layer!r}") from None
    raise TypeError("layer must be a torch.nn.Module or the name of one")


def grad_cam(
    model: Any,
    x: Any,
    target: Any = None,
    *,
    layer: Any,
    output: str = "raw",
    relu: bool = True,
    upsample: bool = True,
) -> np.ndarray:
    """Grad-CAM for a 1-D, 2-D or 3-D convolutional layer.

    The layer's activations ``A_k`` (channels ``k``) are captured with a forward hook
    and weighted by the spatial mean of ``d score / d A_k``; the weighted sum over
    channels, passed through ReLU, is upsampled (linear, bilinear or trilinear) to the
    input size.

    Args:
        model: A module or a differentiable torch function that runs ``layer``.
        x: One case, ``(channels, *spatial)`` with 1 to 3 spatial axes.
        target: Output index to explain (None for a model with one output).
        layer: The convolutional layer (module) or its name in ``model``. Its output
            must have shape ``(batch, channels, *spatial)``; usually the last conv
            block.
        output: ``"raw"`` or ``"probability"``.
        relu: Keep only positive evidence (standard Grad-CAM). False keeps the sign.
        upsample: Resize the map to the input's spatial size; False returns the layer's
            resolution.

    Returns:
        A map of shape ``x.shape[1:]`` (or the layer's spatial shape), non-negative when
        ``relu`` is True.
    """
    _check_output(output)
    module = _resolve_layer(model, layer)
    tensors, multiple, _, _ = _tensors(model, x)
    if multiple:
        raise TypeError("grad_cam takes one input; wrap a model with several inputs")
    item = tensors[0]
    captured: list[torch.Tensor] = []

    def hook(_module: Any, _inputs: Any, result: Any) -> None:
        captured.append(result[0] if isinstance(result, tuple | list) else result)

    handle = module.register_forward_hook(hook)
    try:
        with _eval_mode(model), torch.enable_grad():
            leaf = item.unsqueeze(0).clone().requires_grad_(True)
            score = _target_column(model(leaf), target, output)
            if not captured:
                raise ValueError("the layer was not called in the forward pass")
            activation = captured[-1]
            if activation.ndim not in (3, 4, 5):
                raise ValueError(
                    f"the layer output has shape {tuple(activation.shape)}; Grad-CAM "
                    "needs (batch, channels, *spatial) with 1 to 3 spatial axes"
                )
            (gradient,) = _grad(score, [activation])
    finally:
        handle.remove()
    activation = activation.detach()[0].to(torch.float64)
    gradient = gradient.detach()[0].to(torch.float64)
    axes = tuple(range(1, activation.ndim))
    weights = gradient.mean(dim=axes, keepdim=True)
    cam = (weights * activation).sum(dim=0)
    if relu:
        cam = cam.clamp(min=0)
    if upsample:
        spatial = tuple(item.shape[item.ndim - cam.ndim :])
        if len(spatial) != cam.ndim:
            raise ValueError(
                f"the input shape {tuple(item.shape)} has fewer spatial axes than the "
                "layer output"
            )
        mode = {1: "linear", 2: "bilinear", 3: "trilinear"}[cam.ndim]
        cam = F.interpolate(
            cam[None, None], size=spatial, mode=mode, align_corners=False
        )
        cam = cam[0, 0]
        if relu:
            cam = cam.clamp(min=0)
    return cam.cpu().numpy()


# --------------------------------------------------------------------------- data set


def permutation_importance(
    predict: Any,
    X: Any,
    y: Any,
    metric: Callable[[Any, Any], float],
    *,
    n_repeats: int = 5,
    seed: int = 0,
    groups: Sequence[Sequence[int]] | None = None,
    target: Any = None,
    output: str = "raw",
    batch_size: int = 256,
) -> PermutationImportance:
    """Permutation importance: how much a metric drops when a feature is shuffled.

    For each feature (index on axis 1 of ``X``; a lead or channel for signals) the
    values are permuted across cases, the metric is recomputed, and the drop from the
    unpermuted metric is recorded, ``n_repeats`` times with a seeded generator.

    Args:
        predict: A module, or a function mapping a numpy batch to predictions; its
            result (after ``target`` and ``output``) is passed to ``metric``.
        X: Data of shape ``(cases, features, ...)``.
        y: Reference labels, passed to ``metric`` unchanged.
        metric: ``metric(y, predictions) -> float``, higher is better (for example
            :func:`roc_auc` or :func:`accuracy`).
        n_repeats: Permutations per feature.
        seed: Seed for ``numpy.random.default_rng``.
        groups: Optional lists of feature indices permuted together (with the same
            permutation), for example a value and its missing-indicator.
        target: Output column passed to the metric (None: all columns, or the single
            one).
        output: ``"raw"`` or ``"probability"``.
        batch_size: Rows per forward pass.

    Returns:
        :class:`PermutationImportance`; ``mean`` has one value per feature or group.
    """
    if isinstance(n_repeats, bool) or int(n_repeats) != n_repeats or n_repeats < 1:
        raise ValueError("n_repeats must be a positive integer")
    data = _as_array(X, "X").copy()
    if data.ndim < 2:
        raise ValueError("X must have shape (cases, features, ...)")
    n_cases, n_features = data.shape[0], data.shape[1]
    if n_cases < 2:
        raise ValueError("permutation importance needs at least 2 cases")
    if groups is None:
        index_groups = [[index] for index in range(n_features)]
    else:
        index_groups = []
        for number, group in enumerate(groups):
            indices = [int(index) for index in group]
            if not indices or min(indices) < 0 or max(indices) >= n_features:
                raise ValueError(f"group {number} is empty or out of range")
            index_groups.append(indices)
    scorer = as_predict_fn(predict, target=target, output=output, batch_size=batch_size)
    baseline_score = float(metric(y, scorer(data)))
    rng = np.random.default_rng(seed)
    drops = np.empty((len(index_groups), int(n_repeats)))
    for row, indices in enumerate(index_groups):
        original = data[:, indices].copy()
        for repeat in range(int(n_repeats)):
            order = rng.permutation(n_cases)
            data[:, indices] = original[order]
            drops[row, repeat] = baseline_score - float(metric(y, scorer(data)))
        data[:, indices] = original
    return PermutationImportance(
        drops.mean(axis=1), drops.std(axis=1), drops, baseline_score
    )


def roc_auc(y_true: Any, scores: Any) -> float:
    """Area under the ROC curve for binary labels (ties count one half).

    Args:
        y_true: Labels 0 or 1.
        scores: Scores for the positive class, shape ``(n,)``.

    Returns:
        The AUROC, or NaN when only one class is present.
    """
    labels = np.asarray(y_true).ravel()
    values = np.asarray(scores, dtype=np.float64).ravel()
    if labels.shape != values.shape:
        raise ValueError("y_true and scores must have the same length")
    if not np.all(np.isin(labels, (0, 1))):
        raise ValueError("roc_auc needs labels 0 or 1")
    positive = labels == 1
    n_positive = int(positive.sum())
    n_negative = labels.size - n_positive
    if n_positive == 0 or n_negative == 0:
        return math.nan
    ranks = rankdata(values)
    rank_sum = float(ranks[positive].sum())
    return (rank_sum - n_positive * (n_positive + 1) / 2.0) / (n_positive * n_negative)


def accuracy(y_true: Any, scores: Any, *, threshold: float = 0.5) -> float:
    """Accuracy of predictions given as scores.

    Args:
        y_true: Reference labels.
        scores: ``(n, classes)`` scores (argmax is the prediction), ``(n,)`` integer or
            boolean predicted labels, or ``(n,)`` binary float scores (positive when at
            least ``threshold``; use 0.0 for logits).
        threshold: See ``scores``.

    Returns:
        The fraction of correct predictions.
    """
    labels = np.asarray(y_true).ravel()
    values = np.asarray(scores)
    if values.ndim == 2 and values.shape[1] > 1:
        predicted = values.argmax(axis=1)
    elif np.issubdtype(values.dtype, np.integer) or values.dtype == np.bool_:
        predicted = values.ravel().astype(np.int64)
    else:
        predicted = (values.ravel() >= threshold).astype(np.int64)
    if predicted.shape != labels.shape:
        raise ValueError("y_true and scores must describe the same number of cases")
    return float(np.mean(predicted == labels))
