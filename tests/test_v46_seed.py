"""Warm-start imports preserve eligible graph structure across journal replay."""

import tempfile
import unittest
from pathlib import Path

from engine.session_log import SessionLog
from engine.tree import TreeStore


def discovery(url, kind="surface", relation="discovered"):
    return {"kind": kind, "relation": relation,
            "locator": {"url": url, "method": "GET", "input": "q", "auth_context": "anonymous"},
            "hypothesis": {"type": "sqli", "statement": "candidate"} if kind == "candidate" else None}


class SeedTests(unittest.TestCase):
    def test_source_root_path_must_match_target_even_when_origin_is_same(self):
        from engine.seed import import_seed
        from engine.contracts import ContractError
        with tempfile.TemporaryDirectory() as folder:
            old = SessionLog(Path(folder), "a" * 32, {"experiment_mode": "v4"})
            new = SessionLog(Path(folder), "b" * 32, {"experiment_mode": "v4"})
            try:
                source, target = TreeStore(old), TreeStore(new)
                source.initialize("https://example.test/app-a")
                target.initialize("https://example.test/app-b")
                with self.assertRaises(ContractError):
                    import_seed(target, source.state, old.run_id)
            finally:
                old.close()
                new.close()

    def test_pending_or_stale_source_cannot_reuse_previous_investigation(self):
        from engine.seed import _reusable
        state = {"attempts": {"A0001": {"status": "imported", "execution_status": "succeeded",
                  "result": {"next_observation": ""}}}}
        node = {"id": "N0002", "attempts": ["A0001"], "investigation_state": "done",
                "seed_revalidation_pending": True, "seed_freshness": "unknown"}
        state["nodes"] = {"N0002": node}
        self.assertFalse(_reusable(state, node))
        node.update(seed_revalidation_pending=False, seed_freshness="stale")
        self.assertFalse(_reusable(state, node))
    def test_import_replays_graph_and_excludes_blocked_outside_nodes(self):
        from engine.seed import import_seed
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = SessionLog(root, "a" * 32, {"experiment_mode": "v4"})
            try:
                source = TreeStore(old)
                source.initialize("https://example.test/")
                surface = source.discover("N0001", discovery("https://example.test/search"))
                candidate = source.discover(surface, discovery("https://example.test/item", "candidate", "refines"))
                source.discover("N0001", discovery("https://example.test/item", "candidate"))
                external = source.discover("N0001", discovery("https://outside.test/"))
                source.set_node_state(external, "blocked")
                state = source.state
            finally:
                old.close()
            new = SessionLog(root, "b" * 32, {"experiment_mode": "v4"})
            try:
                target = TreeStore(new)
                target.initialize("https://example.test/")
                result = import_seed(target, state, "a" * 32)
                rebuilt = target.rebuild()
                node = rebuilt["nodes"][candidate]
                self.assertEqual(result["imported_nodes"], 2)
                self.assertEqual(result["excluded_nodes"], 1)
                self.assertEqual(node["carried_from"], {"run_id": "a" * 32, "orig_node_id": candidate})
                self.assertEqual(node["discovery_paths"], [{"parent_id": surface, "relation": "refines"},
                                                          {"parent_id": "N0001", "relation": "discovered"}])
                self.assertEqual(node["verification_state"], "untested")
                self.assertEqual(node["attempts"], [])
                self.assertEqual(node["seed_freshness"], "unknown")
            finally:
                new.close()

    def test_blocked_parent_excludes_same_origin_descendants(self):
        from engine.seed import import_seed
        with tempfile.TemporaryDirectory() as folder:
            old = SessionLog(Path(folder), "a" * 32, {"experiment_mode": "v4"})
            try:
                source = TreeStore(old)
                source.initialize("https://example.test/")
                parent = source.discover("N0001", discovery("https://example.test/a"))
                source.discover(parent, discovery("https://example.test/b", "candidate"))
                source.set_node_state(parent, "blocked")
                state = source.state
            finally:
                old.close()
            new = SessionLog(Path(folder), "b" * 32, {"experiment_mode": "v4"})
            try:
                tree = TreeStore(new)
                tree.initialize("https://example.test/")
                result = import_seed(tree, state, "a" * 32)
                self.assertEqual(result["excluded_nodes"], 2)
                self.assertEqual(result["excluded_by_reason"], {"blocked": 1, "parent": 1})
                self.assertEqual(result["excluded_node_details"], [
                    {"source_node_id": parent, "reason": "blocked"},
                    {"source_node_id": "N0003", "reason": "parent"}])
                self.assertEqual(len(tree.rebuild()["nodes"]), 1)
            finally:
                new.close()


if __name__ == "__main__":
    unittest.main()
