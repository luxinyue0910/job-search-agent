import json
import sys
import tempfile
import unittest
from pathlib import Path


JOB_SEARCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_SEARCH_ROOT))

from agents.application_qa_agent import review_application_materials
from agents.discovery_agent import build_source_health
from agents.fit_review_agent import review_fit
from agents.trace import AgentRunRecorder


class AgentWorkflowTest(unittest.TestCase):
    def test_agent_run_recorder_writes_trace_tool_calls_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "agent_runs" / "run-1"
            recorder = AgentRunRecorder(run_dir, agent="discovery_agent", input_payload={"track": "general_sde"})

            recorder.record_tool_call(
                "discover_jobs",
                {"track": "general_sde", "since_days": 7},
                {"added": 2, "failed_sources": 0},
                decision="continue",
                reason="Discovery completed successfully.",
            )
            recorder.record_decision("handoff", "Send fresh jobs to fit review.", {"fresh_jobs": 2})
            recorder.finish({"status": "success", "priority_count": 1})

            self.assertEqual(json.loads((run_dir / "input.json").read_text())["track"], "general_sde")
            trace_lines = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            tool_lines = [json.loads(line) for line in (run_dir / "tool_calls.jsonl").read_text().splitlines()]
            output = json.loads((run_dir / "output.json").read_text())

            self.assertEqual(trace_lines[0]["type"], "tool_call")
            self.assertEqual(trace_lines[1]["type"], "decision")
            self.assertEqual(tool_lines[0]["tool"], "discover_jobs")
            self.assertEqual(output["priority_count"], 1)

    def test_build_source_health_tracks_success_new_jobs_and_failures(self):
        report = {
            "finished_at": "2026-07-06T17:00:00+00:00",
            "sources": [
                {
                    "company": "HealthyCo",
                    "platform": "greenhouse",
                    "url": "https://job-boards.greenhouse.io/healthy",
                    "status": "searched_no_new_matches",
                    "health": "healthy",
                    "stats": {"added": 0},
                },
                {
                    "company": "NewJobCo",
                    "platform": "lever",
                    "url": "https://jobs.lever.co/newjob",
                    "status": "searched_with_new_matches",
                    "health": "healthy",
                    "stats": {"added": 3},
                },
                {
                    "company": "FailCo",
                    "platform": "workday",
                    "url": "https://fail.example.com",
                    "status": "failed_after_retries",
                    "health": "config_broken",
                    "failure_category": "workday_410",
                    "stats": {"added": 0},
                },
            ],
        }
        existing = {"FailCo": {"consecutive_failures": 1, "last_failure_category": "timeout"}}

        health = build_source_health(report, existing)

        self.assertEqual(health["HealthyCo"]["status"], "healthy")
        self.assertEqual(health["HealthyCo"]["consecutive_failures"], 0)
        self.assertEqual(health["HealthyCo"]["last_success"], "2026-07-06T17:00:00+00:00")
        self.assertEqual(health["NewJobCo"]["last_new_job_found"], "2026-07-06T17:00:00+00:00")
        self.assertEqual(health["FailCo"]["status"], "config_broken")
        self.assertEqual(health["FailCo"]["consecutive_failures"], 2)
        self.assertEqual(health["FailCo"]["last_failure_category"], "workday_410")

    def test_fit_review_uses_rules_before_priority_recommendation(self):
        senior = {
            "company": "Example",
            "role": "Senior Backend Engineer",
            "location": "Seattle, WA",
            "fit_score": 9.4,
            "ats_score": 82,
            "notes": "Requires 5+ years of backend engineering experience.",
        }
        junior = {
            "company": "Example",
            "role": "Software Engineer I",
            "location": "Bellevue, WA",
            "fit_score": 8.8,
            "ats_score": 75,
            "notes": "Full-stack Python and React role.",
        }

        senior_review = review_fit(senior)
        junior_review = review_fit(junior)

        self.assertEqual(senior_review["bucket"], "skip")
        self.assertIn("too_senior", senior_review["risk_flags"])
        self.assertEqual(junior_review["bucket"], "priority")
        self.assertIn("fit_score_high", junior_review["positive_signals"])

    def test_application_qa_flags_sensitive_ungrounded_claims(self):
        result = review_application_materials(
            jd_text="This role requires U.S. Person status and Python backend experience.",
            profile_text="Stella is a lawful permanent resident with Python backend and AWS experience.",
            cover_letter="I am a U.S. citizen and have built Python backend systems.",
            screening_answers="I do not require sponsorship.",
        )

        self.assertFalse(result["pass"])
        self.assertIn("sensitive_claim_not_grounded", [issue["code"] for issue in result["issues"]])


if __name__ == "__main__":
    unittest.main()
