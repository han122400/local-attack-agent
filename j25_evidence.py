"""Deduplicate J2.5 verification observations without model-authored success claims."""

import hashlib
import json
import re
from urllib.parse import urlsplit


_LEVELS = ("candidate", "failed", "partial", "attempted", "observed_information", "independently_verified")
_ACTIONS = {"compare", "verify", "verify_access", "verify_browser"}


def _safe_surface(reference):
    url = reference.get("url", "")
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
        return (parts.path or "/")[:200] + ("#" + parts.fragment.partition("?")[0][:120]
                                            if parts.fragment.startswith("/") else "")
    except ValueError:
        return ""


def reduce_j25_evidence(steps: list[dict], references: dict) -> dict:
    """Return a stable, redacted case ledger; an unregistered denominator stays unknown."""
    visible = {item.get("id"): item for items in references.values() if isinstance(items, list)
               for item in items if isinstance(item, dict)}
    cases = {}
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in _ACTIONS:
            continue
        ref = step.get("reference") or visible.get(step.get("ref")) or {}
        if not isinstance(ref, dict):
            ref = {}
        verdict = step.get("verification") or {}
        if not isinstance(verdict, dict):
            verdict = {}
        kind = verdict.get("kind") or step.get("verification_kind") or "unclassified"
        if kind not in {"authorization", "injection", "browser_input", "disclosure"}:
            kind = "unclassified"
        method = ref.get("method") if ref.get("method") in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "GET"
        surface = _safe_surface(ref)
        field = str(ref.get("field") or "")[:100]
        identity = str(step.get("identity") or "public")[:40]
        owner = str(step.get("owner_identity") or "")[:40] if step.get("action") == "verify_access" else ""
        # shortcut: one endpoint may hide distinct flaws; split only with independent root-cause evidence.
        key = {"surface": surface, "field": field, "method": method}
        case_id = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
        case = cases.setdefault(case_id, {"id": case_id, **key, "identity": identity,
            "owner_identity": owner, "kind": kind, "status": "candidate", "information": [],
            "proofs": [], "missing_checks": [], "exec_ids": [], "step_numbers": [], "reference_ids": []})
        case["exec_ids"].append(str(step.get("exec_id") or "")[:80])
        case["step_numbers"].append(step.get("step"))
        if isinstance(step.get("ref"), str) and step["ref"] not in case["reference_ids"]:
            case["reference_ids"].append(step["ref"][:80])
        status = verdict.get("status")
        if status not in _LEVELS:
            status = "partial" if step.get("status") == "incomplete" else "candidate"
        labels = [label for label in verdict.get("information", []) if isinstance(label, str)
                  and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", label)] if isinstance(verdict.get("information"), list) else []
        if status == "independently_verified" and not labels:
            status = "partial"
        if _LEVELS.index(status) > _LEVELS.index(case["status"]):
            case["status"] = status
            case.update(kind=kind, identity=identity, owner_identity=owner)
        for label in labels:
            if label not in case["information"]:
                case["information"].append(label)
        if status == "independently_verified":
            rows = (step.get("access_samples") if step.get("action") == "verify_access" else
                    (step.get("comparison") or {}).get("samples")) or []
            stage = "unauthorized" if step.get("action") == "verify_access" else "test"
            sample = next((row for row in rows if isinstance(row, dict) and row.get("stage") == stage), {})
            digest = sample.get("body_sha256", "")
            case["proofs"].append({"exec_id": str(step.get("exec_id") or "")[:80],
                "body_sha256": digest if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{1,64}", digest) else "",
                "information": labels})
        else:
            reason = verdict.get("reason")
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,80}", reason):
                reason = "verification_incomplete"
            if reason not in case["missing_checks"]:
                case["missing_checks"].append(reason)
    for case in cases.values():
        if case["status"] == "independently_verified":
            case["missing_checks"] = []
        case["information"].sort()
    counts = {status: sum(case["status"] == status for case in cases.values()) for status in _LEVELS}
    return {"cases": list(cases.values()), "counts": counts, "case_count": len(cases),
            "denominator": None, "rate": None}
