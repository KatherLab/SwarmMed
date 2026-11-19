"""The cap on data-loader workers in the breast-MRI worked example.

MediSwarm's data module in the training image starts one DataLoader worker per CPU
that ``torch.multiprocessing`` reports, which on hosts with many CPUs exceeded a
client's memory. ``experiments/duke_mst_cli/training.py`` therefore caps that count
with ``swarmmed_loader_workers.py`` before it imports MediSwarm's code. Both files are
loaded by path here, as the FLARE job runs them; MediSwarm's modules are replaced by
stand-ins that record the CPU count they see, so the training image is not needed.
"""

import importlib.util
import multiprocessing
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch.multiprocessing
from django.test import SimpleTestCase

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "experiments" / "duke_mst_cli"
VARIABLE = "SWARMMED_MAX_LOADER_WORKERS"

# Stands in for MediSwarm's threedcnn_ptl in the paper's image: it records the CPU
# count when it is imported, as the data module's default argument does, and when
# the data module is built, then stops main() before any training.
FAKE_THREEDCNN_PTL = textwrap.dedent(
    """
    import logging
    import torch.multiprocessing as mp

    CPU_COUNT_AT_IMPORT = mp.cpu_count()


    class StopBeforeTraining(Exception):
        pass


    def set_up_logging():
        return logging.getLogger("fake_threedcnn_ptl")


    def prepare_training(logger, **kwargs):
        raise StopBeforeTraining(mp.cpu_count())
    """
)

# MediSwarm 1.8.0 and later have a module loader_workers of their own (abridged
# here). Their threedcnn_ptl imports it by that name and calls it when it builds the
# data module, with at most 16 workers by default.
FAKE_MEDISWARM_LOADER_WORKERS = textwrap.dedent(
    """
    def cap_loader_workers(configured, num_train_samples, min_samples_per_worker=4):
        return min(
            int(configured), max(1, int(num_train_samples) // min_samples_per_worker)
        )
    """
)
FAKE_THREEDCNN_PTL_1_8 = textwrap.dedent(
    """
    import logging
    import torch.multiprocessing as mp
    from loader_workers import cap_loader_workers


    class StopBeforeTraining(Exception):
        pass


    def set_up_logging():
        return logging.getLogger("fake_threedcnn_ptl")


    def prepare_training(logger, **kwargs):
        raise StopBeforeTraining(cap_loader_workers(min(mp.cpu_count(), 16), 416))
    """
)


def _load(name, path):
    """Import a file of the example without writing __pycache__ into it."""
    with patch.object(sys, "dont_write_bytecode", True):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _LoaderWorkersTestCase(SimpleTestCase):
    def setUp(self):
        self.lw = _load(
            "duke_mst_loader_workers", EXAMPLE_DIR / "swarmmed_loader_workers.py"
        )
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.cgroup = Path(temp_dir.name) / "cgroup"
        self.cgroup.mkdir()
        # limit_loader_workers replaces these for the rest of the process; restore them.
        for module in (multiprocessing, torch.multiprocessing):
            patcher = patch.object(module, "cpu_count", module.cpu_count)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, relative, text):
        path = self.cgroup / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def host(self, cpus, affinity):
        """Patch the host's CPU count and the process's CPU affinity."""
        cpu_count = patch.object(os, "cpu_count", return_value=cpus)
        sched_getaffinity = patch.object(
            os, "sched_getaffinity", return_value=set(range(affinity)), create=True
        )
        cpu_count.start()
        sched_getaffinity.start()
        self.addCleanup(cpu_count.stop)
        self.addCleanup(sched_getaffinity.stop)


class LoaderWorkerCapTests(_LoaderWorkersTestCase):
    def test_unset_or_empty_gives_16(self):
        for environ in ({}, {VARIABLE: ""}, {VARIABLE: "  "}):
            with self.subTest(environ=environ), self.assertNoLogs(self.lw.LOGGER):
                self.assertEqual(self.lw.loader_worker_cap(environ), 16)

    def test_a_positive_whole_number_is_the_cap(self):
        self.assertEqual(self.lw.loader_worker_cap({VARIABLE: "8"}), 8)
        self.assertEqual(self.lw.loader_worker_cap({VARIABLE: " 32 "}), 32)
        with patch.dict(os.environ, {VARIABLE: "6"}):
            self.assertEqual(self.lw.loader_worker_cap(), 6)

    def test_other_values_give_16_with_a_warning(self):
        for raw in ("0", "-4", "eight", "2.5"):
            with (
                self.subTest(raw=raw),
                self.assertLogs(self.lw.LOGGER, "WARNING") as logs,
            ):
                self.assertEqual(self.lw.loader_worker_cap({VARIABLE: raw}), 16)
            self.assertIn(f"{VARIABLE}={raw!r} is not a positive", logs.output[0])


