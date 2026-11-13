"""Explainability services shared by the Hub UI and the CLI.

A project's ``explainability.py`` runs in the sandbox (run type
``explainability``) and writes a bundle (``cases.json`` plus PNG files, format
``swarmed.explanations/1`` of the ``swarmmed_explain`` package). The worker
copies the bundle with ``swarmmed_explain.bundle.safe_copy_bundle`` and this
module stores it. The same bundle format is exported by
``swarmed results explanations --out`` and imported by
``swarmed results explain-import``.

Access rules: viewing follows the project (author, members and break-glass
users through the views' decorators; lookups are scoped to the project);
starting runs, importing bundles and writing reviews need project membership
(author or member); stopping and deleting a run need its creator or the
project author, and deleting a run that holds other users' reviews needs the
project author.

Blinded review: a user reads a run blinded while their blinded mode for it is
on and its model output was never given to them. Then a case they have not
reviewed shows only its case ID, label and input panels; summary figures and
the script output of the run appear once every case is read. Once the output
was given (blinded mode off, zip download, CLI listing or export, a map or
summary image served while not blinded), every case of the run is shown and
reviews are recorded as made after seeing the output.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from celery import current_app
from common.utils import get_s3_client
from data.sandbox import remove_sandbox_container
from logs import logger
from project.models import Project
from project.services import user_can_access_project
from training.utils import extract_flare_job_uuid

from .models import (
    ExplanationCase,
    ExplanationPanel,
    ExplanationReader,
    ExplanationReview,
    ExplanationRun,
    ExplanationSummary,
)
from .services import _resolve_job_and_flare_id
from .tasks import run_results_explanation_task

ACTIVE_STATUSES = ("pending", "running")
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
REVIEW_NOTE_MAX_CHARS = 2000
REVIEW_VERDICTS = ("agree", "disagree", "unsure")
IMAGE_KINDS = ("panel", "summary")
RUN_LIST_LIMIT = 20

_JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


def explain_container_name(run: ExplanationRun) -> str:
    """Return the sandbox container name of a run (used to stop it)."""
    return f"swarmmedhub-explain-{run.identifier}"


def has_explainability_script(project: Project) -> bool:
    """Return whether the project's explainability script exists in storage."""
    field = project.explainability_script
    if not field or not field.name:
        return False
    try:
        return default_storage.exists(field.name)
    except Exception:
        return False


def _parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _resolve_job(project: Project, job_identifier: str):
    """Resolve a job reference to ``(TrainingJob or None, FLARE job ID)``.

    Raises:
        ValueError: If the reference has characters a job ID never has.
    """
    value = str(job_identifier or "").strip()
    if not _JOB_ID_RE.fullmatch(value):
        raise ValueError("Invalid job identifier.")
    if _parse_uuid(value) is None:
        uuid_text = extract_flare_job_uuid(value)
        if uuid_text is None:
            # A result folder that is not named by a UUID has no job record.
            return None, value
        value = uuid_text
    job, flare_id = _resolve_job_and_flare_id(project, value)
    flare_id = str(flare_id)
    if not _JOB_ID_RE.fullmatch(flare_id):
        raise ValueError("Invalid job identifier.")
    return job, flare_id


def _runs_for_job(project: Project, job, flare_id: str):
    query = ExplanationRun.objects.filter(project=project)
    if job is not None:
        return query.filter(models.Q(job=job) | models.Q(flare_job_id=flare_id))
    return query.filter(flare_job_id=flare_id)


def list_explanation_runs(project: Project, *, job_identifier: str):
    """Return a job's explanation runs, newest first."""
    job, flare_id = _resolve_job(project, job_identifier)
    return _runs_for_job(project, job, flare_id).select_related("user", "job")


def get_results_explanation_run(
    project: Project, *, job_identifier: str
) -> ExplanationRun | None:
    """Return the newest explanation run of a job (any status)."""
    return list_explanation_runs(project, job_identifier=job_identifier).first()


def get_latest_successful_run(
    project: Project, *, job_identifier: str
) -> ExplanationRun | None:
    """Return the newest run of a job whose explanations were stored."""
    return (
        list_explanation_runs(project, job_identifier=job_identifier)
        .filter(status="completed", success=True)
        .first()
    )


def resolve_explanation_run(project: Project, run_identifier) -> ExplanationRun:
    """Return a run of the project by UUID.

    Raises:
        LookupError: If the project has no such run.
    """
    run_uuid = _parse_uuid(run_identifier)
    run = (
        ExplanationRun.objects.filter(project=project, identifier=run_uuid)
        .select_related("project", "user", "job")
        .first()
        if run_uuid
        else None
    )
    if run is None:
        raise LookupError("Explanation run not found.")
    return run


