from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.fit_review_agent import review_fit


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_job_fit_eval(path: Path) -> dict[str, Any]:
    cases = load_jsonl(path)
    results = []
    correct = 0
    for case in cases:
        review = review_fit(case.get("job", case))
        expected = case.get("expected_bucket")
        passed = review["bucket"] == expected
        correct += int(passed)
        results.append({"id": case.get("id", ""), "expected": expected, "actual": review["bucket"], "passed": passed})
    total = len(cases)
    return {"suite": "job_fit", "total": total, "correct": correct, "accuracy": correct / total if total else 0, "results": results}

