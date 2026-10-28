"""Model-independent reports and stop/preserve/report/export/destroy lifecycle."""

import json
import time
from pathlib import Path

from session_log import LogFailure, atomic_write_json
from v4_policy import LAB_TARGET
from v4_tree import TreeStore
from v4_usage import summarize_usage


def readable_prefix(log):
    events, corruption = [], None
    try:
        with log.events_path.open("rb") as stream:
            for number in range(1, 1000001):
                raw = stream.readline(2 * 1024 * 1024 + 1)
                if not raw:
                    break
                try:
                    if len(raw) > 2 * 1024 * 1024:
                        raise ValueError()
                    item = json.loads(raw)
                    if (not isinstance(item, dict) or item.get("run_id") != log.run_id
                            or type(item.get("seq")) is not int or item["seq"] != number
                            or not isinstance(item.get("kind"), str)):
                        raise ValueError()
                    events.append(item)
                except (ValueError, UnicodeError, RecursionError):
                    corruption = {"line": number, "reason": "invalid_event_prefix"}
                    break
    except OSError:
        corruption = {"line": len(events) + 1, "reason": "unreadable_events"}
    return events, corruption


def _metadata(log, **fields):
    try:
        log.update(**fields)
    except (LogFailure, AttributeError, OSError):
        # A failed journal must not prevent a separate, atomic report/status
        # snapshot. This does not repair or truncate the journal.
        log.metadata.update(fields)
        atomic_write_json(log.run_dir / "session.json", log.metadata)


def save_report(log, run_result, reason, *, state=None, error=None, persist_metadata=True):
    events, corruption = readable_prefix(log)
    degraded = state is None or corruption is not None
    usage = None
    if not degraded:
        try:
            usage = summarize_usage(events, state)
        except (ValueError, KeyError, TypeError):
            degraded, error = True, error or "Invalid usage events"
    stats = {key: None for key in ("surfaces", "candidates", "tested_candidates", "confirmed", "rejected",
                                  "inconclusive", "unexplored", "unscored", "fallback_selections")}
    high, evidence = [], []
    if not degraded:
        nodes = list(state["nodes"].values())
        candidates = [node for node in nodes if node["kind"] == "candidate"]
        stats.update(surfaces=sum(node["kind"] == "surface" for node in nodes), candidates=len(candidates),
            tested_candidates=sum(bool(node["attempts"]) for node in candidates),
            confirmed=sum(node["verification_state"] == "confirmed" for node in candidates),
            rejected=sum(node["verification_state"] == "rejected" for node in candidates),
            inconclusive=sum(node["verification_state"] not in ("confirmed", "rejected") for node in candidates),
            unexplored=sum(node["investigation_state"] == "pending" and node["exploration_state"] != "blocked" for node in nodes),
            unscored=sum(node["kind"] != "root" and node["risk"].get("score") is None for node in nodes),
            fallback_selections=sum(event["kind"] == "v4_node_selection" and event.get("degraded_mode") is True for event in events))
        high = [{"node_id": node["id"], "score": node["risk"]["score"]} for node in nodes
                if node["investigation_state"] == "pending" and (node["risk"].get("score") or 0) >= 75]
        evidence = [ref for node in nodes for ref in node["evidence_refs"]]
    else:
        # These are prefix references, never final verification statistics.
        for event in events:
            payload = event.get("payload") or {}
            if isinstance(payload, dict):
                refs = payload.get("evidence_refs", [])
                if isinstance(refs, list):
                    evidence.extend(ref for ref in refs if isinstance(ref, dict) and "path" in ref)
    status = "degraded" if degraded else "complete"
    counts = "집계 확인 불가: 이벤트/트리 복구가 불완전합니다." if degraded else (
        f"공격 표면 {stats['surfaces']} · 후보 {stats['candidates']} · 검증 시도 후보 {stats['tested_candidates']}\n"
        f"확인 {stats['confirmed']} · 기각 {stats['rejected']} · 미확정(미검증 포함) {stats['inconclusive']}\n"
        f"미탐색 {stats['unexplored']} · 미점수 {stats['unscored']} · 후순위 선택 {stats['fallback_selections']}")
    summary = f"결과: {run_result} · 종료 사유: {reason}\n{counts}\n발견한 노드 범위의 결과이며 사이트 전체의 취약점 부재를 뜻하지 않습니다."
    report = {"schema_version": 1, "run_id": log.run_id, "status": status, "run_result": run_result,
              "stop_reason": reason, "summary": summary, "stats": stats, "unexplored_high_risk": high,
              "evidence_refs": evidence, "usage": usage, "last_snapshot_usage": log.metadata.get("v4_usage") if degraded else None,
              "last_readable_seq": events[-1]["seq"] if events else 0, "corruption": corruption, "error": error}
    atomic_write_json(log.run_dir / "report.json", report)
    if persist_metadata:
        _metadata(log, report=summary, report_status=status, report_stats=stats, v4_usage=usage)
    return report


