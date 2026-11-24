"""Model-independent reports and stop/preserve/report/export/destroy lifecycle."""

import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from engine.session_log import LogFailure, atomic_write_json
from engine.lab import TARGET as LAB_TARGET
from engine.contracts import dedup_key
from engine.tree import TreeStore, investigation_gaps, latest_observation
from engine.usage import summarize_usage
from engine.seed_metrics import seed_metrics
from engine.execution_evidence import executed_node_ids
from engine.risk_rubric import AUDIT_PROJECTION_VERSION, project_node
from engine.observation_verification import POLICIES, supported_observations
from engine.http_audit import audit_writer, checked_audit, empty_audit, finalized_archive
from engine.observed_http import observed_http_section


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


def _candidate_leads(state, executed=None):
    """Expose model-authored hypotheses and preserved provenance without issuing a verdict."""
    discovery_evidence = {}
    executed = executed_node_ids(state) if executed is None else executed
    discovery_claims = {}
    for attempt in state["attempts"].values():
        if attempt["status"] != "imported" or attempt["execution_status"] != "succeeded":
            continue
        refs = [ref for ref in attempt["evidence_refs"] if ref.get("source") == "model_file"]
        candidates = [item for item in attempt["result"]["discovered"] if item["kind"] == "candidate"]
        for discovery in candidates:
            attributed = refs
            if "evidence_refs" in discovery:
                paths = {ref["path"] for ref in discovery["evidence_refs"]}
                attributed = [ref for ref in refs if ref.get("runtime_path") in paths]
            discovery_evidence.setdefault(dedup_key(discovery), []).extend(attributed)
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
                      "test_attempted": node["id"] in executed,
                      "candidate_proposed_verdict": previous["proposed_verdict"] if previous else None,
                      "candidate_claim": previous["claim"] if previous else "",
                      "discovery_claim": discovery_claims.get(node["dedup_key"], ""),
                      "discovery_evidence_refs": sources[:8],
                      "discovery_evidence_count": len(sources),
                      "candidate_evidence_refs": [ref for attempt_id in node["attempts"]
                          for ref in state["attempts"][attempt_id].get("evidence_refs", [])
                          if ref.get("source") == "model_file"][:8]})
    return leads


