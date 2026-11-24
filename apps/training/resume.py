"""Resume an interrupted swarm run from the newest checkpoint at this site.

NVIDIA FLARE keeps no state for a running swarm job that survives the loss of
the FL server: a restarted or replacement server cannot continue the job. What
survives is on the training sites' disks:

* ``swarmmed_resume_checkpoint.pt``, written by
  :class:`swarmmed_resume.ResumeCheckpointSaver` at every training site at the
  start of each round. It holds the model that round started from.
* ``FL_global_model.pt``, written by FLARE's persistor at the site that
  aggregated a round. The site's job log names the round.

A resumed run is a new FLARE job that starts at the interrupted round
(``start_round``) from the newest of these checkpoints. Only PyTorch jobs can be
resumed, because the checkpoint is loaded by FLARE's ``PTFileModelPersistor``.

Every job also carries the plan of the whole run in ``config/swarmmed_run.json``
of each of its apps (:func:`write_run_file`), and the round-start checkpoints
copy it, so that a site that did not run the FL server knows the run's total.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .utils import extract_swarm_round_plan

# Names shared with swarmmed_resume.py, which runs inside the FLARE job.
CHECKPOINT_FILE = "swarmmed_resume_checkpoint.pt"
METADATA_FILE = "swarmmed_resume_checkpoint.json"
METADATA_KEY = "swarmmed_resume"
RUN_FILE = "swarmmed_run.json"
RUNTIME_MODULE = Path(__file__).with_name("swarmmed_resume.py")
COMPONENT_ID = "swarmmed_resume_checkpoint"
COMPONENT_PATH = "swarmmed_resume.ResumeCheckpointSaver"

GLOBAL_MODEL_FILE = "FL_global_model.pt"
START_CHECKPOINT_FILE = "swarmmed_resume_start.pt"
RESUME_RECORD_FILE = "swarmmed_resume.json"
SERVER_CONFIG_FILE = "config_fed_server.json"
RUN_FILE_FORMAT = 1

# A FLARE job ID as FLARE writes it, and what a job may be named by: its ID or a
# prefix of at least 8 characters. Nothing else is looked up on disk.
FLARE_JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
JOB_ID_PREFIX_RE = re.compile(r"^[0-9a-f][0-9a-f-]{7,35}$")

# Logged by FLARE's swarm client controller when a site writes FL_global_model.pt:
# as the round's aggregator, and when it receives the run's final model.
SAVED_ROUND_RES = (
    re.compile(r"Saving result of round (\d+)"),
    re.compile(r"Got last from client \S+ with metric .* at round (\d+)"),
)

SOURCE_ROUND_START = "round-start"
SOURCE_GLOBAL_MODEL = "global-model"
SOURCE_GIVEN = "given"


def resume_checkpoints_enabled() -> bool:
    """Return whether new jobs save a resume checkpoint at every site each round.

    On by default. ``SWARMMEDHUB_RESUME_CHECKPOINTS=false`` turns it off, for
    example when the extra disk write per round matters.
    """
    raw = os.getenv("SWARMMEDHUB_RESUME_CHECKPOINTS", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass
class ResumeCheckpoint:
    """A model file a run can be resumed from."""

    path: str
    next_round: int | None
    site: str
    source: str
    weights_format: str = "torch"
    modified_at: float = 0.0

    def as_dict(self) -> dict:
        """Return the checkpoint as a plain dict, for CLI output and records."""
        return asdict(self)


@dataclass
class ResumePlan:
    """What a resumed job starts from."""

    checkpoint: ResumeCheckpoint
    start_round: int
    # Rounds of the whole run; None means the same count a new job would get.
    total_rounds: int | None = None
    from_job_identifier: str = ""
    from_flare_job: str = ""

    @property
    def remaining_rounds(self) -> int:
        """Rounds the resumed job runs."""
        return int(self.total_rounds or 0) - self.start_round


@dataclass
class RunPlan:
    """The rounds of a whole run, as one of its jobs' files at this Hub names them."""

    total_rounds: int
    start_round: int
    # The file they were read from, relative to the network's workspace folder.
    origin: str


