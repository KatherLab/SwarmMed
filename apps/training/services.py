"""Shared training services used by the UI and CLI."""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import shutil

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.utils import get_safe_slug
from logs.logger import get_logger
from network.models import SwarmNetwork

from .models import TrainingJob
from .runtime import (
    build_training_status_payload,
    dedupe_keep_order,
    get_training_progress_info,
    log_flare_pre_submit_diagnostics,
    new_secure_session_with_host,
    parse_nvflare_clients,
    resolve_admin_session_target,
)
from .utils import (
    clamp_progress_percent,
    download_s3_folder,
    extract_flare_job_uuid,
    progress_from_rounds,
    should_update_terminal_status,
)


def list_training_jobs(*, project=None, network=None):
    """List training jobs with optional project/network filtering."""
    queryset = TrainingJob.objects.select_related("project", "network")
    if project is not None:
        queryset = queryset.filter(project=project)
    if network is not None:
        queryset = queryset.filter(network=network)
    return queryset.order_by("-created_at")


def _matching_jobs(queryset, identifier: str):
    condition = models.Q(identifier=identifier) | models.Q(
        flare_job_id__icontains=identifier
    )
    flare_job_uuid = extract_flare_job_uuid(identifier) or (
        identifier if re.fullmatch(r"[0-9a-fA-F-]{36}", str(identifier or "")) else None
    )
    if flare_job_uuid:
        condition |= models.Q(flare_job_uuid=flare_job_uuid)
    return queryset.filter(condition)


def ensure_training_job(
    *, network: SwarmNetwork, flare_job_id: str, status: str = "RUNNING"
) -> tuple[TrainingJob, bool]:
    """Return the canonical local job row for a FLARE job identifier."""
    queryset = (
        TrainingJob.objects.select_related("project", "network")
        .filter(network=network)
        .order_by("created_at", "id")
    )
    flare_job_uuid = extract_flare_job_uuid(flare_job_id)
    job = None
    if flare_job_uuid:
        job = queryset.filter(flare_job_uuid=flare_job_uuid).first()
    if not job and flare_job_id:
        job = queryset.filter(flare_job_id=flare_job_id).first()
    if not job and flare_job_id:
        job = queryset.filter(flare_job_id__icontains=flare_job_id).first()

    created = False
    if not job:
        job = TrainingJob.objects.create(
            project=network.project,
            network=network,
            status=status,
            flare_job_id=flare_job_id,
            flare_job_uuid=flare_job_uuid,
        )
        created = True
    else:
        update_fields = []
        if flare_job_uuid and job.flare_job_uuid != flare_job_uuid:
            job.flare_job_uuid = flare_job_uuid
            update_fields.append("flare_job_uuid")
        if flare_job_id and not job.flare_job_id:
            job.flare_job_id = flare_job_id
            update_fields.append("flare_job_id")
        if update_fields:
            job.save(update_fields=update_fields)
    return job, created


def persist_training_job_state(
    job: TrainingJob,
    *,
    flare_job_id: str | None = None,
    status: str | None = None,
    total_rounds: int | None = None,
    rounds_finished: int | None = None,
    progress_percent: int | None = None,
    completed_at=None,
    progress_updated_at=None,
) -> TrainingJob:
    """Persist one canonical training-job state transition."""
    update_fields = []
    new_status = str(status or "").upper() or None

    if flare_job_id:
        flare_job_uuid = extract_flare_job_uuid(flare_job_id)
        if not job.flare_job_id:
            job.flare_job_id = flare_job_id
            update_fields.append("flare_job_id")
        if flare_job_uuid and job.flare_job_uuid != flare_job_uuid:
            job.flare_job_uuid = flare_job_uuid
            update_fields.append("flare_job_uuid")

    if total_rounds is not None and total_rounds > 0 and job.total_rounds != total_rounds:
        job.total_rounds = total_rounds
        update_fields.append("total_rounds")

    if (
        rounds_finished is not None
        and rounds_finished >= 0
        and (job.rounds_finished is None or rounds_finished > job.rounds_finished)
    ):
        job.rounds_finished = rounds_finished
        update_fields.append("rounds_finished")

    if new_status:
        current_status = str(job.status or "").upper()
        if should_update_terminal_status(current_status, new_status):
            if job.status != new_status:
                job.status = new_status
                update_fields.append("status")
            if completed_at is None:
                completed_at = timezone.now()
        elif current_status not in {"COMPLETED", "FAILED", "STOPPED"} and job.status != new_status:
            job.status = new_status
            update_fields.append("status")

    effective_status = str(job.status or new_status or "").upper()
    normalized_progress = progress_percent
    if normalized_progress is not None:
        normalized_progress = clamp_progress_percent(
            normalized_progress, status=effective_status
        )
    elif effective_status == "COMPLETED":
        normalized_progress = 100

    if normalized_progress is not None and job.progress_percent != normalized_progress:
        job.progress_percent = normalized_progress
        update_fields.append("progress_percent")
        if progress_updated_at is None:
            progress_updated_at = timezone.now()

    if completed_at is not None and job.completed_at != completed_at:
        job.completed_at = completed_at
        update_fields.append("completed_at")

    if progress_updated_at is not None and job.progress_updated_at != progress_updated_at:
        job.progress_updated_at = progress_updated_at
        update_fields.append("progress_updated_at")

    if update_fields:
        job.save(update_fields=list(dict.fromkeys(update_fields)))
    return job


