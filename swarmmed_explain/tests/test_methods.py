import numpy as np
import pytest
import torch
from torch import nn

from swarmmed_explain import methods
from swarmmed_explain.methods import (
    accuracy,
    as_predict_fn,
    feature_ablation,
    grad_cam,
    gradient_x_input,
    integrated_gradients,
    occlusion,
    permutation_importance,
    roc_auc,
    saliency,
)

WEIGHTS = np.array([1.5, -2.0, 0.5, 3.0])
BIAS = 0.3


def linear_model(weights=WEIGHTS, bias=BIAS):
    model = nn.Linear(len(weights), 1).double()
    with torch.no_grad():
        model.weight.copy_(torch.as_tensor(weights)[None, :])
        model.bias.fill_(bias)
    return model


def test_occlusion_window_one_on_linear_model_equals_weight_times_difference():
    x = np.array([1.0, 2.0, -1.0, 0.5])
    baseline = np.array([0.1, 0.2, 0.3, 0.4])
    expected = WEIGHTS * (x - baseline)
    np.testing.assert_allclose(
        occlusion(linear_model(), x, window=1, baseline=baseline), expected, atol=1e-12
    )
    np.testing.assert_allclose(
        occlusion(linear_model(), x, window=1, baseline=0.0), WEIGHTS * x, atol=1e-12
    )


def test_occlusion_accepts_a_numpy_predict_function_and_batches():
    x = np.array([1.0, 2.0, -1.0, 0.5])
    calls = []

    def predict(batch):
        calls.append(batch.shape[0])
        return batch @ WEIGHTS + BIAS

    result = occlusion(predict, x, window=1, baseline=0.25, batch_size=3)
    np.testing.assert_allclose(result, WEIGHTS * (x - 0.25), atol=1e-12)
    assert calls == [1, 3, 1]


def test_occlusion_on_nd_input_with_windows_and_overlap():
    rng = np.random.default_rng(0)
    weights = rng.normal(size=(2, 6))
    x = rng.normal(size=(2, 6))

    def predict(batch):
        return (batch * weights).sum(axis=(1, 2))

    contribution = weights * x
    # Non-overlapping windows of 2 samples per channel: each element gets its window's
    # sum.
    result = occlusion(predict, x, window=(1, 2))
    expected = np.repeat(contribution.reshape(2, 3, 2).sum(axis=2), 2, axis=1)
    np.testing.assert_allclose(result, expected, atol=1e-12)
    # A shorter tuple applies to the last axis and occludes both channels together.
    together = occlusion(predict, x, window=(2,))
    window_sums = contribution.sum(axis=0).reshape(3, 2).sum(axis=1)
    np.testing.assert_allclose(together, np.tile(np.repeat(window_sums, 2), (2, 1)))
    # Overlapping windows (stride 1) average the drops of every window covering an
    # element.
    row = contribution[0]
    overlapped = occlusion(
        lambda b: (b * weights[0]).sum(axis=1), x[0], window=2, stride=1
    )
    pairs = row[:-1] + row[1:]
    expected_row = np.empty(6)
    expected_row[0] = pairs[0]
    expected_row[-1] = pairs[-1]
    expected_row[1:-1] = (pairs[:-1] + pairs[1:]) / 2
    np.testing.assert_allclose(overlapped, expected_row, atol=1e-12)


def test_occlusion_covers_the_tail_and_supports_mean_fill():
    x = np.arange(5, dtype=float)
    result = occlusion(lambda b: b.sum(axis=1), x, window=2, baseline=0.0)
    assert np.all(np.isfinite(result))
    # Windows start at 0, 2 and 3 (drops 1, 5, 7): the extra window covers the last
    # element, and element 3, covered twice, gets the mean of 5 and 7.
    np.testing.assert_allclose(result, [1.0, 1.0, 5.0, 6.0, 7.0])
    constant = np.full((3, 8), 2.0)
    filled = occlusion(
        lambda b: b.sum(axis=(1, 2)), constant, window=4, baseline="mean"
    )
    np.testing.assert_allclose(filled, 0.0)


