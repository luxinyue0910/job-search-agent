import argparse
import datetime as dt
import importlib.util
import io
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


def score_result(fit: float, ats: int, status: str = "scored") -> dict:
    return {
        "fit_score": fit,
        "ats_score": ats,
        "status": status,
        "experience_bucket": "1_2",
        "experience_requirements": ["1+ years"],
        "matched_keywords": ["python"],
        "resume_keyword_matches": ["python"],
        "missing_keywords": [],
        "dealbreakers": [],
        "action_items": [],
        "jd_text": "Software engineer role using Python.",
    }


def write_workspace(private_root: Path, app: dict):
    (private_root / "data").mkdir(parents=True)
    (private_root / "resume").mkdir(parents=True)
    (private_root / "tracks").mkdir(parents=True)
    (private_root / "profile.json").write_text(
        json.dumps(
            {
                "targets": {"roles": ["Software Engineer"], "keywords": ["Python"]},
                "preferences": {
                    "relocation_allowed_states": ["WA", "CA"],
                    "relocation_allowed_locations": ["Seattle", "San Francisco"],
                    "preferred_locations_order": ["Seattle", "San Francisco"],
                },
                "dealbreakers": {},
                "work_authorization": {"requires_sponsorship": False},
            }
        ),
        encoding="utf-8",
    )
    (private_root / "resume" / "master_resume.md").write_text("Python software engineer\n", encoding="utf-8")
    for track_id in ["general_sde", "fde_ai_engineer", "qa_engineer"]:
        track_dir = private_root / "tracks" / track_id
        track_dir.mkdir()
        (track_dir / "master_resume.md").write_text(f"Python {track_id}\n", encoding="utf-8")
        (track_dir / "resume.pdf").write_bytes(b"%PDF-test")
        (track_dir / "track.json").write_text(
            json.dumps(
                {
                    "id": track_id,
                    "resume_file": f"tracks/{track_id}/resume.pdf",
                    "master_resume": f"tracks/{track_id}/master_resume.md",
                    "targets": {
                        "roles": ["QA Engineer"] if track_id == "qa_engineer" else ["Software Engineer"],
                        "keywords": ["Python"],
                    },
                    "discovery_title_keywords": (
                        ["qa engineer"] if track_id == "qa_engineer" else ["software engineer"]
                    ),
                    "scoring_keywords": ["Python"],
                    "dealbreakers": {},
                }
            ),
            encoding="utf-8",
        )
    (private_root / "data" / "sources.json").write_text('{"sources": []}\n', encoding="utf-8")
    (private_root / "data" / "applications.json").write_text(
        json.dumps({"applications": [app]}),
        encoding="utf-8",
    )
    (private_root / "data" / "seen_jobs.json").write_text('{"jobs": {}}\n', encoding="utf-8")


def base_application() -> dict:
    return {
        "id": "example-software-engineer-12345678",
        "company": "Example",
        "role": "Software Engineer",
        "url": "https://example.com/jobs/1",
        "platform": "custom",
        "location": "Seattle, WA",
        "status": "found",
        "fit_score": "",
        "ats_score": "",
        "target_track": "general_sde",
        "matched_tracks": ["general_sde"],
        "resume_file": "",
        "date_applied": "",
        "notes": "",
        "dealbreakers": [],
        "action_items": [],
    }


