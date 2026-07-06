from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AgentRunRecorder:
    """Write auditable agent run artifacts without owning business logic."""

    def __init__(self, run_dir: Path, agent: str, input_payload: dict[str, Any]):
        self.run_dir = Path(run_dir)
        self.agent = agent
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._write_json("input.json", input_payload)
        self.trace_path = self.run_dir / "trace.jsonl"
        self.tool_calls_path = self.run_dir / "tool_calls.jsonl"

    def record_tool_call(
        self,
        tool: str,
        input_payload: dict[str, Any],
        output_summary: dict[str, Any] | str,
        *,
        decision: str = "",
        reason: str = "",
    ) -> None:
        event = {
            "timestamp": utc_now(),
            "agent": self.agent,
            "type": "tool_call",
            "tool": tool,
            "input": input_payload,
            "output_summary": output_summary,
            "decision": decision,
            "reason": reason,
        }
        self._append_jsonl(self.trace_path, event)
        self._append_jsonl(self.tool_calls_path, event)

    def record_decision(self, decision: str, reason: str, context: dict[str, Any] | None = None) -> None:
        self._append_jsonl(
            self.trace_path,
            {
                "timestamp": utc_now(),
                "agent": self.agent,
                "type": "decision",
                "decision": decision,
                "reason": reason,
                "context": context or {},
            },
        )

    def finish(self, output_payload: dict[str, Any]) -> None:
        self._write_json("output.json", output_payload)

    def write_report(self, markdown: str) -> None:
        (self.run_dir / "report.md").write_text(markdown.rstrip() + "\n", encoding="utf-8")

    def _write_json(self, filename: str, payload: dict[str, Any]) -> None:
        (self.run_dir / filename).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