class SnapshotLog:
    """Independent report/status sink when the journal cannot be opened."""
    failed = True

    def __init__(self, root, metadata):
        from session_log import validate_run_id
        self.metadata = dict(metadata)
        self.run_id = validate_run_id(metadata["run_id"])
        self.run_dir = Path(root).resolve() / self.run_id
        self.events_path = self.run_dir / "events.jsonl"

    def update(self, **fields):
        raise LogFailure("Journal is unavailable")

    def append(self, *args, **kwargs):
        raise LogFailure("Journal is unavailable")


def owned_records(metadata, events):
    records = {item["pid"]: item for item in metadata.get("managed_processes", [])}
    for event in events:
        process = event.get("process")
        if not isinstance(process, dict) or event["kind"] not in ("process_started", "process_stopped"):
            continue
        if event["kind"] == "process_started":
            records[process["pid"]] = process
        elif records.get(process.get("pid"), {}).get("created_at") == process.get("created_at"):
            records.pop(process.get("pid"), None)
    return list(records.values())


def emergency_recovery(root, metadata, error):
    from command_runtime import CommandRuntime
    log = SnapshotLog(root, metadata)
    runtime = CommandRuntime(root, log)
    events, _corruption = readable_prefix(log)
    stopped = False
    try:
        runtime.restore_owned(log.run_id, owned_records(metadata, events))
        runtime.stop(log.run_id, 20, record=False)
        stopped = not runtime.inspect(log.run_id)["managed_processes"]
    except Exception:
        pass
    # Read only the journal/metadata, never live runtime files. Even if known
    # processes stopped, ownership after a corrupt prefix is uncertain.
    save_report(log, metadata.get("run_result") or "interrupted", metadata.get("finish_reason") or "log_failure", error=error)
    _metadata(log, phase="recovery_required", log_status="failed", cleanup_status="recovery_required",
              cleanup_error=error, managed_processes=[] if stopped else metadata.get("managed_processes", []))
    return log.metadata


def reconcile_pending(tree):
    state = tree.refresh()
    for attempt in state["attempts"].values():
        if attempt["status"] != "pending":
            continue
        executions = [(key, value) for key, value in state["executions"].items() if value["call_id"] == attempt["call_id"]]
        if len(executions) > 1:
            raise LogFailure("Ambiguous attempt execution on recovery")
        exec_id = executions[0][0] if executions else None
        if exec_id:
            try:
                tree.import_result(attempt["attempt_id"], exec_id=exec_id, interrupted=True)
                continue
            except (ValueError, OSError):
                pass
        tree.fail_attempt(attempt["attempt_id"], exec_id=exec_id, reason="interrupted" if exec_id else "not_executed")


def finalize_v4(log, runtime, run_result, reason, *, lab_reset):
    deadline = time.monotonic() + (log.metadata.get("budgets") or {}).get("finalize_seconds", 120)
    def remaining(cap):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Finalization time budget exhausted")
        return min(cap, left)
    state, error, stopped = None, None, False
    try:
        _metadata(log, phase="finalizing", run_result=run_result, finish_reason=reason)
        runtime.stop(log.run_id, remaining(20), record=not log.failed)
        if runtime.inspect(log.run_id)["managed_processes"]:
            raise RuntimeError("Managed processes remain")
        stopped = True
        tree = TreeStore(log)
        reconcile_pending(tree)
        state = tree.rebuild()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        if not stopped:
            try:
                runtime.stop(log.run_id, 20, record=False)
                stopped = not runtime.inspect(log.run_id)["managed_processes"]
            except Exception:
                pass
    try:
        # Always attempt a reducer-independent report, even after stop/replay
        # failure. Runtime evidence cannot be read/deleted until stop succeeds.
        save_report(log, run_result, reason, state=state, error=error)
        if error or not stopped:
            raise RuntimeError(error or "Runtime writers did not stop")
        remaining(120)
        runtime.export_evidence(log.run_id)
        log.flush()
        _metadata(log, log_status="preserved")
        runtime.destroy(log.run_id, remaining(20))
        _metadata(log, cleanup_status="succeeded")
        if log.metadata.get("target") == LAB_TARGET and log.metadata.get("lab_reset_status") != "succeeded":
            _metadata(log, phase="resetting", lab_reset_status="running")
            remaining(120)
            from lab import reset_lab
            reset = lab_reset(deadline=deadline) if lab_reset is reset_lab else lab_reset()
            remaining(120)
            if not isinstance(reset, dict) or reset.get("verified") is not True:
                raise RuntimeError("Lab reset failed")
            log.append("lab_reset", {"probe": reset, "status": "succeeded"})
            _metadata(log, lab_reset_status="succeeded")
        log.append("run_finalized", {"run_result": run_result, "reason": reason})
        _metadata(log, phase="finished", log_status="finalized", cleanup_error="", finished_at=time.time())
    except Exception as exc:
        if log.metadata.get("lab_reset_status") == "running":
            _metadata(log, lab_reset_status="failed")
        _metadata(log, phase="recovery_required", cleanup_error=f"{type(exc).__name__}: {exc}"[:500],
                  cleanup_status="failed" if runtime.inspect(log.run_id)["runtime_exists"] else log.metadata.get("cleanup_status", "failed"),
                  log_status="failed" if log.failed else log.metadata.get("log_status", "collecting"))
    finally:
        log.close()
