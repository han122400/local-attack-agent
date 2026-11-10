"""Filesystem locations rooted at the project checkout."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = PROJECT_ROOT / "runs"
PROMPTS_DIR = PROJECT_ROOT / "prompts" / "system"
ENV_PATH = PROJECT_ROOT / ".env"
DEPS_DIR = PROJECT_ROOT / ".deps"
BENCHMARKS_DIR = PROJECT_ROOT / "benchmarks"
