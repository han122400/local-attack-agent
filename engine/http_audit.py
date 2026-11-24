"""Offline review of individually attributed HTTP/1 exchanges from an operator's recorder."""

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from engine.contracts import MAX_EVIDENCE_BYTES, normalize_url, validate_id
from engine.risk_rubric import AUDIT_PROJECTION_VERSION
from engine.session_log import ControllerLease, atomic_write_json, validate_run_id
from engine.tree import _read_bounded, _safe_path, replay_tree


def empty_audit():
    return {"source": "operator_supplied", "status": "not_recorded", "exchanges": []}


def finalized_archive(events):
    for event in reversed(events):
        if event["kind"] == "run_finalized":
            identity = event.get("http_audit_id")
            if identity is None:
                return None
            return validate_run_id(identity)
    return None


@contextmanager
def audit_writer(directory, archive):
    lock_path = _safe_path(directory, f"artifacts/http-audit/{validate_run_id(archive)}/.controller.lock")
    lease = ControllerLease(lock_path.parent)
    if not lease.acquire():
        raise ValueError("Another audit update is being published")
    try:
        yield
    finally:
        lease.release()


def _external_bytes(path, runs_root):
    source = Path(path).resolve(strict=True)
    if source.is_relative_to(runs_root) or not source.is_file():
        raise ValueError("Select a capture from outside the run store")
    with source.open("rb") as stream:
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("HTTP message exceeds the evidence size limit")
    return raw


def _header_lines(raw):
    end = raw.find(b"\r\n\r\n", 0, 64 * 1024)
    if end < 0:
        raise ValueError("Expected a complete HTTP/1 header block")
    return raw[:end].split(b"\r\n")


def _exchange_status(request, response, target, locator):
    lines = _header_lines(request)
    match = re.fullmatch(rb"([A-Z]+) (/[^\s#]*) HTTP/1\.[01]", lines[0])
    hosts = [line[5:].strip() for line in lines[1:] if line.lower().startswith(b"host:")]
    if not match or len(hosts) != 1 or not re.fullmatch(rb"[A-Za-z0-9.\-:\[\]]+", hosts[0]):
        raise ValueError("Expected an origin-form HTTP/1 request with one Host header")
    expected = urlsplit(normalize_url(locator["url"]))
    origin = urlsplit(normalize_url(target))
    url = normalize_url(f"{origin.scheme}://{hosts[0].decode('ascii')}{match[2].decode('ascii')}")
    actual = urlsplit(url)
    if (actual.scheme != expected.scheme or actual.netloc != origin.netloc or actual.netloc != expected.netloc
            or actual.path != expected.path or match[1].decode("ascii") != locator["method"]):
        raise ValueError("Request origin, method or path differs from the selected node")
    query = parse_qsl(actual.query, keep_blank_values=True, max_num_fields=1024)
    for name, value in parse_qsl(expected.query, keep_blank_values=True, max_num_fields=1024):
        if not any(key == name and (not value or item == value) for key, item in query):
            raise ValueError("Request query differs from the selected node")
    status = re.fullmatch(rb"HTTP/1\.[01] ([2-5][0-9]{2})(?: [^\r\n]*)?", _header_lines(response)[0])
    if not status:
        raise ValueError("Expected a final HTTP/1 response status line")
    return int(status[1])


def _blob_ref(archive, side, raw):
    digest = hashlib.sha256(raw).hexdigest()
    return {"path": f"artifacts/http-audit/{archive}/{side}-{digest}.http", "bytes": len(raw), "sha256": digest}


def _receipt(archive, state, target, attempt_id, request_id, field, request, response):
    validate_id(attempt_id, attempt=True)
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", request_id):
        raise ValueError("Invalid external request ID")
    if field not in (None, "response_status", "baseline_status"):
        raise ValueError("Only an explicitly attributed HTTP status can be compared")
    attempt = state["attempts"].get(attempt_id)
    if not attempt or attempt["status"] != "imported" or attempt["execution_status"] != "succeeded":
        raise ValueError("A successfully imported attempt is required")
    node = state["nodes"][attempt["node_id"]]
    status = _exchange_status(request, response, target, node["locator"])
    receipt = {"attempt_id": attempt_id, "node_id": node["id"], "exec_id": attempt["exec_id"],
               "request_id": request_id, "http_status": status,
               "request": _blob_ref(archive, "request", request), "response": _blob_ref(archive, "response", response)}
    if field:
        claimed = attempt["result"].get("observations", {}).get(field)
        support = "unsupported"
        if claimed is not None:
            support = "supported" if claimed == status else "contradicted"
        receipt["comparison"] = {"field": field, "state": support}
    return receipt


