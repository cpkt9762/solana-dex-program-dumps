"""Unit tests for scripts/collect.py; run with: python3 -m unittest discover -s tests"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import collect  # noqa: E402

PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
OTHER_ID = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
LOADER_V3 = "BPFLoaderUpgradeab1e11111111111111111111111"
LOADER_V2 = "BPFLoader2111111111111111111111111111111111"
NAME = "raydium-amm-v4"
TAG = "snapshot-2026-01-01"


def elf(payload: bytes, *, sh_off: int = 0, sh_num: int = 0) -> bytes:
    """Minimal ELF64 LE image: 64-byte header (optionally pointing at a section table) + payload."""
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", header, 0x28, sh_off)
    struct.pack_into("<HH", header, 0x3A, 64 if sh_num else 0, sh_num)
    return bytes(header) + payload


class FakeSource:
    """In-memory stand-in for SolanaCli: one binary and one show() answer per program."""

    def __init__(self) -> None:
        self.infos: dict[str, collect.ProgramInfo] = {}
        self.binaries: dict[str, bytes] = {}
        self.dump_calls: list[str] = []
        self.show_queue: dict[str, list[collect.ProgramInfo]] = {}
        self.failing: set[str] = set()

    def deploy(self, program_id: str, slot: int | None, binary: bytes) -> None:
        loader = LOADER_V3 if slot is not None else LOADER_V2
        self.infos[program_id] = collect.ProgramInfo(program_id, loader, "PD" if slot else None, slot, "AUTH" if slot else None)
        self.binaries[program_id] = binary

    def show(self, program_id: str) -> collect.ProgramInfo:
        if program_id in self.failing:
            raise collect.CollectError("rpc down")
        queue = self.show_queue.get(program_id)
        if queue:
            return queue.pop(0)
        return self.infos[program_id]

    def dump(self, program_id: str, dest: Path) -> None:
        if program_id in self.failing:
            raise collect.CollectError("rpc down")
        self.dump_calls.append(program_id)
        dest.write_bytes(self.binaries[program_id])


class FakeStore:
    """Records published assets (by file name) and release notes in memory."""

    def __init__(self) -> None:
        self.published: dict[str, tuple[str, bytes]] = {}
        self.notes: dict[str, collect.ReleaseSpec] = {}
        self.fail = False

    def publish(self, spec: collect.ReleaseSpec, asset: Path) -> str:
        if self.fail:
            raise collect.CollectError("github down")
        self.published[asset.name] = (spec.tag, asset.read_bytes())
        return f"https://example.invalid/{spec.tag}/{asset.name}"

    def set_notes(self, spec: collect.ReleaseSpec) -> None:
        self.notes[spec.tag] = spec


def entry(name: str = NAME, program_id: str = PROGRAM_ID) -> collect.RegistryEntry:
    return collect.RegistryEntry(name, program_id, "amm", "test")


class CollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = FakeSource()
        self.store = FakeStore()
        self.clock = "2026-01-01T00:00:00Z"
        self.collector = self.make_collector()

    def make_collector(self, **kwargs: object) -> collect.Collector:
        return collect.Collector(
            collect.Layout(self.root),
            self.source,
            self.store,
            release_tag=TAG,
            sleep=lambda _s: None,
            now=lambda: self.clock,
            **kwargs,  # type: ignore[arg-type]
        )

    def read_meta(self, *parts: str) -> dict[str, object]:
        return json.loads(self.root.joinpath(*parts, "metadata.json").read_text(encoding="utf-8"))

    def test_first_collection_publishes_release_and_writes_metadata_only(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        result = self.collector.collect_one(entry())
        self.assertEqual(result.outcome, collect.Outcome.NEW)
        self.assertEqual(self.store.published, {f"{NAME}-100.so": (TAG, elf(b"v1"))})
        meta = self.read_meta("dumps", NAME)
        self.assertEqual(meta["release_tag"], TAG)
        self.assertEqual(meta["download_url"], f"https://example.invalid/{TAG}/{NAME}-100.so")
        self.assertEqual((meta["last_deployed_slot"], meta["upgrade_authority"]), (100, "AUTH"))
        self.assertEqual(meta["collected_at"], self.clock)
        self.assertIsNone(meta["previous_slot"])
        self.assertEqual([p.name for p in (self.root / "dumps" / NAME).iterdir()], ["metadata.json"])
        self.assertFalse((self.root / "archives").exists())

    def test_padding_is_stripped_but_onchain_hash_is_kept(self) -> None:
        raw = elf(b"v1") + b"\x00" * 500
        self.source.deploy(PROGRAM_ID, 100, raw)
        self.collector.collect_one(entry())
        _, stored = self.store.published[f"{NAME}-100.so"]
        self.assertEqual(stored, elf(b"v1"))
        meta = self.read_meta("dumps", NAME)
        self.assertEqual((meta["size"], meta["sha256"]), (len(stored), hashlib.sha256(stored).hexdigest()))
        self.assertEqual((meta["onchain_size"], meta["onchain_sha256"]), (len(raw), hashlib.sha256(raw).hexdigest()))

    def test_same_slot_is_skipped_without_dumping_or_publishing(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        self.collector.collect_one(entry())
        self.source.dump_calls.clear()
        self.store.published.clear()
        result = self.collector.collect_one(entry())
        self.assertEqual(result.outcome, collect.Outcome.UNCHANGED)
        self.assertEqual((self.source.dump_calls, self.store.published), ([], {}))

    def test_verify_hash_dumps_but_does_not_republish(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        self.collector.collect_one(entry())
        self.source.dump_calls.clear()
        self.store.published.clear()
        result = self.make_collector(verify_hash=True).collect_one(entry())
        self.assertEqual(result.outcome, collect.Outcome.UNCHANGED)
        self.assertEqual((self.source.dump_calls, self.store.published), ([PROGRAM_ID], {}))

    def test_upgrade_archives_previous_metadata_by_slot(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        self.collector.collect_one(entry())
        self.clock = "2026-01-02T00:00:00Z"
        self.source.deploy(PROGRAM_ID, 250, elf(b"v2"))
        result = self.collector.collect_one(entry())
        self.assertEqual((result.outcome, result.asset), (collect.Outcome.UPDATED, f"{NAME}-250.so"))
        archived = self.read_meta("archives", NAME, "100")
        self.assertEqual((archived["last_deployed_slot"], archived["download_url"]), (100, f"https://example.invalid/{TAG}/{NAME}-100.so"))
        meta = self.read_meta("dumps", NAME)
        self.assertEqual((meta["last_deployed_slot"], meta["previous_slot"]), (250, 100))
        self.assertEqual(sorted(self.store.published), [f"{NAME}-100.so", f"{NAME}-250.so"])

    def test_slotless_program_uses_sha256_for_detection_and_names(self) -> None:
        self.source.deploy(PROGRAM_ID, None, elf(b"v1"))
        self.collector.collect_one(entry())
        self.assertEqual(self.collector.collect_one(entry()).outcome, collect.Outcome.UNCHANGED)
        old_sha = str(self.read_meta("dumps", NAME)["sha256"])
        self.source.deploy(PROGRAM_ID, None, elf(b"v2"))
        result = self.collector.collect_one(entry())
        self.assertEqual(result.outcome, collect.Outcome.UPDATED)
        new_sha = str(self.read_meta("dumps", NAME)["sha256"])
        self.assertEqual(result.asset, f"{NAME}-sha256-{new_sha[:12]}.so")
        self.assertTrue((self.root / "archives" / NAME / f"sha256-{old_sha[:12]}" / "metadata.json").is_file())

    def test_same_slot_with_different_bytes_gets_a_distinct_tag(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        self.collector.collect_one(entry())
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1-changed"))
        result = self.make_collector(verify_hash=True).collect_one(entry())
        self.assertEqual(result.outcome, collect.Outcome.UPDATED)
        self.assertNotEqual(result.asset, f"{NAME}-100.so")
        self.assertTrue(str(result.asset).startswith(f"{NAME}-100-"))

    def test_redeploy_during_dump_triggers_redump(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        moved = collect.ProgramInfo(PROGRAM_ID, LOADER_V3, "PD", 101, "AUTH")
        # first show() sees slot 100, the post-dump check already sees 101, then it is stable.
        self.source.show_queue[PROGRAM_ID] = [self.source.infos[PROGRAM_ID], moved, moved]
        result = self.collector.collect_one(entry())
        self.assertEqual((result.slot, len(self.source.dump_calls)), (101, 2))
        self.assertIn(f"{NAME}-101.so", self.store.published)

    def test_non_elf_dump_is_rejected(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, b"<html>rate limited</html>")
        with self.assertRaises(collect.CollectError):
            self.collector.collect_one(entry())
        self.assertEqual(self.store.published, {})
        self.assertFalse((self.root / "dumps").exists())

    def test_publish_failure_leaves_metadata_untouched(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        self.collector.collect_one(entry())
        before = self.read_meta("dumps", NAME)
        self.source.deploy(PROGRAM_ID, 250, elf(b"v2"))
        self.store.fail = True
        with self.assertRaises(collect.CollectError):
            self.collector.collect_one(entry())
        self.assertEqual(self.read_meta("dumps", NAME), before)
        self.assertFalse((self.root / "archives").exists())

    def test_one_failure_does_not_stop_the_run(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"a"))
        self.source.deploy(OTHER_ID, 7, elf(b"b"))
        self.source.failing.add(PROGRAM_ID)
        results = self.collector.run([entry(), entry("raydium-cpmm", OTHER_ID)])
        self.assertEqual([r.outcome for r in results], [collect.Outcome.FAILED, collect.Outcome.NEW])

    def test_metadata_for_other_program_is_not_overwritten(self) -> None:
        self.source.deploy(OTHER_ID, 7, elf(b"b"))
        self.collector.collect_one(entry("raydium-cpmm", OTHER_ID))
        with self.assertRaises(collect.CollectError):
            self.collector.collect_one(entry("raydium-cpmm", PROGRAM_ID))

    def test_failed_metadata_write_restores_archived_version(self) -> None:
        self.source.deploy(PROGRAM_ID, 100, elf(b"v1"))
        self.collector.collect_one(entry())
        self.source.deploy(PROGRAM_ID, 250, elf(b"v2"))
        dump_dir = self.root / "dumps" / NAME
        original = collect.Collector._archive_current

        def archive_then_block(collector: collect.Collector, name: str, prev: collect.Metadata) -> Path:
            dest = original(collector, name, prev)
            # A directory where the temporary metadata file must go makes the write fail.
            (dump_dir / "metadata.json.tmp").mkdir(parents=True)
            return dest

        collect.Collector._archive_current = archive_then_block  # type: ignore[method-assign]
        self.addCleanup(setattr, collect.Collector, "_archive_current", original)
        with self.assertRaises(collect.CollectError):
            self.collector.collect_one(entry())
        self.assertEqual(self.read_meta("dumps", NAME)["last_deployed_slot"], 100)
        self.assertFalse((self.root / "archives" / NAME / "100").exists())


class IndexTest(unittest.TestCase):
    def test_index_lists_latest_then_archived_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, store = FakeSource(), FakeStore()
            clock = ["2026-01-01T00:00:00Z"]
            collector = collect.Collector(
                collect.Layout(root), source, store, release_tag=TAG, sleep=lambda _s: None, now=lambda: clock[0]
            )
            for slot, day in ((100, "01"), (200, "02"), (300, "03")):
                clock[0] = f"2026-01-{day}T00:00:00Z"
                source.deploy(PROGRAM_ID, slot, elf(str(slot).encode()))
                collector.collect_one(entry())
            index = collect.build_index(collect.Layout(root), [entry(), entry("raydium-cpmm", OTHER_ID)])
            programs = index["programs"]
            assert isinstance(programs, list)
            self.assertEqual([v["last_deployed_slot"] for v in programs[0]["versions"]], [300, 200, 100])
            self.assertEqual(programs[0]["versions"][0]["download_url"], f"https://example.invalid/{TAG}/{NAME}-300.so")
            self.assertEqual(programs[1]["versions"], [])


class GhReleaseStoreTest(unittest.TestCase):
    REPO = "owner/repo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.asset = Path(self._tmp.name) / "prog-100.so"
        self.asset.write_bytes(b"12345")
        self.calls: list[list[str]] = []
        self.view_result = collect.CommandResult(1, "", "release not found\n")
        self.spec = collect.ReleaseSpec(tag="prog-100", title="prog @ 100", notes="notes")

    def runner(self, cmd: Sequence[str], timeout: int, label: str) -> collect.CommandResult:
        self.calls.append(list(cmd))
        if cmd[1:3] == ["release", "view"]:
            return self.view_result
        return collect.CommandResult(0, "", "")

    def store(self) -> collect.GhReleaseStore:
        return collect.GhReleaseStore(self.REPO, run=self.runner)

    def actions(self) -> list[str]:
        return [c[2] for c in self.calls]

    def test_missing_release_is_created_with_the_asset(self) -> None:
        url = self.store().publish(self.spec, self.asset)
        self.assertEqual(url, "https://github.com/owner/repo/releases/download/prog-100/prog-100.so")
        self.assertEqual(self.actions(), ["view", "create"])
        create = self.calls[1]
        self.assertIn(str(self.asset), create)
        self.assertEqual(create[create.index("--repo") + 1], self.REPO)

    def test_existing_release_with_same_asset_is_reused(self) -> None:
        self.view_result = collect.CommandResult(0, json.dumps({"assets": [{"name": "prog-100.so", "size": 5}]}), "")
        self.store().publish(self.spec, self.asset)
        self.assertEqual(self.actions(), ["view"])

    def test_existing_release_without_asset_gets_upload(self) -> None:
        self.view_result = collect.CommandResult(0, json.dumps({"assets": []}), "")
        self.store().publish(self.spec, self.asset)
        self.assertEqual(self.actions(), ["view", "upload"])

    def test_existing_asset_with_other_size_is_an_error(self) -> None:
        self.view_result = collect.CommandResult(0, json.dumps({"assets": [{"name": "prog-100.so", "size": 9}]}), "")
        with self.assertRaises(collect.CollectError):
            self.store().publish(self.spec, self.asset)

    def test_unexpected_gh_error_is_reported(self) -> None:
        self.view_result = collect.CommandResult(1, "", "HTTP 401: Bad credentials")
        with self.assertRaises(collect.CollectError):
            self.store().publish(self.spec, self.asset)
        self.assertEqual(self.actions(), ["view"])

    def test_set_notes_edits_the_release(self) -> None:
        self.store().set_notes(collect.ReleaseSpec("prog-100", "Snapshot x", "body"))
        edit = self.calls[0]
        self.assertEqual(edit[1:4], ["release", "edit", "prog-100"])
        self.assertEqual(edit[edit.index("--notes") + 1], "body")


class LocalStoreTest(unittest.TestCase):
    def test_copies_asset_and_returns_file_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "prog-1.so"
            asset.write_bytes(b"abc")
            url = collect.LocalStore(Path(tmp) / "out").publish(collect.ReleaseSpec("prog-1", "t", "n"), asset)
            self.assertTrue(url.startswith("file://"))
            self.assertEqual((Path(tmp) / "out" / "prog-1" / "prog-1.so").read_bytes(), b"abc")

    def test_set_notes_writes_a_notes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            collect.LocalStore(Path(tmp)).set_notes(collect.ReleaseSpec("snap", "Snapshot", "body"))
            self.assertIn("body", (Path(tmp) / "snap" / collect.LocalStore.NOTES_FILE).read_text(encoding="utf-8"))


class SnapshotNotesTest(unittest.TestCase):
    def test_notes_list_only_versions_of_that_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, store = FakeSource(), FakeStore()
            old = collect.Collector(collect.Layout(root), source, store, release_tag="snapshot-2025-12-31", sleep=lambda _s: None)
            source.deploy(PROGRAM_ID, 100, elf(b"v1"))
            old.collect_one(entry())
            new = collect.Collector(collect.Layout(root), source, store, release_tag=TAG, sleep=lambda _s: None)
            source.deploy(PROGRAM_ID, 250, elf(b"v2"))
            source.deploy(OTHER_ID, 7, elf(b"b"))
            new.run([entry(), entry("raydium-cpmm", OTHER_ID)])
            notes = collect.snapshot_notes(collect.Layout(root), [entry(), entry("raydium-cpmm", OTHER_ID)], TAG)
            self.assertIn("[!NOTE]", notes)
            self.assertIn("2 program version(s)", notes)
            self.assertIn(f"| {NAME} | `{PROGRAM_ID}` | 250 | 100 |", notes)
            self.assertIn("`raydium-cpmm-7.so`", notes)
            self.assertNotIn(f"`{NAME}-100.so`", notes)
            self.assertEqual(collect.snapshot_spec(TAG).title, "Snapshot 2026-01-01")


class ElfPaddingTest(unittest.TestCase):
    def test_section_table_ending_in_zeros_is_not_cut(self) -> None:
        # A section table whose only entry is all zeros ends in zero bytes, yet is part of the ELF.
        data = elf(b"\x01" * 10 + b"\x00" * 64, sh_off=74, sh_num=1) + b"\x00" * 1000
        self.assertEqual(collect.elf_extent(data), 74 + 64)
        self.assertEqual(len(collect.strip_padding(data)), 74 + 64)

    def test_non_zero_tail_beyond_headers_is_kept(self) -> None:
        data = elf(b"\x01" * 10) + b"\x00" * 20 + b"\x02"
        self.assertEqual(collect.strip_padding(data), data)

    def test_padding_free_binary_is_unchanged(self) -> None:
        data = elf(b"\x01" * 10)
        self.assertEqual(collect.strip_padding(data), data)

    def test_rejects_non_elf_and_truncated_headers(self) -> None:
        for bad in (b"MZ" + b"\x00" * 100, b"\x7fELF\x02\x01", elf(b"x", sh_off=1 << 40, sh_num=1)):
            with self.assertRaises(collect.CollectError):
                collect.strip_padding(bad)


class ParsingTest(unittest.TestCase):
    def test_show_upgradeable_program(self) -> None:
        text = json.dumps(
            {"programId": PROGRAM_ID, "owner": LOADER_V3, "programdataAddress": "PD", "authority": "AUTH", "lastDeploySlot": 5, "dataLen": 9}
        )
        info = collect.ProgramInfo.from_cli_json(text, PROGRAM_ID)
        self.assertEqual((info.last_deployed_slot, info.upgrade_authority, info.program_data_address), (5, "AUTH", "PD"))

    def test_show_non_upgradeable_program_has_no_slot(self) -> None:
        text = json.dumps({"programId": PROGRAM_ID, "owner": LOADER_V2, "dataLen": 9})
        info = collect.ProgramInfo.from_cli_json(text, PROGRAM_ID)
        self.assertIsNone(info.last_deployed_slot)
        self.assertIsNone(info.upgrade_authority)

    def test_show_immutable_program_maps_none_authority(self) -> None:
        text = json.dumps({"programId": PROGRAM_ID, "owner": LOADER_V3, "authority": "none", "lastDeploySlot": 5})
        self.assertIsNone(collect.ProgramInfo.from_cli_json(text, PROGRAM_ID).upgrade_authority)

    def test_show_rejects_mismatched_program(self) -> None:
        with self.assertRaises(collect.CollectError):
            collect.ProgramInfo.from_cli_json(json.dumps({"programId": OTHER_ID}), PROGRAM_ID)

    def test_registry_rejects_bad_names_and_duplicates(self) -> None:
        good = {"name": "ok-name", "program_id": PROGRAM_ID, "type": "amm", "description": "d"}
        for bad_name in ("../escape", "Upper", "a b", ""):
            with self.assertRaises(collect.CollectError, msg=bad_name):
                collect.RegistryEntry.from_json({**good, "name": bad_name})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text(json.dumps({"programs": [good, good]}), encoding="utf-8")
            with self.assertRaises(collect.CollectError):
                collect.load_registry(path)

    def test_shipped_registry_is_valid(self) -> None:
        entries = collect.load_registry(ROOT / "registry.json")
        self.assertGreaterEqual(len(entries), 11)

    def test_urls_are_redacted_from_cli_errors(self) -> None:
        text = "error sending request for url (https://rpc.example/?api-key=SECRET)"
        self.assertNotIn("SECRET", collect.redact_urls(text))

    def test_commit_message_lists_changes(self) -> None:
        results = [
            collect.Result("a", collect.Outcome.UPDATED, slot=2, previous_slot=1, asset="a-2.so"),
            collect.Result("b", collect.Outcome.NEW, slot=3, asset="b-3.so"),
            collect.Result("c", collect.Outcome.UNCHANGED, slot=4),
        ]
        message = collect.format_commit_message(results, TAG)
        assert message is not None
        self.assertTrue(message.startswith(f"chore(dumps): 1 upgraded, 1 new ({TAG})"))
        self.assertIn("- a: slot 1 -> 2\n", message)
        self.assertNotIn("- c:", message)
        self.assertIsNone(collect.format_commit_message(results[2:], TAG))

    def test_commit_message_for_slotless_program_uses_sha256(self) -> None:
        result = collect.Result("d", collect.Outcome.UPDATED, sha256="b" * 64, previous_sha256="a" * 64, asset="d-x.so")
        message = collect.format_commit_message([result], TAG)
        assert message is not None
        self.assertIn(f"- d: sha256 {'a' * 12} -> {'b' * 12}", message)


class RegistryLinksTest(unittest.TestCase):
    BASE = {"name": "my-dex", "program_id": PROGRAM_ID, "type": "amm", "description": "d"}

    def parse(self, **extra: object) -> collect.RegistryEntry:
        return collect.RegistryEntry.from_json({**self.BASE, **extra})

    def test_links_are_optional(self) -> None:
        entry_without = self.parse()
        self.assertEqual((entry_without.github, entry_without.website), (None, None))
        with_links = self.parse(github="https://github.com/owner/repo", website="https://example.org/")
        self.assertEqual((with_links.github, with_links.website), ("https://github.com/owner/repo", "https://example.org/"))

    def test_invalid_links_are_rejected(self) -> None:
        bad_github = ("https://gitlab.com/owner/repo", "http://github.com/owner/repo", "https://github.com/", "https://github.com/a b", "javascript:alert(1)", 5)
        for value in bad_github:
            with self.assertRaises(collect.CollectError, msg=str(value)):
                self.parse(github=value)
        bad_website = ("http://example.org", "javascript:alert(1)", "https://example", "https://exa mple.org", "https://example.org/a b", "example.org", 5)
        for value in bad_website:
            with self.assertRaises(collect.CollectError, msg=str(value)):
                self.parse(website=value)

    def test_github_links_may_point_to_orgs_repos_and_subfolders(self) -> None:
        for value in (
            "https://github.com/orca-so",
            "https://github.com/orca-so/whirlpools",
            "https://github.com/solana-labs/solana-program-library/tree/master/token-swap",
        ):
            self.assertEqual(self.parse(github=value).github, value)

    def test_link_cell_prefers_github_then_website(self) -> None:
        cell = collect.link_cell
        self.assertEqual(cell(self.parse(github="https://github.com/orca-so/whirlpools")), "[orca-so/whirlpools](https://github.com/orca-so/whirlpools)")
        self.assertEqual(cell(self.parse(github="https://github.com/orca-so")), "[orca-so](https://github.com/orca-so)")
        deep = "https://github.com/solana-labs/solana-program-library/tree/master/token-swap"
        self.assertEqual(cell(self.parse(github=deep)), f"[solana-labs/solana-program-library]({deep})")
        self.assertEqual(cell(self.parse(website="https://www.humidifi.xyz/")), "[humidifi.xyz](https://www.humidifi.xyz/)")
        both = self.parse(github="https://github.com/a/b", website="https://example.org/")
        self.assertTrue(cell(both).startswith("[a/b]"))
        self.assertEqual(cell(self.parse()), "")

    def test_table_has_link_column(self) -> None:
        table = collect.registry_table([self.parse(website="https://example.org/"), self.parse()])
        lines = table.splitlines()
        self.assertEqual(lines[0], "| Name | Program ID | Type | Description | Link |")
        self.assertTrue(lines[2].endswith("| [example.org](https://example.org/) |"))
        self.assertTrue(lines[3].endswith("| d |  |"))

    def test_index_carries_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = collect.build_index(
                collect.Layout(Path(tmp)), [self.parse(github="https://github.com/a/b"), self.parse(name="other", program_id=OTHER_ID)]
            )
        programs = index["programs"]
        assert isinstance(programs, list)
        self.assertEqual((programs[0]["github"], programs[0]["website"]), ("https://github.com/a/b", None))
        self.assertEqual((programs[1]["github"], programs[1]["website"]), (None, None))

    def test_shipped_registry_links_are_wellformed_and_exclusive_by_convention(self) -> None:
        entries = collect.load_registry(ROOT / "registry.json")
        self.assertTrue(all(not (e.github and e.website) for e in entries), "website is only a fallback for entries without GitHub")
        self.assertGreater(sum(bool(e.github or e.website) for e in entries), len(entries) // 2)


class DryRunGuardTest(unittest.TestCase):
    def test_dry_run_refuses_repo_and_non_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = collect.Layout(Path(tmp).resolve())
            (repo.root / "dumps").mkdir()
            (repo.root / "used").mkdir()
            (repo.root / "used" / "x").write_text("x", encoding="utf-8")
            for target in (repo.root, repo.root / "dumps" / "sub", repo.root / "used"):
                args = collect.parse_args(["--dry-run", str(target)])
                with self.assertRaises(collect.CollectError, msg=str(target)):
                    collect.prepare_workspace(repo, args)
            args = collect.parse_args(["--dry-run", str(repo.root / "fresh")])
            layout, store = collect.prepare_workspace(repo, args)
            self.assertEqual(layout.root, repo.root / "fresh")
            self.assertIsInstance(store, collect.LocalStore)
            self.assertTrue((repo.root / "fresh" / "dumps").is_dir())


if __name__ == "__main__":
    unittest.main()
