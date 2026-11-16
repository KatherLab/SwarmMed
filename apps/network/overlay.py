"""This host's overlay network address, as the overlay helper reports it.

The Hub never runs ``netbird`` or ``tailscale`` itself. A helper on the host,
``scripts/overlay_watcher.py`` (started by ``make overlay``), writes
``tmp/overlay_status.json`` every 30 seconds, and the ``app`` and
``celery_worker`` containers read it through the ``./tmp:/app/tmp`` bind mount.
The file written by the old helper, ``tmp/tailscale_status.json``, is still read,
so a host whose old helper is still running keeps working, until that file is
stale and the new helper has written a more recent status.

``SWARMMEDHUB_OVERLAY`` selects whose address the Hub uses:

* ``auto`` (default): NetBird if it is connected, otherwise Tailscale if it is
  connected, otherwise the first one installed. The Hub applies this to every
  overlay the helper lists, whichever one the helper put at the top level
  (``scripts/tailscale_watcher.sh`` runs it with ``--provider tailscale``);
* ``netbird`` or ``tailscale``: only that overlay;
* ``none``: no overlay; the first address in ``SWARMMEDHUB_LOCAL_ADDRESSES`` that
  can be a server address, if any.

The address is the FL server address of networks created without one. Together
with every other overlay address of the host and ``SWARMMEDHUB_LOCAL_ADDRESSES``,
it is also how the Hub recognises itself among a network's participants.

A status older than ``SWARMMEDHUB_OVERLAY_STALE_SECONDS`` (default 120), or dated
more than a minute in the future, is stale: it is shown as such, its address is
still used to recognise this host, and new networks then need an explicit server
address.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

NOT_AVAILABLE = "Not Available"
MODES = ("auto", "netbird", "tailscale", "none")
# The order in which mode auto prefers the overlays.
PROVIDERS = ("netbird", "tailscale")
PROVIDER_LABELS = {"netbird": "NetBird", "tailscale": "Tailscale", "none": "No overlay"}
# "This network" (RFC 1122): not an address another host can connect to.
THIS_NETWORK = ipaddress.IPv4Network("0.0.0.0/8")
STATUS_FILE_NAME = "overlay_status.json"
LEGACY_STATUS_FILE_NAME = "tailscale_status.json"
DEFAULT_STALE_SECONDS = 120.0
LEGACY_INTERVAL_SECONDS = 30.0
FUTURE_TOLERANCE_SECONDS = 60.0
WARNING_INTERVAL_SECONDS = 600.0

# Values already warned about, and when a repeated warning was last logged.
_warned_values: set[tuple[str, str]] = set()
_last_warning_at: dict[str, float] = {}


def _warn_once(kind: str, value: str, message: str, *args) -> None:
    if (kind, value) in _warned_values:
        return
    _warned_values.add((kind, value))
    logger.warning(message, *args)


def _warn_at_most_every(kind: str, message: str, *args) -> None:
    now = time.monotonic()
    last = _last_warning_at.get(kind)
    if last is not None and now - last < WARNING_INTERVAL_SECONDS:
        return
    _last_warning_at[kind] = now
    logger.warning(message, *args)


def reset_warnings() -> None:
    """Forget which warnings were logged (for tests)."""
    _warned_values.clear()
    _last_warning_at.clear()


def overlay_mode() -> str:
    """Return ``SWARMMEDHUB_OVERLAY``: auto, netbird, tailscale or none.

    An empty value means auto; any other value counts as auto, with one warning.
    """
    raw = os.environ.get("SWARMMEDHUB_OVERLAY", "")
    mode = raw.strip().lower()
    if not mode:
        return "auto"
    if mode in MODES:
        return mode
    _warn_once(
        "mode",
        raw,
        "SWARMMEDHUB_OVERLAY=%r is not one of auto, netbird, tailscale, none; "
        "using auto.",
        raw,
    )
    return "auto"


def stale_after_seconds() -> float:
    """Return ``SWARMMEDHUB_OVERLAY_STALE_SECONDS`` (default 120)."""
    raw = os.environ.get("SWARMMEDHUB_OVERLAY_STALE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_STALE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isfinite(value) and value > 0:
        return value
    _warn_once(
        "stale",
        raw,
        "SWARMMEDHUB_OVERLAY_STALE_SECONDS=%r is not a positive number; using %s.",
        raw,
        int(DEFAULT_STALE_SECONDS),
    )
    return DEFAULT_STALE_SECONDS


def status_file_path() -> Path:
    """Path of the helper's status file, computed at call time."""
    return Path(settings.BASE_DIR) / "tmp" / STATUS_FILE_NAME


