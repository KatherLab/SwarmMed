import ast
import re

from django.db import migrations, models
from django.db.models import Q


UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
STATUS_PRIORITY = {
    "COMPLETED": 5,
    "FAILED": 4,
    "STOPPED": 3,
    "RUNNING": 2,
    "STARTING": 1,
    "PENDING": 0,
}


def _extract_flare_job_uuid(flare_job_id_raw):
    if not flare_job_id_raw:
        return None

    match = UUID_RE.search(str(flare_job_id_raw))
    if match:
        return match.group(1)

    try:
        parsed = ast.literal_eval(str(flare_job_id_raw))
    except (ValueError, SyntaxError, TypeError):
        return None

    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            data = item.get("data", "")
            if not isinstance(data, str):
                continue
            match = UUID_RE.search(data)
            if match:
                return match.group(1)
    return None


def _choose_raw_flare_job_id(jobs):
    candidates = [str(job.flare_job_id or "") for job in jobs if job.flare_job_id]
    if not candidates:
        return None
    return max(candidates, key=lambda value: (UUID_RE.search(value) is not None, len(value)))


def _backfill_and_dedupe_training_jobs(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    TrainingJob = apps.get_model("training", "TrainingJob")
    TrainingResult = apps.get_model("results", "TrainingResult")
    ResultsVisualizationRun = apps.get_model("results", "ResultsVisualizationRun")

    jobs_qs = TrainingJob.objects.using(db_alias).order_by("network_id", "created_at", "id")
    for job in jobs_qs.iterator():
        flare_job_uuid = _extract_flare_job_uuid(job.flare_job_id)
        if job.flare_job_uuid != flare_job_uuid:
            job.flare_job_uuid = flare_job_uuid
            job.save(update_fields=["flare_job_uuid"])

    duplicate_keys = (
        TrainingJob.objects.using(db_alias)
        .exclude(flare_job_uuid__isnull=True)
        .values_list("network_id", "flare_job_uuid")
        .distinct()
    )

    for network_id, flare_job_uuid in duplicate_keys:
        jobs = list(
            TrainingJob.objects.using(db_alias)
            .filter(network_id=network_id, flare_job_uuid=flare_job_uuid)
            .order_by("created_at", "id")
        )
        if len(jobs) <= 1:
            continue

        canonical = jobs[0]
        duplicates = jobs[1:]
        all_jobs = jobs

        best_status = max(
            (str(job.status or "").upper() for job in all_jobs),
            key=lambda status: STATUS_PRIORITY.get(status, -1),
        )
        completed_values = [job.completed_at for job in all_jobs if job.completed_at]
        total_rounds_values = [job.total_rounds for job in all_jobs if job.total_rounds]
        rounds_finished_values = [
            job.rounds_finished for job in all_jobs if job.rounds_finished is not None
        ]
        progress_values = [
            job.progress_percent for job in all_jobs if job.progress_percent is not None
        ]

        canonical.status = best_status
        canonical.flare_job_uuid = flare_job_uuid
        chosen_raw_id = _choose_raw_flare_job_id(all_jobs)
        if chosen_raw_id:
            canonical.flare_job_id = chosen_raw_id
        if completed_values:
            canonical.completed_at = min(completed_values)
        if total_rounds_values:
            canonical.total_rounds = max(total_rounds_values)
        if rounds_finished_values:
            canonical.rounds_finished = max(rounds_finished_values)
        if progress_values:
            canonical.progress_percent = max(progress_values)
        if best_status == "COMPLETED":
            canonical.progress_percent = 100
        elif canonical.progress_percent is not None:
            canonical.progress_percent = min(99, canonical.progress_percent)
        canonical.save()

        for duplicate in duplicates:
            for result in TrainingResult.objects.using(db_alias).filter(job=duplicate):
                existing = (
                    TrainingResult.objects.using(db_alias)
                    .filter(job=canonical, file_path=result.file_path)
                    .exclude(pk=result.pk)
                    .first()
                )
                if existing:
                    if result.file_size > existing.file_size:
                        existing.file_size = result.file_size
                        existing.save(update_fields=["file_size"])
                    result.delete()
                else:
                    result.job_id = canonical.id
                    result.save(update_fields=["job"])

            ResultsVisualizationRun.objects.using(db_alias).filter(job=duplicate).update(
                job=canonical
            )
            duplicate.delete()


def _noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("results", "0001_initial"),
        ("training", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="trainingjob",
            name="flare_job_uuid",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Canonical NVFlare job UUID extracted from flare_job_id.",
                max_length=36,
                null=True,
            ),
        ),
        migrations.RunPython(
            _backfill_and_dedupe_training_jobs,
            reverse_code=_noop_reverse,
        ),
        migrations.AddConstraint(
            model_name="trainingjob",
            constraint=models.UniqueConstraint(
                condition=Q(flare_job_uuid__isnull=False),
                fields=("network", "flare_job_uuid"),
                name="training_unique_flare_uuid_per_network",
            ),
        ),
    ]
