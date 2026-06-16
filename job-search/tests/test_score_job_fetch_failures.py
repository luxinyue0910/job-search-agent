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
