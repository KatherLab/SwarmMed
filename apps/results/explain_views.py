"""Views of the explainability section of the results page.

Every view works on the user's current project (``project_membership_required``
falls back to it because no URL here names a project) and looks objects up by
UUID within that project, so another project's runs, cases and images are
reported as not found. State changes use POST with the CSRF token.
"""

from __future__ import annotations

import logging
import tempfile
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse, JsonResponse
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
)

from logs import logger
from project.decorators import project_membership_required
from project.models import Project

from . import explanations as explain_services
from .views import get_user_project

_logger = logging.getLogger(__name__)

# Archives up to this size stay in memory while they are sent.
ZIP_SPOOL_BYTES = 32 * 1024 * 1024


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"success": False, "error": message}, status=status)


def _current_project(request) -> Project:
    project_uuid, _ = get_user_project(request)
    project = (
        Project.objects.filter(identifier=project_uuid).first()
        if project_uuid
        else None
    )
    if project is None:
        raise LookupError("No current project is selected.")
    return project


def _json_errors(view):
    """Turn service errors into JSON responses with a matching status code."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except LookupError as exc:
            return _error(str(exc), 404)
        except PermissionError as exc:
            return _error(str(exc), 403)
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception:
            _logger.exception("Explainability request failed")
            return _error("The request failed; the server log has the details.", 500)

    return wrapped


@login_required
@require_POST
@project_membership_required
@_json_errors
def start_explanation(request, job_id):
    """Start the project's explainability script for a job."""
    project = _current_project(request)
    run = explain_services.start_results_explanation(
        project=project, user=request.user, job_identifier=job_id
    )
    return JsonResponse(
        {
            "success": True,
            "run_id": str(run.identifier),
            "run": explain_services.serialize_explanation_run(run),
        }
    )


@login_required
@require_POST
@project_membership_required
@_json_errors
def stop_explanation(request):
    """Stop a pending or running explanation run (POST ``run_id``)."""
    project = _current_project(request)
    run = explain_services.stop_results_explanation(
        project=project, user=request.user, run_identifier=request.POST.get("run_id")
    )
    return JsonResponse(
        {"success": True, "run": explain_services.serialize_explanation_run(run)}
    )


@login_required
@require_POST
@project_membership_required
@_json_errors
def delete_explanation(request):
    """Delete a finished explanation run (POST ``run_id``)."""
    project = _current_project(request)
    explain_services.delete_explanation_run(
        project=project, user=request.user, run_identifier=request.POST.get("run_id")
    )
    return JsonResponse({"success": True})


@login_required
@require_GET
@project_membership_required
@_json_errors
def explanation_status(request, job_id):
    """Return the explanation runs of a job and the newest run's status."""
    project = _current_project(request)
    return JsonResponse(
        explain_services.explanation_status_payload(project, request.user, job_id)
    )


@login_required
@require_GET
@project_membership_required
@_json_errors
def explanation_cases(request, run_uuid):
    """Return a run's cases, panels, summaries and reviews as JSON.

    Applies the user's blinded mode; without it, the response is recorded as
    the user having seen the run's model output (an access record).
    """
    project = _current_project(request)
    run = explain_services.resolve_explanation_run(project, run_uuid)
    payload = explain_services.explanation_view_payload(run, request.user)
    if run.status == "completed" and run.success:
        logger.get_logger(user=request.user, project=project).access.info(
            f"Viewed explanations of run {run.identifier}"
        )
    return JsonResponse(payload)


@login_required
@require_POST
@project_membership_required
@_json_errors
def explanation_mode(request, run_uuid):
    """Switch the user's blinded review mode for a run (POST ``blinded``)."""
    project = _current_project(request)
    run = explain_services.resolve_explanation_run(project, run_uuid)
    blinded = str(request.POST.get("blinded", "")).lower() in {"1", "true", "on"}
    explain_services.set_reader_mode(run, request.user, blinded=blinded)
    return JsonResponse(explain_services.explanation_view_payload(run, request.user))


@login_required
@require_http_methods(["GET", "POST"])
@project_membership_required
@_json_errors
def explanation_reviews(request, case_uuid):
    """List a case's reviews (GET) or add the user's review (POST).

    POST takes ``verdict`` (agree, disagree, unsure) and ``note``; the reviewer
    is always the logged-in user. Reviews are never changed or deleted here.
    """
    project = _current_project(request)
    if request.method == "POST":
        review = explain_services.add_explanation_review(
            project=project,
            user=request.user,
            case_identifier=case_uuid,
            verdict=request.POST.get("verdict"),
            note=request.POST.get("note", ""),
        )
        return JsonResponse(
            {
                "success": True,
                "review": explain_services.serialize_review(review, user=request.user),
                "case": explain_services.explanation_case_view(
                    review.case, request.user
                ),
            },
            status=201,
        )
    return JsonResponse(
        explain_services.list_case_reviews(project, request.user, case_uuid)
    )


@login_required
@require_GET
@project_membership_required
def explanation_image(request, image_kind, image_uuid):
    """Serve one explanation image (PNG) of the current project."""
    try:
        project = _current_project(request)
        obj = explain_services.resolve_explanation_image(
            project, image_kind, image_uuid, user=request.user
        )
        with obj.image.open("rb") as handle:
            data = handle.read()
    except LookupError:
        return HttpResponse(
            "Image not found", status=404, content_type="text/plain; charset=utf-8"
        )
    except Exception:
        _logger.exception("Could not read an explanation image")
        return HttpResponse(
            "Image could not be read",
            status=500,
            content_type="text/plain; charset=utf-8",
        )
    response = HttpResponse(data, content_type="image/png")
    response["Content-Disposition"] = (
        f'inline; filename="explanation_{image_kind}_{obj.position}.png"'
    )
    response["Cache-Control"] = "private, no-store, no-transform"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@login_required
@require_POST
@project_membership_required
@_json_errors
def explanation_download(request, run_uuid):
    """Send a run's bundle (cases.json and PNG files) as a zip archive."""
    project = _current_project(request)
    run = explain_services.resolve_explanation_run(project, run_uuid)
    # FileResponse closes the file after sending it.
    spooled = tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_BYTES)  # noqa: SIM115
    try:
        explain_services.write_explanation_zip(run, request.user, spooled)
    except BaseException:
        spooled.close()
        raise
    spooled.seek(0)
    logger.get_logger(user=request.user, project=project).access.info(
        f"Downloaded explanations of run {run.identifier}"
    )
    response = FileResponse(
        spooled,
        as_attachment=True,
        filename=f"explanations_{run.identifier}.zip",
        content_type="application/zip",
    )
    response["Cache-Control"] = "private, no-store"
    return response
