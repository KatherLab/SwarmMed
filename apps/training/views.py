import ast
import json
import os
import re
import shutil

from common.utils import get_safe_slug
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from logs.logger import get_logger
from network.models import SwarmNetwork, UserCurrentNetwork
from project.decorators import (
    project_context_required,
    project_membership_required,
)
from project.models import UserCurrentProject

from .models import TrainingJob
from .utils import download_s3_folder

logger = get_logger()


def _build_training_status_payload(current_network, job):
    if not job:
        return {
            "status": "idle",
            "progress": 0,
            "duration": "-",
            "eta": "-",
            "job_id": None,
            "created_at": timezone.now().isoformat(),
        }

    info = get_training_progress_info(job, current_network)
    return {
        "status": info["status"],
        "progress": info["progress"],
        "duration": info["duration"],
        "eta": info["eta"],
        "job_id": job.flare_job_id,
        "created_at": timezone.now().isoformat(),
    }


def _resolve_admin_startup_dir(current_network) -> str | None:
    if current_network.admin_startup_dir and os.path.exists(
        current_network.admin_startup_dir
    ):
        return current_network.admin_startup_dir

    override = os.environ.get("SWARMCLOUD_NVFLARE_ADMIN_DIR", "").strip()
    if override and os.path.exists(override):
        return override

    project_name = get_safe_slug(
        current_network.project.title, current_network.project.identifier
    ).replace("-", "_")
    admin_user_dir = os.path.join(
        "workspaces",
        str(current_network.project.identifier),
        str(current_network.identifier),
        "workspace",
        project_name,
        "prod_00",
        "admin@nvidia.com",
        "startup",
    )
    if os.path.exists(admin_user_dir):
        return admin_user_dir

    prod_00_dir = os.path.join(
        "workspaces",
        str(current_network.project.identifier),
        str(current_network.identifier),
        "workspace",
        project_name,
        "prod_00",
    )
    if os.path.exists(prod_00_dir):
        for item in sorted(os.listdir(prod_00_dir)):
            if not item.startswith("admin-"):
                continue
            cand = os.path.join(prod_00_dir, item, "startup")
            if os.path.exists(cand):
                return cand

    return None


def _resolve_admin_session_target(current_network) -> tuple[str, str] | None:
    startup_dir = _resolve_admin_startup_dir(current_network)
    if not startup_dir:
        return None

    startup_dir = os.path.abspath(startup_dir)
    session_dir = startup_dir
    if os.path.basename(startup_dir.rstrip(os.sep)) == "startup":
        session_dir = os.path.dirname(startup_dir.rstrip(os.sep))

    startup_subdir = os.path.join(session_dir, "startup")
    if not os.path.isdir(startup_subdir):
        if os.path.isdir(startup_dir):
            startup_subdir = startup_dir
            session_dir = os.path.dirname(startup_dir.rstrip(os.sep))
        else:
            return None

    admin_name = "admin@nvidia.com"
    try:
        admin_dir_name = os.path.basename(session_dir.rstrip(os.sep))
        if admin_dir_name and "@" in admin_dir_name:
            admin_name = admin_dir_name
    except Exception:
        admin_name = "admin@nvidia.com"

    return (admin_name, session_dir)


def _parse_nvflare_jobs(response):
    if isinstance(response, dict):
        if "jobs" in response and isinstance(response["jobs"], list):
            return response["jobs"]
        if "data" in response and isinstance(response["data"], list):
            return response["data"]
        if "job_id" in response:
            return [response]
    if isinstance(response, list):
        return response

    text = str(response or "")
    jobs = []
    uuid_re = re.compile(r"([0-9a-f-]{36})")
    status_re = re.compile(r"\b(RUNNING|COMPLETED|FAILED|STOPPED)\b", re.I)
    for line in text.splitlines():
        uid_match = uuid_re.search(line)
        status_match = status_re.search(line)
        if uid_match:
            jobs.append(
                {
                    "job_id": uid_match.group(1),
                    "status": status_match.group(1).upper()
                    if status_match
                    else "UNKNOWN",
                }
            )
    return jobs


