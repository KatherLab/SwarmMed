#!/usr/bin/env python3
"""Record this host's overlay network address for SwarmMedHub.

Runs on the host, not in a container, and needs only the standard library of
Python 3.8 or later (the host's ``python3``). Every 30 seconds it asks NetBird (``netbird status --json``) and
Tailscale (``tailscale status --json``) for their state and IPv4 address and
writes ``tmp/overlay_status.json`` in the repository, which the Hub's
containers read through the ``./tmp:/app/tmp`` bind mount. Neither client's
socket is mounted into a container.

The file lists every overlay found under "providers" and puts one of them at
the top level, chosen by ``--provider`` (default: ``$SWARMMEDHUB_OVERLAY``,
else ``auto``):

  auto       NetBird if it is connected, else Tailscale if it is connected,
             else the first one installed (NetBird before Tailscale), else none
  netbird    NetBird
  tailscale  Tailscale
  none       no overlay (nothing is queried)

Usage:
  python3 scripts/overlay_watcher.py [--provider MODE] [--interval 30]
                                     [--once | --status | --stop] [--output PATH]

Without ``--once`` it runs until stopped and keeps one instance per output
directory: it holds the PID file ``overlay_watcher.pid`` next to the output
locked (flock) while it runs, and the lock ends with the process, however it
ends. It prints only changes of state and errors, which ``make overlay`` appends
to ``tmp/overlay_watcher.log``.

``--status`` prints the PID of the running helper and exits 0, or exits 1 if
none runs; ``--stop`` stops it (SIGTERM), waits until it has ended, prints its
PID and exits 0, or exits 1 if none runs. Both exit 3 on an error. They decide
by the lock, not by the PID alone, so a PID file left behind by a killed helper,
whose PID another program may have since, is never mistaken for a running one.

``netbird status --ipv4`` is not used: when NetBird needs a login it prints the
login instructions instead of an address, and exits 0.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
import shutil
import signal
import subprocess  # nosec B404 - runs the overlay clients' CLIs, without a shell
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "tmp" / "overlay_status.json"
PID_FILE_NAME = "overlay_watcher.pid"
DEFAULT_INTERVAL = 30
MIN_INTERVAL = 5
# netbird retries the daemon socket for about 10 s before it gives up.
COMMAND_TIMEOUT = 20
MODES = ("auto", "netbird", "tailscale", "none")
PROVIDERS = ("netbird", "tailscale")
NETBIRD_DEFAULT_INTERFACE = "wt0"
MACOS_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
ERROR_LIMIT = 300


def parse_ipv4(value) -> str | None:
    """Return the IPv4 address in ``value`` ("100.64.0.10" or "100.64.0.10/16")."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        interface = ipaddress.ip_interface(value.strip())
    except ValueError:
        return None
    if interface.version != 4:
        return None
    return str(interface.ip)


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _short(text, limit: int = ERROR_LIMIT) -> str | None:
    """The first line starting with "Error", else the last line, shortened."""
    if not text:
        return None
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return None
    for line in lines:
        if line.lower().startswith("error"):
            return line[:limit]
    return lines[-1][:limit]


def empty_provider() -> dict:
    """The record of an overlay client that is not installed."""
    return {
        "installed": False,
        "ip": None,
        "connected": None,
        "state": None,
        "peers": None,
        "error": None,
    }


