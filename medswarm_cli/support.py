"""Shared runtime helpers for the medswarm CLI."""

from __future__ import annotations

import getpass
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from django.contrib.auth.models import User

from network import services as network_services
from network.models import SwarmNetwork
from project import services as project_services
from training.models import TrainingJob


@dataclass
class CLIState:
    """Execution state shared across command handlers."""

    user: User
    json_output: bool
    warnings: list[str] = field(default_factory=list)


def json_default(value):
    """JSON serializer for common non-primitive CLI values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def render_output(
    *,
    ok: bool,
    command: str,
    result=None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    json_output: bool = False,
) -> None:
    """Render a command response in human or JSON form."""
    warnings = warnings or []
    errors = errors or []
    if json_output:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "command": command,
                    "result": result,
                    "warnings": warnings,
                    "errors": errors,
                },
                indent=2,
                default=json_default,
            )
        )
        return

    if ok:
        if result is not None:
            print(json.dumps(result, indent=2, default=json_default))
        for warning in warnings:
            print(f"warning: {warning}")
    else:
        for error in errors:
            print(f"error: {error}")


def resolve_actor(explicit_username: str | None = None) -> User:
    """Resolve the acting user for a CLI invocation."""
    candidate = (
        explicit_username
        or os.getenv("MEDSWARM_USER")
        or getpass.getuser()
    )
    user = User.objects.filter(username=candidate).first()
    if not user:
        raise LookupError(
            "Could not resolve the acting user. Pass --user or set MEDSWARM_USER."
        )
    return user


def resolve_project(state: CLIState, project_identifier: str | None = None):
    """Resolve a project from explicit CLI input or the user's current context."""
    if project_identifier:
        return project_services.get_project_for_user(state.user, project_identifier)
    project = project_services.get_current_project(state.user)
    if not project:
        raise LookupError(
            "No current project is selected. Use `medswarm project use <PROJECT_UUID>` or pass --project."
        )
    return project


def resolve_network(
    state: CLIState,
    *,
    network_identifier: str | None = None,
    project=None,
    required: bool = True,
):
    """Resolve a network from CLI input or the user's current context."""
    if network_identifier:
        network = (
            SwarmNetwork.objects.select_related("project", "author")
            .prefetch_related("participants")
            .filter(identifier=network_identifier)
            .first()
        )
        if not network:
            raise LookupError(f"Network '{network_identifier}' was not found.")
        project_services.require_project_access(state.user, network.project)
        if project is not None and network.project_id != project.id:
            raise LookupError(
                f"Network '{network_identifier}' does not belong to project '{project.identifier}'."
            )
        return network

    current_network = network_services.get_current_network(
        state.user, project=project
    )
    if current_network:
        return current_network
    if required:
        raise LookupError(
            "No current network is selected. Use `medswarm network use <NETWORK_UUID>` or pass --network."
        )
    return None


def resolve_training_job(
    state: CLIState,
    *,
    job_identifier: str | None = None,
    network=None,
    required: bool = True,
) -> TrainingJob | None:
    """Resolve a training job by UUID or current network context."""
    queryset = TrainingJob.objects.select_related("project", "network").order_by(
        "-created_at"
    )
    if network is not None:
        queryset = queryset.filter(network=network)

    if job_identifier:
        job = queryset.filter(identifier=job_identifier).first()
        if not job:
            job = queryset.filter(flare_job_id__icontains=job_identifier).first()
        if job and project_services.user_can_access_project(state.user, job.project):
            return job
        if required:
            raise LookupError(f"Training job '{job_identifier}' was not found.")
        return None

    job = queryset.first()
    if job and project_services.user_can_access_project(state.user, job.project):
        return job
    if required:
        raise LookupError(
            "No training job was found in the current context."
        )
    return None


def wait_for_state(
    *,
    fetch: Callable[[], object],
    status_of: Callable[[object], str],
    terminal_statuses: set[str],
    interval_seconds: float = 2.0,
    timeout_seconds: float | None = None,
):
    """Poll until an object reaches a terminal status."""
    deadline = time.time() + timeout_seconds if timeout_seconds else None
    while True:
        obj = fetch()
        status = status_of(obj)
        if status in terminal_statuses:
            return obj
        if deadline is not None and time.time() >= deadline:
            return obj
        time.sleep(interval_seconds)
