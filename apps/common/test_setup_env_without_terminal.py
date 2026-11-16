"""Tests for `make env` (scripts/setup_env.py) once its input has ended.

`make start` and `make restart` run `make env` every time. With no input left
(`</dev/null`, a CI job, `ssh host make restart` without input, Ctrl-D),
input() raised EOFError: an existing deployment whose .env lacked a newly added
key, or still held the SWARMMEDHUB_HOSTNAME placeholder, stopped with a
traceback before the stack started. The script still reads answers from a
terminal or a pipe; once the input has ended it keeps existing values, takes
the template default for a missing key and says so, and stops with a message,
leaving .env unchanged, when a placeholder still needs a value.

Each test runs the script in a temporary directory. The repository's own
template is used where the test gives .env a DOCKER_HOST_IP, so Docker is never
queried.
"""

import os
import pty
import stat
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SCRIPT = Path(settings.BASE_DIR) / "scripts" / "setup_env.py"
TEMPLATE = Path(settings.BASE_DIR) / ".env.template"
PLACEHOLDER_HOSTNAME = "replace-with-your-custom-hostname"


def _template_keys():
    keys = []
    for line in TEMPLATE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


def _read_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


class SetupEnvInputTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.env_path = self.dir / ".env"

    def _use_repository_template(self):
        (self.dir / ".env.template").write_text(TEMPLATE.read_text())

    def _write_existing_env(self, omit=(), overrides=None):
        """Writes a .env as an earlier `make env` would have, with every key set."""
        values = {
            key: f"kept-{key.lower()}" for key in _template_keys() if key not in omit
        }
        values.update(overrides or {})
        self.env_path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        os.chmod(self.env_path, 0o600)
        return values

    def _run(self, piped=None):
        """Runs the script with `piped` on a pipe as input, or with </dev/null."""
        return subprocess.run(  # nosec B603
            [sys.executable, str(SCRIPT)],
            cwd=self.dir,
            input=piped,
            stdin=subprocess.DEVNULL if piped is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )

    def _run_on_a_terminal(self, typed):
        """Runs the script with a pseudo-terminal as standard input."""
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(  # nosec B603
                [sys.executable, str(SCRIPT)],
                cwd=self.dir,
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            os.close(slave)
            slave = None
            os.write(master, typed)
            output, _ = proc.communicate(timeout=120)
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
        return proc.returncode, output

    def test_upgrade_keeps_values_and_takes_the_default_of_a_new_key(self):
        self._use_repository_template()
        before = self._write_existing_env(omit=("SWARMMEDHUB_OVERLAY",))

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Traceback", result.stdout)
        self.assertIn(
            "Not asked for SWARMMEDHUB_OVERLAY (the input ended): used the template "
            "default, auto",
            result.stdout,
        )
        after = _read_env(self.env_path)
        self.assertEqual(after.pop("SWARMMEDHUB_OVERLAY"), "auto")
        self.assertEqual(after, before)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_closed_standard_input_counts_as_ended(self):
        # `<&-`: Python starts without sys.stdin, and input() raises RuntimeError.
        self._use_repository_template()
        before = self._write_existing_env(omit=("SWARMMEDHUB_OVERLAY",))

        result = subprocess.run(  # nosec B603
            ["/bin/sh", "-c", 'exec "$0" "$1" <&-', sys.executable, str(SCRIPT)],
            cwd=self.dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Traceback", result.stdout)
        self.assertIn(
            "Not asked for SWARMMEDHUB_OVERLAY (the input ended)", result.stdout
        )
        after = _read_env(self.env_path)
        self.assertEqual(after.pop("SWARMMEDHUB_OVERLAY"), "auto")
        self.assertEqual(after, before)

    def test_a_placeholder_stops_the_script_with_a_message_and_keeps_env(self):
        self._use_repository_template()
        self._write_existing_env(
            omit=("SWARMMEDHUB_OVERLAY",),
            overrides={"SWARMMEDHUB_HOSTNAME": PLACEHOLDER_HOSTNAME},
        )
        original = self.env_path.read_bytes()

        result = self._run()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("Traceback", result.stdout)
        self.assertIn(
            "could not be asked for (the input ended): SWARMMEDHUB_HOSTNAME.",
            result.stdout,
        )
        self.assertIn("Run `make env` in a terminal", result.stdout)
        self.assertIn(".env was not changed.", result.stdout)
        self.assertEqual(self.env_path.read_bytes(), original)

    def test_a_fresh_install_without_terminal_writes_no_env(self):
        (self.dir / ".env.template").write_text(
            "SECRET_KEY=replace-with-a-secure-random-secret-key\n"
            "DEBUG=False\n"
            f"SWARMMEDHUB_HOSTNAME={PLACEHOLDER_HOSTNAME}\n"
            "SWARMMEDHUB_OVERLAY=auto\n"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("Traceback", result.stdout)
        self.assertIn("SWARMMEDHUB_HOSTNAME", result.stdout)
        self.assertIn(".env was not written.", result.stdout)
        self.assertFalse(self.env_path.exists())

    def test_end_of_input_on_a_terminal_takes_the_default(self):
        self._use_repository_template()
        before = self._write_existing_env(omit=("SWARMMEDHUB_OVERLAY",))

        # Ctrl-D at the prompt for SWARMMEDHUB_OVERLAY.
        returncode, output = self._run_on_a_terminal(b"\x04")

        self.assertEqual(returncode, 0, output)
        self.assertNotIn("Traceback", output)
        self.assertIn("Enter SWARMMEDHUB_OVERLAY [auto]", output)
        self.assertIn("Not asked for SWARMMEDHUB_OVERLAY (the input ended)", output)
        after = _read_env(self.env_path)
        self.assertEqual(after.pop("SWARMMEDHUB_OVERLAY"), "auto")
        self.assertEqual(after, before)

    def test_a_terminal_is_still_asked(self):
        self._use_repository_template()
        self._write_existing_env(omit=("SWARMMEDHUB_OVERLAY",))

        returncode, output = self._run_on_a_terminal(b"netbird\n")

        self.assertEqual(returncode, 0, output)
        self.assertEqual(_read_env(self.env_path)["SWARMMEDHUB_OVERLAY"], "netbird")

    def test_piped_answers_are_used(self):
        self._use_repository_template()
        before = self._write_existing_env(
            omit=("SWARMMEDHUB_OVERLAY",),
            overrides={"SWARMMEDHUB_HOSTNAME": PLACEHOLDER_HOSTNAME},
        )

        result = self._run(piped="site-a\nnetbird\n")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Not asked for", result.stdout)
        after = _read_env(self.env_path)
        self.assertEqual(after.pop("SWARMMEDHUB_OVERLAY"), "netbird")
        self.assertEqual(after.pop("SWARMMEDHUB_HOSTNAME"), "site-a")
        before.pop("SWARMMEDHUB_HOSTNAME")
        self.assertEqual(after, before)

    def test_piped_empty_answers_behave_as_before(self):
        # As `yes '' | make env`: an empty answer takes the default, and for a
        # placeholder stores the placeholder, which a later run asks for again.
        self._use_repository_template()
        self._write_existing_env(
            omit=("SWARMMEDHUB_OVERLAY",),
            overrides={"SWARMMEDHUB_HOSTNAME": PLACEHOLDER_HOSTNAME},
        )

        result = self._run(piped="\n" * 5)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Not asked for", result.stdout)
        after = _read_env(self.env_path)
        self.assertEqual(after["SWARMMEDHUB_HOSTNAME"], PLACEHOLDER_HOSTNAME)
        self.assertEqual(after["SWARMMEDHUB_OVERLAY"], "auto")

    def test_a_pipe_that_ends_early_applies_the_rules_to_the_remaining_keys(self):
        self._use_repository_template()
        self._write_existing_env(
            omit=("SWARMMEDHUB_OVERLAY",),
            overrides={"SWARMMEDHUB_HOSTNAME": PLACEHOLDER_HOSTNAME},
        )

        # One answer, for SWARMMEDHUB_HOSTNAME; the pipe ends at the next key.
        result = self._run(piped="site-a\n")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Traceback", result.stdout)
        self.assertIn(
            "Not asked for SWARMMEDHUB_OVERLAY (the input ended): used the template "
            "default, auto",
            result.stdout,
        )
        after = _read_env(self.env_path)
        self.assertEqual(after["SWARMMEDHUB_HOSTNAME"], "site-a")
        self.assertEqual(after["SWARMMEDHUB_OVERLAY"], "auto")