def job_folders(workspace_root, job_id: str) -> list[Path]:
    """Return this Hub's folders of a FLARE job.

    FLARE runs a job in ``<study>/prod_00/<participant>/<job>`` of the network's
    workspace folder (``<study>/prod_00/<job>`` in older layouts). Both are found
    by globs of fixed depth, not by a walk of the workspace.

    Args:
        workspace_root: The network's ``workspace`` folder at this Hub.
        job_id: A FLARE job ID, or a prefix of one with at least 8 characters.
            Anything else gives no folders, without a look at the disk.

    Returns:
        list[Path]: The folders whose name is a FLARE job ID that starts with
        ``job_id``, sorted.
    """
    prefix = str(job_id or "").strip().lower()
    if not JOB_ID_PREFIX_RE.fullmatch(prefix):
        return []
    root = Path(workspace_root)
    found = set()
    for pattern in (f"*/prod_00/*/{prefix}*", f"*/prod_00/{prefix}*"):
        for path in root.glob(pattern):
            if (
                FLARE_JOB_ID_RE.fullmatch(path.name)
                and path.name.startswith(prefix)
                and path.is_dir()
            ):
                found.add(path)
    return sorted(found)


def _whole_rounds(total_rounds, start_round) -> tuple[int, int] | None:
    """Return the two values if both are ints with 0 <= start < total."""
    values = (total_rounds, start_round)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return None
    return values if 0 <= start_round < total_rounds else None


def _recorded_rounds(content: dict) -> tuple:
    return content.get("total_rounds"), content.get("start_round")


def _controller_rounds(content: dict) -> tuple:
    return extract_swarm_round_plan(content) or (None, None)


def read_run_plan(workspace_root, flare_uuid: str) -> RunPlan | None:
    """Return the plan of the run a job belongs to, from the job's files here.

    Only the job's own folders (:func:`job_folders`) are read, in this order:

    1. ``app_*/config/swarmmed_run.json``, in every app of a job submitted by
       SwarmMed 1.0.6 or later, at every site.
    2. ``app_server/config/config_fed_server.json``, the swarm controller's
       ``start_round`` and ``num_rounds``, at the Hub that ran the job's FL
       server. FLARE keeps this folder only for running and interrupted jobs.
    3. ``app_*/config/swarmmed_resume.json``, written into a job resumed by
       SwarmMed 1.0.3 to 1.0.5, at its starting site.

    Code in the FL client containers can write to the job folders, so only
    ints with ``0 <= start_round < total_rounds`` are taken.

    Args:
        workspace_root: The network's ``workspace`` folder at this Hub.
        flare_uuid: The FLARE job ID.

    Returns:
        RunPlan | None: The first plan found, or None.
    """
    root = Path(workspace_root)
    folders = job_folders(root, flare_uuid)
    for pattern, rounds_in in (
        (f"app_*/config/{RUN_FILE}", _recorded_rounds),
        (f"app_server/config/{SERVER_CONFIG_FILE}", _controller_rounds),
        (f"app_*/config/{RESUME_RECORD_FILE}", _recorded_rounds),
    ):
        for folder in folders:
            for path in sorted(folder.glob(pattern)):
                try:
                    content = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                if not isinstance(content, dict):
                    continue
                rounds = _whole_rounds(*rounds_in(content))
                if rounds:
                    return RunPlan(*rounds, origin=str(path.relative_to(root)))
    return None


def _site_from_app_dir(app_dir: Path) -> str:
    name = app_dir.name
    return name[4:] if name.startswith("app_") else name


def _last_saved_round(job_dir: Path) -> int | None:
    """Return the last round this site saved as aggregator, from its job log."""
    last_round = None
    for log_name in ("log.txt", "log_fl.txt"):
        log_path = job_dir / log_name
        if not log_path.is_file():
            continue
        try:
            text = log_path.read_text(errors="ignore")
        except OSError:
            continue
        for pattern in SAVED_ROUND_RES:
            for match in pattern.finditer(text):
                value = int(match.group(1))
                if last_round is None or value > last_round:
                    last_round = value
        if last_round is not None:
            return last_round
    return last_round


