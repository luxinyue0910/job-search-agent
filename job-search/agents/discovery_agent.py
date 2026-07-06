from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.trace import AgentRunRecorder
from agents.tools import JobSearchTools


FAILURE_STATUSES = {"failed", "failed_after_retries"}


def now_run_id(track: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    safe_track = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in track or "default")
    return f"{timestamp}-{safe_track}"


def source_key(source: dict[str, Any]) -> str:
    return str(source.get("company") or source.get("url") or "unknown").strip() or "unknown"


def build_source_health(report: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    health = dict(existing or {})
    checked_at = str(report.get("finished_at") or report.get("started_at") or "")
    for source in report.get("sources", []):
        if not isinstance(source, dict):
            continue
        key = source_key(source)
        previous = dict(health.get(key) or {})
        status = str(source.get("status") or "")
        failure = status in FAILURE_STATUSES
        stats = source.get("stats") if isinstance(source.get("stats"), dict) else {}
        added = int(stats.get("added") or 0)

        record = {
            **previous,
            "company": key,
            "platform": source.get("platform", previous.get("platform", "")),
            "url": source.get("url", previous.get("url", "")),
            "last_checked": checked_at,
            "last_run_status": status,
            "last_health": source.get("health", "failed" if failure else "healthy"),
            "candidates_returned": int(source.get("candidates_returned") or previous.get("candidates_returned") or 0),
            "last_stats": stats,
        }

        if failure:
            record["status"] = source.get("health") or "failed"
            record["consecutive_failures"] = int(previous.get("consecutive_failures") or 0) + 1
            record["last_failure"] = checked_at
            record["last_failure_category"] = source.get("failure_category", "")
            record["last_error"] = source.get("error", "")
        else:
            record["status"] = "healthy"
            record["consecutive_failures"] = 0
            record["last_success"] = checked_at
            record["last_failure_category"] = ""
            record["last_error"] = ""

        if added > 0:
            record["last_new_job_found"] = checked_at

        health[key] = record
    return health


def summarize_discovery_report(report: dict[str, Any], source_health: dict[str, Any]) -> dict[str, Any]:
    totals = report.get("totals", {}) if isinstance(report.get("totals"), dict) else {}
    attention = [
        company
        for company, record in sorted(source_health.items())
        if int(record.get("consecutive_failures") or 0) > 0
    ]
    return {
        "run_id": report.get("run_id", ""),
        "track": report.get("track", ""),
        "finished_at": report.get("finished_at", ""),
        "sources_planned": totals.get("sources_planned", 0),
        "sources_attempted": totals.get("sources_attempted", 0),
        "failed_sources": totals.get("failed_sources", 0),
        "new_jobs_added": totals.get("added", 0),
        "maybe_backlog": totals.get("maybe_backlog", 0),
        "skipped_old": totals.get("skipped_old", 0),
        "skipped_title": totals.get("skipped_title", 0),
        "skipped_location": totals.get("skipped_location", 0),
        "source_health_attention": attention,
    }


def render_daily_report(summary: dict[str, Any]) -> str:
    lines = [
        f"# Agent Daily Discovery - {summary.get('track') or 'default'}",
        "",
        f"- Run ID: {summary.get('run_id')}",
        f"- Finished at: {summary.get('finished_at')}",
        f"- Sources attempted: {summary.get('sources_attempted')}/{summary.get('sources_planned')}",
        f"- Failed sources: {summary.get('failed_sources')}",
        f"- New jobs added: {summary.get('new_jobs_added')}",
        f"- Maybe backlog: {summary.get('maybe_backlog')}",
        f"- Skipped old/title/location: {summary.get('skipped_old')}/{summary.get('skipped_title')}/{summary.get('skipped_location')}",
        "",
        "## Source Health Attention",
    ]
    attention = summary.get("source_health_attention") or []
    if attention:
        lines.extend(f"- {company}" for company in attention)
    else:
        lines.append("- None")
    return "\n".join(lines)


class DiscoveryAgent:
    def __init__(self, tools: JobSearchTools):
        self.tools = tools

    def run_daily(
        self,
        *,
        track: str,
        since_days: float = 7,
        score: bool = True,
        include_maybe_backlog: bool = False,
        source_company: list[str] | None = None,
        workers: int = 1,
    ) -> dict[str, Any]:
        run_dir = self.tools.agent_runs_dir / now_run_id(track)
        input_payload = {
            "track": track,
            "since_days": since_days,
            "score": score,
            "include_maybe_backlog": include_maybe_backlog,
            "source_company": source_company or [],
            "workers": workers,
        }
        recorder = AgentRunRecorder(run_dir, agent="discovery_agent", input_payload=input_payload)

        command_result = self.tools.discover_jobs(
            track=track,
            since_days=since_days,
            score=score,
            include_maybe_backlog=include_maybe_backlog,
            source_company=source_company or [],
            workers=workers,
        )
        recorder.record_tool_call(
            "discover_jobs",
            input_payload,
            {"returncode": command_result["returncode"], "stdout_tail": command_result["stdout_tail"]},
            decision="read_report",
            reason="Discovery command completed; inspect latest structured report.",
        )

        report = self.tools.latest_discovery_report()
        existing_health = self.tools.load_source_health()
        source_health = build_source_health(report, existing_health)
        self.tools.save_source_health(source_health)
        recorder.record_tool_call(
            "update_source_health",
            {"report": report.get("run_id", "")},
            {"sources": len(source_health)},
            decision="summarize",
            reason="Source health memory updated from discovery report.",
        )

        summary = summarize_discovery_report(report, source_health)
        recorder.record_decision("human_review", "Daily discovery never submits applications automatically.", summary)
        recorder.write_report(render_daily_report(summary))
        recorder.finish(summary)
        return {"run_dir": str(run_dir), **summary}

