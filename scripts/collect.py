#!/usr/bin/env python3
"""Collect Solana mainnet DEX program binaries and publish every version.

For every program in ``registry.json`` the script:

1. reads the on-chain deployment info (``solana program show``),
2. compares ``last_deployed_slot`` with ``dumps/<name>/metadata.json``
   (programs without a slot, e.g. non-upgradeable loaders, are compared by sha256),
3. on change dumps the binary (``solana program dump``), strips the account padding,
   uploads it as ``<name>-<slot>.so`` to the release of the day (tag ``snapshot-YYYY-MM-DD``),
   moves the previous metadata to ``archives/<name>/<slot>/`` and writes the new ``metadata.json``.

Only metadata lives in git, binaries live in GitHub Releases (one release per day with
changes), and ``index.json`` lists every known version. Failures are logged per program and never abort the run.
Exit codes: 0 = all programs processed, 1 = at least one failure, 2 = fatal setup error.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol, TypeVar

LOG = logging.getLogger("collect")

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
METADATA_FILE = "metadata.json"
INDEX_FILE = "index.json"
ELF_MAGIC = b"\x7fELF"
SHT_NOBITS = 8
CLI_TIMEOUT_SECS = 180
GH_TIMEOUT_SECS = 300
MAX_DUMP_ATTEMPTS = 3

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PUBKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
URL_RE = re.compile(r"https?://[^\s'\"]+")
GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?)?$")
WEBSITE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s<>\"'`()\[\]|]*)?$")

T = TypeVar("T")


class CollectError(Exception):
    """A recoverable failure for one program, or a fatal setup problem."""


class Outcome(Enum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Registry and metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    name: str
    program_id: str
    type: str
    description: str
    github: str | None = None
    website: str | None = None
    # Third-party page (DefiLlama, block explorer) for projects with neither GitHub nor a website.
    reference: str | None = None

    @classmethod
    def from_json(cls, raw: object) -> RegistryEntry:
        if not isinstance(raw, dict):
            raise CollectError(f"registry entry must be an object, got {type(raw).__name__}")
        fields: dict[str, str] = {}
        for key in ("name", "program_id", "type", "description"):
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise CollectError(f"registry entry {raw!r}: missing or empty '{key}'")
            fields[key] = value.strip()
        # The name becomes a directory name and part of asset file names, so keep it a strict slug.
        if not NAME_RE.fullmatch(fields["name"]):
            raise CollectError(f"invalid name {fields['name']!r}: use lowercase letters, digits and '-'")
        if not PUBKEY_RE.fullmatch(fields["program_id"]):
            raise CollectError(f"{fields['name']}: invalid program_id {fields['program_id']!r}")
        links = {
            "github": cls._optional_link(raw, "github", GITHUB_URL_RE, fields["name"]),
            "website": cls._optional_link(raw, "website", WEBSITE_URL_RE, fields["name"]),
            "reference": cls._optional_link(raw, "reference", WEBSITE_URL_RE, fields["name"]),
        }
        return cls(**fields, **links)

    @staticmethod
    def _optional_link(raw: dict[object, object], key: str, pattern: re.Pattern[str], name: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise CollectError(f"{name}: invalid {key} URL {value!r}")
        return value


def load_registry(path: Path) -> list[RegistryEntry]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError(f"cannot read registry {path}: {exc}") from exc
    programs = raw.get("programs") if isinstance(raw, dict) else None
    if not isinstance(programs, list) or not programs:
        raise CollectError(f"{path}: expected an object with a non-empty 'programs' array")
    entries = [RegistryEntry.from_json(item) for item in programs]
    for label, values in (("name", [e.name for e in entries]), ("program_id", [e.program_id for e in entries])):
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            raise CollectError(f"registry has duplicate {label}: {', '.join(dupes)}")
    return entries


@dataclass(frozen=True, slots=True)
class ProgramInfo:
    """Deployment info reported by ``solana program show``."""

    program_id: str
    loader: str
    program_data_address: str | None
    last_deployed_slot: int | None
    upgrade_authority: str | None

    @classmethod
    def from_cli_json(cls, text: str, expected_id: str) -> ProgramInfo:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CollectError(f"unparseable 'program show' output: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("programId") != expected_id:
            raise CollectError(f"'program show' returned unexpected data for {expected_id}")
        slot = raw.get("lastDeploySlot")
        if slot is not None and (not isinstance(slot, int) or isinstance(slot, bool)):
            raise CollectError(f"unexpected lastDeploySlot value: {slot!r}")
        return cls(
            program_id=expected_id,
            loader=str(raw.get("owner", "")),
            program_data_address=_none_if_blank(raw.get("programdataAddress")),
            last_deployed_slot=slot,
            upgrade_authority=_none_if_blank(raw.get("authority")),
        )


def _none_if_blank(value: object) -> str | None:
    """Map missing / 'none' CLI values (immutable or non-upgradeable programs) to None."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in {"", "none"} else text


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer or null, got {value!r}")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected a string or null, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class Metadata:
    name: str
    program_id: str
    loader: str
    program_data_address: str | None
    sha256: str
    size: int
    onchain_sha256: str | None
    onchain_size: int | None
    last_deployed_slot: int | None
    upgrade_authority: str | None
    collected_at: str
    previous_slot: int | None
    release_tag: str | None
    download_url: str | None

    def to_dict(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in dataclasses.fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_file(cls, path: Path) -> Metadata:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CollectError(f"{path}: expected a JSON object")
        program_id, sha256 = raw.get("program_id"), raw.get("sha256")
        if not isinstance(program_id, str) or not isinstance(sha256, str):
            raise CollectError(f"{path}: missing 'program_id' or 'sha256'")
        try:
            return cls(
                name=str(raw.get("name", "")),
                program_id=program_id,
                loader=str(raw.get("loader", "")),
                program_data_address=_optional_str(raw.get("program_data_address")),
                sha256=sha256,
                size=int(raw.get("size", 0)),
                onchain_sha256=_optional_str(raw.get("onchain_sha256")),
                onchain_size=_optional_int(raw.get("onchain_size")),
                last_deployed_slot=_optional_int(raw.get("last_deployed_slot")),
                upgrade_authority=_optional_str(raw.get("upgrade_authority")),
                collected_at=str(raw.get("collected_at", "")),
                previous_slot=_optional_int(raw.get("previous_slot")),
                release_tag=_optional_str(raw.get("release_tag")),
                download_url=_optional_str(raw.get("download_url")),
            )
        except (TypeError, ValueError) as exc:
            raise CollectError(f"{path}: invalid field value: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    outcome: Outcome
    slot: int | None = None
    previous_slot: int | None = None
    sha256: str | None = None
    previous_sha256: str | None = None
    asset: str | None = None
    message: str = ""


def version_key(slot: int | None, sha256: str) -> str:
    """Name of a version (archive directory, asset file name suffix): the slot, else a sha256 prefix."""
    return str(slot) if slot is not None else f"sha256-{sha256[:12]}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# External commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], int, str], CommandResult]


def run_command(cmd: Sequence[str], timeout: int, label: str) -> CommandResult:
    """Run without a shell; ``label`` is used in errors instead of the argv (which may hold the RPC URL)."""
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, check=False, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectError(f"'{label}' timed out after {timeout}s") from exc
    except OSError as exc:
        raise CollectError(f"cannot execute '{label}': {exc}") from exc
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def redact_urls(text: str) -> str:
    """RPC URLs may embed API keys and the CLI echoes them in errors."""
    return URL_RE.sub("<url>", text)


def _failure_detail(result: CommandResult) -> str:
    return redact_urls((result.stderr.strip() or result.stdout.strip() or "no output")[:500])


class ProgramSource(Protocol):
    """What the collector needs from the chain; implemented by SolanaCli."""

    def show(self, program_id: str) -> ProgramInfo: ...

    def dump(self, program_id: str, dest: Path) -> None: ...


class SolanaCli:
    """Thin wrapper around the official ``solana`` CLI."""

    def __init__(self, rpc_url: str, keypair: Path, solana_bin: str = "solana", run: Runner = run_command) -> None:
        self._rpc_url = rpc_url
        self._keypair = keypair
        self._bin = solana_bin
        self._run_cmd = run

    def show(self, program_id: str) -> ProgramInfo:
        out = self._run(["program", "show", program_id, "--output", "json"])
        return ProgramInfo.from_cli_json(out, program_id)

    def dump(self, program_id: str, dest: Path) -> None:
        self._run(["program", "dump", program_id, str(dest)])
        if not dest.is_file():
            raise CollectError(f"'program dump' reported success but {dest.name} is missing")

    def _run(self, args: Sequence[str]) -> str:
        # The CLI refuses to run without a signer even for read-only commands,
        # so a throwaway keypair is always passed.
        cmd = [self._bin, "--keypair", str(self._keypair), "--url", self._rpc_url, *args]
        label = f"solana {args[0]} {args[1]}"
        result = self._run_cmd(cmd, CLI_TIMEOUT_SECS, label)
        if result.returncode != 0:
            raise CollectError(f"'{label}' failed: {_failure_detail(result)}")
        return result.stdout


def create_ephemeral_keypair(path: Path) -> Path:
    keygen = shutil.which("solana-keygen")
    if keygen is None:
        raise CollectError("solana-keygen not found in PATH; install the Solana CLI first")
    cmd = [keygen, "new", "--no-bip39-passphrase", "--silent", "--force", "--outfile", str(path)]
    result = run_command(cmd, 60, "solana-keygen new")
    if result.returncode != 0 or not path.is_file():
        raise CollectError(f"solana-keygen failed: {_failure_detail(result)}")
    return path


# ---------------------------------------------------------------------------
# Binary storage (GitHub Releases, or a local directory for dry runs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseSpec:
    tag: str
    title: str
    notes: str


class BinaryStore(Protocol):
    def publish(self, spec: ReleaseSpec, asset: Path) -> str:
        """Make ``asset`` available under release ``spec.tag``; return its download URL."""
        ...

    def set_notes(self, spec: ReleaseSpec) -> None:
        """Replace the title and notes of an existing release."""
        ...


class GhReleaseStore:
    """Publishes binaries as assets of GitHub releases, via the ``gh`` CLI.

    The first asset of a release creates it, later ones are uploaded into it. Publishing is
    idempotent: an asset with the same name and size is reused, so a run that uploaded but
    failed to push can simply be repeated.
    """

    def __init__(self, repo: str, gh_bin: str = "gh", run: Runner = run_command) -> None:
        self._repo = repo
        self._bin = gh_bin
        self._run_cmd = run

    def publish(self, spec: ReleaseSpec, asset: Path) -> str:
        url = f"https://github.com/{self._repo}/releases/download/{spec.tag}/{asset.name}"
        size = asset.stat().st_size
        existing = self._existing_assets(spec.tag)
        if existing is None:
            self._gh(["release", "create", spec.tag, str(asset), "--title", spec.title, "--notes", spec.notes])
        elif asset.name not in existing:
            self._gh(["release", "upload", spec.tag, str(asset)])
        elif existing[asset.name] != size:
            raise CollectError(
                f"release {spec.tag} already has {asset.name} with {existing[asset.name]} bytes, expected {size}"
            )
        else:
            LOG.info("REUSED    release %s already holds %s", spec.tag, asset.name)
        return url

    def set_notes(self, spec: ReleaseSpec) -> None:
        self._gh(["release", "edit", spec.tag, "--title", spec.title, "--notes", spec.notes])

    def _existing_assets(self, tag: str) -> dict[str, int] | None:
        """Asset name -> size of an existing release, or None if the release does not exist."""
        result = self._run_cmd(
            [self._bin, "release", "view", tag, "--repo", self._repo, "--json", "assets"], GH_TIMEOUT_SECS, "gh release view"
        )
        if result.returncode != 0:
            if "release not found" in result.stderr.lower():
                return None
            raise CollectError(f"'gh release view {tag}' failed: {_failure_detail(result)}")
        try:
            assets = json.loads(result.stdout)["assets"]
            return {str(a["name"]): int(a["size"]) for a in assets}
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CollectError(f"unexpected 'gh release view' output: {exc}") from exc

    def _gh(self, args: Sequence[str]) -> None:
        label = f"gh {args[0]} {args[1]} {args[2]}"
        result = self._run_cmd([self._bin, *args, "--repo", self._repo], GH_TIMEOUT_SECS, label)
        if result.returncode != 0:
            raise CollectError(f"'{label}' failed: {_failure_detail(result)}")


class LocalStore:
    """Dry-run store: copies assets to ``<directory>/<tag>/`` and returns a file:// URL."""

    NOTES_FILE = "RELEASE_NOTES.md"

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def publish(self, spec: ReleaseSpec, asset: Path) -> str:
        dest = self._dir / spec.tag / asset.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, dest)
        return dest.resolve().as_uri()

    def set_notes(self, spec: ReleaseSpec) -> None:
        write_text_atomic(self._dir / spec.tag / self.NOTES_FILE, f"# {spec.title}\n\n{spec.notes}")


# ---------------------------------------------------------------------------
# ELF handling
# ---------------------------------------------------------------------------


def elf_extent(data: bytes) -> int:
    """End offset of the last byte the ELF64 (little endian) headers refer to."""
    if data[:4] != ELF_MAGIC or len(data) < 64 or data[4] != 2 or data[5] != 1:
        raise CollectError("dumped file is not a little-endian ELF64 binary")
    try:
        ph_off, sh_off = struct.unpack_from("<QQ", data, 0x20)
        ph_size, ph_num, sh_size, sh_num = struct.unpack_from("<HHHH", data, 0x36)
        end = max(ph_off + ph_size * ph_num, sh_off + sh_size * sh_num)
        for i in range(ph_num):
            base = ph_off + i * ph_size
            (p_offset,) = struct.unpack_from("<Q", data, base + 8)
            (p_filesz,) = struct.unpack_from("<Q", data, base + 32)
            end = max(end, p_offset + p_filesz)
        for i in range(sh_num):
            base = sh_off + i * sh_size
            (sh_type,) = struct.unpack_from("<I", data, base + 4)
            sh_offset, sh_len = struct.unpack_from("<QQ", data, base + 24)
            if sh_type != SHT_NOBITS:
                end = max(end, sh_offset + sh_len)
    except struct.error as exc:
        raise CollectError(f"malformed ELF headers: {exc}") from exc
    return end


def strip_padding(data: bytes) -> bytes:
    """Drop the zero padding of a ProgramData account without cutting into the ELF.

    Plain trailing-zero stripping is not enough: the last section header usually ends
    in zero bytes, so the cut-off point is the larger of the last non-zero byte and the
    end of everything the ELF headers reference.
    """
    keep = max(len(data.rstrip(b"\x00")), elf_extent(data))
    return data[: min(keep, len(data))]


@dataclass(frozen=True, slots=True)
class Binary:
    """A dumped program: the stored (padding-free) bytes and hashes of both forms."""

    data: bytes
    sha256: str
    onchain_sha256: str
    onchain_size: int

    @property
    def size(self) -> int:
        return len(self.data)


def prepare_binary(raw: bytes) -> Binary:
    stored = strip_padding(raw)
    return Binary(
        data=stored,
        sha256=hashlib.sha256(stored).hexdigest(),
        onchain_sha256=hashlib.sha256(raw).hexdigest(),
        onchain_size=len(raw),
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Layout:
    root: Path

    @property
    def registry(self) -> Path:
        return self.root / "registry.json"

    @property
    def index(self) -> Path:
        return self.root / INDEX_FILE

    def dump_dir(self, name: str) -> Path:
        return self.root / "dumps" / name

    def archive_root(self, name: str) -> Path:
        return self.root / "archives" / name


SNAPSHOT_PREFIX = "snapshot-"
PENDING_NOTES = "Collection in progress; the list of programs is added when the run finishes.\n"
SOURCE_CODE_NOTE = (
    "> [!NOTE]\n"
    "> Download the `.so` files below. The two \"Source code\" archives are added to every release by\n"
    "> GitHub automatically and only contain this repository's scripts and metadata.\n"
)


def snapshot_tag(day: str) -> str:
    """Release that holds every version first collected on ``day`` (YYYY-MM-DD, UTC)."""
    return f"{SNAPSHOT_PREFIX}{day}"


def snapshot_spec(tag: str, notes: str = PENDING_NOTES) -> ReleaseSpec:
    return ReleaseSpec(tag=tag, title=f"Snapshot {tag.removeprefix(SNAPSHOT_PREFIX)}", notes=notes)


def program_versions(layout: Layout, entry: RegistryEntry) -> list[Metadata]:
    """All stored versions of a program, newest first."""
    latest_path = layout.dump_dir(entry.name) / METADATA_FILE
    versions = [Metadata.from_file(latest_path)] if latest_path.is_file() else []
    archived = [Metadata.from_file(p) for p in layout.archive_root(entry.name).glob(f"*/{METADATA_FILE}")]
    versions += sorted(archived, key=lambda m: (m.collected_at, m.last_deployed_slot or 0), reverse=True)
    return versions


def asset_name(meta: Metadata) -> str:
    return (meta.download_url or "").rsplit("/", 1)[-1]


def snapshot_notes(layout: Layout, entries: Sequence[RegistryEntry], tag: str, repo_url: str | None = None) -> str:
    """Release notes listing every version stored in release ``tag``, built from the metadata.

    Every row links the project (GitHub, else website, else reference page) and the downloadable file.
    ``repo_url`` (https://github.com/OWNER/REPO) adds links to the metadata kept in the repository.
    """
    rows = []
    for entry in entries:
        for meta in program_versions(layout, entry):
            if meta.release_tag != tag:
                continue
            name = asset_name(meta)
            file_cell = f"[{name}]({meta.download_url})" if name and meta.download_url else "-"
            slot = meta.last_deployed_slot if meta.last_deployed_slot is not None else "-"
            previous = meta.previous_slot if meta.previous_slot is not None else "-"
            rows.append(
                f"| {meta.name} | {link_cell(entry) or '-'} | {file_cell} | {slot} | {previous} "
                f"| `{meta.sha256[:12]}` | {meta.size} | `{meta.program_id}` |"
            )
    where = (
        f" ([dumps/]({repo_url}/tree/main/dumps), [index.json]({repo_url}/blob/main/index.json))" if repo_url else ""
    )
    return (
        f"{SOURCE_CODE_NOTE}\n"
        f"{len(rows)} program version(s) first collected in this snapshot (Slot is `last_deployed_slot`). The\n"
        "binaries have the ProgramData zero padding stripped; each program's `metadata.json` records the\n"
        f"hashes of both forms{where}.\n\n"
        "| Program | Link | File | Slot | Previous slot | sha256 | Size | Program ID |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n"
    )


class Collector:
    def __init__(
        self,
        layout: Layout,
        source: ProgramSource,
        store: BinaryStore,
        *,
        release_tag: str,
        verify_hash: bool = False,
        retries: int = 3,
        backoff_secs: float = 2.0,
        delay_secs: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._layout = layout
        self._source = source
        self._store = store
        self._release_tag = release_tag
        self._verify_hash = verify_hash
        self._retries = max(1, retries)
        self._backoff = backoff_secs
        self._delay = delay_secs
        self._sleep = sleep
        self._now = now

    def run(self, entries: Sequence[RegistryEntry]) -> list[Result]:
        results: list[Result] = []
        for index, entry in enumerate(entries):
            if index and self._delay > 0:
                self._sleep(self._delay)
            try:
                result = self.collect_one(entry)
            except (CollectError, OSError) as exc:
                LOG.error("FAILED    %-20s %s", entry.name, exc)
                result = Result(entry.name, Outcome.FAILED, message=str(exc))
            results.append(result)
        return results

    def collect_one(self, entry: RegistryEntry) -> Result:
        prev = self._load_previous(entry)
        info = self._retry(f"show {entry.name}", lambda: self._source.show(entry.program_id))

        if prev is not None and not self._needs_dump(prev, info):
            LOG.info("SKIPPED   %-20s slot %s unchanged", entry.name, info.last_deployed_slot)
            return Result(entry.name, Outcome.UNCHANGED, slot=info.last_deployed_slot)

        with tempfile.TemporaryDirectory(prefix="dexdump-") as tmp:
            raw_path = Path(tmp) / "onchain.bin"
            info = self._dump_consistent(entry, info, raw_path)
            binary = prepare_binary(raw_path.read_bytes())
            slot = info.last_deployed_slot
            if prev is not None and prev.sha256 == binary.sha256 and prev.last_deployed_slot == slot:
                LOG.info("SKIPPED   %-20s sha256 %s unchanged", entry.name, binary.sha256[:12])
                return Result(entry.name, Outcome.UNCHANGED, slot=slot)
            if prev is not None and prev.sha256 == binary.sha256:
                LOG.info("NOTE      %-20s redeployed with identical bytes", entry.name)

            key = version_key(slot, binary.sha256)
            if prev is not None and slot is not None and prev.last_deployed_slot == slot:
                key = f"{key}-{binary.sha256[:12]}"  # same slot, different bytes: keep file names unique
            asset = Path(tmp) / f"{entry.name}-{key}.so"
            meta = Metadata(
                name=entry.name,
                program_id=entry.program_id,
                loader=info.loader,
                program_data_address=info.program_data_address,
                sha256=binary.sha256,
                size=binary.size,
                onchain_sha256=binary.onchain_sha256,
                onchain_size=binary.onchain_size,
                last_deployed_slot=slot,
                upgrade_authority=info.upgrade_authority,
                collected_at=self._now(),
                previous_slot=prev.last_deployed_slot if prev else None,
                release_tag=self._release_tag,
                download_url=None,
            )
            asset.write_bytes(binary.data)
            spec = snapshot_spec(self._release_tag)
            url = self._retry(f"publish {entry.name}", lambda: self._store.publish(spec, asset))
            meta = dataclasses.replace(meta, download_url=url)
            self._install(entry.name, prev, meta)

        outcome = Outcome.UPDATED if prev is not None else Outcome.NEW
        LOG.info(
            "%-9s %-20s slot %s sha256 %s size %d -> %s/%s",
            outcome.name, entry.name, slot, binary.sha256[:12], binary.size, self._release_tag, asset.name,
        )
        return Result(
            entry.name,
            outcome,
            slot=slot,
            previous_slot=meta.previous_slot,
            sha256=binary.sha256,
            previous_sha256=prev.sha256 if prev else None,
            asset=asset.name,
        )

    def _load_previous(self, entry: RegistryEntry) -> Metadata | None:
        path = self._layout.dump_dir(entry.name) / METADATA_FILE
        if not path.is_file():
            return None
        prev = Metadata.from_file(path)
        if prev.program_id != entry.program_id:
            raise CollectError(
                f"{path} belongs to {prev.program_id}, registry says {entry.program_id}; refusing to overwrite"
            )
        return prev

    def _needs_dump(self, prev: Metadata, info: ProgramInfo) -> bool:
        """Slot first; fall back to sha256 (needs a dump) when no slot is available."""
        if self._verify_hash:
            return True
        if prev.last_deployed_slot is None or info.last_deployed_slot is None:
            return True
        return prev.last_deployed_slot != info.last_deployed_slot

    def _dump_consistent(self, entry: RegistryEntry, info: ProgramInfo, dest: Path) -> ProgramInfo:
        """Dump and make sure the program was not redeployed while downloading."""
        for attempt in range(1, MAX_DUMP_ATTEMPTS + 1):
            self._retry(f"dump {entry.name}", lambda: self._source.dump(entry.program_id, dest))
            with dest.open("rb") as handle:
                if handle.read(len(ELF_MAGIC)) != ELF_MAGIC:
                    raise CollectError("dumped file is not an ELF binary")
            if info.last_deployed_slot is None:
                return info
            after = self._retry(f"show {entry.name}", lambda: self._source.show(entry.program_id))
            if after.last_deployed_slot == info.last_deployed_slot:
                return info
            LOG.warning(
                "RETRY     %-20s redeployed during dump (slot %s -> %s), attempt %d/%d",
                entry.name, info.last_deployed_slot, after.last_deployed_slot, attempt, MAX_DUMP_ATTEMPTS,
            )
            info = after
        raise CollectError("program kept changing while being dumped")

    def _install(self, name: str, prev: Metadata | None, meta: Metadata) -> None:
        """Archive the current metadata, then write the new one; roll back on failure."""
        current = self._layout.dump_dir(name) / METADATA_FILE
        archived = self._archive_current(name, prev) if prev is not None else None
        try:
            write_text_atomic(current, meta.to_json())
        except OSError as exc:
            if archived is not None:
                self._restore(archived, current)
            raise CollectError(f"failed to write new metadata: {exc}") from exc
        if archived is not None:
            LOG.info("ARCHIVED  %-20s -> %s", name, archived.parent.relative_to(self._layout.root))

    def _archive_current(self, name: str, prev: Metadata) -> Path:
        dest_dir = self._layout.archive_root(name) / version_key(prev.last_deployed_slot, prev.sha256)
        if dest_dir.exists():
            dest_dir = dest_dir.with_name(f"{dest_dir.name}-{prev.sha256[:12]}")
        if dest_dir.exists():
            raise CollectError(f"archive {dest_dir} already exists; refusing to overwrite")
        dest = dest_dir / METADATA_FILE
        try:
            dest_dir.mkdir(parents=True)
            shutil.move(str(self._layout.dump_dir(name) / METADATA_FILE), str(dest))
        except (OSError, shutil.Error) as exc:
            raise CollectError(f"failed to archive previous metadata: {exc}") from exc
        return dest

    @staticmethod
    def _restore(archived: Path, current: Path) -> None:
        """Best effort: move archived metadata back into dumps/."""
        try:
            current.with_name(current.name + ".tmp").unlink(missing_ok=True)
        except OSError as exc:
            LOG.error("ROLLBACK  could not remove temporary file next to %s: %s", current, exc)
        try:
            shutil.move(str(archived), str(current))
            archived.parent.rmdir()
        except (OSError, shutil.Error) as exc:
            LOG.error("ROLLBACK  could not restore %s: %s", archived, exc)

    def _retry(self, what: str, fn: Callable[[], T]) -> T:
        for attempt in range(1, self._retries + 1):
            try:
                return fn()
            except CollectError as exc:
                if attempt == self._retries:
                    raise
                wait = self._backoff * 2 ** (attempt - 1)
                LOG.warning("RETRY     %s failed (%d/%d): %s; waiting %.1fs", what, attempt, self._retries, exc, wait)
                self._sleep(wait)
        raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Index and reporting
# ---------------------------------------------------------------------------


def _version_summary(meta: Metadata) -> dict[str, object]:
    return {
        "last_deployed_slot": meta.last_deployed_slot,
        "sha256": meta.sha256,
        "size": meta.size,
        "collected_at": meta.collected_at,
        "release_tag": meta.release_tag,
        "download_url": meta.download_url,
    }


def build_index(layout: Layout, entries: Sequence[RegistryEntry]) -> dict[str, object]:
    """Every known version per registry program, newest first."""
    programs: list[dict[str, object]] = [
        {
            "name": entry.name,
            "program_id": entry.program_id,
            "type": entry.type,
            "description": entry.description,
            "github": entry.github,
            "website": entry.website,
            "reference": entry.reference,
            "versions": [_version_summary(v) for v in program_versions(layout, entry)],
        }
        for entry in entries
    ]
    return {"programs": programs}


def format_commit_message(results: Sequence[Result], release_tag: str) -> str | None:
    changed = [r for r in results if r.outcome in (Outcome.NEW, Outcome.UPDATED)]
    if not changed:
        return None
    new = sum(r.outcome is Outcome.NEW for r in changed)
    upgraded = len(changed) - new
    parts = [f"{upgraded} upgraded"] if upgraded else []
    if new:
        parts.append(f"{new} new")
    lines = [f"chore(dumps): {', '.join(parts)} ({release_tag})", ""]
    for r in changed:
        if r.outcome is Outcome.NEW:
            where = f"slot {r.slot}" if r.slot is not None else f"sha256 {(r.sha256 or '')[:12]}"
            lines.append(f"- {r.name}: first dump at {where}")
        elif r.slot is None:
            lines.append(f"- {r.name}: sha256 {(r.previous_sha256 or '')[:12]} -> {(r.sha256 or '')[:12]}")
        else:
            lines.append(f"- {r.name}: slot {r.previous_slot} -> {r.slot}")
    return "\n".join(lines) + "\n"


def _host(url: str) -> str:
    return url.removeprefix("https://").split("/")[0].removeprefix("www.")


def link_cell(entry: RegistryEntry) -> str:
    """GitHub link (text: owner/repo or the account), else the website, else the reference page (text: host)."""
    if entry.github:
        path = entry.github.removeprefix("https://github.com/")
        owner_repo = "/".join(path.split("/")[:2])
        return f"[{owner_repo}]({entry.github})"
    for url in (entry.website, entry.reference):
        if url:
            return f"[{_host(url)}]({url})"
    return ""


def registry_table(entries: Sequence[RegistryEntry]) -> str:
    rows = ["| Name | Program ID | Type | Description | Link |", "|---|---|---|---|---|"]
    rows += [
        f"| `{e.name}` | `{e.program_id}` | {e.type} | {e.description} | {link_cell(e)} |" for e in entries
    ]
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=root, help="repository root (default: parent of scripts/)")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="only process these registry names")
    parser.add_argument(
        "--release-repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="OWNER/REPO whose releases hold the binaries (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--dry-run",
        type=Path,
        metavar="DIR",
        help="work on a copy of the metadata in DIR and save binaries to DIR/releases; never touches the repo or GitHub",
    )
    parser.add_argument("--verify-hash", action="store_true", help="always dump and compare sha256, even if the slot is unchanged")
    parser.add_argument("--retries", type=int, default=3, help="attempts per external call (default: 3)")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds to wait between programs (default: 0.5)")
    parser.add_argument("--solana-bin", default="solana", help="solana CLI binary (default: solana)")
    parser.add_argument("--commit-message-file", type=Path, help="write a commit message here if anything changed")
    parser.add_argument("--print-registry-table", action="store_true", help="print the registry as a markdown table and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def select_entries(entries: list[RegistryEntry], only: Sequence[str] | None) -> list[RegistryEntry]:
    if not only:
        return entries
    known = {e.name for e in entries}
    unknown = sorted(set(only) - known)
    if unknown:
        raise CollectError(f"unknown program name(s): {', '.join(unknown)}")
    return [e for e in entries if e.name in set(only)]


def prepare_workspace(repo: Layout, args: argparse.Namespace) -> tuple[Layout, BinaryStore]:
    """Pick where metadata is written and where binaries go."""
    if args.dry_run is not None:
        out = args.dry_run.resolve()
        protected = (repo.root / "dumps", repo.root / "archives")
        if out == repo.root or any(out == p or p in out.parents for p in protected):
            raise CollectError(f"--dry-run directory {out} must not be the repository or its metadata folders")
        if out.exists() and any(out.iterdir()):
            raise CollectError(f"--dry-run directory {out} must be empty or not exist yet")
        for sub in ("dumps", "archives"):
            if (repo.root / sub).is_dir():
                shutil.copytree(repo.root / sub, out / sub, dirs_exist_ok=True)
        LOG.info("Dry run: metadata copied to %s, binaries go to %s", out, out / "releases")
        return Layout(out), LocalStore(out / "releases")
    if not REPO_RE.fullmatch(args.release_repo):
        raise CollectError("set --release-repo OWNER/REPO (or GITHUB_REPOSITORY), or use --dry-run DIR")
    if shutil.which("gh") is None:
        raise CollectError("gh (GitHub CLI) not found in PATH; it is needed to publish releases")
    LOG.info("Publishing binaries to releases of %s", args.release_repo)
    return repo, GhReleaseStore(args.release_repo)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    repo = Layout(args.root.resolve())
    # An unset GitHub secret expands to an empty string, so treat "" as unset.
    rpc_url = os.environ.get("RPC_URL", "").strip() or DEFAULT_RPC_URL
    release_tag = snapshot_tag(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    try:
        registry = load_registry(repo.registry)
        entries = select_entries(registry, args.only)
        if args.print_registry_table:
            print(registry_table(entries), end="")
            return 0
        layout, store = prepare_workspace(repo, args)
        LOG.info("RPC: %s", "default public endpoint" if rpc_url == DEFAULT_RPC_URL else "custom RPC_URL")
        LOG.info("Collecting %d program(s)", len(entries))
        with tempfile.TemporaryDirectory(prefix="dexdump-key-") as key_dir:
            keypair = create_ephemeral_keypair(Path(key_dir) / "id.json")
            collector = Collector(
                layout,
                SolanaCli(rpc_url, keypair, args.solana_bin),
                store,
                release_tag=release_tag,
                verify_hash=args.verify_hash,
                retries=args.retries,
                delay_secs=args.delay,
            )
            results = collector.run(entries)
    except CollectError as exc:
        LOG.error("fatal: %s", exc)
        return 2

    failed = [r for r in results if r.outcome is Outcome.FAILED]
    if any(r.outcome in (Outcome.NEW, Outcome.UPDATED) for r in results):
        # Rebuilt from all metadata, so a second run on the same day keeps the full list.
        try:
            repo_url = None if args.dry_run is not None else f"https://github.com/{args.release_repo}"
            store.set_notes(snapshot_spec(release_tag, snapshot_notes(layout, registry, release_tag, repo_url)))
        except (CollectError, OSError) as exc:
            LOG.error("failed to write notes of release %s: %s", release_tag, exc)
            failed.append(Result(release_tag, Outcome.FAILED, message=str(exc)))
    try:
        write_text_atomic(layout.index, json.dumps(build_index(layout, registry), indent=2, ensure_ascii=False) + "\n")
    except (CollectError, OSError) as exc:
        LOG.error("failed to write %s: %s", INDEX_FILE, exc)
        failed.append(Result(INDEX_FILE, Outcome.FAILED, message=str(exc)))

    counts = {outcome: sum(r.outcome is outcome for r in results) for outcome in Outcome}
    LOG.info(
        "Done: %d new, %d updated, %d unchanged, %d failed",
        counts[Outcome.NEW], counts[Outcome.UPDATED], counts[Outcome.UNCHANGED], len(failed),
    )
    for r in failed:
        LOG.error("  failed: %s: %s", r.name, r.message)

    message = format_commit_message(results, release_tag)
    if message and args.commit_message_file:
        args.commit_message_file.write_text(message, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
