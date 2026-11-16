"""Deterministic J2.5 verdicts from worker observations and private test canaries."""


_KINDS = {"authorization", "injection", "browser_input", "disclosure"}


def verify_candidate(kind: str, observations: dict, *, scope: dict) -> dict:
    if kind not in _KINDS:
        raise ValueError("unsupported verification kind")
    result = {"kind": kind, "status": "partial", "information": [], "reason": "incomplete_observation"}
    if kind == "authorization" and "access_samples" in observations:
        rows = observations["access_samples"]
        if (not isinstance(rows, list) or len(rows) != 3 or
                [row.get("stage") if isinstance(row, dict) else None for row in rows] !=
                ["unauthorized", "owner", "recheck"]):
            return result
        unauthorized, owner, recheck = rows
        if (any(row.get("truncated") or row.get("binary") or not isinstance(row.get("status"), int)
                or not isinstance(row.get("body_sha256"), str) for row in rows)
                or (unauthorized["status"], unauthorized["body_sha256"]) !=
                   (recheck["status"], recheck["body_sha256"])):
            result["reason"] = "unstable_or_invalid_access_baseline"
            return result
        if not all(200 <= row["status"] < 300 for row in (unauthorized, owner)):
            result.update(status="attempted", reason="access_response_unavailable")
            return result
        known = scope.get("canaries") or {}
        hits = set(unauthorized.get("canary_hits") or []) & set(owner.get("canary_hits") or []) & set(recheck.get("canary_hits") or [])
        private = sorted(label for label in hits if label in known and known[label].get("protected")
            and observations.get("owner_identity") in known[label].get("authorized_identities", [])
            and observations.get("identity", "public") not in known[label].get("authorized_identities", []))
        if private and not observations.get("request_contains_canary"):
            result.update(status="independently_verified", information=private,
                          reason="unauthorized_identity_read_private_canary")
        else:
            result.update(status="observed_information" if hits else "attempted",
                          reason="cross_identity_security_proof_missing")
        return result
    method = observations.get("method", "GET")
    if method not in {"GET", "HEAD", "OPTIONS"} and not scope.get("mutation_allowed"):
        result.update(status="failed", reason="mutation_outside_test_scope")
        return result
    rows = observations.get("samples")
    if not isinstance(rows, list) or len(rows) != 3 or [row.get("stage") if isinstance(row, dict) else None
                                                        for row in rows] != ["normal", "test", "recheck"]:
        return result
    normal, test, recheck = rows
    if any(row.get("truncated") or row.get("binary") or not isinstance(row.get("status"), int)
           or not isinstance(row.get("body_sha256"), str) for row in rows):
        result["reason"] = "truncated_or_invalid_sample"
        return result
    if (normal["status"], normal["body_sha256"]) != (recheck["status"], recheck["body_sha256"]):
        result["reason"] = "unstable_baseline"
        return result
    if not 200 <= test["status"] < 300:
        result.update(status="attempted", reason="test_response_unavailable")
        return result
    if kind == "browser_input":
        proof = observations.get("execution_proof") or {}
        known = scope.get("canaries") or {}
        private = sorted(label for label in proof.get("canary_hits", []) if label in known
                         and known[label].get("protected"))
        if proof.get("executed") and not proof.get("recheck_executed") and private and not observations.get("test_contains_canary"):
            result.update(status="independently_verified", information=private,
                          reason="browser_code_read_private_canary")
            return result
    hits = set(test.get("canary_hits") or []) - set(normal.get("canary_hits") or []) - set(recheck.get("canary_hits") or [])
    known = scope.get("canaries") or {}
    result["information"] = sorted(hit for hit in hits if hit in known)
    if not hits:
        result.update(status="attempted", reason="no_information_proof")
        return result
    private = [label for label in result["information"] if known[label].get("protected")
               and observations.get("identity", "public") not in known[label].get("authorized_identities", [])]
    if kind == "authorization":
        private = []
    if observations.get("test_contains_canary"):
        private = []
    if kind == "browser_input":
        proof = observations.get("execution_proof") or {}
        if not proof.get("executed"):
            private = []
        else:
            private = [label for label in private if label in proof.get("canary_hits", [])]
    if private:
        result.update(status="independently_verified", information=sorted(private), reason="private_canary_observed")
    else:
        result.update(status="observed_information", reason="independent_security_proof_missing")
    return result