def _store_blob(directory, ref, raw):
    path = _safe_path(directory, ref["path"])
    if path.exists():
        if _read_bounded(directory, ref["path"], MAX_EVIDENCE_BYTES) != raw:
            raise ValueError("Stored audit bytes have changed")
        return
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def checked_audit(directory, archive, state, target):
    """Only previously published references can reappear. Never scan for model-written files."""
    checked = empty_audit()
    relative = f"artifacts/http-audit/{validate_run_id(archive)}/receipts.json"
    if not _safe_path(directory, relative).exists():
        return checked
    prior = json.loads(_read_bounded(directory, relative, MAX_EVIDENCE_BYTES))
    if not isinstance(prior, list):
        raise ValueError("Invalid HTTP audit receipts")
    seen = set()
    for entry in prior:
        raw = []
        for side in ("request", "response"):
            ref = entry[side]
            if not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
                raise ValueError("Invalid audit digest")
            path = f"artifacts/http-audit/{archive}/{side}-{ref['sha256']}.http"
            raw.append(_read_bounded(directory, path, MAX_EVIDENCE_BYTES))
        receipt = _receipt(archive, state, target, entry["attempt_id"], entry["request_id"],
                           entry.get("comparison", {}).get("field"), *raw)
        if receipt != entry or entry["request_id"] in seen:
            raise ValueError("Published HTTP evidence does not match its receipt")
        seen.add(entry["request_id"])
        checked["exchanges"].append(receipt)
    if seen:
        checked["status"] = "recorded"
    return checked


def import_exchange(root, run_id, attempt_id, request_id, request_path, response_path, *, field=None):
    """Attach an operator-selected pair to a finished run, without opening its journal for writing."""
    from engine.reporting import SnapshotLog, readable_prefix

    root = Path(root).resolve()
    directory = _safe_path(root, validate_run_id(run_id))
    metadata = json.loads(_safe_path(directory, "session.json").read_bytes())
    if (metadata.get("run_id") != run_id or metadata.get("experiment_mode") != "v4"
            or metadata.get("phase") != "finished" or metadata.get("log_status") != "finalized"
            or metadata.get("cleanup_status") != "succeeded" or metadata.get("managed_processes")
            or (directory / "runtime").exists()
            or (metadata.get("scorer_settings") or {}).get("reevaluation_policy") != AUDIT_PROJECTION_VERSION):
        raise ValueError("A finalized v4.6 audit-policy run with completed cleanup is required")
    events, corruption = readable_prefix(SnapshotLog(root, metadata))
    archive = finalized_archive(events)
    if corruption or archive is None:
        raise ValueError("Run journal has no intact finalized audit identity")
    state = replay_tree(events, run_id)
    request = _external_bytes(request_path, root)
    response = _external_bytes(response_path, root)
    receipt = _receipt(archive, state, metadata["target"], attempt_id, request_id, field, request, response)

    with audit_writer(directory, archive):
        report_path = _safe_path(directory, "report.json")
        report = json.loads(report_path.read_bytes())
        if (report.get("run_id") != run_id or report.get("observation_policy") != AUDIT_PROJECTION_VERSION
                or report.get("status") != "complete"):
            raise ValueError("A complete matching audit-policy report is required")
        audit = checked_audit(directory, archive, state, metadata["target"])
        for existing in audit["exchanges"]:
            if existing["request_id"] == request_id:
                if existing != receipt:
                    raise ValueError("Request ID is already bound to different evidence")
                break
        else:
            audit["exchanges"].append(receipt)
            if len(json.dumps(audit["exchanges"], ensure_ascii=False, indent=2).encode("utf-8")) > MAX_EVIDENCE_BYTES:
                raise ValueError("Audit receipt index exceeds the evidence size limit")
            _store_blob(directory, receipt["request"], request)
            _store_blob(directory, receipt["response"], response)
            audit["status"] = "recorded"
            manifest = _safe_path(directory, f"artifacts/http-audit/{archive}/receipts.json")
            atomic_write_json(manifest, audit["exchanges"])
        # Rebuild this projection even on retry after a successful manifest write.
        report["http_audit"] = audit
        atomic_write_json(report_path, report)
        return receipt