def _select_nvflare_job(jobs):
    if not jobs:
        return None

    def status_of(job):
        return (
            str(
                job.get("status")
                or job.get("job_status")
                or job.get("state")
                or ""
            )
            .upper()
            .strip()
        )

    running = [j for j in jobs if status_of(j) == "RUNNING"]
    if running:
        return running[0]
    return jobs[0]


def _nvflare_status_payload(current_network):
    cache_key = f"nvflare_status_payload_{current_network.identifier}"
    cached_payload = cache.get(cache_key)
    if cached_payload is not None:
        return cached_payload

    admin_target = _resolve_admin_session_target(current_network)
    if not admin_target:
        cache.set(cache_key, None, 3)
        return None

    try:
        from nvflare.fuel.flare_api.flare_api import new_secure_session

        admin_name, admin_dir = admin_target
        sess = new_secure_session(
            username=admin_name, startup_kit_location=admin_dir
        )
        response = sess.api.do_command("list_jobs")
        try:
            sess.close()
        except Exception:
            pass

        jobs = _parse_nvflare_jobs(response)
        job = _select_nvflare_job(jobs)
        if not job:
            cache.set(cache_key, None, 3)
            return None

        status = (
            str(
                job.get("status")
                or job.get("job_status")
                or job.get("state")
                or "UNKNOWN"
            )
            .upper()
            .strip()
        )
        job_id = job.get("job_id") or job.get("id") or "unknown"

        if status in {"", "UNKNOWN", "N/A", "NONE"}:
            cache.set(cache_key, None, 3)
            return None

        progress = 0
        if status in {"COMPLETED", "STOPPED"}:
            progress = 100

        payload = {
            "status": status.title(),
            "progress": progress,
            "duration": "-",
            "eta": "-",
            "job_id": job_id,
            "created_at": timezone.now().isoformat(),
        }
        cache.set(cache_key, payload, 3)
        return payload
    except Exception:
        cache.set(cache_key, None, 3)
        return None


def _tail_text(file_path: str, max_bytes: int = 2048 * 1024) -> str:
    """Read up to the last max_bytes of a text file (decoded safely)."""
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            start = max(0, end - max_bytes)
            f.seek(start)
            data = f.read()
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _find_latest_training_log(
    project_id: str,
    network_id: str,
    job_uuid: str,
    cache_ttl_seconds: int = 60,
) -> str | None:
    """Locate the most relevant log file for a given NVFlare job.

    Uses a short cache to avoid repeated directory walks.
    """
    cache_key = f"training_log_path_{project_id}_{network_id}_{job_uuid}"
    cached_path = cache.get(cache_key)
    if cached_path and os.path.exists(cached_path):
        return cached_path

    workspace_root = os.path.join(
        "workspaces", project_id, network_id, "workspace"
    )
    if not os.path.exists(workspace_root):
        return None

    latest_log = None
    for root, dirs, files in os.walk(workspace_root):
        # Prune obviously irrelevant/hidden directories.
        dirs[:] = [
            d for d in dirs if not d.startswith(".") and d != "__pycache__"
        ]

        if job_uuid in root:
            for cand in ("log_fl.txt", "log.txt"):
                if cand in files:
                    latest_log = os.path.join(root, cand)
                    break
        if latest_log:
            break

    if latest_log and os.path.exists(latest_log):
        cache.set(cache_key, latest_log, cache_ttl_seconds)
        return latest_log

    return None


def get_user_project(request):
    """
    Helper function to retrieve the user's currently active project.
    """
    try:
        user_current_project = UserCurrentProject.objects.get(
            user=request.user
        )
        if not user_current_project.project:
            return None, False
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


