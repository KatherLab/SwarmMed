"""Fingerprints of startup packages, so that the sites can check they hold the same one.

The coordinating site hands every other site the startup package, outside SwarmMed.
To let the sites check that they all received the same package, the coordinating Hub
writes ``manifest.json`` into the package it exports and the identical file into
every site's kit in it. The manifest gives the SHA-256 of every other file in the
package and sums up the trust material the sites share: the fingerprints of the root
CA and FL server certificates, the FL server's address as the kits name it, its two
ports, and the participants.

The network fingerprint is the SHA-256 of the manifest in canonical JSON, which is
also the exact content of ``manifest.json``. An importing Hub checks every file
against the manifest before it writes anything, and shows the fingerprint; the sites
compare it with each other, by phone or video call, before they start.

Equal fingerprints show that the sites received the same package. They do not show
that the coordinating site is honest, since it holds the root CA's key and every
other key, nor that nobody else copied the package on the way: every site's kit
carries every site's key, the FL server's key and an administrator identity, so the
package must still travel over an encrypted channel.

Nothing here depends on Django.
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "swarmmed-package-manifest/1"

# Shown next to a network fingerprint.
COMPARE_FINGERPRINT_ADVICE = (
    "Compare it with every other site by phone or video call, not through the "
    "channel that carried the package."
)

# Archives in a package that an import keeps as files instead of unpacking them as
# site kits (see services.import_network).
NOT_SITE_KITS = frozenset({"startup-kits.zip", "startup_kits.zip"})

_HEX64 = re.compile(r"[0-9a-f]{64}")

# The most an import unpacks, by the sizes the package declares. Read a chunk at
# a time (_chunks), zipfile unpacks little more than an entry's declared size and
# refuses content that does not match its CRC. It decompresses bzip2 and LZMA
# data whole, whatever size an entry declares, so only the methods that SwarmMed
# writes are accepted (_ACCEPTED_METHODS). A package exported before its network
# first ran is far smaller.
MAX_ENTRY_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
_ACCEPTED_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

# The files whose content the checks read (derive_trust and the network's
# identifier); every other file is only hashed, a chunk at a time.
_READ_BASENAMES = frozenset(
    {
        ".network_id",
        ".participants.json",
        "client.crt",
        "fed_server.json",
        "rootCA.pem",
        "server.crt",
        "server_host.txt",
    }
)
_CHUNK_BYTES = 1024 * 1024

_TRUST_FIELDS = (
    "root_ca_sha256",
    "server_certificate_sha256",
    "server_address",
    "fed_learn_port",
    "admin_port",
)
_PARTICIPANT_FIELDS = ("name", "role", "org", "address", "certificate_sha256")


class PackageVerificationError(ValueError):
    """A startup package is unsafe or does not agree with its manifest.

    Attributes:
        path: The first offending path, such as ``node-A.zip/startup/rootCA.pem``.
        problem: What is wrong with it.
    """

    def __init__(self, path: str, problem: str):
        """Name the offending path and what is wrong with it."""
        self.path = path
        self.problem = problem
        super().__init__(f"{path} {problem}")


@dataclass(frozen=True)
class PackageCheck:
    """The outcome of :func:`check_package` for a package that passed.

    Attributes:
        manifest: The package's manifest; None for a package without one, as
            exported by SwarmMed 1.0.4 or earlier.
        fingerprint: The network fingerprint (64 hex characters); "" without a
            manifest.
        site: The site whose kit was checked on its own, such as ``node-A``; ""
            for a whole package.
    """

    manifest: dict | None
    fingerprint: str
    site: str = ""


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 of ``data`` as 64 lower-case hex characters."""
    return hashlib.sha256(data).hexdigest()


def certificate_fingerprint(pem: bytes) -> str:
    """Return the SHA-256 fingerprint of a PEM certificate's DER encoding.

    The format is OpenSSL's (upper-case hex pairs separated by colons), so that it
    can be compared with ``openssl x509 -noout -fingerprint -sha256 -in <file>``.

    Raises:
        ValueError: If ``pem`` is not a PEM certificate.
    """
    certificate = x509.load_pem_x509_certificate(pem)
    return ":".join(f"{byte:02X}" for byte in certificate.fingerprint(hashes.SHA256()))


