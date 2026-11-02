"""Pure A4 usage reconstruction, including conservatively held unknown costs."""

from risk_rubric import is_finite_number
from scorer_state import usage as scoring_usage


def summarize_usage(events, state, *, elapsed_seconds=None):
    reserved = [event for event in events if event["kind"] == "v4_execution_reserved"]
    responses = {event["call_id"]: event for event in events if event["kind"] == "model_response"}
    known, unknown, held = 0.0, 0, 0.0
    ids = set()
    for event in reserved:
        if (type(event.get("remote")) is not bool or not is_finite_number(event.get("estimate_usd"))
                or event["estimate_usd"] < 0 or not isinstance(event.get("call_id"), str) or event["call_id"] in ids):
            raise ValueError("Invalid execution reservation accounting")
        ids.add(event["call_id"])
        response = responses.get(event["call_id"], {})
        cost = response.get("cost") if event["remote"] else 0.0
        if is_finite_number(cost) and cost >= 0:
            known += cost
        else:
            unknown += 1
            held += event["estimate_usd"]
    scorer = scoring_usage(state["scorer"])
    known += scorer["known_cost_usd"]
    unknown += scorer["unknown_cost_requests"]
    held += scorer["reserved_cost_usd"]
    if elapsed_seconds is None:
        finished = [event for event in events if event["kind"] == "v4_exploration_finished"]
        elapsed_seconds = finished[-1].get("elapsed_seconds") if finished else None
    def tokens(field):
        values = [responses.get(event["call_id"], {}).get(field) for event in reserved]
        return sum(values) if all(type(value) is int and value >= 0 for value in values) else None
    return {"execution_calls": len(reserved), "scoring_calls": scorer["requests"],
            "model_calls": len(reserved) + scorer["requests"],
            "tool_calls": sum(event["kind"] == "exec_started" for event in events),
            "execution_input_tokens": tokens("prompt_eval_count"), "execution_output_tokens": tokens("eval_count"),
            "scoring_input_tokens": scorer["input_tokens"], "scoring_output_tokens": scorer["output_tokens"],
            "cost_usd": None if unknown else known, "known_cost_usd": known,
            "unknown_cost_requests": unknown, "reserved_cost_usd": held, "accounted_cost_usd": known + held,
            "elapsed_seconds": elapsed_seconds}