def save_report(log, run_result, reason, *, state=None, error=None, persist_metadata=True):
    events, corruption = readable_prefix(log)
    degraded = state is None or corruption is not None
    usage = None
    if not degraded:
        try:
            from engine.http_capture import check_capture_records
            check_capture_records(log.run_dir, state.get("http_exchanges", {}).values())
            usage = summarize_usage(events, state)
        except (OSError, ValueError, KeyError, TypeError):
            degraded, error = True, error or "Invalid usage events or captured evidence"
    gaps = investigation_gaps(state) if not degraded else None
    policy = (log.metadata.get("scorer_settings") or {}).get("reevaluation_policy")
    if not policy:
        creation = next((e.get("configuration") for e in events if e["kind"] == "v4_execution_created"), None)
        if isinstance(creation, dict) and isinstance(creation.get("scorer_settings"), dict):
            policy = creation["scorer_settings"].get("reevaluation_policy")
    scorer_config = (state or {}).get("scorer", {}).get("configuration") or {}
    policy = scorer_config.get("projection_version") or policy
    audited = policy in (AUDIT_PROJECTION_VERSION, *POLICIES)
    if audited and gaps:
        gaps = [{"node_id": gap["node_id"], "reason": gap["reason"]} for gap in gaps]
    if run_result == "completed" and (degraded or gaps):
        run_result = "incomplete"
        reason = "investigation_unavailable" if degraded else "investigation_incomplete"
    stats = {key: None for key in ("surfaces", "candidates", "tested_candidates", "confirmed", "rejected",
                                  "inconclusive", "unexplored", "unscored", "fallback_selections", "incomplete_nodes",
                                  "reused_nodes", "seed_pending_candidates", "seed_stale_nodes", "seed_deferred_nodes", "observed_nodes")}
    high, evidence, candidate_leads = [], [], None
    unverified_leads = None
    if not degraded:
        from strategies.greedy import seed_dispatchable
        nodes = list(state["nodes"].values())
        candidates = [node for node in nodes if node["kind"] == "candidate"]
        executed = executed_node_ids(state, events)
        stats.update(surfaces=sum(node["kind"] == "surface" for node in nodes), candidates=len(candidates),
            tested_candidates=sum(node["id"] in executed for node in candidates),
            inconclusive=len(candidates),
            incomplete_nodes=len(gaps),
            reused_nodes=sum(node["investigation_state"] == "reused" for node in nodes),
            observed_nodes=sum(latest_observation(state, node["id"]) is not None for node in nodes),
            seed_pending_candidates=sum(bool(node.get("seeded")) and (node["investigation_state"] != "done"
                                        or node.get("seed_revalidation_pending", False)) for node in candidates),
            seed_stale_nodes=sum(node.get("seed_freshness") == "stale" for node in nodes),
            seed_deferred_nodes=sum(bool(node.get("seeded"))
                                    and node["exploration_state"] not in ("done", "blocked")
                                    and not seed_dispatchable(state, node["id"], log.metadata["target"])
                                    for node in nodes),
            unexplored=sum(node["investigation_state"] == "pending" and node["exploration_state"] != "blocked" for node in nodes),
            unscored=sum(node["kind"] != "root" and node["risk"].get("status") != "scored" for node in nodes),
            fallback_selections=sum(event["kind"] == "v4_node_selection" and event.get("degraded_mode") is True for event in events))
        high = [{"node_id": node["id"], "score": node["risk"]["score"]} for node in nodes
                if node["investigation_state"] not in ("done", "reused") and node["exploration_state"] != "blocked"
                and node["risk"].get("status") == "scored" and (node["risk"].get("score") or 0) >= 75]
        if audited:
            candidate_leads = []
            for node in candidates:
                category = project_node(node, projection_version=AUDIT_PROJECTION_VERSION)["hypothesis_type"]
                candidate_leads.append({"node_id": node["id"], "hypothesis": {"type": category},
                                        "locator": node["locator"], "test_attempted": node["id"] in executed,
                                        "verification_state": "inconclusive", "attempt_ids": list(node["attempts"])})
                if policy in POLICIES:
                    candidate_leads[-1]["observations"] = supported_observations(node, policy=policy)
                    candidate_leads[-1]["observation_verification"] = {"policy": policy, "fields": {
                        field: {key: value for key, value in entry.items() if key != "value"}
                        for field, entry in node.get("observation_verification", {}).get("fields", {}).items()}}
            if policy in POLICIES:
                unverified_leads = []
                for node in candidates:
                    if node.get("origin") != "unverified_lead":
                        continue
                    attempt_id = node["source_attempt_id"]
                    attempt = state["attempts"][attempt_id]
                    fields = attempt.get("observation_verification", {}).get("fields", {})
                    unresolved = {key: entry for key, entry in fields.items()
                                  if key in ("security_signal", "query_behavior")
                                  and entry.get("value") not in (None, "unknown", "unchanged")
                                  and entry.get("state") in ("unsupported", "contradicted")}
                    if node["investigation_state"] in ("done", "incomplete") or node["exploration_state"] == "blocked":
                        followup_status = "inconclusive"
                    elif node["attempts"] or node["exploration_state"] != "discovered":
                        followup_status = "investigated"
                    else:
                        followup_status = "queued"
                    unverified_leads.append({"node_id": node["id"], "source_node_id": attempt["node_id"],
                        "attempt_id": attempt_id, "locator": node["locator"],
                        "hypothesis_type": node["hypothesis"]["type"],
                        "unverified_signal": node["unverified_signal"],
                        "verification_state": "inconclusive", "followup_status": followup_status,
                        "fields": unresolved, "capture_request_ids": list(node["capture_request_ids"]),
                        "claim_refs": [attempt["result_ref"]] if attempt.get("result_ref") else []})
        else:
            evidence = [ref for node in nodes for ref in node["evidence_refs"]]
            candidate_leads = _candidate_leads(state, executed)
    elif not audited:
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
    if audited and candidate_leads:
        summary += "\n미확인 후보 " + ", ".join(lead["node_id"] for lead in candidate_leads[:5])
        if len(candidate_leads) > 5:
            summary += f" 외 {len(candidate_leads) - 5}개"
    elif candidate_leads:
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
    if unverified_leads:
        summary += "\n확인 필요 단서: " + ", ".join(item["node_id"] for item in unverified_leads[:10])
    observed_http, observed_truncated = ([], False) if degraded else observed_http_section(events)
    if observed_http or observed_truncated:
        summary += f"\n브라우저/MITM 미검증 관측 {len(observed_http)}건 (검증·점수 무관)"
        if observed_truncated:
            summary += " · 일부 생략"
    report = {"schema_version": 1, "run_id": log.run_id, "status": status, "run_result": run_result,
              "stop_reason": reason, "summary": summary, "stats": stats, "unexplored_high_risk": high,
              "incomplete_nodes": gaps, "candidate_leads": candidate_leads,
              "seed_metrics": None if degraded else seed_metrics(events, state, log.metadata),
              "evidence_refs": evidence, "usage": usage, "last_snapshot_usage": log.metadata.get("v4_usage") if degraded else None,
              "last_readable_seq": events[-1]["seq"] if events else 0, "corruption": corruption, "error": error}
    report["observed_http"] = observed_http
    report["observed_http_truncated"] = observed_truncated
    archive = None
    if audited:
        report["observation_policy"] = policy
        if policy in POLICIES:
            report["unverified_leads"] = unverified_leads
        report["http_audit"] = empty_audit()
        if policy in POLICIES:
            captures = list(state.get("http_exchanges", {}).values()) if not degraded else []
            capture_status = "unavailable" if degraded else "recorded" if captures else "not_recorded"
            report["http_audit"] = {"source": "harness_http", "status": capture_status,
                                    "exchanges": captures}
        if policy == AUDIT_PROJECTION_VERSION and not degraded and log.metadata.get("phase") == "finished":
            archive = finalized_archive(events)
    if archive:
        with audit_writer(log.run_dir, archive):
            try:
                report["http_audit"] = checked_audit(log.run_dir, archive, state, log.metadata["target"])
            except (OSError, KeyError, TypeError, ValueError):
                report["http_audit"]["status"] = "unavailable"
            atomic_write_json(log.run_dir / "report.json", report)
    else:
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
        final = {"run_result": run_result, "reason": reason}
        if report.get("observation_policy") == AUDIT_PROJECTION_VERSION:
            # Pick the operator archive only after runtime cleanup has completed.
            final["http_audit_id"] = uuid.uuid4().hex
        log.append("run_finalized", final)
        _metadata(log, phase="finished", log_status="finalized", cleanup_error="", finished_at=time.time())
    except Exception as exc:
        if log.metadata.get("lab_reset_status") == "running":
            _metadata(log, lab_reset_status="failed")
        _metadata(log, phase="recovery_required", cleanup_error=f"{type(exc).__name__}: {exc}"[:500],
                  cleanup_status="failed" if runtime.inspect(log.run_id)["runtime_exists"] else log.metadata.get("cleanup_status", "failed"),
                  log_status="failed" if log.failed else log.metadata.get("log_status", "collecting"))
    finally:
        log.close()
