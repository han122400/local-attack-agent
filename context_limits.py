"""Bound the serialized model input, including retry feedback. Originals stay in the run log."""

import copy
import json

from planning import SYSTEM_PROMPT


def context_char_budget(num_ctx: int) -> int:
    # shortcut: characters approximate tokens; use the provider tokenizer for exact token accounting.
    budget = int(num_ctx * 3.3 * 0.65) - len(SYSTEM_PROMPT)
    if budget < 512:
        raise ValueError("num_ctx is too small for the system prompt and output reserve")
    return budget


def compact_context(context: dict, budget: int) -> dict:
    result = copy.deepcopy(context)
    def size():
        return len(json.dumps(result, ensure_ascii=False))
    def shorten(value, limit):
        if isinstance(value, str) and len(value) > limit:
            return value[:limit // 2] + "\n[truncated; full value in run log]\n" + value[-limit // 2:]
        if isinstance(value, dict):
            return {key: shorten(child, limit) for key, child in value.items()}
        if isinstance(value, list):
            return [shorten(child, limit) for child in value]
        return value
    for limit in (1200, 600, 240, 80):
        if size() <= budget:
            return result
        for key in ("history", "recent"):
            if key in result:
                result[key] = shorten(result[key], limit)
        for key in ("history", "recent"):
            while size() > budget and len(result.get(key, [])) > 1:
                result[key].pop(0)
    for key in ("history", "recent"):
        if size() > budget:
            result[key] = []
    if size() > budget:
        raise ValueError("Required context exceeds the input budget; shorten the goal or increase num_ctx")
    return result