def _read_metadata(app_dir: Path) -> dict:
    try:
        payload = json.loads((app_dir / METADATA_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def find_resume_checkpoints(
    workspace_root, flare_job_uuid: str
) -> list[ResumeCheckpoint]:
    """List the checkpoints of a job under a site's workspace, newest round first.

    Args:
        workspace_root: The network's ``workspace`` folder at this Hub.
        flare_job_uuid: The FLARE job ID of the interrupted run.

    Returns:
        list[ResumeCheckpoint]: Checkpoints with a known round first, highest
        round first; a round-start checkpoint wins over a global model of the
        same round.
    """
    root = Path(workspace_root)
    if not flare_job_uuid or not root.is_dir():
        return []

    found: list[ResumeCheckpoint] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        if flare_job_uuid not in current:
            continue
        app_dir = Path(current)
        if CHECKPOINT_FILE in files:
            metadata = _read_metadata(app_dir)
            next_round = metadata.get("next_round")
            found.append(
                ResumeCheckpoint(
                    path=str(app_dir / CHECKPOINT_FILE),
                    next_round=int(next_round) if isinstance(next_round, int) else None,
                    site=str(metadata.get("site") or _site_from_app_dir(app_dir)),
                    source=SOURCE_ROUND_START,
                    weights_format=str(metadata.get("weights_format") or "torch"),
                    modified_at=(app_dir / CHECKPOINT_FILE).stat().st_mtime,
                )
            )
        if GLOBAL_MODEL_FILE in files:
            saved_round = _last_saved_round(app_dir.parent)
            found.append(
                ResumeCheckpoint(
                    path=str(app_dir / GLOBAL_MODEL_FILE),
                    next_round=saved_round + 1 if saved_round is not None else None,
                    site=_site_from_app_dir(app_dir),
                    source=SOURCE_GLOBAL_MODEL,
                    modified_at=(app_dir / GLOBAL_MODEL_FILE).stat().st_mtime,
                )
            )

    def rank(item: ResumeCheckpoint):
        return (
            item.next_round is not None,
            item.next_round if item.next_round is not None else -1,
            item.source == SOURCE_ROUND_START,
            item.modified_at,
        )

    return sorted(found, key=rank, reverse=True)


def describe_given_checkpoint(path: str, start_round: int | None) -> ResumeCheckpoint:
    """Build a :class:`ResumeCheckpoint` for a file named by the operator."""
    checkpoint = Path(path).expanduser().resolve()
    if not checkpoint.is_file():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint}")
    metadata = (
        _read_metadata(checkpoint.parent) if checkpoint.name == CHECKPOINT_FILE else {}
    )
    next_round = start_round
    if next_round is None and isinstance(metadata.get("next_round"), int):
        next_round = metadata["next_round"]
    return ResumeCheckpoint(
        path=str(checkpoint),
        next_round=next_round,
        site=str(metadata.get("site") or ""),
        source=SOURCE_GIVEN,
        weights_format=str(metadata.get("weights_format") or "torch"),
        modified_at=checkpoint.stat().st_mtime,
    )


def load_checkpoint_weights(path: str) -> dict:
    """Load the model weights of a checkpoint as a dict of CPU tensors.

    The file is always loaded with ``weights_only=True``. Code that runs in the
    FL client containers, the study's training script included, can write to
    the job folders, so no checkpoint is trusted to be unpickled in full at
    the Hub.

    Args:
        path: The checkpoint file.

    Returns:
        dict: Parameter name to tensor.
    """
    import torch

    try:
        data = torch.load(path, map_location="cpu", weights_only=True)
    except Exception:
        raise ValueError(
            f"{path} could not be loaded as plain tensors (torch weights_only load "
            "failed). Resume from a round-start checkpoint "
            f"({CHECKPOINT_FILE}) instead."
        ) from None

    if isinstance(data, dict) and isinstance(data.get("model"), dict):
        weights = data["model"]
    elif isinstance(data, dict):
        weights = {k: v for k, v in data.items() if hasattr(v, "shape")}
    else:
        raise ValueError(f"{path} does not contain a model state dict.")
    if not weights:
        raise ValueError(f"{path} contains no model weights.")
    return {name: torch.as_tensor(value).cpu() for name, value in weights.items()}


def _app_folder_for(job_path: Path, site: str) -> Path:
    """Return the app folder that FLARE deploys to ``site`` in an exported job."""
    meta_path = job_path / "meta.json"
    try:
        deploy_map = json.loads(meta_path.read_text()).get("deploy_map", {})
    except (OSError, ValueError):
        deploy_map = {}
    for app_name, sites in deploy_map.items():
        if site in (sites or []):
            return job_path / app_name
    candidate = job_path / f"app_{site}"
    if candidate.is_dir():
        return candidate
    raise LookupError(f"The exported job has no app for site '{site}'.")


def add_resume_checkpoint_component(job_path) -> list[str]:
    """Add the round-start checkpoint component to every client app of a job.

    The component's module must already be in each client app's ``custom``
    folder; it is copied there if missing.

    Returns:
        list[str]: The app folders that were changed.
    """
    job_path = Path(job_path)
    changed = []
    for config_path in sorted(job_path.glob("app_*/config/config_fed_client.json")):
        app_dir = config_path.parent.parent
        config = json.loads(config_path.read_text())
        components = config.setdefault("components", [])
        if not any(
            c.get("id") == COMPONENT_ID for c in components if isinstance(c, dict)
        ):
            components.append({"id": COMPONENT_ID, "path": COMPONENT_PATH, "args": {}})
            config_path.write_text(json.dumps(config, indent=4))
        custom_dir = app_dir / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)
        module_target = custom_dir / RUNTIME_MODULE.name
        if not module_target.exists():
            shutil.copyfile(RUNTIME_MODULE, module_target)
        changed.append(app_dir.name)
    return changed


