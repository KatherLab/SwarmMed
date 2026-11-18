"""Warnings for a running training job that has stalled.

Every site must return its result in every round of a swarm run, so when one
site drops out, because its training process has ended or its FL client has
stopped, the other sites wait for it. Whether NVIDIA FLARE then ends the job
depends on the time limits of the FL server's FLARE build; with long limits the
job stays "Running" for hours. The job monitor (:mod:`training.tasks`) therefore
warns about a running job when the FL server reports a site as dropped out, or
when no round has finished for ``SWARMMEDHUB_STALL_WARNING_MINUTES``. The
warning does not change the job's status.
"""

from __future__ import annotations

import math
import os
import re
from datetime import UTC, datetime, timedelta

from .utils import (
    DROPOUT_DEAD,
    DROPOUT_DISCONNECTED,
    DROPOUT_MISSING,
    DROPOUT_REPORTED,
    ClientDropout,
)

STALL_WARNING_ENV = "SWARMMEDHUB_STALL_WARNING_MINUTES"
DEFAULT_STALL_WARNING_MINUTES = 60

_RESUME_ADVICE = "stop this job and resume it (swarmed training resume)"
_DROPOUT_DETAILS = {
    DROPOUT_MISSING: "the FL server reports its training job as missing",
    DROPOUT_DEAD: "the FL server has lost contact with its FL client",
    DROPOUT_DISCONNECTED: "the FL server reports it as disconnected",
    DROPOUT_REPORTED: "the FL server reports its training job as dead",
}
_WARNING_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
# Invalid values already reported, so that the monitor warns once per value
# rather than every minute.
_reported_invalid_values: set[str] = set()


def stall_warning_minutes(log=None) -> float:
    """Return the minutes without a finished round after which a job is flagged.

    Reads ``SWARMMEDHUB_STALL_WARNING_MINUTES`` (default 60); 0 turns the check
    off. Invalid values fall back to the default, with a warning in the log the
    first time each value is seen.

    Args:
        log: Optional platform logger for rejected values.

    Returns:
        float: The minutes, or 0 when the check is off.
    """
    raw = os.getenv(STALL_WARNING_ENV, "").strip()
    if not raw:
        return DEFAULT_STALL_WARNING_MINUTES
    try:
        minutes = float(raw)
    except ValueError:
        minutes = math.nan
    if math.isfinite(minutes) and minutes >= 0:
        return minutes
    if log is not None and raw not in _reported_invalid_values:
        _reported_invalid_values.add(raw)
        log.training.warning(
            f"Ignoring {STALL_WARNING_ENV}={raw!r}: expected a number of minutes "
            f"(0 turns the warning off); using {DEFAULT_STALL_WARNING_MINUTES}."
        )
    return DEFAULT_STALL_WARNING_MINUTES


def _format_time(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def describe_stall(
    *,
    dropouts: list[ClientDropout],
    last_round_at: datetime | None,
    now: datetime,
    threshold_minutes: float,
) -> str:
    """Return the stall warning for a running job, or "" if it is not stalled.

    Args:
        dropouts: The sites the FL server reports as dropped out of the job.
        last_round_at: When a round last finished, or when the job started.
        now: The current time.
        threshold_minutes: Minutes without a finished round before the job is
            flagged; 0 turns this check off.

    Returns:
        str: One short message that names what happened and what to do. Times
        are given in UTC, with the date.
    """
    if dropouts:
        sentences = [
            f"Site {dropout.client} dropped out at "
            f"{_format_time(dropout.first_reported_at)}: "
            f"{_DROPOUT_DETAILS[dropout.reason]}."
            for dropout in dropouts
        ]
        sites = "the site" if len(dropouts) == 1 else "these sites"
        sentences.append(
            f"The other sites are waiting; check {sites}, then {_RESUME_ADVICE}."
        )
        return " ".join(sentences)
    if (
        threshold_minutes
        and last_round_at is not None
        and now - last_round_at > timedelta(minutes=threshold_minutes)
    ):
        return (
            f"No round has finished since {_format_time(last_round_at)} "
            f"(more than {threshold_minutes:g} min). Check that every site is "
            f"still training; if one has dropped out, {_RESUME_ADVICE}."
        )
    return ""


def same_stall(previous: str, current: str) -> bool:
    """Return True when two stall warnings differ at most in the times they give.

    The monitor reads only the end of the logs, so after hours the first report
    of a site's dropout may no longer be among the lines read; the monitor then
    keeps the warning it gave first.
    """
    return bool(previous) and _WARNING_TIME_RE.sub("", previous) == (
        _WARNING_TIME_RE.sub("", current)
    )