def resolve_explanation_case(project: Project, case_identifier) -> ExplanationCase:
    """Return a case of the project by UUID.

    Raises:
        LookupError: If the project has no such case.
    """
    case_uuid = _parse_uuid(case_identifier)
    case = (
        ExplanationCase.objects.filter(run__project=project, identifier=case_uuid)
        .select_related("run", "run__project")
        .first()
        if case_uuid
        else None
    )
    if case is None:
        raise LookupError("Explanation case not found.")
    return case


def resolve_explanation_image(
    project: Project, image_kind: str, image_identifier, *, user=None
):
    """Return a panel or summary of the project by kind and UUID.

    With ``user``, an image that the user's blinded mode hides is reported as
    not found.

    Raises:
        LookupError: If the project has no such image or it is hidden.
    """
    image_uuid = _parse_uuid(image_identifier)
    obj = None
    if image_uuid and image_kind == "panel":
        obj = (
            ExplanationPanel.objects.filter(
                case__run__project=project, identifier=image_uuid
            )
            .select_related("case", "case__run")
            .first()
        )
    elif image_uuid and image_kind == "summary":
        obj = (
            ExplanationSummary.objects.filter(
                run__project=project, identifier=image_uuid
            )
            .select_related("run")
            .first()
        )
    if obj is None:
        raise LookupError("Explanation image not found.")
    if user is not None and not _image_visible(obj, user):
        raise LookupError("Explanation image not found.")
    return obj


# --------------------------------------------------------------------------
# Starting, stopping and deleting runs
# --------------------------------------------------------------------------


def _require_member(user, project: Project, action: str) -> None:
    if not user_can_access_project(user, project):
        raise PermissionError(f"Only project members can {action}.")


def _can_manage_run(run: ExplanationRun, user) -> bool:
    return bool(
        user.id == run.project.author_id
        or (run.user_id is not None and run.user_id == user.id)
    )


def _mark_cancelled(run: ExplanationRun) -> bool:
    """Mark a pending or running run cancelled.

    The update only applies to an active run, so a run that has just finished
    keeps its result, and the task, which checks the status before it stores
    anything, never overwrites the cancellation.

    Returns:
        bool: False if the run was no longer pending or running.
    """
    now = timezone.now()
    updated = ExplanationRun.objects.filter(
        pk=run.pk, status__in=ACTIVE_STATUSES
    ).update(status="cancelled", completed_at=now, updated_at=now)
    if updated:
        run.status = "cancelled"
        run.completed_at = now
        run.updated_at = now
    return bool(updated)


def _stop_task_and_container(run: ExplanationRun, log) -> None:
    """Stop a cancelled run's task and remove its sandbox container.

    The task is revoked without ``terminate``: a queued task is then dropped,
    and a running one is ended by removing its container, after which the
    task's wait returns, the task finds the run cancelled, stores nothing and
    removes its folders. Terminating would send SIGTERM to the worker process,
    which then dies before that clean-up runs.
    """
    if run.celery_task_id:
        try:
            current_app.control.revoke(run.celery_task_id)
        except Exception as exc:
            log.results.warning(f"Could not revoke explanation task: {exc}")
    try:
        remove_sandbox_container(explain_container_name(run))
    except Exception as exc:
        log.results.warning(f"Could not remove the explanation container: {exc}")
    log.results.info(f"Cancelled explanation run {run.identifier}")


def start_results_explanation(
    *, project: Project, user, job_identifier: str
) -> ExplanationRun:
    """Start the project's explainability script for a job in the sandbox.

    Pending or running explanation runs of the same job are cancelled first.
    If one of them was started by another member and the user may not stop it
    (only its creator or the project author may), nothing is started.

    Raises:
        PermissionError: If the user is not a project member.
        ValueError: If there is no script, the job reference is invalid, or
            another member's run of the job is still active.
    """
    _require_member(user, project, "run explanations")
    if not has_explainability_script(project):
        raise ValueError(
            "The project has no explainability script. Import explainability.py "
            "on the project's edit page or in its code directory first."
        )
    job, flare_id = _resolve_job(project, job_identifier)
    log = logger.get_logger(user=user, project=project)
    with transaction.atomic():
        # One start at a time per project, so that two simultaneous starts of
        # a job cannot both leave an active run.
        Project.objects.select_for_update().only("pk").get(pk=project.pk)
        active = list(
            _runs_for_job(project, job, flare_id)
            .filter(status__in=ACTIVE_STATUSES)
            .select_related("project", "user")
        )
        blocking = next(
            (item for item in active if not _can_manage_run(item, user)), None
        )
        if blocking is not None:
            starter = blocking.user.username if blocking.user_id else "another member"
            raise ValueError(
                f"An explanation run of this job started by {starter} is still "
                "pending or running. Wait for it to finish, or ask its starter "
                "or the project author to stop it."
            )
        cancelled = [item for item in active if _mark_cancelled(item)]
        run = ExplanationRun.objects.create(
            project=project,
            job=job,
            flare_job_id=flare_id,
            user=user,
            source="sandbox",
        )
    for item in cancelled:
        _stop_task_and_container(item, log)
    task = run_results_explanation_task.delay(run.id, flare_id)
    run.celery_task_id = task.id or ""
    run.save(update_fields=["celery_task_id", "updated_at"])
    log.results.info(f"Started explanation run {run.identifier} for job {flare_id}")
    return run


