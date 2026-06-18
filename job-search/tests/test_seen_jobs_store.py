import argparse
import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "job_search.py"


def load_job_search(private_root: Path):
    os.environ["JOB_SEARCH_PRIVATE_DIR"] = str(private_root)
    module_name = f"job_search_under_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_minimal_workspace(private_root: Path) -> None:
    (private_root / "data").mkdir(parents=True)
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
    (private_root / "data" / "sources.json").write_text('{"sources": []}\n', encoding="utf-8")
    (private_root / "data" / "applications.json").write_text('{"applications": []}\n', encoding="utf-8")


class SeenJobsStoreTest(unittest.TestCase):
    def test_migrate_seen_jobs_writes_sharded_jsonl_and_removes_legacy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_minimal_workspace(private_root)
            legacy_jobs = {
                "https://jobs.example.com/a": {
                    "company": "Example",
                    "role": "Software Engineer",
                    "url": "https://jobs.example.com/a",
                    "first_seen": "2026-06-01T00:00:00+00:00",
                    "last_seen": "2026-06-02T00:00:00+00:00",
                    "posted_at": "",
                    "source_query": "",
                },
                "https://jobs.example.com/b": {
                    "company": "Example",
                    "role": "QA Engineer",
                    "url": "https://jobs.example.com/b",
                    "first_seen": "2026-06-01T00:00:00+00:00",
                    "last_seen": "2026-06-02T00:00:00+00:00",
                    "posted_at": "2026-06-01T00:00:00+00:00",
                    "matched_tracks": ["qa_engineer"],
                },
            }
            (private_root / "data" / "seen_jobs.json").write_text(
                json.dumps({"jobs": legacy_jobs}, indent=2) + "\n",
                encoding="utf-8",
            )

            job_search = load_job_search(private_root)
            job_search.command_migrate_seen_jobs(argparse.Namespace(keep_legacy=False))

            self.assertFalse((private_root / "data" / "seen_jobs.json").exists())
            index_path = private_root / "data" / "seen_jobs" / "index.json"
            self.assertTrue(index_path.exists())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["version"], 2)
            self.assertEqual(index["format"], "jsonl-shards")
            self.assertEqual(index["records"], 2)

            loaded = job_search.load_seen_jobs()
            self.assertEqual(set(loaded["jobs"]), set(legacy_jobs))
            self.assertEqual(loaded["jobs"]["https://jobs.example.com/b"]["matched_tracks"], ["qa_engineer"])

    def test_save_seen_jobs_omits_empty_fields_and_writes_deterministic_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_minimal_workspace(private_root)
            job_search = load_job_search(private_root)
            seen = {
                "jobs": {
                    "https://jobs.example.com/a": {
                        "company": "Example",
                        "role": "Software Engineer",
                        "url": "https://jobs.example.com/a",
                        "first_seen": "2026-06-01T00:00:00+00:00",
                        "last_seen": "2026-06-02T00:00:00+00:00",
                        "posted_at": "",
                        "source_query": "",
                    }
                },
                "_seen_jobs_format": "sharded",
            }

            job_search.save_seen_jobs(seen)

            shard_files = sorted((private_root / "data" / "seen_jobs" / "shards").glob("*.jsonl"))
            self.assertEqual(len(shard_files), 1)
            first_write = shard_files[0].read_text(encoding="utf-8")
            line = json.loads(first_write)
            self.assertEqual(line["url"], "https://jobs.example.com/a")
            self.assertNotIn("posted_at", line)
            self.assertNotIn("source_query", line)

            job_search.save_seen_jobs(seen)
            self.assertEqual(shard_files[0].read_text(encoding="utf-8"), first_write)

    def test_existing_seen_job_does_not_persist_last_seen_only_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_minimal_workspace(private_root)
            job_search = load_job_search(private_root)
            url = "https://jobs.example.com/a"
            seen = {
                "jobs": {
                    url: {
                        "company": "Example",
                        "role": "Software Engineer",
                        "url": url,
                        "first_seen": "2026-06-01T00:00:00+00:00",
                        "last_seen": "2026-06-02T00:00:00+00:00",
                        "posted_at": "2026-06-01T00:00:00+00:00",
                    }
                },
                "_seen_jobs_format": "sharded",
            }
            candidate = {
                "company": "Example",
                "role": "Software Engineer",
                "url": url,
                "platform": "greenhouse",
                "location": "Seattle",
                "posted_at": "2026-06-01T00:00:00+00:00",
            }
            args = argparse.Namespace(
                track=None,
                include_unknown_posted_date=True,
                no_role_filter=True,
                score=False,
            )

            job_search.process_discovered_candidates(
                [candidate],
                args,
                job_search.load_profile(),
                seen,
                None,
                "2026-06-17T12:00:00+00:00",
            )

            self.assertEqual(candidate["last_seen"], "2026-06-17T12:00:00+00:00")
            self.assertEqual(seen["jobs"][url]["last_seen"], "2026-06-02T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
