"""Argparse entrypoint for the swarmed CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bootstrap import bootstrap_django


class CLIUsageError(ValueError):
    """Raised when argparse encounters a usage error."""


class CLIArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that returns structured usage errors."""

    def error(self, message: str) -> None:
        raise CLIUsageError(message)


def _add_common_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    json_default = argparse.SUPPRESS if suppress_defaults else False
    user_default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--json",
        action="store_true",
        default=json_default,
        help="Emit JSON output.",
    )
    parser.add_argument(
        "--user",
        default=user_default,
        help="Act as this local SwarmMedHub username. Defaults to SWARMED_USER or the OS username.",
    )


def _add_wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the operation to reach a terminal state.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds while waiting.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Optional timeout in seconds while waiting.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser."""
    parser = CLIArgumentParser(prog="swarmed")
    _add_common_options(parser)

    common_parent = argparse.ArgumentParser(add_help=False)
    _add_common_options(common_parent, suppress_defaults=True)

    subparsers = parser.add_subparsers(dest="resource")

    # project
    project_parser = subparsers.add_parser("project", parents=[common_parent])
    project_subparsers = project_parser.add_subparsers(dest="action")

    project_create = project_subparsers.add_parser(
        "create", parents=[common_parent]
    )
    project_create.add_argument("--title", required=True)
    project_create.add_argument("--description", default="")
    project_create.add_argument("--member", action="append", default=[])
    project_create.add_argument("--code-dir", required=True)
    project_create.set_defaults(handler="project_create", command_name="project create")

    project_list = project_subparsers.add_parser("list", parents=[common_parent])
    project_list.set_defaults(handler="project_list", command_name="project list")

    project_show = project_subparsers.add_parser("show", parents=[common_parent])
    project_show.add_argument("project_identifier")
    project_show.set_defaults(handler="project_show", command_name="project show")

    project_use = project_subparsers.add_parser("use", parents=[common_parent])
    project_use.add_argument("project_identifier")
    project_use.set_defaults(handler="project_use", command_name="project use")

    project_update = project_subparsers.add_parser(
        "update", parents=[common_parent]
    )
    project_update.add_argument("project_identifier")
    project_update.add_argument("--title")
    project_update.add_argument("--description")
    project_update.add_argument("--member", action="append", default=None)
    project_update.add_argument("--code-dir")
    project_update.set_defaults(handler="project_update", command_name="project update")

    # data
    data_parser = subparsers.add_parser("data", parents=[common_parent])
    data_subparsers = data_parser.add_subparsers(dest="action")

    data_upload = data_subparsers.add_parser("upload", parents=[common_parent])
    data_upload.add_argument("sources", nargs="+")
    data_upload.add_argument("--project")
    data_upload.add_argument("--dest", default="")
    data_upload.set_defaults(handler="data_upload", command_name="data upload")

    data_ls = data_subparsers.add_parser("ls", parents=[common_parent])
    data_ls.add_argument("prefix", nargs="?", default="")
    data_ls.add_argument("--project")
    data_ls.set_defaults(handler="data_ls", command_name="data ls")

    data_download = data_subparsers.add_parser(
        "download", parents=[common_parent]
    )
    data_download.add_argument("path")
    data_download.add_argument("--project")
    data_download.add_argument("--out", required=True)
    data_download.set_defaults(handler="data_download", command_name="data download")

    data_mv = data_subparsers.add_parser("mv", parents=[common_parent])
    data_mv.add_argument("source")
    data_mv.add_argument("destination")
    data_mv.add_argument("--project")
    data_mv.set_defaults(handler="data_mv", command_name="data mv")

    data_rm = data_subparsers.add_parser("rm", parents=[common_parent])
    data_rm.add_argument("path")
    data_rm.add_argument("--project")
    data_rm.set_defaults(handler="data_rm", command_name="data rm")

    data_validate = data_subparsers.add_parser(
        "validate", parents=[common_parent]
    )
    data_validate.add_argument("--project")
    _add_wait_options(data_validate)
    data_validate.set_defaults(handler="data_validate", command_name="data validate")

    data_visualize = data_subparsers.add_parser(
        "visualize", parents=[common_parent]
    )
    data_visualize.add_argument("--project")
    _add_wait_options(data_visualize)
    data_visualize.set_defaults(
        handler="data_visualize", command_name="data visualize"
    )

    # network
    network_parser = subparsers.add_parser("network", parents=[common_parent])
    network_subparsers = network_parser.add_subparsers(dest="action")

    network_create = network_subparsers.add_parser(
        "create", parents=[common_parent]
    )
    network_create.add_argument("--project")
    network_create.add_argument("--name", required=True)
    network_create.add_argument("--description", default="")
    network_create.add_argument("--participant", action="append", default=[])
    network_create.add_argument("--server-ip")
    network_create.add_argument("--local-test", action="store_true")
    network_create.add_argument(
        "--no-local-client",
        action="store_true",
        help=(
            "Do not add this host as an extra FL client when creating a real "
            "network. Use this when the local host is server/admin only."
        ),
    )
    network_create.set_defaults(handler="network_create", command_name="network create")

    network_import = network_subparsers.add_parser(
        "import", parents=[common_parent]
    )
    network_import.add_argument("--project")
    network_import.add_argument("--name", required=True)
    network_import.add_argument("--description", default="")
    network_import.add_argument("--package", required=True)
    network_import.set_defaults(handler="network_import", command_name="network import")

    network_list = network_subparsers.add_parser("list", parents=[common_parent])
    network_list.add_argument("--project")
    network_list.set_defaults(handler="network_list", command_name="network list")

    network_show = network_subparsers.add_parser("show", parents=[common_parent])
    network_show.add_argument("network_identifier")
    network_show.set_defaults(handler="network_show", command_name="network show")

    network_use = network_subparsers.add_parser("use", parents=[common_parent])
    network_use.add_argument("network_identifier")
    network_use.set_defaults(handler="network_use", command_name="network use")

    network_export = network_subparsers.add_parser(
        "export-package", parents=[common_parent]
    )
    network_export.add_argument("network_identifier")
    network_export.add_argument("--out")
    network_export.set_defaults(
        handler="network_export_package",
        command_name="network export-package",
    )

    network_start = network_subparsers.add_parser(
        "start", parents=[common_parent]
    )
    network_start.add_argument("network_identifier")
    _add_wait_options(network_start)
    network_start.set_defaults(handler="network_start", command_name="network start")

    network_stop = network_subparsers.add_parser("stop", parents=[common_parent])
    network_stop.add_argument("network_identifier")
    _add_wait_options(network_stop)
    network_stop.set_defaults(handler="network_stop", command_name="network stop")

    network_status = network_subparsers.add_parser(
        "status", parents=[common_parent]
    )
    network_status.add_argument("network_identifier")
    network_status.set_defaults(handler="network_status", command_name="network status")

    # training
    training_parser = subparsers.add_parser("training", parents=[common_parent])
    training_subparsers = training_parser.add_subparsers(dest="action")

    training_start = training_subparsers.add_parser(
        "start", parents=[common_parent]
    )
    training_start.add_argument("--network")
    training_start.set_defaults(handler="training_start", command_name="training start")

    training_list = training_subparsers.add_parser("list", parents=[common_parent])
    training_list.add_argument("--project")
    training_list.add_argument("--network")
    training_list.set_defaults(handler="training_list", command_name="training list")

    training_status = training_subparsers.add_parser(
        "status", parents=[common_parent]
    )
    training_status.add_argument("job_identifier", nargs="?")
    training_status.add_argument("--network")
    training_status.set_defaults(handler="training_status", command_name="training status")

    training_watch = training_subparsers.add_parser(
        "watch", parents=[common_parent]
    )
    training_watch.add_argument("job_identifier")
    training_watch.add_argument("--network")
    training_watch.add_argument("--interval", type=float, default=5.0)
    training_watch.add_argument("--timeout", type=float, default=None)
    training_watch.set_defaults(handler="training_watch", command_name="training watch")

    training_stop = training_subparsers.add_parser(
        "stop", parents=[common_parent]
    )
    training_stop.add_argument("job_identifier", nargs="?")
    training_stop.add_argument("--network")
    training_stop.set_defaults(handler="training_stop", command_name="training stop")

    # results
    results_parser = subparsers.add_parser("results", parents=[common_parent])
    results_subparsers = results_parser.add_subparsers(dest="action")

    results_sync = results_subparsers.add_parser("sync", parents=[common_parent])
    results_sync.add_argument("--project")
    results_sync.set_defaults(handler="results_sync", command_name="results sync")

    results_list = results_subparsers.add_parser("list", parents=[common_parent])
    results_list.add_argument("--project")
    results_list.add_argument("--job")
    results_list.set_defaults(handler="results_list", command_name="results list")

    results_download = results_subparsers.add_parser(
        "download", parents=[common_parent]
    )
    results_download.add_argument("--project")
    results_download.add_argument("--job", required=True)
    results_download.add_argument("--out", required=True)
    results_download.set_defaults(
        handler="results_download", command_name="results download"
    )

    results_visualize = results_subparsers.add_parser(
        "visualize", parents=[common_parent]
    )
    results_visualize.add_argument("--project")
    results_visualize.add_argument("--job", required=True)
    _add_wait_options(results_visualize)
    results_visualize.set_defaults(
        handler="results_visualize", command_name="results visualize"
    )

    return parser


def _parse_participants(values: list[str]) -> list[dict]:
    participants = []
    for value in values:
        if "=" not in value:
            raise ValueError(
                f"Invalid participant '{value}'. Use NAME=IP."
            )
        name, ip = value.split("=", 1)
        participants.append({"name": name.strip(), "ip": ip.strip()})
    return participants


def _refresh(instance):
    model = instance.__class__
    return model.objects.get(pk=instance.pk)


def run(args: argparse.Namespace):
    """Run the selected CLI command."""
    bootstrap_django()

    from data import services as data_services
    from network.runtime import get_local_participant_status
    from network import services as network_services
    from project import services as project_services
    from results import services as results_services
    from training import services as training_services
    from .support import (
        CLIState,
        render_output,
        resolve_actor,
        resolve_network,
        resolve_project,
        resolve_training_job,
        wait_for_state,
    )

    command_name = getattr(args, "command_name", "swarmed")
    try:
        user = resolve_actor(getattr(args, "user", None))
        state = CLIState(user=user, json_output=getattr(args, "json", False))

        handler_name = getattr(args, "handler", None)
        if not handler_name:
            raise ValueError("No command was provided.")

        result = None

        if handler_name == "project_create":
            project = project_services.create_project(
                author=state.user,
                title=args.title,
                description=args.description,
                member_identifiers=args.member,
                code_dir=args.code_dir,
            )
            result = project_services.serialize_project(project, current_for=state.user)

        elif handler_name == "project_list":
            projects = [
                project_services.serialize_project(project, current_for=state.user)
                for project in project_services.list_user_projects(state.user)
            ]
            result = {"projects": projects}

        elif handler_name == "project_show":
            project = project_services.get_project_for_user(
                state.user, args.project_identifier
            )
            result = project_services.serialize_project(project, current_for=state.user)

        elif handler_name == "project_use":
            project = project_services.get_project_for_user(
                state.user, args.project_identifier
            )
            project_services.set_current_project(state.user, project)
            result = project_services.serialize_project(project, current_for=state.user)

        elif handler_name == "project_update":
            project = project_services.get_project_for_user(
                state.user, args.project_identifier
            )
            project = project_services.update_project(
                actor=state.user,
                project=project,
                title=args.title,
                description=args.description,
                member_identifiers=args.member,
                replace_members=args.member is not None,
                code_dir=args.code_dir,
            )
            result = project_services.serialize_project(project, current_for=state.user)

        elif handler_name == "data_upload":
            project = resolve_project(state, args.project)
            upload_result = data_services.upload_local_sources(
                project, sources=args.sources, destination_folder=args.dest
            )
            state.warnings.extend(
                f"{warning['path']}: {warning['reason']}"
                for warning in upload_result["skipped"]
            )
            if not upload_result["saved"]:
                raise ValueError("No files were uploaded.")
            result = {
                "project_identifier": str(project.identifier),
                "saved": upload_result["saved"],
            }

        elif handler_name == "data_ls":
            project = resolve_project(state, args.project)
            result = data_services.list_project_data(project, prefix=args.prefix)
            result["project_identifier"] = str(project.identifier)

        elif handler_name == "data_download":
            project = resolve_project(state, args.project)
            result = data_services.download_project_data(
                project, relative_path=args.path, output_path=args.out
            )
            result["project_identifier"] = str(project.identifier)

        elif handler_name == "data_mv":
            project = resolve_project(state, args.project)
            result = data_services.move_project_data(
                project, args.source, args.destination
            )
            result["project_identifier"] = str(project.identifier)

        elif handler_name == "data_rm":
            project = resolve_project(state, args.project)
            result = data_services.delete_project_data(project, args.path)
            result["project_identifier"] = str(project.identifier)

        elif handler_name == "data_validate":
            project = resolve_project(state, args.project)
            run_obj = data_services.start_validation(project, state.user)
            if args.wait:
                run_obj = wait_for_state(
                    fetch=lambda: _refresh(run_obj),
                    status_of=lambda item: item.status,
                    terminal_statuses={"completed", "failed", "cancelled"},
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout,
                )
            result = data_services.serialize_validation_run(run_obj, project)

        elif handler_name == "data_visualize":
            project = resolve_project(state, args.project)
            run_obj = data_services.start_visualization(project, state.user)
            if args.wait:
                run_obj = wait_for_state(
                    fetch=lambda: _refresh(run_obj),
                    status_of=lambda item: item.status,
                    terminal_statuses={"completed", "failed", "cancelled"},
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout,
                )
            result = data_services.serialize_visualization_run(run_obj, project)

        elif handler_name == "network_create":
            project = resolve_project(state, args.project)
            if args.local_test:
                network = network_services.create_local_test_network(
                    project=project,
                    actor=state.user,
                    name=args.name,
                    description=args.description,
                )
            else:
                network = network_services.create_network(
                    project=project,
                    actor=state.user,
                    name=args.name,
                    description=args.description,
                    participants=_parse_participants(args.participant),
                    server_ip=args.server_ip,
                    include_local_client=not args.no_local_client,
                )
            result = network_services.serialize_network(
                network, current_for=state.user, include_participants=True
            )

        elif handler_name == "network_import":
            project = resolve_project(state, args.project)
            network = network_services.import_network(
                project=project,
                actor=state.user,
                name=args.name,
                description=args.description,
                package_source=str(Path(args.package).expanduser().resolve()),
            )
            result = network_services.serialize_network(
                network, current_for=state.user, include_participants=True
            )

        elif handler_name == "network_list":
            project = resolve_project(state, args.project)
            result = {
                "project_identifier": str(project.identifier),
                "networks": [
                    network_services.serialize_network(
                        network, current_for=state.user
                    )
                    for network in network_services.list_project_networks(project)
                ],
            }

        elif handler_name == "network_show":
            network = resolve_network(state, network_identifier=args.network_identifier)
            result = network_services.serialize_network(
                network, current_for=state.user, include_participants=True
            )

        elif handler_name == "network_use":
            network = resolve_network(state, network_identifier=args.network_identifier)
            network_services.set_current_network(state.user, network)
            result = network_services.serialize_network(
                network, current_for=state.user, include_participants=True
            )

        elif handler_name == "network_export_package":
            network = resolve_network(state, network_identifier=args.network_identifier)
            zip_buffer = network_services.export_startup_package(network)
            out_path = (
                Path(args.out).expanduser().resolve()
                if args.out
                else Path.cwd() / f"{network.name.replace(' ', '_')}_startup_kits.zip"
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(zip_buffer.getvalue())
            result = {
                "network_identifier": str(network.identifier),
                "output_path": str(out_path),
            }

        elif handler_name == "network_start":
            network = resolve_network(state, network_identifier=args.network_identifier)
            network = network_services.start_network(network, state.user)
            if args.wait:
                network = wait_for_state(
                    fetch=lambda: _refresh(network),
                    status_of=lambda item: item.status,
                    terminal_statuses={"RUNNING", "ERROR"},
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout,
                )
            result = network_services.serialize_network(
                network, current_for=state.user, include_participants=True
            )

        elif handler_name == "network_stop":
            network = resolve_network(state, network_identifier=args.network_identifier)
            network = network_services.stop_network(network, state.user)
            if args.wait:
                network = wait_for_state(
                    fetch=lambda: _refresh(network),
                    status_of=lambda item: item.status,
                    terminal_statuses={"STOPPED", "ERROR"},
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout,
                )
            result = network_services.serialize_network(
                network, current_for=state.user, include_participants=True
            )

        elif handler_name == "network_status":
            network = resolve_network(state, network_identifier=args.network_identifier)
            serialized = network_services.serialize_network(
                network, current_for=state.user, include_participants=True
            )
            try:
                live_status = get_local_participant_status(network)
                for participant in serialized["participants"]:
                    participant["status"] = live_status.get(
                        participant["identifier"], participant["status"]
                    )
            except Exception:
                pass
            result = serialized

        elif handler_name == "training_start":
            network = resolve_network(state, network_identifier=args.network)
            job = training_services.submit_training_job(
                actor=state.user, network=network
            )
            result = training_services.serialize_training_job(job)

        elif handler_name == "training_list":
            network = None
            project = None
            if args.network:
                network = resolve_network(state, network_identifier=args.network)
                project = network.project
            elif args.project:
                project = resolve_project(state, args.project)
            else:
                try:
                    project = resolve_project(state)
                except LookupError:
                    project = None

            jobs = training_services.list_training_jobs(
                project=project, network=network
            )
            jobs = [
                training_services.serialize_training_job(job)
                for job in jobs
                if project_services.user_can_access_project(state.user, job.project)
            ]
            result = {"jobs": jobs}

        elif handler_name == "training_status":
            network = None
            if args.network:
                network = resolve_network(state, network_identifier=args.network)
            elif args.job_identifier:
                job = resolve_training_job(
                    state, job_identifier=args.job_identifier, required=True
                )
                network = job.network
            else:
                network = resolve_network(state)
            job = resolve_training_job(
                state,
                job_identifier=args.job_identifier,
                network=network,
                required=False,
            )
            result = training_services.get_training_status_payload(
                network=network, job=job
            )

        elif handler_name == "training_watch":
            job = resolve_training_job(
                state,
                job_identifier=args.job_identifier,
                network=resolve_network(
                    state,
                    network_identifier=args.network,
                    required=not bool(args.job_identifier),
                )
                if args.network
                else None,
            )
            last_signature = None
            while True:
                payload = training_services.get_training_status_payload(
                    network=job.network, job=job
                )
                signature = (
                    payload.get("status"),
                    payload.get("progress"),
                    payload.get("job_id"),
                )
                if not state.json_output and signature != last_signature:
                    print(
                        f"{payload.get('status')} {payload.get('progress', 0)}% "
                        f"(job={payload.get('job_id')})"
                    )
                    last_signature = signature
                if str(payload.get("status", "")).upper() in {
                    "COMPLETED",
                    "FAILED",
                    "STOPPED",
                }:
                    result = payload
                    break
                if args.timeout is not None and args.timeout <= 0:
                    result = payload
                    break
                if args.timeout is not None:
                    args.timeout -= args.interval
                import time

                time.sleep(args.interval)

        elif handler_name == "training_stop":
            network = (
                resolve_network(state, network_identifier=args.network)
                if args.network
                else resolve_network(state)
            )
            job = resolve_training_job(
                state,
                job_identifier=args.job_identifier,
                network=network,
                required=False,
            )
            job = training_services.stop_training_job(
                actor=state.user, network=network, job=job
            )
            result = training_services.serialize_training_job(job)

        elif handler_name == "results_sync":
            project = resolve_project(state, args.project)
            results_services.sync_results(project, asynchronous=False)
            result = {
                "project_identifier": str(project.identifier),
                "synchronized": True,
            }

        elif handler_name == "results_list":
            project = resolve_project(state, args.project)
            result = {
                "project_identifier": str(project.identifier),
                "results": results_services.list_results(
                    project, job_identifier=args.job
                ),
            }

        elif handler_name == "results_download":
            project = resolve_project(state, args.project)
            result = results_services.download_results_to_directory(
                project, output_dir=args.out, job_identifier=args.job
            )

        elif handler_name == "results_visualize":
            project = resolve_project(state, args.project)
            run_obj = results_services.start_results_visualization(
                project=project, user=state.user, job_identifier=args.job
            )
            if args.wait:
                run_obj = wait_for_state(
                    fetch=lambda: _refresh(run_obj),
                    status_of=lambda item: item.status,
                    terminal_statuses={"completed", "failed", "cancelled"},
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout,
                )
            result = results_services.serialize_results_visualization_run(run_obj)

        else:
            raise ValueError(f"Unsupported command handler '{handler_name}'.")

        render_output(
            ok=True,
            command=command_name,
            result=result,
            warnings=state.warnings,
            json_output=state.json_output,
        )
        return 0
    except (ValueError, LookupError, PermissionError) as exc:
        render_output(
            ok=False,
            command=command_name,
            warnings=[],
            errors=[str(exc)],
            json_output=getattr(args, "json", False),
        )
        return 2
    except KeyboardInterrupt:
        render_output(
            ok=False,
            command=command_name,
            errors=["Interrupted."],
            json_output=getattr(args, "json", False),
        )
        return 1
    except Exception as exc:  # pragma: no cover - defensive boundary
        render_output(
            ok=False,
            command=command_name,
            errors=[f"Unexpected failure: {exc}"],
            json_output=getattr(args, "json", False),
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    """Console script entrypoint."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CLIUsageError as exc:
        json_output = bool(argv and "--json" in argv)
        if json_output:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "command": "swarmed",
                        "result": None,
                        "warnings": [],
                        "errors": [str(exc)],
                    },
                    indent=2,
                )
            )
        else:
            parser.print_usage(sys.stderr)
            print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