def canonical_bytes(manifest: dict) -> bytes:
    """Return the canonical JSON of a manifest: sorted keys, no spaces, UTF-8."""
    return json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def network_fingerprint(manifest: dict) -> str:
    """Return the network fingerprint: the SHA-256 of the canonical manifest."""
    return sha256_hex(canonical_bytes(manifest))


def group_fingerprint(fingerprint: str) -> str:
    """Return a fingerprint as groups of four characters, for reading it aloud."""
    value = str(fingerprint or "")
    return " ".join(value[index : index + 4] for index in range(0, len(value), 4))


def normalize_fingerprint(value: str) -> str:
    """Return a typed network fingerprint as 64 lower-case hex characters.

    Spaces, colons and dashes are ignored, and so is the case.

    Raises:
        ValueError: If what is left is not 64 hex characters.
    """
    cleaned = re.sub(r"[\s:\-]", "", str(value or "")).lower()
    if not _HEX64.fullmatch(cleaned):
        raise ValueError(
            "A network fingerprint has 64 hexadecimal characters (spaces, colons "
            f"and dashes are ignored); {str(value or '').strip()!r} has "
            f"{len(cleaned)} characters after removing them, or other characters."
        )
    return cleaned


def unsafe_entry_problem(info: zipfile.ZipInfo) -> str:
    """Return why a ZIP entry may not be extracted, or "" if it may.

    Absolute paths, paths with a ``..`` component and symbolic links are refused,
    whether or not the package has a manifest. So are paths with an empty or
    ``.`` component, such as ``./node-A.zip``, which zipfile would extract under
    another name than the one checked; SwarmMed never writes such names.
    """
    name = info.filename.replace("\\", "/")
    if name.startswith("/") or re.match(r"[A-Za-z]:", name):
        return "is an absolute path"
    parts = name.split("/")
    if ".." in parts:
        return "contains a '..' component"
    # A trailing "/" (a folder's entry) leaves an empty last part, which is fine.
    if any(part in ("", ".") for part in parts[:-1]) or parts[-1] == ".":
        return "contains an empty or '.' component"
    if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
        return "is a symbolic link"
    return ""


def _check_sizes(infos: list[zipfile.ZipInfo], prefix: str, budget: int) -> int:
    """Refuse an entry over MAX_ENTRY_BYTES, or one that exhausts ``budget``.

    Only the sizes the ZIP declares are read, nothing of the content. An entry
    compressed with another method than stored or deflated is refused as well:
    zipfile would decompress it whole, beyond the size it declares.

    Returns:
        int: What is left of ``budget``.
    """
    for info in infos:
        if info.compress_type not in _ACCEPTED_METHODS:
            raise PackageVerificationError(
                prefix + info.filename,
                f"is compressed with method {info.compress_type}; an import accepts "
                "only stored and deflated entries, as SwarmMed writes them",
            )
        if info.file_size > MAX_ENTRY_BYTES:
            raise PackageVerificationError(
                prefix + info.filename,
                f"is {info.file_size} bytes unpacked, more than the "
                f"{MAX_ENTRY_BYTES} an import accepts for one file",
            )
        budget -= info.file_size
        if budget < 0:
            raise PackageVerificationError(
                prefix + info.filename,
                f"takes the package over the {MAX_PACKAGE_BYTES} bytes unpacked "
                "that an import accepts",
            )
    return budget


def _chunks(archive: zipfile.ZipFile, info: zipfile.ZipInfo):
    """Yield an entry's content a chunk at a time.

    zipfile's read() decompresses up to 1 GiB of deflated data in one call before
    it cuts the result to the size the entry declares; a chunk at a time, it
    decompresses no more than a chunk beyond that size.
    """
    with archive.open(info) as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            yield chunk


def _digest(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, keep: bool
) -> tuple[str, bytes | None]:
    """Return an entry's SHA-256, read in chunks, and its content if ``keep``."""
    digest = hashlib.sha256()
    kept = []
    for chunk in _chunks(archive, info):
        digest.update(chunk)
        if keep:
            kept.append(chunk)
    return digest.hexdigest(), b"".join(kept) if keep else None


def read_entry(archive: zipfile.ZipFile, entry: str | zipfile.ZipInfo) -> bytes:
    """Return the content of an entry, read a chunk at a time (see ``_chunks``).

    Use it in place of ZipFile.read() for any entry of a package to import.
    """
    info = entry if isinstance(entry, zipfile.ZipInfo) else archive.getinfo(entry)
    return b"".join(_chunks(archive, info))