def sync_job_from_docker_result(*, network: SwarmNetwork, result: dict) -> TrainingJob | None:
    """Merge one docker-scraped runtime result into the canonical job row."""
    job_id = result.get("job_id")
    if not job_id:
        running_job = (
            TrainingJob.objects.filter(network=network, status="RUNNING")
            .order_by("-created_at")
            .first()
        )
        if running_job:
            job_id = running_job.flare_job_id or running_job.flare_job_uuid
    if not job_id:
        return None

    job, _created = ensure_training_job(
        network=network,
        flare_job_id=str(job_id),
        status="RUNNING",
    )
    total_rounds = job.total_rounds or 10
    rounds_finished = result.get("rounds_finished")
    progress_percent = None
    if rounds_finished is not None and rounds_finished >= 0:
        progress_percent = progress_from_rounds(
            rounds_finished, total_rounds, status=result.get("terminal_status") or "RUNNING"
        )
    return persist_training_job_state(
        job,
        flare_job_id=str(job_id),
        rounds_finished=rounds_finished,
        total_rounds=total_rounds,
        progress_percent=progress_percent,
        progress_updated_at=timezone.now() if progress_percent is not None else None,
        status=result.get("terminal_status"),
    )


def sync_job_from_remote_payload(
    *, network: SwarmNetwork, remote_job: dict
) -> TrainingJob | None:
    """Merge one peer-mirrored job payload into the canonical job row."""
    flare_job_id = str(remote_job.get("flare_job_id") or "").strip()
    if not flare_job_id:
        return None

    job, _created = ensure_training_job(
        network=network,
        flare_job_id=flare_job_id,
        status=str(remote_job.get("status") or "RUNNING").upper(),
    )
    return persist_training_job_state(
        job,
        flare_job_id=flare_job_id,
        status=remote_job.get("status"),
        total_rounds=remote_job.get("total_rounds"),
        rounds_finished=remote_job.get("rounds_finished"),
        progress_percent=remote_job.get("progress_percent"),
        completed_at=timezone.now()
        if str(remote_job.get("status") or "").upper()
        in {"COMPLETED", "FAILED", "STOPPED"}
        else None,
    )


def sync_job_from_nvflare_status(
    *, network: SwarmNetwork, nvflare_status: dict
) -> TrainingJob | None:
    """Merge one NVFlare admin status payload into the canonical job row."""
    job_id_to_match = str(nvflare_status.get("job_id") or "").strip()
    if not job_id_to_match:
        return None

    status_map = {
        "RUNNING": "RUNNING",
        "COMPLETED": "COMPLETED",
        "STOPPED": "STOPPED",
        "FAILED": "FAILED",
        "SUBMITTED": "STARTING",
        "APPROVED": "STARTING",
        "DISPATCHED": "STARTING",
    }
    status_key = str(nvflare_status.get("status", "")).upper().strip()
    mapped_status = status_map.get(status_key)
    if not mapped_status:
        return None

    job, _created = ensure_training_job(
        network=network,
        flare_job_id=job_id_to_match,
        status=mapped_status,
    )
    progress_percent = (
        100 if mapped_status == "COMPLETED" else nvflare_status.get("progress")
    )
    return persist_training_job_state(
        job,
        flare_job_id=job_id_to_match,
        status=mapped_status,
        progress_percent=progress_percent,
        completed_at=timezone.now()
        if mapped_status in {"COMPLETED", "FAILED", "STOPPED"}
        else None,
    )


