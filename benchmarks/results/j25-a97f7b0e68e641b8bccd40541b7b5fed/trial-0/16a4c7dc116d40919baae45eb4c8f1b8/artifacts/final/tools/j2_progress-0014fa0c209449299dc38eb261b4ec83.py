"""Read-only progress from helper metadata; never a security verdict or scheduler."""

from urllib.parse import urlsplit, urlunsplit


def public_path(url):
    try:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname:
            return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path or "/", "", ""))
    except (ValueError, TypeError):
        pass
    return None


def pipeline_view(summaries, executions, rejections=()):
    responses, analyses, failures = {}, {}, {}
    for summary in summaries:
        analyses[summary["source_url"]] = {"step": summary["step"], "summary_truncated": summary["truncated"]}
    for execution in executions:
        step = execution["step"]
        for response in execution.get("http_observations", []):
            if response["method"] == "GET" and not response.get("url_truncated") and len(response["url"]) < 240:
                responses[public_path(response["url"])] = {"step": step, "status": response["status"]}
        signals = [("navigation", error["url"], error["error_type"])
                   for error in execution.get("helper_errors", [])]
        signals += [("access_response", response["url"], f"HTTP {response['status']}")
                    for response in execution.get("http_observations", []) if response["status"] in {401, 403}]
        kind = execution.get("failure_kind")
        if kind:
            signals.append((kind, "", execution.get("exception_type") or execution.get("cause_code") or ""))
        for category, url, error_type in signals:
            key = category, url, error_type
            item = failures.setdefault(key, {"kind": category, "url": url, "error_type": error_type,
                "steps": [], "count": 0, "source_path": "", "later_completion_step": None})
            item["steps"] = (item["steps"] + [step])[-8:]
            item["count"] += 1
            item["source_path"] = execution.get("source_path", "")
            item["later_completion_step"] = None
        for event in execution.get("helper_outcomes", []):
            if event.get("helper") == "goto_and_render" and event.get("outcome") == "completed":
                for (category, url, _), item in failures.items():
                    if (category == "navigation" and len(url) < 240 and len(event["url"]) < 240 and step > item["steps"][-1]
                            and public_path(url) == public_path(event["url"])):
                        item["later_completion_step"] = step
    resources = {}
    for summary in reversed(list(summaries)):
        entries = summary["assets"] + [item for item in summary["hints"] if item.get("kind") == "navigation"]
        for entry in entries:
            url = entry.get("url")
            if not isinstance(url, str) or not public_path(url) or url in resources:
                continue
            resources[url] = {"url": url, "kind": entry.get("kind"), "source_url": summary["source_url"],
                "declared_step": entry.get("step", summary["step"]), "response": responses.get(public_path(url)),
                "analysis": analyses.get(url)}
    items = list(resources.values())
    pending = [item for item in items if item["analysis"] is None]
    recent_failures = sorted(failures.values(), key=lambda item: max(item["steps"][-1], item["later_completion_step"] or 0))
    review = {"declared_resources": len(items), "response_metadata_resources": sum(item["response"] is not None for item in items),
        "analyzed_resources": len(items) - len(pending), "unreviewed_resources": len(pending),
        "limited_analyses": sum(bool(item["analysis"] and item["analysis"]["summary_truncated"]) for item in items),
        "failure_groups": len(failures), "preflight_rejections": len(rejections), "must_visit_all": False}
    return {"resources": items[:128], "omitted_resources": max(0, len(items) - 128),
        "pending_resources": pending[:6], "omitted_pending": max(0, len(pending) - 6),
        "failures": recent_failures[-32:], "omitted_failures": max(0, len(failures) - 32),
        "review": review, "verification": "helper_metadata_and_provided_text_only",
        "limits": "Response matching is by public path (queries removed), not exact request identity. Analysis means supplied text was scanned, not that full/current content was obtained. Later helper completion is not proof the original cause was fixed."}
