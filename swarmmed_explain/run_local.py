r"""Run ``explainability.py`` locally the way the SwarmMedHub sandbox runs it.

Usage::

    python -m swarmmed_explain.run_local --script explainability.py --data DATA_DIR \
        (--model FL_global_model.pt | --results RESULTS_DIR) --out BUNDLE_DIR

The data folder is listed under its relative paths (``fl-client-1/test.npz``), result
files under ``results/...`` (``--model FILE`` becomes ``results/<client>/<file name>``;
``--results DIR`` should hold one folder per participant, as the Hub stores a job's
results). The script is copied into a fresh working directory next to a copy of this
package and run in a new Python process with ``explain`` injected, exactly as in the
sandbox, so a script that needs other local files fails here as it would in the Hub. The
bundle written to ``--out`` is then validated.

This is also the route for sites that cannot use the Hub sandbox, for example when the
model needs a GPU: the bundle can be imported into the Hub afterwards with
``swarmed results explain-import``. Unlike the sandbox, nothing here limits memory or
network access; ``--threads`` and ``--timeout`` approximate the CPU and time limits.

Exit codes: 0 bundle written and valid; 1 the script failed or timed out; 2 bad
arguments or an invalid bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence

from . import bundle as _bundle
from .helper import SANDBOX_SCRIPT, THREADS_ENV, copy_package, wrapper_source

SANDBOX_TIMEOUT_SECONDS = 900
_CLIENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _walk_files(root: str) -> Iterator[tuple[str, str]]:
    """Yield ``(relative path, absolute path)`` of files below ``root``, sorted.

    Hidden files and folders (names starting with ``.``) are skipped, as in the Hub.
    """
    for current, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(current, name)
            if not os.path.isfile(full):
                continue
            relative = os.path.relpath(full, root).replace(os.sep, "/")
            yield relative, os.path.abspath(full)


def build_manifest(
    data_dir: str,
    *,
    results_dir: str | None = None,
    model_file: str | None = None,
    client_name: str = "fl-client-1",
) -> dict[str, str]:
    """Build a local manifest that mirrors the Hub's.

    Args:
        data_dir: The site's data folder; files keep their relative paths.
        results_dir: A job's results folder; files are listed as ``results/<path>``
            (``.py`` and ``.pyc`` files are skipped, as in the Hub).
        model_file: One model file, listed as ``results/<client_name>/<file name>``.
        client_name: Participant folder for ``model_file``.

    Returns:
        ``{relative path: absolute file path}``.

    Raises:
        ValueError: If a path is listed twice or the client name is not a plain name.
        FileNotFoundError: If a folder or file does not exist.
    """
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"data folder {data_dir} does not exist")
    manifest = dict(_walk_files(data_dir))
    results: dict[str, str] = {}
    if results_dir is not None:
        if not os.path.isdir(results_dir):
            raise FileNotFoundError(f"results folder {results_dir} does not exist")
        for relative, full in _walk_files(results_dir):
            if not relative.endswith((".py", ".pyc")):
                results[f"results/{relative}"] = full
    if model_file is not None:
        if not os.path.isfile(model_file):
            raise FileNotFoundError(f"model file {model_file} does not exist")
        if _CLIENT_RE.fullmatch(client_name) is None:
            raise ValueError(f"client name {client_name!r} is not a plain folder name")
        key = f"results/{client_name}/{os.path.basename(model_file)}"
        results[key] = os.path.abspath(model_file)
    for key, full in results.items():
        if key in manifest:
            raise ValueError(
                f"{key} is both a data file and a result file; rename the data "
                "folder's 'results' folder"
            )
        manifest[key] = full
    return manifest


def _prepare_output(path: str, force: bool) -> None:
    if not os.path.exists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        raise ValueError(f"{path} is not a directory")
    entries = os.listdir(path)
    if not entries:
        return
    if not force:
        raise ValueError(
            f"{path} is not empty; choose a new folder or pass --force to replace an "
            "earlier bundle"
        )
    for name in entries:
        full = os.path.join(path, name)
        is_bundle_file = _bundle.is_bundle_file_name(name) or name == ".cases.json.tmp"
        if is_bundle_file and (os.path.islink(full) or os.path.isfile(full)):
            os.unlink(full)
    if os.listdir(path):
        raise ValueError(f"{path} holds files that are not part of a bundle")


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m swarmmed_explain.run_local",
        description="Run explainability.py locally the way the SwarmMedHub sandbox "
        "does, and validate the bundle it writes.",
    )
    parser.add_argument("--script", required=True, help="the explainability.py to run")
    parser.add_argument(
        "--data", required=True, help="the site's data folder, as registered in the Hub"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--model", help="a model file; listed as results/<client>/<file name>"
    )
    source.add_argument(
        "--results",
        help="a job's results folder with one folder per participant; listed as "
        "results/<participant>/<file>",
    )
    parser.add_argument("--out", required=True, help="bundle folder (new or empty)")
    parser.add_argument(
        "--client",
        default="fl-client-1",
        help="participant folder for --model (default: fl-client-1)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        help="CPU threads for the script (the Hub sandbox has 2 CPUs by default)",
    )
    parser.add_argument(
        "--timeout", type=float, help="stop the script after this many seconds"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an earlier bundle in --out (only bundle files are removed)",
    )
    args = parser.parse_args(argv)

    script = os.path.abspath(args.script)
    out = os.path.abspath(args.out)
    try:
        if not os.path.isfile(script):
            raise FileNotFoundError(f"script {args.script} does not exist")
        if args.threads is not None and args.threads < 1:
            raise ValueError("--threads must be at least 1")
        manifest = build_manifest(
            os.path.abspath(args.data),
            results_dir=None if args.results is None else os.path.abspath(args.results),
            model_file=None if args.model is None else os.path.abspath(args.model),
            client_name=args.client,
        )
        _prepare_output(out, args.force)
    except (OSError, ValueError) as exc:
        print(f"run_local: {exc}", file=sys.stderr)
        return 2

    n_results = sum(1 for key in manifest if key.startswith("results/"))
    print(
        f"run_local: {len(manifest) - n_results} data files, {n_results} result files",
        flush=True,
    )
    workdir = tempfile.mkdtemp(prefix="swarmmed_explain_run_")
    try:
        manifest_path = os.path.join(workdir, "data_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=1)
        shutil.copyfile(script, os.path.join(workdir, SANDBOX_SCRIPT))
        copy_package(workdir)
        env = dict(os.environ)
        env["MPLBACKEND"] = "Agg"
        if args.threads is not None:
            env[THREADS_ENV] = str(args.threads)
        command = [
            sys.executable,
            "-c",
            wrapper_source(SANDBOX_SCRIPT, manifest_path, out),
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, cwd=workdir, env=env, timeout=args.timeout, check=False
            )
        except subprocess.TimeoutExpired:
            print(
                f"run_local: stopped the script after {args.timeout:g} s; the bundle "
                f"in {out} is incomplete",
                file=sys.stderr,
            )
            return 1
        elapsed = time.monotonic() - started
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if completed.returncode != 0:
        print(
            f"run_local: {os.path.basename(script)} failed with exit code "
            f"{completed.returncode}; the bundle in {out} is incomplete",
            file=sys.stderr,
        )
        return 1
    try:
        data = _bundle.validate_bundle(out)
    except _bundle.BundleError as exc:
        print(f"run_local: invalid bundle in {out}: {exc}", file=sys.stderr)
        return 2
    print(f"run_local: bundle OK: {_bundle.describe(data)} in {out} ({elapsed:.1f} s)")
    if elapsed > SANDBOX_TIMEOUT_SECONDS:
        print(
            f"run_local: note: the Hub stops explanation runs after "
            f"{SANDBOX_TIMEOUT_SECONDS} s by default (SWARMMEDHUB_EXPLAIN_TIMEOUT)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
