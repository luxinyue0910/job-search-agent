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
        "maybe_old_posted_date": False,
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
    def test_write_json_atomically_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            path = Path(tmp) / "data.json"
            path.write_text('{"old": true}\n', encoding="utf-8")

            job_search.write_json(path, {"new": [1, 2, 3]})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": [1, 2, 3]})
            self.assertFalse(list(Path(tmp).glob(".data.json.*.tmp")))

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

    def test_old_posted_at_can_enter_maybe_backlog_only_when_newly_seen(self):
        candidate = {
            "company": "OldButNewCo",
            "role": "Software Engineer",
            "url": "https://example.com/jobs/old-but-new",
            "platform": "custom",
            "location": "Seattle, WA",
            "posted_at": "2020-01-01T00:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "OldButNewCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([dict(candidate)], "")):
                job_search.command_discover_jobs(discover_args(include_maybe_backlog=False, maybe_old_posted_date=False))
            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            self.assertEqual(tracker["applications"], [])

        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "OldButNewCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([dict(candidate)], "")):
                job_search.command_discover_jobs(discover_args(include_maybe_backlog=True, maybe_old_posted_date=True))
            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            app = tracker["applications"][0]
            self.assertEqual(app["status"], "needs_review")
            self.assertEqual(app["review_bucket"], "maybe")
            self.assertEqual(app["discovery_bucket"], "maybe_backlog")
            self.assertIn("old_posted_at_new_to_us", app["notes"])

            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([dict(candidate)], "")):
                job_search.command_discover_jobs(discover_args(include_maybe_backlog=True, maybe_old_posted_date=True))
            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            self.assertEqual(len(tracker["applications"]), 1)

    def test_inactive_sources_are_skipped_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "InactiveCo", "active": False, "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([], "")) as mocked:
                job_search.command_discover_jobs(discover_args())
            self.assertEqual(mocked.call_count, 0)

    def test_warning_only_source_failure_counts_in_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "WarnCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            warning = "Could not fetch custom source for WarnCo: HTTP Error 403: Forbidden"
            with mock.patch.object(
                job_search,
                "discover_source_candidates_with_retries",
                return_value=([], warning, [{"attempt": 1, "status": "success", "error": ""}]),
            ):
                job_search.command_discover_jobs(discover_args())

            report_path = next((private_root / "data" / "discovery_runs").glob("*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["sources"][0]["status"], "failed")
            self.assertEqual(report["totals"]["failed_sources"], 1)

    def test_parser_fallback_warning_does_not_make_empty_source_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            warning = "Could not parse RSS feed for Adidas with XML parser, using fallback: No module named expat; use SimpleXMLTreeBuilder instead"

            self.assertEqual(
                job_search.discovery_source_status(0, job_search.empty_discovery_stats(), warning),
                "searched_no_jobs_returned",
            )

            report = {
                "status": "searched_no_jobs_returned",
                "result_status": "searched_no_jobs_returned",
                "error": "",
                "warnings": warning,
                "attempts": [],
                "stats": job_search.empty_discovery_stats(),
            }
            job_search.annotate_source_health(report, {"company": "Adidas", "platform": "rss"})
            self.assertEqual(report["health"], "success_no_new")
            self.assertEqual(report["failure_category"], "")

    def test_governmentjobs_adapter_parses_listing_and_newprint_opening_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "King County",
                "platform": "governmentjobs",
                "url": "https://www.governmentjobs.com/careers/kingcounty",
                "agency": "kingcounty",
                "keywords": ["developer"],
                "max_pages": 1,
                "max_detail_pages": 1,
            }
            listing_html = """
            <ul class="unstyled search-results-listing-container job-listing-container">
              <li class="list-item" data-job-id="5393100">
                <h3 class="job-item-link-container">
                  <a class="item-details-link" data-department-name="DES - Executive Services" href="/careers/kingcounty/jobs/5393100/erp-software-developer-principal">ERP Software Developer-Principal</a>
                </h3>
                <ul class="list-meta">
                  <li>Chinook Building 401 5th Avenue Seattle, WA</li>
                  <li>Career Service (Exec) <span>-</span> $139,552.19 - $176,890.48 Annually</li>
                  <li class="categories-list">Category: IT and Computers</li>
                  <li>Department: DES - Executive Services</li>
                </ul>
                <div class="list-entry">Supports enterprise Oracle BI platforms and ETL processes.</div>
                <div class="list-published"><span class="list-entry-starts"><span>Posted 2 weeks ago</span></span></div>
              </li>
            </ul>
            """
            newprint_html = """
            <h1 class="job-title" aria-label="Job title -ERP Software Developer-Principal">ERP Software Developer-Principal</h1>
            <div class="span4"><div class="term-description">LOCATION</div></div><div class="span8"><p>Chinook Building - 401 5th Ave, Seattle, WA</p></div>
            <div class="span4"><div class="term-description">JOB NUMBER</div></div><div class="span8"><p>2026BM27152</p></div>
            <div class="span4"><div class="term-description">OPENING DATE</div></div><div class="span8"><p>06/29/2026</p></div>
            <div class="span4"><div class="term-description">CLOSING DATE</div></div><div class="span8"><p>7/20/2026 11:59 PM Pacific</p></div>
            <div>Full job text with SQL, troubleshooting, and application support.</div>
            """

            def fake_fetch(url, timeout=20):
                if "/jobs/newprint/5393100" in url:
                    return newprint_html
                raise AssertionError(f"Unexpected detail fetch: {url}")

            def fake_fetch_search(url, source_arg, timeout=20):
                self.assertIn("/careers/home/index", url)
                self.assertIn("agency=kingcounty", url)
                self.assertEqual(source_arg["company"], "King County")
                return listing_html

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                with mock.patch.object(job_search, "fetch_governmentjobs_search", side_effect=fake_fetch_search):
                    candidates = job_search.discover_governmentjobs_jobs(source)

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["company"], "King County")
            self.assertEqual(candidate["role"], "ERP Software Developer-Principal")
            self.assertEqual(candidate["location"], "Chinook Building - 401 5th Ave, Seattle, WA")
            self.assertEqual(candidate["job_number"], "2026BM27152")
            self.assertEqual(candidate["posted_at"], "2026-06-29T00:00:00+00:00")
            self.assertEqual(candidate["source_query"], "developer")
            self.assertIn("/careers/kingcounty/jobs/5393100/erp-software-developer-principal", candidate["url"])
            self.assertEqual(candidate["freshness_source"], "governmentjobs_newprint_opening_date")

    def test_governmentjobs_adapter_uses_listing_relative_posted_date_without_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "King County",
                "platform": "governmentjobs",
                "url": "https://www.governmentjobs.com/careers/kingcounty",
                "agency": "kingcounty",
                "keywords": ["systems analyst"],
                "max_pages": 1,
                "max_detail_pages": 0,
            }
            listing_html = """
            <ul class="unstyled search-results-listing-container job-listing-container">
              <li class="list-item" data-job-id="5400000">
                <h3><a class="item-details-link" href="/careers/kingcounty/jobs/5400000/systems-analyst">Systems Analyst</a></h3>
                <ul class="list-meta"><li>Seattle, WA</li></ul>
                <div class="list-entry">Supports enterprise applications.</div>
                <div class="list-published"><span class="list-entry-starts"><span>Posted today</span></span></div>
              </li>
            </ul>
            """

            with mock.patch.object(job_search, "fetch_governmentjobs_search", return_value=listing_html):
                with mock.patch.object(job_search, "fetch_url", side_effect=AssertionError("detail fetch should not run")):
                    candidates = job_search.discover_governmentjobs_jobs(source)

            self.assertEqual(len(candidates), 1)
            self.assertTrue(candidates[0]["posted_at"])
            self.assertEqual(candidates[0]["freshness_source"], "governmentjobs_listing_relative_posted_at")

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

    def test_extract_years_ignores_age_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            self.assertEqual(job_search.extract_years("Applicants must be 18+ years old."), [])
            self.assertEqual(job_search.extract_year_requirements("Applicants must be 18+ years old."), [])
            self.assertEqual(job_search.extract_years("Must be at least 21 years of age."), [])
            self.assertEqual(job_search.extract_year_requirements("Must be at least 21 years of age."), [])
            self.assertEqual(job_search.extract_years("Requires 3+ years of software development experience."), [3])
            self.assertEqual(job_search.extract_year_requirements("Requires 2-5 years of software development experience.")[0]["min"], 2)
            self.assertEqual(job_search.extract_years("Requires 1-3 years of IT support experience."), [1])
            self.assertEqual(job_search.extract_month_requirements("Requires six (6) months of software experience."), [6])
            self.assertEqual(job_search.extract_month_requirements("Requires six (6)&nbsp;months of software experience."), [6])

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
                {
                    "id": "range-years",
                    "status": "scored",
                    "company": "RangeCo",
                    "role": "Software Engineer",
                    "location": "San Francisco, CA",
                    "notes": "1-3 years of experience preferred.",
                    "fit_score": 9,
                    "ats_score": 80,
                },
                {
                    "id": "sde-iii",
                    "status": "scored",
                    "company": "LevelCo",
                    "role": "Software Development Engineer III",
                    "location": "Seattle, WA",
                    "fit_score": 10,
                    "ats_score": 90,
                },
                {
                    "id": "sde-3",
                    "status": "scored",
                    "company": "LevelCo",
                    "role": "SDE 3",
                    "location": "Seattle, WA",
                    "fit_score": 10,
                    "ats_score": 90,
                },
                {
                    "id": "phd",
                    "status": "scored",
                    "company": "AcademicCo",
                    "role": "Machine Learning Engineer, New Grad PhD",
                    "location": "Bellevue, WA",
                    "fit_score": 10,
                    "ats_score": 90,
                },
                {
                    "id": "dc",
                    "status": "scored",
                    "company": "DCCo",
                    "role": "Software Engineer",
                    "location": "Washington, District of Columbia, United States",
                    "fit_score": 10,
                    "ats_score": 90,
                },
            ]

            rows = job_search.daily_review_app_rows(apps, "priority", 8, 10)

            self.assertEqual([row["id"] for row in rows], ["range-years", "good"])

    def test_daily_review_prioritizes_entry_level_and_caps_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            apps = [
                {
                    "id": "two-plus",
                    "status": "scored",
                    "company": "TwoPlusCo",
                    "role": "Software Engineer II",
                    "location": "Seattle, WA",
                    "notes": "Requires 2+ years of experience.",
                    "fit_score": 10,
                    "ats_score": 100,
                },
                {
                    "id": "two-five",
                    "status": "scored",
                    "company": "RangeCo",
                    "role": "Software Engineer",
                    "location": "Seattle, WA",
                    "notes": "Requires 2-5 years of experience.",
                    "fit_score": 10,
                    "ats_score": 100,
                },
                {
                    "id": "one-two",
                    "status": "scored",
                    "company": "SmallCo",
                    "role": "Software Engineer",
                    "location": "Seattle, WA",
                    "notes": "Requires 1-2 years of experience.",
                    "fit_score": 8.5,
                    "ats_score": 75,
                },
                {
                    "id": "new-grad",
                    "status": "scored",
                    "company": "GradCo",
                    "role": "Software Engineer, New Grad",
                    "location": "Bellevue, WA",
                    "fit_score": 8.1,
                    "ats_score": 70,
                },
            ]
            for index in range(5):
                apps.append(
                    {
                        "id": f"bigco-extra-{index}",
                        "status": "scored",
                        "company": "BigCo",
                        "role": "Software Engineer",
                        "location": "Seattle, WA",
                        "notes": "Entry level role; 0-1 years welcome.",
                        "fit_score": 9,
                        "ats_score": 80,
                    }
                )

            rows = job_search.daily_review_app_rows(apps, "priority", 8, 20)

            ids = [row["id"] for row in rows]
            self.assertLess(ids.index("new-grad"), ids.index("two-plus"))
            self.assertLess(rows.index(next(row for row in rows if row["id"] == "two-plus")), rows.index(next(row for row in rows if row["id"] == "two-five")))
            self.assertLess(rows.index(next(row for row in rows if row["id"] == "one-two")), rows.index(next(row for row in rows if row["id"] == "two-plus")))
            self.assertEqual(sum(1 for row in rows if row["company"] == "BigCo"), 3)

    def test_experience_bucket_can_use_existing_jd_path_without_rescoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            jd_path = Path(tmp) / "jd.md"
            jd_path.write_text("Basic Qualifications: 2+ years of software development experience.", encoding="utf-8")
            app = {
                "id": "existing-amazon",
                "status": "scored",
                "company": "Amazon",
                "role": "Machine Learning Engineer",
                "location": "Seattle, WA",
                "fit_score": 9,
                "ats_score": 80,
                "jd_path": str(jd_path),
            }

            self.assertEqual(job_search.experience_requirement_bucket(app), "2_plus")

    def test_experience_bucket_treats_month_requirements_as_entry_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            jd_path = Path(tmp) / "jd.md"
            jd_path.write_text("You have a Master’s degree plus three (3) months of related experience.", encoding="utf-8")
            app = {
                "id": "existing-glean",
                "status": "scored",
                "company": "Glean",
                "role": "Software Engineer",
                "location": "Mountain View, CA",
                "fit_score": 10,
                "ats_score": 82,
                "jd_path": str(jd_path),
            }

            self.assertEqual(job_search.experience_requirement_bucket(app), "0_1")

    def test_experience_bucket_does_not_treat_page_nav_apprenticeship_as_new_grad(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            jd_path = Path(tmp) / "jd.md"
            jd_path.write_text(
                "Careers Engineering Apprenticeship Internship Programs. Your Expertise: 2-4+ years of industry experience.",
                encoding="utf-8",
            )
            app = {
                "id": "airbnb-dev-tools",
                "status": "scored",
                "company": "Airbnb",
                "role": "Software Engineer, Dev Tools",
                "location": "Remote - USA",
                "fit_score": 9.2,
                "ats_score": 71,
                "jd_path": str(jd_path),
            }

            self.assertEqual(job_search.experience_requirement_bucket(app), "2_range")

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

    def test_daily_review_retry_omits_obvious_senior_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            apps = [
                {
                    "id": "retry-good",
                    "status": "needs_retry",
                    "company": "Rubrik",
                    "role": "Software Engineer, Developer Productivity",
                    "location": "Palo Alto, CA",
                },
                {
                    "id": "retry-iii",
                    "status": "needs_retry",
                    "company": "Chewy",
                    "role": "Data Engineer III",
                    "location": "Bellevue, WA",
                },
            ]

            retry = job_search.daily_review_app_rows(apps, "retry", 0, 10)

            self.assertEqual([row["id"] for row in retry], ["retry-good"])

    def test_discovery_reports_for_date_defaults_to_latest_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            reports_dir = private_root / "data" / "discovery_runs"
            reports_dir.mkdir(parents=True)
            old_report = {
                "run_id": "2026-07-06T17-11-46Z",
                "started_at": "2026-07-06T17:11:46+00:00",
                "finished_at": "2026-07-06T17:11:49+00:00",
                "sources": [{"company": "OldDNSFailure", "status": "failed"}],
            }
            latest_report = {
                "run_id": "2026-07-06T17-12-22Z",
                "started_at": "2026-07-06T17:12:22+00:00",
                "finished_at": "2026-07-06T17:40:23+00:00",
                "sources": [{"company": "LatestRun", "status": "searched_no_new_matches"}],
            }
            (reports_dir / "2026-07-06T17-11-46Z.json").write_text(json.dumps(old_report), encoding="utf-8")
            (reports_dir / "2026-07-06T17-12-22Z.json").write_text(json.dumps(latest_report), encoding="utf-8")

            reports = job_search.discovery_reports_for_date("2026-07-06")

            self.assertEqual([report["run_id"] for report in reports], ["2026-07-06T17-12-22Z"])

    def test_discovery_reports_for_date_prefers_broad_report_over_later_limited_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            reports_dir = private_root / "data" / "discovery_runs"
            reports_dir.mkdir(parents=True)
            broad_report = {
                "run_id": "2026-07-06T17-12-22Z",
                "started_at": "2026-07-06T17:12:22+00:00",
                "totals": {"sources_attempted": 364},
                "sources": [],
            }
            limited_report = {
                "run_id": "2026-07-06T17-55-11Z",
                "started_at": "2026-07-06T17:55:11+00:00",
                "totals": {"sources_attempted": 2},
                "sources": [],
            }
            (reports_dir / "2026-07-06T17-12-22Z.json").write_text(json.dumps(broad_report), encoding="utf-8")
            (reports_dir / "2026-07-06T17-55-11Z.json").write_text(json.dumps(limited_report), encoding="utf-8")

            reports = job_search.discovery_reports_for_date("2026-07-06")

            self.assertEqual([report["run_id"] for report in reports], ["2026-07-06T17-12-22Z"])

    def test_discovery_reports_for_date_prefers_latest_nearly_full_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            reports_dir = private_root / "data" / "discovery_runs"
            reports_dir.mkdir(parents=True)
            old_full_report = {
                "run_id": "2026-07-06T17-12-22Z",
                "started_at": "2026-07-06T17:12:22+00:00",
                "totals": {"sources_attempted": 364},
                "sources": [],
            }
            new_full_report = {
                "run_id": "2026-07-06T18-00-36Z",
                "started_at": "2026-07-06T18:00:36+00:00",
                "totals": {"sources_attempted": 359},
                "sources": [],
            }
            limited_report = {
                "run_id": "2026-07-06T18-45-00Z",
                "started_at": "2026-07-06T18:45:00+00:00",
                "totals": {"sources_attempted": 2},
                "sources": [],
            }
            for report in [old_full_report, new_full_report, limited_report]:
                (reports_dir / f"{report['run_id']}.json").write_text(json.dumps(report), encoding="utf-8")

            reports = job_search.discovery_reports_for_date("2026-07-06")

            self.assertEqual([report["run_id"] for report in reports], ["2026-07-06T18-00-36Z"])

    def test_discovery_reports_for_date_can_include_all_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            reports_dir = private_root / "data" / "discovery_runs"
            reports_dir.mkdir(parents=True)
            for run_id in ["2026-07-06T17-11-46Z", "2026-07-06T17-12-22Z"]:
                (reports_dir / f"{run_id}.json").write_text(
                    json.dumps({"run_id": run_id, "started_at": run_id.replace("Z", "+00:00"), "sources": []}),
                    encoding="utf-8",
                )

            reports = job_search.discovery_reports_for_date("2026-07-06", latest_only=False)

            self.assertEqual([report["run_id"] for report in reports], ["2026-07-06T17-11-46Z", "2026-07-06T17-12-22Z"])

    def test_source_issue_resolved_by_later_same_day_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            failed_report = {
                "run_id": "2026-07-06T18-00-36Z",
                "started_at": "2026-07-06T18:00:36+00:00",
                "sources": [
                    {
                        "company": "Airbnb",
                        "platform": "greenhouse",
                        "status": "failed",
                        "health": "fetch_failed",
                    }
                ],
            }
            retry_report = {
                "run_id": "2026-07-06T18-33-22Z",
                "started_at": "2026-07-06T18:33:22+00:00",
                "sources": [
                    {
                        "company": "Airbnb",
                        "platform": "greenhouse",
                        "status": "searched_no_new_matches",
                        "health": "success_no_new",
                    }
                ],
            }

            self.assertTrue(
                job_search.source_issue_resolved_later(
                    failed_report["sources"][0],
                    failed_report,
                    [failed_report, retry_report],
                )
            )


if __name__ == "__main__":
    unittest.main()
