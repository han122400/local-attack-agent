import unittest

from strategies.greedy import seed_dispatchable, needs_seed_prerequisite_observation


def node(node_id, parent, relation, *, freshness="unknown", url="https://example.test/a", seeded=True):
    return {"id": node_id, "parent_id": parent, "seeded": seeded, "seed_freshness": freshness,
            "locator": {"url": url},
            "discovery_paths": [] if parent is None else [{"parent_id": parent, "relation": relation}]}


class SeedSchedulingTests(unittest.TestCase):
    def test_reused_parent_with_enabled_child_requires_new_observation(self):
        parent = node("N0001", None, None)
        parent.update(investigation_state="reused", exploration_state="discovered", child_ids=["N0002"])
        child = node("N0002", "N0001", "enabled_by")
        state = {"nodes": {"N0001": parent, "N0002": child}}
        self.assertTrue(needs_seed_prerequisite_observation(state, "N0001"))
        child["seed_enabled_by_verified"] = ["N0001"]
        self.assertFalse(needs_seed_prerequisite_observation(state, "N0001"))

    def test_reused_alternate_parent_requires_current_observation(self):
        root = node("N0001", None, None)
        primary = node("N0002", "N0001", "discovered")
        alternate = node("N0003", "N0001", "discovered")
        alternate.update(investigation_state="reused", exploration_state="discovered", child_ids=[])
        child = node("N0004", "N0002", "enabled_by")
        child.update(exploration_state="discovered")
        child["discovery_paths"].append({"parent_id": "N0003", "relation": "enabled_by"})
        state = {"nodes": {n["id"]: n for n in (root, primary, alternate, child)}}
        self.assertTrue(needs_seed_prerequisite_observation(state, "N0003"))
        child["seed_enabled_by_verified"] = {"N0003": {"source": "operator_reviewed"}}
        self.assertFalse(needs_seed_prerequisite_observation(state, "N0003"))

    def test_unknown_enabled_by_parent_cannot_dispatch(self):
        state = {"nodes": {"N0001": node("N0001", None, None),
                           "N0002": node("N0002", "N0001", "discovered"),
                           "N0003": node("N0003", "N0002", "enabled_by")}}
        self.assertFalse(seed_dispatchable(state, "N0003", "https://example.test/"))
        state["nodes"]["N0002"]["seed_freshness"] = "checked"
        state["nodes"]["N0002"]["seed_checked_fields"] = ["response_status"]
        self.assertFalse(seed_dispatchable(state, "N0003", "https://example.test/"))
        state["nodes"]["N0003"]["seed_enabled_by_verified"] = ["N0002"]
        self.assertTrue(seed_dispatchable(state, "N0003", "https://example.test/"))

    def test_alternate_discovery_does_not_bypass_unknown_dependency(self):
        state = {"nodes": {"N0001": node("N0001", None, None),
                           "N0002": node("N0002", "N0001", "discovered"),
                           "N0003": node("N0003", "N0002", "enabled_by")}}
        state["nodes"]["N0003"]["discovery_paths"].append({"parent_id": "N0001", "relation": "discovered"})
        self.assertFalse(seed_dispatchable(state, "N0003", "https://example.test/"))

    def test_independently_verified_alternate_enabled_path_can_dispatch(self):
        state = {"nodes": {"N0001": node("N0001", None, None),
                           "N0002": node("N0002", "N0001", "discovered"),
                           "N0003": node("N0003", "N0002", "enabled_by")}}
        state["nodes"]["N0003"]["discovery_paths"].append({"parent_id": "N0001", "relation": "enabled_by"})
        state["nodes"]["N0003"]["seed_enabled_by_verified"] = {"N0001": {"source": "operator_reviewed"}}
        self.assertTrue(seed_dispatchable(state, "N0003", "https://example.test/"))
        state["nodes"]["N0001"]["exploration_state"] = "blocked"
        self.assertFalse(seed_dispatchable(state, "N0003", "https://example.test/"))

    def test_seeded_node_scope_checked_before_dispatch(self):
        state = {"nodes": {"N0001": node("N0001", None, None),
                           "N0002": node("N0002", "N0001", "discovered", url="https://other.test/")}}
        self.assertFalse(seed_dispatchable(state, "N0002", "https://example.test/"))

    def test_nonseeded_nodes_are_not_given_seed_dependency_rules(self):
        state = {"nodes": {"N0001": node("N0001", None, None),
                           "N0002": node("N0002", "N0001", "enabled_by", seeded=False)}}
        self.assertTrue(seed_dispatchable(state, "N0002", "https://example.test/"))


if __name__ == "__main__":
    unittest.main()