def is_site_kit_name(name: str) -> bool:
    """True for a package entry that an import unpacks as a site's kit."""
    return "/" not in name and name.endswith(".zip") and name not in NOT_SITE_KITS


def _certificate(path: str, data: bytes) -> str:
    try:
        return certificate_fingerprint(data)
    except ValueError as exc:
        raise PackageVerificationError(path, "is not a PEM certificate") from exc


def _text(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PackageVerificationError(path, "is not UTF-8 text") from exc


def _server_ports(path: str, data: bytes) -> tuple[int, int]:
    """The FL (fed-learn) and admin ports in a signed ``fed_server.json``."""
    try:
        server = json.loads(data)["servers"][0]
        fed_learn_port = int(str(server["service"]["target"]).rsplit(":", 1)[1])
        admin_port = int(server["admin_port"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise PackageVerificationError(
            path, "does not give the FL server's ports"
        ) from exc
    return fed_learn_port, admin_port


def _values(kit_files: dict[str, bytes], basename: str, parse) -> dict:
    """Map each kit file called ``basename`` to the value ``parse`` reads from it."""
    return {
        path: parse(path, kit_files[path])
        for path in sorted(kit_files)
        if posixpath.basename(path) == basename
    }


def _agreed(values: dict, what: str):
    """Return the value every file gives and the first such file, or (None, "")."""
    if not values:
        return None, ""
    first_path = next(iter(values))
    first = values[first_path]
    for path, value in values.items():
        if value != first:
            raise PackageVerificationError(
                path, f"gives the {what} {value}, but {first_path} gives {first}"
            )
    return first, first_path


def _participant_rows(path: str, data: bytes | None) -> list[dict]:
    if data is None:
        return []
    try:
        rows = json.loads(data)
    except ValueError as exc:
        raise PackageVerificationError(path, "is not valid JSON") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise PackageVerificationError(path, "is not a list of participants")
    return rows


def _client_certificate(kit_files: dict[str, bytes], name: str, complete: bool):
    for candidate in dict.fromkeys((name, name.replace("_", "-"))):
        path = f"{candidate}.zip/startup/client.crt"
        if path in kit_files:
            return _certificate(path, kit_files[path])
    # A whole package has a kit for every client that was given one; a single
    # site's kit cannot show the other sites' certificates.
    return "" if complete else None


def derive_trust(
    kit_files: dict[str, bytes],
    participants_json: bytes | None,
    *,
    participants_path: str = ".participants.json",
    complete: bool = True,
) -> tuple[dict, dict]:
    """Read the trust section of a manifest from the kits it sums up.

    Args:
        kit_files: The files of the site kits, keyed ``<site>.zip/<path>``.
        participants_json: The content of ``.participants.json``, or None.
        participants_path: Where that file is, for messages.
        complete: Whether ``kit_files`` holds every site's kit. Otherwise a
            participant's certificate fingerprint is None when its kit is absent.

    Returns:
        tuple[dict, dict]: The trust section, and for each of its single values the
        first file it was read from.

    Raises:
        PackageVerificationError: If two files disagree, for example two copies of
            the root CA certificate or two ``server_host.txt``, if the kits name
            another FL server address than the SERVER participant's, or if a file
            cannot be read.
    """
    sources = {}
    root_ca, sources["root_ca_sha256"] = _agreed(
        _values(kit_files, "rootCA.pem", _certificate), "root CA fingerprint"
    )
    server_certificate, sources["server_certificate_sha256"] = _agreed(
        _values(kit_files, "server.crt", _certificate),
        "FL server certificate fingerprint",
    )
    server_address, sources["server_address"] = _agreed(
        _values(kit_files, "server_host.txt", _text), "FL server address"
    )
    ports, ports_path = _agreed(
        _values(kit_files, "fed_server.json", _server_ports), "FL and admin ports"
    )
    sources["fed_learn_port"] = sources["admin_port"] = ports_path

    participants = []
    for row in _participant_rows(participants_path, participants_json):
        name = str(row.get("participant_id") or "")
        role = str(row.get("role") or "")
        address = str(row.get("ip") or "")
        if role == "SERVER":
            certificate = server_certificate or ""
            named = address not in ("", "-")
            if server_address and named and address != server_address:
                raise PackageVerificationError(
                    sources["server_address"],
                    f"names the FL server {server_address}, but {participants_path} "
                    f"gives the SERVER participant the address {address}",
                )
        else:
            certificate = _client_certificate(kit_files, name, complete)
        participants.append(
            {
                "name": name,
                "role": role,
                "org": str(row.get("org") or ""),
                "address": address,
                "certificate_sha256": certificate,
            }
        )
    participants.sort(key=lambda entry: (entry["name"], entry["role"]))

    trust = {
        "root_ca_sha256": root_ca or "",
        "server_certificate_sha256": server_certificate or "",
        "server_address": server_address or "",
        "fed_learn_port": ports[0] if ports else None,
        "admin_port": ports[1] if ports else None,
        "participants": participants,
    }
    return trust, sources


def build_manifest(
    *,
    network_id: str,
    project: str,
    top_files: dict[str, bytes],
    kits: dict[str, dict[str, bytes]],
) -> dict:
    """Build the manifest of a startup package.

    Args:
        network_id: The network's identifier.
        project: The FLARE project name.
        top_files: The files at the top of the package, by name.
        kits: Each site's kit (``<site>.zip``), as the files in it by path.

    Returns:
        dict: The manifest. It holds no time, host name or SwarmMed version, so the
        same network exported twice gives the same manifest.

    Raises:
        PackageVerificationError: If the kits disagree (see :func:`derive_trust`).
    """
    kit_files = {
        f"{site}.zip/{path}": data
        for site, files in kits.items()
        for path, data in files.items()
        if path != MANIFEST_NAME
    }
    files = {
        name: sha256_hex(data)
        for name, data in top_files.items()
        if name != MANIFEST_NAME
    }
    files.update((path, sha256_hex(data)) for path, data in kit_files.items())
    trust, _sources = derive_trust(kit_files, top_files.get(".participants.json"))
    return {
        "format": MANIFEST_FORMAT,
        "network_id": str(network_id),
        "project": str(project),
        "trust": trust,
        "files": files,
    }


def parse_manifest(data: bytes) -> dict:
    """Parse ``manifest.json`` and check its form.

    Raises:
        PackageVerificationError: If it is not a manifest this version can check,
            or not in the canonical form SwarmMed writes.
    """
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageVerificationError(MANIFEST_NAME, "is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise PackageVerificationError(MANIFEST_NAME, "is not a JSON object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise PackageVerificationError(
            MANIFEST_NAME,
            f"has the format {manifest.get('format')!r}; this version of SwarmMed "
            f"checks {MANIFEST_FORMAT!r} only",
        )
    try:
        canonical = canonical_bytes(manifest)
    except ValueError as exc:
        raise PackageVerificationError(
            MANIFEST_NAME, "holds a value JSON does not allow"
        ) from exc
    if canonical != data:
        raise PackageVerificationError(
            MANIFEST_NAME, "is not in the canonical form SwarmMed writes"
        )
    files = manifest.get("files")
    trust = manifest.get("trust")
    if (
        not isinstance(files, dict)
        or not all(
            isinstance(value, str) and _HEX64.fullmatch(value)
            for value in files.values()
        )
        or not isinstance(trust, dict)
        or not isinstance(trust.get("participants"), list)
        or not isinstance(manifest.get("network_id"), str)
        or not isinstance(manifest.get("project"), str)
    ):
        raise PackageVerificationError(
            MANIFEST_NAME, "lacks a part of the format or has a part of the wrong type"
        )
    return manifest


def _reject_duplicates(names: list[str], prefix: str = "") -> None:
    seen = set()
    for name in sorted(names):
        if name in seen:
            raise PackageVerificationError(prefix + name, "appears more than once")
        seen.add(name)


def _compare_files(actual: dict[str, str], listed: dict[str, str]) -> None:
    """Refuse at the first path, in sorted order, that is extra, missing or changed."""
    present_kits = {path.split("/", 1)[0] for path in actual if "/" in path}
    for path in sorted(actual.keys() | listed.keys()):
        if path not in listed:
            raise PackageVerificationError(path, f"is not listed in {MANIFEST_NAME}")
        if path not in actual:
            kit = path.split("/", 1)[0] if "/" in path else ""
            if kit and kit not in present_kits:
                # The whole kit is missing; name it rather than its first file.
                path = kit
            raise PackageVerificationError(
                path, f"is listed in {MANIFEST_NAME} but missing"
            )
        if actual[path] != listed[path]:
            raise PackageVerificationError(
                path,
                f"does not match {MANIFEST_NAME} (SHA-256 {actual[path]}, "
                f"listed {listed[path]})",
            )


def _compare_trust(manifest: dict, trust: dict, sources: dict) -> None:
    expected = manifest["trust"]
    for field in _TRUST_FIELDS:
        if trust[field] != expected.get(field):
            source = sources.get(field) or "the package"
            raise PackageVerificationError(
                MANIFEST_NAME,
                f"gives trust.{field} as {expected.get(field)!r}, but {source} "
                f"gives {trust[field]!r}",
            )
    listed = expected["participants"]
    if len(listed) != len(trust["participants"]):
        raise PackageVerificationError(
            MANIFEST_NAME,
            f"lists {len(listed)} participants in trust.participants, but "
            f".participants.json has {len(trust['participants'])}",
        )
    for derived, entry in zip(trust["participants"], listed, strict=True):
        if not isinstance(entry, dict):
            raise PackageVerificationError(
                MANIFEST_NAME,
                "has an entry in trust.participants that is not an object",
            )
        for field in _PARTICIPANT_FIELDS:
            value = derived[field]
            if value is None:
                continue  # this site's kit cannot show another site's certificate
            if entry.get(field) != value:
                raise PackageVerificationError(
                    MANIFEST_NAME,
                    f"gives {field} {entry.get(field)!r} for participant "
                    f"{entry.get('name')!r} in trust.participants, but the package "
                    f"gives {value!r} for {derived['name']!r}",
                )


def _single_kit(package: zipfile.ZipFile, listed: dict[str, str]) -> str:
    """Return the ``<site>.zip`` of the manifest that a single site's kit is."""
    certificate = "startup/client.crt"
    if certificate not in package.namelist():
        raise PackageVerificationError(
            certificate,
            f"is missing, so the kit cannot be matched to a site in {MANIFEST_NAME}",
        )
    digest = sha256_hex(read_entry(package, certificate))
    suffix = "/" + certificate
    sites = sorted(
        path[: -len(suffix)]
        for path, value in listed.items()
        if path.endswith(suffix) and path.count("/") == 2 and value == digest
    )
    if not sites:
        raise PackageVerificationError(
            certificate, f"matches no site's kit in {MANIFEST_NAME}"
        )
    return sites[0]


def check_package(package: zipfile.ZipFile) -> PackageCheck:
    """Check a startup package, or a single site's kit, before it is imported.

    Every entry, and every entry of every site's kit in the package, must be safe to
    extract, stored or deflated, and no larger than MAX_ENTRY_BYTES unpacked; all
    of them together must stay within MAX_PACKAGE_BYTES. A package with
    ``manifest.json`` must then agree with it: every file it lists is there with
    that SHA-256, nothing else is there, every site's kit holds the identical
    manifest, and the trust section agrees with the files it sums up. A file that
    the manifest does not list is refused before it is read. A single site's kit
    is checked against the manifest's entries for that kit.

    Args:
        package: The package, opened for reading.

    Returns:
        PackageCheck: The manifest and the network fingerprint.

    Raises:
        PackageVerificationError: At the first offending path.
    """
    infos = package.infolist()
    for info in infos:
        problem = unsafe_entry_problem(info)
        if problem:
            raise PackageVerificationError(info.filename, problem)
    budget = _check_sizes(infos, "", MAX_PACKAGE_BYTES)
    kits: dict[str, zipfile.ZipFile] = {}
    for info in infos:
        if not is_site_kit_name(info.filename):
            continue
        # Every copy of a kit is checked: an import extracts each of them.
        try:
            kit = zipfile.ZipFile(io.BytesIO(read_entry(package, info)))
        except zipfile.BadZipFile as exc:
            raise PackageVerificationError(info.filename, "is not a ZIP file") from exc
        for inner in kit.infolist():
            problem = unsafe_entry_problem(inner)
            if problem:
                raise PackageVerificationError(
                    f"{info.filename}/{inner.filename}", problem
                )
        budget = _check_sizes(kit.infolist(), f"{info.filename}/", budget)
        kits.setdefault(info.filename, kit)

    names = [info.filename for info in infos]
    if MANIFEST_NAME not in names:
        for name in sorted(kits):
            if MANIFEST_NAME in kits[name].namelist():
                raise PackageVerificationError(
                    MANIFEST_NAME,
                    f"is missing, although {name}/{MANIFEST_NAME} is there: the "
                    "package was changed after it was exported, or SwarmMed 1.0.4 "
                    "or earlier exported it again from a network imported from a "
                    "later version; use the package that the coordinating site "
                    "exported",
                )
        return PackageCheck(manifest=None, fingerprint="")

    _reject_duplicates(names)
    for name in sorted(kits):
        _reject_duplicates(kits[name].namelist(), f"{name}/")
    manifest_bytes = read_entry(package, MANIFEST_NAME)
    manifest = parse_manifest(manifest_bytes)
    listed = manifest["files"]
    actual: dict[str, str] = {}
    # The content of the files the checks below read; the others are only hashed.
    contents: dict[str, bytes] = {}

    def add(path: str, archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
        if path in actual:
            # Two entries for one path; the checks above should have refused them.
            raise PackageVerificationError(path, "appears more than once")
        if path not in listed:
            # Refused before it is read.
            raise PackageVerificationError(path, f"is not listed in {MANIFEST_NAME}")
        keep = posixpath.basename(path) in _READ_BASENAMES
        actual[path], content = _digest(archive, info, keep)
        if content is not None:
            contents[path] = content

    if any(name.startswith("startup/") for name in names):
        site = _single_kit(package, listed)
        prefix = f"{site}/"
        for info in infos:
            if info.filename != MANIFEST_NAME:
                add(prefix + info.filename, package, info)
        listed = {
            path: value for path, value in listed.items() if path.startswith(prefix)
        }
        participants_path = prefix + ".participants.json"
        network_id_path = prefix + ".network_id"
        complete = False
    else:
        site = ""
        listed_kits = {path.split("/", 1)[0] for path in listed if "/" in path}
        for name in sorted(listed_kits & kits.keys()):
            if MANIFEST_NAME not in kits[name].namelist():
                raise PackageVerificationError(f"{name}/{MANIFEST_NAME}", "is missing")
            if read_entry(kits[name], MANIFEST_NAME) != manifest_bytes:
                raise PackageVerificationError(
                    f"{name}/{MANIFEST_NAME}",
                    f"differs from the package's {MANIFEST_NAME}",
                )
        for info in infos:
            name = info.filename
            if name == MANIFEST_NAME:
                continue
            if "/" in name and name.split("/", 1)[0] in kits:
                # It would be extracted into a folder named like the kit next to it.
                raise PackageVerificationError(
                    name,
                    f"lies in a folder named like the site kit {name.split('/', 1)[0]}",
                )
            if name in kits and name in listed_kits:
                for inner in kits[name].infolist():
                    if inner.filename != MANIFEST_NAME:
                        add(f"{name}/{inner.filename}", kits[name], inner)
            else:
                add(name, package, info)
        participants_path = ".participants.json"
        network_id_path = ".network_id"
        complete = True

    _compare_files(actual, listed)

    if network_id_path not in contents:
        raise PackageVerificationError(network_id_path, "is missing")
    network_id = _text(network_id_path, contents[network_id_path])
    if network_id != manifest["network_id"]:
        raise PackageVerificationError(
            MANIFEST_NAME,
            f"gives the network {manifest['network_id']!r}, but {network_id_path} "
            f"gives {network_id!r}",
        )
    kit_files = {path: data for path, data in contents.items() if "/" in path}
    trust, sources = derive_trust(
        kit_files,
        contents.get(participants_path),
        participants_path=participants_path,
        complete=complete,
    )
    _compare_trust(manifest, trust, sources)
    return PackageCheck(
        manifest=manifest,
        fingerprint=sha256_hex(manifest_bytes),
        site=site.removesuffix(".zip"),
    )


def package_fingerprint(package_bytes: bytes) -> str:
    """Return an exported package's network fingerprint; "" without a manifest."""
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as package:
        if MANIFEST_NAME not in package.namelist():
            return ""
        return sha256_hex(package.read(MANIFEST_NAME))
