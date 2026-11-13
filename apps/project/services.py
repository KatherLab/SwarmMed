"""Shared project services used by the UI and CLI."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import User
from django.core.files import File
from django.core.files.storage import default_storage

from logs import logger
from network.models import UserCurrentNetwork
from users.models import Profile

from .models import Project, UserCurrentProject
from .utils import (
    clean_folder_path,
    handle_training_code_upload,
    process_member_identifiers,
    training_code_upload_error,
)

SPECIAL_CODE_DIR_FILES = {
    "requirements.txt": "requirements_file",
    "validation.py": "data_validation_script",
    "visualization.py": "data_visualization_script",
    "results_visualization.py": "results_visualization_script",
    "explainability.py": "explainability_script",
}

# Storage folder (below the project's identifier) of each special file field.
SPECIAL_FIELD_FOLDERS = {
    "requirements_file": "code/requirements/",
    "data_validation_script": "code/data_validation/",
    "data_visualization_script": "code/data_visualization/",
    "results_visualization_script": "code/results_visualization/",
    "explainability_script": "code/explainability/",
}


def user_can_access_project(user: User, project: Project) -> bool:
    """Return whether a user is the author or a member of a project."""
    return bool(
        project.author_id == user.id
        or project.members.filter(id=user.id).exists()
    )


def require_project_access(user: User, project: Project) -> Project:
    """Return the project or raise when the user cannot access it."""
    if not user_can_access_project(user, project):
        raise PermissionError(
            f"User '{user.username}' does not have access to project '{project.title}'."
        )
    return project


def list_user_projects(user: User, include_archived: bool = True):
    """List projects visible to a user."""
    queryset = Project.objects.filter(author=user) | Project.objects.filter(
        members=user
    )
    queryset = queryset.distinct().select_related("author").prefetch_related(
        "members"
    )
    if not include_archived:
        queryset = queryset.exclude(status="ARCHIVED")
    return queryset.order_by("-created_at")


def get_project_for_user(user: User, identifier: str) -> Project:
    """Resolve a project by UUID identifier and validate access."""
    project = Project.objects.filter(identifier=identifier).select_related(
        "author"
    ).prefetch_related("members").first()
    if not project:
        raise LookupError(f"Project '{identifier}' was not found.")
    return require_project_access(user, project)


def get_current_project(user: User) -> Project | None:
    """Return the user's current project, if any."""
    relation = (
        UserCurrentProject.objects.select_related("project")
        .filter(user=user)
        .first()
    )
    if not relation or not relation.project:
        return None
    if not user_can_access_project(user, relation.project):
        relation.delete()
        return None
    return relation.project


def set_current_project(user: User, project: Project) -> Project:
    """Set the active project for a user and clear the active network."""
    require_project_access(user, project)
    UserCurrentProject.objects.update_or_create(
        user=user, defaults={"project": project}
    )
    UserCurrentNetwork.objects.filter(user=user).delete()
    return project


def _member_identifiers_to_string(member_identifiers: list[str] | None) -> str:
    if member_identifiers is None:
        return ""
    return "\n".join(str(identifier).strip() for identifier in member_identifiers if str(identifier).strip())


def _replace_field_from_path(project: Project, field_name: str, source_path: Path) -> None:
    field = getattr(project, field_name)
    if field:
        field.delete(save=False)
    with source_path.open("rb") as handle:
        field.save(source_path.name, File(handle), save=False)


def _delete_project_field(project: Project, field_name: str) -> None:
    field = getattr(project, field_name)
    if field:
        field.delete(save=False)
    setattr(project, field_name, None)


