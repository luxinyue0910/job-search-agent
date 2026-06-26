import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
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


def write_private_workspace(private_root: Path, sources: list[dict]):
    (private_root / "data").mkdir()
    (private_root / "resume").mkdir()
    (private_root / "profile.json").write_text(
        json.dumps(
            {
                "targets": {"roles": ["software engineer"], "keywords": ["python"]},
                "preferences": {
                    "relocation_allowed_states": ["WA", "CA"],
                    "relocation_allowed_locations": ["Seattle", "Bellevue", "San Francisco"],
                    "preferred_locations_order": ["Seattle", "Bellevue", "San Francisco"],
                },
                "dealbreakers": {},
                "work_authorization": {"requires_sponsorship": False},
            }
        ),
        encoding="utf-8",
    )
    (private_root / "data" / "sources.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
    (private_root / "data" / "applications.json").write_text('{"applications": []}\n', encoding="utf-8")
    (private_root / "data" / "seen_jobs.json").write_text('{"jobs": {}}\n', encoding="utf-8")
    (private_root / "resume" / "master_resume.md").write_text("Python AWS API automation testing software engineer\n", encoding="utf-8")


def discover_args(**overrides):
    values = {
        "person": "default",
        "track": None,
        "source_company": None,
        "since_hours": 24,
        "since_days": None,
        "include_unknown_posted_date": False,
        "include_maybe_backlog": False,
        "include_inactive_sources": False,
        "no_role_filter": False,
        "score": False,
        "source_timeout_seconds": 30,
        "source_retries": 0,
        "source_retry_timeout_seconds": 0,
        "workers": 1,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class DiscoveryCompatibilityTest(unittest.TestCase):
    def test_source_health_classifies_failures_without_replacing_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            report = {
                "status": "failed",
                "result_status": "failed",
                "error": "Could not fetch Greenhouse API for Example: HTTP Error 404: Not Found",
                "warnings": "",
                "attempts": [],
                "stats": job_search.empty_discovery_stats(),
            }
            job_search.annotate_source_health(report, {"company": "Example", "platform": "greenhouse"})

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["result_status"], "failed")
            self.assertEqual(report["health"], "config_broken")
            self.assertEqual(report["failure_category"], "greenhouse_404")

            self.assertEqual(
                job_search.source_failure_category(
                    {"company": "WorkdayCo", "platform": "workday"},
                    "Could not fetch Workday API for WorkdayCo: HTTP Error 410: Gone",
                ),
                "workday_410",
            )
            self.assertEqual(
                job_search.source_failure_category(
                    {"company": "SSLCo", "platform": "successfactors"},
                    "certificate verify failed: certificate has expired",
                ),
                "ssl_certificate",
            )
            self.assertEqual(
                job_search.source_failure_category(
                    {"company": "Meta", "platform": "meta_jobs"},
                    "Meta careers did not expose static job results",
                ),
                "meta_static_unavailable",
            )

    def test_parent_reads_payload_path_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "PayloadCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            payload_path = private_root / "payload.json"
            payload_path.write_text(
                json.dumps({"warnings": "large payload warning", "candidates": [{"company": "PayloadCo", "role": "Software Engineer", "url": "https://example.com/jobs/1"}]}),
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"payload_path": str(payload_path)}) + "\n",
                stderr="stderr warning",
            )
            with mock.patch.object(job_search.subprocess, "run", return_value=completed):
                candidates, warnings = job_search.source_candidates_subprocess(0, discover_args(), 30)

            self.assertEqual(candidates[0]["role"], "Software Engineer")
            self.assertIn("large payload warning", warnings)
            self.assertIn("stderr warning", warnings)
            self.assertFalse(payload_path.exists())

    def test_discover_source_candidates_default_stdout_remains_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "StdoutCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            output = io.StringIO()
            with mock.patch.object(
                job_search,
                "discover_source_jobs",
                return_value=[{"company": "StdoutCo", "role": "Software Engineer", "url": "https://example.com/jobs/1"}],
            ):
                with contextlib.redirect_stdout(output):
                    job_search.command_discover_source_candidates(argparse.Namespace(source_index="0", track=None, payload_file_output=False))

            payload = json.loads(output.getvalue())
            self.assertEqual(payload["company"], "StdoutCo")
            self.assertEqual(payload["candidates"][0]["role"], "Software Engineer")
            self.assertNotIn("payload_path", payload)

    def test_unknown_posted_at_default_skips_but_maybe_backlog_keeps_needs_review(self):
        candidate = {
            "company": "MaybeCo",
            "role": "Operations Analyst",
            "url": "https://example.com/jobs/maybe",
            "platform": "custom",
            "location": "Seattle, WA",
        }
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "MaybeCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([dict(candidate)], "")):
                job_search.command_discover_jobs(discover_args(include_maybe_backlog=False))
            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            self.assertEqual(tracker["applications"], [])

        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "MaybeCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([dict(candidate)], "")):
                job_search.command_discover_jobs(discover_args(include_maybe_backlog=True))
            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            app = tracker["applications"][0]
            self.assertEqual(app["status"], "needs_review")
            self.assertEqual(app["review_bucket"], "maybe")
            self.assertEqual(app["discovery_bucket"], "maybe_backlog")

    def test_inactive_sources_are_skipped_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "InactiveCo", "active": False, "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([], "")) as mocked:
                job_search.command_discover_jobs(discover_args())
            self.assertEqual(mocked.call_count, 0)

    def test_discover_jobs_fetches_sources_concurrently_but_processes_on_main_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(
                private_root,
                [
                    {"company": "WorkerOne", "platform": "custom", "url": "https://example.com/one"},
                    {"company": "WorkerTwo", "platform": "custom", "url": "https://example.com/two"},
                ],
            )
            job_search = load_job_search(private_root)
            main_thread = threading.current_thread()
            barrier = threading.Barrier(2, timeout=0.5)
            lock = threading.Lock()
            events: list[str] = []

            def fake_discover(source_index, args, timeout_seconds):
                with lock:
                    events.append(f"fetch_start_{source_index}")
                barrier.wait()
                with lock:
                    events.append(f"fetch_done_{source_index}")
                return (
                    [
                        {
                            "company": f"Worker{source_index}",
                            "role": "Software Engineer",
                            "url": f"https://example.com/jobs/{source_index}",
                            "platform": "custom",
                            "location": "Seattle, WA",
                            "posted_at": "2026-06-23T00:00:00+00:00",
                        }
                    ],
                    "",
                    [{"attempt": 1, "status": "success"}],
                )

            def fake_process(candidates, args, profile, seen, cutoff, current_seen_at):
                self.assertIs(threading.current_thread(), main_thread)
                with lock:
                    events.append(f"process_{candidates[0]['company']}")
                return job_search.empty_discovery_stats()

            with mock.patch.object(job_search, "discover_source_candidates_with_retries", side_effect=fake_discover):
                with mock.patch.object(job_search, "process_discovered_candidates", side_effect=fake_process):
                    job_search.command_discover_jobs(discover_args(workers=2))

            self.assertEqual(events.count("fetch_start_0"), 1)
            self.assertEqual(events.count("fetch_start_1"), 1)
            first_process_index = min(index for index, event in enumerate(events) if event.startswith("process_"))
            self.assertLess(events.index("fetch_done_0"), first_process_index)
            self.assertLess(events.index("fetch_done_1"), first_process_index)

    def test_startup_titles_match_default_discovery_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            profile = {"targets": {"roles": [], "levels": []}}
            self.assertTrue(job_search.discovery_title_matches({"role": "Founding Engineer", "url": "https://example.com"}, profile))
            self.assertTrue(job_search.discovery_title_matches({"role": "Product Engineer", "url": "https://example.com"}, profile))
            self.assertTrue(job_search.discovery_title_matches({"role": "Developer Tools Engineer", "url": "https://example.com"}, profile))

    def test_startup_jobs_adapter_reads_static_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            raw = """
            <html><body>
            <script type="application/ld+json">
            {
              "@type": "JobPosting",
              "title": "Founding Engineer",
              "url": "https://startup.example/jobs/founding-engineer",
              "datePosted": "2026-06-20",
              "hiringOrganization": {"name": "Startup Example"},
              "jobLocation": {"address": {"addressLocality": "Seattle", "addressRegion": "WA"}}
            }
            </script>
            </body></html>
            """
            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_startup_jobs({"company": "Startup Example", "platform": "startup_jobs", "url": "https://startup.example/jobs"})

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["role"], "Founding Engineer")
            self.assertEqual(candidates[0]["platform"], "startup_jobs")

    def test_year_thresholds_can_downrank_three_and_skip_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            profile = {
                "targets": {"roles": ["software engineer"], "keywords": ["python", "aws", "api"]},
                "preferences": {"relocation_allowed_states": ["WA", "CA"]},
                "dealbreakers": {"lower_weight_minimum_years_from": 3, "skip_minimum_years_from": 5},
                "work_authorization": {"requires_sponsorship": False},
            }
            app = {"company": "YearsCo", "role": "Software Engineer", "location": "Seattle, WA", "platform": "greenhouse"}

            three_year = job_search.score_text(app, "Software Engineer role requiring 3+ years with Python AWS API work.", profile)
            five_year = job_search.score_text(app, "Software Engineer role requiring 5+ years with Python AWS API work.", profile)

            self.assertNotEqual(three_year["status"], "skipped")
            self.assertIn("3+ years", " ".join(three_year["action_items"]))
            self.assertEqual(five_year["status"], "skipped")
            self.assertIn("skip threshold 5", " ".join(five_year["dealbreakers"]))

    def test_daily_review_priority_uses_strict_recommendation_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            apps = [
                {
                    "id": "good",
                    "status": "scored",
                    "company": "GoodCo",
                    "role": "Software Engineer",
                    "location": "Seattle, WA",
                    "fit_score": 9,
                    "ats_score": 70,
                },
                {
                    "id": "wrong-location",
                    "status": "scored",
                    "company": "RemoteEU",
                    "role": "Software Engineer",
                    "location": "Spain Remote",
                    "fit_score": 10,
                    "ats_score": 90,
                },
                {
                    "id": "intern",
                    "status": "scored",
                    "company": "InternCo",
                    "role": "Software Engineering Intern",
                    "location": "San Jose, CA",
                    "fit_score": 10,
                    "ats_score": 90,
                },
                {
                    "id": "three-years",
                    "status": "scored",
                    "company": "YearsCo",
                    "role": "Software Engineer",
                    "location": "San Francisco, CA",
                    "notes": "Requires 3+ years of experience.",
                    "fit_score": 10,
                    "ats_score": 90,
                },
            ]

            rows = job_search.daily_review_app_rows(apps, "priority", 8, 10)

            self.assertEqual([row["id"] for row in rows], ["good"])

    def test_daily_review_promotes_high_scoring_maybe_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            apps = [
                {
                    "id": "sorce-like",
                    "status": "scored",
                    "company": "Sorce",
                    "role": "Software Engineer, Browser Agents",
                    "location": "Remote",
                    "fit_score": 10,
                    "ats_score": 80,
                    "review_bucket": "maybe",
                    "discovery_bucket": "maybe_backlog",
                    "notes": "maybe_backlog: fuzzy_title; min_experience=Any (new grads ok)",
                },
                {
                    "id": "low-ats",
                    "status": "scored",
                    "company": "MaybeCo",
                    "role": "Backend Engineer",
                    "location": "San Francisco, CA",
                    "fit_score": 10,
                    "ats_score": 62,
                    "review_bucket": "maybe",
                    "discovery_bucket": "maybe_backlog",
                },
                {
                    "id": "three-years",
                    "status": "scored",
                    "company": "YearsCo",
                    "role": "Software Engineer",
                    "location": "Seattle, WA",
                    "fit_score": 10,
                    "ats_score": 90,
                    "review_bucket": "maybe",
                    "discovery_bucket": "maybe_backlog",
                    "notes": "Requires 3+ years of experience.",
                },
            ]

            promoted = job_search.daily_review_app_rows(apps, "promoted_maybe", 9, 10)

            self.assertEqual([row["id"] for row in promoted], ["sorce-like"])


if __name__ == "__main__":
    unittest.main()
