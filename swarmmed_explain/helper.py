"""Runtime of ``explainability.py``: the ``explain`` helper object.

SwarmMedHub runs a project's ``explainability.py`` in its sandbox with a global
``explain``, an :class:`ExplainHelper` built from the run's data manifest, and collects
the bundle the script writes. ``python -m swarmmed_explain.run_local`` runs a script the
same way on a local machine. Both go through :func:`run_explain_script`.

The manifest maps relative paths to files: the site's data under their own paths (for
example ``fl-client-1/test.npz``) and the job's result files under
``results/<participant>/<file>``. In the Hub the values are presigned MinIO URLs;
locally they are file paths.

Only the standard library is imported at module level; numpy, torch, fsspec and
matplotlib are imported when needed.
"""

from __future__ import annotations

import builtins
import io
import json
import math
import os
import pickle
import runpy
import shutil
import sys
import tempfile
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlparse

from . import bundle as _bundle

SANDBOX_SCRIPT = "explainability.py"
SANDBOX_MANIFEST = "/home/sandboxuser/data/data_manifest.json"
SANDBOX_OUTPUT_DIR = "explain"
INTERNAL_HOST = "minio"
INTERNAL_PORT = 9000
THREADS_ENV = "SWARMMED_EXPLAIN_THREADS"
PANEL_DPI = 100

MODEL_EXTENSIONS = (
    ".pt",
    ".pth",
    ".ckpt",
    ".npy",
    ".npz",
    ".pkl",
    ".joblib",
    ".h5",
    ".keras",
)
GLOBAL_MODEL_FILENAMES = (
    "FL_global_model.pt",
    "best_FL_model.pt",
    "global_model.pt",
    "FL_model.pt",
    "model_weights.npz",
)
RUNTIME_FILES = (
    "__init__.py",
    "agreement.py",
    "bundle.py",
    "helper.py",
    "methods.py",
    "plotting.py",
    "run_local.py",
    "selection.py",
)


