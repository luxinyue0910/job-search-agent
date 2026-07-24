import argparse
import contextlib
import html
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
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
        "score_maybe_limit": 3,
        "source_timeout_seconds": 30,
        "source_retries": 0,
        "source_retry_timeout_seconds": 0,
        "workers": 1,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class DiscoveryCompatibilityTest(unittest.TestCase):
    def test_greenhouse_adapter_applies_source_location_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            response = {
                "jobs": [
                    {
                        "id": 101,
                        "title": "Software Engineer",
                        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/101",
                        "location": {"name": "Seattle, Washington, United States"},
                        "first_published": "2026-07-23T12:30:00Z",
                    },
                    {
                        "id": 102,
                        "title": "Software Engineer",
                        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/102",
                        "location": {"name": "Denver, Colorado, United States"},
                        "first_published": "2026-07-23T12:30:00Z",
                    },
                ]
            }

            with mock.patch.object(job_search, "fetch_json", return_value=response):
                candidates = job_search.discover_greenhouse_jobs(
                    {
                        "company": "Example",
                        "platform": "greenhouse",
                        "board": "example",
                        "url": "https://job-boards.greenhouse.io/example",
                        "location_include_regex": "Washington|Remote",
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["location"], "Seattle, Washington, United States")

    def test_ashby_adapter_applies_source_location_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            response = {
                "jobs": [
                    {
                        "title": "Software Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/example/wa-job",
                        "location": "Seattle, WA",
                        "publishedDate": "2026-07-23T12:30:00Z",
                    },
                    {
                        "title": "Software Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/example/co-job",
                        "location": "Denver, CO",
                        "publishedDate": "2026-07-23T12:30:00Z",
                    },
                ]
            }

            with mock.patch.object(job_search, "fetch_json", return_value=response):
                candidates = job_search.discover_ashby_jobs(
                    {
                        "company": "Example",
                        "platform": "ashby",
                        "board": "example",
                        "url": "https://jobs.ashbyhq.com/example",
                        "location_include_regex": r"\bWA\b|Washington|Remote",
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["location"], "Seattle, WA")

    def test_cyber_recruiter_adapter_walks_groups_and_enriches_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            root_url = "https://jobs.example.com/careers/"
            group_url = (
                "https://jobs.example.com/careers/Careers.aspx?"
                "groupvalue=90&groupby=OLEVEL1&type=DRAWSINGLEGROUPLIST"
            )
            list_url = (
                "https://jobs.example.com/careers/Careers.aspx?"
                "groupvalue=HOME&firstgroup=90&type=DRAWSINGLEGROUPLIST2"
            )
            detail_url = (
                "https://jobs.example.com/careers/Careers.aspx?"
                "req=EX-2026-100&type=JOBDESCR"
            )
            colorado_url = (
                "https://jobs.example.com/careers/Careers.aspx?"
                "req=EX-2026-200&type=JOBDESCR"
            )
            pages = {
                root_url: (
                    '<a href="Careers.aspx?groupvalue=90&amp;groupby=OLEVEL1'
                    '&amp;type=DRAWSINGLEGROUPLIST">Washington</a>'
                ),
                group_url: (
                    '<a href="Careers.aspx?groupvalue=HOME&amp;firstgroup=90'
                    '&amp;type=DRAWSINGLEGROUPLIST2">Home Office</a>'
                ),
                list_url: f"""
                    <table>
                      <tr>
                        <td><a class="JobLink" href="{detail_url}">IT Support Analyst</a></td>
                        <td>Information Technology</td><td>Full time</td>
                        <td>Bellingham, WA (Home Office)</td>
                      </tr>
                      <tr>
                        <td><a class="JobLink" href="{colorado_url}">Systems Analyst</a></td>
                        <td>Information Technology</td><td>Full time</td>
                        <td>Aurora, CO</td>
                      </tr>
                    </table>
                """,
                detail_url: """
                    <table id="CRCareers1_tblJobDescrDetail">
                      <tr><td class="HeaderStyle" colspan="2">IT Support Analyst<BR></td></tr>
                      <tr><td class="CaptionStyle">Location:</td>
                          <td class="MainBodyText">Bellingham</td></tr>
                      <tr><td class="MainBodyText" colspan="2">
                        Support business applications and troubleshoot systems.
                      </td></tr>
                    </table>
                """,
            }

            with mock.patch.object(
                job_search,
                "fetch_url",
                side_effect=lambda url, timeout=20: pages[url],
            ):
                candidates = job_search.discover_cyber_recruiter_jobs(
                    {
                        "company": "Example",
                        "platform": "cyber_recruiter",
                        "url": root_url,
                        "location_include_regex": "WA|Washington",
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["role"], "IT Support Analyst")
            self.assertEqual(candidates[0]["location"], "Bellingham")
            self.assertEqual(candidates[0]["external_job_id"], "EX-2026-100")
            self.assertEqual(candidates[0]["freshness_source"], "first_seen")
            self.assertIn("troubleshoot systems", candidates[0]["_jd_text"])

    def test_compact_location_text_labels_remote_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))

            location = job_search.compact_location_text(
                {
                    "city": "Seattle",
                    "country": "us",
                    "remote": True,
                    "hybrid": False,
                }
            )

            self.assertEqual(location, "Seattle, us, Remote")
            self.assertNotIn("True", location)

    def test_ttcportals_adapter_uses_browser_helper_and_first_seen_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            payload = {
                "jobs": [
                    {
                        "role": "Senior Developer, Analytics (Hybrid)",
                        "url": "https://pacworldwidecareers.ttcportals.com/jobs/18018342-senior-developer-analytics-hybrid",
                        "location": "Redmond, WA",
                        "external_job_id": "18018342",
                        "is_new": True,
                        "source_url": "https://pacworldwidecareers.ttcportals.com/search/jobs/in/wa-washington",
                    },
                    {
                        "role": "QA Representative - Structural",
                        "url": "https://vigorcareers.ttcportals.com/jobs/17346733-qa-representative-structural",
                        "location": "",
                        "external_job_id": "17346733",
                        "is_new": False,
                        "source_url": "https://vigorcareers.ttcportals.com/search/jobs/in/seattle",
                    },
                ]
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
            source = {
                "company": "PAC Worldwide",
                "platform": "ttcportals",
                "url": "https://pacworldwidecareers.ttcportals.com/search/jobs/in/wa-washington",
                "required_locations": ["WA", "Washington"],
            }

            with mock.patch.object(job_search.subprocess, "run", return_value=completed) as run:
                jobs = job_search.discover_ttcportals_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["location"], "Redmond, WA")
            self.assertEqual(jobs[0]["external_job_id"], "18018342")
            self.assertEqual(jobs[0]["posted_at"], "")
            self.assertEqual(jobs[0]["freshness_source"], "first_seen")
            self.assertIn("Listing marked NEW", jobs[0]["notes"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"])["company"], "PAC Worldwide")

            source["company"] = "Vigor Marine Group"
            source["url"] = "https://vigorcareers.ttcportals.com/search/jobs"
            source["listing_location_fallbacks"] = {
                "https://vigorcareers.ttcportals.com/search/jobs/in/seattle": "Seattle, WA"
            }
            vigor_completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"jobs": [payload["jobs"][1]]}),
                stderr="",
            )
            with mock.patch.object(job_search.subprocess, "run", return_value=vigor_completed):
                jobs = job_search.discover_ttcportals_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["role"], "QA Representative - Structural")
            self.assertEqual(jobs[0]["location"], "Seattle, WA")

    def test_ttcportals_detection_and_quality_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Vigor Marine Group",
                "url": "https://vigorcareers.ttcportals.com/search/jobs/in/seattle",
            }

            self.assertEqual(job_search.detect_platform(source["url"]), "ttcportals")
            classified = job_search.classify_source(source)
            self.assertEqual(classified["detected_platform"], "ttcportals")
            self.assertEqual(classified["source"]["platform"], "ttcportals")
            self.assertEqual(job_search.source_quality(classified["source"]), ("api_ok", "first_seen_only"))

    def test_browser_static_adapter_normalizes_configured_page_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            payload = {
                "jobs": [
                    {
                        "role": "Epic Interface Analyst",
                        "url": "https://example.org/jobs/16819",
                        "location": "Yakima, WA",
                        "posted_at": "July 9, 2026",
                        "external_job_id": "16819",
                        "description": "Support Epic interfaces and troubleshoot production issues.",
                        "source_url": "https://example.org/careers",
                    }
                ]
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
            source = {
                "company": "Example Health",
                "platform": "browser_static",
                "url": "https://example.org/careers",
                "item_selector": "article.job",
                "title_selector": "h3",
            }

            with mock.patch.object(job_search.subprocess, "run", return_value=completed) as run:
                jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["platform"], "browser_static")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-09T00:00:00+00:00")
            self.assertEqual(jobs[0]["freshness_source"], "browser_static_posted_date")
            self.assertEqual(jobs[0]["external_job_id"], "16819")
            self.assertIn("troubleshoot production issues", jobs[0]["_jd_text"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"])["company"], "Example Health")
            self.assertEqual(job_search.source_quality(source), ("api_ok", "first_seen_only"))

    def test_static_html_adapter_reuses_configured_link_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example Manufacturer",
                "platform": "static_html",
                "url": "https://example.org/careers",
                "job_link_regex": "^/careers/[^/]+$",
            }
            linked_job = {
                "company": "Example Manufacturer",
                "role": "Junior Automation Engineer",
                "url": "https://example.org/careers/junior-automation-engineer",
                "platform": "custom",
                "location": "Spokane, WA",
                "posted_at": "",
                "notes": "",
            }

            with mock.patch.object(job_search, "fetch_url", return_value="<html></html>"):
                with mock.patch.object(job_search, "parse_json_ld_jobs", return_value=[]):
                    with mock.patch.object(job_search, "find_links_for_source", return_value=[linked_job]):
                        jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["platform"], "static_html")
            self.assertIn("server-rendered", jobs[0]["notes"])
            self.assertEqual(job_search.source_quality(source), ("api_ok", "first_seen_only"))

    def test_static_html_adapter_supports_configured_detail_role_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example Nation",
                "platform": "static_html",
                "url": "https://example.org/jobs",
                "job_link_regex": r"^/jobs/detail\.php$",
                "link_role_regex": r'class=["\']job-title["\'][^>]*>(.*?)</div>',
                "detail_role_regex": r'data-field=["\']job-title["\'][^>]*>(.*?)</div>',
                "posted_at_regex": r'data-field=["\']open-date["\'][^>]*>(.*?)</div>',
                "location_override": "Bellingham, WA",
            }
            listing = (
                '<a href="/jobs/detail.php?id=42"><div class="job-title">'
                "Software Engineer</div><div>INFORMATION TECHNOLOGY</div>"
                "<div>Until Filled</div></a>"
            )
            detail = """
                <html><head><title>Position Details</title></head><body>
                <div data-field="job-title">Software Engineer</div>
                <div data-field="open-date">07/22/2026</div>
                </body></html>
            """

            with mock.patch.object(
                job_search,
                "fetch_url",
                side_effect=[listing, listing, detail],
            ):
                jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["role"], "Software Engineer")
            self.assertEqual(jobs[0]["location"], "Bellingham, WA")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-22T00:00:00+00:00")

    def test_prismhr_adapter_reads_public_requisitions_and_filters_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example Engineering",
                "platform": "prismhr",
                "url": "https://example.org/careers",
                "client_id": "client-42",
                "location_include_regex": r"\bWA\b|Bellingham",
            }
            payload = {
                "Success": True,
                "ResultList": [
                    {
                        "Id": 101,
                        "Title": "Junior Systems Engineer",
                        "Location": "Bellingham Branch",
                        "City": "Bellingham",
                        "State": "WA",
                        "Department": "Technology",
                        "OpenDate": "07/22/2026 15:00:00",
                        "ApplyUrl": "https://example.prismhrtalent.com/Application/Login.aspx?id=101",
                        "Description": "<p>Build and test industrial systems.</p>",
                    },
                    {
                        "Id": 102,
                        "Title": "Engineer",
                        "Location": "Billings Branch",
                        "City": "Billings",
                        "State": "MT",
                        "OpenDate": "07/20/2026 15:00:00",
                        "ApplyUrl": "https://example.prismhrtalent.com/Application/Login.aspx?id=102",
                    },
                ],
            }

            with mock.patch.object(
                job_search,
                "fetch_json_form_post",
                return_value=payload,
            ) as post:
                jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["role"], "Junior Systems Engineer")
            self.assertEqual(jobs[0]["external_job_id"], "101")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-22T15:00:00+00:00")
            self.assertIn("Build and test", jobs[0]["_jd_text"])
            self.assertEqual(
                post.call_args.args[1],
                {"clientIds[]": "client-42"},
            )
            self.assertEqual(job_search.source_quality(source), ("api_good", "official"))

    def test_infor_cloudsuite_adapter_paginates_and_enriches_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://example.inforcloudsuite.com/hcm/Jobs/page/JobsHomePage?"
                "csk.JobBoard=EXTERNAL&csk.HROrganization=1"
            )
            first_page = {
                "dataViewSet": {
                    "data": [
                        {
                            "fields": {
                                "Description": {"value": "IT Support Analyst"},
                                "JobId": {"value": 9416},
                                "PostingDateRange": {"value": "20260723"},
                                "LocationOfJobDescriptionForSort": {
                                    "value": "US:WA:Mount Vernon"
                                },
                                "CategoryDescriptionForSort": {
                                    "value": "Information Services"
                                },
                                "WorkType": {"value": "FULL TIME"},
                            }
                        }
                    ],
                    "pagingInfo": {
                        "hasNext": True,
                        "sortOrderName": "JobPosting.ByPostDateBeginSet",
                        "previousDisabled": True,
                        "fk": "first-key",
                        "hasPrevious": False,
                        "isAscending": False,
                        "lk": "last-key",
                    },
                }
            }
            second_page = {
                "dataViewSet": {
                    "data": [
                        {
                            "fields": {
                                "Description": {"value": "Software Engineer"},
                                "JobId": {"value": 9401},
                                "PostingDateRange": {"value": "20260722"},
                                "LocationOfJobDescriptionForSort": {
                                    "value": "US:WA:Arlington"
                                },
                            }
                        }
                    ],
                    "pagingInfo": {"hasNext": False},
                }
            }
            detail = {
                "fields": {
                    "_op_Description_spc_translation_cp_": {
                        "value": "IT Support Analyst"
                    },
                    "PostingDateRange": {"value": "20260723"},
                    "_op_PositionDescription_spc_translation_cp_": {
                        "value": "<p>Troubleshoot applications and support users.</p>"
                    },
                }
            }
            responses = [
                "<html>bootstrap</html>",
                json.dumps(first_page),
                json.dumps(second_page),
                json.dumps(detail),
                json.dumps(
                    {
                        "fields": {
                            "_op_PositionDescription_spc_translation_cp_": {
                                "value": "<p>Build reliable backend services.</p>"
                            }
                        }
                    }
                ),
            ]
            source = {
                "company": "Example Health",
                "platform": "infor_cloudsuite",
                "url": board_url,
                "page_size": 100,
                "max_pages": 3,
                "max_detail_pages": 2,
            }

            with mock.patch.object(
                job_search,
                "fetch_url_with_opener",
                side_effect=responses,
            ) as fetch:
                jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0]["posted_at"], "2026-07-23T00:00:00+00:00")
            self.assertEqual(jobs[0]["location"], "Mount Vernon, WA")
            self.assertEqual(jobs[0]["freshness_source"], "infor_cloudsuite_posting_date")
            self.assertIn("Troubleshoot applications", jobs[0]["_jd_text"])
            self.assertIn("JobPosting%5BJobPostingSet%5D", jobs[0]["url"])
            self.assertIn("pageop=next", fetch.call_args_list[2].args[1])
            self.assertEqual(job_search.detect_platform(board_url), "infor_cloudsuite")
            self.assertEqual(job_search.source_quality(source), ("api_good", "official"))

    def test_viewpoint_for_cloud_adapter_reads_list_and_detail_apis(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://example-hff.viewpointforcloud.com/careers/browsejobs"
            )
            req_id = "fed12320-a49e-41b7-be2d-0343bb4f642e"
            listing = [
                {
                    "PositionTitle": "Junior Systems Engineer",
                    "ReqNum": 26024,
                    "City": "Bellingham",
                    "State": "WA",
                    "DatePosted": "/Date(1784764800000+0000)/",
                    "DatePostedDisplayValue": "07/23/26",
                    "ReqID": req_id,
                },
                {
                    "PositionTitle": "Junior Systems Engineer",
                    "ReqNum": 26025,
                    "City": "Billings",
                    "State": "MT",
                    "DatePosted": "/Date(1784764800000+0000)/",
                    "DatePostedDisplayValue": "07/23/26",
                    "ReqID": "mt-req",
                }
            ]
            detail = {
                "PositionTitle": "Junior Systems Engineer",
                "ReqNum": 26024,
                "City": "Bellingham",
                "State": "WA",
                "DatePosted": "/Date(1784764800000+0000)/",
                "PositionDesc": "<p>Support applications and automate deployments.</p>",
                "PositionRequirements": "<p>Python and SQL experience.</p>",
                "PositionInstructions": "<p>Apply online.</p>",
            }

            def fetch(url, timeout=20):
                del timeout
                if "GetJobReqSearchExternal" in url:
                    return listing
                if "GetReqDetails" in url:
                    return detail
                raise AssertionError(f"Unexpected URL: {url}")

            source = {
                "company": "Example Contractor",
                "platform": "viewpoint_for_cloud",
                "url": board_url,
                "max_detail_pages": 1,
                "detail_workers": 1,
                "location_include_regex": r"\bWA\b|Bellingham",
            }
            with mock.patch.object(job_search, "fetch_json", side_effect=fetch):
                jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["role"], "Junior Systems Engineer")
            self.assertEqual(jobs[0]["location"], "Bellingham, WA")
            self.assertEqual(
                jobs[0]["posted_at"],
                "2026-07-23T00:00:00+00:00",
            )
            self.assertEqual(jobs[0]["external_job_id"], req_id)
            self.assertIn("/careers/jobdetails/", jobs[0]["url"])
            self.assertIn("automate deployments", jobs[0]["_jd_text"])
            self.assertIn("Python and SQL", jobs[0]["_jd_text"])
            self.assertEqual(
                job_search.detect_platform(board_url),
                "viewpoint_for_cloud",
            )
            self.assertEqual(
                job_search.source_quality(source),
                ("api_good", "official"),
            )

    def test_hireology_adapter_bootstraps_token_and_reads_public_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://careers.hireology.com/example/widget"
            starting_data = {
                "apiUrl": "https://api.hireology.example/v2",
                "apiToken": "test-token",
                "careersPath": "example",
            }
            widget_html = (
                "<script>var startingData = "
                f"{json.dumps(starting_data)};</script>"
            )
            payload = {
                "data": [
                    {
                        "id": 2823510,
                        "name": "Junior Application Support Engineer",
                        "created_at": "2026-07-22T20:17:11.625Z",
                        "updated_at": "2026-07-23T10:00:00Z",
                        "job_description": (
                            "<p>Support business systems and automate "
                            "regression checks.</p>"
                        ),
                        "locations": [
                            {
                                "city": "Ferndale, WA 98248",
                                "state": "WA",
                            }
                        ],
                        "remote": False,
                        "career_site_url": (
                            "https://careers.hireology.com/"
                            "example/2823510/description"
                        ),
                    }
                ],
                "count": 1,
                "page": 1,
                "page_size": 500,
            }
            source = {
                "company": "Example Manufacturer",
                "platform": "hireology",
                "url": board_url,
                "page_size": 500,
            }
            with (
                mock.patch.object(
                    job_search,
                    "fetch_url",
                    return_value=widget_html,
                ),
                mock.patch.object(
                    job_search,
                    "fetch_json_with_headers",
                    return_value=payload,
                ) as fetch,
            ):
                jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                jobs[0]["role"],
                "Junior Application Support Engineer",
            )
            self.assertEqual(jobs[0]["location"], "Ferndale, WA 98248")
            self.assertEqual(
                jobs[0]["posted_at"],
                "2026-07-22T20:17:11+00:00",
            )
            self.assertEqual(jobs[0]["external_job_id"], "2823510")
            self.assertIn("regression checks", jobs[0]["_jd_text"])
            self.assertEqual(
                fetch.call_args.args[1]["Authorization"],
                "Bearer test-token",
            )
            self.assertIn("page_size=500", fetch.call_args.args[0])
            self.assertEqual(
                job_search.detect_platform(board_url),
                "hireology",
            )
            self.assertEqual(
                job_search.source_quality(source),
                ("api_good", "official"),
            )

    def test_applicantstack_adapter_reads_detail_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://example.applicantstack.com/x/openings"
            detail_url = (
                "https://example.applicantstack.com/x/detail/a2example123"
            )
            listing = (
                '<a href="/x/detail/a2example123">'
                "Application Support Specialist</a>"
            )
            detail_payload = {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "datePosted": "2026-07-22T11:58:30-07:00",
                "description": (
                    "<p>Support enterprise applications and write "
                    "Python validation scripts.</p>"
                ),
                "hiringOrganization": {
                    "@type": "Organization",
                    "name": "Example Nonprofit",
                },
                "identifier": {
                    "@type": "PropertyValue",
                    "name": "Example Nonprofit",
                    "value": "a2example123",
                },
                "jobLocation": {
                    "@type": "Place",
                    "address": {
                        "@type": "PostalAddress",
                        "addressCountry": "US",
                        "addressRegion": "WA",
                        "addressLocality": "Spokane",
                    },
                },
                "title": "Application Support Specialist",
            }
            detail = (
                '<script type="application/ld+json">'
                f"{json.dumps(detail_payload)}</script>"
            )

            def fetch(url, timeout=20):
                del timeout
                if url == board_url:
                    return listing
                if url == detail_url:
                    return detail
                raise AssertionError(f"Unexpected URL: {url}")

            source = {
                "company": "Example Nonprofit",
                "platform": "applicantstack",
                "url": board_url,
                "max_detail_pages": 10,
                "detail_workers": 1,
            }
            with mock.patch.object(
                job_search,
                "fetch_url",
                side_effect=fetch,
            ):
                jobs = job_search.discover_source_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                jobs[0]["role"],
                "Application Support Specialist",
            )
            self.assertEqual(jobs[0]["location"], "Spokane, WA, US")
            self.assertEqual(
                jobs[0]["posted_at"],
                "2026-07-22T18:58:30+00:00",
            )
            self.assertEqual(
                jobs[0]["freshness_source"],
                "applicantstack_json_ld_date_posted",
            )
            self.assertIn("Python validation scripts", jobs[0]["_jd_text"])
            self.assertEqual(
                job_search.detect_platform(board_url),
                "applicantstack",
            )
            self.assertEqual(
                job_search.source_quality(source),
                ("api_ok", "official"),
            )

    def test_oracle_cx_job_url_removes_board_query_before_appending_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "url": (
                    "https://effy.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/"
                    "en/sites/CX/jobs?mode=location"
                )
            }

            self.assertEqual(
                job_search.oracle_cx_job_url(source, "2091"),
                (
                    "https://effy.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/"
                    "en/sites/CX/job/2091"
                ),
            )

    def test_workgr8_adapter_reads_public_graphql_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            payload = {
                "data": {
                    "searchJobs": {
                        "results": {
                            "totalCount": 1,
                            "nodes": [
                                {
                                    "status": "OPEN",
                                    "key": "515",
                                    "number": "515",
                                    "title": "IT Support Specialist",
                                    "descriptionHTML": "<p>Support enterprise applications.</p>",
                                    "postedOn": "2026-07-23T10:30:00Z",
                                    "primaryPlace": {"name": "Everett, WA, USA"},
                                    "positionType": {"name": "Full Time"},
                                }
                            ],
                        }
                    }
                }
            }
            source = {
                "company": "Example Aerospace",
                "platform": "workgr8",
                "url": "https://example.workgr8.com/jobs",
            }

            with mock.patch.object(job_search, "fetch_json_post", return_value=payload) as fetch:
                jobs = job_search.discover_workgr8_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["location"], "Everett, WA, USA")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-23T10:30:00+00:00")
            self.assertEqual(
                jobs[0]["url"],
                "https://example.workgr8.com/jobs/515/it-support-specialist",
            )
            self.assertIn("Support enterprise applications", jobs[0]["_jd_text"])
            self.assertEqual(
                fetch.call_args.args[1]["extensions"]["trustedDocument"]["id"],
                "search-jobs",
            )
            self.assertEqual(
                job_search.source_quality(source),
                ("api_good", "official"),
            )

    def test_talentreef_adapter_reads_official_jobs_and_builds_review_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            payload = {
                "hits": {
                    "total": 1,
                    "hits": [
                        {
                            "_source": {
                                "positionType": "Software Support Engineer (Remote)",
                                "category": "Information Technology",
                                "description": "<p>Support APIs and production systems.</p>",
                                "address": {
                                    "country": "US",
                                    "stateOrProvince": "WA",
                                    "city": "Seattle",
                                },
                                "jobId": 11079672,
                                "clientId": 14459,
                                "clientName": "Example Holdings",
                                "internalOrExternal": "externalOnly",
                                "postingUuid": "9f4e146f-7f03-478c-807e-8bce7f2a1fc3",
                                "createdDate": "2026-07-23",
                            }
                        }
                    ],
                }
            }
            source = {
                "company": "Example Data Center",
                "platform": "talentreef",
                "url": "https://careers.example.com/",
                "client_id": "14459",
            }

            with mock.patch.object(
                job_search,
                "fetch_json_post_with_headers",
                return_value=payload,
            ) as fetch:
                jobs = job_search.discover_talentreef_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["company"], "Example Data Center")
            self.assertEqual(jobs[0]["location"], "Remote; Seattle, WA")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-23T00:00:00+00:00")
            self.assertEqual(
                jobs[0]["url"],
                (
                    "https://careers.example.com/clients/14459/posting/"
                    "9f4e146f-7f03-478c-807e-8bce7f2a1fc3/en"
                ),
            )
            self.assertIn("Support APIs", jobs[0]["_jd_text"])
            self.assertIn(
                "/proxy-es/search-en-us/posting/_search",
                fetch.call_args.args[0],
            )
            self.assertEqual(
                fetch.call_args.args[2]["Origin"],
                "https://careers.example.com",
            )
            filters = fetch.call_args.args[1]["query"]["bool"]["filter"]
            self.assertIn({"terms": {"clientId.raw": ["14459"]}}, filters)
            self.assertEqual(
                job_search.source_quality(source),
                ("api_good", "official"),
            )

    def test_talentreef_source_parser_extracts_client_id_from_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            raw = (
                '<meta property="og:image" content="'
                "https://marketing-assets.jobappnetwork.com/14459/logo.png"
                '">'
            )

            source = job_search.source_from_talentreef_page(
                "DataBank",
                "https://www.databankcareers.com/",
                raw,
            )

            self.assertIsNotNone(source)
            self.assertEqual(source["platform"], "talentreef")
            self.assertEqual(source["client_id"], "14459")
            self.assertTrue(source["search_all"])

    def test_rss_adapter_can_parse_adp_location_suffix_from_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            raw = """
                <rss><channel><item>
                  <title>IT Support Specialist - Puyallup, WA</title>
                  <link>https://recruiting.adp.com/job/515</link>
                  <pubDate>Thu, 23 Jul 2026 10:30:00 GMT</pubDate>
                  <description>Support enterprise systems.</description>
                </item></channel></rss>
            """
            source = {
                "company": "Example Manufacturer",
                "platform": "rss",
                "url": "https://recruiting.adp.com/jobs.rss",
                "title_location_regex": (
                    r"^(?P<role>.+)\s+-\s+"
                    r"(?P<location>[^,]+,\s*[A-Z]{2}(?:,\s*[A-Z]{2})?)$"
                ),
            }

            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                jobs = job_search.discover_rss_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["role"], "IT Support Specialist")
            self.assertEqual(jobs[0]["location"], "Puyallup, WA")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-23T10:30:00+00:00")

    def test_successfactors_search_all_can_enrich_official_detail_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            search_html = """
                <table><tr class="data-row">
                  <td>
                    <a href="/job/Bellevue-Application-Analyst-WA/12345"
                       class="jobTitle-link">Application Analyst</a>
                    <span class="jobLocation">Bellevue, WA, US</span>
                  </td>
                </tr></table>
            """
            detail_html = """
                <div itemscope itemtype="http://schema.org/JobPosting">
                  <meta itemprop="datePosted" content="Thu Jul 23 00:00:00 UTC 2026">
                  <span data-careersite-propertyid="customfield1">REQ-515</span>
                  <span itemprop="description">Support business applications.</span>
                </div>
            """
            fetched_urls = []

            def fake_fetch(url, **_kwargs):
                fetched_urls.append(url)
                return detail_html if "/job/" in url else search_html

            source = {
                "company": "Example Insurer",
                "platform": "successfactors",
                "url": "https://jobs.example.com",
                "search_all": True,
                "search_params": {"locationsearch": "Washington"},
                "fetch_details": True,
                "max_pages": 1,
                "page_size": 25,
            }

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                jobs = job_search.discover_successfactors_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertIn("q=", fetched_urls[0])
            self.assertIn("locationsearch=Washington", fetched_urls[0])
            self.assertEqual(jobs[0]["role"], "Application Analyst")
            self.assertEqual(jobs[0]["location"], "Bellevue, WA, US")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-23T00:00:00+00:00")
            self.assertEqual(jobs[0]["job_number"], "REQ-515")
            self.assertEqual(jobs[0]["freshness_source"], "successfactors_datePosted")

    def test_location_tiers_accept_all_us_but_prefer_wa_and_remote_us(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            profile = {"preferences": {"location_tiers": {"preferred": ["Washington State", "Remote US"]}}}

            self.assertEqual(job_search.location_preference_bucket("Seattle, WA", profile), "preferred")
            self.assertEqual(job_search.location_preference_bucket("Remote - USA", profile), "preferred")
            self.assertEqual(job_search.location_preference_bucket("San Francisco, CA", profile), "relocation")
            self.assertEqual(job_search.location_preference_bucket("Charlotte, NC", profile), "relocation")
            self.assertEqual(
                job_search.location_preference_bucket("Washington, District of Columbia, United States", profile),
                "relocation",
            )
            self.assertEqual(job_search.location_preference_bucket("Remote", profile), "maybe")
            self.assertEqual(job_search.location_preference_bucket("", profile), "maybe")
            self.assertEqual(job_search.location_preference_bucket("Toronto, Canada", profile), "rejected")
            self.assertEqual(job_search.location_preference_bucket("Vancouver, WA", profile), "preferred")
            self.assertEqual(job_search.location_preference_bucket("Spokane Valley", profile), "preferred")
            self.assertEqual(job_search.location_preference_bucket("Richland, Washington", profile), "preferred")
            self.assertEqual(job_search.location_preference_bucket("Wenatchee, WA", profile), "preferred")

    def test_wa_traditional_it_titles_include_gis_security_erp_and_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            profile = {"_track": {"id": "traditional_it_wa"}}

            for role in [
                "GIS Developer",
                "Information Security Analyst",
                "ERP Administrator",
                "Cloud Administrator",
                "DevOps Engineer",
                "IT Customer Support - Entry",
            ]:
                with self.subTest(role=role):
                    self.assertTrue(job_search.maybe_backlog_title_relevant({"role": role}, profile))

    def test_classify_source_ignores_static_ats_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            pnnl_html = """
                <link rel="icon" href="https://cms.jibecdn.com/prod/pnnl/assets/FAVICON.ico">
                <script>window.jobsApi = '/api/jobs';</script>
            """
            with mock.patch.object(job_search, "fetch_url", return_value=pnnl_html):
                result = job_search.classify_source(
                    {"company": "PNNL", "platform": "custom", "url": "https://careers.pnnl.gov/"}
                )

            self.assertEqual(result["detected_platform"], "jibe")
            self.assertEqual(result["detected_url"], "https://careers.pnnl.gov/")
            self.assertEqual(result["source"]["api_url"], "https://careers.pnnl.gov/api/jobs")

            talentbrew_html = """
                <meta property="og:image" content="https://tbcdn.talentbrew.com/company/1/og-image.jpg">
                <script>window.vendor = 'talentbrew';</script>
            """
            with mock.patch.object(job_search, "fetch_url", return_value=talentbrew_html):
                result = job_search.classify_source(
                    {"company": "Lamb Weston", "platform": "custom", "url": "https://careers.lambweston.com/"}
                )

            self.assertEqual(result["detected_platform"], "talentbrew")
            self.assertEqual(result["detected_url"], "https://careers.lambweston.com/")
            self.assertEqual(result["source"]["results_url"], "https://careers.lambweston.com/en/search-jobs/results")

    def test_jibe_adapter_supports_full_location_scan_and_real_page_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            requested_urls = []

            def fake_fetch_json(url):
                requested_urls.append(url)
                return {
                    "count": 1,
                    "totalCount": 1,
                    "jobs": [
                        {
                            "data": {
                                "slug": "5901",
                                "req_id": "5901",
                                "title": "Technical Support Representative",
                                "description": "Troubleshoot data-network products and document issues.",
                                "city": "Bothell",
                                "state": "Washington",
                                "country": "United States",
                                "posted_date": "2026-07-22T00:08:00+0000",
                                "meta_data": {
                                    "canonical_url": "https://careers.example.com/jobs/5901?lang=en-us",
                                    "last_mod": "2026-07-23T00:00:00+0000",
                                    "icims": {"uuid": "example-uuid"},
                                },
                            }
                        }
                    ],
                }

            with mock.patch.object(job_search, "fetch_json", side_effect=fake_fetch_json):
                jobs = job_search.discover_jibe_jobs(
                    {
                        "company": "Example Network Manufacturer",
                        "platform": "jibe",
                        "url": "https://careers.example.com/",
                        "search_all": True,
                        "search_params": {"location": "Bothell"},
                        "page_size": 100,
                        "max_pages": 3,
                    }
                )

            self.assertEqual(len(requested_urls), 1)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(requested_urls[0]).query)
            self.assertNotIn("keywords", query)
            self.assertEqual(query["location"], ["Bothell"])
            self.assertEqual(query["limit"], ["100"])
            self.assertEqual(query["page"], ["1"])
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["location"], "Bothell, Washington, United States")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-22T00:08:00+00:00")
            self.assertEqual(jobs[0]["external_job_id"], "example-uuid")
            self.assertEqual(jobs[0]["source_query"], "all")

    def test_custom_source_prioritizes_technical_details_without_dropping_other_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example University",
                "platform": "custom",
                "url": "https://example.edu/postings/search",
                "max_detail_pages": 1,
            }
            listing = """
                <a href="/postings/search?sort=title">Applicant Portal</a>
                <a href="/postings/1">Accountant</a>
                <a href="/postings/2">Nurse</a>
                <a href="/postings/3">GIS Developer</a>
                <a href="/postings/3">View Details</a>
            """
            detail = """
                <title>GIS Developer | Example University</title>
                <script type="application/ld+json">{"datePosted":"2026-07-22"}</script>
            """

            def fake_fetch(url, timeout=30):
                return listing if url.endswith("/postings/search") else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                jobs = job_search.find_links_for_source(source)

            self.assertEqual(len(jobs), 3)
            technical = next(job for job in jobs if "GIS Developer" in job["role"])
            self.assertEqual(technical["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(sum(bool(job.get("posted_at")) for job in jobs), 1)

    def test_custom_source_supports_scoped_link_regex_and_visible_posted_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example Manufacturer",
                "platform": "custom",
                "url": "https://example.com/about/careers/",
                "job_link_regex": r"^/about/careers/[^/]+/?$",
                "preserve_link_title": True,
                "max_detail_pages": 5,
            }
            listing = """
                <a href="/about/careers/">Careers</a>
                <a href="/about/careers/it-manager/">IT Manager</a>
                <a href="/about/privacy/">Privacy</a>
            """
            detail = """
                <title>Example Manufacturer Careers</title>
                <table>
                  <tr><th>Date Posted</th><td>July 22, 2026</td></tr>
                  <tr><th>Location</th><td>Spokane, WA</td></tr>
                </table>
            """

            def fake_fetch(url, timeout=30):
                return listing if url.rstrip("/") == source["url"].rstrip("/") else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                jobs = job_search.find_links_for_source(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["role"], "IT Manager")
            self.assertEqual(jobs[0]["location"], "Spokane, WA")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(jobs[0]["freshness_source"], "official_posted_at")

    def test_custom_source_can_mark_fetch_and_empty_results_as_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Blocked Health System",
                "platform": "custom",
                "url": "https://example.org/careers",
                "fail_on_fetch_error": True,
                "empty_is_failure": True,
            }

            with mock.patch.object(
                job_search,
                "fetch_url",
                side_effect=urllib.error.HTTPError(
                    source["url"],
                    403,
                    "Forbidden",
                    {},
                    None,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "Could not fetch"):
                    job_search.find_links_for_source(source)

            with mock.patch.object(
                job_search,
                "fetch_url",
                return_value="<html><body>No listings rendered</body></html>",
            ):
                with self.assertRaisesRegex(RuntimeError, "No configured job links"):
                    job_search.find_links_for_source(source)

    def test_static_portfolio_adapter_keeps_structured_job_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source_url = "https://jobs.example.vc/jobs"
            job_url = "https://jobs.example.vc/companies/example/jobs/123-software-engineer"
            listing = f"""
                <script type="application/ld+json">
                {{
                  "@context": "https://schema.org",
                  "@type": "JobPosting",
                  "title": "Software Engineer II - Platform",
                  "datePosted": "2026-07-23",
                  "description": "<p>Build production agent infrastructure.</p>",
                  "url": "{job_url}",
                  "hiringOrganization": {{
                    "@type": "Organization",
                    "name": "Example Startup"
                  }},
                  "jobLocation": {{
                    "@type": "Place",
                    "address": {{
                      "@type": "PostalAddress",
                      "addressLocality": "Seattle",
                      "addressRegion": "WA",
                      "addressCountry": "US"
                    }}
                  }}
                }}
                </script>
            """
            generic_link = {
                "company": "Example VC Jobs",
                "role": "Example Venture Portfolio Job Board",
                "url": job_url,
                "platform": "getro_jobs",
                "location": "",
                "posted_at": "2026-07-23T00:00:00+00:00",
                "updated_at": "",
                "notes": "",
            }

            with (
                mock.patch.object(job_search, "fetch_url", return_value=listing),
                mock.patch.object(
                    job_search,
                    "find_links_for_source",
                    return_value=[generic_link],
                ),
            ):
                candidates = job_search.discover_static_job_board_jobs(
                    {
                        "company": "Example VC Jobs",
                        "platform": "getro_jobs",
                        "url": source_url,
                    },
                    "getro_jobs",
                    "Portfolio job board adapter for Getro-style pages.",
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["company"], "Example Startup")
            self.assertEqual(candidate["role"], "Software Engineer II - Platform")
            self.assertEqual(candidate["location"], "Seattle, WA, US")
            self.assertIn("agent infrastructure", candidate["_jd_text"])

    def test_getro_adapter_enriches_detail_pages_from_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source_url = "https://jobs.example.vc/jobs"
            job_url = "https://jobs.example.vc/companies/example/jobs/123-software-engineer"
            listing = f'<a href="{job_url}#content">Software Engineer</a>'
            detail = f"""
                <script type="application/ld+json">
                {{
                  "@context": "https://schema.org",
                  "@type": "JobPosting",
                  "title": "Software Engineer II - Platform",
                  "datePosted": "2026-07-23T12:30:00Z",
                  "description": "<p>Build and test production agent infrastructure.</p>",
                  "url": "{job_url}",
                  "hiringOrganization": {{
                    "@type": "Organization",
                    "name": "Example Startup"
                  }},
                  "jobLocation": {{
                    "@type": "Place",
                    "address": {{
                      "@type": "PostalAddress",
                      "addressLocality": "Seattle",
                      "addressRegion": "WA",
                      "addressCountry": "US"
                    }}
                  }}
                }}
                </script>
            """

            def fake_fetch(url, timeout=20):
                return listing if url == source_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_getro_jobs(
                    {
                        "company": "Example VC Jobs",
                        "platform": "getro_jobs",
                        "url": source_url,
                        "detail_workers": 2,
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["company"], "Example Startup")
            self.assertEqual(candidate["role"], "Software Engineer II - Platform")
            self.assertEqual(candidate["location"], "Seattle, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-23T12:30:00+00:00")
            self.assertEqual(candidate["freshness_source"], "official_posted_at")
            self.assertIn("test production", candidate["_jd_text"])

    def test_getro_adapter_paginates_public_portfolio_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source_url = "https://jobs.example.vc/jobs"
            listing = """
                <script id="__NEXT_DATA__" type="application/json">
                {"props":{"pageProps":{"network":{"id":"151"}}}}
                </script>
            """
            pages = {
                0: {
                    "results": {
                        "count": 21,
                        "jobs": [
                            {
                                "id": 101,
                                "title": "Software Engineer II",
                                "url": "https://jobs.ashbyhq.com/example/abc",
                                "created_at": 1784728145,
                                "locations": ["Seattle", "Washington"],
                                "source": "career_page",
                                "slug": "101-software-engineer-ii",
                                "organization": {
                                    "name": "Example Startup",
                                    "slug": "example-startup",
                                },
                            }
                        ],
                    }
                },
                1: {
                    "results": {
                        "count": 21,
                        "jobs": [
                            {
                                "id": 102,
                                "title": "QA Engineer",
                                "url": "https://job-boards.greenhouse.io/example/jobs/102",
                                "created_at": 1784641745,
                                "locations": ["Remote", "United States"],
                                "source": "career_page",
                                "slug": "102-qa-engineer",
                                "organization": {
                                    "name": "Second Startup",
                                    "slug": "second-startup",
                                },
                            }
                        ],
                    }
                },
            }
            requested_pages = []

            def fake_post(url, payload, headers, timeout=20):
                self.assertIn("/collections/151/search/jobs", url)
                self.assertEqual(headers["Origin"], "https://jobs.example.vc")
                self.assertEqual(headers["Referer"], source_url)
                requested_pages.append(payload["page"])
                return pages[payload["page"]]

            with (
                mock.patch.object(job_search, "fetch_url", return_value=listing),
                mock.patch.object(job_search, "fetch_json_post_with_headers", side_effect=fake_post),
            ):
                candidates = job_search.discover_getro_jobs(
                    {
                        "company": "Example VC Jobs",
                        "platform": "getro_jobs",
                        "url": source_url,
                        "max_pages": 5,
                        "api_workers": 2,
                        "max_detail_pages": 0,
                    }
                )

            self.assertEqual(sorted(requested_pages), [0, 1])
            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0]["company"], "Example Startup")
            self.assertEqual(candidates[0]["platform"], "ashby")
            self.assertEqual(candidates[0]["location"], "Seattle; Washington")
            self.assertEqual(candidates[0]["freshness_source"], "getro_created_at")
            self.assertEqual(candidates[1]["platform"], "greenhouse")
            self.assertEqual(candidates[1]["external_job_id"], "102")

    def test_consider_adapter_uses_public_board_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source_url = "https://jobs.psl.com/jobs"
            listing = """
                <script>
                window.serverInitialData = {
                  "clientConfig": {"id": "pioneer-square-labs"}
                };
                </script>
            """
            response = {
                "total": 1,
                "jobs": [
                    {
                        "companyName": "Example Startup",
                        "title": "Software Engineer",
                        "url": "https://jobs.ashbyhq.com/example/abc",
                        "applyUrl": "https://jobs.ashbyhq.com/example/abc?utm_source=jobs.psl.com",
                        "locations": ["Seattle, Washington, United States"],
                        "timeStamp": "2026-07-22T00:00:00Z",
                        "jobId": "abc",
                        "minYearsExp": 1,
                        "skills": [
                            {"label": "Python"},
                            {"label": "AWS"},
                        ],
                        "remote": False,
                    }
                ],
            }

            def fake_post(url, payload, headers, timeout=20):
                self.assertEqual(url, "https://jobs.psl.com/api-boards/search-jobs")
                self.assertEqual(payload["board"]["id"], "pioneer-square-labs")
                self.assertEqual(payload["meta"]["size"], 250)
                self.assertEqual(headers["Referer"], "https://jobs.psl.com/jobs")
                return response

            with (
                mock.patch.object(job_search, "fetch_url", return_value=listing),
                mock.patch.object(job_search, "fetch_json_post_with_headers", side_effect=fake_post),
            ):
                candidates = job_search.discover_consider_jobs(
                    {
                        "company": "Pioneer Square Labs Jobs",
                        "platform": "consider_jobs",
                        "url": source_url,
                        "max_detail_pages": 0,
                    }
                )

            self.assertEqual(job_search.detect_platform(source_url), "consider_jobs")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["company"], "Example Startup")
            self.assertEqual(candidate["platform"], "ashby")
            self.assertEqual(candidate["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "consider_timestamp")
            self.assertIn("Minimum experience: 1 years", candidate["_jd_text"])
            self.assertIn("Python", candidate["_jd_text"])

    def test_workday_source_parts_normalizes_configured_host_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "url": "https://seamar.wd12.myworkdayjobs.com/sea_mar",
                "host": "https://seamar.wd12.myworkdayjobs.com",
                "tenant": "seamar",
                "site": "sea_mar",
            }

            self.assertEqual(
                job_search.workday_source_parts(source),
                ("seamar.wd12.myworkdayjobs.com", "seamar", "sea_mar"),
            )

    def test_workday_adapter_can_scan_a_board_that_rejects_search_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            payloads = []

            def fake_post(url, payload, timeout=20):
                payloads.append(payload)
                return {
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Business Systems Analyst",
                            "externalPath": "/job/Seattle-WA/Business-Systems-Analyst_R100",
                            "locationsText": "Seattle WA",
                            "postedOn": "Posted Today",
                            "bulletFields": ["R100"],
                        }
                    ],
                }

            with mock.patch.object(job_search, "fetch_json_post", side_effect=fake_post):
                jobs = job_search.discover_workday_jobs(
                    {
                        "company": "Example Health Network",
                        "platform": "workday",
                        "host": "example.wd5.myworkdayjobs.com",
                        "tenant": "example",
                        "site": "Careers",
                        "url": "https://example.wd5.myworkdayjobs.com/Careers",
                        "search_all": True,
                        "page_size": 100,
                    }
                )

            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["searchText"], "")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["role"], "Business Systems Analyst")
            self.assertEqual(jobs[0]["location"], "Seattle WA")
            self.assertEqual(jobs[0]["source_query"], "all")

    def test_workable_adapter_uses_post_pagination_and_published_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example Building Systems",
                "platform": "workable",
                "account": "example-building",
                "url": "https://apply.workable.com/example-building/",
                "keywords": ["software"],
                "max_pages": 3,
            }
            pages = [
                {
                    "results": [
                        {
                            "shortcode": "ABC123",
                            "title": "Building Automation Software Engineer",
                            "published": "2026-07-22T00:00:00.000Z",
                            "location": {
                                "city": "Seattle",
                                "region": "Washington",
                                "country": "United States",
                            },
                        }
                    ],
                    "nextPage": "page-two",
                },
                {
                    "results": [
                        {
                            "shortcode": "XYZ789",
                            "title": "Controls Specialist",
                            "published": "2026-07-21T00:00:00.000Z",
                            "location": {
                                "city": "Tacoma",
                                "region": "Washington",
                                "country": "United States",
                            },
                        }
                    ],
                    "nextPage": "",
                },
            ]

            with mock.patch.object(job_search, "fetch_json_post", side_effect=pages) as fetch_post:
                jobs = job_search.discover_workable_jobs(source)

            self.assertEqual(len(jobs), 2)
            self.assertEqual(fetch_post.call_count, 2)
            self.assertEqual(fetch_post.call_args_list[0].args[1], {"query": "software"})
            self.assertEqual(
                fetch_post.call_args_list[1].args[1],
                {"query": "software", "token": "page-two"},
            )
            self.assertEqual(jobs[0]["location"], "Seattle, Washington, United States")
            self.assertEqual(jobs[0]["posted_at"], "2026-07-22T00:00:00+00:00")

    def test_paycom_platform_and_client_key_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            client_key = "E7A3819EACC22746FA78AB1D19A8ACB0"
            portal_url = f"https://www.paycomonline.net/v4/ats/web.php/portal/{client_key}/career-page"
            legacy_url = f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={client_key}"

            self.assertEqual(job_search.detect_platform(portal_url), "paycom")
            self.assertEqual(job_search.paycom_client_key({"url": portal_url}), client_key)
            self.assertEqual(job_search.paycom_client_key({"url": legacy_url}), client_key)
            self.assertEqual(
                job_search.source_from_paycom_url("Example", legacy_url),
                {
                    "company": "Example",
                    "platform": "paycom",
                    "client_key": client_key,
                    "url": portal_url,
                },
            )

    def test_paycom_adapter_uses_public_api_and_json_ld_posted_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            client_key = "E7A3819EACC22746FA78AB1D19A8ACB0"
            service_url = "https://paycom-api.example/career-portal/"
            portal_config = {
                "sessionJWT": "public-session-token",
                "libConfig": json.dumps({"atsPortalMantleServiceUrl": service_url}),
            }
            portal_html = f"<script>var configsFromHost = {json.dumps(portal_config)};</script>"
            preview_response = {
                "jobPostingPreviewsCount": 1,
                "jobPostingPreviews": [
                    {
                        "jobId": 513750,
                        "jobTitle": "Client Systems IT Administrator",
                        "locations": "Richland, WA 99352",
                        "description": "Support branch technology.",
                        "postedOn": "",
                    }
                ],
            }
            detail_response = {
                "jobPosting": {
                    "jobId": 513750,
                    "jobTitle": "Client Systems IT Administrator",
                    "location": "Richland, WA 99352",
                    "description": "<p>Support branch technology and automation.</p>",
                    "qualifications": "<p>Python and systems troubleshooting.</p>",
                    "googleJobJson": json.dumps(
                        {
                            "@type": "JobPosting",
                            "datePosted": "2026-07-22",
                            "url": f"https://www.paycomonline.net/v4/ats/web.php/portal/{client_key}/jobs/513750",
                        }
                    ),
                }
            }

            def fake_fetch_paycom(url, session_jwt, payload=None, timeout=20):
                self.assertEqual(session_jwt, "public-session-token")
                if url.endswith("/job-posting-previews/search"):
                    self.assertEqual(payload["skip"], 0)
                    self.assertEqual(payload["filtersForQuery"]["keywordSearchText"], "")
                    return preview_response
                self.assertTrue(url.endswith("/job-postings/513750"))
                return detail_response

            source = {
                "company": "Gesa Credit Union",
                "platform": "paycom",
                "client_key": client_key,
                "url": f"https://www.paycomonline.net/v4/ats/web.php/portal/{client_key}/career-page",
                "detail_workers": 1,
            }
            with mock.patch.object(job_search, "fetch_url", return_value=portal_html):
                with mock.patch.object(job_search, "fetch_paycom_json", side_effect=fake_fetch_paycom):
                    candidates = job_search.discover_paycom_jobs(source)

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Client Systems IT Administrator")
            self.assertEqual(candidate["location"], "Richland, WA 99352")
            self.assertEqual(candidate["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidate["external_job_id"], "513750")
            self.assertEqual(candidate["platform"], "paycom")
            self.assertIn("Python and systems troubleshooting", candidate["_jd_text"])
            self.assertEqual(candidate["freshness_source"], "paycom_json_ld_date_posted")

    def test_ultipro_adapter_uses_public_search_and_detail_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://recruiting2.ultipro.com/EXA1000/"
                "JobBoard/362abf68-95c3-4b17-a39d-76a6efe5ff18/"
            )
            opportunity_id = "bb740cf9-1040-4406-838b-64cf2e76f695"
            load_url = f"{board_url}JobBoardView/LoadSearchResults"
            detail_template = (
                f"{board_url}OpportunityDetail?"
                "opportunityId=00000000-0000-0000-0000-000000000000"
            )
            preview_response = {
                "totalCount": 1,
                "opportunities": [
                    {
                        "Id": opportunity_id,
                        "Title": "Application Systems Engineer",
                        "RequisitionNumber": "SYST005780",
                        "JobCategoryName": "Information Technology",
                        "PostedDate": "2026-07-22T18:50:21.251Z",
                        "BriefDescription": "Support enterprise applications.",
                        "Locations": [
                            {
                                "Address": {
                                    "City": "Spokane",
                                    "State": {"Code": "WA"},
                                    "Country": {"Code": "USA"},
                                }
                            }
                        ],
                    }
                ],
            }
            detail_html = f"""
                <script>
                new US.Opportunity.CandidateOpportunityDetail({{
                  "Id": "{opportunity_id}",
                  "Title": "Application Systems Engineer",
                  "RequisitionNumber": "SYST005780",
                  "JobCategoryName": "Information Technology",
                  "PostedDate": "2026-07-22T18:50:21.251Z",
                  "UpdatedDate": "2026-07-23T01:00:00Z",
                  "Description": "<p>Build Python automation and support enterprise systems.</p>",
                  "Locations": [{{
                    "Address": {{
                      "City": "Spokane",
                      "State": {{"Code": "WA"}},
                      "Country": {{"Code": "USA"}}
                    }}
                  }}]
                }});
                </script>
            """

            def fake_page(opener, url, token, payload, referer, timeout=25):
                self.assertEqual(url, load_url)
                self.assertEqual(token, "public-token")
                self.assertEqual(payload["opportunitySearch"]["Skip"], 0)
                self.assertEqual(payload["opportunitySearch"]["Top"], 50)
                self.assertEqual(referer, board_url)
                return preview_response

            source = {
                "company": "Example WA Employer",
                "platform": "ultipro",
                "url": board_url,
                "detail_workers": 1,
            }
            config = {
                "request_token": "public-token",
                "load_url": load_url,
                "detail_url": detail_template,
            }
            with mock.patch.object(job_search, "initialize_ultipro_session", return_value=(object(), config)):
                with mock.patch.object(job_search, "fetch_ultipro_search_page", side_effect=fake_page):
                    with mock.patch.object(job_search, "fetch_url", return_value=detail_html):
                        candidates = job_search.discover_ultipro_jobs(source)

            self.assertEqual(job_search.detect_platform(board_url), "ultipro")
            self.assertEqual(
                job_search.detect_platform(
                    "https://gusea1p01.rec.pro.ukg.net/HEA1511HONT/"
                    "JobBoard/6b8d439c-5173-42c1-9022-ac494fbd145d/"
                ),
                "ultipro",
            )
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Application Systems Engineer")
            self.assertEqual(candidate["location"], "Spokane, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-22T18:50:21+00:00")
            self.assertEqual(candidate["external_job_id"], opportunity_id)
            self.assertEqual(candidate["job_number"], "SYST005780")
            self.assertEqual(candidate["freshness_source"], "ultipro_posted_date")
            self.assertIn("Python automation", candidate["_jd_text"])

    def test_ultipro_adapter_filters_locations_before_detail_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://recruiting2.ultipro.com/EXA1000/"
                "JobBoard/362abf68-95c3-4b17-a39d-76a6efe5ff18/"
            )
            config = {
                "request_token": "public-token",
                "load_url": f"{board_url}JobBoardView/LoadSearchResults",
                "detail_url": (
                    f"{board_url}OpportunityDetail?"
                    "opportunityId=00000000-0000-0000-0000-000000000000"
                ),
            }
            preview_response = {
                "totalCount": 2,
                "opportunities": [
                    {
                        "Id": "wa-job",
                        "Title": "Application Support Analyst",
                        "PostedDate": "2026-07-23T18:00:00Z",
                        "Locations": [
                            {
                                "Address": {
                                    "City": "Spokane",
                                    "State": {"Code": "WA"},
                                    "Country": {"Code": "USA"},
                                }
                            }
                        ],
                    },
                    {
                        "Id": "tx-job",
                        "Title": "Application Support Analyst",
                        "PostedDate": "2026-07-23T18:00:00Z",
                        "Locations": [
                            {
                                "Address": {
                                    "City": "Austin",
                                    "State": {"Code": "TX"},
                                    "Country": {"Code": "USA"},
                                }
                            }
                        ],
                    },
                ],
            }
            source = {
                "company": "Example WA Employer",
                "platform": "ultipro",
                "url": board_url,
                "location_include_regex": r"\bWA\b|Washington",
                "max_detail_pages": 0,
            }
            with (
                mock.patch.object(
                    job_search,
                    "initialize_ultipro_session",
                    return_value=(object(), config),
                ),
                mock.patch.object(
                    job_search,
                    "fetch_ultipro_search_page",
                    return_value=preview_response,
                ),
                mock.patch.object(job_search, "fetch_url") as fetch_url,
            ):
                candidates = job_search.discover_ultipro_jobs(source)

            fetch_url.assert_not_called()
            self.assertEqual([candidate["external_job_id"] for candidate in candidates], ["wa-job"])
            self.assertEqual(candidates[0]["location"], "Spokane, WA")

    def test_zoho_recruit_adapter_parses_embedded_jobs_and_official_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://ziplyfiber.zohorecruit.com/jobs/careers"
            jobs = [
                {
                    "Remote_Job": True,
                    "Posting_Title": "Network Automation Engineer",
                    "Is_Locked": False,
                    "City": None,
                    "State": None,
                    "Country": "United States",
                    "Job_Description": "<p>Build Python and Go network automation.</p>",
                    "Job_Type": "Full time",
                    "id": "403102000049191025",
                    "Department_Name": {"name": "Technology"},
                    "Publish": True,
                    "Date_Opened": "2026-07-22",
                }
            ]
            encoded_jobs = html.escape(json.dumps(jobs), quote=True)
            raw = f'<input type="hidden" value="{encoded_jobs}" id="jobs">'
            source = {
                "company": "Ziply Fiber",
                "platform": "zoho_recruit",
                "url": board_url,
            }

            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_zoho_recruit_jobs(source)

            self.assertEqual(job_search.detect_platform(board_url), "zoho_recruit")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Network Automation Engineer")
            self.assertEqual(candidate["location"], "Remote | United States")
            self.assertEqual(candidate["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidate["source_query"], "Technology")
            self.assertEqual(candidate["external_job_id"], "403102000049191025")
            self.assertIn("Python and Go", candidate["_jd_text"])

    def test_jobvite_scan_all_jobs_enriches_technical_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://jobs.jobvite.com/example"
            detail_url = "https://jobs.jobvite.com/example/job/o123"
            listing = """
                <table class="jv-job-list">
                  <tr>
                    <td class="jv-job-list-name">
                      <a href="/example/job/o123">Application Support Analyst</a>
                    </td>
                    <td class="jv-job-list-location">Bremerton, WA</td>
                  </tr>
                </table>
            """
            detail = """
                <script type="application/ld+json">
                {
                  "@context": "https://schema.org",
                  "@type": "JobPosting",
                  "url": "https://jobs.jobvite.com/example/job/o123",
                  "title": "Application Support Analyst",
                  "datePosted": "2026-07-22",
                  "description": "<p>Support EHR applications and troubleshoot data issues.</p>",
                  "jobLocation": {
                    "@type": "Place",
                    "address": {"addressLocality": "Bremerton", "addressRegion": "WA"}
                  }
                }
                </script>
            """
            requested_urls = []

            def fake_fetch(url, timeout=20):
                requested_urls.append(url)
                return listing if "/jobs?" in url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_jobvite_jobs(
                    {
                        "company": "Example Employer",
                        "platform": "jobvite",
                        "url": board_url,
                        "scan_all_jobs": True,
                        "fetch_detail_limit": 10,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["url"], detail_url)
            self.assertEqual(candidate["location"], "Bremerton, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "jobvite_json_ld_date_posted")
            self.assertIn("troubleshoot data issues", candidate["_jd_text"])
            self.assertNotIn("q=", requested_urls[0])

    def test_jobvite_supports_root_listing_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://jobs.jobvite.com/nlight"
            detail_url = "https://jobs.jobvite.com/nlight/job/o456"
            listing = """
                <table class="jv-job-list">
                  <tr>
                    <td class="jv-job-list-name">
                      <a href="/nlight/job/o456">LabVIEW Developer</a>
                    </td>
                    <td class="jv-job-list-location">Camas, Washington</td>
                  </tr>
                  <tr>
                    <td class="jv-job-list-name">
                      <a href="/nlight/job/o789">Software Engineer</a>
                    </td>
                    <td class="jv-job-list-location">Longmont, Colorado</td>
                  </tr>
                </table>
            """
            detail = """
                <script type="application/ld+json">
                {
                  "@context": "https://schema.org",
                  "@type": "JobPosting",
                  "url": "https://jobs.jobvite.com/nlight/job/o456",
                  "title": "LabVIEW Developer",
                  "datePosted": "2026-07-23",
                  "description": "<p>Build test automation and manufacturing software.</p>",
                  "jobLocation": {
                    "@type": "Place",
                    "address": {"addressLocality": "Camas", "addressRegion": "WA"}
                  }
                }
                </script>
            """
            requested_urls = []

            def fake_fetch(url, timeout=20):
                requested_urls.append(url)
                return listing if url == board_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_jobvite_jobs(
                    {
                        "company": "nLIGHT",
                        "platform": "jobvite",
                        "url": board_url,
                        "listing_url": board_url,
                        "scan_all_jobs": True,
                        "required_locations": ["Camas, Washington", "Vancouver, Washington"],
                        "fetch_detail_limit": 10,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(requested_urls[0], board_url)
            self.assertEqual(candidates[0]["url"], detail_url)
            self.assertEqual(candidates[0]["posted_at"], "2026-07-23T00:00:00+00:00")

    def test_peopleadmin_adapter_parses_official_posting_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://employment.plu.edu/postings/search"
            detail_url = "https://employment.plu.edu/postings/8865"
            listing = """
              <div class="job-item job-item-posting"
                   data-posting-title="Application Systems Analyst">
                <a href="/postings/8865">Application Systems Analyst</a>
                <a href="/postings/8865">View Details</a>
              </div>
            """
            detail = """
              <table>
                <tr><th>Working Title:</th><td>Application Systems Analyst</td></tr>
                <tr><th>Location</th><td>Tacoma, WA 98447</td></tr>
                <tr><th>Posting Date:</th><td>07/22/2026</td></tr>
                <tr><th>Closing Date:</th><td>08/05/2026</td></tr>
                <tr><th>Position Number</th><td>0602406</td></tr>
              </table>
              <div>Support university applications, SQL reporting, and integrations.</div>
            """

            def fake_fetch(url, timeout=30):
                return listing if url == board_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_peopleadmin_jobs(
                    {
                        "company": "Pacific Lutheran University",
                        "platform": "peopleadmin",
                        "url": board_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "peopleadmin")
            self.assertEqual(
                job_search.detect_platform("https://evergreen.peopleadmin.com/postings/search"),
                "peopleadmin",
            )
            self.assertEqual(
                job_search.detect_platform("https://jobs.hr.ewu.edu/postings/search"),
                "peopleadmin",
            )
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Application Systems Analyst")
            self.assertEqual(candidate["url"], detail_url)
            self.assertEqual(candidate["location"], "Tacoma, WA 98447")
            self.assertEqual(candidate["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidate["updated_at"], "2026-08-05T00:00:00+00:00")
            self.assertEqual(candidate["job_number"], "0602406")
            self.assertEqual(candidate["freshness_source"], "peopleadmin_posting_date")
            self.assertIn("SQL reporting", candidate["_jd_text"])

    def test_pageup_adapter_follows_pagination_and_parses_advertised_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://careers.pageuppeople.com/793/cw/en-us/listing/"
            second_page = (
                "https://careers.pageuppeople.com/793/cw/en-us/listing/"
                "?page=2&page-items=85"
            )
            first_detail = (
                "https://careers.pageuppeople.com/793/cw/en-us/job/"
                "503059/data-consultant"
            )
            second_detail = (
                "https://careers.pageuppeople.com/793/cw/en-us/job/"
                "503060/systems-analyst"
            )
            first_listing = """
              <table>
                <tr>
                  <td><a class="job-link" href="/793/cw/en-us/job/503059/data-consultant">Data Consultant</a></td>
                  <td><span class="location">Bellingham, WA</span></td>
                  <td class="closing-date"><time datetime="2026-08-01T06:55:00Z">Jul 31</time></td>
                </tr>
              </table>
              <a href="/793/cw/en-us/listing/?page=2&amp;page-items=85"
                 class="more-link button">More Jobs</a>
            """
            second_listing = """
              <table>
                <tr>
                  <td><a class="job-link" href="/793/cw/en-us/job/503060/systems-analyst">Systems Analyst</a></td>
                  <td><span class="location">Bellingham, WA</span></td>
                </tr>
              </table>
            """
            first_detail_html = """
              <div id="job-content">
                <h2>Data Consultant</h2>
                <span class="location">Bellingham, WA</span>
                <div id="job-details"><p>Build data integrations and reports.</p></div>
                <p><b>Job no:</b> <span class="job-externalJobNo">503059</span></p>
                <b>Advertised:</b> <span class="open-date">
                  <time datetime="2026-07-22T16:00:00Z">Jul 22</time>
                </span>
              </div>
            """
            second_detail_html = """
              <div id="job-content">
                <h2>Systems Analyst</h2>
                <span class="location">Bellingham, WA</span>
                <div id="job-details"><p>Support university applications.</p></div>
                <p><b>Job no:</b> <span class="job-externalJobNo">503060</span></p>
                <b>Advertised:</b> <span class="open-date">
                  <time datetime="2026-07-23T16:00:00Z">Jul 23</time>
                </span>
              </div>
            """
            responses = {
                board_url: first_listing,
                second_page: second_listing,
                first_detail: first_detail_html,
                second_detail: second_detail_html,
            }

            def fake_fetch(url, timeout=30):
                return responses[url]

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_source_jobs(
                    {
                        "company": "Western Washington University",
                        "platform": "pageup",
                        "url": board_url,
                        "max_pages": 3,
                        "detail_workers": 1,
                        "location_map": {"Bellingham, WA": "Northwest Washington"},
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "pageup")
            self.assertEqual(len(candidates), 2)
            by_id = {candidate["external_job_id"]: candidate for candidate in candidates}
            self.assertEqual(
                by_id["503059"]["posted_at"],
                "2026-07-22T16:00:00+00:00",
            )
            self.assertEqual(
                by_id["503060"]["freshness_source"],
                "pageup_advertised_date",
            )
            self.assertEqual(by_id["503060"]["location"], "Northwest Washington")
            self.assertIn("data integrations", by_id["503059"]["_jd_text"])

    def test_taleo_adapter_parses_latest_page_hidden_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://example.taleo.net/careersection/10000/"
                "jobsearch.ftl?lang=en"
            )

            def record(
                internal_id,
                role,
                location,
                posted,
                closes,
                requisition,
            ):
                return [
                    internal_id,
                    role,
                    internal_id,
                    role,
                    internal_id,
                    internal_id,
                    internal_id,
                    internal_id,
                    internal_id,
                    location,
                    "false",
                    "",
                    "",
                    "",
                    "",
                    posted,
                    closes,
                    requisition,
                    "Apply",
                    f"Apply for this position ({role})",
                    internal_id,
                    "true",
                    "Re-apply",
                    "Re-apply for this job",
                    internal_id,
                    "false",
                    "false",
                    internal_id,
                    "false",
                    "false",
                    f"Submission for the position: {role}",
                    "false",
                    "true",
                    "Add to My Job Cart",
                    f"Add this position to the job cart: {role}",
                    internal_id,
                    "false",
                    "true",
                    "false",
                    "false",
                    "false",
                    "false",
                    "false",
                ]

            values = [
                "ftlx0",
                "jobsearch_processSearchInitialHistory!$!requisitionListInterface",
                "listRequisition",
                "rlPager!$!false",
                "false",
                "false",
                "false",
            ]
            values += record(
                "465140",
                "Software Engineer",
                "USA-WA-Seattle",
                "Jul 23, 2026",
                "Aug 22, 2026",
                "01024624",
            )
            values += record(
                "465141",
                "Systems Analyst",
                "USA-WA-Federal-Way",
                "Jul 22, 2026",
                "Aug 10, 2026",
                "01024625",
            )
            listing = (
                '<input type="hidden" id="initialHistory" value="'
                + "!|!".join(values)
                + '">'
            )

            with mock.patch.object(job_search, "fetch_url", return_value=listing):
                candidates = job_search.discover_source_jobs(
                    {
                        "company": "Example",
                        "platform": "taleo",
                        "url": board_url,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "taleo")
            self.assertEqual(len(candidates), 2)
            by_number = {
                candidate["job_number"]: candidate for candidate in candidates
            }
            self.assertEqual(by_number["01024624"]["location"], "Seattle, WA")
            self.assertEqual(
                by_number["01024624"]["posted_at"],
                "2026-07-23T00:00:00+00:00",
            )
            self.assertEqual(by_number["01024625"]["location"], "Federal Way, WA")
            self.assertIn(
                "jobdetail.ftl?job=01024625",
                by_number["01024625"]["url"],
            )
            self.assertEqual(
                by_number["01024625"]["freshness_source"],
                "taleo_posting_date",
            )

    def test_taleo_v2_adapter_paginates_and_reads_official_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://phh.tbe.taleo.net/phh03/ats/careers/v2/"
                "jobSearch?cws=40&org=EXAMPLE"
            )

            def card(rid, role, location, posted):
                detail_url = (
                    "https://phh.tbe.taleo.net/phh03/ats/careers/v2/"
                    "viewRequisition?"
                    f"org=EXAMPLE&cws=40&rid={rid}"
                )
                return (
                    '<h4 class="oracletaleocwsv2-head-title">'
                    f'<a href="{detail_url}" class="viewJobLink">'
                    f"{role}</a></h4>"
                    f'<div tabindex="0">{location}</div>'
                    f'<div tabindex="0">{posted}</div>'
                )

            first_page = (
                card(
                    "1201",
                    "Application Support Analyst",
                    "WA - Vancouver",
                    "7/22/26",
                )
                + '<a href="/phh03/ats/careers/v2/searchResults?'
                'next&rowFrom=10" class="jscroll-next">next</a>'
            )
            second_page = card(
                "1202",
                "Systems Coordinator",
                "WA - Pasco",
                "7/21/26",
            )

            def fetch_listing(opener, url, headers=None, timeout=20):
                del opener, headers, timeout
                return second_page if "rowFrom=10" in url else first_page

            with (
                mock.patch.object(
                    job_search,
                    "fetch_url_with_opener",
                    side_effect=fetch_listing,
                ),
                mock.patch.object(
                    job_search,
                    "fetch_url",
                    return_value=(
                        "<h1>Application Support Analyst</h1>"
                        "<p>Troubleshoot ERP integrations and write "
                        "validation scripts.</p>"
                    ),
                ),
            ):
                jobs = job_search.discover_source_jobs(
                    {
                        "company": "Example Transport",
                        "platform": "taleo",
                        "url": board_url,
                        "max_pages": 5,
                        "max_detail_pages": 1,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(len(jobs), 2)
            by_id = {
                candidate["external_job_id"]: candidate
                for candidate in jobs
            }
            self.assertEqual(
                by_id["1201"]["posted_at"],
                "2026-07-22T00:00:00+00:00",
            )
            self.assertEqual(by_id["1202"]["location"], "WA - Pasco")
            self.assertEqual(
                by_id["1201"]["freshness_source"],
                "taleo_v2_posted_date",
            )
            self.assertIn(
                "ERP integrations",
                by_id["1201"]["_jd_text"],
            )
            self.assertEqual(
                job_search.detect_platform(board_url),
                "taleo",
            )
            self.assertEqual(
                job_search.source_quality(
                    {"platform": "taleo", "url": board_url}
                ),
                ("api_ok", "official"),
            )

    def test_clearcompany_adapter_uses_public_careers_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://example.clearcompany.com/careers/jobs"
            response = [
                {
                    "Id": "d35d2cfd-cc74-ca5e-638e-5c78976859be",
                    "PositionTitle": "Software Engineer, Cloud Services",
                    "DepartmentName": "Infrastructure & Software Engineering",
                    "OfficeName": "Engineering",
                    "OpenDate": "2026-07-20T04:00:00Z",
                    "ApplyUrl": (
                        "https://example.clearcompany.com/careers/jobs/"
                        "d35d2cfd-cc74-ca5e-638e-5c78976859be/apply"
                    ),
                    "Description": "<p>Build Python services and cloud infrastructure.</p>",
                }
            ]

            with mock.patch.object(job_search, "fetch_json_with_headers", return_value=response) as fetch:
                candidates = job_search.discover_clearcompany_jobs(
                    {
                        "company": "Example Institute",
                        "platform": "clearcompany",
                        "url": board_url,
                        "default_location": "Seattle, WA",
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "clearcompany")
            fetch.assert_called_once_with(
                "https://example.clearcompany.com/api/v1/careers/jobs",
                {"API-ShortName": "example"},
            )
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Software Engineer, Cloud Services")
            self.assertEqual(candidate["location"], "Seattle, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-20T04:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "clearcompany_open_date")
            self.assertIn("Python services", candidate["_jd_text"])

    def test_paylocity_adapter_parses_page_data_and_detail_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://recruiting.paylocity.com/recruiting/jobs/All/"
                "01ab991b-8943-482a-9a66-9faa7b131dec/Example"
            )
            detail_url = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4317730"
            page_data = {
                "ModuleTitle": "Example Health",
                "Jobs": [
                    {
                        "JobId": 4317730,
                        "JobTitle": "Application Support Analyst",
                        "LocationName": "Bellingham, WA",
                        "PublishedDate": "2026-07-08T19:02:50-05:00",
                        "Description": "",
                        "HiringDepartment": "Information Technology",
                        "JobLocation": {
                            "City": "Bellingham",
                            "State": "WA",
                            "Country": "USA",
                        },
                    }
                ],
            }
            listing = f"<script>window.pageData = {json.dumps(page_data)};</script>"
            detail = """
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Application Support Analyst",
                "datePosted": "2026-07-15T22:17:24-05:00",
                "description": "<p>Support Epic applications and automate validation.</p>",
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Bellingham",
                    "addressRegion": "WA",
                    "addressCountry": "US"
                  }
                }
              }
              </script>
            """

            def fake_fetch(url, timeout=20):
                return listing if url == board_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_paylocity_jobs(
                    {
                        "company": "Example Health",
                        "platform": "paylocity",
                        "url": board_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "paylocity")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["url"], detail_url)
            self.assertEqual(candidate["location"], "Bellingham, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-16T03:17:24+00:00")
            self.assertEqual(candidate["freshness_source"], "paylocity_json_ld_date_posted")
            self.assertIn("automate validation", candidate["_jd_text"])

    def test_paylocity_adapter_applies_source_location_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://recruiting.paylocity.com/recruiting/jobs/All/"
                "01ab991b-8943-482a-9a66-9faa7b131dec/Example"
            )
            page_data = {
                "Jobs": [
                    {
                        "JobId": 101,
                        "JobTitle": "IT Support Analyst",
                        "LocationName": "Spokane, WA",
                    },
                    {
                        "JobId": 102,
                        "JobTitle": "IT Support Analyst",
                        "LocationName": "Denver, CO",
                    },
                ]
            }
            listing = (
                f"<script>window.pageData = {json.dumps(page_data)};</script>"
            )

            with mock.patch.object(
                job_search,
                "fetch_url",
                return_value=listing,
            ):
                candidates = job_search.discover_paylocity_jobs(
                    {
                        "company": "Example",
                        "platform": "paylocity",
                        "url": board_url,
                        "max_detail_pages": 0,
                        "location_include_regex": r"\bWA\b|Washington",
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["location"], "Spokane, WA")

    def test_paylocity_adapter_supports_public_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://www.example.com/careers"
            feed_url = "https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/example"
            detail_url = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4355000"
            feed_data = {
                "jobs": [
                    {
                        "jobId": 4355000,
                        "title": "Software Engineer I",
                        "displayUrl": detail_url,
                        "publishedDate": "2026-07-23T09:30:00-07:00",
                        "hiringDepartment": "Engineering",
                        "description": "<p>Build and test APIs.</p>",
                        "jobLocation": {
                            "locationDisplayName": "Bellingham, WA",
                        },
                    }
                ]
            }
            detail = """
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Software Engineer I",
                "datePosted": "2026-07-23T09:30:00-07:00",
                "description": "<p>Build and test APIs with C#.</p>",
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Bellingham",
                    "addressRegion": "WA",
                    "addressCountry": "US"
                  }
                }
              }
              </script>
            """

            with (
                mock.patch.object(job_search, "fetch_json", return_value=feed_data) as fetch_json,
                mock.patch.object(job_search, "fetch_url", return_value=detail),
            ):
                candidates = job_search.discover_paylocity_jobs(
                    {
                        "company": "Example",
                        "platform": "paylocity",
                        "url": board_url,
                        "feed_url": feed_url,
                        "detail_workers": 1,
                    }
                )

            fetch_json.assert_called_once_with(feed_url)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["url"], detail_url)
            self.assertEqual(candidate["location"], "Bellingham, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-23T16:30:00+00:00")
            self.assertIn("C#", candidate["_jd_text"])

    def test_dynamicsats_adapter_maps_public_listing_and_enriches_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            form_id = "06afd8b2-ca70-435a-80d3-55ecefe3b361"
            board_url = f"https://portal.dynamicsats.com/JobListing/{form_id}"
            job_id = "cb80f2d9-d744-f111-88b5-0022480923be"
            detail_url = f"https://portal.dynamicsats.com/JobListing/Details/{form_id}/{job_id}"
            listing = [
                {
                    "Id": job_id,
                    "JobUrl": detail_url,
                    "dcrs_category": "Engineering",
                    "dcrs_jobtitle": "Software Programmer",
                    "dcrs_location": "Mukilteo, WA",
                    "dcrs_jobdescription": "Short listing description.",
                }
            ]
            detail = """
              <div class="col-sm-12 col-md-8 col-md-pull-4">
                <p>Build and test software for aerospace automation systems.</p>
              </div>
            </div>
            </div>
            """

            with mock.patch.object(job_search, "fetch_dynamicsats_jobs", return_value=listing):
                with mock.patch.object(job_search, "fetch_url", return_value=detail):
                    candidates = job_search.discover_dynamicsats_jobs(
                        {
                            "company": "Electroimpact",
                            "platform": "dynamicsats",
                            "url": board_url,
                            "detail_workers": 1,
                        }
                    )

            self.assertEqual(job_search.detect_platform(board_url), "dynamicsats")
            self.assertEqual(job_search.dynamicsats_form_id({"url": board_url}), form_id)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Software Programmer")
            self.assertEqual(candidate["location"], "Mukilteo, WA")
            self.assertEqual(candidate["posted_at"], "")
            self.assertEqual(candidate["freshness_source"], "first_seen")
            self.assertIn("aerospace automation", candidate["_jd_text"])

    def test_icims_adapter_supports_fixed_location_params_without_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://uscareers-example.icims.com/jobs/search"
            detail_url = (
                "https://uscareers-example.icims.com/jobs/38073/business-application-analyst/job"
                "?in_iframe=1&mobile=false"
            )
            canonical_detail_url = "https://uscareers-example.icims.com/jobs/38073/business-application-analyst/job"
            listing = f"""
              <script type="application/ld+json">
              {{
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Business Application Analyst",
                "datePosted": "2026-07-21",
                "url": "{detail_url}",
                "jobLocation": {{
                  "@type": "Place",
                  "address": {{
                    "@type": "PostalAddress",
                    "addressLocality": "Bothell",
                    "addressRegion": "WA",
                    "addressCountry": "US"
                  }}
                }}
              }}
              </script>
            """

            with mock.patch.object(job_search, "fetch_url", return_value=listing) as fetch:
                candidates = job_search.discover_icims_jobs(
                    {
                        "company": "Example Medical",
                        "platform": "icims",
                        "url": board_url,
                        "search_all": True,
                        "search_params": {
                            "searchLocation": "12781-12831-Bothell",
                            "in_iframe": "1",
                        },
                    }
                )

            requested_url = urllib.parse.urlparse(fetch.call_args.args[0])
            requested_query = urllib.parse.parse_qs(requested_url.query)
            self.assertEqual(requested_query["searchLocation"], ["12781-12831-Bothell"])
            self.assertEqual(requested_query["in_iframe"], ["1"])
            self.assertNotIn("searchKeyword", requested_query)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["role"], "Business Application Analyst")
            self.assertEqual(candidates[0]["url"], canonical_detail_url)
            self.assertEqual(candidates[0]["location"], "Bothell, WA, US")
            self.assertEqual(candidates[0]["posted_at"], "2026-07-21T00:00:00+00:00")

    def test_smartrecruiters_adapter_can_scan_full_board_without_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            payload = {
                "content": [
                    {
                        "id": "acme-123",
                        "uuid": "acme-123",
                        "name": "Financial Systems Transformation Lead",
                        "releasedDate": "2026-07-22T10:15:00-07:00",
                        "location": {
                            "city": "Bellevue",
                            "region": "WA",
                            "country": "us",
                        },
                    }
                ]
            }

            with mock.patch.object(job_search, "fetch_json", return_value=payload) as fetch:
                candidates = job_search.discover_smartrecruiters_jobs(
                    {
                        "company": "Acme",
                        "platform": "smartrecruiters",
                        "company_identifier": "Acme",
                        "url": "https://careers.smartrecruiters.com/Acme",
                        "search_all": True,
                        "page_size": 100,
                    }
                )

            requested_url = urllib.parse.urlparse(fetch.call_args.args[0])
            requested_query = urllib.parse.parse_qs(requested_url.query)
            self.assertNotIn("q", requested_query)
            self.assertEqual(requested_query["destination"], ["PUBLIC"])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["role"], "Financial Systems Transformation Lead")
            self.assertEqual(candidates[0]["location"], "Bellevue, WA, us")
            self.assertEqual(candidates[0]["source_query"], "all")

    def test_topechelon_adapter_paginates_and_keeps_official_job_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            api_key = "public-board-key"
            first_page = {
                "pagination": {"total_pages": 2},
                "results": [
                    {
                        "id": "dcdfbb27-907b-4ebd-892b-2e2ce66206a5",
                        "posted_date": "2026-07-22T14:51:23.248-04:00",
                        "position_title": "Application Systems Analyst",
                        "city": "Richland",
                        "state": {"abbreviation": "WA"},
                        "description": "<p>Support Python and enterprise systems.</p>",
                    },
                    {
                        "id": "42e0b1d0-2cea-4f95-b8d5-705a2eea3482",
                        "posted_date": "2026-07-22T10:00:00-07:00",
                        "position_title": "Oregon Field Engineer",
                        "city": "Portland",
                        "state": {"abbreviation": "OR"},
                        "description": "<p>Work in Oregon.</p>",
                    },
                ],
            }
            second_page = {
                "pagination": {"total_pages": 2},
                "results": [
                    {
                        "id": "7669763d-5af7-44f6-880f-a73cca92dbd4",
                        "posted_date": "2026-07-23T09:30:00-07:00",
                        "position_title": "Cybersecurity Analyst",
                        "city": "Kennewick",
                        "state": {"abbreviation": "WA"},
                        "description": "<p>Monitor critical infrastructure.</p>",
                    }
                ],
            }
            source = {
                "company": "ANR Group",
                "platform": "topechelon",
                "url": "https://anrgroupinc.com/careers/",
                "api_key": api_key,
                "location_include_regex": r"\bWA\b|Washington",
            }

            with mock.patch.object(
                job_search,
                "fetch_json_with_headers",
                side_effect=[first_page, second_page],
            ) as fetch:
                candidates = job_search.discover_topechelon_jobs(source)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(fetch.call_count, 2)
            first_request = urllib.parse.urlparse(
                fetch.call_args_list[0].args[0]
            )
            second_request = urllib.parse.urlparse(
                fetch.call_args_list[1].args[0]
            )
            self.assertEqual(
                urllib.parse.parse_qs(first_request.query)["page"],
                ["1"],
            )
            self.assertEqual(
                urllib.parse.parse_qs(second_request.query)["page"],
                ["2"],
            )
            self.assertEqual(
                fetch.call_args_list[0].args[1]["Authorization"],
                f"Apikey {api_key}",
            )
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Application Systems Analyst")
            self.assertEqual(candidate["location"], "Richland, WA")
            self.assertEqual(
                candidate["posted_at"],
                "2026-07-22T18:51:23+00:00",
            )
            self.assertEqual(
                candidate["freshness_source"],
                "top_echelon_posted_date",
            )
            parsed_job_url = urllib.parse.urlparse(candidate["url"])
            self.assertEqual(
                parsed_job_url.netloc,
                "bb3jobboard.topechelon.com",
            )
            self.assertEqual(
                parsed_job_url.fragment,
                f"/{candidate['external_job_id']}/detail",
            )
            self.assertIn(
                "Python and enterprise systems",
                candidate["_jd_text"],
            )
            self.assertEqual(
                job_search.detect_platform(candidate["url"]),
                "topechelon",
            )

    def test_cadient_adapter_reads_employer_listing_with_official_posted_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            listing_url = "https://www.tridentseafoods.com/join-our-team/job-openings/"
            detail_url = (
                "https://cta.cadienttalent.com/index.jsp?SEQ=jobDetails"
                "&APPLICATIONNAME=TridentSeafoodsCorporationKTMDReqExt"
                "&POSTING_ID=106847533424&LOCATION_ID=106847533422"
            )
            listing = f"""
              <ul>
                <li class="border border-solid border-blue p-4 flex flex-wrap justify-between">
                  <div><h3 class="text-base">FSQA TECHNICIAN ANACORTES PLANT</h3>
                    <p class="micro">Food Safety Quality Assurance</p></div>
                  <dl>
                    <div><dt>Location</dt><dd class="text-micro">Anacortes<!-- -->, WA</dd></div>
                    <div><dt>Date Posted</dt><dd class="text-micro">06-Jul-2026</dd></div>
                  </dl>
                  <a target="_blank" href="{html.escape(detail_url, quote=True)}">Learn More</a>
                </li>
              </ul>
            """

            with mock.patch.object(job_search, "fetch_url", return_value=listing):
                candidates = job_search.discover_cadient_jobs(
                    {
                        "company": "Trident Seafoods",
                        "platform": "cadient",
                        "url": listing_url,
                    }
                )

            self.assertEqual(job_search.detect_platform(detail_url), "cadient")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "FSQA TECHNICIAN ANACORTES PLANT")
            self.assertEqual(candidate["location"], "Anacortes, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-06T00:00:00+00:00")
            self.assertEqual(candidate["external_job_id"], "106847533424")
            self.assertEqual(candidate["freshness_source"], "cadient_listing_date_posted")

    def test_breezy_adapter_uses_public_json_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://kwik-lok.breezy.hr"
            feed_url = f"{board_url}/json"
            payload = [
                {
                    "id": "7d7a501b2e11",
                    "name": "Business Systems Analyst",
                    "url": f"{board_url}/p/7d7a501b2e11-business-systems-analyst",
                    "published_date": "2026-07-22T19:15:30.000Z",
                    "type": {"id": "fullTime", "name": "Full-Time"},
                    "location": {
                        "country": {"name": "United States", "id": "US"},
                        "state": {"id": "WA", "name": "Washington"},
                        "city": "Yakima",
                        "is_remote": False,
                        "name": "Yakima, WA",
                    },
                    "locations": [{"name": "Yakima, WA"}],
                    "department": {"name": "Information Technology"},
                    "salary": "$70,000 - $85,000 / year",
                }
            ]

            with mock.patch.object(job_search, "fetch_json", return_value=payload) as fetch_json:
                candidates = job_search.discover_breezy_jobs(
                    {
                        "company": "Kwik Lok",
                        "platform": "breezy",
                        "url": board_url,
                    }
                )

            fetch_json.assert_called_once_with(feed_url, timeout=30)
            self.assertEqual(job_search.detect_platform(board_url), "breezy")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Business Systems Analyst")
            self.assertEqual(candidate["location"], "Yakima, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-22T19:15:30+00:00")
            self.assertEqual(candidate["source_query"], "Information Technology")
            self.assertEqual(candidate["freshness_source"], "breezy_published_date")

    def test_hanford_bms_adapter_reads_official_posting_and_detail_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            listing_url = "https://bms.hanford.gov/hrisjp/JobsList.aspx?BU=HMC&PT=E"
            detail_url = "https://bms.hanford.gov/hrisjp/JobDetail.aspx?BU=HMC&ID=41340&PT=E"
            listing = """
              <table>
                <tr style="text-align:center">
                  <td>Professional</td>
                  <td><a href="JobDetail.aspx?BU=HMC&amp;ID=41340&amp;PT=E">Maintenance Specialist</a></td>
                  <td>41340</td><td>2</td><td>Regular</td><td>Full-Time</td>
                  <td>Hanford Mission Integration Solutions</td>
                  <td>07/21/2026</td><td>07/28/2026</td>
                </tr>
              </table>
            """
            detail = """
              <span id="ctl_lblCityState">Richland, WA</span>
              <span id="ctl_lblOPEN_DT">07/21/2026</span>
              <p>Entry level candidates need CMMS experience. U.S. Citizenship Required: Yes.</p>
            """

            def fake_fetch(url, timeout=20):
                return listing if url == listing_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_hanford_bms_jobs(
                    {
                        "company": "Hanford Mission Integration Solutions",
                        "platform": "hanford_bms",
                        "url": listing_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(listing_url), "hanford_bms")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["url"], detail_url)
            self.assertEqual(candidate["location"], "Richland, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-21T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "hanford_posted_date")
            self.assertIn("Citizenship Required", candidate["_jd_text"])

    def test_applicantpro_adapter_uses_public_jobs_api_and_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://example.applicantpro.com/jobs/"
            detail_url = "https://example.applicantpro.com/jobs/4151259"
            listing = """
              <script>
                window.bootstrapVue("#job_listings", ["JobListings"], {
                  componentData: { organizationId: 12008, domainId: 15885 }
                });
              </script>
            """
            response = {
                "success": True,
                "data": {
                    "jobCount": 1,
                    "jobs": [
                        {
                            "id": 4151259,
                            "title": "Sales Data Analyst",
                            "city": "Selah",
                            "abbreviation": "WA",
                            "jobLocation": "220 East 2nd Avenue, Selah, WA, USA",
                            "startDateRef": "Jul 17, 2026",
                            "classification": "Support Center",
                            "jobUrl": detail_url,
                        }
                    ],
                },
            }
            detail = """
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Sales Data Analyst",
                "datePosted": "2026-07-17 00:00:00",
                "description": "<p>Build SQL reports and validate business data.</p>",
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Selah",
                    "addressRegion": "WA",
                    "addressCountry": "US"
                  }
                }
              }
              </script>
            """

            def fake_fetch(url, timeout=30):
                return listing if url == board_url else detail

            with (
                mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch),
                mock.patch.object(job_search, "fetch_json", return_value=response) as fetch_json,
            ):
                candidates = job_search.discover_applicantpro_jobs(
                    {
                        "company": "Tree Top",
                        "platform": "applicantpro",
                        "url": board_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "applicantpro")
            fetch_json.assert_called_once_with(
                "https://example.applicantpro.com/core/jobs/15885?getParams=%7B%7D",
                timeout=25,
            )
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["url"], detail_url)
            self.assertEqual(candidate["location"], "Selah, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-17T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "applicantpro_json_ld_date_posted")
            self.assertIn("SQL reports", candidate["_jd_text"])

    def test_rss_adapter_preserves_teamtailor_location_department_and_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            feed_url = "https://careers.example.com/jobs.rss"
            raw = """
              <rss version="2.0" xmlns:tt="https://teamtailor.com/locations">
                <channel>
                  <item>
                    <title>Business Systems Analyst</title>
                    <description><![CDATA[<p>Support ERP data and SQL reporting.</p>]]></description>
                    <pubDate>Wed, 22 Jul 2026 10:05:06 -0700</pubDate>
                    <link>https://careers.example.com/jobs/663510-business-systems-analyst</link>
                    <guid>job-guid</guid>
                    <tt:locations>
                      <tt:location>
                        <tt:city>Wenatchee</tt:city>
                        <tt:country>United States</tt:country>
                      </tt:location>
                    </tt:locations>
                    <tt:department>Information Technology</tt:department>
                  </item>
                </channel>
              </rss>
            """

            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_rss_jobs(
                    {
                        "company": "Example",
                        "platform": "rss",
                        "url": feed_url,
                        "feed_url": feed_url,
                        "target_platform": "teamtailor",
                        "default_state": "WA",
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["job_number"], "663510")
            self.assertEqual(candidate["external_job_id"], "663510")
            self.assertEqual(candidate["location"], "Wenatchee, WA, United States")
            self.assertEqual(candidate["source_query"], "Information Technology")
            self.assertEqual(candidate["posted_at"], "2026-07-22T17:05:06+00:00")
            self.assertIn("SQL reporting", candidate["_jd_text"])

    def test_rss_adapter_filters_unwanted_locations_without_default_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            feed_url = "https://careers.example.com/jobs.rss"
            raw = """
              <rss version="2.0" xmlns:tt="https://teamtailor.com/locations">
                <channel>
                  <item>
                    <title>Project Engineer</title>
                    <pubDate>Wed, 22 Jul 2026 10:05:06 -0700</pubDate>
                    <link>https://careers.example.com/jobs/100-seattle</link>
                    <tt:locations>
                      <tt:location>
                        <tt:city>Seattle</tt:city>
                        <tt:country>United States</tt:country>
                      </tt:location>
                    </tt:locations>
                  </item>
                  <item>
                    <title>Project Engineer</title>
                    <pubDate>Wed, 22 Jul 2026 10:05:06 -0700</pubDate>
                    <link>https://careers.example.com/jobs/200-reno</link>
                    <tt:locations>
                      <tt:location>
                        <tt:city>Reno</tt:city>
                        <tt:country>United States</tt:country>
                      </tt:location>
                    </tt:locations>
                  </item>
                </channel>
              </rss>
            """

            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_rss_jobs(
                    {
                        "company": "Example",
                        "platform": "rss",
                        "url": feed_url,
                        "feed_url": feed_url,
                        "target_platform": "teamtailor",
                        "location_include_regex": r"Seattle|\bWA\b",
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["external_job_id"], "100")
            self.assertEqual(candidates[0]["location"], "Seattle, United States")

    def test_rss_adapter_preserves_haley_job_location_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            feed_url = "https://jobs.example.com/rss/rss.smpl?state=WA"
            raw = """
              <rss version="2.0"
                   xmlns:job="http://haleymarketing.com/rss/2.0/modules/job/">
                <channel>
                  <item>
                    <title>Application Support Analyst</title>
                    <description><![CDATA[
                      <p>Support applications, SQL data, and production systems.</p>
                    ]]></description>
                    <pubDate>Thu, 23 Jul 2026 00:00:00 EDT</pubDate>
                    <link>
                      https://jobs.example.com/jb/Application-Support-Analyst-Jobs-in-Richland-Washington/13963652
                    </link>
                    <guid>13963652</guid>
                    <job:city>Richland</job:city>
                    <job:state>WA</job:state>
                    <job:country>US</job:country>
                    <job:category>Information Technology</job:category>
                  </item>
                </channel>
              </rss>
            """

            with (
                mock.patch.object(job_search, "fetch_url", return_value=raw),
                mock.patch.object(
                    job_search.ET,
                    "fromstring",
                    side_effect=ImportError("expat unavailable"),
                ),
            ):
                candidates = job_search.discover_rss_jobs(
                    {
                        "company": "Example",
                        "platform": "rss",
                        "url": "https://jobs.example.com/",
                        "feed_url": feed_url,
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["location"], "Richland, WA, US")
            self.assertEqual(candidate["source_query"], "Information Technology")
            self.assertEqual(candidate["posted_at"], "2026-07-23T04:00:00+00:00")
            self.assertIn("production systems", candidate["_jd_text"])

    def test_rss_adapter_can_use_category_as_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            feed_url = "https://example.easyapply.co/rss"
            raw = """
              <rss version="2.0">
                <channel>
                  <item>
                    <title>Financial Systems Analyst</title>
                    <description><![CDATA[<p>Support SQL reports and banking applications.</p>]]></description>
                    <pubDate>Wed, 15 Jul 2026 12:00:00 -0700</pubDate>
                    <link>https://example.easyapply.co/job/financial-systems-analyst</link>
                    <guid>financial-systems-analyst</guid>
                    <category><![CDATA[Longview, WA]]></category>
                  </item>
                </channel>
              </rss>
            """

            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_rss_jobs(
                    {
                        "company": "Example Credit Union",
                        "platform": "rss",
                        "url": "https://example.easyapply.co/",
                        "feed_url": feed_url,
                        "rss_category_field": "location",
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["location"], "Longview, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-15T19:00:00+00:00")
            self.assertIn("banking applications", candidate["_jd_text"])

    def test_rss_adapter_supports_atom_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            feed_url = "https://careers.example.com/timeline.atom"
            raw = """
              <feed xmlns="http://www.w3.org/2005/Atom">
                <entry>
                  <id>tag:careers.example.com,2005:TimelineEvent/896231</id>
                  <published>2026-07-20T16:10:10Z</published>
                  <updated>2026-07-21T16:10:10Z</updated>
                  <link rel="alternate" type="text/html"
                        href="https://careers.example.com/jobs/1479082-business-applications-analyst"/>
                  <title>Business Applications Analyst</title>
                  <content type="html">&lt;p&gt;Support hospital applications and SQL data.&lt;/p&gt;</content>
                </entry>
              </feed>
            """

            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_rss_jobs(
                    {
                        "company": "Example Health",
                        "platform": "rss",
                        "url": "https://careers.example.com/",
                        "feed_url": feed_url,
                        "default_location": "Coupeville, WA",
                        "target_platform": "healthcaresource",
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Business Applications Analyst")
            self.assertEqual(candidate["location"], "Coupeville, WA")
            self.assertEqual(candidate["job_number"], "1479082")
            self.assertEqual(candidate["external_job_id"], "1479082")
            self.assertEqual(candidate["posted_at"], "2026-07-20T16:10:10+00:00")
            self.assertEqual(candidate["freshness_source"], "rss_pubDate")
            self.assertIn("SQL data", candidate["_jd_text"])

    def test_rss_adapter_can_enrich_detail_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            feed_url = "https://careers.example.com/jobs/feed/"
            detail_url = "https://careers.example.com/jobs/application-support-analyst/"
            raw = f"""
              <rss version="2.0">
                <channel>
                  <item>
                    <title>Application Support Analyst</title>
                    <description><![CDATA[<p>Short summary.</p>]]></description>
                    <pubDate>Wed, 22 Jul 2026 20:00:00 +0000</pubDate>
                    <link>{detail_url}</link>
                    <guid>support-role</guid>
                  </item>
                </channel>
              </rss>
            """
            detail = """
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Application Support Analyst",
                "datePosted": "2026-07-21",
                "description": "<p>Support APIs, SQL data, and production systems.</p>",
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Seattle",
                    "addressRegion": "WA",
                    "addressCountry": "US"
                  }
                }
              }
              </script>
            """

            def fake_fetch(url, timeout=30):
                return raw if url == feed_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_rss_jobs(
                    {
                        "company": "Example",
                        "platform": "rss",
                        "url": "https://careers.example.com/jobs/",
                        "feed_url": feed_url,
                        "fetch_details": True,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["location"], "Seattle, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-21T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "rss_json_ld_date_posted")
            self.assertIn("production systems", candidate["_jd_text"])

    def test_healthcaresource_adapter_reads_public_search_api_and_preserves_hash_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://pm.healthcaresource.com/CS/example/"
            response = {
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [
                        {
                            "_id": "2169_3469",
                            "_source": {
                                "datePosted": "2026-07-23T00:00:00Z",
                                "lastIndexedDate": "2026-07-23T20:23:43Z",
                                "title": "Business Applications Analyst",
                                "occupationalCategory": "Information Technology",
                                "jobLocation": {
                                    "address": {
                                        "addressLocality": "Shelton",
                                        "addressRegion": "WA",
                                        "addressLocalityRegion": "Shelton, WA",
                                    }
                                },
                                "userArea": {
                                    "active": True,
                                    "isHiddenOnCareerSite": False,
                                    "jobPostingID": 3469,
                                    "requisitionNumber": "5315",
                                    "jobPostingModifiedDate": "2026-07-23T20:23:40Z",
                                    "bELevel3": "Information Technology",
                                    "jobSummary": "<p>Support hospital applications and SQL data.</p>",
                                },
                            },
                        }
                    ],
                }
            }
            with mock.patch.object(
                job_search,
                "fetch_json_post_with_headers",
                return_value=response,
            ) as fetch:
                candidates = job_search.discover_healthcaresource_jobs(
                    {
                        "company": "Example Health",
                        "platform": "healthcaresource",
                        "url": board_url,
                        "page_size": 100,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "healthcaresource")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(
                candidate["url"],
                "https://pm.healthcaresource.com/CS/example/#/job/3469",
            )
            self.assertEqual(candidate["external_job_id"], "3469")
            self.assertEqual(candidate["job_number"], "5315")
            self.assertEqual(candidate["location"], "Shelton, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-23T00:00:00+00:00")
            self.assertEqual(candidate["updated_at"], "2026-07-23T20:23:40+00:00")
            self.assertIn("SQL data", candidate["_jd_text"])
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[1]["from"], 0)

    def test_paradox_adapter_paginates_and_enriches_json_ld_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://jobs.example.org/all-jobs"

            def listing(reference, title, original_url):
                state = {
                    "jobSearch": {
                        "totalJob": 2,
                        "jobs": [
                            {
                                "reference": reference,
                                "title": title,
                                "originalURL": original_url,
                                "locations": [
                                    {
                                        "locationParsedText": "Forks, WA 98331, United States",
                                    }
                                ],
                            }
                        ],
                    }
                }
                return f"<script>window.__PRELOAD_STATE__ = {json.dumps(state)};</script>"

            page_one = listing(
                "P1-100-0",
                "Application Support Analyst",
                "application-support-analyst/job/P1-100-0",
            )
            page_two = listing(
                "P1-200-0",
                "Data Analyst",
                "data-analyst/job/P1-200-0",
            )
            detail = """
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Application Support Analyst",
                "datePosted": "2026-07-22",
                "description": "<p>Support APIs, SQL, and production systems.</p>",
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Forks",
                    "addressRegion": "WA",
                    "postalCode": "98331",
                    "addressCountry": "US"
                  }
                }
              }
              </script>
            """

            def fake_fetch(url, timeout=30):
                if url == board_url:
                    return page_one
                if url == f"{board_url}/page/2":
                    return page_two
                return detail.replace(
                    "Application Support Analyst",
                    "Data Analyst" if "data-analyst" in url else "Application Support Analyst",
                )

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch) as fetch:
                candidates = job_search.discover_paradox_jobs(
                    {
                        "company": "Example Hospital",
                        "platform": "paradox",
                        "url": board_url,
                        "max_pages": 2,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                {candidate["external_job_id"] for candidate in candidates},
                {"P1-100-0", "P1-200-0"},
            )
            self.assertTrue(
                all(
                    candidate["posted_at"] == "2026-07-22T00:00:00+00:00"
                    for candidate in candidates
                )
            )
            self.assertTrue(
                all(
                    candidate["freshness_source"] == "paradox_json_ld_date_posted"
                    for candidate in candidates
                )
            )
            self.assertEqual(fetch.call_count, 4)

    def test_sitemap_adapter_can_enrich_candidates_from_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            sitemap_url = "https://careers.example.com/jobs-sitemap.xml"
            detail_url = "https://careers.example.com/job/391/it_systems_engineer"
            sitemap = f"""
              <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url>
                  <loc>{detail_url}</loc>
                  <lastmod>2026-07-22T09:02:31+00:00</lastmod>
                </url>
              </urlset>
            """
            detail = """
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "IT Systems Engineer",
                "datePosted": "2026-07-18",
                "description": "<p>Support cloud infrastructure and banking applications.</p>",
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Olympia",
                    "addressRegion": "WA",
                    "addressCountry": "US"
                  }
                }
              }
              </script>
            """

            def fake_fetch(url, timeout=30):
                return sitemap if url == sitemap_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_sitemap_jobs(
                    {
                        "company": "Example Bank",
                        "platform": "sitemap",
                        "url": sitemap_url,
                        "sitemap_url": sitemap_url,
                        "keywords": ["systems"],
                        "fetch_details": True,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "IT Systems Engineer")
            self.assertEqual(candidate["location"], "Olympia, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-18T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "sitemap_json_ld_date_posted")
            self.assertIn("banking applications", candidate["_jd_text"])

    def test_sitemap_adapter_uses_default_location_when_url_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            sitemap_url = "https://app.example.com/jobs-sitemap.xml"
            sitemap = """
              <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <url>
                  <loc>https://health.example.com/residency-manager</loc>
                  <lastmod>2026-07-22T09:02:31+00:00</lastmod>
                </url>
              </urlset>
            """

            with mock.patch.object(job_search, "fetch_url", return_value=sitemap):
                candidates = job_search.discover_sitemap_jobs(
                    {
                        "company": "Example Health",
                        "platform": "sitemap",
                        "url": sitemap_url,
                        "sitemap_url": sitemap_url,
                        "include_url_regex": r"^https://health\.example\.com/",
                        "keywords": ["manager"],
                        "default_location": "Yakima / Ellensburg, WA",
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["location"], "Yakima / Ellensburg, WA")

    def test_kronos_careers_adapter_uses_public_session_api_and_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://example.prd.mykronos.com/ta/6110092.careers?CareersSearch="
            listing = {
                "job_requisitions": [
                    {
                        "id": 2117974489,
                        "job_title": "IT Systems Analyst",
                        "location": {
                            "city": "Sunnyside",
                            "state": "WA",
                            "country": "USA",
                        },
                        "job_description": "Support hospital systems.",
                    }
                ],
                "_paging": {"offset": 0, "size": 100, "total": 1},
            }
            detail = {
                "id": 2117974489,
                "job_title": "IT Systems Analyst",
                "location": {
                    "city": "Sunnyside",
                    "state": "WA",
                    "country": "USA",
                },
                "job_description": "<p>Support production hospital systems.</p>",
                "job_requirement": "<p>Experience with SQL and APIs.</p>",
            }

            def fake_json(_opener, url, _headers, timeout=20):
                return detail if "/2117974489?" in url else listing

            with (
                mock.patch.object(job_search, "fetch_url_with_opener", return_value=""),
                mock.patch.object(job_search, "fetch_json_with_opener", side_effect=fake_json),
            ):
                candidates = job_search.discover_kronos_careers_jobs(
                    {
                        "company": "Example Health",
                        "platform": "kronos_careers",
                        "url": board_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["external_job_id"], "2117974489")
            self.assertEqual(candidate["location"], "Sunnyside, WA, USA")
            self.assertEqual(candidate["posted_at"], "")
            self.assertEqual(candidate["freshness_source"], "first_seen")
            self.assertIn("SQL and APIs", candidate["_jd_text"])
            self.assertIn("ShowJob=2117974489", candidate["url"])

    def test_dayforce_adapter_uses_csrf_session_and_public_search_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://jobs.dayforcehcm.com/example/CANDIDATEPORTAL"
            search_payload = {
                "maxCount": 1,
                "jobPostings": [
                    {
                        "jobPostingId": 22524,
                        "jobReqId": 4599,
                        "jobTitle": "Application Support Analyst",
                        "jobDescription": "<p>Support data systems and automate validation.</p>",
                        "postingStartTimestampUTC": "2026-07-22T10:00:00+00:00",
                        "hasVirtualLocation": False,
                        "postingLocations": [
                            {
                                "formattedAddress": (
                                    "Seattle, 700 5th Ave, Seattle, Washington, "
                                    "United States of America"
                                )
                            }
                        ],
                    }
                ],
            }

            with mock.patch.object(
                job_search,
                "fetch_json_with_opener",
                return_value={"csrfToken": "csrf-value"},
            ) as fetch_csrf:
                with mock.patch.object(
                    job_search,
                    "fetch_json_post_with_opener",
                    return_value=search_payload,
                ) as fetch_jobs:
                    candidates = job_search.discover_dayforce_jobs(
                        {
                            "company": "Example Nonprofit",
                            "platform": "dayforce",
                            "url": board_url,
                        }
                    )

            self.assertEqual(job_search.detect_platform(board_url), "dayforce")
            self.assertEqual(job_search.dayforce_board_parts({"url": board_url}), ("example", "CANDIDATEPORTAL"))
            self.assertIn("/api/auth/csrf", fetch_csrf.call_args.args[1])
            self.assertEqual(
                fetch_jobs.call_args.args[2],
                {
                    "clientNamespace": "example",
                    "jobBoardCode": "CANDIDATEPORTAL",
                    "cultureCode": "en-US",
                    "paginationStart": 0,
                },
            )
            self.assertEqual(fetch_jobs.call_args.args[3]["X-CSRF-TOKEN"], "csrf-value")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Application Support Analyst")
            self.assertIn("Seattle", candidate["location"])
            self.assertEqual(candidate["posted_at"], "2026-07-22T10:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "dayforce_posting_start")
            self.assertEqual(candidate["job_number"], "4599")
            self.assertIn("automate validation", candidate["_jd_text"])

    def test_dayforce_adapter_detects_modern_localized_board_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://jobs.dayforcehcm.com/en-US/kalispel/NQRCCLIENTSITE"

            self.assertEqual(job_search.detect_platform(board_url), "dayforce")
            self.assertEqual(
                job_search.dayforce_board_parts({"url": board_url}),
                ("kalispel", "NQRCCLIENTSITE"),
            )

    def test_adp_workforce_now_adapter_paginates_and_enriches_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                "recruitment.html?cid=example-cid&ccId=19000101_000001&lang=en_US"
            )
            listing = {
                "jobRequisitions": [
                    {
                        "itemID": "9201200899124_1",
                        "requisitionTitle": "Application Support Analyst",
                        "postDate": "2026-07-22T10:00:00.000-07:00",
                        "clientRequisitionID": "1477",
                        "requisitionLocations": [
                            {
                                "nameCode": {"shortName": "Seattle, WA, US"},
                                "address": {
                                    "cityName": "Seattle",
                                    "countrySubdivisionLevel1": {"codeValue": "WA"},
                                },
                            }
                        ],
                        "customFieldGroup": {
                            "codeFields": [
                                {
                                    "shortName": "Information Technology",
                                    "nameCode": {"codeValue": "JobClass"},
                                }
                            ]
                        },
                    }
                ],
                "meta": {"totalNumber": 1},
            }
            detail = {
                **listing["jobRequisitions"][0],
                "requisitionDescription": (
                    "<p>Support business applications, SQL data, and automated validation.</p>"
                ),
            }

            def fake_fetch(url, headers, timeout=20):
                return detail if "/job-requisitions/9201200899124_1?" in url else listing

            with mock.patch.object(job_search, "fetch_json_with_headers", side_effect=fake_fetch) as fetch:
                candidates = job_search.discover_adp_workforce_now_jobs(
                    {
                        "company": "Example Local Employer",
                        "platform": "adp_workforce_now",
                        "url": board_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "adp_workforce_now")
            self.assertEqual(
                job_search.adp_board_parts({"url": board_url}),
                ("example-cid", "19000101_000001", "en_US"),
            )
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["location"], "Seattle, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-22T17:00:00+00:00")
            self.assertEqual(candidate["job_number"], "1477")
            self.assertEqual(candidate["source_query"], "Information Technology")
            self.assertEqual(candidate["freshness_source"], "adp_post_date")
            self.assertIn("automated validation", candidate["_jd_text"])
            self.assertIn("jobId=9201200899124_1", candidate["url"])

    def test_adp_workforce_now_adapter_supports_cloud_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://workforcenow.cloud.adp.com/mascsr/default/mdf/recruitment/"
                "recruitment.html?cid=cloud-cid&ccId=19000101_000001&lang=en_US"
            )

            with mock.patch.object(
                job_search,
                "fetch_json_with_headers",
                return_value={"jobRequisitions": [], "meta": {"totalNumber": 0}},
            ) as fetch:
                candidates = job_search.discover_adp_workforce_now_jobs(
                    {
                        "company": "Cloud ADP Employer",
                        "platform": "adp_workforce_now",
                        "url": board_url,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "adp_workforce_now")
            self.assertEqual(candidates, [])
            request_url, headers = fetch.call_args.args[:2]
            self.assertTrue(request_url.startswith("https://workforcenow.cloud.adp.com/"))
            self.assertEqual(headers["x-forwarded-host"], "workforcenow.cloud.adp.com")

    def test_adp_myjobs_adapter_uses_career_site_token_and_public_requisitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://myjobs.adp.com/examplehomeoffice"
            config = {
                "domain": "examplehomeoffice",
                "properties": {"myadpUrl": "https://my.adp.com"},
                "myJobsToken": "temporary-token",
            }
            listing = {
                "count": 1,
                "jobRequisitions": [
                    {
                        "reqId": "5001213116200",
                        "publishedJobTitle": "Solutions Desk Analyst",
                        "postingDate": "2026-07-20T17:41:27Z",
                        "clientRequisitionID": "12761",
                        "jobDescription": (
                            "<p>Troubleshoot hardware, software, network, and application issues.</p>"
                        ),
                        "jobQualifications": "<p>Basic computer networking experience.</p>",
                        "requisitionLocations": [
                            {
                                "address": {
                                    "cityName": "Lynnwood",
                                    "postalCode": "98036",
                                    "countrySubdivisionLevel1": {"codeValue": "WA"},
                                }
                            }
                        ],
                    }
                ],
            }

            with (
                mock.patch.object(job_search, "fetch_json", return_value=config) as fetch_config,
                mock.patch.object(
                    job_search,
                    "fetch_json_with_headers",
                    return_value=listing,
                ) as fetch_jobs,
            ):
                candidates = job_search.discover_adp_myjobs_jobs(
                    {
                        "company": "Example Retailer",
                        "platform": "adp_myjobs",
                        "url": board_url,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "adp_myjobs")
            self.assertEqual(job_search.adp_myjobs_domain({"url": board_url}), "examplehomeoffice")
            self.assertEqual(
                fetch_config.call_args.args[0],
                "https://myjobs.adp.com/public/staffing/v1/career-site/examplehomeoffice",
            )
            request_url = fetch_jobs.call_args.args[0]
            request_headers = fetch_jobs.call_args.args[1]
            self.assertIn("apply-custom-filters", request_url)
            self.assertIn("%24top=50", request_url)
            self.assertEqual(request_headers["MyJobsToken"], "temporary-token")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Solutions Desk Analyst")
            self.assertEqual(candidate["location"], "Lynnwood, WA, 98036")
            self.assertEqual(candidate["posted_at"], "2026-07-20T17:41:27+00:00")
            self.assertEqual(candidate["job_number"], "12761")
            self.assertEqual(candidate["freshness_source"], "adp_myjobs_posting_date")
            self.assertIn("computer networking", candidate["_jd_text"])
            self.assertIn("reqId=5001213116200", candidate["url"])

    def test_appone_adapter_enriches_listing_links_from_job_microdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://www2.appone.com/Search/Search.aspx?"
                "ServerVar=www.example.appone.com"
            )
            listing_url = (
                "https://recruiting.myapps.paychex.com/appone/Branding/"
                "ReqTemplate/BrowseAllJobsbyCategory.asp?ClientID=6911"
            )
            detail_url = (
                "https://recruiting.myapps.paychex.com/appone/"
                "MainInfoReq.asp?R_ID=7185692&B_ID=91"
            )
            listing = (
                '<a href="/appone/MainInfoReq.asp?R_ID=7185692&amp;B_ID=91">'
                "Solutions Desk Analyst - Seattle, Washington - Job</a>"
            )
            detail = """
                <meta itemprop="title" content="Solutions Desk Analyst" />
                <meta itemprop="datePosted" content="07/20/2026" />
                <meta itemprop="addressLocality" content="Seattle" />
                <meta itemprop="addressRegion" content="Washington" />
                <meta itemprop="description"
                      content="Troubleshoot software, network, and application issues." />
            """

            def fake_fetch(url, timeout=20):
                return detail if "MainInfoReq.asp" in url else listing

            with mock.patch.object(
                job_search,
                "fetch_url",
                side_effect=fake_fetch,
            ):
                candidates = job_search.discover_appone_jobs(
                    {
                        "company": "Example Employer",
                        "platform": "appone",
                        "url": board_url,
                        "listing_url": listing_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "appone")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Solutions Desk Analyst")
            self.assertEqual(candidate["location"], "Seattle, Washington")
            self.assertEqual(candidate["posted_at"], "2026-07-20T00:00:00+00:00")
            self.assertEqual(candidate["job_number"], "7185692")
            self.assertEqual(candidate["freshness_source"], "appone_date_posted")
            self.assertIn("application issues", candidate["_jd_text"])
            self.assertEqual(
                candidate["url"],
                job_search.normalize_job_url(detail_url),
            )

    def test_avature_adapter_uses_public_job_list_api_and_paginates(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://jobs.slalom.com/en_US/careersmarketplace/SearchJobs"
            props = {
                "uuid": "job-list-uuid",
                "hasToIncludePaginationOptions": False,
                "allowListSorting": True,
                "fetchJobIdInPeopleLists": True,
                "listType": "JobList",
                "firstColumnLinks": {},
                "additionalColumnLinks": {},
                "allowFilteringFromUrlParams": True,
                "layout": "custom",
                "links": {},
                "dynamicValueConfigs": [],
                "shouldAddBase64FileFields": False,
                "searchMode": "ResultsAndCount",
                "conditionalLinkConfig": {},
                "qtvc": "page-token",
                "formId": "job-search-form",
            }
            listing = (
                '<meta name="avature.portal.id" content="13">'
                '<meta name="avature.portal.urlPath" content="careersmarketplace">'
                '<meta name="avature.portal.lang" content="en_US">'
                f"<list data-props='{html.escape(json.dumps(props), quote=True)}'></list>"
            )

            def record(job_id, title, location, posted):
                return {
                    "id": job_id,
                    "fields": {
                        "name": {"stringValue": title},
                        "postedDate": {"stringValue": posted},
                        "req": {"stringValue": f"REQ-{job_id}"},
                        "description": {
                            "stringValue": (
                                "Build and test production software, APIs, and data "
                                "pipelines for client systems."
                            )
                        },
                        "locations": {"stringValue": location},
                        "department": {"stringValue": "Technology"},
                    },
                }

            first_page = {
                "total": 3,
                "results": [
                    record(101, "Software Engineer", "Seattle, WA", "2026-07-23"),
                    record(102, "Data Engineer", "Portland, OR", "2026-07-22"),
                ],
                "links": [
                    {
                        "detailPage": (
                            "https://jobs.slalom.com/en_US/careersmarketplace/"
                            "JobDetail?jobId=101"
                        )
                    },
                    {
                        "detailPage": (
                            "https://jobs.slalom.com/en_US/careersmarketplace/"
                            "JobDetail?jobId=102"
                        )
                    },
                ],
            }
            second_page = {
                "total": 3,
                "results": [
                    record(103, "QA Engineer", "Bellevue, WA", "2026-07-21"),
                ],
                "links": [
                    {
                        "detailPage": (
                            "https://jobs.slalom.com/en_US/careersmarketplace/"
                            "JobDetail?jobId=103"
                        )
                    }
                ],
            }

            def fake_api(_opener, url, _headers, timeout=20):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                self.assertEqual(query["filters"], ['{"search":"Seattle"}'])
                self.assertEqual(query["recordsPerPage"], ["2"])
                return second_page if query["offset"] == ["2"] else first_page

            with (
                mock.patch.object(
                    job_search,
                    "fetch_url_with_opener",
                    return_value=listing,
                ),
                mock.patch.object(
                    job_search,
                    "fetch_json_with_opener",
                    side_effect=fake_api,
                ) as fetch_api,
            ):
                candidates = job_search.discover_avature_jobs(
                    {
                        "company": "Example Consultancy",
                        "platform": "avature",
                        "url": board_url,
                        "search_queries": ["Seattle"],
                        "page_size": 2,
                        "max_pages": 3,
                        "description_field": "description",
                        "location_fields": ["locations"],
                        "department_field": "department",
                        "location_include_regex": r"\b[A-Za-z .'-]+,\s*WA\b",
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "avature")
            self.assertEqual(fetch_api.call_count, 2)
            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                {candidate["role"] for candidate in candidates},
                {"Software Engineer", "QA Engineer"},
            )
            first = next(
                candidate
                for candidate in candidates
                if candidate["role"] == "Software Engineer"
            )
            self.assertEqual(first["location"], "Seattle, WA")
            self.assertEqual(first["posted_at"], "2026-07-23T00:00:00+00:00")
            self.assertEqual(first["freshness_source"], "avature_posted_date")
            self.assertEqual(first["job_number"], "REQ-101")
            self.assertIn("production software", first["_jd_text"])
            self.assertIn("jobId=101", first["url"])
            self.assertEqual(job_search.source_quality({"platform": "avature"}), ("api_good", "official"))

    def test_official_rss_feed_quality_is_not_treated_as_updated_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            self.assertEqual(
                job_search.source_quality({"platform": "rss", "official_feed": True}),
                ("api_good", "official"),
            )
            self.assertEqual(
                job_search.source_quality(
                    {
                        "platform": "adp_myjobs",
                        "posted_at_quality_override": "first_seen_only",
                    }
                ),
                ("api_good", "first_seen_only"),
            )

    def test_jubilant_careers_adapter_filters_and_enriches_official_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://jubilantcareer.jubl.com/explorejobs/hsallergy"
            listing = {
                "jobList": [
                    {
                        "jobId": "41191",
                        "jobTitle": "Security Operations Analyst",
                        "locationDescription": "Spokane, Washington",
                        "functionalArea": "Digital and IT",
                        "company": "Jubilant HollisterStier Spokane",
                    },
                    {
                        "jobId": "50000",
                        "jobTitle": "Software Engineer",
                        "locationDescription": "Noida, India",
                        "functionalArea": "Digital and IT",
                        "company": "Jubilant Corporate",
                    },
                ]
            }
            detail = {
                "status": "010",
                "jobOpeningId": 41191,
                "jobtitle": "Security Operations Analyst",
                "jobpostingdate": "14/07/26",
                "locationdescr": "Spokane, Washington",
                "funct": "Digital and IT",
                "companydescr": "Jubilant HollisterStier Spokane",
                "jobdescr": (
                    "<p>Monitor EDR and SIEM alerts and maintain security systems.</p>"
                ),
            }

            def fake_fetch(url, timeout=20):
                return detail if "getJobDetails/41191" in url else listing

            with mock.patch.object(job_search, "fetch_json", side_effect=fake_fetch) as fetch:
                candidates = job_search.discover_jubilant_careers_jobs(
                    {
                        "company": "Jubilant HollisterStier",
                        "platform": "jubilant_careers",
                        "url": board_url,
                        "location_keywords": ["Spokane, Washington"],
                        "company_contains": "Jubilant HollisterStier Spokane",
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "jubilant_careers")
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Security Operations Analyst")
            self.assertEqual(candidate["location"], "Spokane, Washington")
            self.assertEqual(candidate["posted_at"], "2026-07-14T00:00:00+00:00")
            self.assertEqual(candidate["job_number"], "41191")
            self.assertEqual(candidate["source_query"], "Digital and IT")
            self.assertEqual(
                candidate["freshness_source"],
                "jubilant_official_posting_date",
            )
            self.assertIn("EDR and SIEM", candidate["_jd_text"])
            self.assertIn("/jobprofile/41191/home", candidate["url"])

    def test_jazzhr_adapter_enriches_listings_from_detail_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://seattletimes.applytojob.com/apply/jobs"
            detail_url = "https://seattletimes.applytojob.com/apply/jobs/details/abc123"
            listing = """
                <table>
                  <tr>
                    <td><a class="job_title_link" href="/apply/jobs/details/abc123?&">Software Engineer</a></td>
                    <td>Seattle, WA</td>
                  </tr>
                </table>
            """
            detail = """
                <script type="application/ld+json">
                {
                  "@context": "https://schema.org",
                  "@type": "JobPosting",
                  "url": "https://seattletimes.applytojob.com/apply/abc123/software-engineer",
                  "title": "Software Engineer",
                  "datePosted": "2026-07-22",
                  "description": "<p>Build newsroom data systems.</p>",
                  "jobLocation": {
                    "@type": "Place",
                    "address": {"addressLocality": "Seattle", "addressRegion": "WA"}
                  }
                }
                </script>
            """

            def fake_fetch(url, timeout=20):
                return listing if url == board_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_jazzhr_jobs(
                    {"company": "The Seattle Times", "platform": "jazzhr", "url": board_url}
                )

            self.assertEqual(job_search.detect_platform(board_url), "jazzhr")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Software Engineer")
            self.assertEqual(candidate["location"], "Seattle, WA")
            self.assertEqual(candidate["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidate["external_job_id"], "abc123")
            self.assertEqual(candidate["freshness_source"], "jazzhr_json_ld_date_posted")
            self.assertNotEqual(candidate["url"], detail_url)

    def test_jazzhr_adapter_keeps_listing_location_without_jobposting_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://opala.applytojob.com/apply/jobs"
            listing = """
                <table><tr>
                  <td><a class="job_title_link" href="/apply/jobs/details/abc123?&">Platform Engineer</a></td>
                  <td>Remote</td>
                </tr></table>
            """
            detail = """
                <script type="application/ld+json">
                {"@type":"Organization","name":"Example"}
                </script>
            """

            def fake_fetch(url, timeout=20):
                return listing if url == board_url else detail

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_jazzhr_jobs(
                    {"company": "Opala", "platform": "jazzhr", "url": board_url}
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["location"], "Remote")
            self.assertEqual(candidates[0]["freshness_source"], "unknown")

    def test_hiringthing_adapter_enriches_listings_from_detail_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://sequoyah.viewpointhr-ats.com/"
            detail_url = "https://sequoyah.viewpointhr-ats.com/job/899524/project-manager-data-center"
            board_html = """
                <div class="job-container" data-job-id="899524">
                  <a href="/job/899524/project-manager-data-center">
                    <h2>Project Manager - Data Center</h2>
                  </a>
                  <div class="job-location">Redmond, WA</div>
                  <div class="job-category"><span>Critical Facilities</span></div>
                  <div class="job-description">Coordinate critical infrastructure projects.</div>
                </div>
            """
            detail_html = f"""
                <script type="application/ld+json">
                {{
                  "@context": "https://schema.org",
                  "@type": "JobPosting",
                  "title": "Project Manager - Data Center",
                  "datePosted": "2026-07-22T09:00:00-07:00",
                  "description": "<p>Deliver data-center infrastructure projects.</p>",
                  "url": "{detail_url}",
                  "jobLocation": {{
                    "@type": "Place",
                    "address": {{
                      "@type": "PostalAddress",
                      "addressLocality": "Redmond",
                      "addressRegion": "WA",
                      "addressCountry": "US"
                    }}
                  }}
                }}
                </script>
            """

            def fake_fetch(url, timeout=20):
                return detail_html if "/job/" in url else board_html

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_hiringthing_jobs(
                    {
                        "company": "Sequoyah Electric",
                        "platform": "hiringthing",
                        "url": board_url,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "hiringthing")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Project Manager - Data Center")
            self.assertEqual(candidate["external_job_id"], "899524")
            self.assertIn("Redmond", candidate["location"])
            self.assertEqual(candidate["posted_at"], "2026-07-22T16:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "hiringthing_json_ld_date_posted")
            self.assertIn("data-center infrastructure", candidate["_jd_text"])

    def test_paycor_adapter_parses_newton_rows_and_detail_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://recruitingbypaycor.com/career/CareerHome.action?"
                "clientId=example-client"
            )
            detail_url = (
                "https://recruitingbypaycor.com/career/JobIntroduction.action?"
                "clientId=example-client&id=example-job&source=&lang=en"
            )
            board_html = f"""
                <div class="gnewtonCareerGroupRowClass">
                  <div class="gnewtonCareerGroupJobTitleClass">
                    <a href="{detail_url}">IT Support Specialist</a>
                  </div>
                  <div class="gnewtonCareerGroupJobDescriptionClass">
                    Spokane, WA
                  </div>
                </div>
            """
            detail_html = """
                <td id="gnewtonJobDescriptionText">
                  <div><b>Location:</b> Spokane, WA</div>
                  <p>Support business applications, devices, and network access.</p>
                </td>
            """

            def fake_fetch(url, timeout=20):
                return detail_html if "JobIntroduction.action" in url else board_html

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_paycor_jobs(
                    {
                        "company": "Cowles Company",
                        "platform": "paycor",
                        "url": board_url,
                        "detail_workers": 1,
                    }
                )

            self.assertEqual(job_search.detect_platform(board_url), "paycor")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "IT Support Specialist")
            self.assertEqual(candidate["location"], "Spokane, WA")
            self.assertEqual(candidate["external_job_id"], "example-job")
            self.assertEqual(candidate["posted_at"], "")
            self.assertIn("business applications", candidate["_jd_text"])

    def test_wp_search_index_adapter_filters_terms_and_keeps_official_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            api_url = "https://example.com/wp-json/jobs/index.json"
            records = [
                {
                    "id": 3014,
                    "title": {"rendered": "Early Career Electrical Engineer"},
                    "excerpt": {"rendered": "<p>Support site engineering systems.</p>"},
                    "content": {"rendered": "<p>Build reliable technical workflows.</p>"},
                    "link": "/jobs/early-career-electrical-engineer/",
                    "date_formatted": "23 Jul 2026",
                    "reference": "2026-3014",
                    "terms": {
                        "ts_geographical_location": [
                            {"name": "United States"},
                            {"name": "Washington"},
                        ],
                        "ts_site": [{"name": "Richland"}],
                    },
                },
                {
                    "id": 9999,
                    "title": {"rendered": "Engineer"},
                    "link": "/jobs/virginia-engineer/",
                    "date_formatted": "22 Jul 2026",
                    "terms": {
                        "ts_geographical_location": [{"name": "Virginia"}],
                    },
                },
            ]
            source = {
                "company": "Framatome",
                "platform": "wp_search_index",
                "url": "https://example.com/jobs/",
                "api_url": api_url,
                "required_terms": ["Washington"],
            }
            with mock.patch.object(job_search, "fetch_json", return_value=records):
                candidates = job_search.discover_wp_search_index_jobs(source)

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Early Career Electrical Engineer")
            self.assertEqual(candidate["location"], "United States, Washington, Richland")
            self.assertEqual(candidate["posted_at"], "2026-07-23T00:00:00+00:00")
            self.assertEqual(candidate["external_job_id"], "2026-3014")
            self.assertEqual(candidate["freshness_source"], "wp_search_index_official_date")
            self.assertIn("technical workflows", candidate["_jd_text"])

    def test_joveo_adapter_uses_geo_filter_and_official_start_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            api_url = "https://example.joveo.site/jobs-api/v2/clients/example/jobs/search"
            response = {
                "totalPages": 1,
                "totalRecords": 1,
                "records": [
                    {
                        "id": "internal-id",
                        "externalId": "R-240001",
                        "referenceNumber": "R-240001",
                        "urlSlug": "data-analyst-in-vancouver-r-240001",
                        "title": "Data Analyst",
                        "description": "<p>Build reliable reporting and validation workflows.</p>",
                        "careerSiteApplyUrl": "https://workday.example/jobs/R-240001",
                        "normalisedFields": {
                            "title": "Data Analyst",
                            "city": "Vancouver",
                            "stateCode": "WA",
                            "countryCode": "US",
                        },
                        "startDate": "2026-07-22T00:00:00.000+00:00",
                        "updatedAt": "2026-07-23T12:00:00.000+00:00",
                    }
                ],
            }
            source = {
                "company": "Example Employer",
                "platform": "joveo",
                "url": "https://jobs.example.com/jobs",
                "public_base_url": "https://jobs.example.com",
                "api_url": api_url,
                "latitude": 45.6387,
                "longitude": -122.6615,
                "distance": 50,
            }
            with mock.patch.object(
                job_search,
                "fetch_json_post",
                return_value=response,
            ) as fetch:
                candidates = job_search.discover_joveo_jobs(source)

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Data Analyst")
            self.assertEqual(candidate["location"], "Vancouver, WA, US")
            self.assertEqual(candidate["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidate["external_job_id"], "R-240001")
            self.assertEqual(candidate["freshness_source"], "joveo_start_date")
            self.assertIn("validation workflows", candidate["_jd_text"])
            self.assertEqual(
                candidate["url"],
                "https://jobs.example.com/job/data-analyst-in-vancouver-r-240001",
            )
            payload = fetch.call_args.args[1]
            self.assertEqual(payload["filters"][0]["filterValue"]["distance"], 50.0)
            self.assertEqual(payload["pageSize"], 100)

    def test_clinch_adapter_filters_state_and_enriches_json_ld(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = (
                "https://www.amentumcareers.com/jobs/search"
                "?query=software&page=99"
            )
            detail_url = (
                "https://www.amentumcareers.com/jobs/"
                "software-engineer-richland-washington-united-states"
            )
            board_html = f"""
                <table>
                  <tr data-job-url="{detail_url}">
                    <td class="job-search-results-title">
                      <a href="{detail_url}">Software Engineer</a>
                    </td>
                    <td aria-label="Requisition Identifier: R0166687">R0166687</td>
                    <td class="job-search-results-location">
                      <li aria-label="Location: Richland, Washington, United States">
                        Richland, Washington, United States
                      </li>
                    </td>
                  </tr>
                </table>
            """
            detail_html = f"""
                <script type="application/ld+json">
                {{
                  "@context": "https://schema.org",
                  "@type": "JobPosting",
                  "title": "Software Engineer",
                  "url": "{detail_url}",
                  "datePosted": "2026-07-23T14:47:08Z",
                  "description": "<p>Develop and validate production software systems.</p>",
                  "hiringOrganization": {{"name": "Amentum"}},
                  "jobLocation": [{{
                    "@type": "Place",
                    "address": {{
                      "addressLocality": "Richland",
                      "addressRegion": "Washington",
                      "addressCountry": "US"
                    }}
                  }}]
                }}
                </script>
            """

            def fake_fetch(url, timeout=20):
                return detail_html if url == detail_url else board_html

            source = {
                "company": "Amentum",
                "platform": "clinch",
                "url": board_url,
                "search_params": {
                    "country_codes[]": ["US"],
                    "states[]": ["Washington"],
                },
                "detail_workers": 1,
            }
            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch) as fetch:
                candidates = job_search.discover_clinch_jobs(source)

            self.assertEqual(job_search.detect_platform(board_url), "clinch")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Software Engineer")
            self.assertEqual(candidate["external_job_id"], "R0166687")
            self.assertIn("Richland", candidate["location"])
            self.assertEqual(candidate["posted_at"], "2026-07-23T14:47:08+00:00")
            self.assertEqual(candidate["freshness_source"], "clinch_json_ld_date_posted")
            self.assertIn("validate production software", candidate["_jd_text"])
            listing_url = fetch.call_args_list[0].args[0]
            self.assertIn("states%5B%5D=Washington", listing_url)
            self.assertIn("country_codes%5B%5D=US", listing_url)
            self.assertIn("query=software", listing_url)
            self.assertIn("page=1", listing_url)
            self.assertNotIn("page=99", listing_url)
            self.assertEqual(listing_url.count("?"), 1)

    def test_atkins_adapter_uses_public_token_and_filters_washington(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            response = {
                "jobs": [
                    {
                        "id": 1001,
                        "job_requisition_id": "R-160001",
                        "job_posting_title": "Data Lab Technologist",
                        "job_description": "<p>Build data and software workflows.</p>",
                        "person_requirements": "<p>Python, SQL, and AWS.</p>",
                        "location_mappings": [
                            "Richland, Washington, United States of America"
                        ],
                        "external_posting_url": (
                            "https://example.myworkdayjobs.com/Careers/job/"
                            "Richland/Data-Lab-Technologist_R-160001"
                        ),
                        "start_date": "2026-07-23T00:00:00.000Z",
                        "last_functionally_updated": "2026-07-23T12:00:00.000Z",
                    },
                    {
                        "id": 1002,
                        "job_requisition_id": "R-160002",
                        "job_posting_title": "Engineer",
                        "location_mappings": [
                            "Portland, Oregon, United States of America"
                        ],
                        "start_date": "2026-07-22T00:00:00.000Z",
                    },
                    {
                        "id": 1003,
                        "job_requisition_id": "R-160003",
                        "job_posting_title": "Contracts Manager",
                        "location_mappings": [
                            "Washington, United States of America"
                        ],
                        "start_date": "2026-07-21T00:00:00.000Z",
                    },
                ],
                "meta": {
                    "totalCount": 3,
                    "perPage": 50,
                    "totalPages": 1,
                    "currentPage": 1,
                },
            }
            source = {
                "company": "AtkinsRéalis",
                "platform": "atkins_jobs",
                "url": "https://careers.atkinsrealis.com/en/search-results",
                "required_location_patterns": [
                    ",\\s*Washington\\b",
                    "\\bWA\\b",
                ],
            }
            with (
                mock.patch.object(
                    job_search,
                    "fetch_json",
                    return_value={"token": "public-short-lived-token"},
                ),
                mock.patch.object(
                    job_search,
                    "fetch_json_post_with_headers",
                    return_value=response,
                ) as fetch,
            ):
                candidates = job_search.discover_atkins_jobs(source)

            self.assertEqual(
                job_search.detect_platform(source["url"]),
                "atkins_jobs",
            )
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Data Lab Technologist")
            self.assertEqual(candidate["external_job_id"], "R-160001")
            self.assertEqual(candidate["posted_at"], "2026-07-23T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "atkins_official_start_date")
            self.assertIn("Python, SQL, and AWS", candidate["_jd_text"])
            self.assertEqual(
                candidate["url"],
                "https://careers.atkinsrealis.com/en/jobs/"
                "data-lab-technologist-r-160001",
            )
            self.assertEqual(
                fetch.call_args.args[2]["Authorization"],
                "Bearer public-short-lived-token",
            )

    def test_isg_poweredby_adapter_uses_public_blob_and_marks_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Panorama",
                "platform": "isg_poweredby",
                "url": "https://www.panorama.org/careers/",
                "blob_id": "068efc8c-94eb-446e-8df2-0cb3af944955",
            }
            response = [
                {
                    "JobId": 90001,
                    "Title": "IT Network Admin Specialist",
                    "Description": "<p>Support networks, systems, and users.</p>",
                    "ExperienceText": "1 to 3 years",
                    "EducationText": "Bachelor's degree",
                    "SalaryRange": "$70,000-$85,000",
                    "ApplyMode": 1,
                    "ApplyUrl": "",
                    "Locations": [{"City": "Lacey", "StateCode": "WA"}],
                }
            ]
            with mock.patch.object(
                job_search,
                "fetch_json",
                return_value=response,
            ) as fetch:
                candidates = job_search.discover_isg_poweredby_jobs(source)

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "IT Network Admin Specialist")
            self.assertEqual(candidate["location"], "Lacey, WA")
            self.assertEqual(candidate["posted_at"], "")
            self.assertEqual(candidate["external_job_id"], "90001")
            self.assertEqual(
                candidate["url"],
                "https://jobs.localjobnetwork.com/apply/add/90001",
            )
            self.assertIn("Support networks, systems, and users", candidate["_jd_text"])
            self.assertIn("first_seen", candidate["notes"])
            self.assertEqual(
                fetch.call_args.args[0],
                "https://isgpoweredbydata.blob.core.windows.net/public-data/"
                "068efc8c-94eb-446e-8df2-0cb3af944955.json",
            )

    def test_embedded_jobs_adapter_keeps_each_card_date_and_title_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source_url = "https://example.com/careers"
            raw = """
                <div class="job-card">
                  <p class="posted">July 21, 2026</p>
                  <h3 class="job-title">Site Implementation Engineer</h3>
                  <a href="https://www.linkedin.com/jobs/view/111/">See Job Posting</a>
                  <div class="summary"><p>Deploy charging stations.</p></div>
                </div>
                <div class="job-card">
                  <p class="posted">July 22, 2026</p>
                  <h3 class="job-title">Software Engineer</h3>
                  <a href="/jobs/222">See Job Posting</a>
                  <div class="summary"><p>Build charging software.</p></div>
                </div>
            """
            source = {
                "company": "Electric Era",
                "platform": "embedded_jobs",
                "url": source_url,
                "title_regex": r'class="job-title"[^>]*>(.*?)</h3>',
                "job_url_regex": r'href=["\']([^"\']+)["\']',
                "date_regex": r'class="posted"[^>]*>(.*?)</p>',
                "description_regex": r'class="summary"[^>]*>.*?<p>(.*?)</p>',
                "job_id_regex": r"/(?:view|jobs)/(\d+)",
                "default_location": "Seattle, WA",
            }
            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_embedded_jobs(source)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0]["role"], "Site Implementation Engineer")
            self.assertEqual(candidates[0]["posted_at"], "2026-07-21T00:00:00+00:00")
            self.assertEqual(candidates[0]["external_job_id"], "111")
            self.assertIn("Deploy charging stations", candidates[0]["_jd_text"])
            self.assertEqual(candidates[1]["role"], "Software Engineer")
            self.assertEqual(candidates[1]["posted_at"], "2026-07-22T00:00:00+00:00")
            self.assertEqual(candidates[1]["url"], "https://example.com/jobs/222")
            self.assertEqual(candidates[1]["location"], "Seattle, WA")

    def test_embedded_jobs_adapter_reads_named_date_and_location_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            raw = """
              <h4 data-id="data-analyst">
                Healthcare Data Analyst
                <span>Aberdeen, WA</span>
                <time>06/09/26</time>
              </h4>
            """
            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_embedded_jobs(
                    {
                        "company": "Example Health",
                        "platform": "embedded_jobs",
                        "url": "https://example.org/careers",
                        "title_regex": (
                            r'<h4 data-id="(?P<job_id>[^"]+)">\s*'
                            r"(?P<title>[^<]+)<span>(?P<location>[^<]+)</span>\s*"
                            r"<time>(?P<date>[^<]+)</time>"
                        ),
                        "job_url_template": "https://example.org/careers?job={job_id}",
                    }
                )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["role"], "Healthcare Data Analyst")
            self.assertEqual(candidates[0]["location"], "Aberdeen, WA")
            self.assertEqual(candidates[0]["posted_at"], "2026-06-09T00:00:00+00:00")

    def test_embedded_jobs_adapter_builds_unique_urls_for_same_page_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source_url = "https://example.com/careers"
            raw = """
                <h2 id="FSAE">Field Service and Applications Engineer</h2>
                <p>Diagnose software and instrument issues.</p>
                <h2 id="SSE">Software Engineer</h2>
                <p>Build cloud analysis software.</p>
            """
            source = {
                "company": "Curi Bio",
                "platform": "embedded_jobs",
                "url": source_url,
                "title_regex": r'<h2 id=["\'](?P<job_id>[^"\']+)["\']>(?P<title>.*?)</h2>',
                "job_url_template": f"{source_url}?position={{job_id}}",
                "description_regex": r"<p>(.*?)</p>",
                "default_location": "Seattle, WA",
            }
            with mock.patch.object(job_search, "fetch_url", return_value=raw):
                candidates = job_search.discover_embedded_jobs(source)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0]["external_job_id"], "FSAE")
            self.assertEqual(candidates[0]["url"], f"{source_url}?position=FSAE")
            self.assertEqual(candidates[1]["external_job_id"], "SSE")
            self.assertEqual(candidates[1]["url"], f"{source_url}?position=SSE")
            self.assertNotEqual(candidates[0]["url"], candidates[1]["url"])

    def test_talentbrew_browse_pages_parse_regional_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Kaiser Permanente",
                "platform": "talentbrew",
                "url": "https://www.kaiserpermanentejobs.org/location/washington-jobs/641/6252001-5815135/3/1",
                "browse_pages": True,
                "browse_url_template": (
                    "https://www.kaiserpermanentejobs.org/location/"
                    "washington-jobs/641/6252001-5815135/3/{page}"
                ),
                "max_pages": 3,
                "fetch_details": False,
                "fetch_detail_limit": 0,
            }
            first_page = """
            <ul class="branded-list__list">
              <li class="branded-list__list-item">
                <a href="/job/renton/application-analyst/641/12345"
                   data-job-id="12345">
                  <h2>Application Analyst</h2>
                  <span class="job-location">Renton, WA</span>
                </a>
              </li>
            </ul>
            """
            second_page = """
            <ul class="branded-list__list">
              <li class="branded-list__list-item">
                <a href="/job/seattle/it-support-specialist/641/67890"
                   data-job-id="67890">
                  <div class="job-result">
                    <h2>IT Support Specialist</h2>
                    <p>Seattle, WA</p>
                  </div>
                </a>
              </li>
            </ul>
            """
            requested_urls = []

            def fake_fetch(url, timeout=30):
                requested_urls.append(url)
                if url.endswith("/1"):
                    return first_page
                if url.endswith("/2"):
                    return second_page
                if url.endswith("/3"):
                    return "<p>No jobs found.</p>"
                raise AssertionError(f"Unexpected URL: {url}")

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_talentbrew_jobs(source)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                {candidate["role"] for candidate in candidates},
                {"Application Analyst", "IT Support Specialist"},
            )
            self.assertEqual(
                {candidate["location"] for candidate in candidates},
                {"Renton, WA", "Seattle, WA"},
            )
            self.assertEqual(
                {candidate["external_job_id"] for candidate in candidates},
                {"12345", "67890"},
            )
            self.assertEqual(len(requested_urls), 3)

    def test_wordpress_taleo_adapter_maps_public_ajax_jobs_and_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            board_url = "https://www.mckinstry.com/join-us/jobs/"
            board_html = '<input type="hidden" id="nonce4" name="nonce4" value="public-nonce" />'
            api_data = {
                "jobLocations": [
                    {
                        "id": 1,
                        "locationName": "WA - Seattle",
                        "city": "Seattle",
                        "state": "US-WA",
                        "countryCode": "US",
                    }
                ],
                "jobs": [
                    {
                        "id": 1234,
                        "title": "Application Support Engineer",
                        "location": 1,
                        "link": "https://www.mckinstry.com/join-us/jobs/1234",
                        "jobFamily": ["Information Technology"],
                        "description": "Support business applications and automation.",
                    }
                ],
            }

            def fake_post(url, payload, timeout=20):
                self.assertEqual(url, "https://www.mckinstry.com/wp-admin/admin-ajax.php")
                self.assertEqual(payload, {"action": "get_jobs_data", "nonce": "public-nonce"})
                return api_data

            with mock.patch.object(job_search, "fetch_url", return_value=board_html):
                with mock.patch.object(job_search, "fetch_json_form_post", side_effect=fake_post):
                    candidates = job_search.discover_wordpress_taleo_jobs(
                        {"company": "McKinstry", "platform": "wordpress_taleo", "url": board_url}
                    )

            self.assertEqual(job_search.detect_platform(board_url), "wordpress_taleo")
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["role"], "Application Support Engineer")
            self.assertIn("Seattle", candidate["location"])
            self.assertEqual(candidate["external_job_id"], "1234")
            self.assertEqual(candidate["source_query"], "Information Technology")
            self.assertIn("automation", candidate["_jd_text"])

    def test_eightfold_source_can_filter_shared_board_by_operating_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {"operating_companies": ["Fluke"]}

            self.assertTrue(
                job_search.eightfold_job_matches_source(
                    source, {"efcustomTextOperatingcompany": ["Fluke"]}
                )
            )
            self.assertFalse(
                job_search.eightfold_job_matches_source(
                    source, {"efcustomTextOperatingcompany": ["ServiceChannel"]}
                )
            )

    def test_eightfold_html_fallback_merges_multiple_filtered_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            pages = {
                "https://jobs.example.com/careers/location": {
                    "positions": [
                        {
                            "id": 101,
                            "name": "Application Analyst",
                            "canonicalPositionUrl": "/careers/job/101",
                            "locations": ["Seattle, Washington, United States"],
                            "t_create": 1784822400,
                        }
                    ]
                },
                "https://jobs.example.com/careers/technology": {
                    "positions": [
                        {
                            "id": 101,
                            "name": "Application Analyst",
                            "canonicalPositionUrl": "/careers/job/101",
                            "locations": ["Seattle, Washington, United States"],
                            "t_create": 1784822400,
                        },
                        {
                            "id": 202,
                            "name": "Data Engineer",
                            "canonicalPositionUrl": "/careers/job/202",
                            "locations": ["Remote, United States"],
                            "t_create": 1784822400,
                        },
                    ]
                },
            }

            def fake_fetch(url, **_kwargs):
                return f"<script>window.state = {json.dumps(pages[url])}</script>"

            source = {
                "company": "Example",
                "platform": "eightfold",
                "url": "https://jobs.example.com/careers",
                "base_url": "https://jobs.example.com",
                "domain": "example.com",
                "html_urls": list(pages),
            }

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_eightfold_html_jobs(source)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                {candidate["role"] for candidate in candidates},
                {"Application Analyst", "Data Engineer"},
            )
            self.assertTrue(
                all(candidate["source_query"] in pages for candidate in candidates)
            )

    def test_json_ld_parser_preserves_html_entities_inside_json_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            raw = """
                <script type="application/ld+json">
                {"@type":"JobPosting","title":"Data Analyst","datePosted":"2026-07-22",
                 "description":"Use &quot;quoted&quot; values","url":"https://example.com/jobs/1"}
                </script>
            """

            jobs = job_search.parse_json_ld_jobs(raw, "https://example.com/jobs/1", "Example")

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["posted_at"], "2026-07-22T00:00:00+00:00")

    def test_discovery_writes_relocation_and_unknown_location_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            profile = job_search.load_profile()
            posted_at = job_search.now_utc_iso()
            candidates = [
                {
                    "company": "RelocateCo",
                    "role": "Software Engineer",
                    "url": "https://example.com/jobs/relocate",
                    "platform": "custom",
                    "location": "Austin, TX",
                    "posted_at": posted_at,
                },
                {
                    "company": "UnknownCo",
                    "role": "Software Engineer",
                    "url": "https://example.com/jobs/unknown",
                    "platform": "custom",
                    "location": "",
                    "posted_at": posted_at,
                },
            ]

            stats = job_search.process_discovered_candidates(
                candidates,
                discover_args(),
                profile,
                {"jobs": {}},
                job_search.dt.datetime.now(job_search.dt.timezone.utc) - job_search.dt.timedelta(days=1),
                job_search.now_utc_iso(),
            )
            apps = {app["company"]: app for app in job_search.load_tracker()["applications"]}

            self.assertEqual(stats["added"], 2)
            self.assertEqual(apps["RelocateCo"]["location_bucket"], "relocation")
            self.assertEqual(apps["RelocateCo"]["status"], "found")
            self.assertEqual(apps["UnknownCo"]["location_bucket"], "maybe")
            self.assertEqual(apps["UnknownCo"]["status"], "needs_review")
            self.assertEqual(apps["UnknownCo"]["review_bucket"], "maybe")

    def test_bot_challenge_is_treated_as_a_fetch_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            reason = job_search.job_text_fetch_failure_reason(
                "JavaScript is disabled. We need to verify that you're not a robot."
            )
            self.assertIn("verify", reason.lower())

    def test_jobsyn_adapter_uses_origin_header_and_merges_locations_by_requisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Jacobs",
                "platform": "jobsyn",
                "url": "https://jacobs.jobs/jobs/",
                "origin": "jacobs.jobs",
                "job_url_template": "https://careers.jacobs.com/en_US/careers/JobDetail/{role_slug}/{reqid}",
                "keywords": ["AI Engineer"],
                "page_size": 20,
                "max_pages": 1,
            }
            payload = {
                "jobs": [
                    {
                        "title_exact": "AI Engineer",
                        "location_exact": "Seattle, WA",
                        "reqid": "42414",
                        "guid": "SEATTLE-GUID",
                        "title_slug": "ai-engineer",
                        "date_new": "2026-07-18T03:36:08Z",
                        "date_updated": "2026-07-18T03:36:08Z",
                        "description": "<p>Build agentic AI systems.</p>",
                    },
                    {
                        "title_exact": "AI Engineer",
                        "location_exact": "San Francisco, CA",
                        "reqid": "42414",
                        "guid": "SF-GUID",
                        "title_slug": "ai-engineer",
                        "date_new": "2026-07-18T03:36:08Z",
                        "date_updated": "2026-07-18T03:36:08Z",
                        "description": "<p>Build agentic AI systems.</p>",
                    },
                ],
                "pagination": {"has_more_pages": False},
            }

            class FakeResponse:
                headers = mock.Mock()

                def __enter__(self):
                    self.headers.get_content_charset.return_value = "utf-8"
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return json.dumps(payload).encode()

            with mock.patch.object(job_search.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
                jobs = job_search.discover_jobsyn_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["job_number"], "42414")
            self.assertEqual(jobs[0]["external_job_id"], "42414")
            self.assertEqual(
                jobs[0]["url"],
                "https://careers.jacobs.com/en_US/careers/JobDetail/AI-Engineer/42414",
            )
            self.assertIn("Seattle, WA", jobs[0]["location"])
            self.assertIn("San Francisco, CA", jobs[0]["location"])
            self.assertIn("agentic AI systems", jobs[0]["_jd_text"])
            request = urlopen.call_args.args[0]
            self.assertEqual(request.get_header("X-origin"), "jacobs.jobs")

    def test_jobsyn_adapter_supports_full_board_featured_jobs_and_job_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Alaska Air Group",
                "platform": "jobsyn",
                "url": "https://careers.alaskaair.com/jobs/",
                "origin": "careers.alaskaair.com",
                "api_url": (
                    "https://prod-search-api.jobsyn.org/api/v1/solr/search"
                    "?buids=1318%2C35119"
                ),
                "job_url_template": (
                    "https://careers.alaskaair.com/{location_slug}/"
                    "{title_slug}/{guid}/job/"
                ),
                "search_all": True,
                "use_job_company": True,
                "page_size": 10,
                "max_pages": 1,
            }
            payload = {
                "featured_jobs": [
                    {
                        "company_exact": "Hawaiian Airlines",
                        "title_exact": "Systems Analyst",
                        "location_exact": "Seattle, WA",
                        "reqid": "2026-20001",
                        "guid": "FEATURED-GUID",
                        "title_slug": "systems-analyst",
                        "date_new": "2026-07-22T03:36:08Z",
                    }
                ],
                "jobs": [
                    {
                        "company_exact": "Alaska Airlines",
                        "title_exact": "Software Engineer",
                        "location_exact": "SeaTac, WA",
                        "reqid": "2026-20002",
                        "guid": "JOB-GUID",
                        "title_slug": "software-engineer",
                        "date_new": "2026-07-23T03:36:08Z",
                    }
                ],
                "pagination": {"has_more_pages": False},
            }

            class FakeResponse:
                headers = mock.Mock()

                def __enter__(self):
                    self.headers.get_content_charset.return_value = "utf-8"
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return json.dumps(payload).encode()

            with mock.patch.object(
                job_search.urllib.request,
                "urlopen",
                return_value=FakeResponse(),
            ) as urlopen:
                jobs = job_search.discover_jobsyn_jobs(source)

            self.assertEqual(len(jobs), 2)
            by_number = {job["job_number"]: job for job in jobs}
            self.assertEqual(
                by_number["2026-20001"]["company"],
                "Hawaiian Airlines",
            )
            self.assertEqual(
                by_number["2026-20002"]["url"],
                "https://careers.alaskaair.com/seatac-wa/software-engineer/"
                "JOB-GUID/job",
            )
            request_url = urlopen.call_args.args[0].full_url
            self.assertNotIn("q=", request_url)
            self.assertIn("page=1", request_url)

    def test_workday_recruiting_url_preserves_tenant_and_enriches_multi_location_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Snap Inc.",
                "platform": "workday",
                "host": "wd1.myworkdaysite.com",
                "tenant": "snapchat",
                "site": "snap",
                "url": "https://wd1.myworkdaysite.com/recruiting/snapchat/snap",
                "keywords": ["Software Engineer"],
                "page_size": 20,
                "max_pages": 1,
            }
            search_payload = {
                "jobPostings": [
                    {
                        "title": "Software Engineer, Full Stack, Level 4",
                        "externalPath": (
                            "/job/Los-Angeles-California/"
                            "Software-Engineer--Full-Stack--Level-4_Q326SWEFS2-1"
                        ),
                        "locationsText": "6 Locations",
                        "postedOn": "Posted 18 Days Ago",
                        "bulletFields": ["Q326SWEFS2"],
                    }
                ]
            }
            detail_payload = {
                "jobPostingInfo": {
                    "title": "Software Engineer, Full Stack, Level 4",
                    "location": "Los Angeles, California",
                    "additionalLocations": [
                        "Bellevue - 110 110th Ave NE",
                        "Seattle - 2025 1st Avenue",
                    ],
                    "postedOn": "Posted 18 Days Ago",
                }
            }

            with mock.patch.object(job_search, "fetch_json_post", return_value=search_payload):
                with mock.patch.object(job_search, "fetch_json", return_value=detail_payload) as fetch_detail:
                    jobs = job_search.discover_workday_jobs(source)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                jobs[0]["url"],
                (
                    "https://wd1.myworkdaysite.com/recruiting/snapchat/snap/job/"
                    "Los-Angeles-California/"
                    "Software-Engineer--Full-Stack--Level-4_Q326SWEFS2-1"
                ),
            )
            self.assertIn("Bellevue", jobs[0]["location"])
            self.assertIn("Seattle", jobs[0]["location"])
            fetch_detail.assert_called_once()

    def test_workday_candidate_from_recruiting_url_keeps_board_and_all_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            url = (
                "https://wd1.myworkdaysite.com/recruiting/snapchat/snap/job/"
                "Los-Angeles-California/"
                "Software-Engineer--Full-Stack--Level-4_Q326SWEFS2-1"
            )
            detail_payload = {
                "hiringOrganization": {"name": "Snap Inc."},
                "jobPostingInfo": {
                    "title": "Software Engineer, Full Stack, Level 4",
                    "jobReqId": "Q326SWEFS2",
                    "location": "Los Angeles, California",
                    "additionalLocations": ["Bellevue, Washington", "Seattle, Washington"],
                    "postedOn": "Posted 18 Days Ago",
                },
            }

            with mock.patch.object(job_search, "fetch_json", return_value=detail_payload):
                candidate = job_search.workday_candidate_from_url(url)

            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["url"], url)
            self.assertEqual(
                candidate["source"],
                "https://wd1.myworkdaysite.com/recruiting/snapchat/snap",
            )
            self.assertIn("Bellevue", candidate["location"])
            self.assertIn("Seattle", candidate["location"])
            self.assertEqual(
                job_search.workday_human_url(
                    "example.wd5.myworkdayjobs.com",
                    "External",
                    "/job/Seattle/Software-Engineer_R1",
                    tenant="example",
                ),
                "https://example.wd5.myworkdayjobs.com/External/job/Seattle/Software-Engineer_R1",
            )

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
            "role": "Technical Support Specialist",
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

    def test_maybe_backlog_title_prefilter_rejects_unrelated_roles(self):
        candidate = {
            "company": "HospitalCo",
            "role": "Registered Nurse Specialist",
            "url": "https://example.com/jobs/nurse",
            "platform": "custom",
            "location": "Seattle, WA",
        }
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "HospitalCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([candidate], "")):
                job_search.command_discover_jobs(discover_args(include_maybe_backlog=True))

            tracker = json.loads((private_root / "data" / "applications.json").read_text(encoding="utf-8"))
            report_path = next((private_root / "data" / "discovery_runs").glob("*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(tracker["applications"], [])
            self.assertEqual(report["totals"]["skipped_title"], 1)

    def test_relevant_maybe_candidate_is_scored_with_per_source_limit(self):
        candidate = {
            "company": "SupportCo",
            "role": "Integrations Support Specialist",
            "url": "https://example.com/jobs/support",
            "platform": "custom",
            "location": "Seattle, WA",
        }
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [{"company": "SupportCo", "platform": "custom", "url": "https://example.com/jobs"}])
            job_search = load_job_search(private_root)
            with mock.patch.object(job_search, "source_candidates_subprocess", return_value=([candidate], "")):
                with mock.patch.object(job_search, "command_score_job") as score_job:
                    job_search.command_discover_jobs(
                        discover_args(include_maybe_backlog=True, score=True, score_maybe_limit=1)
                    )

            report_path = next((private_root / "data" / "discovery_runs").glob("*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            score_job.assert_called_once()
            self.assertEqual(report["totals"]["maybe_scored"], 1)

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
                self.assertIn("sort=PostingDate", url)
                self.assertIn("isDescendingSort=true", url)
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
            self.assertEqual(candidate["url"], "https://www.governmentjobs.com/jobs/5393100")
            self.assertEqual(candidate["freshness_source"], "governmentjobs_newprint_opening_date")

    def test_governmentjobs_query_plan_supports_all_and_category_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))

            all_plan = job_search.governmentjobs_query_plan(
                {
                    "track_hint": "traditional_it_wa",
                    "governmentjobs_query_mode": "all_recent",
                }
            )
            self.assertEqual(
                all_plan,
                [{"keyword": "", "category": "", "label": "all_recent_jobs", "kind": "all"}],
            )

            legacy_plan = job_search.governmentjobs_query_plan(
                {
                    "track_hint": "traditional_it_wa",
                    "governmentjobs_query_mode": "keywords",
                    "keywords": ["developer"],
                }
            )
            legacy_keywords = {item["keyword"] for item in legacy_plan}
            self.assertTrue(
                {
                    "application support",
                    "help desk",
                    "service desk",
                    "desktop support",
                    "IT support",
                    "systems administrator",
                    "IT operations",
                    "implementation specialist",
                    "AI transformation",
                }.issubset(legacy_keywords)
            )

            category_plan = job_search.governmentjobs_query_plan(
                {
                    "governmentjobs_query_mode": "category_plus_keywords",
                    "categories": ["IT and Computers"],
                    "keywords": ["software engineer", "GIS"],
                    "scan_all_jobs": True,
                }
            )
            self.assertEqual([item["kind"] for item in category_plan], ["all", "category", "keyword", "keyword"])
            category_url = job_search.governmentjobs_search_url(
                {"url": "https://www.governmentjobs.com/careers/washington", "agency": "washington"},
                "",
                1,
                "IT and Computers",
            )
            self.assertIn("category%5B0%5D=IT+and+Computers", category_url)

            self.assertEqual(
                job_search.normalize_job_url(
                    "https://www.governmentjobs.com/careers/washington/jobs/5415716/it-customer-support-entry"
                ),
                job_search.normalize_job_url(
                    "https://www.governmentjobs.com/jobs/5415716-0/it-customer-support-entry"
                ),
            )

    def test_governmentjobs_global_adapter_parses_and_enriches_statewide_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "GovernmentJobs Washington IT",
                "platform": "governmentjobs_global",
                "url": "https://www.governmentjobs.com/jobs",
                "location": "Washington",
                "categories": ["IT and Computers"],
                "organizations": ["Bellevue College", "Seattle Colleges"],
                "max_pages": 2,
                "max_detail_pages": 2,
                "detail_workers": 2,
            }
            listing_html = """
            <ul class="unstyled job-listing-container">
              <li class="job-item" data-job-id="5415716-0">
                <div class="job-item-container">
                  <h3><a class="job-details-link" href="/jobs/5415716-0/it-customer-support-entry">IT Customer Support - Entry</a></h3>
                  <div class="primaryInfo job-organization">State of Washington</div>
                  <div class="primaryInfo"><span class="job-location">Thurston County - Olympia, WA</span></div>
                </div>
              </li>
            </ul>
            """
            detail_html = """
            <script type="application/ld+json">
            {
              "@context":"https://schema.org/",
              "@type":"JobPosting",
              "title":"IT Customer Support - Entry",
              "description":"<p>Provide tier 1 application and hardware support.</p>",
              "datePosted":"2026-07-21",
              "validThrough":"2026-08-04",
              "hiringOrganization":{"@type":"Organization","name":"State of Washington"},
              "jobLocation":{"@type":"Place","address":{"addressLocality":"Olympia","addressRegion":"WA","addressCountry":"US"}}
            }
            </script>
            """
            requested_urls = []

            def fake_fetch(url, timeout=30):
                requested_urls.append(url)
                if "category%5B0%5D" in url:
                    return listing_html
                if url == "https://www.governmentjobs.com/jobs/5415716":
                    return detail_html
                raise AssertionError(f"Unexpected URL: {url}")

            with mock.patch.object(job_search, "fetch_url", side_effect=fake_fetch):
                candidates = job_search.discover_governmentjobs_global_jobs(source)

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["company"], "State of Washington")
            self.assertEqual(candidate["url"], "https://www.governmentjobs.com/jobs/5415716")
            self.assertEqual(candidate["posted_at"], "2026-07-21T00:00:00+00:00")
            self.assertEqual(candidate["freshness_source"], "governmentjobs_json_ld_datePosted")
            self.assertIn("tier 1 application", candidate["_jd_text"])
            self.assertEqual(sum("category%5B0%5D" in url for url in requested_urls), 1)
            self.assertIn("organization%5B0%5D=Bellevue+College", requested_urls[0])
            self.assertIn("organization%5B1%5D=Seattle+Colleges", requested_urls[0])

    def test_governmentjobs_all_recent_mode_stops_after_an_old_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example County",
                "platform": "governmentjobs",
                "url": "https://www.governmentjobs.com/careers/example",
                "agency": "example",
                "track_hint": "traditional_it_wa",
                "governmentjobs_query_mode": "all_recent",
                "max_pages": 10,
                "max_detail_pages": 0,
                "crawl_recent_days": 14,
            }

            def listing(job_id, title, posted, next_page):
                next_link = f'<a href="/careers/Home?page={next_page}">Next</a>' if next_page else ""
                return f"""
                <li class="list-item" data-job-id="{job_id}">
                  <a class="item-details-link" href="/careers/example/jobs/{job_id}/role">{title}</a>
                  <ul class="list-meta"><li>Olympia, WA</li></ul>
                  <div class="list-published"><span class="list-entry-starts"><span>{posted}</span></span></div>
                </li>{next_link}
                """

            pages = [
                listing("1", "Help Desk Technician", "Posted today", 2),
                listing("2", "GIS Developer", "Posted 30+ days ago", 3),
            ]
            requested_urls = []

            def fake_fetch_search(url, source_arg, timeout=20):
                requested_urls.append(url)
                return pages[len(requested_urls) - 1]

            with mock.patch.object(job_search, "fetch_governmentjobs_search", side_effect=fake_fetch_search):
                candidates = job_search.discover_governmentjobs_jobs(source)

            self.assertEqual(len(requested_urls), 2)
            self.assertTrue(all("keyword=" in url for url in requested_urls))
            self.assertEqual({candidate["role"] for candidate in candidates}, {"Help Desk Technician", "GIS Developer"})
            self.assertTrue(all(candidate["source_query"] == "all_recent_jobs" for candidate in candidates))

    def test_governmentjobs_details_prioritize_relevant_technical_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            source = {
                "company": "Example City",
                "platform": "governmentjobs",
                "url": "https://www.governmentjobs.com/careers/example",
                "agency": "example",
                "track_hint": "traditional_it_wa",
                "governmentjobs_query_mode": "all_recent",
                "max_pages": 1,
                "max_detail_pages": 1,
            }
            listing_html = """
            <li class="list-item" data-job-id="1">
              <a class="item-details-link" href="/careers/example/jobs/1/nurse">Nurse</a>
              <ul class="list-meta"><li>Seattle, WA</li></ul>
            </li>
            <li class="list-item" data-job-id="2">
              <a class="item-details-link" href="/careers/example/jobs/2/gis-developer">GIS Developer</a>
              <ul class="list-meta"><li>Seattle, WA</li></ul>
            </li>
            """
            detail_html = """
            <h1 class="job-title">GIS Developer</h1>
            <div class="span4"><div class="term-description">OPENING DATE</div></div><div class="span8"><p>07/22/2026</p></div>
            """
            detail_urls = []

            def fake_detail(url, timeout=20):
                detail_urls.append(url)
                return detail_html

            with mock.patch.object(job_search, "fetch_governmentjobs_search", return_value=listing_html):
                with mock.patch.object(job_search, "fetch_url", side_effect=fake_detail):
                    candidates = job_search.discover_governmentjobs_jobs(source)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(len(detail_urls), 1)
            self.assertIn("/jobs/newprint/2", detail_urls[0])

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
            self.assertEqual(job_search.extract_year_requirements("Employees receive more vacation after 5 years of service."), [])

    def test_experience_bucket_uses_lowest_viable_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_search = load_job_search(Path(tmp))
            app = {
                "role": "Software Engineer",
                "notes": "Level I requires 2+ years of experience; Level II requires 4+ years; Level III requires 6+ years.",
            }

            self.assertEqual(job_search.experience_requirement_bucket(app), "2_plus")
            self.assertFalse(job_search.has_year_requirement(job_search.application_filter_text(app), 3))

    def test_score_text_recomputes_stale_experience_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            profile = {
                "targets": {"roles": ["software engineer"], "keywords": ["python"]},
                "preferences": {"relocation_allowed_states": ["WA", "CA"]},
                "dealbreakers": {"lower_weight_minimum_years_from": 3, "skip_minimum_years_from": 5},
                "work_authorization": {"requires_sponsorship": False},
            }
            app = {
                "company": "FreshCo",
                "role": "Software Engineer",
                "location": "Seattle, WA",
                "platform": "greenhouse",
                "experience_bucket": "3_plus",
            }

            score = job_search.score_text(
                app,
                "Software Engineer role requiring 1+ years of experience with Python.",
                profile,
            )

            self.assertEqual(score["experience_bucket"], "1_2")
            self.assertNotEqual(score["status"], "skipped")

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

    def test_score_location_tiers_do_not_zero_other_us_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_root = Path(tmp)
            write_private_workspace(private_root, [])
            job_search = load_job_search(private_root)
            profile = job_search.load_profile()
            jd = "Software Engineer role using Python APIs."

            preferred = job_search.score_text(
                {"company": "WA", "role": "Software Engineer", "location": "Seattle, WA"}, jd, profile
            )
            relocation = job_search.score_text(
                {"company": "TX", "role": "Software Engineer", "location": "Austin, TX"}, jd, profile
            )
            unknown = job_search.score_text(
                {"company": "Unknown", "role": "Software Engineer", "location": "Remote"}, jd, profile
            )
            rejected = job_search.score_text(
                {"company": "EU", "role": "Software Engineer", "location": "Berlin, Germany"}, jd, profile
            )

            self.assertEqual(preferred["location_bucket"], "preferred")
            self.assertEqual(relocation["location_bucket"], "relocation")
            self.assertEqual(unknown["location_bucket"], "maybe")
            self.assertEqual(rejected["location_bucket"], "rejected")
            self.assertGreater(preferred["fit_score"], relocation["fit_score"])
            self.assertGreater(relocation["fit_score"], unknown["fit_score"])
            self.assertNotEqual(relocation["status"], "skipped")
            self.assertEqual(rejected["status"], "skipped")

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

            priority = job_search.daily_review_app_rows(apps, "priority", 8, 10)
            relocation = job_search.daily_review_app_rows(apps, "relocation", 8, 10)

            self.assertEqual([row["id"] for row in priority], ["good"])
            self.assertEqual({row["id"] for row in relocation}, {"range-years", "dc"})

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