class TrackEvaluationTest(unittest.TestCase):
    def test_rescore_backlog_selects_recent_unsubmitted_supported_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            cutoff = job_search.parse_datetime("2026-07-01T00:00:00+00:00")
            applications = [
                {
                    "id": "recent-found",
                    "status": "found",
                    "posted_at": "2026-06-01",
                    "first_seen": "2026-07-16T12:00:00+00:00",
                    "date_applied": "",
                },
                {
                    "id": "recent-scored",
                    "status": "scored",
                    "date_found": "2026-07-15",
                    "date_applied": "",
                },
                {
                    "id": "already-applied",
                    "status": "scored",
                    "date_found": "2026-07-17",
                    "date_applied": "2026-07-17",
                },
                {
                    "id": "prepared-by-default",
                    "status": "prepared",
                    "date_found": "2026-07-17",
                    "date_applied": "",
                },
                {
                    "id": "old-scored",
                    "status": "scored",
                    "date_found": "2026-06-01",
                    "date_applied": "",
                },
            ]

            selected = job_search.select_rescore_backlog_applications(
                applications,
                cutoff,
                set(job_search.DEFAULT_RESCORE_BACKLOG_STATUSES),
            )

            self.assertEqual([item[0]["id"] for item in selected], ["recent-found", "recent-scored"])
            self.assertEqual(selected[0][2], "first_seen")

    def test_rescore_all_tracks_forces_every_primary_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            app = {
                "target_track": "general_sde",
                "matched_tracks": ["general_sde"],
                "track_evaluations": {"general_sde": {"fit_score": 9.0}},
            }

            tracks = job_search.rescore_tracks_for_application(app, [], all_tracks=True)

            self.assertEqual(tracks, job_search.DEFAULT_DISCOVER_ALL_TRACKS)

    def test_rescore_command_queues_existing_evaluation_and_preserves_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app.update(
                {
                    "status": "scored",
                    "date_found": dt.date.today().isoformat(),
                    "notes": "Human review note",
                    "track_evaluations": {
                        "general_sde": {
                            **score_result(8.0, 75),
                            "track_id": "general_sde",
                        }
                    },
                }
            )
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)
            args = argparse.Namespace(
                since_hours=None,
                since_days=30,
                status=None,
                tracks=["general_sde"],
                all_tracks=False,
                limit=0,
                score_workers=2,
                dry_run=False,
                quiet=True,
            )

            with mock.patch.object(
                job_search,
                "execute_discovery_score_queue",
                return_value={"selected_tasks": 1, "scoring_failed": 0},
            ) as execute:
                job_search.command_rescore_backlog(args)

            queue, forwarded_args = execute.call_args.args
            self.assertEqual(
                [(task["app_id"], task["track"]) for task in queue],
                [(app["id"], "general_sde")],
            )
            self.assertTrue(forwarded_args.preserve_notes)
            self.assertEqual(forwarded_args.max_maybe_scores, 0)

    def test_rescore_command_dry_run_does_not_execute_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app.update({"date_found": dt.date.today().isoformat()})
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)
            args = argparse.Namespace(
                since_hours=None,
                since_days=30,
                status=None,
                tracks=["general_sde"],
                all_tracks=False,
                limit=0,
                score_workers=2,
                dry_run=True,
                quiet=False,
            )

            with mock.patch.object(job_search, "execute_discovery_score_queue") as execute:
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    job_search.command_rescore_backlog(args)

            execute.assert_not_called()
            self.assertIn("Backlog rescore dry run.", stdout.getvalue())
            self.assertIn(app["id"], stdout.getvalue())

    def test_source_for_tracks_unions_track_queries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_workspace(private_root, base_application())
            job_search = load_job_search(private_root)
            source = {
                "company": "QueryCo",
                "platform": "microsoft_jobs",
                "keywords": ["Software Engineer"],
                "track_keywords": {
                    "qa_engineer": ["SDET", "QA Engineer"],
                    "traditional_it_wa": ["Application Support Analyst"],
                },
            }

            combined = job_search.source_for_tracks(
                source,
                ["general_sde", "qa_engineer", "traditional_it_wa"],
            )

            self.assertIn("Software Engineer", combined["keywords"])
            self.assertIn("SDET", combined["keywords"])
            self.assertIn("Application Support Analyst", combined["keywords"])
            self.assertIn("Forward Deployed Engineer", combined["keywords"])
            self.assertEqual(combined["keywords"].count("Software Engineer"), 1)

    def test_scores_each_track_from_one_cached_jd_and_keeps_higher_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)

            def score_for_track(_app, _jd_text, profile):
                track_id = profile["_track"]["id"]
                return score_result(9.1, 82) if track_id == "general_sde" else score_result(7.4, 90)

            with mock.patch.object(job_search, "read_job_text", return_value="Real Python software engineer JD.") as read:
                with mock.patch.object(job_search, "score_text", side_effect=score_for_track):
                    job_search.command_score_job(
                        argparse.Namespace(id=app["id"], jd_file=None, track="general_sde")
                    )
                    job_search.command_score_job(
                        argparse.Namespace(id=app["id"], jd_file=None, track="fde_ai_engineer")
                    )

            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            saved = tracker["applications"][0]
            self.assertEqual(read.call_count, 1)
            self.assertEqual(set(saved["track_evaluations"]), {"general_sde", "fde_ai_engineer"})
            self.assertEqual(saved["target_track"], "general_sde")
            self.assertEqual(saved["fit_score"], 9.1)
            self.assertEqual(saved["ats_score"], 82)
            self.assertTrue(Path(saved["score_report_path"]).exists())

    def test_score_job_can_preserve_existing_human_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app["notes"] = "Keep this manual review note."
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)
            score = score_result(8.8, 81)
            score["action_items"] = ["Review one missing keyword."]

            with mock.patch.object(job_search, "read_job_text", return_value="Real Python software engineer JD."):
                with mock.patch.object(job_search, "score_text", return_value=score):
                    job_search.command_score_job(
                        argparse.Namespace(
                            id=app["id"],
                            jd_file=None,
                            track="general_sde",
                            preserve_notes=True,
                            quiet=True,
                        )
                    )

            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            self.assertEqual(tracker["applications"][0]["notes"], "Keep this manual review note.")

    def test_higher_new_track_becomes_legacy_top_level_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app.update(
                {
                    "status": "scored",
                    "fit_score": 7.0,
                    "ats_score": 70,
                    "experience_bucket": "1_2",
                    "resume_file": str(private_root / "tracks" / "general_sde" / "resume.pdf"),
                }
            )
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)

            with mock.patch.object(job_search, "read_job_text", return_value="Real AI integration JD."):
                with mock.patch.object(job_search, "score_text", return_value=score_result(9.3, 84)):
                    job_search.command_score_job(
                        argparse.Namespace(id=app["id"], jd_file=None, track="fde_ai_engineer")
                    )

            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            saved = tracker["applications"][0]
            self.assertTrue(saved["track_evaluations"]["general_sde"]["legacy_imported"])
            self.assertEqual(saved["track_evaluations"]["fde_ai_engineer"]["fit_score"], 9.3)
            self.assertEqual(saved["target_track"], "fde_ai_engineer")
            self.assertEqual(saved["fit_score"], 9.3)
            self.assertTrue(saved["resume_file"].endswith("tracks/fde_ai_engineer/resume.pdf"))

    def test_discovery_scores_existing_job_when_current_track_has_no_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app.update({"status": "scored", "fit_score": 8.5, "ats_score": 80})
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)
            profile = job_search.profile_for_track("qa_engineer")
            candidate = {
                "company": "Example",
                "role": "QA Engineer",
                "url": app["url"],
                "platform": "custom",
                "location": "Seattle, WA",
                "posted_at": "2026-07-17T12:00:00+00:00",
            }
            args = argparse.Namespace(
                track="qa_engineer",
                include_unknown_posted_date=False,
                include_maybe_backlog=False,
                maybe_old_posted_date=False,
                no_role_filter=False,
                score=True,
                score_maybe_limit=3,
            )

            with mock.patch.object(job_search, "command_score_job") as score_job:
                job_search.process_discovered_candidates(
                    [candidate],
                    args,
                    profile,
                    {"jobs": {}},
                    None,
                    "2026-07-17T12:00:00+00:00",
                )

            score_job.assert_called_once()
            self.assertEqual(score_job.call_args.args[0].track, "qa_engineer")

    def test_old_maybe_job_can_receive_first_evaluation_for_a_new_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app.update({"status": "scored", "fit_score": 8.5, "ats_score": 80})
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)
            profile = job_search.profile_for_track("fde_ai_engineer")
            candidate = {
                "company": "Example",
                "role": "Software Engineer",
                "url": app["url"],
                "platform": "custom",
                "location": "Seattle, WA",
                "posted_at": "2025-01-01T00:00:00+00:00",
            }
            args = argparse.Namespace(
                track="fde_ai_engineer",
                include_unknown_posted_date=False,
                include_maybe_backlog=True,
                maybe_old_posted_date=True,
                no_role_filter=False,
                score=True,
                score_maybe_limit=3,
            )
            seen = {
                "jobs": {
                    app["url"]: {
                        "company": app["company"],
                        "role": app["role"],
                        "url": app["url"],
                        "first_seen": "2026-07-16T12:00:00+00:00",
                        "last_seen": "2026-07-16T12:00:00+00:00",
                    }
                }
            }

            with mock.patch.object(job_search, "command_score_job") as score_job:
                stats = job_search.process_discovered_candidates(
                    [candidate],
                    args,
                    profile,
                    seen,
                    job_search.parse_datetime("2026-07-10T00:00:00+00:00"),
                    "2026-07-17T12:00:00+00:00",
                )

            self.assertEqual(stats["maybe_backlog"], 1)
            score_job.assert_called_once()
            self.assertEqual(score_job.call_args.args[0].track, "fde_ai_engineer")

    def test_all_track_processing_routes_one_candidate_to_multiple_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            write_workspace(private_root, app)
            (private_root / "data" / "applications.json").write_text(
                '{"applications": []}\n',
                encoding="utf-8",
            )
            job_search = load_job_search(private_root)
            profiles = {
                track_id: job_search.profile_for_track(track_id)
                for track_id in ["general_sde", "fde_ai_engineer", "qa_engineer"]
            }
            candidate = {
                "company": "Example",
                "role": "Software Engineer",
                "url": "https://example.com/jobs/multi-track",
                "platform": "custom",
                "location": "Seattle, WA",
                "posted_at": "2026-07-17T12:00:00+00:00",
            }
            args = argparse.Namespace(
                include_unknown_posted_date=False,
                include_maybe_backlog=False,
                maybe_old_posted_date=False,
                no_role_filter=False,
                score=True,
                score_maybe_limit=3,
            )

            with mock.patch.object(job_search, "command_score_job") as score_job:
                stats = job_search.process_discovered_candidates_all_tracks(
                    [candidate],
                    args,
                    profiles,
                    {"jobs": {}},
                    None,
                    "2026-07-17T12:00:00+00:00",
                )

            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            saved = tracker["applications"][0]
            self.assertEqual(stats["added"], 1)
            self.assertEqual(set(saved["matched_tracks"]), {"general_sde", "fde_ai_engineer"})
            self.assertEqual(
                {call.args[0].track for call in score_job.call_args_list},
                {"general_sde", "fde_ai_engineer"},
            )

    def test_discover_all_fetches_a_source_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_workspace(private_root, base_application())
            (private_root / "data" / "applications.json").write_text(
                '{"applications": []}\n',
                encoding="utf-8",
            )
            (private_root / "data" / "sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "company": "OneFetchCo",
                                "platform": "custom",
                                "url": "https://example.com/jobs",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            job_search = load_job_search(private_root)
            args = argparse.Namespace(
                person="default",
                tracks=["general_sde", "qa_engineer", "fde_ai_engineer"],
                source_company=None,
                since_hours=24,
                since_days=None,
                include_unknown_posted_date=False,
                include_maybe_backlog=False,
                maybe_old_posted_date=False,
                include_inactive_sources=False,
                no_role_filter=False,
                score=False,
                score_maybe_limit=3,
                source_timeout_seconds=30,
                source_retries=0,
                source_retry_timeout_seconds=0,
                workers=1,
            )
            candidate = {
                "company": "OneFetchCo",
                "role": "Software Engineer",
                "url": "https://example.com/jobs/1",
                "platform": "custom",
                "location": "Seattle, WA",
                "posted_at": "2026-07-17T12:00:00+00:00",
            }

            with mock.patch.object(
                job_search,
                "source_candidates_subprocess",
                return_value=([candidate], ""),
            ) as fetch:
                job_search.command_discover_all(args)

            fetch.assert_called_once()
            report_path = next((private_root / "data" / "discovery_runs").glob("*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["track"], "all")
            self.assertEqual(report["tracks"], args.tracks)

    def test_unified_scoring_selects_maybe_candidates_with_one_global_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_workspace(private_root, base_application())
            job_search = load_job_search(private_root)
            queue = [
                {
                    "app_id": "strict-app",
                    "track": "general_sde",
                    "maybe": False,
                    "priority": 1,
                    "posted_timestamp": 1,
                },
                {
                    "app_id": "maybe-high",
                    "track": "general_sde",
                    "maybe": True,
                    "priority": 10,
                    "posted_timestamp": 10,
                },
                {
                    "app_id": "maybe-high",
                    "track": "fde_ai_engineer",
                    "maybe": True,
                    "priority": 10,
                    "posted_timestamp": 10,
                },
                {
                    "app_id": "maybe-low",
                    "track": "general_sde",
                    "maybe": True,
                    "priority": 2,
                    "posted_timestamp": 20,
                },
            ]

            selected, maybe_candidates, maybe_selected = job_search.select_discovery_score_tasks(queue, 1)

            self.assertEqual(maybe_candidates, 2)
            self.assertEqual(maybe_selected, 1)
            self.assertEqual(
                {(task["app_id"], task["track"]) for task in selected},
                {
                    ("strict-app", "general_sde"),
                    ("maybe-high", "general_sde"),
                    ("maybe-high", "fde_ai_engineer"),
                },
            )

    def test_unified_scoring_prefetches_each_selected_job_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_workspace(private_root, base_application())
            job_search = load_job_search(private_root)
            queue = [
                {
                    "app_id": "one-app",
                    "track": "general_sde",
                    "maybe": False,
                    "priority": 1,
                    "posted_timestamp": 1,
                },
                {
                    "app_id": "one-app",
                    "track": "fde_ai_engineer",
                    "maybe": False,
                    "priority": 1,
                    "posted_timestamp": 1,
                },
            ]
            args = argparse.Namespace(max_maybe_scores=20, score_workers=4)

            with mock.patch.object(
                job_search,
                "prefetch_job_description",
                return_value="/tmp/jd.md",
            ) as prefetch:
                with mock.patch.object(job_search, "command_score_job") as score_job:
                    summary = job_search.execute_discovery_score_queue(queue, args)

            prefetch.assert_called_once_with("one-app")
            self.assertEqual(score_job.call_count, 2)
            self.assertTrue(all(call.args[0].quiet for call in score_job.call_args_list))
            self.assertEqual(summary["unique_apps"], 1)
            self.assertEqual(summary["selected_tasks"], 2)

    def test_rescore_queue_preserves_notes_when_scoring_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app["notes"] = "Keep this note."
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)
            queue = [
                {
                    "app_id": app["id"],
                    "track": "general_sde",
                    "maybe": False,
                    "priority": 1,
                    "posted_timestamp": 1,
                }
            ]
            args = argparse.Namespace(
                max_maybe_scores=0,
                score_workers=1,
                preserve_notes=True,
            )

            with mock.patch.object(
                job_search,
                "prefetch_job_description",
                return_value="/tmp/jd.md",
            ):
                with mock.patch.object(
                    job_search,
                    "command_score_job",
                    side_effect=RuntimeError("test failure"),
                ):
                    summary = job_search.execute_discovery_score_queue(queue, args)

            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["scoring_failed"], 1)
            self.assertIn("Keep this note.", tracker["applications"][0]["notes"])
            self.assertIn("Scoring failed: test failure", tracker["applications"][0]["notes"])

    def test_discover_all_quiet_suppresses_per_source_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_workspace(private_root, base_application())
            (private_root / "data" / "applications.json").write_text(
                '{"applications": []}\n',
                encoding="utf-8",
            )
            (private_root / "data" / "sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "company": "QuietCo",
                                "platform": "custom",
                                "url": "https://example.com/jobs",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            job_search = load_job_search(private_root)
            args = argparse.Namespace(
                person="default",
                tracks=["general_sde"],
                source_company=None,
                since_hours=24,
                since_days=None,
                include_unknown_posted_date=False,
                include_maybe_backlog=False,
                maybe_old_posted_date=False,
                include_inactive_sources=False,
                no_role_filter=False,
                score=False,
                score_maybe_limit=3,
                max_maybe_scores=20,
                score_workers=4,
                source_timeout_seconds=30,
                source_retries=0,
                source_retry_timeout_seconds=0,
                workers=1,
                quiet=True,
            )

            with mock.patch.object(
                job_search,
                "source_candidates_subprocess",
                return_value=([], ""),
            ):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    job_search.command_discover_all(args)

            output = stdout.getvalue()
            self.assertNotIn("[1/1]", output)
            self.assertNotIn("candidates=", output)
            self.assertIn("All-track discovery complete.", output)

    def test_backlog_can_use_one_tracks_independent_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            app = base_application()
            app.update(
                {
                    "status": "needs_review",
                    "fit_score": 5.0,
                    "ats_score": 40,
                    "track_evaluations": {
                        "general_sde": {
                            **score_result(5.0, 40, status="needs_review"),
                            "track_id": "general_sde",
                            "eligible": True,
                        },
                        "qa_engineer": {
                            **score_result(9.2, 85),
                            "track_id": "qa_engineer",
                            "eligible": True,
                        },
                    },
                }
            )
            write_workspace(private_root, app)
            job_search = load_job_search(private_root)
            output = private_root / "qa-backlog.md"

            job_search.command_application_backlog(
                argparse.Namespace(
                    bucket="priority",
                    track="qa_engineer",
                    status=None,
                    min_fit=8.0,
                    preferred_locations=True,
                    exclude_years=3,
                    hide_intern=True,
                    company_limit=3,
                    limit=50,
                    output=str(output),
                )
            )

            report = output.read_text(encoding="utf-8")
            self.assertIn("- track: qa_engineer", report)
            self.assertIn("| 9.2 | 85 |", report)
            self.assertIn("Example", report)


if __name__ == "__main__":
    unittest.main()