def format_duration(seconds):
    """
    Helper to convert seconds into a human-readable string like '2m 15s'.
    """
    if seconds < 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def get_training_progress_info(training_job, current_network):
    """
    Helper function to calculate progress, duration, and ETA for a training job.
    Cached for 30 seconds to reduce filesystem overhead during active training.
    """
    # Cache key based on job ID and status
    cache_key = f"training_progress_{training_job.id}_{training_job.status}"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    training_progress = 0
    training_status = training_job.status.title()
    is_training_running = False
    duration_str = "-"
    eta_str = "-"

    # Clean the NVFlare job ID.
    job_uuid = str(training_job.flare_job_id)
    match = re.search(r"Submitted job:\s*([0-9a-f-]+)", job_uuid)
    if match:
        job_uuid = match.group(1)
    else:
        match_uuid = re.search(r"([0-9a-f-]{36})", job_uuid)
        if match_uuid:
            job_uuid = match_uuid.group(1)

    if training_job.status == "RUNNING":
        is_training_running = True

        # Fast path: if Celery recently updated progress, use it.
        have_cached_progress = False
        try:
            if training_job.progress_updated_at:
                age = (
                    timezone.now() - training_job.progress_updated_at
                ).total_seconds()
                if age <= 60 and training_job.progress_percent is not None:
                    training_progress = int(training_job.progress_percent)
                    if training_progress >= 100:
                        training_progress = 99
                    have_cached_progress = True
        except (TypeError, ValueError):
            have_cached_progress = False

        try:
            # STEP A: Find out how many rounds the user configured.
            total_rounds = 10
            server_cfg_path = os.path.join(
                "workspaces",
                str(training_job.project.identifier),
                str(current_network.identifier),
                "job",
                "app_server",
                "config",
                "config_fed_server.json",
            )
            if os.path.exists(server_cfg_path):
                with open(server_cfg_path) as f:
                    cfg = json.load(f)
                    for workflow in cfg.get("workflows", []):
                        if workflow.get("id") == "swarm_controller":
                            total_rounds = int(
                                workflow.get("args", {}).get("num_rounds", 10)
                            )
                            break

            # STEP B: Count how many rounds have actually finished.
            rounds_finished = 0
            workspace_dir = os.path.join(
                "workspaces",
                str(training_job.project.identifier),
                str(current_network.identifier),
                "workspace",
            )
            ended = False

            # If we already have a progress value from Celery, avoid doing expensive log work.
            if have_cached_progress:
                ended = False
                rounds_finished = training_job.rounds_finished or 0

            # Prefer a direct log if we can find it; otherwise fall back to scanning.
            preferred_log = _find_latest_training_log(
                str(training_job.project.identifier),
                str(current_network.identifier),
                job_uuid,
                cache_ttl_seconds=60,
            )

            round_re = re.compile(r"finished training round (\d+)")

            def scan_log_tail(fpath: str) -> None:
                nonlocal ended, rounds_finished
                data = _tail_text(fpath)
                if not data:
                    return
                if (
                    "ending workflow" in data and "swarm_controller" in data
                ) or ("ending workflow controller" in data) or (
                    "child worker process finished with RC 0" in data
                ):
                    ended = True
                for m in round_re.finditer(data):
                    rnum = int(m.group(1))
                    if rnum > rounds_finished:
                        rounds_finished = rnum

            if not have_cached_progress:
                if preferred_log and os.path.exists(preferred_log):
                    scan_log_tail(preferred_log)
                else:
                    for root, dirs, files in os.walk(workspace_dir):
                        dirs[:] = [
                            d
                            for d in dirs
                            if not d.startswith(".") and d != "__pycache__"
                        ]
                        if job_uuid in root:
                            for fname in files:
                                if fname.startswith("log") and fname.endswith(
                                    ".txt"
                                ):
                                    scan_log_tail(os.path.join(root, fname))
                                    if ended:
                                        break
                        if ended:
                            break

            if ended:
                training_progress = 100
                training_status = "Completed"
                is_training_running = False
                if training_job.status != "COMPLETED":
                    training_job.status = "COMPLETED"
                    training_job.completed_at = timezone.now()
                    training_job.save()
            elif not have_cached_progress and total_rounds > 0:
                rounds_completed = rounds_finished + 1 if rounds_finished >= 0 else 0
                training_progress = min(
                    99, int(rounds_completed * 100 / total_rounds)
                )
        except Exception as e:
            logger.training.debug(
                f"Failed to calculate training progress: {e}"
            )
            training_progress = 0

    elif training_job.status == "COMPLETED":
        training_progress = 100
        training_status = "Completed"

    # Time Calculation
    now = timezone.now()
    start_time = training_job.created_at

    if training_job.status == "RUNNING":
        elapsed = (now - start_time).total_seconds()
        duration_str = format_duration(elapsed)
        if training_progress > 0:
            total_est = elapsed / (training_progress / 100.0)
            remaining = total_est - elapsed
            eta_str = format_duration(remaining)
        else:
            eta_str = "Calculating..."
    elif training_job.completed_at:
        elapsed = (training_job.completed_at - start_time).total_seconds()
        duration_str = format_duration(elapsed)
        eta_str = "Finished"

    result = {
        "status": training_status,
        "progress": training_progress,
        "duration": duration_str,
        "eta": eta_str,
        "is_running": is_training_running,
    }

    # Cache for 5 seconds (only cache if status is RUNNING for dynamic updates)
    if training_job.status == "RUNNING":
        cache.set(cache_key, result, 5)

    return result


