from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


class JobSearchTools:
    """Thin tool wrapper around the existing job_search.py engine."""

    def __init__(self, *, repo_root: Path | None = None, private_root: Path | None = None):
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[1])
        self.private_root = Path(private_root or os.environ.get("JOB_SEARCH_PRIVATE_DIR") or self.repo_root).expanduser()
        self.data_dir = self.private_root / "data"
        self.agent_runs_dir = self.data_dir / "agent_runs"
        self.source_health_path = self.data_dir / "source_health.json"
        self.discovery_runs_dir = self.data_dir / "discovery_runs"
        self.job_search_script = self.repo_root / "scripts" / "job_search.py"

    def discover_jobs(
        self,
        *,
        track: str,
        since_days: float,
        score: bool,
        include_maybe_backlog: bool,
        source_company: list[str],
        workers: int,
    ) -> dict[str, Any]:
        command = [
            sys.executable,
            str(self.job_search_script),
            "discover-jobs",
            "--track",
            track,
            "--since-days",
            str(since_days),
            "--workers",
            str(workers),
        ]
        if score:
            command.append("--score")
        if include_maybe_backlog:
            command.append("--include-maybe-backlog")
        for company in source_company:
            command.extend(["--source-company", company])
        return self.run(command)

    def run(self, command: list[str]) -> dict[str, Any]:
        env = os.environ.copy()
        env["JOB_SEARCH_PRIVATE_DIR"] = str(self.private_root)
        completed = subprocess.run(command, cwd=self.repo_root, env=env, capture_output=True, text=True, check=False)
        stdout_tail = "\n".join(completed.stdout.splitlines()[-12:])
        stderr_tail = "\n".join(completed.stderr.splitlines()[-12:])
        if completed.returncode != 0:
            raise RuntimeError(stderr_tail or stdout_tail or f"command failed with {completed.returncode}")
        return {
            "returncode": completed.returncode,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "command": command,
        }

    def latest_discovery_report(self) -> dict[str, Any]:
        reports = sorted(self.discovery_runs_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if not reports:
            raise FileNotFoundError(f"No discovery reports found under {self.discovery_runs_dir}")
        return json.loads(reports[-1].read_text(encoding="utf-8"))

    def load_source_health(self) -> dict[str, Any]:
        if not self.source_health_path.exists():
            return {}
        return json.loads(self.source_health_path.read_text(encoding="utf-8"))

    def save_source_health(self, source_health: dict[str, Any]) -> None:
        self.source_health_path.parent.mkdir(parents=True, exist_ok=True)
        self.source_health_path.write_text(json.dumps(source_health, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