def stop_results_explanation(
    *, project: Project, user, run_identifier
) -> ExplanationRun:
    """Cancel a pending or running explanation run and remove its container.

    Raises:
        LookupError: If the project has no such run.
        PermissionError: If the user neither started the run nor owns the project.
        ValueError: If the run is not pending or running.
    """
    run = resolve_explanation_run(project, run_identifier)
    if not _can_manage_run(run, user):
        raise PermissionError(
            "Only the user who started the run or the project author can stop it."
        )
    if run.status not in ACTIVE_STATUSES or not _mark_cancelled(run):
        raise ValueError("The explanation run is not running.")
    _stop_task_and_container(run, logger.get_logger(user=user, project=project))
    return run


def delete_explanation_run(*, project: Project, user, run_identifier) -> None:
    """Delete a finished explanation run with its cases, images and reviews.

    Raises:
        LookupError: If the project has no such run.
        PermissionError: If the user neither started the run nor owns the
            project, or if other users reviewed the run and the user is not
            the project author.
        ValueError: If the run is still pending or running.
    """
    run = resolve_explanation_run(project, run_identifier)
    if not _can_manage_run(run, user):
        raise PermissionError(
            "Only the user who started the run or the project author can delete it."
        )
    if run.status in ACTIVE_STATUSES:
        raise ValueError("Stop the explanation run before deleting it.")
    if (
        user.id != run.project.author_id
        and ExplanationReview.objects.filter(case__run=run)
        .exclude(reviewer=user)
        .exists()
    ):
        raise PermissionError(
            "Other users have reviewed these explanations, so only the project "
            "author can delete them."
        )
    identifier = run.identifier
    run.delete()
    logger.get_logger(user=user, project=project).results.info(
        f"Deleted explanation run {identifier}"
    )


# --------------------------------------------------------------------------
# Storing bundles (sandbox output and imports)
# --------------------------------------------------------------------------


def _text_or_blank(value) -> str:
    return "" if value is None else str(value)


def _store_image(
    obj, bundle_dir: str, source: str, name: str, stored: list[str]
) -> None:
    """Store the bundle file ``source`` as the image ``name`` of ``obj``."""
    path = os.path.join(bundle_dir, source)
    with open(path, "rb") as handle:
        content = handle.read()
    obj.image.save(name, ContentFile(content), save=False)
    stored.append(obj.image.name)


def _random_order(count: int) -> list[int]:
    """Return a random permutation of ``range(count)`` (from the OS's source)."""
    order = list(range(count))
    secrets.SystemRandom().shuffle(order)
    return order


def delete_stored_images(names) -> None:
    """Delete stored image files, ignoring files that are already gone."""
    for name in names:
        with contextlib.suppress(Exception):
            default_storage.delete(name)