@login_required
@project_context_required
def training(request):
    """
    Main training dashboard view.
    """
    try:
        current_network = UserCurrentNetwork.objects.get(
            user=request.user
        ).network
        if not current_network or current_network.status != "RUNNING":
            return render(
                request,
                "apps/training/no_network_started.html",
                {"segment": "training"},
            )
    except UserCurrentNetwork.DoesNotExist:
        return render(
            request,
            "apps/training/no_network_started.html",
            {"segment": "training"},
        )

    is_training_running = False
    training_job = None
    training_progress = 0
    training_status = "Not started"
    training_logs = []
    duration_str = "-"
    eta_str = "-"

    if current_network:
        training_job = (
            TrainingJob.objects.filter(network=current_network)
            .order_by("-created_at")
            .first()
        )

        if training_job:
            info = get_training_progress_info(training_job, current_network)
            training_status = info["status"]
            training_progress = info["progress"]
            duration_str = info["duration"]
            eta_str = info["eta"]
            is_training_running = info["is_running"]

            # Log Collection
            try:
                job_uuid = str(training_job.flare_job_id)
                match = re.search(r"Submitted job:\s*([0-9a-f-]+)", job_uuid)
                if match:
                    job_uuid = match.group(1)
                else:
                    match_uuid = re.search(r"([0-9a-f-]{36})", job_uuid)
                    if match_uuid:
                        job_uuid = match_uuid.group(1)

                latest_log = _find_latest_training_log(
                    str(training_job.project.identifier),
                    str(current_network.identifier),
                    job_uuid,
                    cache_ttl_seconds=60,
                )

                if latest_log and os.path.exists(latest_log):
                    # Tail-read to avoid loading huge logs into memory.
                    tail = _tail_text(latest_log, max_bytes=256 * 1024)
                    lines = tail.splitlines()[-50:]
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        ts = ""
                        level = ""
                        msg = line
                        logger_name = ""
                        ts_match = re.match(
                            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})",
                            line,
                        )
                        if ts_match:
                            ts = ts_match.group(1)
                            remaining = line[len(ts) :].strip()
                            dash_parts = remaining.split(" - ")
                            if len(dash_parts) >= 3:
                                logger_name = dash_parts[0].strip(" -")
                                level = dash_parts[1].strip()
                                msg = " - ".join(dash_parts[2:])
                            else:
                                msg = remaining.strip(" -")
                        training_logs.append(
                            {
                                "timestamp": ts,
                                "level": level,
                                "message": msg.strip(),
                                "logger": logger_name,
                            }
                        )
            except Exception as e:
                logger.training.debug(f"Failed to collect training logs: {e}")

    context = {
        "segment": "training",
        "current_network": current_network,
        "is_training_running": is_training_running,
        "training_status": training_status,
        "training_progress": training_progress,
        "training_logs": training_logs,
        "duration_str": duration_str,
        "eta_str": eta_str,
    }
    return render(request, "apps/training/training.html", context)