def test_occlusion_rejects_bad_windows():
    x = np.zeros((2, 10))
    with pytest.raises(ValueError, match="does not fit"):
        occlusion(lambda b: b.sum(axis=(1, 2)), x, window=(3, 2))
    with pytest.raises(ValueError, match="larger than the window"):
        occlusion(lambda b: b.sum(axis=(1, 2)), x, window=(1, 2), stride=(1, 3))
    with pytest.raises(ValueError, match="model evaluations"):
        occlusion(lambda b: b.sum(axis=(1, 2)), x, window=1, max_evaluations=5)
    with pytest.raises(ValueError, match="baseline"):
        occlusion(lambda b: b.sum(axis=(1, 2)), x, window=1, baseline=np.zeros(3))
    with pytest.raises(ValueError, match="NaN"):
        occlusion(lambda b: b.sum(axis=(1, 2)), np.full((2, 3), np.nan), window=1)


def test_target_selection_and_probability_output():
    torch.manual_seed(0)
    multi = nn.Linear(4, 3).double()
    x = np.array([0.5, -1.0, 2.0, 0.1])
    with pytest.raises(ValueError, match="pass target"):
        occlusion(multi, x, window=1)
    with pytest.raises(ValueError, match="single output"):
        occlusion(linear_model(), x, window=1, target=1)
    with pytest.raises(ValueError, match="outside"):
        occlusion(multi, x, window=1, target=3)
    model = linear_model()
    raw = feature_ablation(model, x, baseline=0.0)
    probability = feature_ablation(model, x, baseline=0.0, output="probability")
    full = torch.sigmoid(torch.tensor(WEIGHTS @ x + BIAS)).item()
    for index in range(4):
        ablated = x.copy()
        ablated[index] = 0.0
        drop = full - torch.sigmoid(torch.tensor(WEIGHTS @ ablated + BIAS)).item()
        assert probability[index] == pytest.approx(drop, abs=1e-12)
    np.testing.assert_allclose(raw, WEIGHTS * x, atol=1e-12)


def test_module_mode_is_restored_and_results_repeat():
    torch.manual_seed(0)
    model = nn.Sequential(nn.Linear(4, 16), nn.Dropout(0.5), nn.Linear(16, 1))
    model.train()
    x = np.array([0.2, -0.4, 1.0, 0.3], dtype=np.float32)
    first = occlusion(model, x, window=1)
    second = occlusion(model, x, window=1)
    np.testing.assert_array_equal(first, second)
    ig_first = integrated_gradients(model, x, steps=16).attributions
    ig_second = integrated_gradients(model, x, steps=16).attributions
    np.testing.assert_array_equal(ig_first, ig_second)
    assert model.training and model[1].training


def test_integrated_gradients_completeness_on_a_smooth_network():
    torch.manual_seed(0)
    rng = np.random.default_rng(1)
    mlp = nn.Sequential(nn.Linear(6, 16), nn.Tanh(), nn.Linear(16, 3)).double()
    x = rng.normal(size=6)
    baseline = 0.1 * rng.normal(size=6)
    result = integrated_gradients(mlp, x, target=1, baseline=baseline, steps=64)
    with torch.no_grad():
        delta = (mlp(torch.tensor(x)[None]) - mlp(torch.tensor(baseline)[None]))[0, 1]
    assert result.attributions.shape == (6,)
    assert abs(result.completeness_gap) < 1e-8
    assert result.attributions.sum() == pytest.approx(delta.item(), abs=1e-8)
    for rule in ("trapezoid", "midpoint"):
        other = integrated_gradients(
            mlp, x, target=1, baseline=baseline, steps=400, rule=rule
        )
        assert abs(other.completeness_gap) < 1e-4


def test_integrated_gradients_completeness_float32_conv_model():
    torch.manual_seed(0)
    model = nn.Sequential(
        nn.Conv1d(3, 8, 5, padding=2), nn.Tanh(), nn.Flatten(), nn.Linear(8 * 40, 2)
    )
    x = np.random.default_rng(2).normal(size=(3, 40)).astype(np.float32)
    result = integrated_gradients(model, x, target=1, steps=64, batch_size=8)
    delta = result.attributions.sum() - result.completeness_gap
    assert result.attributions.shape == (3, 40)
    assert abs(result.completeness_gap) <= 1e-3 * abs(delta) + 1e-5