def persist_explanation_bundle(
    run: ExplanationRun, collected: dict, bundle_dir: str
) -> list[str]:
    """Store a checked bundle as the cases, panels and summaries of a run.

    The cases are stored in a random order (``secrets.SystemRandom``), and
    their positions and image names follow that order. Scripts usually add
    cases grouped by outcome, so in blinded review the bundle order would tell
    a reviewer a case's outcome. Export (``build_explanation_bundle``) writes
    the stored order.

    Args:
        run: The run the bundle belongs to (without cases yet).
        collected: The result of ``swarmmed_explain.bundle.safe_copy_bundle``.
        bundle_dir: The private folder that ``safe_copy_bundle`` wrote.

    Returns:
        list[str]: The storage names of the stored images. If the caller's
            transaction fails after this returns, it must delete them.

    Raises:
        ValueError: If the run already has cases.
    """
    from swarmmed_explain.bundle import describe, panel_file_name, summary_file_name

    data = collected["data"]
    files = collected["files"]
    if run.cases.exists():
        raise ValueError("The explanation run already has stored cases.")

    items = data["cases"]
    # order[k] is the bundle index of the case stored at position k + 1.
    order = _random_order(len(items))
    stored: list[str] = []
    try:
        with transaction.atomic():
            cases = []
            for position, index in enumerate(order, start=1):
                item = items[index]
                cases.append(
                    ExplanationCase(
                        run=run,
                        position=position,
                        case_id=item["case_id"],
                        label=_text_or_blank(item["label"]),
                        prediction=_text_or_blank(item["prediction"]),
                        target=_text_or_blank(item["target"]),
                        score=item["score"],
                        outcome=item["outcome"] or "",
                        details=item["details"] or {},
                    )
                )
            ExplanationCase.objects.bulk_create(cases)

            panels = []
            for case, index in zip(cases, order, strict=True):
                for position, panel in enumerate(items[index]["panels"], start=1):
                    source = panel_file_name(index + 1, position)
                    if panel["file"] != source:
                        raise ValueError(f"Unexpected panel file {panel['file']!r}.")
                    obj = ExplanationPanel(
                        case=case,
                        position=position,
                        method=panel["method"],
                        title=_text_or_blank(panel["title"]),
                        width=files[source]["width"],
                        height=files[source]["height"],
                    )
                    name = panel_file_name(case.position, position)
                    _store_image(obj, bundle_dir, source, name, stored)
                    panels.append(obj)
            ExplanationPanel.objects.bulk_create(panels)

            summaries = []
            for position, item in enumerate(data["summaries"], start=1):
                name = summary_file_name(position)
                if item["file"] != name:
                    raise ValueError(f"Unexpected summary file {item['file']!r}.")
                obj = ExplanationSummary(
                    run=run,
                    position=position,
                    title=_text_or_blank(item["title"]),
                    width=files[name]["width"],
                    height=files[name]["height"],
                )
                _store_image(obj, bundle_dir, name, name, stored)
                summaries.append(obj)
            ExplanationSummary.objects.bulk_create(summaries)

            run.info = data.get("info") or {}
            run.save(update_fields=["info", "updated_at"])
    except BaseException:
        delete_stored_images(stored)
        raise

    logger.get_logger(project=run.project).results.info(
        f"Stored explanations of run {run.identifier}: {describe(data)}"
    )
    return stored


def import_explanation_bundle(
    *, project: Project, user, job_identifier: str, bundle_dir
) -> ExplanationRun:
    """Import a bundle folder (for example from a local run at the site).

    The folder is treated as untrusted: it is copied with
    ``swarmmed_explain.bundle.safe_copy_bundle`` (fixed file names, regular files,
    size caps, strict JSON, PNGs re-encoded) before anything is stored.

    Raises:
        PermissionError: If the user is not a project member.
        ValueError: If the job reference or the bundle is invalid
            (``swarmmed_explain.bundle.BundleError`` is a ``ValueError``).
    """
    from swarmmed_explain.bundle import safe_copy_bundle

    _require_member(user, project, "import explanations")
    job, flare_id = _resolve_job(project, job_identifier)
    source = Path(bundle_dir).expanduser()
    if not source.is_dir():
        raise ValueError(f"Bundle folder '{bundle_dir}' does not exist.")

    staging = tempfile.mkdtemp(prefix="explain_import_", dir=settings.PROJECT_TEMP_DIR)
    stored: list[str] = []
    try:
        staged_bundle = os.path.join(staging, "bundle")
        collected = safe_copy_bundle(source, staged_bundle)
        now = timezone.now()
        # The run, its cases and images are stored together or not at all.
        with transaction.atomic():
            run = ExplanationRun.objects.create(
                project=project,
                job=job,
                flare_job_id=flare_id,
                user=user,
                source="import",
                status="completed",
                success=True,
                started_at=now,
                completed_at=now,
            )
            stored = persist_explanation_bundle(run, collected, staged_bundle)
        return run
    except BaseException:
        delete_stored_images(stored)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# Blinded review mode
# --------------------------------------------------------------------------


def _reader(run: ExplanationRun, user) -> ExplanationReader:
    """Return the user's reader row for a run, creating it when needed.

    A new row starts in the mode the user last chose in this project, and in
    blinded mode if the user has not opened explanations of the project
    before.
    """
    reader = ExplanationReader.objects.filter(run=run, user=user).first()
    if reader is not None:
        return reader
    # updated_at changes only when a row is created or its mode is switched
    # (see _mark_output_seen), so the newest row holds the last choice.
    previous = (
        ExplanationReader.objects.filter(user=user, run__project_id=run.project_id)
        .order_by("-updated_at", "-id")
        .first()
    )
    reader, _ = ExplanationReader.objects.get_or_create(
        run=run,
        user=user,
        defaults={"blinded": True if previous is None else previous.blinded},
    )
    return reader


