"""Model-independent reports and stop/preserve/report/export/destroy lifecycle."""

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from engine.session_log import LogFailure, atomic_write_json
from engine.lab import TARGET as LAB_TARGET
from engine.contracts import dedup_key
from engine.tree import TreeStore, investigation_gaps, latest_observation
from engine.usage import summarize_usage


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


def _candidate_leads(state):
    """Expose model-authored hypotheses and preserved provenance without issuing a verdict."""
    discovery_evidence = {}
    discovery_claims = {}
    for attempt in state["attempts"].values():
        if attempt["status"] != "imported" or attempt["execution_status"] != "succeeded":
            continue
        refs = [ref for ref in attempt["evidence_refs"] if ref.get("source") == "model_file"]
        candidates = [item for item in attempt["result"]["discovered"] if item["kind"] == "candidate"]
        for discovery in candidates:
            discovery_evidence.setdefault(dedup_key(discovery), []).extend(refs)
        claim = attempt["result"]["claim"].strip()
        if len(candidates) == 1 and claim:
            discovery_claims[dedup_key(candidates[0])] = claim

    leads = []
    for node in state["nodes"].values():
        if node["kind"] != "candidate":
            continue
        previous = latest_observation(state, node["id"])
        sources = []
        seen = set()
        for ref in discovery_evidence.get(node["dedup_key"], []):
            if ref["path"] not in seen:
                sources.append(ref)
                seen.add(ref["path"])
        leads.append({"node_id": node["id"], "locator": node["locator"],
                      "hypothesis": node["hypothesis"], "investigation_state": node["investigation_state"],
                      "investigation_reason": node["investigation_reason"],
                      "test_attempted": bool(node["attempts"]),
                      "candidate_proposed_verdict": previous["proposed_verdict"] if previous else None,
                      "candidate_claim": previous["claim"] if previous else "",
                      "discovery_claim": discovery_claims.get(node["dedup_key"], ""),
                      "discovery_evidence_refs": sources[:8],
                      "discovery_evidence_count": len(sources),
                      "candidate_evidence_refs": [ref for ref in node["evidence_refs"]
                                                  if ref.get("source") == "model_file"][:8]})
    return leads


def save_report(log, run_result, reason, *, state=None, error=None, persist_metadata=True):
    events, corruption = readable_prefix(log)
    degraded = state is None or corruption is not None
    usage = None
    if not degraded:
        try:
            usage = summarize_usage(events, state)
        except (ValueError, KeyError, TypeError):
            degraded, error = True, error or "Invalid usage events"
    gaps = investigation_gaps(state) if not degraded else None
    if run_result == "completed" and (degraded or gaps):
        run_result = "incomplete"
        reason = "investigation_unavailable" if degraded else "investigation_incomplete"
    stats = {key: None for key in ("surfaces", "candidates", "tested_candidates", "confirmed", "rejected",
                                  "inconclusive", "unexplored", "unscored", "fallback_selections", "incomplete_nodes")}
    high, evidence, candidate_leads = [], [], None
    if not degraded:
        nodes = list(state["nodes"].values())
        candidates = [node for node in nodes if node["kind"] == "candidate"]
        stats.update(surfaces=sum(node["kind"] == "surface" for node in nodes), candidates=len(candidates),
            tested_candidates=sum(bool(node["attempts"]) for node in candidates),
            inconclusive=len(candidates),
            incomplete_nodes=len(gaps),
            unexplored=sum(node["investigation_state"] == "pending" and node["exploration_state"] != "blocked" for node in nodes),
            unscored=sum(node["kind"] != "root" and node["risk"].get("score") is None for node in nodes),
            fallback_selections=sum(event["kind"] == "v4_node_selection" and event.get("degraded_mode") is True for event in events))
        high = [{"node_id": node["id"], "score": node["risk"]["score"]} for node in nodes
                if node["investigation_state"] != "done" and node["exploration_state"] != "blocked"
                and (node["risk"].get("score") or 0) >= 75]
        evidence = [ref for node in nodes for ref in node["evidence_refs"]]
        candidate_leads = _candidate_leads(state)
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
        f"공격 표면 {stats['surfaces']} · 후보 {stats['candidates']} · 테스트 시도 후보 {stats['tested_candidates']}\n"
        f"자동 취약점 판정 없음 · 미확정(미검증 포함) {stats['inconclusive']}\n"
        f"미탐색 {stats['unexplored']} · 조사 미완료 {stats['incomplete_nodes']} · 미점수 {stats['unscored']} · 후순위 선택 {stats['fallback_selections']}")
    summary = f"결과: {run_result} · 종료 사유: {reason}\n{counts}\n발견한 노드 범위의 결과이며 사이트 전체의 취약점 부재를 뜻하지 않습니다."
    if candidate_leads:
        summary += "\n후보 단서: " + "; ".join(
            f"{lead['node_id']} {lead['hypothesis']['type']} {lead['locator']['method']} "
            f"{urlsplit(lead['locator']['url']).path} [{lead['locator']['input']}] "
            f"({'테스트 시도' if lead['test_attempted'] else '테스트 미시도'}, "
            f"발견 시도 자료 {lead['discovery_evidence_count']}개)"
            for lead in candidate_leads[:5])
        if len(candidate_leads) > 5:
            summary += f" 외 {len(candidate_leads) - 5}개"
        for lead in candidate_leads[:5]:
            claim = lead["candidate_claim"] or lead["discovery_claim"]
            if claim:
                summary += f"\n모델 기록(미확인, {lead['node_id']}): {claim[:220]}"
    if gaps:
        summary += "\n조사 미완료: " + "; ".join(f"{item['node_id']} ({item['reason']})" for item in gaps[:10])
    report = {"schema_version": 1, "run_id": log.run_id, "status": status, "run_result": run_result,
              "stop_reason": reason, "summary": summary, "stats": stats, "unexplored_high_risk": high,
              "incomplete_nodes": gaps, "candidate_leads": candidate_leads,
              "evidence_refs": evidence, "usage": usage, "last_snapshot_usage": log.metadata.get("v4_usage") if degraded else None,
              "last_readable_seq": events[-1]["seq"] if events else 0, "corruption": corruption, "error": error}
    atomic_write_json(log.run_dir / "report.json", report)
    if persist_metadata:
        _metadata(log, report=summary, report_status=status, report_stats=stats, v4_usage=usage,
                  run_result=run_result, finish_reason=reason)
    return report


class SnapshotLog:
    """Independent report/status sink when the journal cannot be opened."""
    failed = True

    def __init__(self, root, metadata):
        from engine.session_log import validate_run_id
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
    from engine.command_runtime import CommandRuntime
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
        report = save_report(log, run_result, reason, state=state, error=error)
        run_result, reason = report["run_result"], report["stop_reason"]
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
            from engine.lab import reset_lab
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