def get_training_job(*, network=None, identifier: str) -> TrainingJob:
    """Resolve a training job by UUID identifier or FLARE job id."""
    queryset = TrainingJob.objects.select_related("project", "network")
    if network is not None:
        queryset = queryset.filter(network=network)
    job = _matching_jobs(queryset, identifier).order_by("-created_at").first()
    if not job:
        raise LookupError(f"Training job '{identifier}' was not found.")
    return job


def get_latest_training_job(network: SwarmNetwork) -> TrainingJob | None:
    """Return the latest training job for a network."""
    return (
        TrainingJob.objects.filter(network=network)
        .select_related("project", "network")
        .order_by("-created_at")
        .first()
    )


def serialize_training_job(job: TrainingJob) -> dict:
    """Serialize a training job for CLI output."""
    info = get_training_progress_info(job, job.network)
    return {
        "identifier": str(job.identifier),
        "project_identifier": str(job.project.identifier),
        "network_identifier": str(job.network.identifier),
        "status": job.status,
        "display_status": info["status"],
        "progress_percent": info["progress"],
        "duration": info["duration"],
        "eta": info["eta"],
        "flare_job_id": job.flare_job_id,
        "flare_job_uuid": job.flare_job_uuid,
        "total_rounds": job.total_rounds,
        "rounds_finished": job.rounds_finished,
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def submit_training_job(*, actor, network: SwarmNetwork) -> TrainingJob:
    """Assemble and submit an NVFlare job for a running network."""
    if network.status != "RUNNING":
        raise ValueError(
            f"Network '{network.name}' is not running. Start the network before training."
        )

    project = network.project
    log = get_logger(user=actor, project=project)
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
    admin_target = resolve_admin_session_target(network)
    if not admin_target:
        raise LookupError(
            "No admin startup kit found for this center. Re-provision or upload a complete startup package."
        )

    admin_username, admin_session_dir, server_ip = admin_target
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
    except Exception as exc:
        log.training.error(f"Failed to download training code: {exc}")

    flare_adapter_src = os.path.join(
        settings.BASE_DIR, "apps", "training", "flare_adapter.py"
    )
    shutil.copyfile(
        flare_adapter_src, os.path.join(app_client_custom_dir, "flare_adapter.py")
    )

    training_py_path = os.path.join(app_client_custom_dir, "training.py")
    if os.path.exists(training_py_path):
        with open(training_py_path) as handle:
            content = handle.read()
        replacement = f'main(project_id="{str(project.identifier)}")'
        content = content.replace(
            'main(project_id="default_project")', replacement
        ).replace("main(project_id='default_project')", replacement)
        with open(training_py_path, "w") as handle:
            handle.write(content)

    client_names = list(
        network.participants.filter(role="CLIENT").values_list(
            "participant_id", flat=True
        )
    )
    if not client_names:
        workspace_root = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(project.identifier),
            str(network.identifier),
        )
        try:
            prod_00_check = os.path.dirname(os.path.abspath(admin_session_dir))
            local_client_names_file = os.path.join(
                prod_00_check, ".local_client_names.json"
            )
            if os.path.exists(local_client_names_file):
                with open(local_client_names_file) as handle:
                    local_client_names = json.load(handle)
                if isinstance(local_client_names, list):
                    client_names.extend(
                        [str(name) for name in local_client_names if name]
                    )

            if not client_names:
                participants_file = os.path.join(
                    admin_session_dir, "startup", ".all_participants.json"
                )
                if os.path.exists(participants_file):
                    with open(participants_file) as handle:
                        all_participants = json.load(handle)
                    if isinstance(all_participants, dict) and "clients" in all_participants:
                        client_names.extend(
                            [
                                str(name)
                                for name in all_participants["clients"]
                                if name
                            ]
                        )
        except Exception:
            pass

        if not client_names:
            try:
                import yaml

                project_yml = os.path.join(workspace_root, "project.yml")
                if os.path.exists(project_yml):
                    with open(project_yml) as handle:
                        project_yaml = yaml.safe_load(handle) or {}
                    for participant in project_yaml.get("participants", []):
                        role = str(
                            participant.get("type", participant.get("role", ""))
                        ).lower()
                        name = str(participant.get("name", "")).strip()
                        if name and role in {"client", "fl_client"}:
                            client_names.append(name)
            except Exception:
                pass

        if not client_names:
            try:
                prod_00_dir = os.path.dirname(os.path.abspath(admin_session_dir))
                excluded = {
                    "server",
                    "admin_startup",
                    "startup",
                    "transfer",
                    "local",
                    "logs",
                    "custom",
                }
                for entry in sorted(os.scandir(prod_00_dir), key=lambda item: item.name):
                    if (
                        entry.is_dir()
                        and entry.name not in excluded
                        and not entry.name.startswith(".")
                        and os.path.isdir(os.path.join(entry.path, "startup"))
                    ):
                        client_names.append(entry.name)
            except Exception:
                pass

    client_names = dedupe_keep_order(
        [
            name
            for name in client_names
            if name and name.lower() not in ["server", "admin"]
        ]
    )

    server_names = list(
        network.participants.filter(role="SERVER").values_list(
            "participant_id", flat=True
        )
    )
    if "server" not in server_names:
        server_names = ["server"]

    framework = "np"
    if os.path.exists(training_py_path):
        with open(training_py_path) as handle:
            script_text = handle.read()
        try:
            tree = ast.parse(script_text)
            imported_modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = (alias.name or "").split(".")[0].strip()
                        if module:
                            imported_modules.add(module)
                elif isinstance(node, ast.ImportFrom):
                    module = (node.module or "").split(".")[0].strip()
                    if module:
                        imported_modules.add(module)
            if imported_modules.intersection({"tensorflow", "keras"}):
                framework = "tf"
            elif imported_modules.intersection(
                {
                    "torch",
                    "pytorch_lightning",
                    "lightning",
                    "transformers",
                    "monai",
                }
            ):
                framework = "pt"
            elif imported_modules.intersection({"sklearn", "numpy", "pandas"}):
                framework = "np"
        except SyntaxError:
            if (
                "tensorflow" in script_text.lower()
                or "keras" in script_text.lower()
            ):
                framework = "tf"

    log.training.info(f"Detected training framework: {framework}")
    if framework == "pt":
        runtime_requirements_path = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(project.identifier),
            str(network.identifier),
            "runtime_requirements.txt",
        )
        has_torch_dependency = False
        if os.path.exists(runtime_requirements_path):
            try:
                with open(runtime_requirements_path) as handle:
                    for raw_line in handle:
                        line = raw_line.strip().lower()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("torch"):
                            has_torch_dependency = True
                            break
            except Exception as exc:
                log.training.warning(
                    f"Could not validate runtime requirements for PyTorch: {exc}"
                )
        if not has_torch_dependency:
            raise ValueError(
                "PyTorch training detected, but runtime requirements do not include 'torch'. "
                "Add torch to the project's requirements file, re-provision/start the network, and retry."
            )

    try:
        submit_connect_timeout = 20.0
        env_timeout = os.getenv("MEDSWARMHUB_FLARE_CONNECT_TIMEOUT", "").strip()
        if env_timeout:
            with contextlib.suppress(ValueError):
                submit_connect_timeout = float(env_timeout)

        log.training.info(
            "FLARE submit timeout config: "
            f"requested_timeout={submit_connect_timeout}, "
            f"env_MEDSWARMHUB_FLARE_CONNECT_TIMEOUT={env_timeout or 'unset'}"
        )

        from nvflare.apis.dxo import DataKind
        from nvflare.app_common.aggregators.intime_accumulate_model_aggregator import (
            InTimeAccumulateWeightedAggregator,
        )
        from nvflare.app_common.ccwf import (
            SwarmClientController,
            SwarmServerController,
        )
        from nvflare.app_common.ccwf.comps.simple_intime_model_selector import (
            SimpleIntimeModelSelector,
        )
        from nvflare.app_common.ccwf.comps.simple_model_shareable_generator import (
            SimpleModelShareableGenerator,
        )
        from nvflare.app_common.executors.in_process_client_api_executor import (
            InProcessClientAPIExecutor,
        )
        from nvflare.job_config.api import FedJob

        executor = InProcessClientAPIExecutor(task_script_path="custom/training.py")

        if framework == "np":
            try:
                from nvflare.app_common.np.np_model_persistor import (
                    NPModelPersistor,
                )

                persistor = NPModelPersistor()
            except (ModuleNotFoundError, ImportError):
                from nvflare.app_opt.pt.file_model_persistor import (
                    PTFileModelPersistor,
                )

                persistor = PTFileModelPersistor()
        else:
            try:
                from nvflare.app_opt.pt.file_model_persistor import (
                    PTFileModelPersistor,
                )

                persistor = PTFileModelPersistor()
            except (ModuleNotFoundError, ImportError):
                from nvflare.app_common.np.np_model_persistor import (
                    NPModelPersistor,
                )

                persistor = NPModelPersistor()

        if framework == "tf":
            try:
                from nvflare.app_opt.tf.in_process_client_api_executor import (
                    TFInProcessClientAPIExecutor,
                )

                executor = TFInProcessClientAPIExecutor(
                    task_script_path="custom/training.py"
                )
            except (ModuleNotFoundError, ImportError):
                pass
        elif framework == "pt":
            use_pt_executor = (
                os.getenv("MEDSWARMHUB_ENABLE_PT_EXECUTOR", "")
                .strip()
                .lower()
                in {"1", "true", "yes", "on"}
            )
            if use_pt_executor:
                try:
                    from nvflare.app_opt.pt.in_process_client_api_executor import (
                        PTInProcessClientAPIExecutor,
                    )

                    executor = PTInProcessClientAPIExecutor(
                        task_script_path="custom/training.py"
                    )
                except (ModuleNotFoundError, ImportError):
                    pass

        shareable_generator = SimpleModelShareableGenerator()
        aggregator = InTimeAccumulateWeightedAggregator(
            expected_data_kind=DataKind.WEIGHTS
        )
        model_selector = SimpleIntimeModelSelector(
            validation_metric_name="accuracy"
        )

        log.training.info(
            "Selected NVFlare executor: "
            f"{executor.__class__.__module__}.{executor.__class__.__name__}"
        )

        log_flare_pre_submit_diagnostics(
            log=log,
            username=admin_username,
            startup_kit_location=admin_session_dir,
            requested_host=server_ip,
        )

        session = new_secure_session_with_host(
            username=admin_username,
            startup_kit_location=admin_session_dir,
            host=server_ip,
            timeout=submit_connect_timeout,
            network_id=network.identifier,
        )

        if not client_names or set(client_names).issubset(
            {"fl-client-1", "fl-client-2"}
        ):
            try:
                log.training.info("Querying live server for connected clients...")
                client_response = session.api.do_command("list_clients")
                live_clients = parse_nvflare_clients(client_response)
                if live_clients:
                    client_names = live_clients
                    log.training.info(
                        f"Discovered connected clients: {client_names}"
                    )
            except Exception as client_error:
                log.training.warning(
                    f"Live client discovery failed: {client_error}"
                )

        if not client_names:
            client_names = ["fl-client-1", "fl-client-2"]

        job_definition = FedJob(name=f"{project_name}_job")
        private_p2p = (
            os.getenv("MEDSWARMHUB_PRIVATE_P2P", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        starting_client = client_names[0] if client_names else ""

        swarm_rounds = 10
        try:
            if os.path.exists(training_py_path):
                with open(training_py_path) as handle:
                    content = handle.read()
                match = re.search(r"SWARM_ROUNDS\s*=\s*(\d+)", content)
                if match:
                    swarm_rounds = int(match.group(1))
                    log.training.info(
                        f"Extracted SWARM_ROUNDS={swarm_rounds} from training script: {training_py_path}"
                    )
                else:
                    log.training.warning(
                        f"SWARM_ROUNDS not found in {training_py_path}, defaulting to 10"
                    )
        except Exception as exc:
            log.training.warning(
                f"Failed to extract SWARM_ROUNDS from training script: {exc}"
            )

        controller = SwarmServerController(
            num_rounds=swarm_rounds,
            participating_clients=client_names,
            result_clients=client_names,
            starting_client=starting_client,
            private_p2p=private_p2p,
            aggr_clients=client_names,
            train_clients=client_names,
        )
        log.training.info(
            "Swarm controller config: "
            f"private_p2p={private_p2p}, starting_client={starting_client}, "
            f"participants={client_names}"
        )
        for server_name in server_names:
            job_definition.to(controller, server_name)
            job_definition.to(persistor, server_name, id="persistor")
            job_definition.to(
                shareable_generator, server_name, id="shareable_generator"
            )
            job_definition.to(aggregator, server_name, id="aggregator")

        swarm_client_controller = SwarmClientController(
            learn_task_name="train",
            persistor_id="persistor",
            aggregator_id="aggregator",
            shareable_generator_id="shareable_generator",
            min_responses_required=len(client_names),
        )

        for client_name in client_names:
            job_definition.to(
                executor, client_name, tasks=["train", "validate", "submit_model"]
            )
            job_definition.to(
                swarm_client_controller, client_name, tasks=["swarm_*"]
            )
            job_definition.to(persistor, client_name, id="persistor")
            job_definition.to(
                shareable_generator, client_name, id="shareable_generator"
            )
            job_definition.to(aggregator, client_name, id="aggregator")
            job_definition.to(model_selector, client_name, id="model_selector")
            job_definition.to(app_client_custom_dir, client_name)

        generated_jobs_root = os.path.join(job_dir, "generated")
        os.makedirs(generated_jobs_root, exist_ok=True)
        job_definition.export_job(generated_jobs_root)
        job_definition_path = os.path.abspath(
            os.path.join(generated_jobs_root, job_definition.name)
        )
        log.training.info(
            f"Submitting exported job from: {job_definition_path}"
        )

        job_id = session.submit_job(job_definition_path)
        with contextlib.suppress(Exception):
            session.close()

        if not job_id:
            return TrainingJob.objects.create(
                project=project,
                network=network,
                status="FAILED",
                flare_job_id="unknown",
            )

        training_job, _created = ensure_training_job(
            network=network,
            flare_job_id=job_id,
            status="RUNNING",
        )
        return persist_training_job_state(
            training_job,
            flare_job_id=job_id,
            status="RUNNING",
        )
    except Exception as exc:
        log.training.error(f"Submit job via FLARE API failed: {exc}")
        failed_job = TrainingJob.objects.create(
            project=project,
            network=network,
            status="FAILED",
            flare_job_id="error",
        )
        persist_training_job_state(failed_job, status="FAILED")
        raise


def stop_training_job(
    *, actor, network: SwarmNetwork, job: TrainingJob | None = None
) -> TrainingJob:
    """Abort a running training job on a network."""
    job = job or (
        TrainingJob.objects.filter(network=network, status="RUNNING")
        .order_by("-created_at")
        .first()
    )
    if not job:
        raise LookupError("No running job found to stop.")

    log = get_logger(user=actor, project=network.project)
    admin_target = resolve_admin_session_target(network)
    if not admin_target:
        raise LookupError("No admin startup kit found for this center.")

    admin_username, admin_user_dir, server_ip = admin_target
    try:
        session = new_secure_session_with_host(
            username=admin_username,
            startup_kit_location=admin_user_dir,
            host=server_ip,
            network_id=network.identifier,
        )
        job_uuid = job.flare_job_uuid or extract_flare_job_uuid(job.flare_job_id)
        if not job_uuid:
            job_uuid = str(job.flare_job_id)
        session.api.do_command(f"abort_job {job_uuid}")
        return persist_training_job_state(
            job,
            status="STOPPED",
            completed_at=timezone.now(),
            progress_percent=job.progress_percent,
            progress_updated_at=timezone.now(),
        )
    except Exception as exc:
        log.training.error(f"Abort job failed: {exc}")
        err = str(exc).lower()
        if any(token in err for token in ["not running", "invalid job id", "no such job"]):
            return persist_training_job_state(
                job,
                status="COMPLETED",
                completed_at=timezone.now(),
                progress_percent=100,
                progress_updated_at=timezone.now(),
            )
        raise


def get_training_status_payload(
    *, network: SwarmNetwork, job: TrainingJob | None = None
) -> dict:
    """Return a CLI-friendly training status payload."""
    job = job or get_latest_training_job(network)
    payload = build_training_status_payload(network, job)
    if job:
        normalized_status = str(payload.get("status") or "").upper()
        progress_percent = payload.get("progress")
        progress_updated_at = (
            timezone.now() if progress_percent is not None else None
        )
        if normalized_status in {"COMPLETED", "FAILED", "STOPPED"}:
            persist_training_job_state(
                job,
                status=normalized_status,
                progress_percent=progress_percent,
                completed_at=job.completed_at or timezone.now(),
                progress_updated_at=progress_updated_at,
            )
        elif normalized_status == "RUNNING":
            persist_training_job_state(
                job,
                status="RUNNING",
                progress_percent=progress_percent,
                progress_updated_at=progress_updated_at,
            )
        payload["identifier"] = str(job.identifier)
        payload["network_identifier"] = str(network.identifier)
        payload["project_identifier"] = str(job.project.identifier)
    return payload