def _is_http(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _clean(relative_path: Any) -> str:
    path = os.fspath(relative_path)
    if not isinstance(path, str):
        raise TypeError("paths must be strings")
    return path.lstrip("/")


def _pyplot() -> Any:
    if "matplotlib.pyplot" not in sys.modules:
        import matplotlib

        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _render_png(figure: Any) -> bytes:
    buffer = io.BytesIO()
    figure.savefig(
        buffer,
        format="png",
        dpi=PANEL_DPI,
        bbox_inches="tight",
        facecolor="white",
        metadata={"Software": None},
    )
    return buffer.getvalue()


class ExplainHelper:
    """The ``explain`` object available to ``explainability.py``.

    Data access (``open``, ``exists``, ``listdir``, ``get_model``, ``load_weights``)
    works like the ``visualization`` helper of ``results_visualization.py``; the other
    methods write the explanation bundle (see :mod:`swarmmed_explain.bundle`):

    - :meth:`add_case` describes one case (at most 24);
    - :meth:`add_panel` saves the current matplotlib figure as one of its panels (at
      most 4 per case);
    - :meth:`add_summary` saves the current figure as a run-level figure (at most 4);
    - :meth:`set_info` records the methods, the explained target, notes and metrics.

    Every call checks its values and raises ``ValueError`` (``BundleError``) when one
    breaks a rule, so mistakes stop the script instead of being dropped silently.
    """

    def __init__(
        self,
        manifest_file: str | os.PathLike[str] | Mapping[str, str],
        output_dir: str | os.PathLike[str],
        *,
        rewrite_urls: bool = True,
        internal_host: str = INTERNAL_HOST,
        internal_port: int = INTERNAL_PORT,
        verbose: bool = True,
    ) -> None:
        """Load the manifest and start an empty bundle in ``output_dir``.

        Args:
            manifest_file: Path of a JSON manifest, or the mapping itself. Values are
                http(s) URLs, ``file://`` URLs or file paths (relative paths are
                resolved against the manifest's directory).
            output_dir: New or empty directory for the bundle.
            rewrite_urls: Point http(s) URLs at ``internal_host:internal_port`` (the
                Hub's MinIO as the sandbox reaches it), as the results-visualisation
                helper does.
            internal_host: Host for rewritten URLs.
            internal_port: Port for rewritten URLs.
            verbose: Print short progress lines (never URLs or file contents).
        """
        if isinstance(manifest_file, Mapping):
            raw: Any = dict(manifest_file)
            self._base_dir = os.getcwd()
        else:
            path = os.fspath(manifest_file)
            with builtins.open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            self._base_dir = os.path.dirname(os.path.abspath(path))
        if not isinstance(raw, dict):
            raise ValueError("the manifest must map relative paths to URLs or files")
        self._rewrite = rewrite_urls
        self._endpoint = f"{internal_host}:{int(internal_port)}"
        self._verbose = verbose
        self._fs: Any = None
        self.manifest: dict[str, str] = self._process_manifest(raw)
        self._writer = _bundle.BundleWriter(output_dir)
        self.output_dir = self._writer.out_dir
        self._log(f"explain: {len(self.manifest)} files in the manifest")

    # ----------------------------------------------------------------- data access

    def _log(self, message: str) -> None:
        if self._verbose:
            print(message, flush=True)

    def _process_manifest(self, raw: Mapping[Any, Any]) -> dict[str, str]:
        processed: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("manifest keys and values must be strings")
            relative = key.lstrip("/")
            if _is_http(value):
                processed[relative] = (
                    self._rewrite_url(value) if self._rewrite else value
                )
            elif value.startswith("file://"):
                processed[relative] = self._local_path(unquote(urlparse(value).path))
            elif "://" in value:
                raise ValueError(
                    f"manifest entry {relative!r} uses an unsupported URL scheme"
                )
            else:
                processed[relative] = self._local_path(value)
        return processed

    def _rewrite_url(self, url: str) -> str:
        parsed = urlparse(url)
        old_base = f"{parsed.scheme}://{parsed.netloc}"
        return url.replace(old_base, f"{parsed.scheme}://{self._endpoint}", 1)

    def _local_path(self, path: str) -> str:
        if not os.path.isabs(path):
            path = os.path.join(self._base_dir, path)
        return os.path.normpath(path)

    def _http(self) -> Any:
        if self._fs is None:
            import fsspec

            self._fs = fsspec.filesystem("http")
        return self._fs

    def open(self, relative_path: str, mode: str = "rb", **kwargs: Any) -> Any:
        """Open a file of the manifest for reading (streamed for URLs).

        Args:
            relative_path: Path as listed by :meth:`listdir`, e.g.
                ``fl-client-1/test.npz`` or ``results/fl-client-1/FL_global_model.pt``.
            mode: ``"rb"`` (default) or ``"r"``; writing is not allowed.
            **kwargs: Passed to ``fsspec`` (URLs) or ``open`` (local files).

        Returns:
            A file object; use it in a ``with`` block.

        Raises:
            FileNotFoundError: If the path is not in the manifest.
            ValueError: For a writing mode.
        """
        if any(flag in mode for flag in "wax+"):
            raise ValueError("explain.open is read-only")
        path = _clean(relative_path)
        if path not in self.manifest:
            raise FileNotFoundError(f"File {path} not in manifest.")
        target = self.manifest[path]
        if _is_http(target):
            kwargs.setdefault("ssl", False)
            return self._http().open(target, mode=mode, **kwargs)
        kwargs.pop("ssl", None)
        return builtins.open(target, mode, **kwargs)

    def exists(self, relative_path: str) -> bool:
        """Return True for a file in the manifest or a folder that holds one."""
        path = _clean(relative_path).rstrip("/")
        if not path:
            return bool(self.manifest)
        prefix = path + "/"
        return path in self.manifest or any(
            key.startswith(prefix) for key in self.manifest
        )

    def listdir(self, relative_path: str = "") -> list[str]:
        """List the manifest below a folder, recursively, as paths relative to it.

        ``listdir("")`` returns every manifest path; ``listdir("fl-client-1")`` returns
        for example ``["test.npz", "train.npz"]``; ``listdir("results")`` returns
        ``["fl-client-1/FL_global_model.pt", ...]``. The list is sorted.
        """
        path = _clean(relative_path).rstrip("/")
        if not path:
            return sorted(self.manifest)
        prefix = path + "/"
        return sorted(
            key[len(prefix) :] for key in self.manifest if key.startswith(prefix)
        )

    def find_model(
        self, client_name: str = "fl-client-1", model_filename: str = "model.pt"
    ) -> str:
        """Return the manifest path of the model file :meth:`get_model` would load.

        Search order: ``results/<client_name>/<model_filename>``; the same file name in
        any results folder; a global-model name (``FL_global_model.pt``, ...) in the
        client's folder, then in any folder; any file with a model extension.

        Raises:
            FileNotFoundError: If the results hold no model file.
        """
        wanted = f"results/{client_name}/{model_filename}"
        if wanted in self.manifest:
            return wanted
        results = sorted(key for key in self.manifest if key.startswith("results/"))
        for key in results:
            if key.rsplit("/", 1)[-1] == model_filename:
                return key
        for name in GLOBAL_MODEL_FILENAMES:
            key = f"results/{client_name}/{name}"
            if key in self.manifest:
                return key
        for name in GLOBAL_MODEL_FILENAMES:
            for key in results:
                if key.rsplit("/", 1)[-1] == name:
                    return key
        for key in results:
            if key.lower().endswith(MODEL_EXTENSIONS):
                return key
        raise FileNotFoundError("Model weights not found in manifest.")

    def get_model(
        self, client_name: str = "fl-client-1", model_filename: str = "model.pt"
    ) -> Any:
        """Load model weights from the job's results.

        ``.pt``, ``.pth`` and ``.ckpt`` files are read with
        ``torch.load(map_location="cpu", weights_only=True)``; a dict is unwrapped from
        the keys ``numpy_key``, ``weights``, ``model`` or ``state_dict`` (NVIDIA FLARE's
        ``FL_global_model.pt`` keeps the state dict under ``model``). ``.npy`` and
        ``.npz`` are read without pickle; ``.pkl``/``.joblib`` and Keras
        ``.h5``/``.keras`` files are supported as in the results-visualisation helper.

        Args:
            client_name: Participant folder to look in first.
            model_filename: File name to look for first (see :meth:`find_model`).

        Returns:
            The weights: usually a state dict of tensors.
        """
        key = self.find_model(client_name, model_filename)
        self._log(f"explain: loading model weights from {key}")
        lower = key.lower()
        if lower.endswith((".h5", ".keras")):
            import keras

            folder = tempfile.mkdtemp(prefix="swarmmed_explain_")
            try:
                local = os.path.join(folder, os.path.basename(key))
                with self.open(key, "rb") as source, builtins.open(local, "wb") as copy:
                    shutil.copyfileobj(source, copy)
                return keras.models.load_model(local).get_weights()
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        with self.open(key, "rb") as handle:
            if lower.endswith((".pt", ".pth", ".ckpt")):
                import torch

                data = torch.load(handle, map_location="cpu", weights_only=True)
                if isinstance(data, dict):
                    data = data.get(
                        "numpy_key",
                        data.get(
                            "weights",
                            data.get("model", data.get("state_dict", data)),
                        ),
                    )
                return data
            if lower.endswith(".npy"):
                import numpy as np

                return np.load(handle, allow_pickle=False)
            if lower.endswith(".npz"):
                import numpy as np

                with np.load(handle, allow_pickle=False) as archive:
                    arrays = {name: archive[name] for name in archive.files}
                return arrays.get("params", arrays.get("weights", arrays))
            if lower.endswith((".pkl", ".joblib")):
                try:
                    import joblib
                except ImportError:
                    # Same as the results-visualisation helper; the script is trusted
                    # code in the same sandbox, so pickle adds no new capability.
                    return pickle.load(handle)
                return joblib.load(handle)
        raise ValueError(f"unsupported model file type: {key}")

    def load_weights(
        self,
        model: Any,
        client_name: str = "fl-client-1",
        model_filename: str = "model.pt",
        *,
        strict: bool = False,
    ) -> bool:
        """Load the job's model weights into ``model``.

        PyTorch modules get ``load_state_dict(strict=strict)``; with the default
        ``strict=False`` missing and unexpected keys are printed instead of raising, so
        check the output (or pass ``strict=True``). Keras models get ``set_weights``;
        fitted scikit-learn linear models get ``coef_`` and ``intercept_``.

        Args:
            model: The model to fill.
            client_name: See :meth:`get_model`.
            model_filename: See :meth:`get_model`.
            strict: Passed to ``load_state_dict``.

        Returns:
            True when weights were loaded.
        """
        weights = self.get_model(client_name, model_filename)
        if weights is None:
            return False
        if hasattr(model, "load_state_dict"):
            if not isinstance(weights, Mapping):
                return False
            import torch

            state = {key: torch.as_tensor(value) for key, value in weights.items()}
            result = model.load_state_dict(state, strict=strict)
            missing = list(getattr(result, "missing_keys", []) or [])
            unexpected = list(getattr(result, "unexpected_keys", []) or [])
            if missing or unexpected:
                self._log(
                    f"explain: WARNING {len(missing)} missing and {len(unexpected)} "
                    f"unexpected keys when loading weights (first: "
                    f"{(missing + unexpected)[:3]})"
                )
            return True
        if hasattr(model, "set_weights"):
            if isinstance(weights, Mapping):
                import numpy as np

                try:
                    keys = sorted(weights, key=int)
                except (TypeError, ValueError):
                    keys = sorted(weights)
                weights = [np.array(weights[key]) for key in keys]
            model.set_weights(weights)
            return True
        if hasattr(model, "coef_") and isinstance(weights, Mapping):
            if "coef" in weights:
                model.coef_ = weights["coef"]
            if "intercept" in weights:
                model.intercept_ = weights["intercept"]
            return True
        return False

    # ----------------------------------------------------------------- bundle

    @property
    def case_ids(self) -> list[str]:
        """The case IDs added so far."""
        return self._writer.case_ids

    def add_case(
        self,
        case_id: Any,
        *,
        label: Any = None,
        prediction: Any = None,
        score: Any = None,
        target: Any = None,
        outcome: Any = None,
        details: Any = None,
    ) -> int:
        """Describe one case; returns its position (1 to 24).

        Args:
            case_id: The site's pseudonymous case ID: letters, digits, ``. _ : -``, at
                most 128 characters, starting with a letter or digit; unique in the run.
            label: Reference label (class name or number; stored as text, max 64).
            prediction: The model's prediction, stored like ``label``.
            score: The model's score for the explained target (finite number).
            target: What the panels explain, e.g. ``"abnormal"``.
            outcome: ``"TP"``, ``"FP"``, ``"FN"``, ``"TN"``, ``"correct"``,
                ``"incorrect"`` or None.
            details: Small flat dict (at most 16 keys) of str, int, float, bool or None;
                never direct identifiers (names, birth dates, record numbers, contact
                data); keys containing such words are refused.

        Returns:
            The case position.
        """
        return self._writer.add_case(
            case_id,
            label=label,
            prediction=prediction,
            score=score,
            target=target,
            outcome=outcome,
            details=details,
        )

    def _figure(self, figure: Any) -> Any:
        plt = _pyplot()
        if figure is not None:
            return figure
        if not plt.get_fignums():
            raise ValueError("there is no open matplotlib figure; draw one first")
        return plt.gcf()

    def add_panel(
        self, case_id: Any, method: Any, title: Any = None, *, figure: Any = None
    ) -> str:
        """Save the current matplotlib figure as a case's next panel, then close it.

        Args:
            case_id: A case added with :meth:`add_case`.
            method: Method name, e.g. ``"integrated_gradients"`` (max 64 characters).
            title: Optional caption (max 255 characters).
            figure: Save this figure instead of the current one.

        Returns:
            The panel's file name in the bundle.
        """
        self._writer.check_panel(case_id, method, title)
        fig = self._figure(figure)
        png = _render_png(fig)
        _pyplot().close(fig)
        return self._writer.add_panel_png(case_id, method, png, title=title)

    def add_summary(self, title: Any, *, figure: Any = None) -> str:
        """Save the current matplotlib figure as a run-level figure, then close it.

        Args:
            title: The figure's title (required, max 255 characters).
            figure: Save this figure instead of the current one.

        Returns:
            The figure's file name in the bundle.
        """
        self._writer.check_summary(title)
        fig = self._figure(figure)
        png = _render_png(fig)
        _pyplot().close(fig)
        return self._writer.add_summary_png(title, png)

    def set_info(
        self,
        *,
        methods: Any = None,
        target: Any = None,
        notes: Any = None,
        metrics: Any = None,
    ) -> None:
        """Record run-level information; arguments left as None keep earlier values.

        Args:
            methods: Method names used (at most 16).
            target: What is explained, e.g. ``"abnormal ECG (class 1)"`` (max 255).
            notes: Free text for reviewers (max 4,000 characters), e.g. the baseline
                used.
            metrics: Flat dict of finite numbers, e.g. ``{"auroc_test": 0.87}``.
        """
        self._writer.set_info(
            methods=methods, target=target, notes=notes, metrics=metrics
        )

    def finish(self) -> dict[str, Any]:
        """Mark the bundle complete and check it; later calls return the same result.

        The sandbox calls this after the script ends, so scripts need not call it.

        Returns:
            The bundle description (as in ``cases.json``).
        """
        data = self._writer.finish()
        self._log(f"explain: bundle complete: {_bundle.describe(data)}")
        return data

    finalize = finish


# --------------------------------------------------------------------------- runner


def _cgroup_cpus() -> int | None:
    try:
        with builtins.open("/sys/fs/cgroup/cpu.max", encoding="ascii") as handle:
            quota, period = handle.read().split()[:2]
        if quota != "max":
            return max(1, math.ceil(int(quota) / int(period)))
        return None
    except (OSError, ValueError):
        pass
    try:
        with builtins.open(
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us", encoding="ascii"
        ) as f:
            quota_us = int(f.read())
        with builtins.open(
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us", encoding="ascii"
        ) as f:
            period_us = int(f.read())
        if quota_us > 0 and period_us > 0:
            return max(1, math.ceil(quota_us / period_us))
    except (OSError, ValueError):
        pass
    return None


def thread_limit() -> int | None:
    """CPU threads to use: ``$SWARMMED_EXPLAIN_THREADS``, else the cgroup CPU quota."""
    value = os.environ.get(THREADS_ENV, "").strip()
    if value:
        try:
            return max(1, int(value))
        except ValueError:
            print(f"explain: ignoring {THREADS_ENV}={value!r}", flush=True)
    return _cgroup_cpus()


def _limit_threads() -> None:
    limit = thread_limit()
    if limit is None:
        return
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(name, str(limit))
    try:
        import torch
    except ImportError:
        return
    if torch.get_num_threads() > limit:
        torch.set_num_threads(limit)


def run_explain_script(
    script: str | os.PathLike[str] = SANDBOX_SCRIPT,
    manifest_file: str | os.PathLike[str] = SANDBOX_MANIFEST,
    output_dir: str | os.PathLike[str] = SANDBOX_OUTPUT_DIR,
) -> dict[str, Any]:
    """Run ``explainability.py`` with ``explain`` injected and finish its bundle.

    The script runs as ``__main__`` through ``runpy.run_path``, so tracebacks show its
    own file name and line numbers, and an exception propagates (the bundle then stays
    incomplete and is not imported). ``sys.exit(0)`` counts as success. CPU threads are
    limited to ``$SWARMMED_EXPLAIN_THREADS`` or the container's CPU quota, and the
    matplotlib backend is Agg.

    Args:
        script: Path of the script.
        manifest_file: Path of the data manifest (JSON).
        output_dir: New or empty bundle directory.

    Returns:
        The finished bundle description.
    """
    _limit_threads()
    _pyplot()
    explain = ExplainHelper(manifest_file, output_dir)
    path = os.path.abspath(os.fspath(script))
    saved_argv = sys.argv[:]
    sys.argv = [path]
    try:
        runpy.run_path(path, init_globals={"explain": explain}, run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise
    finally:
        sys.argv = saved_argv
    return explain.finish()


def wrapper_source(
    script: str = SANDBOX_SCRIPT,
    manifest_file: str = SANDBOX_MANIFEST,
    output_dir: str = SANDBOX_OUTPUT_DIR,
) -> str:
    """Return the Python source that starts an explanation run.

    The defaults are the sandbox paths: the Hub writes this source as the sandbox's
    ``script.py`` next to ``explainability.py`` and a copy of the ``swarmmed_explain``
    package, and ``run_local`` runs the same source with local paths.
    """
    return (
        "from swarmmed_explain.helper import run_explain_script\n"
        "\n"
        f"run_explain_script({os.fspath(script)!r}, {os.fspath(manifest_file)!r}, "
        f"{os.fspath(output_dir)!r})\n"
    )


def copy_package(destination: str | os.PathLike[str]) -> str:
    """Copy the package's runtime modules to ``destination/swarmmed_explain/``.

    The Hub does this for every sandbox run (next to ``explainability.py`` and the
    wrapper from :func:`wrapper_source`), and ``run_local`` does the same, so scripts
    can ``import swarmmed_explain`` in both places. Tests and documentation are not
    copied.

    Args:
        destination: An existing directory without a ``swarmmed_explain`` folder.

    Returns:
        The path of the new package folder.
    """
    source = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(os.fspath(destination), "swarmmed_explain")
    os.mkdir(target)
    for name in RUNTIME_FILES:
        shutil.copyfile(os.path.join(source, name), os.path.join(target, name))
    return target
