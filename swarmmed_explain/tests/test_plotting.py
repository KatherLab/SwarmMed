import io

import numpy as np
import pytest
import torch
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure

from swarmmed_explain.plotting import (
    AGREEMENT_COLORS,
    METHOD_STYLES,
    NEGATIVE_COLOR,
    POSITIVE_COLOR,
    method_style,
    plot_feature_bars,
    plot_feature_table,
    plot_image,
    plot_signal,
    plot_spectrogram,
    plot_volume,
    symmetric_limit,
)

RNG = np.random.default_rng(0)


def renders(figure):
    assert isinstance(figure, Figure)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=50)
    assert buffer.getvalue().startswith(b"\x89PNG")


def bar_labels(figure):
    """Return the feature names of a bar chart from top to bottom."""
    labels = figure.axes[0].get_yticklabels()
    ordered = sorted(labels, key=lambda label: -label.get_position()[1])
    return [label.get_text().split(" = ")[0] for label in ordered]


def test_symmetric_limit():
    assert symmetric_limit(np.zeros(5)) == 1.0
    assert symmetric_limit([-4.0, 1.0, 2.0], percentile=100) == pytest.approx(4.0)
    assert symmetric_limit(torch.tensor([0.5, -0.25]), percentile=100) == pytest.approx(
        0.5
    )


@pytest.mark.parametrize("shape", [(16, 20), (16, 20, 3), (3, 16, 20), (1, 16, 20)])
def test_plot_image(shape):
    image = RNG.random(shape)
    renders(plot_image(image, title="image only"))
    renders(plot_image(image, RNG.normal(size=(16, 20)), title="with a map"))
    with pytest.raises(ValueError, match="does not match"):
        plot_image(image, np.zeros((5, 5)))
    with pytest.raises(ValueError, match="NaN"):
        plot_image(image, np.full((16, 20), np.nan))


def test_plot_image_rejects_bad_shapes():
    with pytest.raises(ValueError, match="image must have shape"):
        plot_image(np.zeros((2, 3, 4, 5)))


def test_plot_volume():
    volume = RNG.random((10, 12, 14))
    attribution = np.zeros_like(volume)
    attribution[6] = 1.0
    renders(plot_volume(volume, attribution, n_slices=4, title="volume"))
    renders(plot_volume(volume, slices=[0, 9], axis=1))
    with pytest.raises(ValueError, match="slices must be indices"):
        plot_volume(volume, slices=[10])
    with pytest.raises(ValueError, match="volume must have shape"):
        plot_volume(np.zeros((3, 4)))


def test_plot_signal():
    signal = RNG.normal(size=(3, 200))
    attribution = RNG.normal(size=(3, 200))
    renders(plot_signal(signal, attribution, fs=100.0, channel_names=["I", "II", "V1"]))
    renders(plot_signal(torch.as_tensor(signal[0]), attribution[0], units="mV"))
    renders(plot_signal(signal, attribution, channels=[0, 2], title="two leads"))
    with pytest.raises(ValueError, match="one channel name per channel"):
        plot_signal(signal, channel_names=["I"])
    with pytest.raises(ValueError, match="does not match the signal"):
        plot_signal(signal, attribution[:, :100])
    with pytest.raises(ValueError, match="channels must be indices"):
        plot_signal(signal, channels=[3])
    with pytest.raises(ValueError, match="signal must have shape"):
        plot_signal(np.zeros((2, 3, 4)))


def test_plot_spectrogram():
    spectrogram = RNG.random((32, 50))
    attribution = RNG.normal(size=(32, 50))
    times = np.arange(50) * 0.02
    freqs = np.linspace(0, 1000, 32)
    renders(plot_spectrogram(spectrogram, attribution, times=times, freqs=freqs))
    renders(plot_spectrogram(spectrogram[None], title="no map"))
    with pytest.raises(ValueError, match="times must have one value per frame"):
        plot_spectrogram(spectrogram, times=times[:10])
    with pytest.raises(ValueError, match="freqs must have one value"):
        plot_spectrogram(spectrogram, freqs=freqs[:10])
    with pytest.raises(ValueError, match="spectrogram must have shape"):
        plot_spectrogram(np.zeros(5))


def mel_centres(n_bands=32, fmin=25.0, fmax=800.0):
    """Centre frequencies of HTK mel bands (unevenly spaced in Hz)."""
    low, high = 2595.0 * np.log10(1.0 + np.array([fmin, fmax]) / 700.0)
    edges = np.linspace(low, high, n_bands + 2)
    return 700.0 * (10.0 ** (edges[1:-1] / 2595.0) - 1.0)


def assert_frequency_ticks_match_bands(figure, centres):
    """Every visible y tick names a frequency inside the band drawn at its position.

    A band reaches from the midpoint to its lower neighbour's centre to the midpoint to
    its upper neighbour's centre.
    """
    figure.canvas.draw()
    ax = figure.axes[0]
    images = ax.get_images()
    _, _, bottom, top = images[0].get_extent()
    assert all(image.get_extent() == images[0].get_extent() for image in images)
    middle = (centres[1:] + centres[:-1]) / 2
    low = np.concatenate([[2 * centres[0] - middle[0]], middle])
    high = np.concatenate([middle, [2 * centres[-1] - middle[-1]]])
    ymin, ymax = sorted(ax.get_ylim())
    checked = 0
    for position, label in zip(ax.get_yticks(), ax.get_yticklabels(), strict=True):
        if not ymin <= position <= ymax or not label.get_text():
            continue
        row = int((position - bottom) / (top - bottom) * len(centres))
        row = min(max(row, 0), len(centres) - 1)
        value = float(label.get_text().replace("\N{MINUS SIGN}", "-"))
        assert low[row] <= value <= high[row], (value, centres[row])
        checked += 1
    assert checked >= 3