def legacy_status_file_path() -> Path:
    """Path of the file written by the old ``scripts/tailscale_watcher.sh``."""
    return Path(settings.BASE_DIR) / "tmp" / LEGACY_STATUS_FILE_NAME


def parse_ipv4(value) -> str | None:
    """Return ``value`` as a dotted IPv4 address, or None.

    Prefix lengths (``100.64.0.10/16``), host names and IPv6 addresses are
    rejected: the FL server address becomes a Docker ``--add-host`` entry and
    must be a plain IPv4 address.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        return None


def is_usable_server_address(value) -> bool:
    """True if ``value`` is an IPv4 address that remote sites could connect to.

    Rejects host names, IPv6, prefix lengths, 0.0.0.0/8, loopback, link-local,
    multicast and reserved addresses (including 255.255.255.255).
    """
    text = parse_ipv4(value)
    if text is None:
        return False
    address = ipaddress.IPv4Address(text)
    return not (
        address in THIS_NETWORK
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    )


def configured_local_addresses() -> tuple[str, ...]:
    """IPv4 addresses listed in ``SWARMMEDHUB_LOCAL_ADDRESSES``, in order."""
    raw = os.environ.get("SWARMMEDHUB_LOCAL_ADDRESSES", "")
    addresses: list[str] = []
    for item in raw.split(","):
        if not item.strip():
            continue
        address = parse_ipv4(item)
        if address is None:
            _warn_once(
                "local-address",
                item.strip(),
                "Ignoring %r in SWARMMEDHUB_LOCAL_ADDRESSES: not an IPv4 address.",
                item.strip(),
            )
            continue
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _as_bool(value) -> bool | None:
    """True, False or None (unknown); accepts the strings the old helper wrote."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    return None


def _as_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _peer_counts(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    connected = _as_count(value.get("connected"))
    total = _as_count(value.get("total"))
    if connected is None or total is None:
        return None
    return {"connected": connected, "total": total}


def _text(value, limit: int = 300) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text[:limit] or None


def _parse_timestamp(value) -> float | None:
    """Seconds since the epoch for an ISO 8601 time, or None.

    None also for a time that cannot be represented as a UTC datetime, such as
    0001-01-01T00:00:00+14:00; the file's modification time is used instead.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        seconds = parsed.timestamp()
        datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return seconds


def _positive_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


@dataclass(frozen=True)
class _Source:
    """One status record read from a file."""

    origin: str  # "status-file" or "legacy-file"
    provider: str
    ip: str | None
    connected: bool | None
    state: str | None
    peers: dict | None
    installed: bool | None
    error: str | None
    updated: float  # seconds since the epoch
    interval: float | None


def _read_json(path: Path) -> tuple[object, float | None, str | None, bool]:
    """Return (payload, mtime, error, exists) for a status file."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None, None, None, False
    except OSError as exc:
        return None, None, f"{path.name} cannot be read: {exc}", True
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), mtime, None, True
    except (OSError, ValueError) as exc:
        return None, mtime, f"{path.name} is not valid JSON: {exc}", True


def _source_from_entry(
    entry: dict, provider: str, updated: float, interval: float | None
) -> _Source:
    raw_ip = entry.get("ip")
    ip = parse_ipv4(raw_ip)
    if ip is None and raw_ip not in (None, ""):
        _warn_once(
            "ip",
            str(raw_ip),
            "Ignoring overlay address %r from %s: not an IPv4 address.",
            raw_ip,
            STATUS_FILE_NAME,
        )
    installed = entry.get("installed")
    return _Source(
        origin="status-file",
        provider=provider,
        ip=ip,
        connected=_as_bool(entry.get("connected")),
        state=_text(entry.get("state"), 60),
        peers=_peer_counts(entry.get("peers")),
        installed=installed if isinstance(installed, bool) else None,
        error=_text(entry.get("error")),
        updated=updated,
        interval=interval,
    )


def _auto_choice(providers: dict) -> str:
    """Mode auto's overlay among the helper's ``providers``, or "none".

    NetBird if it is connected, otherwise Tailscale if it is connected,
    otherwise the first one installed, as the helper's own auto mode does.
    """
    entries = [
        (name, providers[name])
        for name in PROVIDERS
        if isinstance(providers.get(name), dict)
    ]
    for name, entry in entries:
        if _as_bool(entry.get("connected")) is True:
            return name
    for name, entry in entries:
        if entry.get("installed") is True:
            return name
    return "none"


