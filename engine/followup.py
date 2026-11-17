"""Bounded follow-up candidates from measured surface observations.

These are leads requiring a new candidate investigation, never verdicts. The
caller validates/captures result evidence and invokes this only for a successful
execution, before committing the result event. Historical events are not changed.
"""

import copy

from engine.contracts import dedup_key


_HYPOTHESES = {
    "auth_bypass": ("auth", "Verify whether the observed authentication result bypasses the expected access boundary using a control and repeat test."),
    "weak_credentials": ("auth", "Verify whether weak or default credentials grant unintended access, and establish the resulting account privileges."),
    "account_enumeration": ("auth", "Verify whether controlled account-identifier comparisons reproducibly reveal another account's existence."),
    "query_injection": ("sqli", "Verify whether the observed query-specific anomaly permits controlled query manipulation with meaningful impact."),
    "unescaped_reflection": ("xss", "Verify whether the observed unescaped reflection is executable in its actual output context."),
    "cross_user_access": ("idor", "Verify whether another user's private data is accessible without the required ownership or authorization."),
}


def followup_candidates(node: dict, result: dict) -> list[dict]:
    """Return at most one missing candidate for a validated current surface.

    An explicit signal and corroborating categorical observations are required.
    Neither a model's ``confirmed`` claim nor a generic HTTP error is sufficient.
    The original files stay local; evidence_refs bind the lead to this result's
    captured evidence. Tree discovery handles deduplication across prior attempts.
    """
    facts = result.get("observations", {})
    refs = result.get("evidence_refs", [])
    if (node.get("kind") != "surface" or not refs
            or facts.get("baseline_compared") is not True
            or facts.get("input_control") not in ("full", "partial")):
        return []
    signal = facts.get("security_signal")
    if signal not in _HYPOTHESES:
        return []
    difference = facts.get("query_behavior") == "controlled_difference"
    private = facts.get("data_exposure") in ("private", "sensitive")
    cross_user = facts.get("object_scope") == "cross_user"
    if signal in ("auth_bypass", "weak_credentials"):
        status = facts.get("response_status")
        supported = difference and private and type(status) is int and 200 <= status < 400
    elif signal == "account_enumeration":
        supported = difference and cross_user
    elif signal == "query_injection":
        supported = facts.get("query_behavior") in ("error", "controlled_difference")
    elif signal == "unescaped_reflection":
        supported = facts.get("reflection") == "unescaped"
    else:  # cross_user_access
        supported = cross_user and private and difference
    if not supported:
        return []
    category, statement = _HYPOTHESES[signal]
    candidate = {"kind": "candidate", "relation": "refines", "locator": copy.deepcopy(node["locator"]),
                 "hypothesis": {"type": category, "statement": statement},
                 "observations": copy.deepcopy(facts), "evidence_refs": copy.deepcopy(refs)}
    identity = dedup_key(candidate)
    if any(item.get("kind") == "candidate" and dedup_key(item) == identity
           for item in result.get("discovered", [])):
        return []
    return [candidate]
