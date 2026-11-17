"""Join attempts to actual command starts, without treating reservations as tests."""


def executed_node_ids(state, events=()):
    calls = {execution.get("call_id") for execution in state.get("executions", {}).values()}
    calls.update(event.get("call_id") for event in events if event.get("kind") == "exec_started")
    calls.discard(None)
    return {attempt["node_id"] for attempt in state.get("attempts", {}).values()
            if attempt.get("call_id") in calls}