def test_plot_spectrogram_places_uneven_bins_at_their_frequencies():
    # Mel bands are 16 Hz apart at 25 Hz and 33 Hz apart at 800 Hz; a linear Hz axis
    # spread evenly over the rows drew band 16 (354 Hz) at 416 Hz.
    centres = mel_centres()
    spectrogram = RNG.random((32, 50))
    figure = plot_spectrogram(
        spectrogram,
        RNG.normal(size=(32, 50)),
        times=np.arange(50) * 0.02,
        freqs=centres,
    )
    renders(figure)
    assert_frequency_ticks_match_bands(figure, centres)
    assert "uneven" in figure.axes[0].get_ylabel()
    # Evenly spaced bins keep a linear frequency axis.
    even = np.linspace(0, 1000, 32)
    linear = plot_spectrogram(spectrogram, freqs=even)
    renders(linear)
    assert_frequency_ticks_match_bands(linear, even)
    assert linear.axes[0].get_ylabel() == "frequency (Hz)"


def test_plot_feature_bars():
    values = np.array([0.1, -0.5, 0.3, 0.0, 2.0])
    names = ["a", "b", "c", "d", "e"]
    figure = plot_feature_bars(
        values, names, errors=np.full(5, 0.05), feature_values=[1, 2.5, "x", None, 3]
    )
    renders(figure)
    # Sorted by magnitude, largest at the top.
    assert bar_labels(figure) == ["e", "b", "c", "a", "d"]
    texts = [label.get_text() for label in figure.axes[0].get_yticklabels()]
    assert {"c = x", "d = None", "b = 2.5", "a = 1"} <= set(texts)
    top = plot_feature_bars(values, names, max_features=2)
    renders(top)
    assert bar_labels(top) == ["e", "b"]
    unsorted = plot_feature_bars(values, names, sort=False, max_features=3)
    renders(unsorted)
    assert bar_labels(unsorted) == ["a", "b", "c"]
    assert "2 of 5 features shown" in top.axes[0].get_xlabel()
    with pytest.raises(ValueError, match="one name per value"):
        plot_feature_bars(values, names[:2])
    with pytest.raises(ValueError, match="one error per value"):
        plot_feature_bars(values, errors=[0.1])
    with pytest.raises(ValueError, match="one feature value per value"):
        plot_feature_bars(values, feature_values=[1])
    with pytest.raises(ValueError, match="empty"):
        plot_feature_bars([])


def test_plot_feature_table_marks_missing_values():
    names = ["age", "sex", "cholesterol", "chest pain"]
    figure = plot_feature_table(
        names,
        [63.0, "male", float("nan"), None],
        reference=["54", "male (68%)", 240.5, None],
        reference_header="site median",
        title="Recorded values",
    )
    renders(figure)
    cells = figure.axes[0].tables[0].get_celld()
    texts = {key: cell.get_text().get_text() for key, cell in cells.items()}
    assert [texts[(0, column)] for column in range(3)] == [
        "variable",
        "value",
        "site median",
    ]
    assert [texts[(row, 1)] for row in range(1, 5)] == [
        "63",
        "male",
        "missing",
        "missing",
    ]
    assert texts[(3, 2)] == "240" and texts[(4, 2)] == ""
    assert cells[(3, 1)].get_text().get_fontstyle() == "italic"
    assert cells[(1, 1)].get_text().get_fontstyle() == "normal"
    assert figure.texts[0].get_text() == "Recorded values"
    # Long tables are split into blocks side by side.
    many = plot_feature_table([f"f{i}" for i in range(45)], list(range(45)))
    renders(many)
    assert [len(ax.tables[0].get_celld()) for ax in many.axes] == [42, 42, 12]
    with pytest.raises(ValueError, match="one value per name"):
        plot_feature_table(names, [1.0])
    with pytest.raises(ValueError, match="one reference value per name"):
        plot_feature_table(names, [1, 2, 3, 4], reference=[1])
    with pytest.raises(ValueError, match="empty"):
        plot_feature_table([], [])


def test_method_colours_are_neutral_and_fixed():
    signs = {to_rgb(POSITIVE_COLOR), to_rgb(NEGATIVE_COLOR)}
    colours = [style["color"] for style in METHOD_STYLES.values()]
    colours += list(AGREEMENT_COLORS.values()) + [method_style("unknown")["color"]]
    for colour in colours:
        red, green, blue = to_rgb(colour)
        assert red == green == blue, colour  # a grey, never red or blue
        assert (red, green, blue) not in signs
    assert method_style("integrated_gradients") != method_style("occlusion")
    assert method_style("occlusion") == method_style("feature_ablation")
    style = method_style("occlusion")
    style["color"] = "red"
    assert method_style("occlusion")["color"] != "red"  # a copy each time