def _mark_output_seen(reader: ExplanationReader) -> None:
    if reader.output_seen_at is None:
        reader.output_seen_at = timezone.now()
        # Not updated_at: it marks the user's last mode choice (see _reader).
        reader.save(update_fields=["output_seen_at"])


def mark_output_seen(run: ExplanationRun, user) -> None:
    """Record that the run's full model output was given to the user."""
    _mark_output_seen(_reader(run, user))


def set_reader_mode(run: ExplanationRun, user, *, blinded: bool) -> ExplanationReader:
    """Switch the user's blinded review mode for a run."""
    reader = _reader(run, user)
    if reader.blinded != bool(blinded):
        reader.blinded = bool(blinded)
        reader.save(update_fields=["blinded", "updated_at"])
    return reader


def _reads_blinded(reader: ExplanationReader) -> bool:
    """Whether the user reads the run blinded: mode on, output never given."""
    return bool(reader.blinded and reader.output_seen_at is None)


def _revealed_case_ids(run: ExplanationRun, user, reader) -> set[int] | None:
    """Return the IDs of cases whose output the user may see (None: all).

    One rule decides what is shown and how a review is recorded: unless the
    user reads the run blinded, every case is shown, and reviews count as
    made after seeing the output (``add_explanation_review``).
    """
    if not _reads_blinded(reader):
        return None
    return set(
        ExplanationReview.objects.filter(case__run=run, reviewer=user).values_list(
            "case_id", flat=True
        )
    )


def _image_visible(obj, user) -> bool:
    """Return whether the user may get an image.

    Giving a map panel or a summary to a user who does not read the run
    blinded records the run's output as seen by the user.
    """
    run = obj.explanation_run
    reader = _reader(run, user)
    revealed = _revealed_case_ids(run, user, reader)
    is_input = isinstance(obj, ExplanationPanel) and obj.is_input
    if revealed is None:
        if not is_input:
            _mark_output_seen(reader)
        return True
    if isinstance(obj, ExplanationPanel):
        return is_input or obj.case_id in revealed
    return not run.cases.exclude(id__in=revealed).exists()


# --------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------


def _clean_note(note) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise ValueError("The note must be text.")
    note = note.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(note) > REVIEW_NOTE_MAX_CHARS:
        raise ValueError(
            f"The note is longer than {REVIEW_NOTE_MAX_CHARS:,} characters."
        )
    if _CONTROL_RE.search(note):
        raise ValueError("The note contains control characters.")
    return note


def add_explanation_review(
    *, project: Project, user, case_identifier, verdict, note=""
) -> ExplanationReview:
    """Append a review of a case by ``user`` (reviews are never changed).

    ``blinded`` is set by the server: true only if the user reads the run
    blinded (mode on and the run's model output never given to the user) and
    had not reviewed the case before, that is, exactly when the case was
    shown with its output hidden.

    Raises:
        PermissionError: If the user is not a project member.
        LookupError: If the project has no such case.
        ValueError: If the verdict or note is invalid.
    """
    _require_member(user, project, "review explanations")
    case = resolve_explanation_case(project, case_identifier)
    verdict = str(verdict or "").strip().lower()
    if verdict not in REVIEW_VERDICTS:
        raise ValueError("The verdict must be agree, disagree or unsure.")
    note = _clean_note(note)

    with transaction.atomic():
        reader = ExplanationReader.objects.select_for_update().get(
            pk=_reader(case.run, user).pk
        )
        had_review = ExplanationReview.objects.filter(case=case, reviewer=user).exists()
        blinded = _reads_blinded(reader) and not had_review
        return ExplanationReview.objects.create(
            case=case, reviewer=user, verdict=verdict, note=note, blinded=blinded
        )


def serialize_review(review: ExplanationReview, *, user=None) -> dict:
    """Serialize a review."""
    return {
        "identifier": str(review.identifier),
        "reviewer": review.reviewer.username if review.reviewer_id else None,
        "verdict": review.verdict,
        "note": review.note,
        "blinded": review.blinded,
        "created_at": review.created_at.isoformat(),
        "is_mine": bool(user is not None and review.reviewer_id == user.id),
    }


def _latest_per_reviewer(reviews) -> list[ExplanationReview]:
    latest: dict[int, ExplanationReview] = {}
    for review in sorted(
        reviews, key=lambda item: (item.created_at, item.id), reverse=True
    ):
        latest.setdefault(review.reviewer_id, review)
    return sorted(latest.values(), key=lambda item: item.reviewer.username)


