"""Versioned A4 configuration and pure, narrowly scoped lab verdict policy."""

from urllib.parse import urlsplit

from risk_rubric import is_finite_number


POLICY_VERSION = "juice-shop-sqli-v1"
LAB_TARGET = "http://127.0.0.1:3000/"
LAB_IMAGE = "bkimminich/juice-shop:v20.2.0"
CHALLENGE_KEY = "dbSchemaChallenge"
DEFAULT_SETTINGS = {"version": "v4-execution-v1", "node_attempts": 2, "node_tool_calls": 6,
                    "oracle_max_calls": 100, "oracle_timeout_seconds": 5,
                    "deep_reserve_calls": 4, "verification_reserve_seconds": 25,
                    "max_cost_usd": .10, "remote_call_reserve_usd": .005, "exclusive_lab": False}


class V4BudgetStop(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def execution_settings(raw=None):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or raw.keys() - DEFAULT_SETTINGS.keys():
        raise ValueError("Unsupported v4 execution setting")
    result = {**DEFAULT_SETTINGS, **raw}
    if result["version"] != DEFAULT_SETTINGS["version"] or type(result["exclusive_lab"]) is not bool:
        raise ValueError("Invalid v4 execution version/isolation setting")
    for key, maximum in (("node_attempts", 5), ("node_tool_calls", 20), ("oracle_max_calls", 500)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError("Invalid v4 node/oracle limit")
    if type(result["deep_reserve_calls"]) is not int or not 0 <= result["deep_reserve_calls"] <= 100:
        raise ValueError("Invalid v4 deep-node reserve")
    if not is_finite_number(result["verification_reserve_seconds"]) or not 0 <= result["verification_reserve_seconds"] <= 120:
        raise ValueError("Invalid v4 verification time reserve")
    for key, maximum in (("oracle_timeout_seconds", 30), ("max_cost_usd", 100), ("remote_call_reserve_usd", 10)):
        if not is_finite_number(result[key]) or not 0 < result[key] <= maximum:
            raise ValueError("Invalid v4 cost/time setting")
    return result


def supported_candidate(node):
    if not isinstance(node, dict) or node.get("kind") != "candidate" or (node.get("hypothesis") or {}).get("type") != "sqli":
        return False
    locator = node.get("locator", {})
    try:
        url = urlsplit(locator.get("url", ""))
        return (url.scheme, url.netloc, url.path, locator.get("method"), locator.get("input")) == (
            "http", "127.0.0.1:3000", "/rest/products/search", "GET", "q")
    except (TypeError, ValueError):
        return False


def oracle_verdict(proof, node, *, execution_status):
    result = {"verdict": "inconclusive", "reason": "oracle_requirements_not_met",
              "policy_version": POLICY_VERSION, "source": "lab_oracle"}
    if not supported_candidate(node):
        return {**result, "reason": "unsupported_verification_scope"}
    try:
        before, after = proof["before"], proof["after"]
        identity = proof["identity_before"]
        if (proof["policy_version"] != POLICY_VERSION or proof["source"] != "harness_http"
                or proof["target"] != LAB_TARGET or proof["exclusive_lab"] is not True or proof["fresh_reset"] is not True
                or execution_status != "succeeded" or before["solved"] is not False or after["solved"] is not True
                or type(before["id"]) is not int or type(after["id"]) is not int or before["id"] < 1 or after["id"] != before["id"]
                or before["key"] != CHALLENGE_KEY or after["key"] != CHALLENGE_KEY
                or before["name"] != "Database Schema" or after["name"] != "Database Schema"
                or before["category"] != "Injection" or after["category"] != "Injection"
                or identity != proof["identity_after"] or identity["image"] != LAB_IMAGE
                or any(not isinstance(identity.get(key), str) or not identity[key] for key in ("container_id", "image_id", "started_at"))
                or type(proof["evidence_count"]) is not int or proof["evidence_count"] < 5
                or any(proof["canary"].get(key) is not True for key in ("control_absent", "test_present", "repeat_present"))):
            return result
    except (TypeError, KeyError, AttributeError):
        return result
    return {**result, "verdict": "confirmed", "reason": "lab_transition_and_independent_sql_expression"}
