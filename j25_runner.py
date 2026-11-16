"""J2.5 fixed-action loop and model-independent reporting."""

import copy
import json
import re
import time
from dataclasses import asdict
from urllib.parse import urlsplit, urlunsplit

from harness import Harness, ModelFailure
from j25_evidence import reduce_j25_evidence
from planning import load_prompt


def rank_j25_references(references: dict, goal: str, examined: set[str], recent: set[str]) -> dict:
    terms = set(re.findall(r"[a-z0-9]{2,}|[가-힣]{2,}", goal.casefold()))
    aliases = {"검색": ("search", "query"), "입력": ("search", "query", "input", "form"),
               "인증": ("auth", "login"),
               "권한": ("access", "permission"), "계정": ("account", "user")}
    for word, english in aliases.items():
        if word in goal:
            terms.update(english)
    def rank(pair):
        index, item = pair
        path = urlsplit(item.get("url", "")).path.casefold()
        label = path + " " + str(item.get("field", "")).casefold()
        return (item.get("supported", True) or item.get("interactable", False) or item.get("replayable", False),
                any(term in label for term in terms), not path.startswith("/socket.io/"),
                item.get("id") not in examined, item.get("id") not in recent,
                {"observed": 2, "declared": 1}.get(item.get("basis"), 0), -index)
    return {kind: [item for _, item in sorted(enumerate(items), key=rank, reverse=True)]
            for kind, items in references.items()}


def compose_j25_report(state, run_result, reason):
    lines = [f"결과: {run_result} · J2.5 고정 실행 {len(state.get('steps', []))}개 · 단계 중 기록된 요청 시도 {state.get('request_count', 0)}회"]
    ledger = state.get("evidence")
    if isinstance(ledger, dict):
        counts = ledger.get("counts") or {}
        lines.append(f"검증된 고유 사례 {counts.get('independently_verified', 0)}건 · 정보 관찰 {counts.get('observed_information', 0)}건 · "
                     f"시도 {counts.get('attempted', 0)}건 · 부분·실패 {counts.get('partial', 0) + counts.get('failed', 0)}건")
        lines.append("전체 취약점 대비 비율: 측정 불가" if ledger.get("rate") is None
                     else f"등록된 사례 대비 비율: {ledger['rate']}")
        for case in ledger.get("cases", []):
            lines.append(f"  - 사례 {case['id']} · {case['kind']} · {case['method']} {case['surface']} "
                         f"{case['field']} · {case['status']} · 실행 {', '.join(case['exec_ids'])}")
            lines.append("    전제: 허가된 대상" +
                         (f" · 시험 신원 {case['identity']}" if case.get("identity") else "") +
                         (f" · 소유자 대조 신원 {case['owner_identity']}" if case.get("owner_identity") else ""))
            sequence = ("비인가→소유자→비인가" if case["kind"] == "authorization" else
                        "정상 입력→고정 시험 입력→정상 입력" if case["kind"] == "browser_input" else
                        "기준→시험→재확인")
            lines.append(f"    시험 순서: {sequence}")
            if case.get("information"):
                lines.append("    획득 정보의 시험 표식: " + ", ".join(case["information"]))
            if case.get("proofs"):
                lines.append("    안전한 응답 증거: " + json.dumps(case["proofs"], ensure_ascii=False))
            if case.get("missing_checks"):
                lines.append("    미확인 조건: " + ", ".join(case["missing_checks"]))
            remediation = {"authorization": "서버에서 객체별 권한을 검사하고 미허가 응답을 차단",
                           "browser_input": "사용자 입력을 HTML로 해석하지 않고 안전한 DOM API로 출력",
                           "injection": "입력값을 자료로 처리하고 문맥별 매개변수화·인코딩 적용",
                           "disclosure": "비공개 데이터의 노출 경로와 응답 권한을 검사"}
            lines.append("    수정 방향: " + remediation.get(case["kind"], "노출 조건을 재현한 뒤 원인별로 수정"))
    else:
        lines.append("고유 사례 검증 기록 없음(이전 J2.5 기록은 건수를 추정하지 않음)")
    if "coverage_status" in state:
        lines.append(f"목표(모델 판단·미검증): {state.get('goal_status', 'unknown')} · 발견된 참조 범위: {state['coverage_status']}")
    lines.append("단계 사이의 백그라운드 요청과 일부 리다이렉트는 이 수에 포함되지 않을 수 있습니다.")
    refs = {item.get("id"): item for items in state.get("references", {}).values() for item in items}
    refs.update({item.get("id"): item for item in state.get("expired", [])})
    for step in state.get("steps", []):
        lines.append(f"  - [{step['exec_id']}] {step['action']} {step.get('ref', '')}: {step['status']}")
        if step.get("identity"):
            lines.append(f"    시험 신원: {step['identity']}")
        reference = step.get("reference") or refs.get(step.get("ref"), {})
        if reference:
            lines.append(f"    대상: {reference.get('url', '')} {reference.get('field', '')} · 출처 {reference.get('source', '')}")
        if step.get("hypothesis"):
            lines.append(f"    모델 가설(미검증): {step['hypothesis']}")
        if step.get("action") == "compare":
            lines.append(f"    비교 입력: 정상 {step.get('normal', '')!r} · 시험 {step.get('test', '')!r}")
        if step.get("verification"):
            lines.append("    코드 검증: " + json.dumps(step["verification"], ensure_ascii=False))
        if step.get("access_samples"):
            lines.append("    신원별 접근 표본: " + json.dumps(step["access_samples"], ensure_ascii=False))
        if step.get("navigation"):
            lines.append("    페이지 접근: " + json.dumps(step["navigation"], ensure_ascii=False))
        if step.get("response"):
            lines.append("    스크립트 응답: " + json.dumps(step["response"], ensure_ascii=False))
        if step.get("visible_inputs"):
            lines.append("    보이는 입력: " + json.dumps(step["visible_inputs"], ensure_ascii=False))
        if step.get("page_excerpt"):
            lines.append("    페이지 텍스트: " + step["page_excerpt"])
        if step.get("interaction"):
            lines.append("    화면 입력: " + json.dumps(step["interaction"], ensure_ascii=False))
        for blocked in step.get("blocked_requests", []):
            lines.append(f"    요청 차단: {blocked.get('method', '')} {blocked.get('url', '')} · {blocked.get('reason', '')}")
        comparison = step.get("comparison")
        if comparison:
            lines.append("    비교 관찰(미검증): " + json.dumps(comparison, ensure_ascii=False))
        if step.get("error"):
            lines.append(f"    실패·제한: {step['error']}")
        if step.get("truncated"):
            lines.append("    관찰 또는 분석이 잘렸습니다.")
    if state.get("pending"):
        lines.append(f"미완료 참조: 총 {len(state['pending'])}건" +
                     (" (상위 20건 표시)" if len(state["pending"]) > 20 else ""))
        for ref in state["pending"][:20]:
            item = refs.get(ref, {})
            lines.append(f"미완료: {ref} {item.get('url', '')} {item.get('field', '')} {item.get('limitation', '')}")
    if any(state.get("omitted", {}).values()):
        lines.append("일부 참조는 보존 한도로 생략됐습니다: " + json.dumps(state["omitted"], ensure_ascii=False))
    if reason:
        lines.append(("모델 종료 설명(미검증): " if state.get("model_finished") else "종료 사유: ") + reason)
    lines.append("모델 설명과 일반 응답 차이는 취약점이나 안전성의 독립 확인이 아닙니다.")
    return "\n".join(lines)