def test_integrated_gradients_on_a_linear_model_and_with_two_inputs():
    x = np.array([1.0, 2.0, -1.0, 0.5])
    result = integrated_gradients(linear_model(), x, baseline=0.5, steps=8)
    np.testing.assert_allclose(result.attributions, WEIGHTS * (x - 0.5), atol=1e-12)

    class TwoInputs(nn.Module):
        def __init__(self):
            super().__init__()
            self.signal = nn.Linear(4, 1)
            self.meta = nn.Linear(3, 1)

        def forward(self, signal, meta):
            return torch.tanh(self.signal(signal)) + self.meta(meta)

    torch.manual_seed(0)
    model = TwoInputs().double()
    signal = np.array([0.3, -1.2, 0.7, 2.0])
    meta = np.array([1.0, 0.0, 0.5])
    meta_base = np.array([0.2, 0.2, 0.2])
    ig = integrated_gradients(
        model, (signal, meta), baseline=(0.0, meta_base), steps=64
    )
    sig_attr, meta_attr = ig.attributions
    assert sig_attr.shape == (4,) and meta_attr.shape == (3,)
    meta_weights = model.meta.weight.detach().numpy()[0]
    np.testing.assert_allclose(meta_attr, meta_weights * (meta - meta_base), atol=1e-10)
    assert abs(ig.completeness_gap) < 1e-8


def test_gradient_x_input_and_saliency_on_a_linear_model():
    x = np.array([1.0, 2.0, -1.0, 0.5])
    np.testing.assert_allclose(gradient_x_input(linear_model(), x), WEIGHTS * x)
    np.testing.assert_allclose(saliency(linear_model(), x), np.abs(WEIGHTS))
    np.testing.assert_allclose(saliency(linear_model(), x, absolute=False), WEIGHTS)
    with pytest.raises(TypeError, match="differentiable"):
        saliency(lambda batch: torch.as_tensor(batch.detach().numpy().sum(1)), x)


class Pooled(nn.Module):
    """Conv features, spatial mean and a linear head.

    d score / d features = W[target] / size.
    """

    def __init__(self, conv, channels, classes=3):
        super().__init__()
        self.features = conv
        self.head = nn.Linear(channels, classes)

    def forward(self, x):
        features = self.features(x)
        return self.head(features.flatten(2).mean(-1))


@pytest.mark.parametrize(
    ("dims", "shape"),
    [(1, (2, 50)), (2, (3, 20, 24)), (3, (1, 8, 10, 12))],
)
def test_grad_cam_shapes_and_non_negativity(dims, shape):
    torch.manual_seed(dims)
    conv_class = {1: nn.Conv1d, 2: nn.Conv2d, 3: nn.Conv3d}[dims]
    conv = nn.Sequential(
        conv_class(shape[0], 4, 3, padding=1),
        nn.ReLU(inplace=True),
        conv_class(4, 6, 3, stride=2),
        nn.ReLU(),
    )
    model = Pooled(conv, 6)
    x = np.random.default_rng(dims).normal(size=shape).astype(np.float32)
    cam = grad_cam(model, x, target=2, layer="features.2")
    assert cam.shape == shape[1:]
    assert np.all(cam >= 0)
    assert np.all(np.isfinite(cam))
    small = grad_cam(model, x, target=2, layer=model.features[2], upsample=False)
    expected_size = tuple((size - 3) // 2 + 1 for size in shape[1:])
    assert small.shape == expected_size
    signed = grad_cam(model, x, target=0, layer="features", relu=False, upsample=False)
    assert signed.shape == expected_size


def test_grad_cam_matches_the_definition():
    torch.manual_seed(3)
    model = Pooled(nn.Sequential(nn.Conv1d(2, 5, 3, padding=1), nn.ReLU()), 5)
    x = np.random.default_rng(3).normal(size=(2, 30)).astype(np.float32)
    cam = grad_cam(model, x, target=1, layer="features", upsample=False)
    with torch.no_grad():
        activation = model.features(torch.as_tensor(x)[None])[0].double()
    weights = model.head.weight.detach()[1].double() / activation.shape[-1]
    expected = torch.clamp((weights[:, None] * activation).sum(0), min=0).numpy()
    np.testing.assert_allclose(cam, expected, rtol=1e-5, atol=1e-7)


def test_grad_cam_rejects_unused_and_non_convolutional_layers():
    model = Pooled(nn.Sequential(nn.Conv1d(2, 5, 3, padding=1), nn.ReLU()), 5)
    x = np.zeros((2, 30), dtype=np.float32)
    with pytest.raises(ValueError, match="not called"):
        grad_cam(model, x, target=0, layer=nn.Conv1d(2, 2, 1))
    with pytest.raises(ValueError, match="spatial"):
        grad_cam(model, x, target=0, layer="head")
    with pytest.raises(ValueError, match="no layer"):
        grad_cam(model, x, target=0, layer="missing")


def test_feature_ablation_against_a_reference_and_with_groups():
    x = np.array([1.0, 2.0, -1.0, 0.5])
    reference = np.array([0.5, 0.5, 0.5, 0.5])
    result = feature_ablation(linear_model(), x, baseline=reference)
    np.testing.assert_allclose(result, WEIGHTS * (x - reference), atol=1e-12)
    grouped = feature_ablation(
        linear_model(), x, baseline=reference, groups=[[0, 1], [2], [3, 2]]
    )
    contribution = WEIGHTS * (x - reference)
    np.testing.assert_allclose(
        grouped,
        [contribution[0] + contribution[1], contribution[2], contribution[2:].sum()],
        atol=1e-12,
    )
    with pytest.raises(ValueError, match="group 0"):
        feature_ablation(linear_model(), x, baseline=0.0, groups=[[4]])


def make_synthetic(n=400, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 5))
    y = (2.0 * X[:, 2] + 0.3 * rng.normal(size=n) > 0).astype(int)
    return X, y


