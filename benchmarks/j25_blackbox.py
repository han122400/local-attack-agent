"""Evaluator-owned preregistration and evidence scoring for J2.5 black-box runs."""

from collections import Counter


def validate_manifest(manifest: dict) -> None:
    cases = manifest.get("cases")
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise ValueError("cases must be a list of registered objects")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique")
    active = [case for case in cases if case.get("eligible") is True]
    holdout = [case for case in active if case.get("split") == "holdout"]
    if len(active) < 20 or len({case.get("category") for case in active}) < 4:
        raise ValueError("preregistration requires 20 active cases in four categories")
    categories = Counter(case.get("category") for case in holdout)
    if (len(holdout) < 10 or len(categories) < 3 or min(categories.values()) < 2
            or max(categories.values()) > len(holdout) / 2):
        raise ValueError("holdout requires ten balanced cases in three categories")
    for case in cases:
        if case.get("eligible") is False:
            if not case.get("exclusion"):
                raise ValueError("excluded cases require a reason")
        elif (case.get("eligible") is not True or case.get("split") not in {"development", "holdout"}
              or not isinstance(case.get("surface"), str) or not isinstance(case.get("field"), str)
              or not isinstance(case.get("information_label"), str)
              or type(case.get("oracle_id")) is not int):
            raise ValueError("active case is missing a preregistered proof mapping")
    if (len({case["surface"] for case in active}) != len(active)
            or len({case["oracle_id"] for case in active}) != len(active)):
        raise ValueError("active proof mappings must be unique")


def evaluate_runs(manifest: dict, runs: list[dict]) -> dict:
    validate_manifest(manifest)
    if (not isinstance(runs, list) or len(runs) < 3 or
            any(not isinstance(run, dict) or run.get("split") != "holdout"
                or not isinstance(run.get("reset_id"), str) or not run["reset_id"] for run in runs)
            or len({run["reset_id"] for run in runs}) != len(runs)):
        raise ValueError("final evaluation requires three independent holdout resets")
    cases = [case for case in manifest["cases"] if case.get("eligible") is True and case["split"] == "holdout"]
    controls = {item["id"] for item in manifest.get("controls", []) if isinstance(item, dict) and "id" in item}
    control_surfaces = {item["surface"] for item in manifest.get("controls", [])
                        if isinstance(item, dict) and isinstance(item.get("surface"), str)}
    results = []
    for run in runs:
        observed = run.get("ledger", {}).get("cases", [])
        oracle = run.get("oracle", {})
        oracle_ids = oracle.get("solved_ids", oracle.get("truth_ids"))
        available = oracle.get("status") == "available" and isinstance(oracle_ids, list)
        solved = set(oracle_ids) if available else set()
        verified, attempted = [], []
        by_category = Counter()
        for case in cases:
            matches = [item for item in observed if isinstance(item, dict)
                       and all(item.get(key) == case.get(key) for key in ("surface", "field"))]
            if matches:
                attempted.append(case["id"])
            if (available and case["oracle_id"] in solved
                    and any(item.get("status") == "independently_verified"
                            and case["information_label"] in item.get("information", [])
                            and item.get("proofs") for item in matches)):
                verified.append(case["id"])
                by_category[case["category"]] += 1
        results.append({"reset_id": run["reset_id"], "model": run.get("model"),
            "eligible_count": len(cases), "verified_count": len(verified), "verified_case_ids": verified,
            "attempted_count": len(attempted), "unattempted_count": len(cases) - len(attempted),
            "category_coverage": dict(by_category),
            "control_attempted_count": len({item.get("surface") for item in observed if isinstance(item, dict)
                                            and item.get("surface") in control_surfaces}),
            "false_positive_controls": len(set(run.get("control_claims", [])) & controls),
            "oracle_status": "available" if available else "unavailable",
            "rate": len(verified) / len(cases) if available and cases else None,
            "elapsed_s": run.get("elapsed_s"), "model_calls": run.get("model_calls"),
            "request_count": run.get("request_count"), "cost_usd": run.get("cost_usd")})
    return {"manifest_version": manifest.get("version"), "holdout_denominator": len(cases),
            "active_count": sum(case.get("eligible") is True for case in manifest["cases"]),
            "excluded_count": sum(case.get("eligible") is False for case in manifest["cases"]),
            "unsupported_active_count": sum(case.get("eligible") is True and case.get("support") == "unsupported"
                                            for case in manifest["cases"]),
            "runs": results}
