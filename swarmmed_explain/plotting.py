"""Figures for explanations: images, volumes, signals, spectrograms, bars and tables.

Every function draws one small matplotlib figure, makes it the current figure and
returns it, so ``explain.add_panel(...)`` can save it directly. Attributions use one
zero-centred diverging colour map (ColorBrewer RdBu, reversed: red supports the
explained output, blue works against it, white is zero), which stays distinguishable
with the common forms of colour blindness. The colour scale is symmetric around zero and
clipped at the 99th percentile of ``|attribution|`` by default, so a few extreme values
do not wash out the rest. Colour scales are per figure: do not compare brightness
between figures. Figures that compare methods tell them apart by greys and line styles
(``METHOD_STYLES``, ``method_style``), never by red and blue.

When pyplot has not been imported yet, the non-interactive Agg backend is selected.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from typing import Any

import numpy as np

DIVERGING_CMAP = "RdBu_r"
POSITIVE_COLOR = "#b2182b"
NEGATIVE_COLOR = "#2166ac"
SIGN_NOTE = "red supports, blue opposes the output"
TITLE_SIZE = 9
LABEL_SIZE = 8
TICK_SIZE = 7
MISSING_COLOR = "0.45"

# Colours and line styles that tell methods apart where two maps are compared (method
# agreement panels and summaries). They are greys on purpose: red and blue mean
# "supports" and "opposes" the output, never a method.
METHOD_STYLES = {
    "integrated_gradients": {"color": "#1a1a1a", "linestyle": "-"},
    "occlusion": {"color": "#878787", "linestyle": "--"},
    "feature_ablation": {"color": "#878787", "linestyle": "--"},
    "gradient_x_input": {"color": "#4d4d4d", "linestyle": "-."},
    "saliency": {"color": "#4d4d4d", "linestyle": "-."},
    "grad_cam": {"color": "#bababa", "linestyle": ":"},
}
# Bar colours of the two agreement statistics in summary figures.
AGREEMENT_COLORS = {"spearman": "#4d4d4d", "top_k_overlap": "#bababa"}


def method_style(method: str) -> dict[str, str]:
    """Return the colour and line style of a method in comparison figures.

    The same method gets the same grey in every template; unknown methods get dark grey,
    dotted. Pass the result to ``Axes.plot`` (``**method_style(name)``); for bars use
    its ``"color"``.
    """
    return dict(METHOD_STYLES.get(method, {"color": "#4d4d4d", "linestyle": ":"}))


def _pyplot() -> Any:
    if "matplotlib.pyplot" not in sys.modules:
        import matplotlib

        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _numpy(value: Any, name: str) -> np.ndarray:
    detach = getattr(value, "detach", None)
    if callable(detach):
        value = detach().cpu().numpy()
    array = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains NaN or infinite values")
    return array


def symmetric_limit(values: Any, percentile: float = 99.0) -> float:
    """Return the colour limit ``m`` for a symmetric scale ``[-m, m]``.

    Args:
        values: Attribution values.
        percentile: Percentile of ``|values|`` used as the limit (100 = maximum).

    Returns:
        A positive limit (1.0 for an all-zero map).
    """
    magnitude = np.abs(np.asarray(values, dtype=np.float64)).ravel()
    magnitude = magnitude[np.isfinite(magnitude)]
    if magnitude.size == 0:
        return 1.0
    limit = float(np.percentile(magnitude, percentile)) if percentile < 100 else 0.0
    if limit <= 0:
        limit = float(magnitude.max())
    return limit if limit > 0 else 1.0


def _norm(limit: float) -> Any:
    from matplotlib.colors import Normalize

    return Normalize(vmin=-limit, vmax=limit)


def _overlay(attribution: np.ndarray, limit: float, alpha: float) -> np.ndarray:
    """RGBA overlay: diverging-map colour, opacity growing with |attribution|."""
    import matplotlib

    scaled = np.clip(attribution / limit, -1.0, 1.0)
    rgba = matplotlib.colormaps[DIVERGING_CMAP]((scaled + 1.0) / 2.0)
    rgba[..., 3] = alpha * np.abs(scaled)
    return rgba


def _colorbar(fig: Any, axes: Any, limit: float, label: str) -> Any:
    from matplotlib.cm import ScalarMappable

    mappable = ScalarMappable(norm=_norm(limit), cmap=DIVERGING_CMAP)
    bar = fig.colorbar(mappable, ax=axes, fraction=0.046, pad=0.03, shrink=0.9)
    bar.set_label(label, fontsize=TICK_SIZE)
    bar.ax.tick_params(labelsize=TICK_SIZE - 1)
    return bar


def _color_label(label: str) -> str:
    return f"{label}\n({SIGN_NOTE})"


def _robust_range(values: np.ndarray) -> tuple[float, float]:
    low, high = np.percentile(values, [1.0, 99.0])
    if high <= low:
        low, high = float(values.min()), float(values.max())
    if high <= low:
        high = low + 1.0
    return float(low), float(high)


def _image_2d(image: Any) -> np.ndarray:
    array = _numpy(image, "image")
    if (
        array.ndim == 3
        and array.shape[0] in (1, 3, 4)
        and array.shape[-1] not in (3, 4)
    ):
        array = np.moveaxis(array, 0, -1)
    if array.ndim == 3 and array.shape[-1] == 1:
        array = array[..., 0]
    if array.ndim == 2:
        return array
    if array.ndim == 3 and array.shape[-1] in (3, 4):
        low, high = float(array.min()), float(array.max())
        if low < 0 or high > 1:
            array = (array - low) / (high - low if high > low else 1.0)
        return array
    raise ValueError(
        "image must have shape (H, W), (H, W, 3), (H, W, 4) or (1|3|4, H, W), not "
        f"{array.shape}"
    )


def _map_like(attribution: Any, spatial: tuple[int, ...]) -> np.ndarray:
    """Return an attribution of shape ``spatial``.

    A leading or trailing channel axis is summed.
    """
    array = _numpy(attribution, "attribution")
    if array.shape == spatial:
        return array
    if array.ndim == len(spatial) + 1:
        if array.shape[1:] == spatial:
            return array.sum(axis=0)
        if array.shape[:-1] == spatial:
            return array.sum(axis=-1)
    raise ValueError(
        f"attribution of shape {array.shape} does not match the data shape {spatial}"
    )


def plot_image(
    image: Any,
    attribution: Any = None,
    *,
    title: str | None = None,
    attribution_label: str = "attribution",
    alpha: float = 0.75,
    percentile: float = 99.0,
    figsize: tuple[float, float] = (4.0, 4.0),
) -> Any:
    """A 2-D image with an attribution overlay.

    Args:
        image: ``(H, W)`` greyscale, ``(H, W, 3|4)`` colour, or channel-first
            ``(1|3|4, H, W)``.
        attribution: Optional map ``(H, W)``, or with a channel axis (summed over it).
        title: Figure title.
        attribution_label: Colour bar label (give units if the scores have them).
        alpha: Opacity of the strongest attributions; weak ones fade out.
        percentile: Colour limit as a percentile of ``|attribution|``.
        figsize: Figure size in inches.

    Returns:
        The matplotlib figure.
    """
    plt = _pyplot()
    picture = _image_2d(image)
    fig, ax = plt.subplots(figsize=figsize, layout="constrained")
    if picture.ndim == 2:
        low, high = _robust_range(picture)
        ax.imshow(picture, cmap="gray", vmin=low, vmax=high, interpolation="nearest")
    else:
        ax.imshow(picture, interpolation="nearest")
    if attribution is not None:
        values = _map_like(attribution, picture.shape[:2])
        limit = symmetric_limit(values, percentile)
        ax.imshow(_overlay(values, limit, alpha), interpolation="nearest")
        _colorbar(fig, ax, limit, _color_label(attribution_label))
    ax.set_axis_off()
    if title:
        ax.set_title(title, fontsize=TITLE_SIZE)
    return fig


def plot_volume(
    volume: Any,
    attribution: Any = None,
    *,
    axis: int = 0,
    n_slices: int = 6,
    slices: Sequence[int] | None = None,
    ncols: int = 3,
    title: str | None = None,
    slice_label: str = "slice",
    attribution_label: str = "attribution",
    alpha: float = 0.75,
    percentile: float = 99.0,
) -> Any:
    """Slices of a 3-D volume with an attribution overlay.

    Without ``slices``, the ``n_slices`` slices with the largest total ``|attribution|``
    are shown in order (evenly spaced slices when there is no attribution). All slices
    share one grey scale and one attribution scale.

    Args:
        volume: ``(D, H, W)`` or ``(1, D, H, W)``.
        attribution: Optional map of the same shape, or with a channel axis.
        axis: Volume axis to slice along.
        n_slices: Number of slices to show.
        slices: Explicit slice indices.
        ncols: Columns of the slice grid.
        title: Figure title.
        slice_label: Label in each slice title (e.g. ``"axial slice"``).
        attribution_label: Colour bar label.
        alpha: Opacity of the strongest attributions.
        percentile: Colour limit as a percentile of ``|attribution|`` in the shown
            slices.

    Returns:
        The matplotlib figure.
    """
    plt = _pyplot()
    data = _numpy(volume, "volume")
    if data.ndim == 4 and data.shape[0] == 1:
        data = data[0]
    if data.ndim != 3:
        raise ValueError(f"volume must have shape (D, H, W), not {data.shape}")
    values = None
    if attribution is not None:
        values = np.moveaxis(_map_like(attribution, data.shape), axis, 0)
    data = np.moveaxis(data, axis, 0)
    depth = data.shape[0]
    if slices is not None:
        chosen = [int(index) for index in slices]
        if not chosen or min(chosen) < 0 or max(chosen) >= depth:
            raise ValueError(f"slices must be indices in 0..{depth - 1}")
    elif values is not None:
        mass = np.abs(values).reshape(depth, -1).sum(axis=1)
        order = np.lexsort((np.arange(depth), -mass))
        chosen = sorted(int(index) for index in order[: min(n_slices, depth)])
    else:
        chosen = sorted(
            {int(round(i)) for i in np.linspace(0, depth - 1, min(n_slices, depth))}
        )
    ncols = max(1, min(ncols, len(chosen)))
    nrows = math.ceil(len(chosen) / ncols)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(2.0 * ncols + 1.0, 2.0 * nrows + 0.5),
        squeeze=False,
        layout="constrained",
    )
    low, high = _robust_range(data[chosen])
    limit = symmetric_limit(values[chosen], percentile) if values is not None else 1.0
    flat_axes = axes.ravel()
    for ax, index in zip(flat_axes, chosen, strict=False):
        ax.imshow(
            data[index], cmap="gray", vmin=low, vmax=high, interpolation="nearest"
        )
        if values is not None:
            ax.imshow(_overlay(values[index], limit, alpha), interpolation="nearest")
        ax.set_title(f"{slice_label} {index}", fontsize=LABEL_SIZE)
        ax.set_axis_off()
    for ax in flat_axes[len(chosen) :]:
        ax.set_axis_off()
    if values is not None:
        _colorbar(fig, list(flat_axes), limit, _color_label(attribution_label))
    if title:
        fig.suptitle(title, fontsize=TITLE_SIZE)
    return fig


def plot_signal(
    signal: Any,
    attribution: Any = None,
    *,
    fs: float | None = None,
    channel_names: Sequence[str] | None = None,
    channels: Sequence[int] | None = None,
    title: str | None = None,
    units: str | None = None,
    attribution_label: str = "attribution",
    percentile: float = 99.0,
    figsize: tuple[float, float] | None = None,
) -> Any:
    """A multichannel signal, one row per channel, with attribution shading behind it.

    Args:
        signal: ``(channels, samples)`` or ``(samples,)``.
        attribution: Optional ``(channels, samples)`` map, or ``(samples,)`` shared by
            all channels (for example a Grad-CAM map over time).
        fs: Sampling rate in Hz; the time axis is then in seconds, otherwise in samples.
        channel_names: One name per channel (e.g. ECG leads), shown at the left.
        channels: Indices of the channels to draw (default all).
        title: Figure title.
        units: Amplitude units, e.g. ``"mV"`` or ``"uV"`` (or ``"z-score"``).
        attribution_label: Colour bar label.
        percentile: Colour limit as a percentile of ``|attribution|``.
        figsize: Figure size; default 7 inches wide, about 0.55 inches per channel.

    Returns:
        The matplotlib figure.
    """
    plt = _pyplot()
    data = _numpy(signal, "signal")
    if data.ndim == 1:
        data = data[None, :]
    if data.ndim != 2:
        raise ValueError("signal must have shape (channels, samples) or (samples,)")
    n_channels, n_samples = data.shape
    values = None
    if attribution is not None:
        values = _numpy(attribution, "attribution")
        if values.ndim == 1:
            values = np.broadcast_to(values, data.shape)
        if values.shape != data.shape:
            raise ValueError(
                f"attribution of shape {values.shape} does not match the signal "
                f"{data.shape}"
            )
    if channel_names is not None and len(channel_names) != n_channels:
        raise ValueError("give one channel name per channel")
    names = (
        [str(name) for name in channel_names]
        if channel_names is not None
        else [f"ch {index}" for index in range(n_channels)]
    )
    shown = list(range(n_channels)) if channels is None else [int(c) for c in channels]
    if not shown or min(shown) < 0 or max(shown) >= n_channels:
        raise ValueError(f"channels must be indices in 0..{n_channels - 1}")
    step = 1.0 / fs if fs else 1.0
    times = np.arange(n_samples) * step
    end = n_samples * step
    if figsize is None:
        figsize = (7.0, 0.55 * len(shown) + 1.2)
    fig, axes = plt.subplots(
        len(shown), 1, sharex=True, figsize=figsize, squeeze=False, layout="constrained"
    )
    axes = axes[:, 0]
    limit = symmetric_limit(values[shown], percentile) if values is not None else 1.0
    from matplotlib.ticker import MaxNLocator

    for ax, channel in zip(axes, shown, strict=True):
        trace = data[channel]
        low, high = float(trace.min()), float(trace.max())
        pad = 0.08 * (high - low) if high > low else 0.5
        low, high = low - pad, high + pad
        if values is not None:
            ax.imshow(
                values[channel][None, :],
                aspect="auto",
                cmap=DIVERGING_CMAP,
                norm=_norm(limit),
                extent=(0.0, end, low, high),
                interpolation="nearest",
                zorder=0,
            )
        ax.plot(times, trace, color="black", linewidth=0.6, zorder=1)
        ax.set_xlim(0.0, end)
        ax.set_ylim(low, high)
        ax.set_ylabel(
            names[channel],
            rotation=0,
            ha="right",
            va="center",
            fontsize=TICK_SIZE,
        )
        ax.yaxis.set_major_locator(MaxNLocator(nbins=2))
        ax.tick_params(labelsize=TICK_SIZE - 1)
    axes[-1].set_xlabel("time (s)" if fs else "sample", fontsize=LABEL_SIZE)
    if units:
        fig.supylabel(f"amplitude ({units})", fontsize=LABEL_SIZE)
    if values is not None:
        _colorbar(fig, list(axes), limit, _color_label(attribution_label))
    if title:
        fig.suptitle(title, fontsize=TITLE_SIZE)
    return fig


def plot_spectrogram(
    spectrogram: Any,
    attribution: Any = None,
    *,
    times: Any = None,
    freqs: Any = None,
    title: str | None = None,
    time_unit: str = "s",
    freq_unit: str = "Hz",
    attribution_label: str = "attribution",
    alpha: float = 0.75,
    percentile: float = 99.0,
    figsize: tuple[float, float] = (6.0, 3.4),
) -> Any:
    """A spectrogram in grey with an attribution overlay.

    Args:
        spectrogram: ``(freqs, frames)`` or ``(1, freqs, frames)``, low frequencies
            first (for example log-mel power).
        attribution: Optional map of the same shape, or with a channel axis.
        times: Optional frame times (length ``frames``) for the x axis.
        freqs: Optional bin centre frequencies (length ``freqs``) for the y axis.
            Evenly spaced bins get a linear axis. Unevenly spaced bins (for example
            mel bands) are drawn one row per bin, with tick labels at the true centre
            frequencies of some rows; ``times`` are handled the same way.
        title: Figure title.
        time_unit: Unit of ``times``.
        freq_unit: Unit of ``freqs`` (e.g. ``"Hz"`` or ``"mel"``).
        attribution_label: Colour bar label.
        alpha: Opacity of the strongest attributions.
        percentile: Colour limit as a percentile of ``|attribution|``.
        figsize: Figure size in inches.

    Returns:
        The matplotlib figure.
    """
    plt = _pyplot()
    data = _numpy(spectrogram, "spectrogram")
    if data.ndim == 3 and data.shape[0] == 1:
        data = data[0]
    if data.ndim != 2:
        raise ValueError(
            f"spectrogram must have shape (freqs, frames), not {data.shape}"
        )
    n_freqs, n_frames = data.shape
    xlabel, ylabel = "frame", "frequency bin"
    extent = (0.0, float(n_frames), 0.0, float(n_freqs))
    xticks = yticks = None
    if times is not None:
        axis_times = _numpy(times, "times").ravel()
        if axis_times.size != n_frames:
            raise ValueError("times must have one value per frame")
        low, high, xticks = _bin_axis(axis_times)
        extent = (low, high, extent[2], extent[3])
        xlabel = f"time ({time_unit})"
        if xticks is not None:
            xlabel += ", unevenly spaced frames"
    if freqs is not None:
        axis_freqs = _numpy(freqs, "freqs").ravel()
        if axis_freqs.size != n_freqs:
            raise ValueError("freqs must have one value per frequency bin")
        low, high, yticks = _bin_axis(axis_freqs)
        extent = (extent[0], extent[1], low, high)
        ylabel = f"frequency ({freq_unit})"
        if yticks is not None:
            ylabel += ", unevenly spaced bins"
    fig, ax = plt.subplots(figsize=figsize, layout="constrained")
    low, high = _robust_range(data)
    ax.imshow(
        data,
        origin="lower",
        aspect="auto",
        cmap="gray",
        vmin=low,
        vmax=high,
        extent=extent,
        interpolation="nearest",
    )
    if attribution is not None:
        values = _map_like(attribution, data.shape)
        limit = symmetric_limit(values, percentile)
        ax.imshow(
            _overlay(values, limit, alpha),
            origin="lower",
            aspect="auto",
            extent=extent,
            interpolation="nearest",
        )
        _colorbar(fig, ax, limit, _color_label(attribution_label))
    if xticks is not None:
        ax.set_xticks(*xticks)
    if yticks is not None:
        ax.set_yticks(*yticks)
    ax.set_xlabel(xlabel, fontsize=LABEL_SIZE)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.tick_params(labelsize=TICK_SIZE)
    if title:
        ax.set_title(title, fontsize=TITLE_SIZE)
    return fig


def _tick_text(value: float) -> str:
    return f"{value:.0f}" if abs(value) >= 100 else f"{value:.3g}"


def _bin_axis(
    centres: np.ndarray, n_ticks: int = 6
) -> tuple[float, float, tuple[list[float], list[str]] | None]:
    """Axis range for bins centred on ``centres``, and ticks when they are uneven.

    Evenly spaced bins are drawn on a linear axis in the centres' unit. Unevenly spaced
    bins (for example mel bands) are drawn one row or column per bin, from 0 to the
    number of bins, with ticks at some bins' middles labelled with their true centres;
    a linear axis would put most of them at the wrong value.
    """
    count = centres.size
    if count == 1:
        return float(centres[0]) - 0.5, float(centres[0]) + 0.5, None
    steps = np.diff(centres)
    if np.allclose(steps, steps[0], rtol=1e-3, atol=0.0):
        half = float(steps[0]) / 2
        return float(centres[0]) - half, float(centres[-1]) + half, None
    rows = np.unique(np.linspace(0, count - 1, min(count, n_ticks)).round().astype(int))
    ticks = (
        [float(row) + 0.5 for row in rows],
        [_tick_text(centres[row]) for row in rows],
    )
    return 0.0, float(count), ticks


def _format_value(value: Any) -> str:
    if isinstance(value, bool | np.bool_):
        return str(bool(value))
    if isinstance(value, int | np.integer):
        return str(int(value))
    if isinstance(value, float | np.floating):
        return f"{float(value):.3g}"
    text = str(value)
    return text if len(text) <= 24 else text[:21] + "..."


def plot_feature_bars(
    values: Any,
    names: Sequence[str] | None = None,
    *,
    errors: Any = None,
    feature_values: Sequence[Any] | None = None,
    title: str | None = None,
    xlabel: str = f"attribution ({SIGN_NOTE})",
    max_features: int = 15,
    sort: bool = True,
    figsize: tuple[float, float] | None = None,
) -> Any:
    """Horizontal signed bars, one per feature, largest magnitude at the top.

    Args:
        values: One value per feature (attribution, or importance).
        names: Feature names (default ``feature 0`` ...).
        errors: Optional error bar half-widths (e.g. permutation-importance std).
        feature_values: Optional values of the features for this case, shown as
            ``name = value``.
        title: Figure title.
        xlabel: Axis label; say what the values are and their units, e.g.
            ``"drop in AUROC when permuted"``.
        max_features: Show at most this many bars.
        sort: Sort by ``|value|``; False keeps the given order.
        figsize: Figure size; default 5 inches wide, about 0.28 inches per bar.

    Returns:
        The matplotlib figure.
    """
    plt = _pyplot()
    data = _numpy(values, "values").ravel()
    count = data.size
    if count == 0:
        raise ValueError("values is empty")
    if names is not None and len(names) != count:
        raise ValueError("give one name per value")
    labels = (
        [str(name) for name in names]
        if names is not None
        else [f"feature {i}" for i in range(count)]
    )
    if feature_values is not None:
        if len(feature_values) != count:
            raise ValueError("give one feature value per value")
        labels = [
            f"{label} = {_format_value(value)}"
            for label, value in zip(labels, feature_values, strict=True)
        ]
    spread = None
    if errors is not None:
        spread = _numpy(errors, "errors").ravel()
        if spread.size != count:
            raise ValueError("give one error per value")
    if sort:
        order = np.lexsort((np.arange(count), -np.abs(data)))[:max_features]
    else:
        order = np.arange(min(count, max_features))
    shown = data[order]
    positions = np.arange(len(order))[::-1]
    colors = [POSITIVE_COLOR if value >= 0 else NEGATIVE_COLOR for value in shown]
    if figsize is None:
        figsize = (5.0, 0.28 * len(order) + 1.3)
    fig, ax = plt.subplots(figsize=figsize, layout="constrained")
    ax.barh(
        positions,
        shown,
        color=colors,
        xerr=None if spread is None else spread[order],
        ecolor="0.25",
        capsize=2,
        height=0.7,
    )
    ax.axvline(0.0, color="0.25", linewidth=0.8)
    ax.set_yticks(positions)
    ax.set_yticklabels([labels[i] for i in order], fontsize=TICK_SIZE)
    extent = np.abs(shown) + (0.0 if spread is None else spread[order])
    limit = float(extent.max()) * 1.1 if float(extent.max()) > 0 else 1.0
    ax.set_xlim(-limit, limit)
    if count > len(order):
        xlabel = f"{xlabel}\n{len(order)} of {count} features shown"
    ax.set_xlabel(xlabel, fontsize=LABEL_SIZE)
    ax.tick_params(axis="x", labelsize=TICK_SIZE)
    if title:
        ax.set_title(title, fontsize=TITLE_SIZE)
    return fig


def _cell_text(value: Any, missing: str) -> tuple[str, bool]:
    """Table text of a recorded value, and whether the value is missing."""
    if value is None:
        return missing, True
    if isinstance(value, float | np.floating) and not math.isfinite(float(value)):
        return missing, True
    text = value if isinstance(value, str) else _format_value(value)
    return text, text == missing


def plot_feature_table(
    names: Sequence[str],
    values: Sequence[Any],
    *,
    reference: Sequence[Any] | None = None,
    value_header: str = "value",
    reference_header: str = "reference",
    missing: str = "missing",
    title: str | None = None,
    rows_per_column: int = 20,
    figsize: tuple[float, float] | None = None,
) -> Any:
    """One case's recorded input values as a table, without any attribution.

    Meant for a case's input panel (method ``"input"``), the panel a blinded review
    shows before the reader saves a read: show what was recorded, never a score, a
    prediction or an attribution. Missing values are shown in grey italics. More than
    ``rows_per_column`` features are split into blocks side by side.

    Args:
        names: Feature names.
        values: One recorded value per feature; ``None``, NaN and the ``missing`` text
            count as missing.
        reference: Optional third column with one value per feature (for example the
            site's training median); ``None`` leaves a cell empty.
        value_header: Header of the value column.
        reference_header: Header of the reference column.
        missing: Text shown for a missing value.
        title: Figure title.
        rows_per_column: Rows per block.
        figsize: Figure size; by default it follows the text length and the rows.

    Returns:
        The matplotlib figure.
    """
    plt = _pyplot()
    labels = [str(name) for name in names]
    count = len(labels)
    if count == 0:
        raise ValueError("names is empty")
    if len(values) != count:
        raise ValueError("give one value per name")
    if reference is not None and len(reference) != count:
        raise ValueError("give one reference value per name")
    if rows_per_column < 1:
        raise ValueError("rows_per_column must be at least 1")
    cells = [_cell_text(value, missing) for value in values]
    headers = ["variable", value_header]
    rows = [[label, text] for label, (text, _) in zip(labels, cells, strict=True)]
    if reference is not None:
        headers.append(reference_header)
        for row, value in zip(rows, reference, strict=True):
            row.append("" if value is None else _cell_text(value, "")[0])
    widths = [
        max(len(headers[column]), *(len(row[column]) for row in rows)) + 2
        for column in range(len(headers))
    ]
    blocks = math.ceil(count / rows_per_column)
    height = min(count, rows_per_column)
    if figsize is None:
        figsize = (max(3.0, 0.075 * sum(widths)) * blocks, 0.24 * (height + 1) + 0.7)
    fig, axes = plt.subplots(
        1, blocks, figsize=figsize, squeeze=False, layout="constrained"
    )
    for block, ax in enumerate(axes[0]):
        ax.axis("off")
        first = block * rows_per_column
        part = rows[first : first + rows_per_column]
        share = (len(part) + 1) / (height + 1)
        table = ax.table(
            cellText=part,
            colLabels=headers,
            cellLoc="left",
            colLoc="left",
            colWidths=[width / sum(widths) for width in widths],
            bbox=(0.0, 1.0 - share, 1.0, share),
        )
        table.auto_set_font_size(False)
        table.set_fontsize(TICK_SIZE + 0.5)
        for (row, column), cell in table.get_celld().items():
            cell.set_edgecolor("0.8")
            if row == 0:
                cell.set_facecolor("0.92")
                cell.get_text().set_fontweight("bold")
            elif column == 1 and cells[first + row - 1][1]:
                cell.get_text().set_color(MISSING_COLOR)
                cell.get_text().set_fontstyle("italic")
    if title:
        fig.suptitle(title, fontsize=TITLE_SIZE)
    return fig
