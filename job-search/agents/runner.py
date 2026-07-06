from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.discovery_agent import DiscoveryAgent
from agents.eval_runner import run_job_fit_eval
from agents.tools import JobSearchTools


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-in-the-loop job search agent workflow runner.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    daily = subcommands.add_parser("daily", help="Run daily discovery through the agent harness.")
    daily.add_argument("--track", required=True)
    daily.add_argument("--since-days", type=float, default=7)
    daily.add_argument("--no-score", action="store_true")
    daily.add_argument("--include-maybe-backlog", action="store_true")
    daily.add_argument("--source-company", action="append", default=[])
    daily.add_argument("--workers", type=int, default=1)
    daily.add_argument("--private-dir", help="Private job-search repo path. Defaults to JOB_SEARCH_PRIVATE_DIR.")

    eval_cmd = subcommands.add_parser("eval", help="Run local agent eval suites.")
    eval_cmd.add_argument("--suite", choices=["job_fit"], required=True)
    eval_cmd.add_argument("--cases", required=True)

    args = parser.parse_args()
    if args.command == "daily":
        tools = JobSearchTools(private_root=Path(args.private_dir).expanduser() if args.private_dir else None)
        result = DiscoveryAgent(tools).run_daily(
            track=args.track,
            since_days=args.since_days,
            score=not args.no_score,
            include_maybe_backlog=args.include_maybe_backlog,
            source_company=args.source_company,
            workers=args.workers,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "eval":
        if args.suite == "job_fit":
            print(json.dumps(run_job_fit_eval(Path(args.cases)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
