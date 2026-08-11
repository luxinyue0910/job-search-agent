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


class ScoreJobFetchFailureTest(unittest.TestCase):
    def test_detects_large_application_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            shell = (
                '{"title": "Candidate questions", "inactiveApplicationStages": []}'
                + " application metadata" * 7_000
            )

            reason = job_search.job_text_fetch_failure_reason(shell)

            self.assertEqual(
                reason,
                "job board returned an application shell instead of a job description",
            )

    def test_long_unsupported_browser_shell_is_a_fetch_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            shell = (
                "Powering our way of life. You are using an unsupported browser. "
                "To use this site, please use a supported browser. "
                + "navigation " * 100
            )

            reason = job_search.job_text_fetch_failure_reason(shell)

            self.assertIn("unsupported-browser shell", reason)

    def test_upsert_persists_substantive_adapter_job_text_for_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            (private_root / "data").mkdir()
            (private_root / "data" / "applications.json").write_text(
                '{"applications": []}\n',
                encoding="utf-8",
            )
            (private_root / "data" / "sources.json").write_text(
                '{"sources": []}\n',
                encoding="utf-8",
            )
            (private_root / "profile.json").write_text('{}\n', encoding="utf-8")
            job_search = load_job_search(private_root)
            adapter_text = " ".join(
                [
                    "Business Systems Analyst I supports requirements analysis, data reporting,",
                    "test planning, user acceptance testing, process improvement, SQL, and Python.",
                ]
                * 8
            )

            app, created = job_search.upsert_application(
                {
                    "company": "Example Utility",
                    "role": "Business Systems Analyst I",
                    "url": "https://example.com/jobs/123",
                    "platform": "ultipro",
                    "location": "Ephrata, WA",
                    "_jd_text": adapter_text,
                }
            )

            self.assertTrue(created)
            self.assertEqual(app["jd_source"], "adapter:ultipro")
            jd_path = Path(app["jd_path"])
            self.assertTrue(jd_path.is_file())
            self.assertEqual(jd_path.read_text(encoding="utf-8").strip(), adapter_text)
            with mock.patch.object(job_search, "read_job_text", side_effect=AssertionError("unexpected refetch")):
                self.assertEqual(job_search.cached_job_text_for_scoring(app), adapter_text)

    def test_upsert_rejects_adapter_browser_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            (private_root / "data").mkdir()
            (private_root / "data" / "applications.json").write_text(
                '{"applications": []}\n',
                encoding="utf-8",
            )
            (private_root / "data" / "sources.json").write_text(
                '{"sources": []}\n',
                encoding="utf-8",
            )
            (private_root / "profile.json").write_text('{}\n', encoding="utf-8")
            job_search = load_job_search(private_root)
            shell = "You are using an unsupported browser. " + "navigation " * 100

            app, _created = job_search.upsert_application(
                {
                    "company": "Example Utility",
                    "role": "Business Systems Analyst I",
                    "url": "https://example.com/jobs/456",
                    "platform": "ultipro",
                    "location": "Ephrata, WA",
                    "_jd_text": shell,
                }
            )

            self.assertNotIn("jd_path", app)
            self.assertNotIn("jd_source", app)

    def test_amazon_job_text_includes_official_qualifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))

            text = job_search.amazon_job_text(
                {
                    "title": "System Development Engineer",
                    "normalized_location": "Seattle, Washington, USA",
                    "description": "Build secure infrastructure automation.",
                    "basic_qualifications": "Experience with Python and Linux.",
                    "preferred_qualifications": "Experience with CI/CD pipelines.",
                }
            )

            self.assertIn("Build secure infrastructure automation", text)
            self.assertIn("Python and Linux", text)
            self.assertIn("CI/CD pipelines", text)

    def test_score_job_marks_internal_server_error_as_needs_retry_without_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            (private_root / "data").mkdir()
            (private_root / "profile.json").write_text(
                json.dumps(
                    {
                        "targets": {
                            "roles": ["software engineer"],
                            "keywords": ["python", "java", "aws"],
                        },
                        "dealbreakers": {},
                        "work_authorization": {"requires_sponsorship": False},
                    }
                ),
                encoding="utf-8",
            )
            (private_root / "data" / "sources.json").write_text('{"sources": []}\n', encoding="utf-8")
            (private_root / "data" / "applications.json").write_text(
                json.dumps(
                    {
                        "applications": [
                            {
                                "id": "chewy-software-engineer-ii-r29125",
                                "company": "Chewy",
                                "role": "Software Engineer II",
                                "url": "https://wd5.myworkdaysite.com/External/job/Bellevue-WA/Software-Engineer-II_R29125",
                                "platform": "workday",
                                "location": "Bellevue, WA",
                                "status": "found",
                                "fit_score": "",
                                "ats_score": "",
                                "date_applied": "",
                                "notes": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            job_search = load_job_search(private_root)
            with mock.patch.object(
                job_search,
                "read_job_text",
                return_value="Internal Server Error. (id: )",
            ):
                job_search.command_score_job(
                    argparse.Namespace(id="chewy-software-engineer-ii-r29125", jd_file=None, track=None)
                )

            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            app = tracker["applications"][0]
            self.assertEqual(app["status"], "needs_retry")
            self.assertEqual(app["fit_score"], "")
            self.assertEqual(app["ats_score"], "")
            self.assertIn("fetch_failed", app["notes"])
            self.assertIn("retry scoring", " ".join(app["action_items"]).lower())


if __name__ == "__main__":
    unittest.main()