def list_case_reviews(project: Project, user, case_identifier) -> dict:
    """Return a case's reviews visible to the user (all, newest first).

    In blinded mode the reviews of a case stay hidden until the user saved a
    read of it.
    """
    case = resolve_explanation_case(project, case_identifier)
    revealed = _revealed_case_ids(case.run, user, _reader(case.run, user))
    reviews = list(case.reviews.select_related("reviewer"))
    if revealed is not None and case.id not in revealed:
        reviews = [review for review in reviews if review.reviewer_id == user.id]
    return {
        "case": str(case.identifier),
        "reviews": [serialize_review(review, user=user) for review in reviews],
    }


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def _has_stored_cases(run: ExplanationRun) -> bool:
    return run.status == "completed" and bool(run.success)


def serialize_explanation_run(
    run: ExplanationRun | None, *, include_output: bool | None = None
) -> dict:
    """Serialize a run's status and metadata (no case data).

    With ``include_output=False`` the script output is left out (``output`` is
    empty and ``output_hidden`` tells whether there was any). By default it is
    left out only for a run with stored explanations: its output can hold
    model output, which is given to a user only with the run's cases
    (``explanation_view_payload``, ``describe_explanations``). The output of a
    run that stored nothing (the error's context) is always included.
    """
    if run is None:
        return {"status": "none", "cases": 0, "summaries": 0}
    if include_output is None:
        include_output = not _has_stored_cases(run)
    return {
        "identifier": str(run.identifier),
        "status": run.status,
        "success": run.success,
        "source": run.source,
        "user": run.user.username if run.user_id else None,
        "error_message": run.error_message,
        "output": run.output if include_output else "",
        "output_hidden": bool(run.output) and not include_output,
        "info": run.info or {},
        "script_sha256": run.script_sha256,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "job_identifier": str(run.job.identifier) if run.job_id else None,
        "flare_job_id": run.flare_job_id,
        "cases": run.cases.count(),
        "summaries": run.summaries.count(),
    }


def _image_url(kind: str, obj) -> str:
    return reverse(
        "results:explanation_image",
        kwargs={"image_kind": kind, "image_uuid": str(obj.identifier)},
    )


def _panel_dict(panel: ExplanationPanel) -> dict:
    return {
        "identifier": str(panel.identifier),
        "position": panel.position,
        "method": panel.method,
        "title": panel.title,
        "is_input": panel.is_input,
        "width": panel.width,
        "height": panel.height,
        "image_url": _image_url("panel", panel),
    }


def _case_dict(case: ExplanationCase, user, *, revealed: bool) -> dict:
    panels = list(case.panels.all())
    shown = [panel for panel in panels if revealed or panel.is_input]
    reviews = list(case.reviews.all())
    if not revealed:
        reviews = [review for review in reviews if review.reviewer_id == user.id]
    own = [review for review in reviews if review.reviewer_id == user.id]
    return {
        "identifier": str(case.identifier),
        "position": case.position,
        "case_id": case.case_id,
        "label": case.label,
        "hidden": not revealed,
        # Scripts may put model output into the target and the details too.
        "target": case.target if revealed else None,
        "details": (case.details or {}) if revealed else {},
        "prediction": case.prediction if revealed else None,
        "score": case.score if revealed else None,
        "outcome": case.outcome if revealed else None,
        "panels": [_panel_dict(panel) for panel in shown],
        "hidden_panels": len(panels) - len(shown),
        "reviews": [
            serialize_review(review, user=user)
            for review in _latest_per_reviewer(reviews)
        ],
        "my_review_count": len(own),
    }


def explanation_view_payload(run: ExplanationRun, user) -> dict:
    """Return a run's cases, panels and summaries as the user may see them.

    While the user reads the run blinded, a case shows only its case ID, label
    and input panels (method ``raw`` or ``input``) until the user saved a read
    of it: prediction, score, outcome, target, details, map panels and other
    reviewers' reviews are left out. Summary figures and the script output
    are left out until every case was read. Without blinded mode, the run's
    output counts as seen by the user from then on, and every case is shown
    even if blinded mode is switched on again.
    """
    reader = _reader(run, user)
    revealed = _revealed_case_ids(run, user, reader)
    if revealed is None:
        _mark_output_seen(reader)
    cases = list(run.cases.prefetch_related("panels", "reviews", "reviews__reviewer"))
    case_dicts = [
        _case_dict(case, user, revealed=revealed is None or case.id in revealed)
        for case in cases
    ]
    all_read = all(not item["hidden"] for item in case_dicts)
    # A failed run has no cases, so its output (the error's context) stays.
    summaries = [
        {
            "identifier": str(summary.identifier),
            "position": summary.position,
            "title": summary.title,
            "width": summary.width,
            "height": summary.height,
            "image_url": _image_url("summary", summary),
        }
        for summary in run.summaries.all()
    ]
    return {
        "run": serialize_explanation_run(run, include_output=all_read),
        "blinded": bool(reader.blinded),
        "reads_blinded": _reads_blinded(reader),
        "can_review": user_can_access_project(user, run.project),
        "can_manage": _can_manage_run(run, user),
        "cases": case_dicts,
        "summaries": summaries if all_read else [],
        "hidden_summaries": 0 if all_read else len(summaries),
    }


