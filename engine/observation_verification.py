"""Deterministic support decisions for harness-recorded HTTP observations."""

from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

POLICY = "supported_observations_only"
SHAPE_POLICY = "supported_observations_only_shape_v2"
POLICIES = (POLICY, SHAPE_POLICY)


def _matches(facts, locator):
    actual, expected = urlsplit(facts["url"]), urlsplit(locator["url"])
    query = parse_qsl(actual.query, keep_blank_values=True)
    return ((actual.scheme, actual.netloc, actual.path) == (expected.scheme, expected.netloc, expected.path)
            and facts["method"] == locator["method"] and facts["auth_context"] == locator["auth_context"]
            and all(any(k == name and (not value or v == value) for k, v in query)
                    for name, value in parse_qsl(expected.query, keep_blank_values=True)))


def _comparison(proof, records, locator, *, policy=POLICY):
    baseline, tests = proof.get("baseline", []), proof.get("test", [])
    if len(baseline) != 2 or len(tests) != 2 or len(set(baseline + tests)) != 4:
        return None
    facts = [records[key]["facts"] for key in baseline + tests]
    # V1 compares only array lengths. V2 may also compare bounded object/type
    # signatures; both use the same four-request controlled protocol.
    pointer = proof.get("json_pointer")
    if (not isinstance(pointer, str)
            or any(f["method"] != "GET" or not 200 <= f["status"] < 300 for f in facts)
            or not locator["input"]):
        return None
    if any((f["headers_sha256"], f["body_sha256"], f["status"]) !=
           (facts[0]["headers_sha256"], facts[0]["body_sha256"], facts[0]["status"]) for f in facts):
        return None
    queries = [parse_qsl(urlsplit(f["url"]).query, keep_blank_values=True) for f in facts]
    controlled = [[v for k, v in query if k == locator["input"]] for query in queries]
    other = [[(k, v) for k, v in query if k != locator["input"]] for query in queries]
    if (any(len(values) != 1 for values in controlled) or controlled[0] != controlled[1]
            or controlled[2] != controlled[3] or controlled[0] == controlled[2]
            or any(item != other[0] for item in other)):
        return None
    if all(pointer in f["json_counts"] for f in facts):
        values = [f["json_counts"][pointer] for f in facts]
    elif policy == SHAPE_POLICY and all(pointer in f.get("json_shapes", {}) for f in facts):
        # Remove both controlled values from every signature. Filtering each
        # response separately would invent a difference if a static key happened
        # to equal the test input.
        reflected = {text for value in (controlled[0][0], controlled[2][0]) if value
                     for text in (value, quote(value, safe=""), quote_plus(value, safe=""))}
        def without_reflection(signature):
            if signature.get("type") != "object":
                return signature
            keys = [key for key in signature["keys"]
                    if not any(key == text or (len(text) >= 3 and text in key) for text in reflected)]
            return {"type": "object", "empty": not keys, "keys": keys}
        values = [without_reflection(f["json_shapes"][pointer]) for f in facts]
    else:
        return None
    if values[0] != values[1] or values[2] != values[3]:
        return None
    return values[0] != values[2]


def verify_observations(value, locator, records, *, policy=POLICY):
    if policy not in POLICIES:
        raise ValueError("Unknown observation verification policy")
    fields = {}
    proofs = value.get("observation_evidence", {})
    for field, claimed in value.get("observations", {}).items():
        proof = proofs.get(field, {})
        ids = proof.get("request_ids", []) + proof.get("baseline", []) + proof.get("test", [])
        entry = {"state": "unsupported", "value": claimed, "reason": "no_matching_capture", "request_ids": []}
        fields[field] = entry
        if not ids or any(key not in records or not _matches(records[key]["facts"], locator) for key in ids):
            continue
        entry["request_ids"] = ids
        observed = None
        if field in ("response_status", "baseline_status") and len(ids) == 1:
            observed = records[ids[0]]["facts"]["status"]
        elif field in ("query_behavior", "reproduced", "baseline_compared"):
            difference = _comparison(proof, records, locator, policy=policy)
            if difference is not None:
                if field == "query_behavior":
                    observed = "controlled_difference" if difference else "unchanged"
                elif field == "baseline_compared":
                    observed = True
                else:
                    observed = difference
        if observed is None or claimed is None or claimed == "unknown":
            entry["reason"] = "automatic_check_unavailable"
        else:
            entry.update(state="supported" if type(claimed) is type(observed) and claimed == observed else "contradicted",
                         reason="captured_measurement")
    return {"policy": policy, "fields": fields}


def supported_observations(node, *, policy=POLICY):
    # Nullable booleans keep an absent measurement distinct from a negative one.
    from engine.risk_rubric import observations

    raw = observations(node.get("observations", {}))
    verification = node.get("observation_verification", {})
    fields = verification.get("fields", {}) if verification.get("policy") == policy else {}
    result = {}
    for key, value in raw.items():
        entry = fields.get(key, {})
        supported = (entry.get("state") == "supported" and type(entry.get("value")) is type(value)
                     and entry.get("value") == value)
        result[key] = value if supported else "unknown" if isinstance(value, str) else None
    return result