def write_run_file(
    job_path,
    *,
    network_id,
    total_rounds: int,
    start_round: int,
    from_flare_job: str = "",
) -> list[str]:
    """Write the plan of the whole run into every app of an exported job.

    Each app's ``config/swarmmed_run.json``, the server's included, names the
    network, the run's total number of rounds, the round the job starts at and
    the job it resumes, if any. FLARE deploys the whole app folder to each site,
    and :class:`swarmmed_resume.ResumeCheckpointSaver` copies the plan into each
    round-start checkpoint, so every training site knows the run's total.

    Args:
        job_path: Folder of the exported job.
        network_id: The network's identifier, the same at every site.
        total_rounds: Rounds of the whole run.
        start_round: The round the job starts at; 0 for a new run.
        from_flare_job: The FLARE job ID of the run's interrupted job.

    Returns:
        list[str]: The app folders that were changed.
    """
    run = {
        "format": RUN_FILE_FORMAT,
        "network": str(network_id),
        "total_rounds": int(total_rounds),
        "start_round": int(start_round),
        "num_rounds": int(total_rounds) - int(start_round),
        "from_flare_job": str(from_flare_job or ""),
    }
    changed = []
    for config_dir in sorted(Path(job_path).glob("app_*/config")):
        if config_dir.is_dir():
            (config_dir / RUN_FILE).write_text(json.dumps(run, indent=4))
            changed.append(config_dir.parent.name)
    return changed


def ship_resume_checkpoint(job_path, starting_client: str, plan: ResumePlan) -> Path:
    """Put the resume checkpoint into the starting client's app of a job.

    FLARE's ``PTFileModelPersistor`` loads ``APP_CKPT`` from the app's
    ``config/environment.json`` at the start of the job, and the starting
    client sends that model to every site for the first resumed round.

    Returns:
        Path: The checkpoint file inside the job.
    """
    import torch

    app_dir = _app_folder_for(Path(job_path), starting_client)
    weights = load_checkpoint_weights(plan.checkpoint.path)
    target = app_dir / "custom" / START_CHECKPOINT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"model": weights}, target)

    config_dir = app_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    environment_path = config_dir / "environment.json"
    environment = {}
    if environment_path.exists():
        try:
            environment = json.loads(environment_path.read_text()) or {}
        except ValueError:
            environment = {}
    environment["APP_CKPT"] = f"custom/{START_CHECKPOINT_FILE}"
    environment_path.write_text(json.dumps(environment, indent=4))

    record = {
        "start_round": plan.start_round,
        "total_rounds": plan.total_rounds,
        "from_job": plan.from_job_identifier,
        "from_flare_job": plan.from_flare_job,
        "checkpoint_source": plan.checkpoint.source,
        "checkpoint_site": plan.checkpoint.site,
        "checkpoint_round": plan.checkpoint.next_round,
        "num_tensors": len(weights),
    }
    (config_dir / RESUME_RECORD_FILE).write_text(json.dumps(record, indent=4))
    return target
