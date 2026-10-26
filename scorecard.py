"""Read-only quality scorecard for exploration runs.

Scores each run under runs/<run_id>/ from its saved session.json (and events.jsonl for
compaction counts). It never touches the lab, browser, Ollama, or the controller lease, so it is
safe to run while an experiment is in progress — an in-flight run with a partial/locked session.json
is simply skipped.

The composite score is a convenience for A/B comparison; the raw columns are the real signal. Weights
live in SCORE_WEIGHTS and are meant to be tuned.

Usage (from the project folder):
    python scorecard.py                     # table of every finished run, newest last
    python scorecard.py <run_id|prefix>     # per-step detail for one run
    python scorecard.py --group model       # aggregate mean/stdev grouped by a field
    python scorecard.py --group num_ctx     # ... or by context size, goal, scenario_source, model
    python scorecard.py --json              # machine-readable per-run metrics
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

RUNS_ROOT = Path(__file__).resolve().parent / "runs"

# Composite-score weights (sum to 1.0). Tune freely; the raw metrics below do not depend on them.
SCORE_WEIGHTS = {"evidence_rate": 0.5, "non_waste_rate": 0.3, "clean_finish": 0.2}


def _num_ctx(session: dict) -> int | None:
    top = session.get("num_ctx")
    if isinstance(top, int):
        return top
    opts = (session.get("model_settings") or {}).get("options") or {}
    value = opts.get("num_ctx")
    return value if isinstance(value, int) else None


def _count_compactions(run_dir: Path) -> int | None:
    """Count context_compacted events; None if the log is unreadable (e.g. an in-flight run)."""
    events = run_dir / "events.jsonl"
    if not events.exists():
        return None
    try:
        count = 0
        with events.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("kind") == "context_compacted":
                        count += 1
                except json.JSONDecodeError:
                    continue
        return count
    except OSError:
        return None


def score_run(session: dict, run_dir: Path) -> dict:
    """Metrics for one run from its session dict. Pure; tolerant of missing/older fields."""
    findings = session.get("findings") or []
    steps = len(findings)
    # Runs from before the evidence flag landed have no "evidence" key at all; their evidence/
    # inconclusive split is not trustworthy, so mark them and keep them out of quality headlines.
    instrumented = any("evidence" in f for f in findings)
    evidence = sum(1 for f in findings if f.get("evidence"))
    inconclusive = sum(1 for f in findings if f.get("status") == "ok" and not f.get("evidence"))
    failed = sum(1 for f in findings if f.get("status") in ("error", "timeout"))
    repeats = sum(1 for f in findings if f.get("repeat_of"))
    # Coverage proxy: distinct evidence-bearing observations (dwelling on one page does not add here).
    unique_obs = len({f.get("observed") for f in findings if f.get("evidence")})
    result = session.get("run_result")
    clean_finish = result == "completed"

    evidence_rate = evidence / steps if steps else 0.0
    non_waste_rate = 1.0 - ((repeats + inconclusive + failed) / steps) if steps else 0.0
    score = (SCORE_WEIGHTS["evidence_rate"] * evidence_rate
             + SCORE_WEIGHTS["non_waste_rate"] * max(0.0, non_waste_rate)
             + SCORE_WEIGHTS["clean_finish"] * (1.0 if clean_finish else 0.0))

    started, finished = session.get("started_at"), session.get("finished_at")
    duration = round(finished - started) if isinstance(started, (int, float)) and isinstance(finished, (int, float)) else None

    return {
        "run_id": session.get("run_id", run_dir.name),
        "model": session.get("model"),
        "num_ctx": _num_ctx(session),
        "result": result,
        "goal": (session.get("goal") or "")[:60],
        "scenario_source": session.get("scenario_source"),
        "instrumented": instrumented,
        "steps": steps,
        "evidence": evidence,
        "inconclusive": inconclusive,
        "failed": failed,
        "repeats": repeats,
        "unique_obs": unique_obs,
        "compactions": _count_compactions(run_dir),
        "clean_finish": clean_finish,
        "duration_s": duration,
        "cost_usd": session.get("cost_usd"),
        "score": round(score, 3),
    }


def load_runs(runs_root: Path = RUNS_ROOT) -> list[dict]:
    """Every readable, finished run, oldest first by start time. In-flight/unfinished runs are skipped."""
    rows = []
    for run_dir in sorted(p.parent for p in runs_root.glob("*/session.json")):
        try:
            session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # partial write / locked while running
        if not session.get("findings") and session.get("run_result") is None:
            continue  # nothing scored yet (likely in flight)
        rows.append(score_run(session, run_dir))
    rows.sort(key=lambda r: (r["duration_s"] is None, r.get("run_id")))
    return rows


_COLS = [("run_id", 12), ("model", 12), ("num_ctx", 7), ("result", 10), ("instrumented", 5),
         ("steps", 5), ("evidence", 4), ("inconclusive", 5), ("failed", 6), ("repeats", 7),
         ("unique_obs", 4), ("compactions", 6), ("duration_s", 8), ("cost_usd", 9), ("score", 6)]


def _fmt(value: object, width: int) -> str:
    if value is None:
        value = "-"
    elif value is True:
        value = "Y"
    elif value is False:
        value = "N"
    text = str(value)
    if isinstance(value, str) and len(text) > width:
        text = text[:width]
    return text.ljust(width)


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("No finished runs found under", RUNS_ROOT)
        return
    header = "  ".join(name[:width].ljust(width) for name, width in _COLS)
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = []
        for name, width in _COLS:
            value = row["run_id"][:12] if name == "run_id" else row.get(name)
            cells.append(_fmt(value, width))
        print("  ".join(cells))
    scores = [r["score"] for r in rows]
    print("-" * len(header))
    evless = sum(1 for r in rows if r["instrumented"] and r["clean_finish"] and r["evidence"] == 0)
    legacy = sum(1 for r in rows if not r["instrumented"])
    print(f"runs={len(rows)}  score mean={statistics.mean(scores):.3f}"
          + (f"  stdev={statistics.pstdev(scores):.3f}" if len(scores) > 1 else "")
          + f"  evidence-less completed={evless} (of instrumented)"
          + (f"  ·  {legacy} legacy run(s) pre-evidence-flag" if legacy else ""))


def print_detail(rows: list[dict], query: str) -> None:
    matches = [r for r in rows if r["run_id"].startswith(query)]
    if not matches:
        print(f"No finished run matches '{query}'.")
        return
    row = matches[0]
    run_dir = RUNS_ROOT / row["run_id"]
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    print(f"run {row['run_id']}  model={row['model']}  num_ctx={row['num_ctx']}  result={row['result']}")
    print(f"goal: {session.get('goal')}")
    print(f"score={row['score']}  steps={row['steps']}  evidence={row['evidence']}  "
          f"inconclusive={row['inconclusive']}  failed={row['failed']}  repeats={row['repeats']}  "
          f"compactions={row['compactions']}  duration={row['duration_s']}s")
    print("-" * 80)
    for f in session.get("findings") or []:
        flag = "EV" if f.get("evidence") else ("!!" if f.get("status") in ("error", "timeout") else "..")
        rep = f"  <repeat of step {f['repeat_of']}>" if f.get("repeat_of") else ""
        print(f"  {f.get('step'):>2} [{flag}] {f.get('action'):<11} {str(f.get('observed'))[:70]}{rep}")
    reason = session.get("finish_reason") or session.get("report")
    if reason:
        print("-" * 80)
        print("finish/report:", str(reason)[:600])


def print_group(rows: list[dict], field: str) -> None:
    buckets: dict[object, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row.get(field), []).append(row)
    print(f"group by {field}")
    print(f"{'value':<28} {'n':>3} {'score mean':>11} {'stdev':>7} {'ev mean':>8} {'steps':>6} {'ev-less done':>12}")
    print("-" * 80)
    for value, group in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        scores = [r["score"] for r in group]
        evid = [r["evidence"] for r in group]
        steps = [r["steps"] for r in group]
        evless = sum(1 for r in group if r["instrumented"] and r["clean_finish"] and r["evidence"] == 0)
        print(f"{str(value)[:28]:<28} {len(group):>3} {statistics.mean(scores):>11.3f} "
              f"{(statistics.pstdev(scores) if len(scores) > 1 else 0):>7.3f} "
              f"{statistics.mean(evid):>8.1f} {statistics.mean(steps):>6.1f} {evless:>12}")


def main(argv: list[str]) -> int:
    rows = load_runs()
    if "--json" in argv:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if "--group" in argv:
        idx = argv.index("--group")
        field = argv[idx + 1] if idx + 1 < len(argv) else "model"
        print_group(rows, field)
        return 0
    positional = [a for a in argv if not a.startswith("--")]
    if positional:
        print_detail(rows, positional[0])
        return 0
    print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
