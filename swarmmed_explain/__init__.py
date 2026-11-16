"""Per-case explanations for SwarmMed models, and the bundle format SwarmMedHub reads.

Modules:

- :mod:`swarmmed_explain.methods`: occlusion, integrated gradients, gradient x input,
  saliency, Grad-CAM, feature ablation, permutation importance (numpy and torch);
- :mod:`swarmmed_explain.selection`: which cases to explain (most confident per
  outcome);
- :mod:`swarmmed_explain.agreement`: rank agreement between two maps of one case;
- :mod:`swarmmed_explain.plotting`: figures for images, volumes, signals, spectrograms
  and feature bars;
- :mod:`swarmmed_explain.helper`: the ``explain`` object of ``explainability.py``;
- :mod:`swarmmed_explain.bundle`: the bundle schema, writer, validator and safe copy;
- :mod:`swarmmed_explain.run_local`: run ``explainability.py`` locally like the Hub.

The names below are imported on first use, so ``import swarmmed_explain.bundle`` needs
neither torch nor matplotlib. No module imports Django.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "1.0.0"

_EXPORTS = {
    "IntegratedGradients": "methods",
    "PermutationImportance": "methods",
    "accuracy": "methods",
    "as_predict_fn": "methods",
    "feature_ablation": "methods",
    "grad_cam": "methods",
    "gradient_x_input": "methods",
    "integrated_gradients": "methods",
    "occlusion": "methods",
    "permutation_importance": "methods",
    "roc_auc": "methods",
    "saliency": "methods",
    "SelectedCase": "selection",
    "binary_outcome": "selection",
    "select_cases": "selection",
    "rank_agreement": "agreement",
    "rankdata": "agreement",
    "spearman": "agreement",
    "top_k_overlap": "agreement",
    "plot_feature_bars": "plotting",
    "plot_image": "plotting",
    "plot_signal": "plotting",
    "plot_spectrogram": "plotting",
    "plot_volume": "plotting",
    "symmetric_limit": "plotting",
    "ExplainHelper": "helper",
    "copy_package": "helper",
    "run_explain_script": "helper",
    "wrapper_source": "helper",
    "MAX_CASES": "bundle",
    "MAX_PANELS_PER_CASE": "bundle",
    "MAX_SUMMARIES": "bundle",
    "OUTCOMES": "bundle",
    "SCHEMA": "bundle",
    "BundleError": "bundle",
    "BundleWriter": "bundle",
    "read_bundle": "bundle",
    "safe_copy_bundle": "bundle",
    "sanitize_png": "bundle",
    "validate_bundle": "bundle",
    "validate_cases": "bundle",
    "write_bundle": "bundle",
}

__all__ = ["__version__", *sorted(_EXPORTS)]


def __getattr__(name: str) -> Any:
    """Import an exported name from its module on first use."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """List the module's names, including those not imported yet."""
    return sorted({*globals(), *_EXPORTS})
