import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.session_log import SessionStore
from engine.tree import TreeStore
from engine.seed_source import resolve_seed
from orchestrator import ExplorerV3


class SeedSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name))

    def source(self, run_id, target="https://example.test/", **metadata):
        log = self.store.create(run_id, {"experiment_mode": "v4", "target": target,
            "tree_schema_version": 1, **metadata})
        TreeStore(log).initialize(target)
        log.update(phase="finished", log_status="finalized", cleanup_status="succeeded",
                   lab_reset_status="not_applicable")
        log.close()
        return run_id

    def test_latest_selects_matching_finalized_source_and_replays(self):
        chosen = self.source("a" * 32)
        self.source("b" * 32, target="https://elsewhere.test/")
        source = resolve_seed(self.store, "latest", "https://example.test/")
        self.assertEqual(source["run_id"], chosen)
        self.assertEqual(source["state"]["root_id"], "N0001")
        self.assertEqual(source["last_event_seq"], 1)

    def test_invalid_source_or_corrupt_journal_is_rejected(self):
        run_id = self.source("c" * 32)
        with self.assertRaises(ValueError):
            resolve_seed(self.store, run_id, "https://other.test/")
        path = self.store.root / run_id / "events.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"run_id": run_id, "seq": 3, "kind": "bad"}) + "\n")
        with self.assertRaises((ValueError, OSError)):
            resolve_seed(self.store, run_id, "https://example.test/")

    def test_latest_skips_corrupt_journals_without_modifying_them(self):
        healthy = self.source("8" * 32, created_at=1)
        corrupt = self.source("9" * 32, created_at=2)
        path = self.store.root / corrupt / "events.jsonl"
        original = path.read_bytes()
        bad_sequence = json.dumps({"run_id": corrupt, "seq": 3, "kind": "bad"}).encode() + b"\n"
        for damaged in (b'{"truncated":', b"\xff\n", original + bad_sequence):
            with self.subTest(journal=damaged):
                path.write_bytes(damaged)
                source = resolve_seed(self.store, "latest", "https://example.test/")
                self.assertEqual(source["run_id"], healthy)
                self.assertEqual(source["state"]["root_id"], "N0001")
                with self.assertRaisesRegex(ValueError, "journal is invalid"):
                    resolve_seed(self.store, corrupt, "https://example.test/")
                self.assertEqual(path.read_bytes(), damaged)

    def test_latest_with_no_readable_source_reports_no_compatible_source(self):
        run_id = self.source("7" * 32)
        path = self.store.root / run_id / "events.jsonl"
        path.unlink()
        with self.assertRaisesRegex(ValueError, "No finalized compatible seed source"):
            resolve_seed(self.store, "latest", "https://example.test/")
        with self.assertRaisesRegex(ValueError, "journal is invalid"):
            resolve_seed(self.store, run_id, "https://example.test/")
        self.assertFalse(path.exists())

    def test_unfinished_source_is_rejected(self):
        run_id = self.source("d" * 32)
        metadata = self.store.read(run_id)
        metadata["phase"] = "running"
        (self.store.root / run_id / "session.json").write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ValueError):
            resolve_seed(self.store, run_id, "https://example.test/")

    def test_same_origin_different_root_target_is_rejected(self):
        run_id = self.source("d" * 32, target="https://example.test/app-a")
        with self.assertRaises(ValueError):
            resolve_seed(self.store, run_id, "https://example.test/app-b")

    def test_seeded_execution_imports_before_worker_starts(self):
        source = self.source("e" * 32)
        explorer = ExplorerV3(self.store.root, browser_enabled=False)
        self.addCleanup(explorer.lease.release)
        seen = []
        def inspect_worker(worker):
            run_id = worker._args[0]
            events = self.store.read_events(run_id)
            seen.extend(event["kind"] for event in events)
        with patch("threading.Thread.start", autospec=True, side_effect=inspect_worker):
            run_id = explorer.start(target="https://example.test/", model="mock:model", goal="inspect",
                experiment_mode="v4", scorer_provider="openrouter", scorer_model="typesafe/jev-1.14",
                seed_settings={"start_mode": "seeded", "seed_from": source})
        self.assertIn("seed_completed", seen)
        self.assertEqual(self.store.read(run_id)["seed_source"]["run_id"], source)

    def test_seed_failure_does_not_start_worker(self):
        source = self.source("f" * 32)
        explorer = ExplorerV3(self.store.root, browser_enabled=False)
        self.addCleanup(explorer.lease.release)
        with patch("engine.seed.import_seed", side_effect=ValueError("invalid seed")), \
             patch("threading.Thread.start") as start:
            with self.assertRaisesRegex(ValueError, "invalid seed"):
                explorer.start(target="https://example.test/", model="mock:model", goal="inspect",
                    experiment_mode="v4", scorer_provider="openrouter", scorer_model="typesafe/jev-1.14",
                    seed_settings={"start_mode": "seeded", "seed_from": source})
            start.assert_not_called()
        self.assertTrue(explorer.can_start()[0])
        failed = next(item for item in self.store.list_runs() if item["run_id"] != source)
        self.assertEqual(failed["run_result"], "error")

    def test_crash_during_seed_recovery_cannot_become_a_source(self):
        source = self.source("1" * 32)
        explorer = ExplorerV3(self.store.root, browser_enabled=False)
        crashed_logs = []
        def crash_after_partial_import(tree, _state, _source):
            tree.discover("N0001", {"kind": "surface", "relation": "discovered", "hypothesis": None,
                "locator": {"url": "https://example.test/partial", "method": "GET", "input": "q", "auth_context": "anonymous"}})
            crashed_logs.append(tree.log)
            raise SystemExit("simulated process termination")
        try:
            with patch("engine.seed.import_seed", side_effect=crash_after_partial_import):
                with self.assertRaises(SystemExit):
                    explorer.start(target="https://example.test/", model="mock:model", goal="inspect",
                        experiment_mode="v4", scorer_provider="openrouter", scorer_model="typesafe/jev-1.14",
                        seed_settings={"start_mode": "seeded", "seed_from": source})
        finally:
            for log in crashed_logs:
                log.close()
            explorer.lease.release()
        run_id = crashed_logs[0].run_id
        recovered = ExplorerV3(self.store.root, browser_enabled=False)
        self.addCleanup(recovered.lease.release)
        self.assertTrue(recovered.recover(run_id))
        recovered.worker.join(10)
        self.assertFalse(recovered.worker.is_alive())
        metadata = self.store.read(run_id)
        self.assertEqual(metadata["phase"], "finished")
        self.assertEqual(metadata["seed_import_status"], "incomplete")
        self.assertEqual(metadata["run_result"], "incomplete")
        self.assertEqual(metadata["finish_reason"], "seed_import_incomplete")
        with self.assertRaisesRegex(ValueError, "seed import"):
            resolve_seed(self.store, run_id, "https://example.test/")
        self.assertEqual(resolve_seed(self.store, "latest", "https://example.test/")["run_id"], source)
        report = json.loads((self.store.root / run_id / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["run_result"], "incomplete")

    def test_seeded_source_requires_complete_pair_but_accepts_normal_import(self):
        from engine.session_log import SessionLog
        incomplete = self.source("2" * 32, seed_settings={"start_mode": "seeded"})
        with self.assertRaises(ValueError):
            resolve_seed(self.store, incomplete, "https://example.test/")
        log = SessionLog(self.store.root, incomplete, recovery=True)
        try:
            log.append("seed_started", {"source": {"run_id": "3" * 32, "last_event_seq": 1}})
            log.append("seed_completed", {"counts": {"imported_nodes": 0}})
        finally:
            log.close()
        self.assertEqual(resolve_seed(self.store, incomplete, "https://example.test/")["run_id"], incomplete)

    def test_seed_import_markers_must_form_one_ordered_successful_transaction(self):
        from engine.seed_source import seed_import_status
        metadata = {"seed_settings": {"start_mode": "seeded"}}
        start = {"seq": 1, "kind": "seed_started"}
        complete = {"seq": 2, "kind": "seed_completed"}
        self.assertEqual(seed_import_status({}, []), "fresh")
        self.assertEqual(seed_import_status(metadata, [start, complete]), "complete")
        for events in ([], [start], [complete], [complete, {**start, "seq": 3}],
                       [start, {**start, "seq": 2}, {**complete, "seq": 3}]):
            self.assertEqual(seed_import_status(metadata, events), "incomplete")
        self.assertEqual(seed_import_status(metadata, [start, complete, {"seq": 3, "kind": "seed_failed"}]), "failed")
