"""How many data-loader workers MediSwarm's training code may start.

MediSwarm's data module in the training image starts one DataLoader worker per
CPU of the host (``torch.multiprocessing.cpu_count()``). Every worker holds the
volumes it is preparing, so on hosts with many CPUs the workers alone can exceed
the memory available to the client. ``training.py`` calls ``limit_loader_workers``
before it imports MediSwarm's code, so that the CPU count the data module reads is
the number of workers to start: at most ``SWARMMED_MAX_LOADER_WORKERS`` (default
16) and no more than the CPUs available to the process.

MediSwarm 1.8.0 and later have a module ``loader_workers`` with a function
``cap_loader_workers`` of their own, which their training code imports by that name.
This module is shipped in the job's custom folder, which is on the module search
path too, so a module ``loader_workers`` here would replace MediSwarm's when
``training.py`` imported it first. This module and its function therefore have
other names.

The module needs neither NVFlare nor the training image, so it is tested on its own.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing
import os
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

MAX_LOADER_WORKERS_ENV = "SWARMMED_MAX_LOADER_WORKERS"
# The CPU count of the hosts of the paper's runs, which therefore ran 16 workers.
DEFAULT_MAX_LOADER_WORKERS = 16
CGROUP_ROOT = Path("/sys/fs/cgroup")

LOGGER = logging.getLogger(__name__)


class LoaderWorkers(NamedTuple):
    """The number of data-loader workers and the numbers it was derived from."""

    workers: int
    host_cpus: int
    available_cpus: int
    cap: int

    def summary(self) -> str:
        """Return the line that ``training.py`` logs at startup."""
        return (
            f"Data-loader workers: {self.workers} (host CPUs: {self.host_cpus}, "
            f"available: {self.available_cpus}, cap {self.cap})"
        )


def _quota_cpus(quota: int, period: int) -> int | None:
    """Return a CPU quota in whole CPUs, rounded up, or None if it sets no limit."""
    if quota <= 0 or period <= 0:
        return None
    return -(-quota // period)


def _cgroup_v2_cpus(cgroup_root: Path) -> int | None:
    """Return the quota in cgroup v2's ``cpu.max`` ("<quota> <period>")."""
    try:
        fields = (cgroup_root / "cpu.max").read_text().split()
        if len(fields) != 2 or fields[0] == "max":
            return None
        return _quota_cpus(int(fields[0]), int(fields[1]))
    except (OSError, ValueError):
        return None


def _cgroup_v1_cpus(cgroup_root: Path) -> int | None:
    """Return the quota in cgroup v1's ``cpu.cfs_quota_us`` (-1 for none)."""
    for controller in ("cpu", "cpu,cpuacct"):
        folder = cgroup_root / controller
        try:
            quota = int((folder / "cpu.cfs_quota_us").read_text())
            period = int((folder / "cpu.cfs_period_us").read_text())
        except (OSError, ValueError):
            continue
        return _quota_cpus(quota, period)
    return None


def cgroup_cpu_limit(cgroup_root: Path = CGROUP_ROOT) -> int | None:
    """Return the CPU quota of the process's container in whole CPUs.

    Reads cgroup v2's ``cpu.max`` (quota ``max`` for no limit) and cgroup v1's
    ``cpu.cfs_quota_us`` and ``cpu.cfs_period_us`` (quota -1 for no limit). A file
    that is missing or cannot be read or parsed counts as no limit.

    Args:
        cgroup_root: Where the cgroup file system is mounted.

    Returns:
        The quota divided by the period, rounded up, or None without a quota.
    """
    limits = [
        limit
        for limit in (_cgroup_v2_cpus(cgroup_root), _cgroup_v1_cpus(cgroup_root))
        if limit is not None
    ]
    return min(limits) if limits else None


def available_cpus(cgroup_root: Path = CGROUP_ROOT) -> int:
    """Return the number of CPUs this process can use.

    The smallest of the host's CPU count, the number of CPUs the process may run
    on (its CPU affinity, where the platform reports it) and the container's CPU
    quota (``cgroup_cpu_limit``).

    Args:
        cgroup_root: Where the cgroup file system is mounted.

    Returns:
        The number of CPUs, at least 1.
    """
    counts = [os.cpu_count() or 1]
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        with contextlib.suppress(OSError):
            counts.append(len(sched_getaffinity(0)))
    limit = cgroup_cpu_limit(cgroup_root)
    if limit is not None:
        counts.append(limit)
    return max(1, min(counts))


def loader_worker_cap(environ: Mapping[str, str] | None = None) -> int:
    """Return the most data-loader workers to start.

    Args:
        environ: The environment to read ``SWARMMED_MAX_LOADER_WORKERS`` from;
            ``os.environ`` by default.

    Returns:
        The variable's value, or 16 if it is unset or empty. A value that is not a
        positive whole number also gives 16, with a warning.
    """
    environ = os.environ if environ is None else environ
    raw = str(environ.get(MAX_LOADER_WORKERS_ENV, "")).strip()
    if not raw:
        return DEFAULT_MAX_LOADER_WORKERS
    try:
        cap = int(raw)
    except ValueError:
        cap = 0
    if cap < 1:
        LOGGER.warning(
            "%s=%r is not a positive whole number; using %d data-loader workers "
            "at most",
            MAX_LOADER_WORKERS_ENV,
            raw,
            DEFAULT_MAX_LOADER_WORKERS,
        )
        return DEFAULT_MAX_LOADER_WORKERS
    return cap


def limit_loader_workers(
    environ: Mapping[str, str] | None = None, cgroup_root: Path = CGROUP_ROOT
) -> LoaderWorkers:
    """Make the CPU count that MediSwarm's data module reads the number of workers.

    Replaces ``multiprocessing.cpu_count`` and ``torch.multiprocessing.cpu_count``
    for the rest of the process with a function that returns the smaller of
    ``loader_worker_cap`` and ``available_cpus``. MediSwarm reads the count when
    its data module is imported and again when the module is built, so call this
    before importing MediSwarm's code.

    Args:
        environ: The environment to read the cap from; ``os.environ`` by default.
        cgroup_root: Where the cgroup file system is mounted.

    Returns:
        The number of workers, the host's CPU count, the CPUs available and the cap.
    """
    import torch.multiprocessing as torch_multiprocessing

    cap = loader_worker_cap(environ)
    available = available_cpus(cgroup_root)
    workers = min(cap, available)

    def capped_cpu_count() -> int:
        return workers

    multiprocessing.cpu_count = capped_cpu_count
    torch_multiprocessing.cpu_count = capped_cpu_count
    return LoaderWorkers(workers, os.cpu_count() or 1, available, cap)