class CgroupCpuLimitTests(_LoaderWorkersTestCase):
    def test_no_cgroup_files_means_no_limit(self):
        self.assertIsNone(self.lw.cgroup_cpu_limit(self.cgroup))
        self.assertIsNone(self.lw.cgroup_cpu_limit(self.cgroup / "missing"))

    def test_cgroup_v2_quota_in_whole_cpus_rounded_up(self):
        for text, cpus in (
            ("max 100000\n", None),
            ("1600000 100000\n", 16),
            ("150000 100000\n", 2),
            ("50000 100000\n", 1),
        ):
            with self.subTest(text=text):
                self.write("cpu.max", text)
                self.assertEqual(self.lw.cgroup_cpu_limit(self.cgroup), cpus)

    def test_cgroup_v1_quota(self):
        self.write("cpu/cpu.cfs_period_us", "100000\n")
        self.write("cpu/cpu.cfs_quota_us", "-1\n")
        self.assertIsNone(self.lw.cgroup_cpu_limit(self.cgroup))
        self.write("cpu/cpu.cfs_quota_us", "800000\n")
        self.assertEqual(self.lw.cgroup_cpu_limit(self.cgroup), 8)

    def test_cgroup_v1_combined_controller_folder(self):
        self.write("cpu,cpuacct/cpu.cfs_period_us", "100000\n")
        self.write("cpu,cpuacct/cpu.cfs_quota_us", "250000\n")
        self.assertEqual(self.lw.cgroup_cpu_limit(self.cgroup), 3)

    def test_the_smaller_quota_wins_when_both_are_present(self):
        self.write("cpu.max", "1200000 100000\n")
        self.write("cpu/cpu.cfs_period_us", "100000\n")
        self.write("cpu/cpu.cfs_quota_us", "400000\n")
        self.assertEqual(self.lw.cgroup_cpu_limit(self.cgroup), 4)

    def test_unreadable_or_malformed_files_are_ignored(self):
        for text in ("", "lots 100000", "1600000", "1600000 0", "0 100000"):
            with self.subTest(text=text):
                self.write("cpu.max", text)
                self.assertIsNone(self.lw.cgroup_cpu_limit(self.cgroup))
        (self.cgroup / "cpu.max").write_bytes(b"\xff\xfe 100000")
        self.assertIsNone(self.lw.cgroup_cpu_limit(self.cgroup))
        (self.cgroup / "cpu.max").unlink()
        (self.cgroup / "cpu.max").mkdir()
        self.write("cpu/cpu.cfs_quota_us", "800000\n")  # no period file
        self.assertIsNone(self.lw.cgroup_cpu_limit(self.cgroup))


class AvailableCpusTests(_LoaderWorkersTestCase):
    def test_smallest_of_host_affinity_and_quota(self):
        self.host(cpus=36, affinity=20)
        self.assertEqual(self.lw.available_cpus(self.cgroup), 20)
        self.write("cpu.max", "1600000 100000\n")
        self.assertEqual(self.lw.available_cpus(self.cgroup), 16)

    def test_host_count_alone_when_smaller(self):
        self.host(cpus=8, affinity=8)
        self.write("cpu.max", "1600000 100000\n")
        self.assertEqual(self.lw.available_cpus(self.cgroup), 8)

    def test_at_least_one_and_affinity_errors_ignored(self):
        with (
            patch.object(os, "cpu_count", return_value=None),
            patch.object(os, "sched_getaffinity", side_effect=OSError, create=True),
        ):
            self.assertEqual(self.lw.available_cpus(self.cgroup), 1)

    def test_platforms_without_affinity(self):
        no_affinity = SimpleNamespace(cpu_count=lambda: 12)
        with patch.object(self.lw, "os", no_affinity):
            self.assertEqual(self.lw.available_cpus(self.cgroup), 12)