def _load_status_file(mode: str) -> tuple[_Source | None, set[str], str | None]:
    """Read ``tmp/overlay_status.json``.

    Returns the record for ``mode`` (None if the file has none), every overlay
    address the file names, and an error text.
    """
    payload, mtime, error, exists = _read_json(status_file_path())
    if not exists or error or mtime is None:
        return None, set(), error
    if not isinstance(payload, dict):
        return None, set(), f"{STATUS_FILE_NAME} does not hold a JSON object"

    updated = _parse_timestamp(payload.get("updated_at"))
    if updated is None:
        updated = mtime
    interval = _positive_number(payload.get("interval_seconds"))

    providers = payload.get("providers")
    if not isinstance(providers, dict):
        providers = None

    addresses: set[str] = set()
    top_ip = parse_ipv4(payload.get("ip"))
    if top_ip:
        addresses.add(top_ip)
    for entry in (providers or {}).values():
        if isinstance(entry, dict):
            address = parse_ipv4(entry.get("ip"))
            if address:
                addresses.add(address)

    top_provider = str(payload.get("provider") or "none").strip().lower()
    if top_provider not in PROVIDER_LABELS:
        top_provider = "none"

    if mode == "auto":
        if providers is None:
            # A file without "providers": its own choice is all there is.
            entry, provider = payload, top_provider
        else:
            # The Hub's own preference, whatever the helper's --provider was.
            provider = _auto_choice(providers)
            entry = providers[provider] if provider != "none" else {}
        return _source_from_entry(entry, provider, updated, interval), addresses, None
    if providers is not None:
        entry = providers.get(mode)
        if not isinstance(entry, dict):
            return None, addresses, None
        return _source_from_entry(entry, mode, updated, interval), addresses, None
    # A file without "providers" counts only if its own choice is the mode.
    if top_provider == mode:
        return _source_from_entry(payload, mode, updated, interval), addresses, None
    return None, addresses, None


def _load_legacy_file() -> tuple[_Source | None, set[str], str | None]:
    """Read ``tmp/tailscale_status.json``, written by the old shell helper.

    Its keys are ``ipv4``, ``backend_state`` and ``connected`` (the strings
    "true"/"false"); the file has no timestamp, but the old helper replaced it
    every 30 s, so its modification time is the heartbeat.
    """
    payload, mtime, error, exists = _read_json(legacy_status_file_path())
    if not exists or error or mtime is None:
        return None, set(), error
    if not isinstance(payload, dict):
        return None, set(), f"{LEGACY_STATUS_FILE_NAME} does not hold a JSON object"
    ip = parse_ipv4(payload.get("ipv4"))
    source = _Source(
        origin="legacy-file",
        provider="tailscale",
        ip=ip,
        connected=_as_bool(payload.get("connected")),
        state=_text(payload.get("backend_state"), 60),
        peers=None,
        installed=None,
        error=None,
        updated=mtime,
        interval=LEGACY_INTERVAL_SECONDS,
    )
    return source, ({ip} if ip else set()), None


# The status file states how often its helper writes, and a helper that writes rarely
# gets more time before its status counts as stale; but the value comes from the file
# itself, so it is capped and a bad value cannot keep an old address fresh.
MAX_STATUS_INTERVAL_SECONDS = 600.0


def _stale_threshold(source: _Source, base: float) -> float:
    if source.interval is None:
        return base
    return max(base, 3 * min(source.interval, MAX_STATUS_INTERVAL_SECONDS))