def explanation_case_view(case: ExplanationCase, user) -> dict:
    """Return one case as the user may see it (after a review, for example)."""
    revealed = _revealed_case_ids(case.run, user, _reader(case.run, user))
    case = ExplanationCase.objects.prefetch_related(
        "panels", "reviews", "reviews__reviewer"
    ).get(pk=case.pk)
    return _case_dict(case, user, revealed=revealed is None or case.id in revealed)


def explanation_status_payload(project: Project, user, job_identifier: str) -> dict:
    """Return the explanation status of a job for the results page.

    The script output of a run with stored explanations is left out (the
    run's case payload has it, unless the user reads the run blinded); the
    output of a failed run is kept.
    """
    runs = list(
        list_explanation_runs(project, job_identifier=job_identifier)[:RUN_LIST_LIMIT]
    )
    latest = runs[0] if runs else None
    return {
        "status": latest.status if latest else "none",
        "has_script": bool(project.explainability_script),
        "can_start": user_can_access_project(user, project),
        "latest": serialize_explanation_run(latest) if latest else None,
        "runs": [
            {
                "identifier": str(run.identifier),
                "status": run.status,
                "success": run.success,
                "source": run.source,
                "user": run.user.username if run.user_id else None,
                "created_at": run.created_at.isoformat(),
                "can_manage": _can_manage_run(run, user),
            }
            for run in runs
        ],
    }


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def _read_image(field) -> bytes:
    with field.open("rb") as handle:
        return handle.read()


def build_explanation_bundle(run: ExplanationRun) -> tuple[dict, dict[str, bytes]]:
    """Return a run's bundle description and images (as in ``cases.json``)."""
    from swarmmed_explain.bundle import SCHEMA, panel_file_name, summary_file_name

    cases = []
    images: dict[str, bytes] = {}
    for case in run.cases.prefetch_related("panels"):
        panels = []
        for panel in case.panels.all():
            name = panel_file_name(case.position, panel.position)
            images[name] = _read_image(panel.image)
            panels.append(
                {"method": panel.method, "title": panel.title or None, "file": name}
            )
        cases.append(
            {
                "case_id": case.case_id,
                "label": case.label or None,
                "prediction": case.prediction or None,
                "score": case.score,
                "target": case.target or None,
                "outcome": case.outcome or None,
                "details": case.details or {},
                "panels": panels,
            }
        )
    summaries = []
    for summary in run.summaries.all():
        name = summary_file_name(summary.position)
        images[name] = _read_image(summary.image)
        summaries.append({"title": summary.title or None, "file": name})
    data = {
        "schema": SCHEMA,
        "complete": True,
        "info": run.info or {},
        "cases": cases,
        "summaries": summaries,
    }
    return data, images


def _run_for_export(project: Project, *, job_identifier=None, run_identifier=None):
    if run_identifier:
        run = resolve_explanation_run(project, run_identifier)
    elif job_identifier:
        run = get_latest_successful_run(project, job_identifier=job_identifier)
        if run is None:
            raise LookupError("The job has no stored explanations.")
    else:
        raise ValueError("Give a job or an explanation run.")
    if not (run.status == "completed" and run.success):
        raise LookupError("The explanation run has no stored explanations.")
    return run


def export_explanations_to_directory(
    project: Project,
    user,
    *,
    output_dir,
    job_identifier: str | None = None,
    run_identifier: str | None = None,
) -> dict:
    """Write a run's bundle (``cases.json`` and PNGs) to a new or empty folder.

    The folder can be imported again with ``swarmed results explain-import``.
    """
    from swarmmed_explain.bundle import bundle_file_names, write_bundle

    run = _run_for_export(
        project, job_identifier=job_identifier, run_identifier=run_identifier
    )
    data, images = build_explanation_bundle(run)
    output_path = Path(output_dir).expanduser().resolve()
    written = write_bundle(output_path, data, images)
    mark_output_seen(run, user)
    names = ["cases.json", *bundle_file_names(written)]
    return {
        "run_identifier": str(run.identifier),
        "output_dir": str(output_path),
        "written_files": [str(output_path / name) for name in names],
    }