def run_command(argv: list[str], timeout: float = COMMAND_TIMEOUT):
    """Run a command; return (exit code or None, stdout, stderr)."""
    try:
        completed = subprocess.run(  # nosec B603 - fixed argument list, no shell
            argv,
            capture_output=True,
            text=True,
            # A byte that is not valid in the locale's encoding must not stop
            # the helper (error texts and ip's output can hold any bytes).
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"{Path(argv[0]).name} did not answer within {timeout:g} s"
    except OSError as exc:
        return None, "", f"{Path(argv[0]).name} could not be run: {exc}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def parse_netbird_status(text: str) -> dict | None:
    """Parse ``netbird status --json``; None if it is not a JSON object.

    Fields used (NetBird client, ``client/status/status.go``): ``daemonStatus``
    (Idle, Connecting, Connected, NeedsLogin, LoginFailed, SessionExpired),
    ``netbirdIp`` (with a prefix length, e.g. "100.64.0.10/16"; empty when not
    connected), ``management.connected`` and ``management.error``, and
    ``peers.connected`` / ``peers.total``. Other peers' names and addresses are
    not kept.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    state = data.get("daemonStatus")
    state = state.strip() if isinstance(state, str) and state.strip() else None
    management = data.get("management")
    management = management if isinstance(management, dict) else {}
    management_connected = management.get("connected")

    if state is not None:
        connected = state == "Connected"
    elif isinstance(management_connected, bool):
        # Clients that do not report daemonStatus.
        connected = management_connected
    else:
        connected = None

    peers = None
    peer_data = data.get("peers")
    if isinstance(peer_data, dict):
        peer_connected = _count(peer_data.get("connected"))
        peer_total = _count(peer_data.get("total"))
        if peer_connected is not None and peer_total is not None:
            peers = {"connected": peer_connected, "total": peer_total}

    error = None
    management_error = management.get("error")
    if isinstance(management_error, str) and management_error.strip():
        error = f"management: {management_error.strip()}"[:ERROR_LIMIT]
    elif state == "Connected" and management_connected is False:
        error = "management server not connected"

    return {
        "ip": parse_ipv4(data.get("netbirdIp")),
        "connected": connected,
        "state": state,
        "peers": peers,
        "error": error,
    }


def _ip_binary() -> str | None:
    found = shutil.which("ip")
    if found:
        return found
    for candidate in ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def interface_ipv4(name: str) -> str | None:
    """IPv4 address of a network interface (Linux, ``ip -4 -o addr show``)."""
    ip_binary = _ip_binary()
    if not ip_binary or not name:
        return None
    code, out, _ = run_command([ip_binary, "-4", "-o", "addr", "show", "dev", name], 5)
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            index = parts.index("inet")
            if index + 1 < len(parts):
                address = parse_ipv4(parts[index + 1])
                if address:
                    return address
    return None


def probe_netbird(env=None) -> dict:
    """NetBird's state and IPv4 address on this host."""
    env = os.environ if env is None else env
    binary = env.get("NETBIRD_BIN") or shutil.which("netbird")
    if not binary:
        return empty_provider()
    record = empty_provider()
    record["installed"] = True

    code, out, err = run_command([binary, "status", "--json"])
    parsed = parse_netbird_status(out) if code == 0 else None
    if parsed is None:
        record["connected"] = False
        if code == 0:
            record["state"] = "unreadable-status"
            record["error"] = "netbird status --json printed no JSON object"
        else:
            # Exit 1 after about 10 s when the daemon is not running.
            record["state"] = "daemon-unavailable"
            record["error"] = (
                _short(err) or _short(out) or "netbird status --json failed"
            )
    else:
        record.update(parsed)

    withheld = (
        parsed is not None and parsed["state"] == "Connected" and not parsed["ip"]
    )
    if parsed is None or withheld:
        # From NetBird 0.83 the daemon gives detailed status, including the
        # address, only to root and the user who connected; others get
        # daemonStatus only. The interface still carries the address.
        name = env.get("SWARMMEDHUB_NETBIRD_INTERFACE") or NETBIRD_DEFAULT_INTERFACE
        address = interface_ipv4(name)
        if withheld:
            record["peers"] = None
            note = "netbird status did not include the address"
            if address:
                record["ip"] = address
                record["error"] = f"{note}; read from interface {name}"
            else:
                record["error"] = (
                    f"{note} (run the helper as root or as the user who ran netbird up)"
                )
        elif address:
            record["ip"] = address
            record["connected"] = None
            record["state"] = "interface-only"
            note = f"{record['error']}; address read from interface {name}"
            record["error"] = note[:ERROR_LIMIT]
    return record


def parse_tailscale_status(text: str) -> dict | None:
    """Parse ``tailscale status --json``; None if it is not a JSON object.

    Fields used: ``BackendState`` (NoState, NeedsLogin, NeedsMachineAuth,
    Stopped, Starting, Running), the first IPv4 address in
    ``Self.TailscaleIPs`` (or the top-level ``TailscaleIPs``), and the number
    of entries in ``Peer`` and of those with ``Online`` true. Other peers'
    names and addresses are not kept.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    state = data.get("BackendState")
    state = state.strip() if isinstance(state, str) and state.strip() else None

    own = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    address = None
    for candidates in (own.get("TailscaleIPs"), data.get("TailscaleIPs")):
        if isinstance(candidates, list):
            for candidate in candidates:
                address = parse_ipv4(candidate)
                if address:
                    break
        if address:
            break

    peers = None
    peer_map = data.get("Peer")
    if peer_map is None or isinstance(peer_map, dict):
        values = list((peer_map or {}).values())
        peers = {
            "connected": sum(
                1
                for peer in values
                if isinstance(peer, dict) and peer.get("Online") is True
            ),
            "total": len(values),
        }

    return {
        "ip": address,
        "connected": (state == "Running") if state else None,
        "state": state,
        "peers": peers,
        "error": None,
    }


def _tailscale_binary(env) -> str | None:
    binary = env.get("TAILSCALE_BIN") or shutil.which("tailscale")
    if binary:
        return binary
    if os.access(MACOS_TAILSCALE, os.X_OK):
        return MACOS_TAILSCALE
    return None


def probe_tailscale(env=None) -> dict:
    """Tailscale's state and IPv4 address on this host."""
    env = os.environ if env is None else env
    binary = _tailscale_binary(env)
    if not binary:
        return empty_provider()
    record = empty_provider()
    record["installed"] = True

    code, out, err = run_command([binary, "status", "--json"])
    parsed = parse_tailscale_status(out) if code == 0 else None
    if parsed is None:
        record["connected"] = False
        if code == 0:
            record["state"] = "unreadable-status"
            record["error"] = "tailscale status --json printed no JSON object"
        else:
            record["state"] = "daemon-unavailable"
            record["error"] = (
                _short(err) or _short(out) or "tailscale status --json failed"
            )
    else:
        record.update(parsed)

    if not record["ip"]:
        code, out, _ = run_command([binary, "ip", "-4"])
        if code == 0:
            lines = out.strip().splitlines()
            # Plain addresses only; "tailscale ip -4" prints no prefix length.
            first = lines[0].strip() if lines else ""
            if "/" not in first and parse_ipv4(first):
                record["ip"] = parse_ipv4(first)
    return record


def choose_provider(mode: str, providers: dict) -> str:
    """The overlay to report at the top level for ``mode``."""
    if mode in PROVIDERS:
        return mode
    if mode == "none":
        return "none"
    for name in PROVIDERS:
        if providers.get(name, {}).get("connected") is True:
            return name
    for name in PROVIDERS:
        if providers.get(name, {}).get("installed"):
            return name
    return "none"


def utc_timestamp(now: float | None = None) -> str:
    # timezone.utc rather than datetime.UTC: the host's python3 may be older than 3.11.
    utc = timezone.utc  # noqa: UP017
    moment = datetime.fromtimestamp(time.time() if now is None else now, tz=utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_status(mode: str, providers: dict, interval: int, now: float | None = None):
    """The JSON document written to the status file."""
    chosen = choose_provider(mode, providers)
    top = providers.get(chosen) if chosen in PROVIDERS else None
    top = top or {**empty_provider(), "installed": None}
    return {
        "schema": SCHEMA_VERSION,
        "provider": chosen,
        "ip": top["ip"],
        "connected": top["connected"],
        "state": top["state"],
        "peers": top["peers"],
        "installed": top["installed"],
        "error": top["error"],
        "updated_at": utc_timestamp(now),
        "interval_seconds": interval,
        "watcher_mode": mode,
        "providers": providers,
    }


def collect(mode: str, interval: int, env=None) -> dict:
    """Query the overlays (none for mode "none") and build the status."""
    if mode == "none":
        providers = {}
    else:
        providers = {"netbird": probe_netbird(env), "tailscale": probe_tailscale(env)}
    return build_status(mode, providers, interval)


def write_status(path: Path, status: dict) -> None:
    """Write the status atomically (temporary file and rename), mode 0644."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = handle.name
        try:
            json.dump(status, handle, indent=2)
            handle.write("\n")
        except BaseException:
            handle.close()
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    try:
        # The containers read the file as another user.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# Descriptors of the PID files this process holds locked, by path.
_held_pid_files: dict = {}


def _lock(fd: int, operation: int, wait: float = 0.0) -> bool:
    """``flock`` without blocking, retried for ``wait`` seconds; False if held."""
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def acquire_pid_file(pid_path: Path, wait: float = 1.0) -> bool:
    """Lock the PID file and write this PID to it; False if a watcher holds it.

    The lock is retried for ``wait`` seconds, because ``--status`` and
    ``--stop`` hold it for a moment while they check.
    """
    pid_path = Path(pid_path)
    if str(pid_path) in _held_pid_files:
        return True
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(pid_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        locked = _lock(fd, fcntl.LOCK_EX, wait)
        if locked:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
    except BaseException:
        os.close(fd)
        raise
    if not locked:
        os.close(fd)
        return False
    _held_pid_files[str(pid_path)] = fd
    return True


def release_pid_file(pid_path: Path) -> None:
    """Empty the PID file and release its lock, if this process holds it.

    The file is not removed: a watcher that is starting may have it open
    already, and would then lock a file that no longer has a name.
    """
    fd = _held_pid_files.pop(str(Path(pid_path)), None)
    if fd is None:
        return
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
    os.close(fd)


def running_watcher(pid_path: Path) -> int | None:
    """The PID of the watcher that holds ``pid_path`` locked; None if none runs."""
    try:
        fd = os.open(pid_path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        # A watcher writes its PID right after it has taken the lock, and
        # empties the file right before it releases the lock.
        for _ in range(20):
            if _lock(fd, fcntl.LOCK_SH):
                return None  # nobody holds it; closing the file releases it
            try:
                pid = int(os.pread(fd, 32, 0).strip() or b"0")
            except ValueError:
                pid = 0
            if pid > 0:
                return pid
            time.sleep(0.05)
        raise OSError(f"{pid_path} is locked but names no process")
    finally:
        os.close(fd)


def stop_watcher(pid_path: Path, timeout: float = 10.0) -> int | None:
    """Stop the running watcher (SIGTERM) and wait until it has ended.

    Returns its PID, or None if none runs. Raises OSError if it cannot be
    signalled, or if it still runs after ``timeout`` seconds.
    """
    pid = running_watcher(pid_path)
    if pid is None:
        return None
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while running_watcher(pid_path) is not None:
        if time.monotonic() >= deadline:
            raise OSError(f"PID {pid} still runs {timeout:g} s after SIGTERM")
        time.sleep(0.1)
    return pid


def describe(status: dict) -> str:
    """One line summarising a status, for the log (no peer details)."""

    def part(record):
        if not record.get("installed"):
            return "not installed"
        text = f"{record.get('state') or 'unknown'}, ip={record.get('ip') or '-'}"
        if record.get("error"):
            text += f", {record['error']}"
        return text

    providers = status.get("providers") or {}
    details = "; ".join(f"{name}: {part(rec)}" for name, rec in providers.items())
    head = (
        f"provider={status['provider']} ip={status['ip'] or '-'} "
        f"connected={json.dumps(status['connected'])}"
    )
    return f"{head}" + (f" ({details})" if details else "")


def _state_key(status: dict):
    providers = status.get("providers") or {}
    return (
        status["provider"],
        status["ip"],
        status["connected"],
        tuple(
            (
                name,
                rec.get("installed"),
                rec.get("ip"),
                rec.get("connected"),
                rec.get("state"),
                rec.get("error"),
            )
            for name, rec in sorted(providers.items())
        ),
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Record this host's NetBird or Tailscale address for SwarmMedHub."
    )
    default_mode = os.environ.get("SWARMMEDHUB_OVERLAY", "").strip().lower() or "auto"
    if default_mode not in MODES:
        print(
            f"SWARMMEDHUB_OVERLAY={default_mode!r} is not one of {', '.join(MODES)}; "
            "using auto.",
            file=sys.stderr,
        )
        default_mode = "auto"
    parser.add_argument("--provider", choices=MODES, default=default_mode)
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"seconds between updates (default {DEFAULT_INTERVAL}, at least {MIN_INTERVAL})",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--once", action="store_true", help="write one status and exit")
    action.add_argument(
        "--status",
        action="store_true",
        help="print the PID of the running helper; exit 1 if none runs",
    )
    action.add_argument(
        "--stop",
        action="store_true",
        help="stop the running helper and print its PID; exit 1 if none runs",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="status file (default: tmp/overlay_status.json in the repository)",
    )
    args = parser.parse_args(argv)
    args.interval = max(MIN_INTERVAL, args.interval)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    output = args.output.resolve()
    pid_path = output.parent / PID_FILE_NAME

    if args.status or args.stop:
        try:
            pid = stop_watcher(pid_path) if args.stop else running_watcher(pid_path)
        except OSError as exc:
            action = "stop" if args.stop else "check"
            print(f"cannot {action} the overlay helper: {exc}", file=sys.stderr)
            return 3
        if pid is None:
            return 1
        print(pid, flush=True)
        return 0

    if args.once:
        status = collect(args.provider, args.interval)
        write_status(output, status)
        print(f"{status['updated_at']} {describe(status)}", flush=True)
        return 0

    try:
        acquired = acquire_pid_file(pid_path)
    except OSError as exc:
        print(f"{utc_timestamp()} cannot lock {pid_path}: {exc}; exiting", flush=True)
        return 3
    if not acquired:
        print(
            f"{utc_timestamp()} overlay helper already running (PID file {pid_path}); exiting",
            flush=True,
        )
        return 0

    def stop(signum, frame):
        raise SystemExit(0)

    # SIGHUP is left alone: under nohup it is ignored, as it should be.
    signal.signal(signal.SIGTERM, stop)
    print(
        f"{utc_timestamp()} overlay helper started: provider={args.provider}, "
        f"every {args.interval} s, writing {output}",
        flush=True,
    )
    last_key = None
    last_error = None
    try:
        while True:
            started = time.monotonic()
            error = None
            try:
                status = collect(args.provider, args.interval)
                try:
                    write_status(output, status)
                except OSError as exc:
                    error = f"cannot write {output}: {exc}"
                key = _state_key(status)
                if key != last_key:
                    print(f"{status['updated_at']} {describe(status)}", flush=True)
                    last_key = key
            except Exception as exc:
                # Whatever goes wrong, the helper keeps running and tries again.
                error = f"error: {type(exc).__name__}: {exc}"
            # Each error is logged once, until an update succeeds or it changes.
            if error is not None and error != last_error:
                print(f"{utc_timestamp()} {error}", flush=True)
            last_error = error
            # Keep the interval between starts; a probe can take ~10 s.
            time.sleep(max(1.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        release_pid_file(pid_path)
        print(f"{utc_timestamp()} overlay helper stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
