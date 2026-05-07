"""Shared network runtime helpers used by the UI and CLI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from logs.logger import get_logger

from .tasks import _container_name_for, _is_container_running
from .utils import _safe_cache_get, _safe_cache_set, get_tailscale_ip

logger = get_logger()


def get_local_participant_status(swarm_network):
    """Heuristically determine local participant status from containers and logs."""
    cache_key = f"network_status_{swarm_network.identifier}"
    cached_status = _safe_cache_get(cache_key)
    if cached_status:
        return cached_status

    participants = swarm_network.participants.all()
    server_logs = ""
    local_ip = get_tailscale_ip()

    logger.network.debug(
        f"[get_local_participant_status] Checking local status for network "
        f"{swarm_network.identifier}. Local IP: {local_ip}"
    )

    if swarm_network.status == "RUNNING":
        try:
            server_container = _container_name_for(swarm_network.identifier, "server")
            result = subprocess.run(
                ["docker", "logs", "--tail", "3000", server_container],
                capture_output=True,
                text=True,
                timeout=2,
            )
            server_logs = (result.stdout + result.stderr).lower()
            logger.network.debug(
                "[get_local_participant_status] Successfully read logs from "
                f"{server_container} ({len(server_logs)} bytes)"
            )
        except Exception as exc:
            logger.network.debug(
                f"[get_local_participant_status] Could not read server logs: {exc}"
            )

    conn_map = {}
    conn_close_positions = {}
    latest_total_clients = None
    expected_client_count = participants.filter(role="CLIENT").count()
    if server_logs:
        total_clients_pattern = r"total clients:\s*(\d+)"
        for match in re.finditer(total_clients_pattern, server_logs):
            try:
                latest_total_clients = int(match.group(1))
            except (TypeError, ValueError):
                latest_total_clients = None

        creation_pattern = r"connection \[(cn\d+) ([^\]]*?) ssl ([^\]\s]+)\] is created"
        for match in re.finditer(creation_pattern, server_logs):
            cn_id, conn_details, principal = match.groups()
            if ":8002" not in conn_details:
                continue
            participant_id = principal.split("@", 1)[0].strip().lower()
            if participant_id.startswith("admin-"):
                continue
            conn_map[cn_id] = participant_id

        closure_pattern = r"connection \[(cn\d+) [^\]]*\] is closed"
        for match in re.finditer(closure_pattern, server_logs):
            conn_close_positions[match.group(1)] = match.start()

    try:
        label_filter = f"swarmmedhub.network_id={swarm_network.identifier}"
        running_containers = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label={label_filter}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.splitlines()
        logger.network.debug(
            "[get_local_participant_status] Locally running containers for this "
            f"network: {running_containers}"
        )
    except Exception as exc:
        logger.network.debug(
            f"[get_local_participant_status] Could not list docker containers: {exc}"
        )

    status_map = {}
    participant_metrics = {}
    for participant in participants:
        name = participant.participant_id
        role = participant.role.lower()
        is_me = participant.ip == local_ip
        status = "Offline"

        if swarm_network.status == "RUNNING":
            if role == "server":
                try:
                    docker_path = shutil.which("docker") or "docker"
                    env = os.environ.copy()
                    if _is_container_running(
                        docker_path,
                        _container_name_for(swarm_network.identifier, "server"),
                        env,
                    ):
                        status = "Online"
                except Exception:
                    if is_me:
                        status = "Online"
                status_map[name] = status
                continue

            lower_name = name.lower()
            joined_patterns = [
                rf"Client: New client {lower_name}@.* joined",
                rf"Re-activate the client: {lower_name} at .* with token:",
                rf"registered client {lower_name}",
                rf"starting communication with client {lower_name}",
                rf"client: {lower_name} joined",
                rf"client {lower_name} joined",
                rf"Receive heartbeat from Client:{lower_name}",
                rf"heartbeat from {lower_name}",
            ]
            leave_patterns = [
                rf"Client Name:{lower_name} \tToken: .* left",
                rf"client: {lower_name} left",
                rf"client {lower_name} left",
                rf"removed client {lower_name}",
                rf"missing job on client '{lower_name}'",
                rf"client manager: remove client {lower_name}",
                rf"disconnected client {lower_name}",
                rf"notified SJ of dead-job: .* {lower_name}",
            ]

            last_join_idx = -1
            for pattern in joined_patterns:
                for match in re.finditer(pattern, server_logs):
                    if match.start() > last_join_idx:
                        last_join_idx = match.start()

            last_cn_created_idx = -1
            last_cn_closed_idx = -1
            for cn_id, participant_id in conn_map.items():
                if participant_id != lower_name:
                    continue
                for match in re.finditer(
                    rf"connection \[{re.escape(cn_id)}[^\]]*\] is created",
                    server_logs,
                ):
                    if match.start() > last_cn_created_idx:
                        last_cn_created_idx = match.start()
                cn_closed_at = conn_close_positions.get(cn_id, -1)
                if cn_closed_at > last_cn_closed_idx:
                    last_cn_closed_idx = cn_closed_at

            last_activity_idx = max(last_join_idx, last_cn_created_idx)
            last_leave_idx = -1
            for pattern in leave_patterns:
                for match in re.finditer(pattern, server_logs):
                    if match.start() > last_leave_idx:
                        last_leave_idx = match.start()

            if last_activity_idx > -1:
                if last_leave_idx > last_activity_idx:
                    status = "Disconnected"
                else:
                    status = "Joined"
            else:
                status = "Offline"

            participant_metrics[name] = {
                "last_activity": last_activity_idx,
                "last_leave": last_leave_idx,
                "last_cn_close": last_cn_closed_idx,
                "is_me": is_me,
            }
            status_map[name] = status

    if latest_total_clients is not None and expected_client_count > 0:
        joined_participants = [
            name for name, status in status_map.items() if status == "Joined"
        ]

        if len(joined_participants) > latest_total_clients:

            def demotion_score(name):
                metrics = participant_metrics[name]
                closed_after_activity = (
                    1 if metrics["last_cn_close"] > metrics["last_activity"] else 0
                )
                return closed_after_activity, -metrics["last_activity"]

            joined_participants.sort(key=demotion_score, reverse=True)
            to_demote = len(joined_participants) - latest_total_clients
            for index in range(to_demote):
                participant_name = joined_participants[index]
                if not participant_metrics[participant_name]["is_me"]:
                    status_map[participant_name] = "Disconnected"
        elif len(joined_participants) < latest_total_clients:
            potential_recoveries = [
                name
                for name, status in status_map.items()
                if status == "Disconnected"
            ]

            def recovery_score(name):
                metrics = participant_metrics[name]
                has_leave = 1 if metrics["last_leave"] > -1 else 0
                return has_leave, -metrics["last_activity"]

            potential_recoveries.sort(key=recovery_score)
            to_recover = min(
                len(potential_recoveries),
                latest_total_clients - len(joined_participants),
            )
            for index in range(to_recover):
                status_map[potential_recoveries[index]] = "Joined"

    for participant in participants:
        name = participant.participant_id
        if status_map.get(name) not in {"Offline", "Disconnected"}:
            continue
        if participant.ip != local_ip:
            continue
        try:
            docker_path = shutil.which("docker") or "docker"
            env = os.environ.copy()
            my_container = _container_name_for(swarm_network.identifier, name)
            if _is_container_running(docker_path, my_container, env):
                status_map[name] = "Online"
            else:
                fallback_container = _container_name_for(
                    swarm_network.identifier, "client"
                )
                if _is_container_running(docker_path, fallback_container, env):
                    status_map[name] = "Online"
        except Exception as exc:
            logger.network.debug(
                f"[get_local_participant_status] Error checking local "
                f"container for {name}: {exc}"
            )

    _safe_cache_set(cache_key, status_map, 10)
    return status_map