def _duration_text(seconds: float) -> str:
    """A duration in words, for example "7 min"."""
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 2 * 3600:
        return f"{seconds // 60} min"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def _utc(seconds: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class OverlayStatus:
    """What the Hub knows about this host's overlay network."""

    mode: str
    provider: str = "none"
    ip: str | None = None
    connected: bool | None = None
    state: str | None = None
    peers: dict | None = None
    installed: bool | None = None
    updated_at: datetime | None = None
    age_seconds: float | None = None
    stale: bool = False
    source: str = "none"  # "status-file", "legacy-file", "env" or "none"
    error: str | None = None
    addresses: frozenset[str] = frozenset()
    # How far in the future the status is dated, when that makes it stale (the
    # clock was set back after the helper wrote it); its age is then unknown.
    ahead_seconds: float | None = None

    @property
    def provider_label(self) -> str:
        """The provider's name: NetBird, Tailscale or No overlay."""
        return PROVIDER_LABELS.get(self.provider, self.provider)

    @property
    def connection(self) -> str:
        """Return "connected", "disconnected", "stale" or "Not Available"."""
        if self.source in ("none", "env"):
            return NOT_AVAILABLE
        if self.stale:
            return "stale"
        if self.provider == "none" or self.installed is False:
            return NOT_AVAILABLE
        return "connected" if self.connected is True else "disconnected"

    @property
    def age_text(self) -> str:
        """The age of the status in words, for example "7 min"."""
        if self.age_seconds is None:
            return ""
        return _duration_text(self.age_seconds)

    @property
    def summary(self) -> str:
        """One line for the Network page."""
        if self.source == "none":
            if self.mode == "none":
                return "No overlay (SWARMMEDHUB_OVERLAY=none)"
            if self.error:
                return "The overlay helper's status cannot be read"
            return "No status from the overlay helper"
        if self.source == "env":
            return (
                "No overlay (SWARMMEDHUB_OVERLAY=none); address from "
                "SWARMMEDHUB_LOCAL_ADDRESSES"
            )
        if self.stale and self.ahead_seconds is not None:
            return (
                "Stale: the overlay helper's last status is dated "
                f"{_duration_text(self.ahead_seconds)} in the future"
            )
        if self.stale:
            return f"Stale: no update from the overlay helper for {self.age_text}"
        if self.provider == "none" or self.installed is False:
            return "No NetBird or Tailscale client found on this host"
        if self.connected is True:
            text = "Connected"
        elif self.connected is False:
            text = "Not connected"
        else:
            text = "Connection state unknown"
        if self.state and self.state != "Connected":
            text = f"{text} ({self.state})"
        return text

    @property
    def peers_text(self) -> str:
        """Peer counts in the overlay's own terms, or an empty string.

        Empty unless the overlay is connected: a client that is logged out or
        reconnecting lists no peers (NetBird 0.82.1 reports 0 of 0).
        """
        if not self.peers or self.stale or self.connected is not True:
            return ""
        verb = "online" if self.provider == "tailscale" else "connected"
        return f"{self.peers['connected']} of {self.peers['total']} peers {verb}"

    @property
    def current_address(self) -> str | None:
        """The address a new network may use as its FL server address.

        None when the status is stale, absent or reports no connection: kits
        carry this address for the whole study, so it must be current.
        """
        if not self.ip or self.stale:
            return None
        if self.source == "env":
            # The first usable address of SWARMMEDHUB_LOCAL_ADDRESSES, if any.
            return self.ip if is_usable_server_address(self.ip) else None
        if self.source == "none" or self.connected is False:
            return None
        return self.ip

    def unavailable_reason(self) -> str:
        """Why ``current_address`` is None, in words."""
        label = self.provider_label
        if self.source == "none":
            if self.mode == "none":
                return (
                    "SWARMMEDHUB_OVERLAY is none and SWARMMEDHUB_LOCAL_ADDRESSES "
                    "names no address"
                )
            if self.error:
                return f"the overlay helper's status cannot be read ({self.error})"
            return (
                "the overlay helper has not reported this host's address "
                "(start it on the host with `make overlay`)"
            )
        if self.stale and self.ahead_seconds is not None:
            return (
                "the overlay helper's last status is dated "
                f"{_duration_text(self.ahead_seconds)} in the future, so its age "
                "is unknown (start the helper on the host with `make overlay`)"
            )
        if self.stale:
            return (
                f"the overlay helper has not reported for {self.age_text} "
                "(start it on the host with `make overlay`)"
            )
        if self.source == "env":
            return (
                "SWARMMEDHUB_OVERLAY is none and SWARMMEDHUB_LOCAL_ADDRESSES names "
                "no address that other sites can connect to (loopback, link-local, "
                "multicast, reserved and 0.0.0.0/8 addresses cannot be used)"
            )
        if self.provider == "none" or self.installed is False:
            return "no NetBird or Tailscale client was found on this host"
        if self.connected is False:
            state = f" ({self.state})" if self.state else ""
            return f"{label} is not connected{state}"
        if not self.ip:
            return f"{label} reports no IPv4 address"
        return f"{self.ip} cannot be used as a server address"


def get_overlay_status() -> OverlayStatus:
    """Read this host's overlay status (no caching; the files are small)."""
    mode = overlay_mode()
    env_addresses = configured_local_addresses()

    if mode == "none":
        # The first address that can be a server address, else the first one.
        usable = [a for a in env_addresses if is_usable_server_address(a)]
        ip = next(iter(usable or env_addresses), None)
        return OverlayStatus(
            mode=mode,
            provider="none",
            ip=ip,
            source="env" if ip else "none",
            addresses=frozenset(env_addresses),
        )

    new_source, new_addresses, new_error = _load_status_file(mode)
    legacy_source, legacy_addresses, legacy_error = _load_legacy_file()
    now = time.time()
    base = stale_after_seconds()

    def ahead_of(source: _Source) -> float | None:
        # A time up to a minute ahead counts as now. More than that cannot come
        # from a running helper; its age is unknown, so the status is stale.
        ahead = source.updated - now
        return ahead if ahead > FUTURE_TOLERANCE_SECONDS else None

    def is_stale(source: _Source) -> bool:
        if ahead_of(source) is not None:
            return True
        return now - source.updated > _stale_threshold(source, base)

    if (
        legacy_source is not None
        and new_source is not None
        and is_stale(legacy_source)
        and new_source.updated > legacy_source.updated
    ):
        # The old helper stopped and the new one has written since: the old
        # file no longer counts, not even to recognise this host, because its
        # address may since have been given to another peer.
        legacy_source, legacy_addresses = None, set()

    if legacy_source is not None and mode not in ("auto", "tailscale"):
        legacy_source = None
    addresses = frozenset(new_addresses | legacy_addresses | set(env_addresses))

    # The helper's file wins while it is fresh; the old helper's file is used
    # when it is the only fresh one; otherwise the more recent of the two.
    candidates = [s for s in (new_source, legacy_source) if s is not None]
    chosen = None
    if new_source is not None and not is_stale(new_source):
        chosen = new_source
    elif legacy_source is not None and not is_stale(legacy_source):
        chosen = legacy_source
    elif candidates:
        chosen = max(candidates, key=lambda s: s.updated)

    if chosen is None:
        return OverlayStatus(
            mode=mode,
            provider=mode if mode in ("netbird", "tailscale") else "none",
            source="none",
            error=new_error or legacy_error,
            addresses=addresses,
        )

    ahead = ahead_of(chosen)
    age = None if ahead is not None else max(0.0, now - chosen.updated)
    stale = is_stale(chosen)
    if ahead is not None:
        _warn_at_most_every(
            "future",
            "The overlay status is dated %.0f s in the future (was the clock set "
            "back?); treating it as stale. Start the helper with `make overlay`.",
            ahead,
        )
    elif stale and chosen.ip:
        _warn_at_most_every(
            "stale",
            "No update from the overlay helper for %.0f s; still using the last "
            "known address %s to recognise this host. Start the helper with "
            "`make overlay`.",
            age,
            chosen.ip,
        )
    return OverlayStatus(
        mode=mode,
        provider=chosen.provider,
        ip=chosen.ip,
        connected=chosen.connected,
        state=chosen.state,
        peers=chosen.peers,
        installed=chosen.installed,
        updated_at=_utc(
            chosen.updated if ahead is not None else min(chosen.updated, now)
        ),
        age_seconds=age,
        stale=stale,
        source=chosen.origin,
        error=chosen.error,
        addresses=addresses,
        ahead_seconds=ahead,
    )


def get_overlay_ip() -> str:
    """This host's overlay IPv4 address, or "Not Available".

    A stale status still yields its last known address.
    """
    return get_overlay_status().ip or NOT_AVAILABLE


def is_overlay_connected() -> str:
    """Return "connected", "disconnected", "stale" or "Not Available"."""
    return get_overlay_status().connection


def get_local_addresses() -> frozenset[str]:
    """Every address that identifies this host as a network participant.

    The overlay addresses the helper reports (NetBird and Tailscale, stale or
    not), the old helper's Tailscale address, and ``SWARMMEDHUB_LOCAL_ADDRESSES``.
    With ``SWARMMEDHUB_OVERLAY=none`` only the last.
    """
    return get_overlay_status().addresses


def resolve_server_address(explicit: str | None = None) -> str:
    """Return the FL server address for a new network, or raise ValueError.

    An explicit address must be usable (see ``is_usable_server_address``).
    Without one, this host's current overlay address is used; a stale, absent
    or disconnected overlay status is refused.
    """
    text = "" if explicit is None else str(explicit).strip()
    if text:
        if not is_usable_server_address(text):
            raise ValueError(
                f"'{text}' cannot be used as the FL server address. Give the IPv4 "
                "address at which every site reaches this host, for example "
                "100.64.0.10; host names, IPv6, prefix lengths, loopback, "
                "link-local and multicast addresses are not accepted."
            )
        return str(ipaddress.IPv4Address(text))
    status = get_overlay_status()
    address = status.current_address
    if address is None or not is_usable_server_address(address):
        raise ValueError(
            "No FL server address was given, and this host's overlay address "
            f"cannot be used: {status.unavailable_reason()}. Give the address "
            "explicitly: --server-ip in the CLI, or the FL server address field "
            "in the web form."
        )
    return address