def test_permutation_importance_ranks_the_informative_feature_first():
    X, y = make_synthetic()

    def predict(batch):
        return 2.0 * batch[:, 2] + 0.05 * batch[:, 0] - 0.05 * batch[:, 4]

    result = permutation_importance(predict, X, y, roc_auc, n_repeats=5, seed=0)
    assert result.values.shape == (5, 5)
    assert int(np.argmax(result.mean)) == 2
    assert result.mean[2] > 0.3
    assert result.baseline_score > 0.95
    np.testing.assert_allclose(result.mean[[1, 3]], 0.0)
    again = permutation_importance(predict, X, y, roc_auc, n_repeats=5, seed=0)
    np.testing.assert_array_equal(result.values, again.values)
    other = permutation_importance(predict, X, y, roc_auc, n_repeats=5, seed=1)
    assert not np.array_equal(result.values, other.values)


def test_permutation_importance_with_a_torch_module_and_groups():
    X, y = make_synthetic()
    model = linear_model(np.array([0.05, 0.0, 2.0, 0.0, -0.05]), 0.0)
    result = permutation_importance(
        model, X, y, roc_auc, n_repeats=3, seed=0, output="probability"
    )
    assert int(np.argmax(result.mean)) == 2
    grouped = permutation_importance(
        model, X, y, roc_auc, n_repeats=3, seed=0, groups=[[0, 1], [2, 3], [4]]
    )
    assert grouped.mean.shape == (3,)
    assert int(np.argmax(grouped.mean)) == 1


def test_as_predict_fn_shapes():
    torch.manual_seed(0)
    model = nn.Linear(4, 3)
    X = np.random.default_rng(0).normal(size=(10, 4)).astype(np.float32)
    assert as_predict_fn(model, batch_size=3)(X).shape == (10, 3)
    probabilities = as_predict_fn(model, output="probability")(X)
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.0, atol=1e-6)
    assert as_predict_fn(model, target=1)(X).shape == (10,)
    assert as_predict_fn(linear_model())(X.astype(np.float64)).shape == (10,)


def test_roc_auc_and_accuracy():
    assert roc_auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == pytest.approx(0.75)
    assert roc_auc([0, 1, 0, 1], [0.5, 0.5, 0.2, 0.9]) == pytest.approx(0.875)
    assert np.isnan(roc_auc([1, 1], [0.2, 0.3]))
    assert accuracy([0, 1, 2], np.eye(3)) == 1.0
    assert accuracy([0, 1, 1], [0.2, 0.7, 0.4]) == pytest.approx(2 / 3)
    assert accuracy([0, 2, 1], np.array([0, 2, 2])) == pytest.approx(2 / 3)


def test_module_constants():
    assert methods.OUTPUTS == ("raw", "probability")