class J25Runner:
    def __init__(self, run_id, log, runtime, *, transport=None, stop_requested=None, recent_state=None):
        self.run_id, self.log, self.runtime = run_id, log, runtime
        self.stop_requested = stop_requested or (lambda: False)
        self.recent_state = recent_state if recent_state is not None else {}
        self.budgets = log.metadata["budgets"]
        self.state = {"references": {"pages": [], "assets": [], "inputs": []}, "steps": [], "expired": [],
                      "examined": [], "pending": [], "omitted": {}, "request_count": 0, "errors": 0,
                      "repeat_blocks": 0, "goal_status": "unknown", "goal_status_source": "not_assessed",
                      "identity": "public"}
        prompt, digest = load_prompt("J2.5")
        self.harness = Harness(log, transport=transport, max_calls=self.budgets["model_calls"],
            stop_requested=self.stop_requested, system_prompt=prompt, system_prompt_version="J2.5",
            system_prompt_sha256=digest, command_guard=self._guard, report_reserve=1)
        self.tool_calls = 0
        self.model_decision_seconds = 0.0
        self._focus = None

    def _focus_for_recent(self):
        if not self.state["steps"]:
            return None
        step = self.state["steps"][-1]
        if step.get("action") not in {"start", "observe"} or step.get("status") != "ok":
            return None
        matches = [item for item in self.state["references"]["inputs"]
                   if item.get("source") in {step.get("ref"), step.get("exec_id")}
                   and not urlsplit(item.get("url", "")).path.startswith("/socket.io/")]
        ordered = rank_j25_references({"inputs": matches}, self.log.metadata.get("goal", ""),
                                      set(self.state["examined"]), set())["inputs"]
        for action, flag in (("verify", "supported"), ("verify_browser", "interactable")):
            candidate = next((item for item in ordered if item.get(flag)), None)
            if candidate:
                return {"action": action, "refs": [candidate["id"]]}
        if (step.get("action") == "observe" and self.state["identity"] == "public"
                and len(self.log.metadata.get("j25_identity_names", ["public"])) > 1):
            return {"action": "verify_access", "refs": [step["ref"]]}
        return None

    def _failed_observations(self):
        failures = {}
        for step in self.state["steps"]:
            if step.get("action") != "observe":
                continue
            ref = step.get("ref")
            if step.get("status") == "ok":
                for key in list(failures):
                    if key[0] == ref:
                        failures.pop(key)
            elif step.get("error"):
                key = (ref, step["error"])
                failures[key] = failures.get(key, 0) + 1
        return {ref for (ref, _error), count in failures.items() if count >= 2}

    def _guard(self, decision, report_only):
        if decision.action == "finish":
            return
        if self._focus and (decision.action != self._focus["action"]
                            or decision.ref not in self._focus["refs"]):
            raise ValueError("Complete the focused verification before choosing another reference")
        if decision.action == "identity":
            if (decision.value not in self.log.metadata.get("j25_identity_names", ["public"])
                    or decision.value == self.state["identity"]):
                raise ValueError("Unknown or already active test identity")
            return
        recent = self.state["steps"][-2:]
        if len(recent) == 2 and all(
            ("observe" if step["action"] == "start" else step["action"]) == decision.action
            and step["ref"] == decision.ref and step["status"] == "incomplete"
            and (decision.action not in {"compare", "verify"} or (step.get("normal"), step.get("test"))
                 == (decision.normal, decision.test)) for step in recent
        ) and recent[0].get("error") == recent[1].get("error"):
            self.state["repeat_blocks"] += 1
            self.log.append("j25_repeat_blocked", {"action": decision.action, "ref": decision.ref,
                                                     "error": recent[-1].get("error", "")})
            raise ValueError("The same action failed twice with the same error; choose another action, input or finish")
        if decision.action == "observe" and decision.ref in self.state["examined"]:
            raise ValueError("Page already observed successfully; choose a new page or supported input to compare")
        if decision.action == "observe" and decision.ref in self._failed_observations():
            raise ValueError("Page failed twice; choose another page or supported input")
        if decision.action in {"compare", "verify"} and any(step["action"] == decision.action and step["status"] == "ok"
            and (step["ref"], step.get("normal"), step.get("test"), step.get("verification_kind", "")) ==
                (decision.ref, decision.normal, decision.test, getattr(decision, "kind", ""))
            for step in self.state["steps"]):
            raise ValueError("Input already compared with these values; choose new values or finish")
        kind = {"observe": "pages", "analyze": "assets", "compare": "inputs", "verify": "inputs",
                "verify_browser": "inputs",
                "verify_access": "pages",
                "interact": "inputs", "submit": "inputs", "replay": "inputs"}.get(decision.action)
        if decision.action == "submit" and not self.log.metadata.get("j25_mutation_allowed", False):
            raise ValueError("Submitting a form requires test scope")
        if kind is None or not any(item.get("id") == decision.ref and
            (item.get("interactable", False) if decision.action in {"interact", "submit", "verify_browser"} else
             item.get("replayable", False) if decision.action == "replay" else item.get("supported", True))
            for item in self.state["references"][kind]):
            raise ValueError("Reference is missing, expired or belongs to another action")

    def _ranked_references(self):
        return rank_j25_references(self.state["references"], self.log.metadata.get("goal", ""),
            set(self.state["examined"]), {step["ref"] for step in self.state["steps"][-2:]})

    def _save(self):
        self.state["evidence"] = reduce_j25_evidence(self.state["steps"], self.state["references"])
        self.state["pending"] = [item["id"] for values in self._ranked_references().values() for item in values
                                 if item["id"] not in self.state["examined"]]
        self.state["pending"] += [item["id"] for item in self.state["expired"] if item["id"] not in self.state["examined"]]
        coverage = ("unknown" if not self.state["steps"] else "partial" if
                    self.state["pending"] or self.state["errors"] or any(self.state["omitted"].values()) else "complete")
        self.state["coverage_status"] = coverage
        findings = [{"step": item["step"], "action": item["action"], "plan": item["action"],
                     "status": "ok" if item["status"] == "ok" else "error", "evidence": item["status"] == "ok",
                     "observed": f"{item['action']} {item['ref']} · {item['status']}",
                     "error": item.get("error", "")} for item in self.state["steps"]]
        metrics = {"model_calls": self.harness.calls, "model_decision_seconds": round(self.model_decision_seconds, 3),
                   "first_comparison_step": next((item["step"] for item in self.state["steps"]
                       if item["action"] in {"compare", "verify", "verify_browser"} and item["status"] == "ok"), None),
                   "repeat_blocks": self.state["repeat_blocks"]}
        self.state["metrics"] = metrics
        statuses = {"goal_status": self.state["goal_status"], "goal_status_source": self.state["goal_status_source"],
                    "coverage_status": coverage}
        self.log.update(j25_state=copy.deepcopy(self.state), findings=findings, tool_calls=self.tool_calls,
                        cost_usd=round(self.harness.total_cost, 6), **metrics, **statuses)
        self.recent_state.update(tool_calls=self.tool_calls, cost_usd=round(self.harness.total_cost, 6), **metrics, **statuses)

    def _execute(self, decision, deadline):
        action = decision["action"]
        remaining = deadline - time.monotonic()
        if remaining <= 0 or self.stop_requested():
            return
        previous = {item["id"]: item for values in self.state["references"].values() for item in values}
        execution = self.runtime.run(self.run_id, {"action": "j25", "decision": decision},
                                     call_id=decision.get("call_id", ""),
                                     timeout=min(self.budgets["command_seconds"], remaining))
        self.tool_calls += 1
        value = execution.j25_result if isinstance(execution.j25_result, dict) else {}
        references = value.get("references")
        if isinstance(references, dict) and all(isinstance(references.get(k), list) for k in ("pages", "assets", "inputs")):
            current = {item["id"] for values in references.values() for item in values}
            known_expired = {item["id"] for item in self.state["expired"]}
            for ref, item in previous.items():
                if ref not in current and ref not in self.state["examined"] and ref not in known_expired:
                    if len(self.state["expired"]) < 256:
                        self.state["expired"].append({**item, "limitation": "worker_reference_expired"})
                    else:
                        self.state["errors"] += 1
            self.state["references"] = references
        self.state["omitted"] = value.get("omitted", self.state["omitted"])
        self.state["request_count"] += value.get("request_count", 0)
        successful = (execution.exit_code == 0 and not execution.cancelled and not execution.timed_out
                      and value.get("status") == "ok" and not value.get("truncated"))
        comparison = value.get("comparison")
        comparison_error = ""
        if action in {"compare", "verify", "verify_browser"}:
            samples = comparison.get("samples", []) if isinstance(comparison, dict) else []
            if (not isinstance(samples, list) or len(samples) != 3 or
                    any(not isinstance(sample, dict) for sample in samples) or
                    [sample["stage"] if "stage" in sample else "" for sample in samples] != ["normal", "test", "recheck"]):
                comparison_error = "comparison_incomplete"
            elif comparison.get("baseline_stable") is not True or any(
                    sample.get("truncated") or sample.get("binary") for sample in samples):
                comparison_error = "comparison_unstable_or_truncated"
            if comparison_error:
                successful = False
        ref = value.get("ref", decision.get("ref", ""))
        reference = previous.get(ref) or next((item for values in self.state["references"].values()
                                               for item in values if item.get("id") == ref), {})
        if successful and ref and ref not in self.state["examined"]:
            self.state["examined"].append(ref)
        if successful and action == "identity":
            self.state["identity"] = value.get("identity", decision.get("value"))
        if not successful:
            self.state["errors"] += 1
        step = {"step": self.tool_calls, "exec_id": execution.exec_id, "call_id": decision.get("call_id", ""),
                "action": action, "ref": ref, "status": "ok" if successful else "incomplete",
                "identity": self.state["identity"],
                "reason": decision.get("reason", ""), "hypothesis": decision.get("hypothesis", ""),
                "verification_kind": decision.get("kind", ""), "verification": value.get("verification"),
                "owner_identity": decision.get("value", "") if action == "verify_access" else "",
                "access_samples": value.get("access_samples"),
                "comparison": comparison, "error": value.get("error") or comparison_error or (execution.cause_code if not successful else ""),
                "request_count": value.get("request_count", 0), "truncated": value.get("truncated", False),
                "visible_inputs": value.get("visible_inputs", []), "page_excerpt": value.get("page_excerpt", ""),
                "response": value.get("response"),
                "navigation": value.get("navigation"), "interaction": value.get("interaction"),
                "reference": reference,
                "blocked_requests": execution.blocked_requests}
        if action in {"compare", "verify"}:
            step.update(normal=decision.get("normal", ""), test=decision.get("test", ""))
        self.state["steps"].append(step)
        self.log.append("j25_observation", step)
        self.log.update(last_exec_id=execution.exec_id)
        self._save()

    def run(self):
        deadline = time.monotonic() + self.budgets["seconds"]
        if self.stop_requested():
            self._save()
            return "stopped", "User stopped the run"
        self._execute({"action": "start"}, deadline)
        if not self.state["steps"]:
            self._save()
            return "incomplete", "No time available for the initial observation"
        durations, report_required = [], False
        while True:
            if self.stop_requested():
                return "stopped", "User stopped the run"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timed_out", "Run time budget exhausted"
            avg = sum(durations[-3:]) / len(durations[-3:]) if durations else 45.0
            finish_soon = (remaining < max(avg * 1.5, 45) or report_required
                           or self.harness.calls >= self.budgets["model_calls"] - 1
                           or self.tool_calls >= self.budgets["tool_calls"])
            parts = urlsplit(self.log.metadata["target"])
            target = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            ranked = self._ranked_references()
            blocked_pages = self._failed_observations()
            ranked["pages"] = [item for item in ranked["pages"] if item["id"] not in self.state["examined"]
                                and item["id"] not in blocked_pages]
            limits = {"pages": 8, "assets": 8, "inputs": 12}
            references = {kind: items[:limits[kind]] for kind, items in ranked.items()}
            self._focus = self._focus_for_recent() if not finish_soon else None
            if self._focus:
                focus_kind = "pages" if self._focus["action"] == "verify_access" else "inputs"
                selected = [item for item in self.state["references"][focus_kind]
                            if item["id"] in self._focus["refs"]]
                if selected:
                    references = {"pages": [], "assets": [], "inputs": []}
                    references[focus_kind] = selected
                else:
                    self._focus = None
            context_omitted = {kind: len(items) - len(references[kind]) for kind, items in ranked.items()}
            visible = {item["id"] for items in references.values() for item in items}
            context = {"goal": self.log.metadata["goal"], "target": target,
                       "identities": self.log.metadata.get("j25_identity_names", ["public"]),
                       "current_identity": self.state["identity"],
                       "mutation_allowed": self.log.metadata.get("j25_mutation_allowed", False),
                       "references": references, "reference_order": "priority", "omitted": self.state["omitted"],
                       "focus": self._focus,
                       "blocked_pages": len(blocked_pages),
                       "context_omitted": context_omitted, "recent": self.state["steps"][-2:],
                       "pending": [ref for ref in self.state["pending"] if ref in visible][:20],
                       "remaining": {"seconds": max(0, int(remaining)),
                           "model_calls": self.budgets["model_calls"] - self.harness.calls,
                           "tool_calls": self.budgets["tool_calls"] - self.tool_calls, "finish_soon": finish_soon}}
            started = time.monotonic()
            try:
                decision = self.harness.decide(self.run_id, context, model=self.log.metadata["model"],
                                               settings=self.log.metadata["model_settings"], deadline=deadline)
            except ModelFailure as exc:
                if exc.kind == "cancelled" or self.stop_requested():
                    return "stopped", "User stopped the run"
                if not finish_soon and exc.kind in {"budget", "format", "output_limit"} and self.harness.calls < self.budgets["model_calls"]:
                    report_required = True
                    continue
                self.state["errors"] += 1
                self._save()
                return "incomplete", f"Model report unavailable ({exc.kind})"
            finally:
                elapsed = time.monotonic() - started
                durations.append(elapsed)
                self.model_decision_seconds += elapsed
                self._save()
            self.log.update(plan=decision.action, reason=decision.reason, last_call_id=decision.call_id)
            if decision.action == "finish":
                self.state["model_finished"] = True
                self.state["goal_status"] = (decision.goal_status if decision.goal_status != "completed" or
                    any(step["status"] == "ok" for step in self.state["steps"]) else "unknown")
                self.state["goal_status_source"] = ("model_unverified" if self.state["goal_status"] != "unknown"
                                                    else "not_assessed")
                self._save()
                incomplete = (self.state["pending"] or self.state["errors"] or any(self.state["omitted"].values())
                              or not any(step["status"] == "ok" for step in self.state["steps"]))
                return ("incomplete" if incomplete else "completed"), decision.reason
            payload = {key: value for key, value in asdict(decision).items()
                       if value and key in {"action", "ref", "reason", "hypothesis", "normal", "test", "value", "kind", "call_id"}}
            self._execute(payload, deadline)
