"""Versioned v4 execution limits."""

from risk_rubric import is_finite_number


# Node limits bound consecutive unproductive results and tools since progress.
# Productive follow-ups use the shared run budgets.
DEFAULT_SETTINGS = {"version": "v4-execution-v2", "node_attempts": 2, "node_tool_calls": 6,
                    "deep_reserve_calls": 4, "max_cost_usd": .10, "remote_call_reserve_usd": .005}


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
    if result["version"] != DEFAULT_SETTINGS["version"]:
        raise ValueError("Invalid v4 execution version")
    for key, maximum in (("node_attempts", 5), ("node_tool_calls", 20)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError("Invalid v4 node limit")
    if type(result["deep_reserve_calls"]) is not int or not 0 <= result["deep_reserve_calls"] <= 100:
        raise ValueError("Invalid v4 deep-node reserve")
    for key, maximum in (("max_cost_usd", 100), ("remote_call_reserve_usd", 10)):
        if not is_finite_number(result[key]) or not 0 < result[key] <= maximum:
            raise ValueError("Invalid v4 cost/time setting")
    return result