def write_explanation_zip(run: ExplanationRun, user, handle) -> None:
    """Write a run's bundle as a zip archive to a binary file object."""
    from swarmmed_explain.bundle import bundle_file_names, dumps_cases, validate_cases

    if not (run.status == "completed" and run.success):
        raise LookupError("The explanation run has no stored explanations.")
    data, images = build_explanation_bundle(run)
    normalised = validate_cases(data)
    with zipfile.ZipFile(handle, "w") as archive:
        archive.writestr(
            "cases.json", dumps_cases(normalised), compress_type=zipfile.ZIP_DEFLATED
        )
        for name in bundle_file_names(normalised):
            archive.writestr(name, images[name], compress_type=zipfile.ZIP_STORED)
    mark_output_seen(run, user)


def job_has_result_files(project: Project, flare_id: str) -> bool:
    """Return whether the job has result files (the results page lists those)."""
    s3 = get_s3_client()
    response = s3.list_objects_v2(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=f"{project.identifier}/results/{flare_id}/",
        MaxKeys=1,
    )
    return bool(response.get("Contents"))


def describe_explanations(
    project: Project,
    user,
    *,
    job_identifier: str,
    run_identifier: str | None = None,
    include_reviews: bool = False,
) -> dict:
    """Return a job's explanation runs and one run's cases (for the CLI).

    The run is ``run_identifier`` or the job's newest run with stored
    explanations. Each case lists the latest review of each reviewer, or every
    review with ``include_reviews``. Listing the cases counts as the user having
    seen the run's model output. In the list of runs, the script output of
    the other runs with stored explanations is left out (it can hold model
    output that the user has not been given).
    """
    runs = list(list_explanation_runs(project, job_identifier=job_identifier))
    if run_identifier:
        run = resolve_explanation_run(project, run_identifier)
    else:
        run = next(
            (item for item in runs if item.status == "completed" and item.success),
            None,
        )
    cases = []
    summaries = []
    if run is not None and run.status == "completed" and run.success:
        for case in run.cases.prefetch_related(
            "panels", "reviews", "reviews__reviewer"
        ):
            reviews = list(case.reviews.all())
            if not include_reviews:
                reviews = _latest_per_reviewer(reviews)
            cases.append(
                {
                    "identifier": str(case.identifier),
                    "position": case.position,
                    "case_id": case.case_id,
                    "label": case.label or None,
                    "prediction": case.prediction or None,
                    "score": case.score,
                    "target": case.target or None,
                    "outcome": case.outcome or None,
                    "details": case.details or {},
                    "panels": [
                        {
                            "position": panel.position,
                            "method": panel.method,
                            "title": panel.title or None,
                            "width": panel.width,
                            "height": panel.height,
                        }
                        for panel in case.panels.all()
                    ],
                    "reviews": [
                        serialize_review(review, user=user) for review in reviews
                    ],
                }
            )
        summaries = [
            {"position": summary.position, "title": summary.title or None}
            for summary in run.summaries.all()
        ]
        mark_output_seen(run, user)
    return {
        "project_identifier": str(project.identifier),
        "runs": [
            serialize_explanation_run(
                item,
                include_output=(
                    not _has_stored_cases(item)
                    or (run is not None and item.pk == run.pk)
                ),
            )
            for item in runs
        ],
        "run": (
            serialize_explanation_run(run, include_output=True)
            if run is not None
            else None
        ),
        "cases": cases,
        "summaries": summaries,
    }


def explain_page_config(project: Project, context: dict) -> dict:
    """Return the configuration of the results page's explainability section."""
    selected = context.get("selected_job_details")
    job_id = str(selected.identifier) if selected else context.get("selected_job_id")
    job_id = str(job_id) if job_id else ""
    if not _JOB_ID_RE.fullmatch(job_id):
        job_id = ""
    placeholder = "00000000-0000-0000-0000-000000000000"
    return {
        "job_id": job_id,
        "has_script": bool(project.explainability_script),
        "status_url": (
            reverse("results:explanation_status", kwargs={"job_id": job_id})
            if job_id
            else None
        ),
        "start_url": (
            reverse("results:start_explanation", kwargs={"job_id": job_id})
            if job_id
            else None
        ),
        "stop_url": reverse("results:stop_explanation"),
        "delete_url": reverse("results:delete_explanation"),
        "placeholder": placeholder,
        "cases_url": reverse(
            "results:explanation_cases", kwargs={"run_uuid": placeholder}
        ),
        "mode_url": reverse(
            "results:explanation_mode", kwargs={"run_uuid": placeholder}
        ),
        "download_url": reverse(
            "results:explanation_download", kwargs={"run_uuid": placeholder}
        ),
        "reviews_url": reverse(
            "results:explanation_reviews", kwargs={"case_uuid": placeholder}
        ),
    }