class LimitLoaderWorkersTests(_LoaderWorkersTestCase):
    def test_paper_hosts_keep_16_workers(self):
        self.host(cpus=16, affinity=16)
        result = self.lw.limit_loader_workers({}, self.cgroup)
        self.assertEqual(tuple(result), (16, 16, 16, 16))
        self.assertEqual(multiprocessing.cpu_count(), 16)
        self.assertEqual(torch.multiprocessing.cpu_count(), 16)

    def test_many_cpu_host_is_capped_at_16(self):
        self.host(cpus=36, affinity=36)
        result = self.lw.limit_loader_workers({}, self.cgroup)
        self.assertEqual(multiprocessing.cpu_count(), 16)
        self.assertEqual(torch.multiprocessing.cpu_count(), 16)
        self.assertEqual(
            result.summary(),
            "Data-loader workers: 16 (host CPUs: 36, available: 36, cap 16)",
        )

    def test_container_quota_below_the_cap(self):
        self.host(cpus=36, affinity=36)
        self.write("cpu.max", "400000 100000\n")
        result = self.lw.limit_loader_workers({}, self.cgroup)
        self.assertEqual(torch.multiprocessing.cpu_count(), 4)
        self.assertEqual(
            result.summary(),
            "Data-loader workers: 4 (host CPUs: 36, available: 4, cap 16)",
        )

    def test_cap_from_the_variable(self):
        self.host(cpus=36, affinity=36)
        result = self.lw.limit_loader_workers({VARIABLE: "8"}, self.cgroup)
        self.assertEqual((result.workers, result.cap), (8, 8))
        self.assertEqual(multiprocessing.cpu_count(), 8)


class TrainingScriptTests(SimpleTestCase):
    """training.py caps the count before MediSwarm's code first reads it."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp = Path(temp_dir.name)
        self.mediswarm = self.temp / "mediswarm"
        self.mediswarm.mkdir()
        # As in the FLARE job, the job's custom folder (training.py and its helper)
        # is on the path, and MediSwarm's folder is not until main() adds it.
        for patcher in (
            patch.object(sys, "path", [*sys.path, str(EXAMPLE_DIR)]),
            patch.object(multiprocessing, "cpu_count", multiprocessing.cpu_count),
            patch.object(
                torch.multiprocessing, "cpu_count", torch.multiprocessing.cpu_count
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("threedcnn_ptl", "loader_workers", "swarmmed_loader_workers"):
            self.addCleanup(sys.modules.pop, name, None)

    def run_main(self):
        """Run training.py's main() until MediSwarm's stand-in stops it.

        Returns:
            The stand-in threedcnn_ptl module, the arguments it stopped with and
            the lines logged through its logger.
        """
        training = _load("duke_mst_training", EXAMPLE_DIR / "training.py")
        environ = {
            "SITE_NAME": "node_A",
            "DATA_DIR": str(self.temp),
            "SCRATCH_DIR": str(self.temp),
            # 1 is never more than the CPUs available, so the result is known.
            VARIABLE: "1",
        }

        def add_mediswarm_paths():
            # Like _add_mediswarm_paths() in the image: MediSwarm's folder first.
            sys.path.insert(0, str(self.mediswarm))

        with (
            patch.dict(os.environ, environ),
            patch.object(training, "_add_mediswarm_paths", add_mediswarm_paths),
            patch.object(training.flare_util, "init"),
            patch.object(training.flare, "get_site_name", return_value="node-A"),
            self.assertLogs("fake_threedcnn_ptl", "INFO") as logs,
            self.assertRaises(Exception) as stopped,
        ):
            training.main()

        mediswarm = sys.modules["threedcnn_ptl"]
        self.assertIsInstance(
            stopped.exception, mediswarm.StopBeforeTraining, repr(stopped.exception)
        )
        return mediswarm, stopped.exception.args, logs.output

    def test_mediswarm_sees_the_capped_count_from_its_import_on(self):
        (self.mediswarm / "threedcnn_ptl.py").write_text(FAKE_THREEDCNN_PTL)
        mediswarm, args, log = self.run_main()
        self.assertEqual(mediswarm.CPU_COUNT_AT_IMPORT, 1)
        self.assertEqual(args, (1,))
        summary = [line for line in log if "Data-loader workers" in line]
        self.assertEqual(len(summary), 1)
        self.assertIn("Data-loader workers: 1 (host CPUs: ", summary[0])
        self.assertTrue(summary[0].endswith(", cap 1)"))

    def test_mediswarm_1_8_imports_its_own_loader_workers_module(self):
        (self.mediswarm / "loader_workers.py").write_text(FAKE_MEDISWARM_LOADER_WORKERS)
        (self.mediswarm / "threedcnn_ptl.py").write_text(FAKE_THREEDCNN_PTL_1_8)
        _, args, _ = self.run_main()
        self.assertEqual(
            Path(sys.modules["loader_workers"].__file__).parent, self.mediswarm
        )
        # MediSwarm's own cap, applied to the CPU count training.py set.
        self.assertEqual(args, (1,))