@login_required
@project_membership_required
def start_training(request, network_id):
    """
    Submit a Job to NVFlare.
    """
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    log = get_logger(user=request.user, project=project)

    job_dir = os.path.join(
        settings.BASE_DIR,
        "workspaces",
        str(project.identifier),
        str(network.identifier),
        "job",
    )
    project_name = get_safe_slug(project.title, project.identifier).replace(
        "-", "_"
    )
    admin_target = _resolve_admin_session_target(network)
    if not admin_target:
        messages.error(
            request,
            "No admin startup kit found for this center. Please re-provision or upload a complete startup package.",
        )
        return redirect("training:training")

    admin_username, admin_session_dir = admin_target

    app_server_dir = os.path.join(job_dir, "app_server")
    app_client_dir = os.path.join(job_dir, "app_client")
    app_client_custom_dir = os.path.join(app_client_dir, "custom")

    os.makedirs(os.path.join(app_server_dir, "config"), exist_ok=True)
    os.makedirs(os.path.join(app_client_dir, "config"), exist_ok=True)
    os.makedirs(app_client_custom_dir, exist_ok=True)

    source_code_prefix = f"{project.identifier}/code/training/"
    try:
        download_s3_folder(
            settings.AWS_STORAGE_BUCKET_NAME,
            source_code_prefix,
            app_client_custom_dir,
        )
        log.training.info(
            f"Downloaded training code from S3: {source_code_prefix}"
        )
    except Exception as e:
        log.training.error(f"Failed to download training code: {e}")

    flare_adapter_src = os.path.join(
        settings.BASE_DIR, "apps", "training", "flare_adapter.py"
    )
    shutil.copyfile(
        flare_adapter_src,
        os.path.join(app_client_custom_dir, "flare_adapter.py"),
    )

    from common.utils import get_internal_s3_download_url, get_s3_client

    # Build a manifest of project data files using an S3 paginator.
    # This avoids recursive folder listing calls which become very slow for large datasets.
    s3 = get_s3_client()
    root_data_prefix = f"{project.identifier}/data/"
    paginator = s3.get_paginator("list_objects_v2")

    log.training.debug(
        f"Building data manifest for prefix: {root_data_prefix}"
    )
    data_manifest = {}
    file_count = 0
    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=root_data_prefix,
    ):
        for obj in page.get("Contents", []):
            file_key = obj.get("Key")
            if not file_key or file_key.endswith("/"):
                continue

            # Skip hidden files/dirs (e.g. .DS_Store, .ipynb_checkpoints)
            rel_path = file_key[len(root_data_prefix) :]
            if not rel_path or any(
                part.startswith(".") for part in rel_path.split("/")
            ):
                continue

            data_manifest[rel_path] = get_internal_s3_download_url(
                file_key, expires=86400
            )
            file_count += 1

    log.training.info(f"Data manifest built with {file_count} files.")
    manifest_path = os.path.join(app_client_custom_dir, "data_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(data_manifest, f, indent=2)

    training_py_path = os.path.join(app_client_custom_dir, "training.py")
    if os.path.exists(training_py_path):
        with open(training_py_path) as f:
            content = f.read()
        replacement = f'main(project_id="{str(project.identifier)}")'
        content = content.replace(
            'main(project_id="default_project")', replacement
        )
        content = content.replace(
            "main(project_id='default_project')", replacement
        )
        with open(training_py_path, "w") as f:
            f.write(content)

    client_names = list(
        network.participants.filter(role="CLIENT").values_list(
            "participant_id", flat=True
        )
    )
    if not client_names:
        client_names = ["fl-client-1", "fl-client-2"]

    server_names = list(
        network.participants.filter(role="SERVER").values_list(
            "participant_id", flat=True
        )
    )
    if "server" not in server_names:
        server_names = ["server"]

    framework = "np"
    if os.path.exists(training_py_path):
        with open(training_py_path) as f:
            script_text = f.read()

        try:
            tree = ast.parse(script_text)
            imported_modules = set()

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = (alias.name or "").split(".")[0].strip()
                        if mod:
                            imported_modules.add(mod)
                elif isinstance(node, ast.ImportFrom):
                    mod = (node.module or "").split(".")[0].strip()
                    if mod:
                        imported_modules.add(mod)

            if imported_modules.intersection({"tensorflow", "keras"}):
                framework = "tf"
            elif imported_modules.intersection(
                {"torch", "pytorch_lightning"}
            ):
                framework = "pt"
            elif "sklearn" in imported_modules:
                framework = "np"
        except SyntaxError:
            lowered_script_text = script_text.lower()
            if "tensorflow" in lowered_script_text or "keras" in lowered_script_text:
                framework = "tf"
            elif "torch" in lowered_script_text or "pytorch" in lowered_script_text:
                framework = "pt"

    log.training.info(f"Detected training framework: {framework}")

    try:
        from nvflare.fuel.flare_api.flare_api import new_secure_session
        from nvflare.job_config.api import FedJob
        from nvflare.app_common.ccwf import SwarmServerController, SwarmClientController
        from nvflare.apis.dxo import DataKind
        from nvflare.app_common.aggregators.intime_accumulate_model_aggregator import (
            InTimeAccumulateWeightedAggregator,
        )
        from nvflare.app_common.ccwf.comps.simple_model_shareable_generator import (
            SimpleModelShareableGenerator,
        )

        if framework == "tf":
            from nvflare.app_opt.tf.in_process_client_api_executor import (
                TFInProcessClientAPIExecutor,
            )
            from nvflare.app_common.np.np_model_persistor import NPModelPersistor

            executor = TFInProcessClientAPIExecutor(
                task_script_path="custom/training.py"
            )
            persistor = NPModelPersistor()
        elif framework == "pt":
            try:
                from nvflare.app_opt.pt.in_process_client_api_executor import (
                    PTInProcessClientAPIExecutor,
                )
                from nvflare.app_opt.pt.file_model_persistor import (
                    PTFileModelPersistor,
                )

                executor = PTInProcessClientAPIExecutor(
                    task_script_path="custom/training.py"
                )
                persistor = PTFileModelPersistor()
            except ModuleNotFoundError:
                log.training.warning(
                    "PyTorch executor requested but torch is unavailable; "
                    "falling back to generic in-process executor."
                )
                from nvflare.app_common.executors.in_process_client_api_executor import (
                    InProcessClientAPIExecutor,
                )
                from nvflare.app_common.np.np_model_persistor import NPModelPersistor

                executor = InProcessClientAPIExecutor(
                    task_script_path="custom/training.py"
                )
                persistor = NPModelPersistor()
        else:
            from nvflare.app_common.executors.in_process_client_api_executor import (
                InProcessClientAPIExecutor,
            )
            from nvflare.app_common.np.np_model_persistor import NPModelPersistor

            executor = InProcessClientAPIExecutor(
                task_script_path="custom/training.py"
            )
            persistor = NPModelPersistor()

        shareable_generator = SimpleModelShareableGenerator()
        aggregator = InTimeAccumulateWeightedAggregator(
            expected_data_kind=DataKind.WEIGHTS
        )

        sess = new_secure_session(
            username=admin_username,
            startup_kit_location=admin_session_dir,
        )

        # Create the Job object using the 2.7.1 Job API
        job = FedJob(name=f"{project_name}_job")

        # Define Server side
        controller = SwarmServerController(num_rounds=10)
        for server_name in server_names:
            job.to(controller, server_name)

        # Define Client side
        # Swarm Client Controller (handles collaborative logic)
        swarm_client_controller = SwarmClientController(
            learn_task_name="train",
            persistor_id="persistor",
            aggregator_id="aggregator",
            shareable_generator_id="shareable_generator",
            min_responses_required=len(client_names),
        )

        # Map all components to each client target
        for client_name in client_names:
            # In Job API, we add executors to the app
            job.to(
                executor,
                client_name,
                tasks=["train", "validate", "submit_model"],
            )
            job.to(
                swarm_client_controller,
                client_name,
                tasks=["swarm_*"],
            )

            # Add shared components to the client app
            job.to(persistor, client_name, id="persistor")
            job.to(
                shareable_generator,
                client_name,
                id="shareable_generator",
            )
            job.to(aggregator, client_name, id="aggregator")

            # Add custom code directory (training.py, flare_adapter.py, data_manifest.json)
            job.to(app_client_custom_dir, client_name)

        # Export job config and submit by folder path (required by FLARE API)
        generated_jobs_root = os.path.join(job_dir, "generated")
        os.makedirs(generated_jobs_root, exist_ok=True)
        job.export_job(generated_jobs_root)
        job_definition_path = os.path.abspath(
            os.path.join(generated_jobs_root, job.name)
        )
        log.training.info(f"Submitting exported job from: {job_definition_path}")

        job_id = sess.submit_job(job_definition_path)
        try:
            sess.close()
        except Exception:
            pass

        TrainingJob.objects.create(
            project=project,
            network=network,
            status="RUNNING" if job_id else "FAILED",
            flare_job_id=job_id or "unknown",
        )
        messages.success(request, f"Successfully submitted job {job_id}")
    except Exception as e:
        log.training.error(f"Submit job via FLARE API failed: {e}")
        TrainingJob.objects.create(
            project=project,
            network=network,
            status="FAILED",
            flare_job_id="error",
        )
        messages.error(request, f"Failed to submit job: {e}")

    return redirect("training:training")


@login_required
@project_membership_required
def stop_training(request, network_id):
    """
    Aborts the currently running job via the NVFlare API.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    job = (
        TrainingJob.objects.filter(network=network, status="RUNNING")
        .order_by("-created_at")
        .first()
    )

    if not job:
        messages.warning(request, "No running job found to stop.")
        return redirect("training:training")

    try:
        from nvflare.fuel.flare_api.flare_api import new_secure_session

        admin_target = _resolve_admin_session_target(network)
        if not admin_target:
            messages.error(
                request,
                "No admin startup kit found for this center.",
            )
            return redirect("training:training")

        admin_username, admin_user_dir = admin_target
        sess = new_secure_session(
            username=admin_username, startup_kit_location=admin_user_dir
        )
        job_uuid = str(job.flare_job_id)
        match = re.search(r"([0-9a-f-]{36})", job_uuid)
        if match:
            job_uuid = match.group(1)
        sess.api.do_command(f"abort_job {job_uuid}")
        job.status = "STOPPED"
        job.completed_at = timezone.now()
        job.progress_percent = 100
        job.progress_updated_at = timezone.now()
        job.save(
            update_fields=[
                "status",
                "completed_at",
                "progress_percent",
                "progress_updated_at",
            ]
        )
        messages.success(request, f"Successfully aborted job {job_uuid}")
    except Exception as e:
        logger.training.error(f"Abort job failed: {e}")
        err = str(e).lower()
        if any(
            token in err for token in ["not running", "invalid job id", "no such job"]
        ):
            job.status = "COMPLETED"
            job.completed_at = timezone.now()
            job.progress_percent = 100
            job.progress_updated_at = timezone.now()
            job.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "progress_percent",
                    "progress_updated_at",
                ]
            )
            messages.info(
                request,
                "Training job had already finished. Status updated to Completed.",
            )
        else:
            messages.error(request, f"Failed to abort job: {e}")

    return redirect("training:training")


@login_required
@project_context_required
def training_status_api(request):
    """
    AJAX endpoint to poll the current training status.
    """
    try:
        current_network = UserCurrentNetwork.objects.get(
            user=request.user
        ).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"status": "no_network"})

    job = (
        TrainingJob.objects.filter(network=current_network)
        .order_by("-created_at")
        .first()
    )

    nvflare_status = _nvflare_status_payload(current_network)
    if nvflare_status and nvflare_status.get("job_id"):
        status_map = {
            "RUNNING": "RUNNING",
            "COMPLETED": "COMPLETED",
            "STOPPED": "STOPPED",
            "FAILED": "FAILED",
        }
        status_key = str(nvflare_status.get("status", "")).upper().strip()
        mapped_status = status_map.get(status_key)

        if mapped_status:
            mirror_job = (
                TrainingJob.objects.filter(
                    network=current_network,
                    flare_job_id=str(nvflare_status.get("job_id")),
                )
                .order_by("-created_at")
                .first()
            )

            if not mirror_job:
                mirror_job = TrainingJob.objects.create(
                    project=current_network.project,
                    network=current_network,
                    status=mapped_status,
                    flare_job_id=str(nvflare_status.get("job_id")),
                )
            elif mirror_job.status != mapped_status:
                mirror_job.status = mapped_status
                if mapped_status in {"COMPLETED", "STOPPED", "FAILED"}:
                    mirror_job.completed_at = timezone.now()
                mirror_job.save(update_fields=["status", "completed_at"])

            if not job or mirror_job.created_at >= job.created_at:
                job = mirror_job

    if not job:
        if nvflare_status:
            return JsonResponse(nvflare_status)
        return JsonResponse({"status": "idle"})

    payload = _build_training_status_payload(current_network, job)

    if nvflare_status:
        local_status = str(payload.get("status", "")).upper().strip()
        nv_status = str(nvflare_status.get("status", "")).upper().strip()
        terminal_states = {"COMPLETED", "FAILED", "STOPPED"}
        unknown_states = {"", "UNKNOWN", "N/A", "NONE"}

        # Prefer local terminal status inferred from logs/progress so the UI
        # does not regress back to RUNNING due to stale list_jobs output.
        should_override_status = (
            nv_status not in unknown_states
            and not (
                local_status in terminal_states
                and nv_status not in terminal_states
            )
        )

        if should_override_status:
            payload["status"] = nvflare_status.get(
                "status", payload["status"]
            )

        payload["job_id"] = nvflare_status.get("job_id", payload["job_id"])
        nvflare_progress = nvflare_status.get("progress")
        if isinstance(nvflare_progress, (int, float)) and payload[
            "progress"
        ] < int(nvflare_progress):
            payload["progress"] = int(nvflare_progress)

    return JsonResponse(payload)




@login_required
@project_context_required
def training_logs_api(request):
    """
    AJAX endpoint returning the last 100 log lines as a JSON list.
    """
    try:
        current_network = UserCurrentNetwork.objects.get(
            user=request.user
        ).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"logs": []})

    logs = []
    try:
        job = (
            TrainingJob.objects.filter(network=current_network)
            .order_by("-created_at")
            .first()
        )
        if not job:
            return JsonResponse({"logs": logs})

        job_uuid = str(job.flare_job_id)
        match = re.search(r"Submitted job:\s*([0-9a-f-]+)", job_uuid)
        if match:
            job_uuid = match.group(1)
        else:
            match_uuid = re.search(r"([0-9a-f-]{36})", job_uuid)
            if match_uuid:
                job_uuid = match_uuid.group(1)

        # Cache key for this specific job log path
        cache_key = f"training_log_path_{job_uuid}"
        latest_log = cache.get(cache_key)

        # Verify if cached path still exists, else clear it
        if latest_log and not os.path.exists(latest_log):
            latest_log = None
            cache.delete(cache_key)

        # If not cached, find it via os.walk
        if not latest_log:
            workspace_root = os.path.join(
                "workspaces",
                str(job.project.identifier),
                str(current_network.identifier),
                "workspace",
            )
            for root, _, files in os.walk(workspace_root):
                if job_uuid in root:
                    for cand in ("log_fl.txt", "log.txt"):
                        if cand in files:
                            latest_log = os.path.join(root, cand)
                            # Cache valid path for 60 seconds
                            cache.set(cache_key, latest_log, 60)
                            break
                if latest_log:
                    break

        if latest_log and os.path.exists(latest_log):
            with open(latest_log) as lf:
                lines = lf.readlines()[-100:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                ts, level, msg = "", "INFO", line
                ts_match = re.match(
                    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", line
                )
                if ts_match:
                    ts = ts_match.group(1)
                    remaining = line[len(ts) :].strip(" -")
                    parts = remaining.split(" - ")
                    if len(parts) >= 2:
                        if parts[1] in [
                            "DEBUG",
                            "INFO",
                            "WARNING",
                            "ERROR",
                            "CRITICAL",
                        ]:
                            level, msg = parts[1], " - ".join(parts[2:])
                        elif parts[0] in [
                            "DEBUG",
                            "INFO",
                            "WARNING",
                            "ERROR",
                            "CRITICAL",
                        ]:
                            level, msg = parts[0], " - ".join(parts[1:])
                logs.append({"timestamp": ts, "level": level, "message": msg})
    except Exception as e:
        logger.training.debug(f"Error in training_logs_api: {e}")
    return JsonResponse({"logs": logs})


