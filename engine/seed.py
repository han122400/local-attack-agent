"""Copy an already replayed source tree into a fresh execution journal."""
from urllib.parse import urlsplit
from collections import deque

from engine.contracts import ContractError, dedup_key, normalize_url, validate_discovery
from engine.tree import latest_observation


def _origin(url):
    parsed = urlsplit(normalize_url(url))
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _reusable(state, node):
    previous = latest_observation(state, node["id"])
    return (not node.get("seed_revalidation_pending") and node.get("seed_freshness") != "stale"
            and node["investigation_state"] == "done" and previous is not None
            and not previous["next_observation"].strip())


def import_seed(tree, source_state, source_run_id):
    """Import the primary tree, then alternate paths; preserve provenance in events.

    The caller resolves a finalized source and replays its authoritative journal.
    Source root origin must match the initialized target root.
    """
    state = tree.refresh()
    if len(state["nodes"]) != 1 or state["root_id"] is None:
        raise ContractError("Seed target must be a fresh initialized tree")
    old_nodes = source_state["nodes"]
    old_root = source_state["root_id"]
    new_root = state["root_id"]
    if (source_state["run_id"] != source_run_id or normalize_url(old_nodes[old_root]["locator"]["url"])
            != normalize_url(state["nodes"][new_root]["locator"]["url"])):
        raise ContractError("Seed target or run identity mismatch")
    origin = _origin(state["nodes"][new_root]["locator"]["url"])
    mapping = {old_root: new_root}
    excluded = set()
    excluded_by_reason = {}
    excluded_node_details = []
    excluded_path_details = []
    ordered = []
    seen = {old_root}
    queue = deque(old_nodes[old_root]["child_ids"])
    while queue:
        old_id = queue.popleft()
        if old_id in seen or old_id not in old_nodes:
            raise ContractError("Invalid primary seed tree")
        seen.add(old_id)
        node = old_nodes[old_id]
        parent = node["parent_id"]
        if parent not in seen:
            raise ContractError("Invalid seed parent order")
        queue.extend(node["child_ids"])
        reason = ("parent" if parent in excluded else "blocked" if node["exploration_state"] == "blocked"
                  else "origin" if _origin(node["locator"]["url"]) != origin else None)
        if reason:
            excluded.add(old_id)
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
            excluded_node_details.append({"source_node_id": old_id, "reason": reason})
            excluded_path_details.extend({"source_node_id": old_id, "source_parent_id": path["parent_id"],
                                          "relation": path["relation"], "reason": "node_excluded"}
                                         for path in node["discovery_paths"])
            continue
        paths = node["discovery_paths"]
        primary = next((path for path in paths if path["parent_id"] == parent), None)
        if primary is None:
            raise ContractError("Missing primary discovery path")
        data = validate_discovery({"kind": node["kind"], "relation": primary["relation"],
                                   "locator": node["locator"], "hypothesis": node["hypothesis"],
                                   "observations": node["observations"]})
        if dedup_key(data) != node["dedup_key"]:
            raise ContractError("Seed dedup identity mismatch")
        ordered.append((old_id, node, data, primary))
    if seen != set(old_nodes):
        raise ContractError("Seed tree has unreachable nodes")
    tree.mark_seeded(new_root, source_run_id, old_root, old_nodes[old_root]["observations"],
                     reused=bool(ordered) and _reusable(source_state, old_nodes[old_root]))
    for old_id, node, data, primary in ordered:
        new_id = tree.seed_discover(mapping[node["parent_id"]], data)
        if new_id in mapping.values():
            raise ContractError("Seed nodes share a dedup identity")
        mapping[old_id] = new_id
        tree.mark_seeded(new_id, source_run_id, old_id, node["observations"],
                         reused=node["kind"] == "surface" and _reusable(source_state, node))
    added = 0
    excluded_paths = 0
    for old_id, node, data, primary in ordered:
        for path in node["discovery_paths"]:
            if path == primary:
                continue
            if path["parent_id"] not in mapping:
                excluded_paths += 1
                excluded_path_details.append({"source_node_id": old_id, "source_parent_id": path["parent_id"],
                                              "relation": path["relation"], "reason": "parent_excluded"})
                continue
            tree.seed_discover(mapping[path["parent_id"]], {**data, "relation": path["relation"]})
            added += 1
    return {"imported_nodes": len(ordered), "excluded_nodes": len(excluded), "excluded_by_reason": excluded_by_reason,
            "excluded_node_details": excluded_node_details, "excluded_path_details": excluded_path_details,
            "imported_paths": added, "excluded_paths": len(excluded_path_details)}