def ingest_code_directory(project: Project, code_dir: str | Path) -> Project:
    """Replace the project's code bundle from a local directory."""
    code_dir_path = Path(code_dir).expanduser().resolve()
    if not code_dir_path.exists() or not code_dir_path.is_dir():
        raise ValueError(f"Code directory '{code_dir}' does not exist or is not a directory.")

    training_path = code_dir_path / "training.py"
    if not training_path.exists() or not training_path.is_file():
        raise ValueError(
            f"Code directory '{code_dir}' must contain a root-level training.py file."
        )

    clean_folder_path(project.identifier, "code/training/")
    for field_name, subfolder in SPECIAL_FIELD_FOLDERS.items():
        clean_folder_path(project.identifier, subfolder)
        _delete_project_field(project, field_name)

    if project.training_code:
        project.training_code.delete(save=False)
    with training_path.open("rb") as handle:
        project.training_code.save("training.py", File(handle), save=False)

    for path in sorted(code_dir_path.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(code_dir_path)
        # Python's bytecode caches are not code to upload.
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        relative_posix = relative_path.as_posix()

        if relative_posix == "training.py":
            continue

        if relative_path.parent == Path(".") and path.name in SPECIAL_CODE_DIR_FILES:
            _replace_field_from_path(project, SPECIAL_CODE_DIR_FILES[path.name], path)
            continue

        storage_key = (
            f"{project.identifier}/code/training/{relative_posix}"
        )
        with path.open("rb") as handle:
            default_storage.save(storage_key, File(handle))

    project.save()
    return project


def _check_training_code_upload(request) -> None:
    """Refuse uploaded training code without a root-level training.py.

    Runs before anything is created or changed, so a refused upload leaves no
    half-created project and keeps a project's current code.
    """
    if request is None:
        return
    error = training_code_upload_error(request.POST, request.FILES)
    if error:
        raise ValueError(error)


def _apply_uploaded_project_files(
    project: Project,
    *,
    request=None,
    training_code_file=None,
    requirements_file=None,
    data_validation_script=None,
    data_visualization_script=None,
    results_visualization_script=None,
    explainability_script=None,
    remove_explainability_script: bool = False,
) -> Project:
    """Apply uploaded project files from the web UI to a project."""
    training_handled = False
    if request is not None:
        training_handled = handle_training_code_upload(project, request)

    if training_code_file is not None and not training_handled:
        project.training_code = training_code_file

    if requirements_file is not None:
        project.requirements_file = requirements_file
    if data_validation_script is not None:
        project.data_validation_script = data_validation_script
    if data_visualization_script is not None:
        project.data_visualization_script = data_visualization_script
    if results_visualization_script is not None:
        project.results_visualization_script = results_visualization_script
    if explainability_script is not None:
        project.explainability_script = explainability_script
    elif remove_explainability_script:
        remove_explainability_script_file(project, save=False)

    project.save()
    return project


def remove_explainability_script_file(
    project: Project, *, save: bool = True
) -> Project:
    """Remove a project's explainability script and its storage folder.

    Explanations already computed stay; only new runs need the script.
    """
    clean_folder_path(
        project.identifier, SPECIAL_FIELD_FOLDERS["explainability_script"]
    )
    _delete_project_field(project, "explainability_script")
    if save:
        project.save()
    return project


def create_project(
    *,
    author: User,
    title: str,
    description: str = "",
    member_identifiers: list[str] | None = None,
    code_dir: str | Path | None = None,
    request=None,
    training_code_file=None,
    requirements_file=None,
    data_validation_script=None,
    data_visualization_script=None,
    results_visualization_script=None,
    explainability_script=None,
) -> Project:
    """Create a project from UI or CLI inputs."""
    if code_dir is None:
        _check_training_code_upload(request)
    project = Project.objects.create(
        title=title,
        description=description,
        author=author,
    )
    process_member_identifiers(
        project, _member_identifiers_to_string(member_identifiers)
    )

    if code_dir is not None:
        project = ingest_code_directory(project, code_dir)
    else:
        project = _apply_uploaded_project_files(
            project,
            request=request,
            training_code_file=training_code_file,
            requirements_file=requirements_file,
            data_validation_script=data_validation_script,
            data_visualization_script=data_visualization_script,
            results_visualization_script=results_visualization_script,
            explainability_script=explainability_script,
        )

    log = logger.get_logger(user=author, project=project)
    log.project.info(f"PROJECT CREATED SUCCESSFULLY - {project.title}")
    return project


def update_project(
    *,
    actor: User,
    project: Project,
    title: str | None = None,
    description: str | None = None,
    member_identifiers: list[str] | None = None,
    replace_members: bool = False,
    code_dir: str | Path | None = None,
    request=None,
    training_code_file=None,
    requirements_file=None,
    data_validation_script=None,
    data_visualization_script=None,
    results_visualization_script=None,
    explainability_script=None,
    remove_explainability_script: bool = False,
) -> Project:
    """Update project metadata and files from UI or CLI inputs."""
    if actor != project.author:
        raise PermissionError(
            f"Only the project author can update '{project.title}'."
        )
    if code_dir is None:
        _check_training_code_upload(request)

    if title is not None:
        project.title = title
    if description is not None:
        project.description = description
    project.save()

    if replace_members:
        process_member_identifiers(
            project, _member_identifiers_to_string(member_identifiers)
        )
        UserCurrentProject.objects.filter(project=project).exclude(
            user=project.author
        ).exclude(user__in=project.members.all()).delete()

    if code_dir is not None:
        project = ingest_code_directory(project, code_dir)
    else:
        project = _apply_uploaded_project_files(
            project,
            request=request,
            training_code_file=training_code_file,
            requirements_file=requirements_file,
            data_validation_script=data_validation_script,
            data_visualization_script=data_visualization_script,
            results_visualization_script=results_visualization_script,
            explainability_script=explainability_script,
            remove_explainability_script=remove_explainability_script,
        )

    log = logger.get_logger(user=actor, project=project)
    log.project.info(f"Project updated successfully: {project.title}")
    return project


def project_member_profile_identifiers(project: Project) -> list[str]:
    """Return member profile UUIDs for a project."""
    identifiers: list[str] = []
    for member in project.members.all():
        profile = Profile.objects.filter(user=member).first()
        if profile:
            identifiers.append(str(profile.identifier))
    return identifiers


def serialize_project(project: Project, *, current_for: User | None = None) -> dict:
    """Serialize a project for CLI output."""
    current_project = get_current_project(current_for) if current_for else None
    return {
        "identifier": str(project.identifier),
        "title": project.title,
        "description": project.description,
        "status": project.status,
        "author": project.author.username,
        "member_identifiers": project_member_profile_identifiers(project),
        "is_current": bool(current_project and current_project.id == project.id),
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }
