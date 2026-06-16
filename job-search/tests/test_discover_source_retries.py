import argparse
import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "job_search.py"


def load_job_search(private_root: Path):
    os.environ["JOB_SEARCH_PRIVATE_DIR"] = str(private_root)
    module_name = f"job_search_under_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DiscoverSourceRetryTest(unittest.TestCase):
    def test_failed_source_retries_and_records_recovery_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            (private_root / "data").mkdir()
            (private_root / "profile.json").write_text(
                json.dumps(
                    {
                        "targets": {"roles": ["software engineer"], "keywords": ["python"]},
                        "dealbreakers": {},
                        "work_authorization": {"requires_sponsorship": False},
                    }
                ),
                encoding="utf-8",
            )
            (private_root / "data" / "sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "company": "SlowCo",
                                "platform": "custom",
                                "url": "https://example.com/jobs",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (private_root / "data" / "applications.json").write_text('{"applications": []}\n', encoding="utf-8")
            (private_root / "data" / "seen_jobs.json").write_text('{"jobs": {}}\n', encoding="utf-8")

            job_search = load_job_search(private_root)
            args = argparse.Namespace(
                person="default",
                track=None,
                source_company=None,
                since_hours=24,
                since_days=None,
                include_unknown_posted_date=False,
                no_role_filter=False,
                score=False,
                source_timeout_seconds=30,
                source_retries=1,
                source_retry_timeout_seconds=90,
            )

            with mock.patch.object(
                job_search,
                "source_candidates_subprocess",
                side_effect=[RuntimeError("temporary source failure"), ([], "")],
            ) as run_source:
                job_search.command_discover_jobs(args)

            self.assertEqual(run_source.call_count, 2)
            self.assertEqual(run_source.call_args_list[0].args[2], 30)
            self.assertEqual(run_source.call_args_list[1].args[2], 90)

            reports = sorted((private_root / "data" / "discovery_runs").glob("*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["totals"]["failed_sources"], 0)
            self.assertEqual(report["totals"]["retried_sources"], 1)
            self.assertEqual(report["totals"]["retry_recovered_sources"], 1)

            source = report["sources"][0]
            self.assertEqual(source["status"], "retry_success")
            self.assertEqual(source["result_status"], "searched_no_jobs_returned")
            self.assertEqual(source["retry_attempts"], 1)
            self.assertEqual([attempt["status"] for attempt in source["attempts"]], ["failed", "success"])
            self.assertIn("temporary source failure", source["attempts"][0]["error"])

    def test_source_failed_after_retries_is_marked_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            (private_root / "data").mkdir()
            (private_root / "profile.json").write_text(
                json.dumps(
                    {
                        "targets": {"roles": ["software engineer"], "keywords": ["python"]},
                        "dealbreakers": {},
                        "work_authorization": {"requires_sponsorship": False},
                    }
                ),
                encoding="utf-8",
            )
            (private_root / "data" / "sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "company": "DownCo",
                                "platform": "custom",
                                "url": "https://example.com/jobs",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (private_root / "data" / "applications.json").write_text('{"applications": []}\n', encoding="utf-8")
            (private_root / "data" / "seen_jobs.json").write_text('{"jobs": {}}\n', encoding="utf-8")

            job_search = load_job_search(private_root)
            args = argparse.Namespace(
                person="default",
                track=None,
                source_company=None,
                since_hours=24,
                since_days=None,
                include_unknown_posted_date=False,
                no_role_filter=False,
                score=False,
                source_timeout_seconds=30,
                source_retries=1,
                source_retry_timeout_seconds=90,
            )

            with mock.patch.object(
                job_search,
                "source_candidates_subprocess",
                side_effect=[RuntimeError("first failure"), RuntimeError("second failure")],
            ) as run_source:
                job_search.command_discover_jobs(args)

            self.assertEqual(run_source.call_count, 2)

            reports = sorted((private_root / "data" / "discovery_runs").glob("*.json"))
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["totals"]["failed_sources"], 1)
            self.assertEqual(report["totals"]["retried_sources"], 1)
            self.assertEqual(report["totals"]["retry_recovered_sources"], 0)
            self.assertEqual(report["totals"]["failed_after_retries"], 1)

            source = report["sources"][0]
            self.assertEqual(source["status"], "failed_after_retries")
            self.assertEqual(source["retry_attempts"], 1)
            self.assertEqual([attempt["status"] for attempt in source["attempts"]], ["failed", "failed"])
            self.assertIn("second failure", source["error"])


if __name__ == "__main__":
    unittest.main()
