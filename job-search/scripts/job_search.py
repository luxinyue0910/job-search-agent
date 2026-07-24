#!/usr/bin/env python3
"""Local, human-in-the-loop job search automation.

The script intentionally keeps external automation conservative:
- Trackers and generated materials are local files.
- Gmail sending is delegated to a separate browser script and only to self.
- ATS form submission is never performed here.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import contextlib
import csv
import datetime as dt
import email.utils
import hashlib
import html
import io
import http.cookiejar
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_BASE_ROOT = Path(os.environ["JOB_SEARCH_PRIVATE_DIR"]).expanduser() if os.environ.get("JOB_SEARCH_PRIVATE_DIR") else ROOT
PERSON = os.environ.get("JOB_SEARCH_PERSON", "default")
PERSON_ROOT = PRIVATE_BASE_ROOT
PROFILE_PATH = PRIVATE_BASE_ROOT / "profile.json"
APPLICATIONS_JSON = PRIVATE_BASE_ROOT / "data" / "applications.json"
APPLICATIONS_CSV = PRIVATE_BASE_ROOT / "data" / "applications.csv"
SOURCES_PATH = PRIVATE_BASE_ROOT / "data" / "sources.json"
WATCHLIST_PATH = PRIVATE_BASE_ROOT / "data" / "company_watchlist.json"
SEEN_JOBS_PATH = PRIVATE_BASE_ROOT / "data" / "seen_jobs.json"
SEEN_JOBS_DIR = PRIVATE_BASE_ROOT / "data" / "seen_jobs"
SEEN_JOBS_INDEX_PATH = SEEN_JOBS_DIR / "index.json"
SEEN_JOBS_SHARDS_DIR = SEEN_JOBS_DIR / "shards"
TRACKS_DIR = PRIVATE_BASE_ROOT / "tracks"
OUTPUT_DIR = PRIVATE_BASE_ROOT / "output"
NOTIFICATIONS_DIR = OUTPUT_DIR / "notifications"
DISCOVERY_RUNS_DIR = PRIVATE_BASE_ROOT / "data" / "discovery_runs"
SEEN_JOBS_SHARD_COUNT = 256
SEEN_JOBS_INTERNAL_KEYS = {"_seen_jobs_format"}

CSV_FIELDS = [
    "id",
    "company",
    "role",
    "url",
    "platform",
    "job_number",
    "external_job_id",
    "location",
    "status",
    "fit_score",
    "ats_score",
    "experience_bucket",
    "date_found",
    "posted_at",
    "updated_at",
    "first_seen",
    "last_seen",
    "source",
    "source_query",
    "freshness_source",
    "review_bucket",
    "discovery_bucket",
    "location_bucket",
    "target_track",
    "matched_tracks",
    "resume_file",
    "date_applied",
    "resume_path",
    "cover_letter_path",
    "screenshot_path",
    "notes",
]

TECH_KEYWORDS = [
    "python",
    "typescript",
    "javascript",
    "react",
    "node",
    "java",
    "go",
    "sql",
    "postgres",
    "redis",
    "aws",
    "gcp",
    "azure",
    "docker",
    "kubernetes",
    "machine learning",
    "ml",
    "ai",
    "llm",
    "rag",
    "backend",
    "frontend",
    "distributed systems",
    "api",
    "microservices",
]

SUBMIT_WORDS = [
    "submit",
    "send application",
    "apply",
    "finish application",
    "complete application",
]

DEFAULT_DISCOVERY_TITLE_KEYWORDS = [
    "software",
    "backend",
    "back end",
    "full stack",
    "full-stack",
    "frontend",
    "front end",
    "ai",
    "machine learning",
    "ml",
    "platform",
    "devops",
    "infrastructure",
    "system development engineer",
    "systems development engineer",
    "cloud",
    "new grad",
    "junior",
    "entry level",
    "swe",
    "developer",
    "product engineer",
    "founding engineer",
    "developer tools",
    "developer tools engineer",
    "integration engineer",
    "integrations engineer",
    "customer engineer",
    "qa automation",
    "sdet",
]

DEFAULT_WEB_DISCOVERY_ROLES = [
    "Software Engineer",
    "Backend Engineer",
    "AI Engineer",
    "Machine Learning Engineer",
    "New Grad Software Engineer",
    "Software Engineer I",
    "Platform Engineer",
    "DevOps Engineer",
]

DEFAULT_WEB_DISCOVERY_LOCATIONS = [
    "Seattle",
    "Bellevue",
    "Washington",
    "Remote",
    "San Francisco",
    "California",
]

DEFAULT_WORKDAY_KEYWORDS = [
    "Software Engineer",
    "Software Development Engineer",
    "Backend Engineer",
    "AI Engineer",
    "Machine Learning Engineer",
    "Platform Engineer",
    "DevOps Engineer",
    "SDET",
    "QA Engineer",
    "Forward Deployed Engineer",
]

DEFAULT_GOVERNMENTJOBS_TRADITIONAL_IT_KEYWORDS = [
    "technical support",
    "application support",
    "help desk",
    "service desk",
    "desktop support",
    "IT support",
    "systems administrator",
    "IT operations",
    "implementation specialist",
    "implementation engineer",
    "IT service engineer",
    "AI transformation",
    "information technology",
    "application",
    "systems",
    "data",
    "network",
    "security",
    "GIS",
    "ERP",
    "DevOps",
]

ATS_SEARCH_SITES = [
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class SearchRateLimited(RuntimeError):
    """Raised when a search provider asks us to stop sending requests."""


class SourceTimeout(RuntimeError):
    """Raised when one discovery source exceeds its time budget."""


class SourceDiscoveryFailed(RuntimeError):
    """Raised after all attempts for one discovery source fail."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]):
        super().__init__(message)
        self.attempts = attempts


@contextlib.contextmanager
def source_timeout(seconds: float | None):
    if not seconds or seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise SourceTimeout(f"source discovery exceeded {seconds:g}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def today() -> str:
    return dt.date.today().isoformat()


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def configure_person(person: str) -> None:
    """Select a person's partition.

    The root files remain the backward-compatible default. Any explicit person
    uses job-search/profiles/<person>/..., which keeps private data separate.
    """
    global PERSON, PERSON_ROOT, PROFILE_PATH, APPLICATIONS_JSON, APPLICATIONS_CSV, SOURCES_PATH, WATCHLIST_PATH, SEEN_JOBS_PATH, SEEN_JOBS_DIR, SEEN_JOBS_INDEX_PATH, SEEN_JOBS_SHARDS_DIR, TRACKS_DIR, OUTPUT_DIR, NOTIFICATIONS_DIR, DISCOVERY_RUNS_DIR

    PERSON = slugify(person or "default")
    default_profile_dir = PRIVATE_BASE_ROOT / "profiles" / "default"
    if PERSON == "default" and not default_profile_dir.exists():
        PERSON_ROOT = PRIVATE_BASE_ROOT
    else:
        PERSON_ROOT = PRIVATE_BASE_ROOT / "profiles" / PERSON
    PROFILE_PATH = PERSON_ROOT / "profile.json"
    APPLICATIONS_JSON = PERSON_ROOT / "data" / "applications.json"
    APPLICATIONS_CSV = PERSON_ROOT / "data" / "applications.csv"
    SOURCES_PATH = PERSON_ROOT / "data" / "sources.json"
    WATCHLIST_PATH = PERSON_ROOT / "data" / "company_watchlist.json"
    SEEN_JOBS_PATH = PERSON_ROOT / "data" / "seen_jobs.json"
    SEEN_JOBS_DIR = PERSON_ROOT / "data" / "seen_jobs"
    SEEN_JOBS_INDEX_PATH = SEEN_JOBS_DIR / "index.json"
    SEEN_JOBS_SHARDS_DIR = SEEN_JOBS_DIR / "shards"
    TRACKS_DIR = PERSON_ROOT / "tracks"
    OUTPUT_DIR = PERSON_ROOT / "output"
    NOTIFICATIONS_DIR = OUTPUT_DIR / "notifications"
    DISCOVERY_RUNS_DIR = PERSON_ROOT / "data" / "discovery_runs"


def require_person_files() -> None:
    missing = [path for path in [PROFILE_PATH, APPLICATIONS_JSON, SOURCES_PATH] if not path.exists()]
    if missing:
        paths = "\n".join(f"- {path}" for path in missing)
        raise SystemExit(
            f"Missing person workspace files for '{PERSON}'. Run:\n"
            f"python3 job-search/scripts/job_search.py --person {PERSON} init-person\n\n"
            f"If your private data lives outside this repo, set JOB_SEARCH_PRIVATE_DIR first.\n\n"
            f"Missing:\n{paths}"
        )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = file.name
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path:
            with contextlib.suppress(FileNotFoundError):
                Path(temp_path).unlink()


def seen_jobs_shard_for_url(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return f"{digest[:2]}.jsonl"


def compact_seen_record(url: str, record: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"url": record.get("url") or url}
    preferred_fields = [
        "company",
        "role",
        "platform",
        "location",
        "posted_at",
        "updated_at",
        "first_seen",
        "last_seen",
        "source",
        "source_query",
        "freshness_source",
        "job_number",
        "external_job_id",
        "target_track",
        "resume_file",
        "matched_tracks",
    ]
    for field in preferred_fields:
        value = record.get(field)
        if value in ("", None, [], {}):
            continue
        compact[field] = value
    for field in sorted(record):
        if field in compact or field in preferred_fields or field in SEEN_JOBS_INTERNAL_KEYS:
            continue
        value = record.get(field)
        if value in ("", None, [], {}):
            continue
        compact[field] = value
    return compact


def clean_seen_jobs_for_legacy_json(seen: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in seen.items() if key not in SEEN_JOBS_INTERNAL_KEYS}
    cleaned.setdefault("jobs", {})
    return cleaned


def load_sharded_seen_jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = {}
    if SEEN_JOBS_SHARDS_DIR.exists():
        for shard_path in sorted(SEEN_JOBS_SHARDS_DIR.glob("*.jsonl")):
            with shard_path.open("r", encoding="utf-8") as file:
                for line_number, line in enumerate(file, 1):
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    url = str(record.get("url", "")).strip()
                    if not url:
                        raise ValueError(f"Missing url in {shard_path}:{line_number}")
                    jobs[url] = record
    return {"jobs": jobs, "_seen_jobs_format": "sharded"}


def write_sharded_seen_jobs(seen: dict[str, Any]) -> bool:
    jobs = seen.setdefault("jobs", {})
    SEEN_JOBS_SHARDS_DIR.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
    for url, record in jobs.items():
        normalized_url = str(record.get("url") or url)
        record["url"] = normalized_url
        grouped[seen_jobs_shard_for_url(normalized_url)].append((normalized_url, record))

    changed = False
    shard_names = set(grouped) | {path.name for path in SEEN_JOBS_SHARDS_DIR.glob("*.jsonl")}
    for shard_name in sorted(shard_names):
        shard_path = SEEN_JOBS_SHARDS_DIR / shard_name
        rows = grouped.get(shard_name, [])
        if not rows:
            if shard_path.exists():
                shard_path.unlink()
                changed = True
            continue
        lines = [
            json.dumps(compact_seen_record(url, record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for url, record in sorted(rows, key=lambda item: item[0])
        ]
        content = "\n".join(lines) + "\n"
        if shard_path.exists() and shard_path.read_text(encoding="utf-8") == content:
            continue
        shard_path.write_text(content, encoding="utf-8")
        changed = True

    index = {
        "version": 2,
        "format": "jsonl-shards",
        "shard_count": SEEN_JOBS_SHARD_COUNT,
        "records": len(jobs),
        "last_updated": now_utc_iso(),
    }
    existing_index = load_json(SEEN_JOBS_INDEX_PATH) if SEEN_JOBS_INDEX_PATH.exists() else {}
    comparable_existing = {key: value for key, value in existing_index.items() if key != "last_updated"}
    comparable_new = {key: value for key, value in index.items() if key != "last_updated"}
    if changed or comparable_existing != comparable_new:
        SEEN_JOBS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_JOBS_INDEX_PATH.write_text(
            json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        changed = True
    return changed


def load_seen_jobs() -> dict[str, Any]:
    if SEEN_JOBS_INDEX_PATH.exists() or SEEN_JOBS_SHARDS_DIR.exists():
        return load_sharded_seen_jobs()
    if not SEEN_JOBS_PATH.exists():
        return {"jobs": {}, "_seen_jobs_format": "sharded"}
    data = load_json(SEEN_JOBS_PATH)
    data.setdefault("jobs", {})
    data["_seen_jobs_format"] = "legacy"
    return data


def save_seen_jobs(seen: dict[str, Any]) -> None:
    if seen.get("_seen_jobs_format") == "sharded" or SEEN_JOBS_INDEX_PATH.exists() or SEEN_JOBS_SHARDS_DIR.exists():
        write_sharded_seen_jobs(seen)
        return
    cleaned = clean_seen_jobs_for_legacy_json(seen)
    cleaned["last_updated"] = now_utc_iso()
    write_json(SEEN_JOBS_PATH, cleaned)


def load_profile() -> dict[str, Any]:
    require_person_files()
    return load_json(PROFILE_PATH)


def load_track(track_id: str | None) -> dict[str, Any]:
    track = str(track_id or "").strip()
    if not track:
        return {}
    safe_track = re.sub(r"[^a-zA-Z0-9_-]+", "-", track).strip("-")
    path = TRACKS_DIR / safe_track / "track.json"
    if not path.exists():
        raise SystemExit(f"Unknown track '{track}'. Expected {path}")
    data = load_json(path)
    data.setdefault("id", safe_track)
    data.setdefault("root", str(path.parent))
    return data


def merge_unique(base: list[Any], extra: list[Any]) -> list[Any]:
    items: list[Any] = []
    seen: set[str] = set()
    for item in base + extra:
        key = str(item).strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        items.append(item)
    return items


def profile_for_track(track_id: str | None) -> dict[str, Any]:
    profile = json.loads(json.dumps(load_profile()))
    track = load_track(track_id)
    if not track:
        profile["_track"] = {}
        return profile

    targets = profile.setdefault("targets", {})
    track_targets = track.get("targets", {})
    for key in ["roles", "levels"]:
        if track_targets.get(key):
            targets[key] = [str(item) for item in track_targets.get(key, []) if str(item).strip()]
    for key in ["keywords"]:
        targets[key] = merge_unique(
            [str(item) for item in targets.get(key, [])],
            [str(item) for item in track_targets.get(key, [])],
        )
    if isinstance(track.get("dealbreakers"), dict):
        dealbreakers = profile.setdefault("dealbreakers", {})
        dealbreakers.update(track["dealbreakers"])
    profile["_track"] = track
    return profile


def path_from_track(track: dict[str, Any], field: str) -> Path | None:
    value = str(track.get(field, "")).strip()
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return PERSON_ROOT / value


def load_tracker() -> dict[str, Any]:
    require_person_files()
    return load_json(APPLICATIONS_JSON)


def save_tracker(tracker: dict[str, Any]) -> None:
    apps = tracker.get("applications", [])
    tracker["stats"] = compute_stats(apps)
    tracker["last_updated"] = now_iso()
    write_json(APPLICATIONS_JSON, tracker)
    sync_csv(tracker)


def compute_stats(apps: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {status: 0 for status in ["applied", "prepared", "needs_review", "skipped"]}
    fit_scores = []
    ats_scores = []
    for app in apps:
        status = app.get("status", "")
        if status in statuses:
            statuses[status] += 1
        if isinstance(app.get("fit_score"), (int, float)):
            fit_scores.append(app["fit_score"])
        if isinstance(app.get("ats_score"), (int, float)):
            ats_scores.append(app["ats_score"])
    return {
        "total": len(apps),
        "applied": statuses["applied"],
        "prepared": statuses["prepared"],
        "needs_review": statuses["needs_review"],
        "skipped": statuses["skipped"],
        "avg_fit_score": round(sum(fit_scores) / len(fit_scores), 1) if fit_scores else 0,
        "avg_ats_score": round(sum(ats_scores) / len(ats_scores), 1) if ats_scores else 0,
    }


def sync_csv(tracker: dict[str, Any] | None = None) -> None:
    tracker = tracker or load_tracker()
    APPLICATIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with APPLICATIONS_CSV.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for app in tracker.get("applications", []):
            writer.writerow({field: app.get(field, "") for field in CSV_FIELDS})


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "unknown"


def detect_platform(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    path = urllib.parse.urlparse(url).path.lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "jobs.gem.com" in host:
        return "gem"
    if "ats.rippling.com" in host:
        return "rippling_jobs"
    if "ripplehire.com" in host and "/candidate" in path:
        return "ripplehire"
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        return "workday"
    if "joinbytedance.com" in host or "jobs.bytedance.com" in host:
        return "bytedance_jobs"
    if "careers.shein.com" in host:
        return "shein_jobs"
    if "we.dji.com" in host:
        return "dji_jobs"
    if "talent.alibaba.com" in host:
        return "alibaba_jobs"
    if "careers.pddglobalhr.com" in host:
        return "pdd_globalhr_jobs"
    if "career.huawei.com" in host:
        return "huawei_jobs"
    if "amazon.jobs" in host:
        return "amazon_jobs"
    if "google.com" in host and "/about/careers/applications" in path:
        return "google_jobs"
    if "careers.google.com" in host:
        return "google_jobs"
    if "metacareers.com" in host:
        return "meta_jobs"
    if "m-cloud.io" in host:
        return "m_cloud"
    if "hirebridge.com" in host:
        return "hirebridge"
    if "successfactors.com" in host:
        return "successfactors"
    if "eightfold.ai" in host or "jobs.nvidia.com" in host:
        return "eightfold"
    if "jobs.apple.com" in host:
        return "apple_jobs"
    if "providence.jobs" in host or "prod-search-api.jobsyn.org" in host:
        return "providence_jobs"
    if host in {"jacobs.jobs", "ironmountain.jobs"}:
        return "jobsyn"
    if "careers.salesforce.com" in host:
        return "salesforce_jobs"
    if "governmentjobs.com" in host:
        return "governmentjobs"
    if host == "careers.zoom.us":
        return "zoom_careers"
    if host == "pm.healthcaresource.com" and path.startswith("/cs/"):
        return "healthcaresource"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if host == "bb3jobboard.topechelon.com":
        return "topechelon"
    if host == "sta.smithgardens.com" and path.startswith("/careers"):
        return "cyber_recruiter"
    if path.endswith("/careers.aspx") and re.search(
        r"(?:^|&)(?:type|req)=",
        urllib.parse.urlparse(url).query,
        flags=re.I,
    ):
        return "cyber_recruiter"
    if "applicantpro.com" in host:
        return "applicantpro"
    if host.endswith(".viewpointhr-ats.com") or host.endswith(".hiringthing.com"):
        return "hiringthing"
    if host.endswith(".viewpointforcloud.com") and path.startswith("/careers"):
        return "viewpoint_for_cloud"
    if host == "careers.hireology.com" or (
        host.endswith(".hireology.com")
        and host not in {"api.hireology.com", "app.hireology.com"}
    ):
        return "hireology"
    if host.endswith(".applicantstack.com") and path.startswith("/x/"):
        return "applicantstack"
    if "icims.com" in host:
        return "icims"
    if host.endswith(".clearcompany.com") and "/careers" in path:
        return "clearcompany"
    if host == "recruitingbypaycor.com":
        return "paycor"
    if host.endswith(".prismhrtalent.com"):
        return "prismhr"
    if host.endswith(".joveo.site"):
        return "joveo"
    if host in {"www.amentumcareers.com", "careers.equinix.com"}:
        return "clinch"
    if host == "careers.atkinsrealis.com":
        return "atkins_jobs"
    if host == "isgpoweredbydata.blob.core.windows.net" or (
        host == "jobs.localjobnetwork.com" and "/apply/add/" in path
    ):
        return "isg_poweredby"
    if host == "recruiting.paylocity.com" and "/recruiting/jobs" in path:
        return "paylocity"
    if host == "portal.dynamicsats.com" and "/joblisting/" in path:
        return "dynamicsats"
    if host == "bms.hanford.gov" and "/hrisjp/jobslist.aspx" in path:
        return "hanford_bms"
    if host.endswith("dayforcehcm.com"):
        return "dayforce"
    if host.endswith(".mykronos.com") and "/ta/" in path and ".careers" in path:
        return "kronos_careers"
    if host == "myjobs.adp.com":
        return "adp_myjobs"
    if host in {"workforcenow.adp.com", "workforcenow.cloud.adp.com"} and "/mdf/recruitment/recruitment.html" in path:
        return "adp_workforce_now"
    if host == "www2.appone.com" or (
        host == "recruiting.myapps.paychex.com"
        and ("appone" in path or "maininforeq.asp" in path)
    ):
        return "appone"
    if host == "jobs.slalom.com":
        return "avature"
    if host == "jubilantcareer.jubl.com":
        return "jubilant_careers"
    if host == "careers.bankofamerica.com":
        return "boa_careers"
    if ("oraclecloud.com" in host and ("/candidateexperience/" in path or "/cx_" in path)) or "/sites/cx_" in path:
        return "oracle_cx"
    if "jobvite.com" in host:
        return "jobvite"
    if host == "cta.cadienttalent.com":
        return "cadient"
    if host.endswith(".taleo.net") and (
        "/careersection/" in path or "/ats/careers/v2/" in path
    ):
        return "taleo"
    if host == "careers.pageuppeople.com" and "/listing" in path:
        return "pageup"
    if host.endswith("jobappnetwork.com") or host in {
        "databankcareers.com",
        "www.databankcareers.com",
    }:
        return "talentreef"
    if (
        host.endswith(".peopleadmin.com")
        or host in {"employment.plu.edu", "jobs.hr.ewu.edu"}
    ) and "/postings" in path:
        return "peopleadmin"
    if "paycomonline.net" in host and "/v4/ats/" in path:
        return "paycom"
    if (
        host.endswith(".ultipro.com")
        or host.endswith(".rec.pro.ukg.net")
    ) and "/jobboard/" in path:
        return "ultipro"
    if host.endswith(".zohorecruit.com") and "/jobs/" in path:
        return "zoho_recruit"
    if host.endswith(".breezy.hr"):
        return "breezy"
    if "workable.com" in host:
        return "workable"
    if "bamboohr.com" in host:
        return "bamboohr"
    if "ycombinator.com" in host and path.startswith("/jobs"):
        return "yc_job_board"
    if "ycombinator.com" in host and "/companies/" in path and "/jobs" in path:
        return "yc_jobs"
    if "startup.jobs" in host:
        return "startup_jobs"
    if host in {"builtin.com", "www.builtin.com"} or host.startswith("builtin") or ".builtin" in host:
        return "builtin_jobs"
    if host.startswith("jobs.") and any(domain in host for domain in ["madrona.com", "a16z.com", "lsvp.com"]):
        return "getro_jobs"
    if host == "jobs.psl.com":
        return "consider_jobs"
    if "news.ycombinator.com" in host or "hn.algolia.com" in host:
        return "hn_who_is_hiring"
    if path.endswith(".xml") or "/services/rss/" in path:
        return "rss"
    if "jibecdn.com" in host or "jibeapply.com" in host or "careers.costco.com" in host:
        return "jibe"
    if "talentbrew.com" in host or "tbcdn.talentbrew.com" in host or "jobs.walgreens.com" in host:
        return "talentbrew"
    if host.endswith(".ttcportals.com"):
        return "ttcportals"
    if host.endswith(".workgr8.com"):
        return "workgr8"
    if "careerpuck.com" in host:
        return "careerpuck"
    if "pinpointhq.com" in host:
        return "pinpoint"
    if "brassring.com" in host:
        return "brassring"
    if host.endswith(".inforcloudsuite.com") and "/hcm/jobs/" in path:
        return "infor_cloudsuite"
    if "careers.kula.ai" in host:
        return "kula"
    if "applytojob.com" in host:
        return "jazzhr"
    if host.endswith("mckinstry.com") and "/join-us/jobs" in path:
        return "wordpress_taleo"
    return "custom"


def make_id(company: str, role: str, url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{slugify(company)}-{slugify(role)}-{digest}"


def fetch_url(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_url_with_opener(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            **(headers or {}),
        },
    )
    with opener.open(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def fetch_json_with_headers(url: str, headers: dict[str, str], timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,*/*;q=0.8",
            **headers,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def fetch_json_post(url: str, payload: dict[str, Any], timeout: int = 20) -> Any:
    return fetch_json_post_with_headers(url, payload, {}, timeout=timeout)


def fetch_json_post_with_headers(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int = 20,
) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,*/*;q=0.8",
            "Content-Type": "application/json",
            **headers,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def fetch_json_form_post(url: str, payload: dict[str, Any], timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def parse_jsonp(raw: str) -> Any:
    stripped = raw.strip()
    start = stripped.find("(")
    end = stripped.rfind(")")
    if start >= 0 and end > start:
        stripped = stripped[start + 1 : end]
    return json.loads(stripped)


def fetch_jsonp(url: str, timeout: int = 20, referer: str | None = None) -> Any:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/javascript,application/json;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return parse_jsonp(response.read().decode(charset, errors="replace"))


def fetch_json_with_opener(opener: urllib.request.OpenerDirector, url: str, headers: dict[str, str], timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def fetch_json_post_with_opener(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int = 20,
) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    with opener.open(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<script.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style.*?</style>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def parse_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    original_raw = raw
    iso_raw = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    iso_raw = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso_raw)
    loose_date = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", iso_raw)
    if loose_date:
        iso_raw = f"{loose_date.group(1)}-{int(loose_date.group(2)):02d}-{int(loose_date.group(3)):02d}"
    try:
        parsed = dt.datetime.fromisoformat(iso_raw)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(original_raw)
        except (TypeError, ValueError):
            parsed = None
    if parsed is None:
        for fmt in (
            "%B %d, %Y",
            "%b %d, %Y",
            "%d %B %Y",
            "%d %b %Y",
            "%d-%b-%Y",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%y %H:%M:%S",
            "%m/%d/%Y",
            "%m/%d/%y",
        ):
            try:
                parsed = dt.datetime.strptime(original_raw, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def normalize_datetime(value: Any) -> str:
    parsed = parse_datetime(value)
    return parsed.replace(microsecond=0).isoformat() if parsed else ""


def relative_search_days(args: argparse.Namespace) -> float | None:
    if getattr(args, "since_hours", None) is not None:
        return max(float(args.since_hours) / 24, 0.05)
    return getattr(args, "since_days", None)


def normalize_job_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    host = parsed.netloc.lower()
    if host == "governmentjobs.com" or host.endswith(".governmentjobs.com"):
        job_match = re.search(r"/(?:careers/[^/]+/)?jobs/(?:newprint/)?(\d+)", parsed.path, flags=re.I)
        if job_match:
            return f"https://www.governmentjobs.com/jobs/{job_match.group(1)}"
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if not key.lower().startswith("utm_")]
    if host.endswith(".icims.com"):
        presentation_params = {"in_iframe", "mobile", "needsredirect", "height", "width"}
        query = [(key, value) for key, value in query if key.lower() not in presentation_params]
    fragment = ""
    if "ripplehire.com" in parsed.netloc.lower() and re.search(r"(?:detail|apply)/job/[^/?#]+", urllib.parse.unquote(parsed.fragment or "")):
        fragment = parsed.fragment
    if host == "pm.healthcaresource.com" and re.match(
        r"/?job/\d+",
        urllib.parse.unquote(parsed.fragment or ""),
        flags=re.I,
    ):
        fragment = parsed.fragment
    if host == "bb3jobboard.topechelon.com" and re.match(
        r"/?[0-9a-f-]{36}/detail(?:$|[/?])",
        urllib.parse.unquote(parsed.fragment or ""),
        flags=re.I,
    ):
        fragment = parsed.fragment
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query), fragment=fragment)).rstrip("/")


def discovery_cutoff(args: argparse.Namespace) -> dt.datetime:
    if args.since_hours is not None and args.since_days is not None:
        raise SystemExit("Use either --since-hours or --since-days, not both.")
    if args.since_days is not None:
        delta = dt.timedelta(days=args.since_days)
    else:
        delta = dt.timedelta(hours=args.since_hours if args.since_hours is not None else 24)
    return dt.datetime.now(dt.timezone.utc) - delta


def source_discovery_cutoff(source: dict[str, Any], default_cutoff: dt.datetime) -> dt.datetime | None:
    if source.get("ignore_posted_cutoff"):
        return None
    if source.get("posted_cutoff_days") not in (None, ""):
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=float(source["posted_cutoff_days"]))
    if source.get("posted_cutoff_hours") not in (None, ""):
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=float(source["posted_cutoff_hours"]))
    return default_cutoff


def source_platform(source: dict[str, Any]) -> str:
    return str(source.get("platform") or source.get("type") or detect_platform(source.get("url", ""))).lower()


def truthy_source_flag(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"0", "false", "no", "n", "inactive", "disabled"}:
        return False
    if normalized in {"1", "true", "yes", "y", "active", "enabled"}:
        return True
    return default


def source_is_active(source: dict[str, Any]) -> bool:
    return truthy_source_flag(source.get("active"), default=True)


def source_for_track(source: dict[str, Any], track_id: str | None) -> dict[str, Any]:
    if not track_id:
        return source
    selected = dict(source)
    track_overrides = source.get("track_overrides", {})
    if isinstance(track_overrides, dict) and isinstance(track_overrides.get(track_id), dict):
        selected.update(track_overrides[track_id])
    track_keywords = source.get("track_keywords", {})
    if isinstance(track_keywords, dict) and track_keywords.get(track_id):
        selected["keywords"] = track_keywords[track_id]
    track_locations = source.get("track_locations", {})
    if isinstance(track_locations, dict) and track_locations.get(track_id):
        selected["locations"] = track_locations[track_id]
    return selected


DEFAULT_DISCOVER_ALL_TRACKS = [
    "general_sde",
    "qa_engineer",
    "fde_ai_engineer",
    "traditional_it_wa",
    "data_center_infra",
]

DEFAULT_RESCORE_BACKLOG_STATUSES = [
    "found",
    "needs_review",
    "needs_retry",
    "scored",
]

DISCOVER_ALL_ROLE_QUERIES = [
    "Software Engineer",
    "SDET",
    "Forward Deployed Engineer",
    "Application Support Analyst",
    "Data Center Engineer",
]


def source_for_tracks(source: dict[str, Any], track_ids: list[str]) -> dict[str, Any]:
    """Build one source request configuration that covers all requested tracks."""
    selected = dict(source)
    keywords = [str(item) for item in source.get("keywords", []) if str(item).strip()]
    locations = [str(item) for item in source.get("locations", []) if str(item).strip()]
    for track_id in track_ids:
        track_source = source_for_track(source, track_id)
        keywords = merge_unique(
            keywords,
            [str(item) for item in track_source.get("keywords", []) if str(item).strip()],
        )
        locations = merge_unique(
            locations,
            [str(item) for item in track_source.get("locations", []) if str(item).strip()],
        )
    if truthy_source_flag(source.get("include_default_union_queries"), default=True):
        keywords = merge_unique(keywords, DISCOVER_ALL_ROLE_QUERIES)
    selected["keywords"] = keywords
    if locations:
        selected["locations"] = locations
    return selected


def greenhouse_board_from_source(source: dict[str, Any]) -> str | None:
    if source.get("board"):
        return str(source["board"])
    parsed = urllib.parse.urlparse(source.get("url", ""))
    if "greenhouse.io" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if "boards" in parts:
        index = parts.index("boards")
        if index + 1 < len(parts):
            return parts[index + 1]
    if "board_token" in parts:
        index = parts.index("board_token")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[0] if parts else None


def greenhouse_board_is_fetchable(board: str) -> bool:
    try:
        data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs?content=true", timeout=10)
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("jobs"), list)


def lever_site_from_source(source: dict[str, Any]) -> str | None:
    if source.get("site"):
        return str(source["site"])
    parsed = urllib.parse.urlparse(source.get("url", ""))
    if "lever.co" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else None


def ashby_board_from_source(source: dict[str, Any]) -> str | None:
    if source.get("board"):
        return str(source["board"])
    parsed = urllib.parse.urlparse(source.get("url", ""))
    if "ashbyhq.com" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else None


def gem_board_from_source(source: dict[str, Any]) -> str | None:
    if source.get("board"):
        return str(source["board"])
    parsed = urllib.parse.urlparse(source.get("url", ""))
    if "jobs.gem.com" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else None


def workday_source_parts(source: dict[str, Any]) -> tuple[str, str, str] | None:
    url = source.get("url", "")
    parsed = urllib.parse.urlparse(url)
    host = str(source.get("host") or parsed.netloc).strip()
    if "://" in host:
        host = urllib.parse.urlparse(host).netloc
    if "myworkdayjobs.com" not in host and "myworkdaysite.com" not in host:
        return None
    tenant = str(source.get("tenant") or "").strip()
    parts = [part for part in parsed.path.split("/") if part]
    if not tenant and "myworkdaysite.com" in host and len(parts) >= 3 and parts[0].lower() == "recruiting":
        tenant = parts[1]
        site = str(source.get("site") or parts[2]).strip()
        return (host, tenant, site) if host and tenant and site else None
    if not tenant:
        match = re.match(r"([a-zA-Z0-9_-]+)\.wd\d+\.", host)
        if match:
            tenant = match.group(1)
    site = str(source.get("site") or "").strip()
    if not site and parts:
        site_candidates = [
            part
            for part in parts
            if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", part) and part.lower() not in {"job", "jobs", "login"}
        ]
        site = site_candidates[0] if site_candidates else parts[-1]
    if not host or not tenant or not site:
        return None
    return host, tenant, site


def parse_greenhouse_published_at(url: str) -> str:
    try:
        raw = fetch_url(url)
    except Exception:
        return ""
    match = re.search(r'"published_at"\s*:\s*"([^"]+)"', raw)
    if match:
        return normalize_datetime(match.group(1))
    return ""


def source_location_allowed(source: dict[str, Any], location: str) -> bool:
    pattern = str(source.get("location_include_regex") or "").strip()
    if not pattern:
        return True
    try:
        return bool(re.search(pattern, location, flags=re.I))
    except re.error:
        return True


def discover_greenhouse_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    board = greenhouse_board_from_source(source)
    if not board:
        return find_links_for_source(source)
    company = source.get("company", "Unknown Company")
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs?content=true"
    try:
        data = fetch_json(api_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Greenhouse API for {company}: {error}", file=sys.stderr)
        return find_links_for_source(source)

    candidates: list[dict[str, Any]] = []
    for job in data.get("jobs", []):
        url = normalize_job_url(str(job.get("absolute_url") or ""))
        if not url:
            continue
        posted_at = normalize_datetime(job.get("first_published") or job.get("published_at"))
        if not posted_at:
            posted_at = parse_greenhouse_published_at(url)
        updated_at = normalize_datetime(job.get("updated_at"))
        location = job.get("location", {}).get("name", "") if isinstance(job.get("location"), dict) else job.get("location", "")
        if not source_location_allowed(source, str(location or "")):
            continue
        candidates.append(
            {
                "company": company,
                "role": job.get("title") or infer_role_from_url(url),
                "url": url,
                "platform": "greenhouse",
                "location": location or "",
                "posted_at": posted_at,
                "updated_at": updated_at,
                "source": source.get("url", ""),
                "notes": "",
            }
        )
    return candidates


def discover_lever_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    site = lever_site_from_source(source)
    if not site:
        return find_links_for_source(source)
    company = source.get("company", "Unknown Company")
    api_url = f"https://api.lever.co/v0/postings/{urllib.parse.quote(site)}?mode=json"
    try:
        data = fetch_json(api_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Lever API for {company}: {error}", file=sys.stderr)
        return find_links_for_source(source)

    candidates: list[dict[str, Any]] = []
    for job in data:
        categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        url = normalize_job_url(str(job.get("hostedUrl") or job.get("applyUrl") or ""))
        if not url:
            continue
        location = str(categories.get("location", "") or "")
        if not source_location_allowed(source, location):
            continue
        candidates.append(
            {
                "company": company,
                "role": job.get("text") or infer_role_from_url(url),
                "url": url,
                "platform": "lever",
                "location": location,
                "posted_at": normalize_datetime(job.get("createdAt")),
                "updated_at": normalize_datetime(job.get("updatedAt")),
                "source": source.get("url", ""),
                "notes": "",
            }
        )
    return candidates


def discover_ashby_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    board = ashby_board_from_source(source)
    if not board:
        return find_links_for_source(source)
    company = source.get("company", "Unknown Company")
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"
    try:
        data = fetch_json(api_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Ashby API for {company}: {error}", file=sys.stderr)
        return find_links_for_source(source)

    candidates: list[dict[str, Any]] = []
    for job in data.get("jobs", []):
        url = normalize_job_url(str(job.get("jobUrl") or ""))
        if not url:
            continue
        location = str(job.get("location", "") or "")
        if not source_location_allowed(source, location):
            continue
        candidates.append(
            {
                "company": company,
                "role": job.get("title") or infer_role_from_url(url),
                "url": url,
                "platform": "ashby",
                "location": location,
                "posted_at": normalize_datetime(job.get("publishedDate") or job.get("publishedAt") or job.get("createdAt")),
                "updated_at": normalize_datetime(job.get("updatedAt")),
                "source": source.get("url", ""),
                "notes": "",
            }
        )
    return candidates


GEM_LIST_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations {
        name
        city
        isoCountry
        isRemote
      }
      job {
        locationType
        employmentType
        department {
          name
        }
      }
    }
  }
}
"""


GEM_DETAIL_QUERY = """
query ExternalJobPostingQuery($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id
    title
    extId
    startDateTs
    firstPublishedTsSec
  }
}
"""


def discover_gem_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    board = gem_board_from_source(source)
    if not board:
        return find_links_for_source(source)
    company = source.get("company", "Unknown Company")
    endpoint = "https://jobs.gem.com/api/public/graphql"
    try:
        data = fetch_json_post(
            endpoint,
            {
                "operationName": "JobBoardList",
                "variables": {"boardId": board},
                "query": GEM_LIST_QUERY,
            },
        )
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Gem API for {company}: {error}", file=sys.stderr)
        return find_links_for_source(source)

    postings = data.get("data", {}).get("oatsExternalJobPostings", {}).get("jobPostings", [])
    candidates: list[dict[str, Any]] = []
    for job in postings:
        ext_id = str(job.get("extId") or "").strip()
        if not ext_id:
            continue
        posted_at = ""
        updated_at = ""
        try:
            detail = fetch_json_post(
                endpoint,
                {
                    "operationName": "ExternalJobPostingQuery",
                    "variables": {"boardId": board, "extId": ext_id},
                    "query": GEM_DETAIL_QUERY,
                },
                timeout=12,
            )
            posting = detail.get("data", {}).get("oatsExternalJobPosting", {}) or {}
            posted_at = normalize_datetime(posting.get("firstPublishedTsSec") or posting.get("startDateTs"))
            updated_at = normalize_datetime(posting.get("startDateTs"))
        except Exception:  # noqa: BLE001
            pass
        locations = job.get("locations") if isinstance(job.get("locations"), list) else []
        location_names = [str(item.get("name") or item.get("city") or "").strip() for item in locations if isinstance(item, dict)]
        candidates.append(
            {
                "company": company,
                "role": job.get("title") or infer_role_from_url(ext_id),
                "url": normalize_job_url(f"https://jobs.gem.com/{board}/{ext_id}"),
                "platform": "gem",
                "location": ", ".join([name for name in location_names if name]),
                "posted_at": posted_at,
                "updated_at": updated_at,
                "source": source.get("url", ""),
                "notes": "",
            }
        )
    return candidates


def rippling_slug_from_source(source: dict[str, Any]) -> str:
    if source.get("slug"):
        return str(source["slug"]).strip("/")
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else ""


def rippling_next_data(raw_html: str) -> dict[str, Any]:
    match = re.search(r'<script\s+id=["\']__NEXT_DATA__["\']\s+type=["\']application/json["\']>(.*?)</script>', raw_html, flags=re.I | re.S)
    if not match:
        return {}
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return {}
    return data.get("props", {}).get("pageProps", {}).get("apiData", {}) if isinstance(data, dict) else {}


def rippling_detail_job(source: dict[str, Any], slug: str, job_id: str, summary: dict[str, str]) -> dict[str, Any] | None:
    url = normalize_job_url(f"https://ats.rippling.com/{urllib.parse.quote(slug)}/jobs/{urllib.parse.quote(job_id)}")
    try:
        raw = fetch_url(url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Rippling job detail for {source.get('company', slug)}: {error}", file=sys.stderr)
        return None
    api_data = rippling_next_data(raw)
    job_post = api_data.get("jobPost") if isinstance(api_data.get("jobPost"), dict) else {}
    department = api_data.get("department") if isinstance(api_data.get("department"), dict) else {}
    descriptions = job_post.get("description") if isinstance(job_post.get("description"), dict) else {}
    description = "\n\n".join(html_to_text(str(value)) for value in descriptions.values() if str(value).strip())
    locations = api_data.get("workLocations") if isinstance(api_data.get("workLocations"), list) else []
    location = ", ".join(str(item).strip() for item in locations if str(item).strip()) or summary.get("location", "")
    role = str(job_post.get("name") or summary.get("role") or infer_role_from_url(url)).strip()
    posted_raw = job_post.get("postedAt") or job_post.get("createdAt")
    return {
        "company": source.get("company", "Unknown Company"),
        "role": role,
        "url": url,
        "platform": "rippling_jobs",
        "location": location,
        "posted_at": normalize_datetime(posted_raw),
        "updated_at": normalize_datetime(job_post.get("updatedAt")),
        "source": source.get("url", f"https://ats.rippling.com/{slug}/jobs"),
        "source_query": "",
        "external_job_id": str(job_post.get("uuid") or job_id),
        "job_number": str(job_post.get("uuid") or job_id),
        "description": description,
        "_jd_text": "\n\n".join(block for block in [role, location, description] if block),
        "department": str(department.get("name") or summary.get("department") or ""),
        "notes": "Rippling public job board adapter. Rippling does not always expose posted_at in the rendered payload, so first_seen may be the freshness fallback.",
        "freshness_source": "official_posted_at" if posted_raw else "unknown",
    }


def discover_rippling_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    slug = rippling_slug_from_source(source)
    if not slug:
        return find_links_for_source(source)
    board_url = str(source.get("url") or f"https://ats.rippling.com/{slug}/jobs")
    if "ats.rippling.com" not in urllib.parse.urlparse(board_url).netloc.lower():
        board_url = f"https://ats.rippling.com/{slug}/jobs"
    try:
        raw = fetch_url(board_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Rippling board for {source.get('company', slug)}: {error}", file=sys.stderr)
        return []

    summaries: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        rf'<a[^>]+href=["\']/(?P<path>{re.escape(slug)}/jobs/(?P<id>[0-9a-f-]{{36}}))["\'][^>]*>(?P<title>.*?)</a>'
        r'(?P<body>.*?)(?=<a[^>]+href=["\']/[^/]+/jobs/[0-9a-f-]{36}["\']|</script>)',
        flags=re.I | re.S,
    )
    for match in pattern.finditer(raw):
        job_id = match.group("id")
        body = match.group("body")
        metadata = re.findall(r'<p[^>]*class=["\'][^"\']*css-kcy3vt[^"\']*["\'][^>]*>(.*?)</p>', body, flags=re.I | re.S)
        summaries[job_id] = {
            "role": html_to_text(match.group("title")),
            "department": html_to_text(metadata[0]) if metadata else "",
            "location": html_to_text(metadata[-1]) if metadata else "",
        }

    if not summaries:
        for job_id in sorted(set(re.findall(rf'/{re.escape(slug)}/jobs/([0-9a-f-]{{36}})', raw, flags=re.I))):
            summaries[job_id] = {"role": "", "department": "", "location": ""}

    candidates: dict[str, dict[str, Any]] = {}
    for job_id, summary in summaries.items():
        detail = rippling_detail_job(source, slug, job_id, summary)
        if detail:
            candidates[detail["url"]] = detail
    return list(candidates.values())


def parse_workday_posted_on(value: Any, now: dt.datetime | None = None) -> str:
    if value in (None, ""):
        return ""
    now = now or dt.datetime.now(dt.timezone.utc)
    raw = str(value).strip()
    normalized = normalize_datetime(raw)
    if normalized:
        return normalized
    text = raw.lower()
    if "today" in text:
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if "yesterday" in text:
        return (now - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r"(?:about|approximately|around)?\s*(an?|one)\s+hours?\b", text)
    if match:
        return (now - dt.timedelta(hours=1)).replace(microsecond=0).isoformat()
    match = re.search(r"(?:about|approximately|around)?\s*(an?|one)\s+days?\b", text)
    if match:
        return (now - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r"(\d+)\+?\s+days?\s+ago", text)
    if match:
        return (now - dt.timedelta(days=int(match.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r"(\d+)\+?\s+hours?\s+ago", text)
    if match:
        return (now - dt.timedelta(hours=int(match.group(1)))).replace(microsecond=0).isoformat()
    match = re.search(r"(?:about|approximately|around)?\s*(\d+)\+?\s+days?\b", text)
    if match:
        return (now - dt.timedelta(days=int(match.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r"(?:about|approximately|around)?\s*(\d+)\+?\s+hours?\b", text)
    if match:
        return (now - dt.timedelta(hours=int(match.group(1)))).replace(microsecond=0).isoformat()
    match = re.search(r"(?:about|approximately|around)?\s*(\d+)\+?\s+weeks?\b", text)
    if match:
        return (now - dt.timedelta(days=7 * int(match.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r"(?:about|approximately|around)?\s*(\d+)\+?\s+months?\b", text)
    if match:
        return (now - dt.timedelta(days=30 * int(match.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r"(?:about|approximately|around)?\s*(\d+)\+?\s+years?\b", text)
    if match:
        return (now - dt.timedelta(days=365 * int(match.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    raw = re.sub(r"^posted\s+(on\s+)?", "", raw, flags=re.I).strip()
    return normalize_datetime(raw)


def workday_api_url(host: str, tenant: str, site: str, suffix: str) -> str:
    suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    return f"https://{host}/wday/cxs/{urllib.parse.quote(tenant)}/{urllib.parse.quote(site)}{suffix}"


def workday_board_url(host: str, tenant: str, site: str) -> str:
    if "myworkdaysite.com" in host.lower():
        return normalize_job_url(
            f"https://{host}/recruiting/{urllib.parse.quote(tenant)}/{urllib.parse.quote(site)}"
        )
    return normalize_job_url(f"https://{host}/{urllib.parse.quote(site)}")


def workday_human_url(host: str, site: str, external_path: str, tenant: str = "") -> str:
    if not external_path.startswith("/"):
        external_path = f"/{external_path}"
    board_url = workday_board_url(host, tenant, site) if tenant else normalize_job_url(
        f"https://{host}/{urllib.parse.quote(site)}"
    )
    return normalize_job_url(f"{board_url}{external_path}")


def workday_location_text(info: dict[str, Any]) -> str:
    values = [str(info.get("location") or "").strip()]
    additional = info.get("additionalLocations") or []
    if isinstance(additional, str):
        additional = [additional]
    values.extend(str(item).strip() for item in additional if str(item).strip())
    return "; ".join(dict.fromkeys(value for value in values if value))


def discover_workday_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    parts = workday_source_parts(source)
    if not parts:
        return find_links_for_source(source)
    host, tenant, site = parts
    company = source.get("company", "Unknown Company")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    query_keywords = [""] if truthy_source_flag(source.get("search_all"), default=False) else [
        str(item) for item in keywords if str(item).strip() or len(keywords) == 1
    ]
    limit = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    candidates: dict[str, dict[str, Any]] = {}
    endpoint = workday_api_url(host, tenant, site, "/jobs")
    detail_cache: dict[str, dict[str, Any]] = {}

    for keyword in query_keywords:
        for page_index in range(max_pages):
            payload = {
                "appliedFacets": source.get("applied_facets", {}),
                "limit": limit,
                "offset": page_index * limit,
                "searchText": keyword,
            }
            try:
                data = fetch_json_post(endpoint, payload)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Workday API for {company}: {error}", file=sys.stderr)
                break
            postings = data.get("jobPostings") or data.get("jobs") or []
            if not postings:
                break
            for job in postings:
                external_path = str(job.get("externalPath") or job.get("externalPathname") or "").strip()
                if not external_path:
                    continue
                url = workday_human_url(host, site, external_path, tenant=tenant)
                title = job.get("title") or job.get("jobTitle") or infer_role_from_url(url)
                locations = job.get("locationsText") or job.get("locationsDisplayText") or job.get("location") or ""
                posted_at = parse_workday_posted_on(job.get("postedOn") or job.get("postedOnDate"))
                if not locations or re.fullmatch(r"\s*\d+\s+locations?\s*", str(locations), flags=re.I):
                    if external_path not in detail_cache:
                        try:
                            detail_cache[external_path] = fetch_json(
                                workday_api_url(host, tenant, site, external_path)
                            )
                        except Exception:  # noqa: BLE001
                            detail_cache[external_path] = {}
                    info = detail_cache[external_path].get("jobPostingInfo", {})
                    if isinstance(info, dict):
                        locations = workday_location_text(info) or locations
                        posted_at = parse_workday_posted_on(
                            info.get("postedOn") or info.get("startDate")
                        ) or posted_at
                candidates[url] = {
                    "company": company,
                    "role": title,
                    "url": url,
                    "platform": "workday",
                    "location": locations,
                    "posted_at": posted_at,
                    "updated_at": "",
                    "source": source.get("url", ""),
                    "source_query": keyword or "all",
                    "external_job_id": job.get("bulletFields", [""])[0] if isinstance(job.get("bulletFields"), list) else "",
                    "notes": "",
                }
            if len(postings) < limit:
                break
    return list(candidates.values())


def bytedance_location_text(city_info: Any) -> str:
    parts: list[str] = []
    current = city_info if isinstance(city_info, dict) else None
    while current:
        name = str(current.get("en_name") or current.get("i18n_name") or current.get("name") or "").strip()
        if name:
            parts.append(name)
        parent = current.get("parent")
        current = parent if isinstance(parent, dict) else None
    return ", ".join(parts)


def discover_bytedance_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "ByteDance / TikTok")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    endpoint = str(source.get("api_url") or "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts")
    limit = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    candidates: dict[str, dict[str, Any]] = {}
    headers = {
        "Content-Type": "application/json",
        "accept-language": "en-US",
        "website-path": str(source.get("website_path") or "en"),
        "origin": "https://joinbytedance.com",
    }
    if source.get("x_tt_env", "boe_epam_api"):
        headers["x-tt-env"] = str(source.get("x_tt_env", "boe_epam_api"))
    opener = urllib.request.build_opener()

    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for page_index in range(max_pages):
            payload = {
                "keyword": keyword,
                "limit": limit,
                "offset": page_index * limit,
                "recruitment_id_list": source.get("recruitment_id_list", []),
                "subject_id_list": source.get("subject_id_list", []),
                "job_category_id_list": source.get("job_category_id_list", []),
                "location_code_list": source.get("location_code_list", []),
                "tag_id_list": source.get("tag_id_list", []),
            }
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with opener.open(request, timeout=20) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch ByteDance jobs API for {company}: {error}", file=sys.stderr)
                break
            if data.get("code") not in (0, "0", None):
                print(f"Could not fetch ByteDance jobs API for {company}: {data.get('message') or data}", file=sys.stderr)
                break
            body = data.get("data") or {}
            jobs = body.get("job_post_list") or body.get("jobs") or []
            if not jobs:
                break
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("id") or "").strip()
                if not job_id:
                    continue
                url = normalize_job_url(f"https://joinbytedance.com/search/{urllib.parse.quote(job_id)}")
                description = "\n\n".join(
                    item for item in [str(job.get("description") or "").strip(), str(job.get("requirement") or "").strip()] if item
                )
                candidates[url] = {
                    "company": company,
                    "role": str(job.get("title") or "").strip(),
                    "url": url,
                    "platform": "bytedance_jobs",
                    "location": bytedance_location_text(job.get("city_info")),
                    "posted_at": normalize_datetime(job.get("publish_time") or job.get("created_at") or job.get("updated_at")),
                    "updated_at": normalize_datetime(job.get("update_time") or job.get("updated_at")),
                    "source": source.get("url", "https://joinbytedance.com/search"),
                    "source_query": keyword,
                    "external_job_id": job_id,
                    "job_number": str(job.get("code") or job_id),
                    "description": description,
                    "notes": "ByteDance/TikTok official careers API adapter; API currently does not always expose posted_at.",
                    "freshness_source": "official_posted_at" if job.get("publish_time") or job.get("created_at") else "unknown",
                }
            total = int(body.get("count") or body.get("total") or 0)
            if len(jobs) < limit or (total and (page_index + 1) * limit >= total):
                break
    return list(candidates.values())


def shein_location_text(job: dict[str, Any]) -> str:
    city_infos = job.get("cityInfos") if isinstance(job.get("cityInfos"), list) else []
    cities = [str(item.get("cityName") or "").strip() for item in city_infos if isinstance(item, dict)]
    parts = [item for item in cities if item]
    country = str(job.get("countryName") or job.get("countryId") or "").strip()
    if country:
        parts.append(country)
    return ", ".join(dict.fromkeys(parts))


def discover_shein_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "SHEIN")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    endpoint = str(source.get("api_url") or "https://careers.shein.com/api/v1/open/grw/front/jobPage")
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in [str(item).strip() for item in keywords if str(item).strip()]:
        for page in range(1, max_pages + 1):
            payload = {
                "current": page,
                "size": page_size,
                "key": keyword,
                "langCode": str(source.get("lang_code") or "EN"),
            }
            try:
                data = fetch_json_post(endpoint, payload)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch SHEIN careers API for {company}: {error}", file=sys.stderr)
                break
            if data.get("code") not in (0, "0", None):
                print(f"Could not fetch SHEIN careers API for {company}: {data.get('msg') or data}", file=sys.stderr)
                break
            body = data.get("info") or data.get("data") or {}
            records = body.get("records") or []
            if not records:
                break
            for job in records:
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("jobId") or "").strip()
                role = str(job.get("jobTitle") or "").strip()
                if not job_id or not role:
                    continue
                detail_url = str(job.get("jobDetailUrl") or "").strip()
                url = detail_url or f"https://careers.shein.com/Recruit?id={urllib.parse.quote(job_id)}"
                url = normalize_job_url(url)
                candidates[url] = {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "shein_jobs",
                    "location": shein_location_text(job),
                    "posted_at": normalize_datetime(job.get("releaseDate")),
                    "updated_at": normalize_datetime(job.get("updateDate") or job.get("releaseDate")),
                    "source": source.get("url", "https://careers.shein.com/All-Jobs"),
                    "source_query": keyword,
                    "external_job_id": job_id,
                    "job_number": job_id,
                    "description": html_to_text(str(job.get("description") or "")),
                    "notes": "SHEIN official careers API adapter.",
                    "freshness_source": "official_posted_at" if job.get("releaseDate") else "unknown",
                }
            total = int(body.get("total") or 0)
            if len(records) < page_size or (total and page * page_size >= total):
                break
    return list(candidates.values())


def dji_location_text(job: dict[str, Any]) -> str:
    parts = [
        str(job.get("locationEnDescription") or job.get("locationDescription") or "").strip(),
        str(job.get("positionEnRegion") or job.get("positionRegion") or "").strip(),
    ]
    return ", ".join(dict.fromkeys(part for part in parts if part))


def discover_dji_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "DJI")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    endpoint = str(source.get("api_url") or "https://we.dji.com/hire_front/api/common/position/queryUsingAndOldPositionVoList")
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in [str(item).strip() for item in keywords if str(item).strip()]:
        for page in range(1, max_pages + 1):
            payload = {
                "showStatus": str(source.get("show_status") or "en"),
                "keyWord": keyword,
                "locationList": source.get("location_list", [None]),
                "currentPage": page,
                "pageSize": page_size,
            }
            try:
                data = fetch_json_post(endpoint, payload)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch DJI careers API for {company}: {error}", file=sys.stderr)
                break
            if data.get("success") is False:
                print(f"Could not fetch DJI careers API for {company}: {data.get('message') or data}", file=sys.stderr)
                break
            body = data.get("data") or {}
            records = body.get("datas") or body.get("records") or []
            if not records:
                break
            for job in records:
                if not isinstance(job, dict):
                    continue
                position_id = str(job.get("positionId") or job.get("jobId") or "").strip()
                role = str(job.get("jobTitle") or "").strip()
                if not position_id or not role:
                    continue
                url = normalize_job_url(f"https://we.dji.com/detail_en.html?positionId={urllib.parse.quote(position_id)}")
                description = "\n\n".join(
                    item
                    for item in [
                        str(job.get("duty") or "").strip(),
                        str(job.get("requirement") or "").strip(),
                    ]
                    if item
                )
                posted_raw = job.get("postdate") or job.get("createdate") or job.get("approveTime")
                candidates[url] = {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "dji_jobs",
                    "location": dji_location_text(job),
                    "posted_at": normalize_datetime(posted_raw),
                    "updated_at": normalize_datetime(job.get("modifiedate") or job.get("approveTime") or posted_raw),
                    "source": source.get("url", "https://we.dji.com/jobs_en.html"),
                    "source_query": keyword,
                    "external_job_id": position_id,
                    "job_number": position_id,
                    "description": html_to_text(description),
                    "notes": "DJI official careers API adapter.",
                    "freshness_source": "official_posted_at" if posted_raw else "unknown",
                }
            total = int(body.get("totalCount") or body.get("total") or 0)
            if len(records) < page_size or (total and page * page_size >= total):
                break
    return list(candidates.values())


def alibaba_search_token(raw_html: str) -> str:
    match = re.search(r'__token__\s*:\s*"([^"]+)"', raw_html)
    return match.group(1) if match else ""


def discover_alibaba_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Alibaba Cloud / Alibaba Group")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    landing_url = str(source.get("url") or "https://talent.alibaba.com/off-campus/position-list?lang=en")
    search_url = str(source.get("api_url") or "https://talent.alibaba.com/position/search")
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    language = str(source.get("language") or "en")
    channel = str(source.get("channel") or "group_overseas_official_site")
    candidates: dict[str, dict[str, Any]] = {}

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    try:
        request = urllib.request.Request(
            landing_url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with opener.open(request, timeout=20) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            token = alibaba_search_token(response.read().decode(charset, errors="replace"))
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Alibaba careers page for {company}: {error}", file=sys.stderr)
        return []
    if not token:
        for cookie in cookie_jar:
            if cookie.name == "XSRF-TOKEN":
                token = cookie.value
                break
    if not token:
        print(f"Could not fetch Alibaba careers API for {company}: missing XSRF token", file=sys.stderr)
        return []

    endpoint = f"{search_url}?_csrf={urllib.parse.quote(token)}"
    for keyword in [str(item).strip() for item in keywords if str(item).strip()]:
        for page in range(1, max_pages + 1):
            payload = {
                "channel": channel,
                "language": language,
                "batchId": source.get("batch_id", ""),
                "categories": source.get("categories", ""),
                "deptCodes": source.get("dept_codes", []),
                "key": keyword,
                "pageIndex": page,
                "pageSize": page_size,
                "regions": source.get("regions", ""),
                "subCategories": source.get("sub_categories", ""),
            }
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": "https://talent.alibaba.com",
                    "Referer": landing_url,
                    "Bx-V": "2.5.31",
                },
                method="POST",
            )
            try:
                with opener.open(request, timeout=20) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    data = json.loads(response.read().decode(charset, errors="replace"))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Alibaba careers API for {company}: {error}", file=sys.stderr)
                break
            if not data.get("success"):
                print(f"Could not fetch Alibaba careers API for {company}: {data.get('errorMsg') or data}", file=sys.stderr)
                break
            body = data.get("content") or {}
            records = body.get("datas") or []
            if not records:
                break
            for job in records:
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("id") or "").strip()
                role = str(job.get("name") or "").strip()
                if not job_id or not role:
                    continue
                url = normalize_job_url(f"https://talent.alibaba.com/en/off-campus/position-detail?positionId={urllib.parse.quote(job_id)}")
                description = "\n\n".join(
                    item for item in [str(job.get("description") or "").strip(), str(job.get("requirement") or "").strip()] if item
                )
                experience = job.get("experience") if isinstance(job.get("experience"), dict) else {}
                candidates[url] = {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "alibaba_jobs",
                    "location": ", ".join(str(item).strip() for item in (job.get("workLocations") or []) if str(item).strip()),
                    "posted_at": normalize_datetime(job.get("publishTime")),
                    "updated_at": normalize_datetime(job.get("modifyTime") or job.get("publishTime")),
                    "source": landing_url,
                    "source_query": keyword,
                    "external_job_id": job_id,
                    "job_number": str(job.get("code") or job_id),
                    "description": html_to_text(description),
                    "minimum_years_experience": experience.get("from", ""),
                    "notes": "Alibaba official careers API adapter.",
                    "freshness_source": "official_posted_at" if job.get("publishTime") else "unknown",
                }
            total = int(body.get("totalCount") or 0)
            if len(records) < page_size or (total and page * page_size >= total):
                break
    return list(candidates.values())


def pdd_location_allowed(location: str, source: dict[str, Any]) -> bool:
    allowed = source.get("allowed_location_keywords") or source.get("locations") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed = [str(item).strip().lower() for item in allowed if str(item).strip()]
    if not allowed:
        return True
    value = str(location or "").strip().lower()
    return any(item in value for item in allowed)


def discover_pdd_globalhr_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "PDD / Temu")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    endpoint = str(source.get("api_url") or "https://careers.pddglobalhr.com/api/careers/api/recruit/position/list")
    referer = str(source.get("url") or "https://careers.pddglobalhr.com/jobs")
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in [str(item).strip() for item in keywords if str(item).strip()]:
        for page in range(1, max_pages + 1):
            payload = {
                "job": source.get("job", ""),
                "page": page,
                "pageSize": page_size,
                "name": keyword,
                "workLocationList": source.get("work_location_list", []),
            }
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": "https://careers.pddglobalhr.com",
                    "Referer": referer,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    data = json.loads(response.read().decode(charset, errors="replace"))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch PDD Global HR API for {company}: {error}", file=sys.stderr)
                break
            if not data.get("success"):
                print(f"Could not fetch PDD Global HR API for {company}: {data.get('errorMsg') or data}", file=sys.stderr)
                break
            body = data.get("result") or {}
            records = body.get("list") or []
            if not records:
                break
            for job in records:
                if not isinstance(job, dict):
                    continue
                code = str(job.get("code") or job.get("id") or "").strip()
                role = str(job.get("name") or "").strip()
                if not code or not role:
                    continue
                location = str(job.get("workLocationName") or job.get("workLocation") or "").strip()
                if not pdd_location_allowed(location, source):
                    continue
                url = normalize_job_url(f"https://careers.pddglobalhr.com/jobs/detail?code={urllib.parse.quote(code)}")
                description = "\n\n".join(
                    item
                    for item in [
                        str(job.get("jobDuty") or "").strip(),
                        str(job.get("serveRequirement") or job.get("jobRequirement") or "").strip(),
                    ]
                    if item
                )
                posted_raw = job.get("releaseTime") or job.get("updateTime")
                candidates[url] = {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "pdd_globalhr_jobs",
                    "location": location,
                    "posted_at": normalize_datetime(posted_raw),
                    "updated_at": normalize_datetime(job.get("updateTime") or posted_raw),
                    "source": referer,
                    "source_query": keyword,
                    "external_job_id": str(job.get("id") or code),
                    "job_number": code,
                    "description": html_to_text(description),
                    "notes": "PDD Global HR official careers API adapter. Temu public careers page currently redirects to LinkedIn, so this source uses the PDD group job board.",
                    "freshness_source": "official_posted_at" if posted_raw else "unknown",
                }
            total = int(body.get("total") or 0)
            if len(records) < page_size or (total and page * page_size >= total):
                break
    return list(candidates.values())


def huawei_title_allowed(role: str, source_query: str, source: dict[str, Any]) -> bool:
    forced_terms = source.get("title_keywords") or DEFAULT_DISCOVERY_TITLE_KEYWORDS + ["sdet", "qa", "quality", "engineer"]
    role_lower = role.lower()
    if any(re.search(rf"\b{re.escape(str(term).lower())}\b", role_lower) for term in forced_terms):
        return True
    query = str(source_query or "").lower()
    if re.search(r"\b(software|backend|frontend|engineer|sdet|qa|cloud|ai|machine|platform|devops)\b", query):
        return keyword_matches_title(query, role)
    return False


def discover_huawei_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Huawei")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    base_url = str(source.get("url") or "https://career.huawei.com/reccampportal/portal5/social-recruitment.html?v=20241208")
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    endpoint_base = str(source.get("api_base") or "https://career.huawei.com/reccampportal/services/portal/portalpub/getJob/newHr/page")
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in [str(item).strip() for item in keywords if str(item).strip()]:
        for page in range(1, max_pages + 1):
            params = {
                "jobType": source.get("job_type", "1"),
                "orderBy": source.get("order_by", "P_COUNT_DESC"),
                "searchText": keyword,
                "language": source.get("language", "en_US"),
            }
            url = f"{endpoint_base.rstrip('/')}/{page_size}/{page}?{urllib.parse.urlencode(params)}"
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": base_url,
                    "Cookie": f"locale={source.get('language', 'en_US')}",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    data = json.loads(response.read().decode(charset, errors="replace"))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Huawei careers API for {company}: {error}", file=sys.stderr)
                break
            records = data.get("result") or []
            if not records:
                break
            for job in records:
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("jobId") or "").strip()
                role = str(job.get("nameEn") or job.get("jobname") or "").strip()
                if not job_id or not role or not huawei_title_allowed(role, keyword, source):
                    continue
                location = str(job.get("jobAddress") or job.get("jobArea") or "").strip()
                if not pdd_location_allowed(location, source):
                    continue
                data_source = str(job.get("dataSource") or "1")
                detail_url = normalize_job_url(
                    f"https://career.huawei.com/reccampportal/portal5/social-recruitment-detail.html?jobId={urllib.parse.quote(job_id)}&dataSource={urllib.parse.quote(data_source)}"
                )
                description = "\n\n".join(
                    item
                    for item in [
                        str(job.get("mainBusinessEn") or job.get("mainBusiness") or "").strip(),
                        str(job.get("jobRequireEn") or job.get("jobRequire") or "").strip(),
                    ]
                    if item
                )
                posted_raw = job.get("releaseDate") or job.get("creationDate")
                candidates[detail_url] = {
                    "company": company,
                    "role": role,
                    "url": detail_url,
                    "platform": "huawei_jobs",
                    "location": location.replace("\\", "/"),
                    "posted_at": normalize_datetime(posted_raw),
                    "updated_at": normalize_datetime(job.get("lastUpdateDate") or posted_raw),
                    "source": base_url,
                    "source_query": keyword,
                    "external_job_id": job_id,
                    "job_number": str(job.get("advertisementCode") or job_id),
                    "description": html_to_text(description),
                    "minimum_years_experience": job.get("workYear", ""),
                    "notes": "Huawei official careers API adapter. Source uses US-location and title guards because Huawei global search returns many unrelated international roles.",
                    "freshness_source": "official_posted_at" if posted_raw else "unknown",
                }
            page_info = data.get("pageVO") or {}
            total_pages = int(page_info.get("totalPages") or 0)
            if len(records) < page_size or (total_pages and page >= total_pages):
                break
    return list(candidates.values())


def phenom_job_url(source: dict[str, Any], job: dict[str, Any]) -> str:
    base_url = str(source.get("base_url") or source.get("url") or "").rstrip("/")
    locale_path = str(source.get("locale_path") or "/us/en").strip()
    if locale_path and not locale_path.startswith("/"):
        locale_path = f"/{locale_path}"
    job_id = urllib.parse.quote(str(job.get("jobId") or job.get("reqId") or job.get("jobSeqNo") or ""))
    title_slug = slugify(str(job.get("title") or "job"))
    if base_url and job_id:
        return normalize_job_url(f"{base_url}{locale_path}/job/{job_id}/{title_slug}")
    return normalize_job_url(str(job.get("applyUrl") or source.get("url") or ""))


def discover_phenom_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    endpoint = str(source.get("widgets_url") or urllib.parse.urljoin(str(source.get("url", "")).rstrip("/") + "/", "/widgets"))
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for page_index in range(max_pages):
            payload = {
                "ddoKey": "refineSearch",
                "sortBy": "Most recent",
                "subsearch": "",
                "from": page_index * size,
                "jobs": True,
                "counts": True,
                "all_fields": source.get("all_fields", ["category", "country", "state", "city"]),
                "pageName": source.get("page_name", "search-results"),
                "size": size,
                "clearAll": False,
                "jdsource": "facets",
                "keywords": keyword,
                "global": True,
                "selected_fields": source.get("selected_fields", {}),
            }
            try:
                data = fetch_json_post(endpoint, payload)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Phenom API for {company}: {error}", file=sys.stderr)
                break
            block = data.get("refineSearch", {}) if isinstance(data, dict) else {}
            jobs = block.get("data", {}).get("jobs", []) if isinstance(block.get("data"), dict) else []
            if not jobs:
                break
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                url = phenom_job_url(source, job)
                apply_url = normalize_job_url(str(job.get("applyUrl") or ""))
                if source.get("prefer_apply_url") and apply_url:
                    url = apply_url
                # Prefer Workday detail URLs when available because they expose richer JD text.
                elif detect_platform(apply_url) == "workday":
                    url = apply_url.removesuffix("/apply")
                location_values = job.get("multi_location") if isinstance(job.get("multi_location"), list) else []
                location = "; ".join(str(item) for item in location_values if item) or str(
                    job.get("location") or job.get("cityStateCountry") or job.get("cityState") or ""
                )
                candidates[url] = {
                    "company": company,
                    "role": job.get("title") or infer_role_from_url(url),
                    "url": url,
                    "platform": "phenom",
                    "location": location,
                    "posted_at": normalize_datetime(job.get("postedDate") or job.get("dateCreated")),
                    "updated_at": normalize_datetime(job.get("dateCreated")),
                    "source": source.get("url", ""),
                    "job_number": job.get("reqId") or job.get("jobId") or "",
                    "external_job_id": job.get("jobSeqNo") or "",
                    "notes": "",
                }
            if len(jobs) < size:
                break
    return list(candidates.values())


def m_cloud_location_text(job: dict[str, Any]) -> str:
    locations = []
    google_locations = job.get("google_locations")
    if isinstance(google_locations, list):
        for location in google_locations:
            if not isinstance(location, dict):
                continue
            city = str(location.get("city") or "").strip()
            state = str(location.get("state") or "").strip()
            country = str(location.get("country") or "").strip()
            text = ", ".join(item for item in [city, state, country] if item)
            if text:
                locations.append(text)
    if locations:
        return "; ".join(merge_unique(locations, []))
    for key in ["primary_city", "primary_state", "primary_country"]:
        value = str(job.get(key) or "").strip()
        if value:
            locations.append(value)
    locations.extend(str(item) for item in job.get("addtnl_locations", []) if item) if isinstance(job.get("addtnl_locations"), list) else None
    return "; ".join(merge_unique(locations, []))


def m_cloud_job_url(job: dict[str, Any]) -> str:
    url = str(job.get("url") or "").strip()
    if url:
        return normalize_job_url(url)
    seo_url = str(job.get("seo_url") or "").strip()
    if seo_url:
        return normalize_job_url(seo_url.removesuffix("/apply"))
    job_id = str(job.get("id") or job.get("clientid") or "")
    return normalize_job_url(job_id)


def discover_m_cloud_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    endpoint = str(source.get("api_url") or "").rstrip("/")
    company_name = str(source.get("company_name") or source.get("organization") or "").strip()
    if not endpoint or not company_name:
        return []
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    page_size = int(source.get("page_size", 25))
    max_pages = int(source.get("max_pages", 5))
    custom_attribute_filter = str(source.get("custom_attribute_filter") or "").strip()
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for page_index in range(max_pages):
            params = {
                "pageSize": str(page_size),
                "offset": str(page_index * page_size),
                "companyName": company_name,
                "query": keyword,
                "sortBy": str(source.get("sort_by") or "open_date"),
                "sortOrder": str(source.get("sort_order") or "descending"),
                "callback": "jobSearchCallback",
            }
            if custom_attribute_filter:
                params["customAttributeFilter"] = custom_attribute_filter
            api_url = f"{endpoint}/job/search?{urllib.parse.urlencode(params)}"
            try:
                data = fetch_jsonp(api_url, referer=str(source.get("url") or ""))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch m-cloud API for {company}: {error}", file=sys.stderr)
                break
            results = data.get("searchResults", []) if isinstance(data, dict) else []
            if not results:
                break
            for result in results:
                job = result.get("job", {}) if isinstance(result, dict) else {}
                if not isinstance(job, dict):
                    continue
                url = m_cloud_job_url(job)
                if not url:
                    continue
                role = str(job.get("title") or infer_role_from_url(url)).strip()
                jd_text = html_to_text(str(job.get("description") or ""))
                candidates[url] = {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "m_cloud",
                    "location": m_cloud_location_text(job),
                    "posted_at": normalize_datetime(job.get("open_date")),
                    "updated_at": normalize_datetime(job.get("timestamp")),
                    "source": source.get("url", ""),
                    "job_number": job.get("ref") or job.get("clientid") or "",
                    "external_job_id": str(job.get("id") or ""),
                    "_jd_text": "\n\n".join(block for block in [role, m_cloud_location_text(job), jd_text] if block),
                    "notes": f"m-cloud direct adapter; ref={job.get('ref', '')}",
                }
            if len(results) < page_size:
                break
    return list(candidates.values())


def discover_hirebridge_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    client_id = str(source.get("client_id") or "").strip()
    if not client_id:
        return []
    feed_url = str(source.get("feed_url") or f"https://rss.hirebridge.com/{urllib.parse.quote(client_id)}.json")
    try:
        data = fetch_json(feed_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Hirebridge feed for {company}: {error}", file=sys.stderr)
        return []
    source_block = data.get("source", {}) if isinstance(data, dict) else {}
    jobs = source_block.get("job", [])
    if isinstance(jobs, dict):
        jobs = [jobs]
    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        role = str(job.get("title") or "").strip()
        url = normalize_job_url(str(job.get("url") or job.get("ApplicationURL") or ""))
        if not url:
            continue
        location = str(job.get("location") or "").strip()
        blocks = [
            role,
            location,
            job.get("department", ""),
            job.get("category", ""),
            html_to_text(str(job.get("jobdesc") or job.get("description") or "")),
        ]
        candidates[url] = {
            "company": company,
            "role": role or infer_role_from_url(url),
            "url": url,
            "platform": "hirebridge",
            "location": location,
            "posted_at": normalize_datetime(job.get("date")),
            "updated_at": normalize_datetime(job.get("modifydate")),
            "source": source.get("url", ""),
            "job_number": str(job.get("referencenumber") or ""),
            "external_job_id": str(job.get("referencenumber") or ""),
            "_jd_text": "\n\n".join(str(block) for block in blocks if block),
            "notes": f"Hirebridge direct adapter; ref={job.get('referencenumber', '')}",
        }
    return list(candidates.values())


def successfactors_detail_metadata(url: str) -> dict[str, str]:
    try:
        raw = fetch_url(url, timeout=15)
    except Exception:  # noqa: BLE001
        return {}
    date_match = re.search(
        r'<meta\b[^>]*itemprop=["\']datePosted["\'][^>]*content=["\']([^"\']+)["\']',
        raw,
        flags=re.I | re.S,
    )
    if not date_match:
        date_match = re.search(
            r'<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*itemprop=["\']datePosted["\']',
            raw,
            flags=re.I | re.S,
        )
    job_number_match = re.search(
        r'<span\b[^>]*data-careersite-propertyid=["\']customfield1["\'][^>]*>(.*?)</span>',
        raw,
        flags=re.I | re.S,
    )
    return {
        "posted_at": normalize_datetime(
            html.unescape(date_match.group(1)) if date_match else ""
        ),
        "job_number": html_to_text(job_number_match.group(1)) if job_number_match else "",
        "_jd_text": html_to_text(raw),
    }


def discover_successfactors_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    base_url = str(source.get("url") or "").rstrip("/")
    search_path = str(source.get("search_path") or "/search/")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    query_keywords = [""] if truthy_source_flag(source.get("search_all"), default=False) else [
        str(item) for item in keywords if str(item).strip()
    ]
    max_pages = int(source.get("max_pages", 3))
    page_size = int(source.get("page_size", 25))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in query_keywords:
        for page_index in range(max_pages):
            params = {
                "q": keyword,
                "sortColumn": "referencedate",
                "sortDirection": "desc",
            }
            extra_params = source.get("search_params")
            if isinstance(extra_params, dict):
                params.update({str(key): str(value) for key, value in extra_params.items() if value not in (None, "")})
            if page_index:
                params["startrow"] = str(page_index * page_size)
            search_url = urllib.parse.urljoin(base_url + "/", search_path.lstrip("/")) + "?" + urllib.parse.urlencode(params)
            try:
                raw = fetch_url(search_url)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch SuccessFactors search for {company}: {error}", file=sys.stderr)
                break
            rows = re.findall(r'<tr[^>]+class=["\'][^"\']*data-row[^"\']*["\'][^>]*>(.*?)</tr>', raw, flags=re.I | re.S)
            if not rows:
                break
            for row in rows:
                link_match = re.search(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*jobTitle-link[^"\']*["\'][^>]*>(.*?)</a>', row, flags=re.I | re.S)
                if not link_match:
                    continue
                url = normalize_job_url(urllib.parse.urljoin(base_url + "/", html.unescape(link_match.group(1))))
                role = html_to_text(link_match.group(2))
                date_match = re.search(r'<span[^>]+class=["\'][^"\']*jobDate[^"\']*["\'][^>]*>(.*?)</span>', row, flags=re.I | re.S)
                location_match = re.search(r'<span[^>]+class=["\'][^"\']*jobLocation[^"\']*["\'][^>]*>(.*?)</span>', row, flags=re.I | re.S)
                department_match = re.search(r'<span[^>]+class=["\'][^"\']*jobDepartment[^"\']*["\'][^>]*>(.*?)</span>', row, flags=re.I | re.S)
                country_match = re.search(r'<span[^>]+class=["\'][^"\']*jobShifttype[^"\']*["\'][^>]*>(.*?)</span>', row, flags=re.I | re.S)
                job_number_match = re.search(r'<span[^>]+class=["\'][^"\']*jobFacility[^"\']*["\'][^>]*>(.*?)</span>', row, flags=re.I | re.S)
                location = html_to_text(location_match.group(1)) if location_match else ""
                if not location:
                    location = ", ".join(
                        item
                        for item in [
                            html_to_text(department_match.group(1)) if department_match else "",
                            html_to_text(country_match.group(1)) if country_match else "",
                        ]
                        if item
                    )
                candidates[url] = {
                    "company": company,
                    "role": role or infer_role_from_url(url),
                    "url": url,
                    "platform": "successfactors",
                    "location": location,
                    "posted_at": normalize_datetime(html_to_text(date_match.group(1)) if date_match else ""),
                    "updated_at": "",
                    "source": source.get("url", ""),
                    "source_query": keyword,
                    "job_number": html_to_text(job_number_match.group(1)) if job_number_match else "",
                    "external_job_id": url.rstrip("/").split("/")[-1],
                    "notes": "SuccessFactors search adapter.",
                }
            if len(rows) < page_size:
                break
    if truthy_source_flag(source.get("fetch_details"), default=False):
        selected = list(candidates.values())[: max(0, int(source.get("max_detail_pages", len(candidates))))]
        detail_workers = max(1, int(source.get("detail_workers", 8)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            future_to_candidate = {
                executor.submit(successfactors_detail_metadata, candidate["url"]): candidate
                for candidate in selected
            }
            for future in concurrent.futures.as_completed(future_to_candidate):
                candidate = future_to_candidate[future]
                with contextlib.suppress(Exception):
                    detail = future.result()
                    if detail.get("posted_at"):
                        candidate["posted_at"] = detail["posted_at"]
                        candidate["freshness_source"] = "successfactors_datePosted"
                    if detail.get("job_number"):
                        candidate["job_number"] = detail["job_number"]
                    if detail.get("_jd_text"):
                        candidate["_jd_text"] = detail["_jd_text"]
    return list(candidates.values())


def isg_poweredby_blob_id(source: dict[str, Any]) -> str:
    configured = str(source.get("blob_id") or "").strip()
    if configured:
        return configured
    raw = fetch_url(str(source.get("url") or ""))
    for script_tag in re.findall(r"<script\b[^>]*>", raw, flags=re.I | re.S):
        if "app-hook-v2.bundle.js" not in script_tag:
            continue
        key_match = re.search(r"\bkey=[\"']([^\"']+)[\"']", script_tag, flags=re.I)
        if key_match:
            return html.unescape(key_match.group(1)).strip()
    return ""


def discover_isg_poweredby_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    blob_id = isg_poweredby_blob_id(source)
    if not blob_id:
        raise ValueError(f"Could not find ISG PoweredBy feed key for {company}")
    feed_url = str(
        source.get("feed_url")
        or f"https://isgpoweredbydata.blob.core.windows.net/public-data/{urllib.parse.quote(blob_id)}.json"
    )
    data = fetch_json(feed_url)
    jobs = data if isinstance(data, list) else []
    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("JobId") or job.get("jobId") or "").strip()
        title = str(job.get("Title") or job.get("title") or "").strip()
        if not job_id or not title:
            continue
        locations = job.get("Locations") or job.get("locations") or []
        location_parts: list[str] = []
        if isinstance(locations, list):
            for location in locations:
                if not isinstance(location, dict):
                    continue
                city = str(location.get("City") or location.get("city") or "").strip()
                state = str(location.get("StateCode") or location.get("stateCode") or "").strip()
                value = ", ".join(item for item in [city, state] if item)
                if value and value not in location_parts:
                    location_parts.append(value)
        apply_url = normalize_job_url(str(job.get("ApplyUrl") or job.get("applyUrl") or ""))
        if not apply_url:
            apply_url = f"https://jobs.localjobnetwork.com/apply/add/{urllib.parse.quote(job_id)}"
        description = html_to_text(str(job.get("Description") or job.get("description") or ""))
        details = [
            description,
            str(job.get("ExperienceText") or job.get("experienceText") or "").strip(),
            str(job.get("EducationText") or job.get("educationText") or "").strip(),
            str(job.get("SalaryRange") or job.get("salaryRange") or "").strip(),
            str(job.get("SalaryNotes") or job.get("salaryNotes") or "").strip(),
            str(job.get("WorkHours") or job.get("workHours") or "").strip(),
        ]
        candidates[apply_url] = {
            "company": company,
            "role": title,
            "url": apply_url,
            "platform": "isg_poweredby",
            "location": " + ".join(location_parts),
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": "",
            "updated_at": "",
            "source": source.get("url", feed_url),
            "source_query": f"blob_id={blob_id}",
            "notes": (
                "Official ISG PoweredBy public job feed. The feed does not expose "
                "publication timestamps, so freshness begins at first_seen."
            ),
            "_jd_text": "\n\n".join(item for item in details if item),
        }
    return list(candidates.values())


def microsoft_pcsx_session() -> tuple[urllib.request.OpenerDirector, dict[str, str]]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    base_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    response = opener.open(urllib.request.Request("https://apply.careers.microsoft.com/careers", headers=base_headers), timeout=20)
    response.read()
    csrf_token = response.headers.get("x-csrf-token", "")
    headers = {
        "User-Agent": base_headers["User-Agent"],
        "Accept": "application/json,*/*;q=0.8",
        "Referer": "https://apply.careers.microsoft.com/careers",
    }
    if csrf_token:
        headers["X-CSRFToken"] = csrf_token
    return opener, headers


def microsoft_job_url(position_id: Any) -> str:
    return f"https://jobs.careers.microsoft.com/global/en/job/{position_id}"


def discover_microsoft_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Microsoft")
    keywords = source.get("keywords") or ["Software Engineer", "Software Development Engineer", "New Grad Software Engineer", "AI Engineer"]
    locations = source.get("locations") or ["Redmond, Washington, United States", "Seattle, Washington, United States", "Bellevue, Washington, United States", "Mountain View, California, United States", "San Francisco, California, United States"]
    max_pages = int(source.get("max_pages", 3))
    page_size = 10
    opener, headers = microsoft_pcsx_session()
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in keywords:
        for location in locations:
            for page_index in range(max_pages):
                params = {
                    "domain": "microsoft.com",
                    "query": str(keyword),
                    "location": str(location),
                    "start": str(page_index * page_size),
                    "hl": "en",
                }
                api_url = f"https://apply.careers.microsoft.com/api/pcsx/search?{urllib.parse.urlencode(params)}"
                try:
                    data = fetch_json_with_opener(opener, api_url, headers)
                except Exception as error:  # noqa: BLE001
                    print(f"Could not fetch Microsoft careers search for {keyword} / {location}: {error}", file=sys.stderr)
                    break
                positions = data.get("data", {}).get("positions", []) if isinstance(data, dict) else []
                if not positions:
                    break
                for job in positions:
                    position_id = job.get("id")
                    if not position_id:
                        continue
                    url = normalize_job_url(microsoft_job_url(position_id))
                    location_values = job.get("standardizedLocations") or job.get("locations") or []
                    if isinstance(location_values, str):
                        location_text = location_values
                    else:
                        location_text = "; ".join(str(item) for item in location_values if item)
                    candidates[url] = {
                        "company": company,
                        "role": job.get("name") or infer_role_from_url(url),
                        "url": url,
                        "platform": "microsoft_jobs",
                        "location": location_text,
                        "job_number": str(job.get("displayJobId") or ""),
                        "external_job_id": str(position_id),
                        "posted_at": normalize_datetime(job.get("postedTs")),
                        "updated_at": normalize_datetime(job.get("updatedTs") or job.get("lastModifiedTs")),
                        "source": source.get("url", "https://jobs.careers.microsoft.com"),
                        "notes": f"Microsoft careers direct adapter; display_job_id={job.get('displayJobId', '')}",
                    }
                if len(positions) < page_size:
                    break
    return list(candidates.values())


def amazon_job_url(job: dict[str, Any]) -> str:
    job_path = str(job.get("job_path") or "")
    if job_path:
        return urllib.parse.urljoin("https://www.amazon.jobs", job_path)
    job_id = str(job.get("id_icims") or job.get("id") or "")
    return f"https://www.amazon.jobs/en/jobs/{urllib.parse.quote(job_id)}"


def amazon_location_text(job: dict[str, Any]) -> str:
    locations = job.get("locations") or []
    parsed_locations: list[str] = []
    if isinstance(locations, list):
        for item in locations:
            if isinstance(item, str):
                try:
                    value = json.loads(item)
                except json.JSONDecodeError:
                    value = {"location": item}
            elif isinstance(item, dict):
                value = item
            else:
                continue
            parsed = value.get("normalizedLocation") or value.get("locationNonStemming") or value.get("location")
            if parsed:
                parsed_locations.append(str(parsed))
    return "; ".join(merge_unique(parsed_locations, [])) or str(job.get("normalized_location") or job.get("location") or "")


def discover_amazon_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Amazon")
    keywords = source.get("keywords") or [
        "Software Development Engineer",
        "Software Engineer",
        "Backend Engineer",
        "AI Engineer",
        "SDET",
    ]
    locations = source.get("locations") or [
        "Seattle, Washington, United States",
        "Bellevue, Washington, United States",
        "Redmond, Washington, United States",
        "San Francisco, California, United States",
        "Palo Alto, California, United States",
        "United States",
    ]
    max_pages = int(source.get("max_pages", 3))
    page_size = int(source.get("page_size", 10))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in keywords:
        for location in locations:
            for page_index in range(max_pages):
                params = {
                    "base_query": str(keyword),
                    "loc_query": str(location),
                    "offset": str(page_index * page_size),
                    "result_limit": str(page_size),
                    "sort": "recent",
                }
                api_url = f"https://www.amazon.jobs/en/search.json?{urllib.parse.urlencode(params)}"
                try:
                    data = fetch_json(api_url)
                except Exception as error:  # noqa: BLE001
                    print(f"Could not fetch Amazon jobs search for {keyword} / {location}: {error}", file=sys.stderr)
                    break
                jobs = data.get("jobs", []) if isinstance(data, dict) else []
                if not jobs:
                    break
                for job in jobs:
                    url = normalize_job_url(amazon_job_url(job))
                    job_number = str(job.get("id_icims") or "")
                    candidates[url] = {
                        "company": company,
                        "role": str(job.get("title") or infer_role_from_url(url)).strip(),
                        "url": url,
                        "platform": "amazon_jobs",
                        "location": amazon_location_text(job),
                        "job_number": job_number,
                        "external_job_id": str(job.get("id") or job_number),
                        "posted_at": normalize_datetime(job.get("posted_date")),
                        "updated_at": normalize_datetime(job.get("updated_time")),
                        "source": source.get("url", "https://www.amazon.jobs"),
                        "notes": f"Amazon jobs direct adapter; id_icims={job_number}",
                    }
                if len(jobs) < page_size:
                    break
    return list(candidates.values())


def google_job_url(job_id: Any, title: str = "") -> str:
    suffix = f"-{slugify(title)}" if title else ""
    return f"https://www.google.com/about/careers/applications/jobs/results/{urllib.parse.quote(str(job_id))}{suffix}"


def extract_balanced_json_array(raw: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


def google_array_text(value: Any) -> str:
    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return html_to_text(value[1])
    if isinstance(value, str):
        return html_to_text(value)
    return ""


def google_timestamp(value: Any) -> str:
    if isinstance(value, list) and value and isinstance(value[0], (int, float)):
        return normalize_datetime(value[0])
    return normalize_datetime(value)


def parse_google_jobs_from_html(raw: str, source_url: str, fallback_company: str = "Google") -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r'\["\d{10,}","', raw):
        payload = extract_balanced_json_array(raw, match.start())
        if not payload:
            continue
        try:
            job = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if len(job) < 11 or not str(job[0]).isdigit():
            continue
        job_id = str(job[0])
        role = str(job[1] or infer_role_from_url(job_id)).strip()
        locations = []
        if isinstance(job[9], list):
            for item in job[9]:
                if isinstance(item, list) and item:
                    locations.append(str(item[0]))
                elif isinstance(item, str):
                    locations.append(item)
        url = normalize_job_url(google_job_url(job_id, role))
        responsibilities = google_array_text(job[3] if len(job) > 3 else "")
        qualifications = google_array_text(job[4] if len(job) > 4 else "")
        description = google_array_text(job[10] if len(job) > 10 else "")
        candidates[url] = {
            "company": str(job[7] or fallback_company),
            "role": role,
            "url": url,
            "platform": "google_jobs",
            "location": "; ".join(merge_unique(locations, [])),
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": google_timestamp(job[12] if len(job) > 12 else ""),
            "updated_at": google_timestamp(job[13] if len(job) > 13 else ""),
            "source": source_url,
            "notes": "Google careers direct adapter; timestamps are parsed from Google Careers embedded data.",
            "_jd_text": "\n\n".join(block for block in [role, "; ".join(locations), description, responsibilities, qualifications] if block),
        }
    return list(candidates.values())


def discover_google_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Google")
    keywords = source.get("keywords") or [
        "Software Engineer",
        "Early Career Software Engineer",
        "AI Engineer",
        "Backend Engineer",
        "Site Reliability Engineer",
    ]
    locations = source.get("locations") or [
        "Seattle, WA, USA",
        "Kirkland, WA, USA",
        "Sunnyvale, CA, USA",
        "Mountain View, CA, USA",
        "San Francisco, CA, USA",
    ]
    max_pages = int(source.get("max_pages", 2))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in keywords:
        for location in locations:
            for page_index in range(max_pages):
                params = {
                    "q": str(keyword),
                    "location": str(location),
                }
                if page_index:
                    params["page"] = str(page_index + 1)
                search_url = f"https://www.google.com/about/careers/applications/jobs/results/?{urllib.parse.urlencode(params)}"
                try:
                    raw = fetch_url(search_url)
                except Exception as error:  # noqa: BLE001
                    print(f"Could not fetch Google careers search for {keyword} / {location}: {error}", file=sys.stderr)
                    break
                page_candidates = parse_google_jobs_from_html(raw, source.get("url", "https://www.google.com/about/careers/applications/jobs/results/"), str(company))
                if not page_candidates:
                    break
                for candidate in page_candidates:
                    candidate.pop("_jd_text", None)
                    candidates[candidate["url"]] = candidate
                if f"page={page_index + 2}" not in raw and f"page&#61;{page_index + 2}" not in raw:
                    break
    return list(candidates.values())


def discover_meta_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Meta")
    keywords = source.get("keywords") or ["Software Engineer", "Backend Engineer", "AI Engineer", "SDET"]
    locations = source.get("locations") or ["Seattle, WA", "Bellevue, WA", "Menlo Park, CA", "San Francisco, CA", "Remote, US"]
    max_requests = max(1, int(source.get("max_requests", 1)))
    include_location_filters = bool(source.get("include_location_filters", False))
    search_queries = [str(item) for item in source.get("search_queries", []) if str(item).strip()]
    if not search_queries:
        search_queries = [str(keyword) for keyword in keywords]
    candidates: dict[str, dict[str, Any]] = {}
    requests_made = 0

    for query in search_queries:
        location_values = locations if include_location_filters else [""]
        for location in location_values:
            if requests_made >= max_requests:
                return list(candidates.values())
            params = {"q": str(query)}
            if location:
                params["locations[0]"] = str(location)
            search_url = f"https://www.metacareers.com/jobs/?{urllib.parse.urlencode(params)}"
            requests_made += 1
            try:
                raw = fetch_url(search_url)
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    print("Meta careers returned 429 Too Many Requests; stopping Meta adapter for this run.", file=sys.stderr)
                    return list(candidates.values())
                print(f"Could not fetch Meta careers search for {query} / {location or 'all locations'}: {error}", file=sys.stderr)
                continue
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Meta careers search for {query} / {location or 'all locations'}: {error}", file=sys.stderr)
                continue
            for job_id in re.findall(r"/(?:v2/)?jobs/(\d{6,})", raw):
                url = normalize_job_url(f"https://www.metacareers.com/jobs/{job_id}/")
                candidates[url] = {
                    "company": company,
                    "role": infer_role_from_url(url),
                    "url": url,
                    "platform": "meta_jobs",
                    "location": location,
                    "job_number": job_id,
                    "external_job_id": job_id,
                    "posted_at": "",
                    "updated_at": "",
                    "source": source.get("url", "https://www.metacareers.com/jobs/"),
                    "source_query": query,
                    "notes": "Meta careers page adapter; Meta does not expose posted_at in static search HTML.",
                }
    if not candidates:
        print("Meta careers did not expose static job results; use --include-unknown-posted-date only for manual Meta review.", file=sys.stderr)
    return list(candidates.values())


def eightfold_job_url(source: dict[str, Any], job: dict[str, Any]) -> str:
    base_url = str(source.get("base_url") or source.get("url") or "").rstrip("/")
    if not base_url:
        base_url = "https://jobs.nvidia.com" if str(source.get("domain") or "").lower() == "nvidia.com" else ""
    position_url = str(job.get("canonicalPositionUrl") or job.get("positionUrl") or "").strip()
    if position_url:
        return normalize_job_url(urllib.parse.urljoin(base_url + "/", position_url.lstrip("/")))
    job_id = str(job.get("id") or job.get("position_id") or "").strip()
    return normalize_job_url(urllib.parse.urljoin(base_url + "/", f"careers/job/{job_id}"))


def eightfold_candidate_from_job(source: dict[str, Any], job: dict[str, Any], keyword: str = "", queried_location: str = "") -> dict[str, Any] | None:
    company = source.get("company", "Unknown Company")
    url = eightfold_job_url(source, job)
    if not url:
        return None
    location_values = job.get("standardizedLocations") or job.get("locations") or job.get("location") or []
    if isinstance(location_values, str):
        location_text = location_values
    else:
        location_text = "; ".join(str(item) for item in location_values if item)
    posted_raw = job.get("postedTs")
    if not posted_raw:
        posted_raw = job.get("t_create") or job.get("creationTs") or job.get("createdTs")
    updated_raw = job.get("t_update") or job.get("updatedTs")
    if not updated_raw:
        updated_raw = job.get("creationTs")
    notes = "Eightfold direct adapter."
    if queried_location:
        notes = f"Eightfold direct adapter; queried_location={queried_location}"
    return {
        "company": company,
        "role": str(job.get("name") or job.get("posting_name") or job.get("displayJobTitle") or infer_role_from_url(url)).strip(),
        "url": url,
        "platform": "eightfold",
        "location": location_text,
        "job_number": str(job.get("displayJobId") or job.get("display_job_id") or job.get("atsJobId") or job.get("ats_job_id") or ""),
        "external_job_id": str(job.get("id") or ""),
        "posted_at": normalize_datetime(posted_raw),
        "updated_at": normalize_datetime(updated_raw),
        "source": source.get("url", ""),
        "source_query": keyword,
        "notes": notes,
    }


def discover_eightfold_html_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    configured_urls = source.get("html_urls") or [
        source.get("url") or source.get("base_url") or ""
    ]
    if isinstance(configured_urls, str):
        configured_urls = [configured_urls]
    page_urls = [str(item).strip() for item in configured_urls if str(item).strip()]
    if not page_urls:
        return []
    candidates: dict[str, dict[str, Any]] = {}
    for page_url in page_urls:
        try:
            raw = html.unescape(fetch_url(page_url))
        except Exception as error:  # noqa: BLE001
            print(
                f"Could not fetch Eightfold careers page for {source.get('company', 'Unknown Company')}: {error}",
                file=sys.stderr,
            )
            continue
        match = re.search(r'"positions"\s*:\s*', raw)
        if not match:
            continue
        try:
            positions, _ = json.JSONDecoder().raw_decode(raw[match.end() :])
        except json.JSONDecodeError:
            continue
        for job in positions if isinstance(positions, list) else []:
            if not isinstance(job, dict):
                continue
            if not eightfold_job_matches_source(source, job):
                continue
            candidate = eightfold_candidate_from_job(source, job)
            if candidate:
                candidate["source_query"] = page_url
                candidate["notes"] = "Eightfold HTML fallback; API was unavailable or blocked."
                candidates[candidate["url"]] = candidate
    return list(candidates.values())


def eightfold_job_matches_source(source: dict[str, Any], job: dict[str, Any]) -> bool:
    allowed = source.get("operating_companies") or source.get("operating_company") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed_values = {str(item).strip().lower() for item in allowed if str(item).strip()}
    if not allowed_values:
        return True
    actual = job.get("efcustomTextOperatingcompany") or job.get("operatingCompany") or []
    if isinstance(actual, str):
        actual = [actual]
    actual_values = {str(item).strip().lower() for item in actual if str(item).strip()}
    return bool(allowed_values & actual_values)


def discover_eightfold_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    base_url = str(source.get("base_url") or source.get("url") or "").rstrip("/")
    domain = str(source.get("domain") or urllib.parse.urlparse(base_url).netloc.replace(".eightfold.ai", ".com")).strip()
    if not base_url or not domain:
        return []
    if truthy_source_flag(source.get("html_only"), default=False):
        return discover_eightfold_html_jobs(source)
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    locations = source.get("locations") or [
        "Seattle, WA",
        "Bellevue, WA",
        "Redmond, WA",
        "San Francisco, CA",
        "Sunnyvale, CA",
        "Santa Clara, CA",
        "Remote, US",
    ]
    max_pages = int(source.get("max_pages", 3))
    page_size = int(source.get("page_size", 10))
    candidates: dict[str, dict[str, Any]] = {}

    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for location in [str(item) for item in locations if str(item).strip()]:
            for page_index in range(max_pages):
                params = {
                    "domain": domain,
                    "query": keyword,
                    "location": location,
                    "start": str(page_index * page_size),
                    "sort_by": str(source.get("sort_by") or "timestamp"),
                }
                api_url = f"{base_url}/api/pcsx/search?{urllib.parse.urlencode(params)}"
                try:
                    data = fetch_json(api_url)
                except Exception as error:  # noqa: BLE001
                    print(f"Could not fetch Eightfold API for {company}: {error}", file=sys.stderr)
                    if not candidates:
                        return discover_eightfold_html_jobs(source)
                    break
                block = data.get("data", {}) if isinstance(data, dict) else {}
                jobs = block.get("positions", []) if isinstance(block, dict) else []
                if not jobs:
                    break
                for job in jobs:
                    if not isinstance(job, dict):
                        continue
                    if not eightfold_job_matches_source(source, job):
                        continue
                    candidate = eightfold_candidate_from_job(source, job, keyword=keyword, queried_location=location)
                    if candidate:
                        candidates[candidate["url"]] = candidate
                if len(jobs) < page_size:
                    break
    return list(candidates.values())


def parse_apple_search_results(raw: str, source_url: str, company: str = "Apple") -> tuple[list[dict[str, Any]], int]:
    match = re.search(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);", raw, flags=re.S)
    if not match:
        return [], 0
    try:
        data = json.loads(json.loads(match.group(1)))
    except json.JSONDecodeError:
        return [], 0
    search_data = data.get("loaderData", {}).get("search", {}) if isinstance(data, dict) else {}
    results = search_data.get("searchResults", []) if isinstance(search_data, dict) else []
    total_records = int(search_data.get("totalRecords") or 0) if isinstance(search_data, dict) else 0
    candidates: dict[str, dict[str, Any]] = {}
    for job in results:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("positionId") or job.get("jobPositionId") or "").strip()
        title = str(job.get("postingTitle") or job.get("title") or "").strip()
        slug = str(job.get("transformedPostingTitle") or slugify(title)).strip()
        team = job.get("team", {}).get("teamCode") if isinstance(job.get("team"), dict) else ""
        team_query = f"?team={urllib.parse.quote(str(team))}" if team else ""
        url = normalize_job_url(f"https://jobs.apple.com/en-us/details/{urllib.parse.quote(job_id)}/{slug}{team_query}") if job_id else source_url
        locations = []
        for location in job.get("locations", []) if isinstance(job.get("locations"), list) else []:
            if not isinstance(location, dict):
                continue
            name = location.get("name") or ", ".join(str(location.get(key) or "") for key in ["city", "stateProvince", "countryName"] if location.get(key))
            if name:
                locations.append(str(name))
        candidates[url] = {
            "company": company,
            "role": title or infer_role_from_url(url),
            "url": url,
            "platform": "apple_jobs",
            "location": "; ".join(merge_unique(locations, [])),
            "job_number": str(job.get("reqId") or ""),
            "external_job_id": job_id,
            "posted_at": normalize_datetime(job.get("postDateInGMT") or job.get("postingDate")),
            "updated_at": "",
            "source": source_url,
            "notes": "Apple careers direct adapter; parsed from static router hydration data.",
        }
    return list(candidates.values()), total_records


def discover_apple_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Apple")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    locations = source.get("locations") or ["united-states-USA"]
    teams = source.get("teams") or ["software-and-services-SFTWR"]
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for location in [str(item) for item in locations if str(item).strip()]:
            for team in [str(item) for item in teams if str(item).strip()]:
                for page_index in range(max_pages):
                    params = {
                        "sort": "newest",
                        "location": location,
                        "team": team,
                        "search": keyword,
                    }
                    if page_index:
                        params["page"] = str(page_index + 1)
                    search_url = f"https://jobs.apple.com/en-us/search?{urllib.parse.urlencode(params)}"
                    try:
                        raw = fetch_url(search_url)
                    except Exception as error:  # noqa: BLE001
                        print(f"Could not fetch Apple careers search for {keyword}: {error}", file=sys.stderr)
                        break
                    page_candidates, total_records = parse_apple_search_results(raw, source.get("url", search_url), str(company))
                    if not page_candidates:
                        break
                    for candidate in page_candidates:
                        candidate["source_query"] = keyword
                        candidates[candidate["url"]] = candidate
                    if len(candidates) >= total_records or len(page_candidates) < 20:
                        break
    return list(candidates.values())


def providence_attr(job: dict[str, Any], key: str) -> str:
    custom = job.get("customAttributes", {}) if isinstance(job.get("customAttributes"), dict) else {}
    value = custom.get(key, {}) if isinstance(custom.get(key), dict) else {}
    values = value.get("stringValues", []) if isinstance(value, dict) else []
    return str(values[0]) if values else ""


def providence_job_url(job: dict[str, Any]) -> str:
    city_slug = providence_attr(job, "city_display_slug") or slugify(providence_attr(job, "city_display") or "remote")
    title_slug = providence_attr(job, "title_slug") or slugify(str(job.get("title") or "job"))
    req_id = str(job.get("requisitionId") or providence_attr(job, "reqid") or "").strip()
    if req_id:
        return normalize_job_url(f"https://providence.jobs/{city_slug}/{title_slug}/{urllib.parse.quote(req_id)}/job/")
    apply_urls = job.get("applicationInfo", {}).get("uris", []) if isinstance(job.get("applicationInfo"), dict) else []
    return normalize_job_url(str(apply_urls[0])) if apply_urls else "https://providence.jobs/jobs/"


def discover_providence_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Providence")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for page_index in range(max_pages):
            params = {
                "q": keyword,
                "page": str(page_index + 1),
                "num_items": str(page_size),
                "source": "google_talent",
                "use_solr_filters": "true",
                "tenant_uuid": str(source.get("tenant_uuid") or "eb572606-dfb6-4aeb-b85c-0ec27f806dd6"),
                "company_uuids": str(source.get("company_uuid") or "c677bf29-de60-446f-bc13-fe37c6eb46b2"),
                "googleTalentDiversificationLevel": "DISABLED",
                "buids": str(source.get("buids") or "59189,14582,17234,36244,37007,41626,55042,53254"),
            }
            api_url = "https://prod-search-api.jobsyn.org/api/v1/google-talent/search?" + urllib.parse.urlencode(params)
            request = urllib.request.Request(
                api_url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json,*/*;q=0.8",
                    "X-Origin": "providence.jobs",
                    "Referer": "https://providence.jobs/jobs/",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace"))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Providence jobs API for {keyword}: {error}", file=sys.stderr)
                break
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            if not jobs:
                break
            for item in jobs:
                job = item.get("job", {}) if isinstance(item, dict) else {}
                if not isinstance(job, dict):
                    continue
                url = providence_job_url(job)
                location = providence_attr(job, "full_location") or "; ".join(str(item) for item in job.get("addresses", []) if item)
                candidates[url] = {
                    "company": company,
                    "role": str(job.get("title") or providence_attr(job, "title") or infer_role_from_url(url)).strip(),
                    "url": url,
                    "platform": "providence_jobs",
                    "location": location,
                    "job_number": str(providence_attr(job, "reqid") or job.get("requisitionId") or ""),
                    "external_job_id": str(job.get("name") or job.get("requisitionId") or ""),
                    "posted_at": normalize_datetime(job.get("postingPublishTime") or job.get("postingCreateTime")),
                    "updated_at": normalize_datetime(job.get("postingUpdateTime")),
                    "source": source.get("url", "https://providence.jobs/jobs/"),
                    "source_query": keyword,
                    "notes": "Providence/jobsyn Google Talent direct adapter.",
                    "_jd_text": html_to_text(str(job.get("description") or "")),
                }
            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            if not pagination.get("has_more_pages") or len(jobs) < page_size:
                break
    return list(candidates.values())


def jobsyn_job_url(source: dict[str, Any], job: dict[str, Any]) -> str:
    req_id = str(job.get("reqid") or "").strip()
    guid = str(job.get("guid") or "").strip()
    role = str(job.get("title_exact") or job.get("title") or "job").strip()
    role_slug = re.sub(r"[^A-Za-z0-9]+", "-", role).strip("-")
    title_slug = str(job.get("title_slug") or slugify(role)).strip("/")
    city = str(job.get("city_exact") or "remote").strip()
    city_slug = slugify(city)
    location_slug = slugify(str(job.get("location_exact") or city))
    template = str(source.get("job_url_template") or "").strip()
    if template:
        return normalize_job_url(
            template.format(
                reqid=urllib.parse.quote(req_id),
                guid=urllib.parse.quote(guid),
                role_slug=urllib.parse.quote(role_slug),
                title_slug=urllib.parse.quote(title_slug),
                city_slug=urllib.parse.quote(city_slug),
                location_slug=urllib.parse.quote(location_slug),
            )
        )
    origin = str(source.get("origin") or urllib.parse.urlparse(str(source.get("url") or "")).netloc).strip()
    if origin and guid:
        return normalize_job_url(f"https://{origin}/{city_slug}/{title_slug}/{urllib.parse.quote(guid)}/job/")
    return normalize_job_url(str(source.get("url") or ""))


def discover_jobsyn_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "").strip()
    origin = str(source.get("origin") or urllib.parse.urlparse(str(source.get("url") or "")).netloc).strip()
    if not origin:
        raise ValueError("Jobsyn source requires origin or a source URL with a hostname")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    queries = (
        [""]
        if truthy_source_flag(source.get("search_all"), default=False)
        else [str(item) for item in keywords if str(item).strip()]
    )
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 3))
    api_base = str(source.get("api_url") or "https://prod-search-api.jobsyn.org/api/v1/google-talent/search")
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in queries:
        for page_index in range(max_pages):
            params = {
                "page": str(page_index + 1),
                "num_items": str(page_size),
                "source": str(source.get("search_source") or "solr"),
                "use_solr_filters": "true",
            }
            if keyword:
                params["q"] = keyword
            api_url = api_base + ("&" if "?" in api_base else "?") + urllib.parse.urlencode(params)
            request = urllib.request.Request(
                api_url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json,*/*;q=0.8",
                    "X-Origin": origin,
                    "Referer": f"https://{origin}/jobs/",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace"))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Jobsyn API for {company or origin} / {keyword}: {error}", file=sys.stderr)
                break
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            featured_jobs = (
                data.get("featured_jobs", []) if isinstance(data, dict) else []
            )
            if isinstance(featured_jobs, list):
                jobs = featured_jobs + jobs
            if not jobs:
                break
            for raw_job in jobs:
                job = raw_job.get("job", {}) if isinstance(raw_job, dict) and isinstance(raw_job.get("job"), dict) else raw_job
                if not isinstance(job, dict):
                    continue
                req_id = str(job.get("reqid") or job.get("requisitionId") or "").strip()
                guid = str(job.get("guid") or "").strip()
                url = jobsyn_job_url(source, job)
                if not url:
                    continue
                location = str(job.get("location_exact") or "").strip()
                if not location:
                    locations = job.get("all_locations", []) if isinstance(job.get("all_locations"), list) else []
                    location = ", ".join(str(item) for item in locations if item)
                candidate_key = req_id or guid or url
                existing = candidates.get(candidate_key)
                if existing:
                    existing["location"] = "; ".join(merge_unique(str(existing.get("location") or "").split("; "), [location]))
                    continue
                candidates[candidate_key] = {
                    "company": (
                        str(job.get("company_exact") or company or origin).strip()
                        if truthy_source_flag(
                            source.get("use_job_company"),
                            default=False,
                        )
                        else company or str(job.get("company_exact") or origin).strip()
                    ),
                    "role": str(job.get("title_exact") or job.get("title") or infer_role_from_url(url)).strip(),
                    "url": url,
                    "platform": "jobsyn",
                    "location": location,
                    "job_number": req_id,
                    "external_job_id": req_id or guid or str(job.get("id") or ""),
                    "posted_at": normalize_datetime(job.get("date_new") or job.get("date_added")),
                    "updated_at": normalize_datetime(job.get("date_updated")),
                    "source": source.get("url", f"https://{origin}/jobs/"),
                    "source_query": keyword or "all_jobs",
                    "notes": "Jobsyn direct search API adapter.",
                    "_jd_text": html_to_text(str(job.get("description") or "")),
                }
            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            if not pagination.get("has_more_pages"):
                break
    return list(candidates.values())


def compact_location_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = []
        address = value.get("address") if isinstance(value.get("address"), dict) else {}
        for key in ["city", "region", "state", "stateProvince", "country", "countryName", "name", "addressLocality", "addressRegion", "addressCountry"]:
            if value.get(key):
                parts.append(str(value.get(key)))
            if address.get(key):
                parts.append(str(address.get(key)))
        if truthy_source_flag(value.get("remote"), default=False):
            parts.append("Remote")
        return ", ".join(merge_unique(parts, []))
    if isinstance(value, list):
        return "; ".join(merge_unique([compact_location_text(item) for item in value if item], []))
    return ""


def smartrecruiters_identifier(source: dict[str, Any]) -> str:
    if source.get("company_identifier"):
        return str(source["company_identifier"]).strip()
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if "jobs.smartrecruiters.com" in parsed.netloc.lower() and parts:
        return parts[0]
    if "careers.smartrecruiters.com" in parsed.netloc.lower() and parts:
        return parts[0]
    return slugify(str(source.get("company") or "")).replace("-", "")


def discover_smartrecruiters_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    identifier = smartrecruiters_identifier(source)
    if not identifier:
        return []
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    query_keywords = [""] if truthy_source_flag(source.get("search_all"), default=False) else [
        str(item) for item in keywords if str(item).strip()
    ]
    page_size = min(int(source.get("page_size", 50)), 100)
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in query_keywords:
        for page_index in range(max_pages):
            params = {
                "limit": str(page_size),
                "offset": str(page_index * page_size),
                "destination": "PUBLIC",
            }
            if keyword:
                params["q"] = keyword
            api_url = f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(identifier)}/postings?{urllib.parse.urlencode(params)}"
            try:
                data = fetch_json(api_url)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch SmartRecruiters API for {company}: {error}", file=sys.stderr)
                break
            jobs = data.get("content", []) if isinstance(data, dict) else []
            if not jobs:
                break
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("id") or job.get("uuid") or "").strip()
                role = str(job.get("name") or infer_role_from_url(job_id)).strip()
                if keyword and not keyword_matches_title(keyword, role):
                    continue
                url = normalize_job_url(str(job.get("ref") or job.get("postingUrl") or ""))
                if (not url or "api.smartrecruiters.com" in urllib.parse.urlparse(url).netloc.lower()) and job_id:
                    url = normalize_job_url(f"https://jobs.smartrecruiters.com/{identifier}/{urllib.parse.quote(job_id)}-{slugify(role)}")
                candidates[url] = {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "smartrecruiters",
                    "location": compact_location_text(job.get("location")),
                    "job_number": job_id,
                    "external_job_id": str(job.get("uuid") or job_id),
                    "posted_at": normalize_datetime(job.get("releasedDate") or job.get("publishedDate") or job.get("createdOn")),
                    "updated_at": normalize_datetime(job.get("updatedDate") or job.get("lastUpdated")),
                    "source": source.get("url", f"https://careers.smartrecruiters.com/{identifier}"),
                    "source_query": keyword or "all",
                    "notes": f"SmartRecruiters direct adapter; company_identifier={identifier}",
                }
            if len(jobs) < page_size:
                break
    return list(candidates.values())


def topechelon_api_key(source: dict[str, Any]) -> str:
    if source.get("api_key"):
        return str(source["api_key"]).strip()
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(str(source.get("url") or "")).query
    )
    return str((query.get("board") or [""])[0]).strip()


def topechelon_location_text(job: dict[str, Any]) -> str:
    state = job.get("state")
    if isinstance(state, dict):
        state_text = str(
            state.get("abbreviation")
            or state.get("name")
            or ""
        ).strip()
    else:
        state_text = str(state or "").strip()
    country = job.get("country")
    if isinstance(country, dict):
        country_text = str(
            country.get("abbreviation")
            or country.get("code")
            or country.get("name")
            or ""
        ).strip()
    else:
        country_text = str(country or "").strip()
    if country_text.upper() in {"US", "USA", "UNITED STATES"}:
        country_text = ""
    parts = [
        str(job.get("city") or "").strip(),
        state_text,
        country_text,
    ]
    location = ", ".join(part for part in parts if part)
    remote_option = str(job.get("remote_option") or "").strip().lower()
    if truthy_source_flag(job.get("remote"), default=False) or remote_option in {
        "remote",
        "fully_remote",
    }:
        location = ", ".join(part for part in [location, "Remote"] if part)
    return location


def topechelon_job_url(api_key: str, job_id: str) -> str:
    query = urllib.parse.urlencode({"board": api_key})
    return normalize_job_url(
        "https://bb3jobboard.topechelon.com/"
        f"?{query}#/{urllib.parse.quote(job_id)}/detail"
    )


def discover_topechelon_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    api_key = topechelon_api_key(source)
    if not api_key:
        print(
            f"Could not fetch Top Echelon jobs for {company}: missing public api_key",
            file=sys.stderr,
        )
        return []
    api_base = str(
        source.get("api_base") or "https://bb3api.topechelon.com"
    ).rstrip("/")
    endpoint = f"{api_base}/job_board/job_searches/one_off_search.json"
    max_pages = max(int(source.get("max_pages", 25)), 1)
    timeout = int(source.get("timeout", 25))
    candidates: dict[str, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        api_url = f"{endpoint}?{urllib.parse.urlencode({'page': page})}"
        try:
            data = fetch_json_with_headers(
                api_url,
                {"Authorization": f"Apikey {api_key}"},
                timeout=timeout,
            )
        except Exception as error:  # noqa: BLE001
            print(
                f"Could not fetch Top Echelon jobs for {company}: {error}",
                file=sys.stderr,
            )
            break
        results = data.get("results") or [] if isinstance(data, dict) else []
        if not isinstance(results, list) or not results:
            break
        for job in results:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("id") or "").strip()
            role = str(
                job.get("position_title")
                or job.get("title")
                or f"Job {job_id}"
            ).strip()
            if not job_id or not role:
                continue
            location = topechelon_location_text(job)
            location_pattern = str(
                source.get("location_include_regex") or ""
            ).strip()
            if location_pattern and not re.search(
                location_pattern,
                location,
                flags=re.I,
            ):
                continue
            posted_at = normalize_datetime(
                job.get("posted_date") or job.get("published_date")
            )
            url = topechelon_job_url(api_key, job_id)
            description = html_to_text(str(job.get("description") or ""))
            candidates[job_id] = {
                "company": company,
                "role": role,
                "url": url,
                "platform": "topechelon",
                "location": location,
                "job_number": str(
                    job.get("external_id")
                    or job.get("job_number")
                    or ""
                ).strip(),
                "external_job_id": job_id,
                "posted_at": posted_at,
                "updated_at": normalize_datetime(job.get("updated_at")),
                "source": str(source.get("url") or "").strip(),
                "source_query": "all_open_postings",
                "freshness_source": (
                    "top_echelon_posted_date" if posted_at else "unknown"
                ),
                "notes": (
                    "Top Echelon public job board API adapter; "
                    "official posting date and complete description."
                ),
                "_jd_text": "\n\n".join(
                    part for part in [role, location, description] if part
                ),
            }
        pagination = data.get("pagination") or {}
        total_pages = int(
            pagination.get("total_pages") or page
        ) if isinstance(pagination, dict) else page
        if page >= total_pages:
            break
    return list(candidates.values())


def ripplehire_base_url(source: dict[str, Any]) -> str:
    base_url = str(source.get("url") or "").strip() or "https://usource.ripplehire.com/candidate/"
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme:
        base_url = f"https://{base_url}"
        parsed = urllib.parse.urlparse(base_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/candidate"):
        if "/candidate/" in path:
            path = path.split("/candidate/", 1)[0].rstrip("/") + "/candidate"
        elif not path.endswith("/candidate"):
            path = path.rstrip("/") + "/candidate"
    return urllib.parse.urlunparse(parsed._replace(path=path + "/", params="", query="", fragment=""))


def ripplehire_source_name(source: dict[str, Any]) -> str:
    return str(source.get("ripplehire_source") or source.get("ats_source") or "CAREERSITE").strip() or "CAREERSITE"


def ripplehire_job_url(base_url: str, token: str, source_name: str, job_seq: str) -> str:
    query = urllib.parse.urlencode({"token": token, "source": source_name})
    return f"{base_url}?{query}#detail/job/{urllib.parse.quote(job_seq)}"


def ripplehire_posted_at(value: Any) -> str:
    if isinstance(value, str):
        value = value.replace("-", " ")
    return normalize_datetime(value)


def ripplehire_detail(source: dict[str, Any], job_seq: str) -> dict[str, Any] | None:
    base_url = ripplehire_base_url(source)
    token = str(source.get("token") or "").strip()
    source_name = ripplehire_source_name(source)
    if not token or not job_seq:
        return None
    params = {
        "token": token,
        "jobSeq": job_seq,
        "source": source_name,
        "lang": str(source.get("lang") or "en"),
    }
    detail_url = urllib.parse.urljoin(base_url, "candidatejobdetail") + "?" + urllib.parse.urlencode(params)
    data = fetch_json(detail_url)
    if isinstance(data, dict):
        job = data.get("jobVO") or data.get("jobVo") or data.get("job")
        if isinstance(job, dict):
            return job
    return data if isinstance(data, dict) else None


def ripplehire_job_text(job: dict[str, Any]) -> str:
    parts = [
        str(job.get("jobTitle") or ""),
        compact_location_text(job.get("locations") or job.get("jobLocation")),
        str(job.get("jobReqExp") or ""),
        str(job.get("jobTypeCustom3") or ""),
    ]
    if job.get("jobMinExp") not in (None, "") or job.get("jobMaxExp") not in (None, ""):
        parts.append(f"Experience: {job.get('jobMinExp') or ''}-{job.get('jobMaxExp') or ''} years")
    parts.extend([html_to_text(str(job.get("jobDesc") or "")), html_to_text(str(job.get("jobSkills") or ""))])
    return "\n\n".join(part for part in parts if str(part).strip())


def discover_ripplehire_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    base_url = ripplehire_base_url(source)
    token = str(source.get("token") or "").strip()
    if not token:
        print(f"Could not fetch RippleHire API for {company}: missing token", file=sys.stderr)
        return []
    source_name = ripplehire_source_name(source)
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for page_index in range(max_pages):
            search_params = {
                "token": token,
                "source": source_name,
                "search": keyword,
                "page": str(page_index),
            }
            payload = {
                "careerSiteUrlParams": json.dumps(search_params, separators=(",", ":")),
                "lang": str(source.get("lang") or "en"),
            }
            api_url = urllib.parse.urljoin(base_url, "candidatejobsearch")
            try:
                data = fetch_json_form_post(api_url, payload)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch RippleHire API for {company}: {error}", file=sys.stderr)
                break
            jobs = data.get("jobVoList", []) if isinstance(data, dict) else []
            if not jobs:
                break
            for summary in jobs:
                if not isinstance(summary, dict):
                    continue
                job_seq = str(summary.get("jobSeq") or summary.get("jobId") or "").strip()
                role = str(summary.get("jobTitle") or infer_role_from_url(job_seq)).strip()
                if not job_seq or not role or not keyword_matches_title(keyword, role):
                    continue
                try:
                    detail = ripplehire_detail(source, job_seq) or {}
                except Exception as error:  # noqa: BLE001
                    print(f"Could not fetch RippleHire detail for {company} job {job_seq}: {error}", file=sys.stderr)
                    detail = {}
                job = {**summary, **detail}
                url = ripplehire_job_url(base_url, token, source_name, job_seq)
                location = compact_location_text(job.get("locations") or job.get("jobLocation"))
                candidates[url] = {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "ripplehire",
                    "location": location,
                    "job_number": str(job.get("jobCode") or job_seq),
                    "external_job_id": job_seq,
                    "posted_at": ripplehire_posted_at(
                        job.get("jobPostingDate") or job.get("careerSiteDate") or job.get("openDate") or job.get("createDttm")
                    ),
                    "updated_at": ripplehire_posted_at(job.get("updatedDate") or job.get("modifiedDttm")),
                    "source": source.get("url", base_url),
                    "source_query": keyword,
                    "notes": f"RippleHire/USource adapter; source={source_name}",
                    "_jd_text": ripplehire_job_text(job),
                }
            if len(jobs) < int(source.get("page_size", 10)):
                break
    return list(candidates.values())


def parse_json_ld_jobs(raw: str, source_url: str, fallback_company: str = "Unknown Company") -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r'<script[^>]+type=["\']application/(?:ld\+json|ld&#x2B;json)["\'][^>]*>(.*?)</script>', raw, flags=re.I | re.S):
        payload = match.group(1).strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            try:
                data = json.loads(html.unescape(payload))
            except json.JSONDecodeError:
                continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph") if isinstance(item.get("@graph"), list) else [item]
            for job in graph:
                if not isinstance(job, dict) or str(job.get("@type", "")).lower() != "jobposting":
                    continue
                url = normalize_job_url(str(job.get("url") or source_url))
                org = job.get("hiringOrganization", {})
                company = org.get("name") if isinstance(org, dict) else fallback_company
                candidates[url] = {
                    "company": str(company or fallback_company),
                    "role": str(job.get("title") or infer_role_from_url(url)).strip(),
                    "url": url,
                    "platform": detect_platform(url),
                    "location": compact_location_text(job.get("jobLocation")),
                    "job_number": str(job.get("identifier", {}).get("value") if isinstance(job.get("identifier"), dict) else job.get("identifier") or ""),
                    "external_job_id": str(job.get("identifier", {}).get("value") if isinstance(job.get("identifier"), dict) else job.get("identifier") or ""),
                    "posted_at": normalize_datetime(job.get("datePosted")),
                    "updated_at": normalize_datetime(job.get("validThrough")),
                    "source": source_url,
                    "notes": "Parsed from JobPosting JSON-LD.",
                    "_jd_text": html_to_text(str(job.get("description") or "")),
                }
    return list(candidates.values())


def ttcportals_candidate_matches_source(
    source: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    required_locations = source.get("required_locations") or []
    if isinstance(required_locations, str):
        required_locations = [required_locations]
    required_locations = [
        str(item).strip().lower()
        for item in required_locations
        if str(item).strip()
    ]
    if not required_locations:
        return True
    location = str(candidate.get("location") or "").strip().lower()
    return any(required_location in location for required_location in required_locations)


def discover_ttcportals_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Read Cloudflare-protected TalentTech Portals listings through Playwright.

    Detail pages are intentionally not fetched: those pages commonly trigger an
    additional bot challenge. These candidates therefore use first_seen rather
    than claiming an official posted date.
    """

    helper_path = ROOT / "scripts" / "ttcportals_collect.js"
    timeout = max(10, int(source.get("browser_subprocess_timeout", 40)))
    try:
        completed = subprocess.run(
            ["node", str(helper_path)],
            cwd=ROOT,
            input=json.dumps(source, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Node.js is required for the TalentTech Portals adapter.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"TalentTech Portals browser helper exceeded {timeout}s.") from error

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "browser helper failed").strip()
        raise RuntimeError(
            f"TalentTech Portals browser helper failed: {message}. "
            "Run `npm install` in job-search/ and retry."
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"TalentTech Portals browser helper returned invalid JSON: {error}") from error

    company = str(source.get("company") or "Unknown Company")
    location_fallbacks = source.get("listing_location_fallbacks") or {}
    if not isinstance(location_fallbacks, dict):
        location_fallbacks = {}
    candidates: dict[str, dict[str, Any]] = {}
    for item in payload.get("jobs", []):
        if not isinstance(item, dict):
            continue
        url = normalize_job_url(str(item.get("url") or ""))
        role = str(item.get("role") or "").strip()
        if not url or not role:
            continue
        item_source_url = str(item.get("source_url") or "")
        location = str(item.get("location") or "").strip()
        if not location:
            location = str(
                location_fallbacks.get(item_source_url)
                or location_fallbacks.get(normalize_job_url(item_source_url))
                or source.get("default_location")
                or ""
            ).strip()
        candidate = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "ttcportals",
            "location": location,
            "job_number": str(item.get("external_job_id") or ""),
            "external_job_id": str(item.get("external_job_id") or ""),
            "posted_at": "",
            "updated_at": "",
            "source": str(source.get("url") or item.get("source_url") or ""),
            "source_query": item_source_url,
            "freshness_source": "first_seen",
            "notes": (
                "TalentTech Portals browser listing adapter; official detail dates are "
                "Cloudflare-protected, so freshness uses first_seen."
                + (" Listing marked NEW by the career site." if item.get("is_new") else "")
            ),
            "_jd_text": "",
        }
        if ttcportals_candidate_matches_source(source, candidate):
            candidates[url] = candidate
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    return list(candidates.values())


def discover_browser_static_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a configured public job list through an isolated Chrome process."""

    helper_path = ROOT / "scripts" / "browser_static_collect.js"
    timeout = max(10, int(source.get("browser_subprocess_timeout", 45)))
    try:
        completed = subprocess.run(
            ["node", str(helper_path)],
            cwd=ROOT,
            input=json.dumps(source, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Node.js is required for the browser_static adapter.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"browser_static helper exceeded {timeout}s.") from error

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "browser helper failed").strip()
        raise RuntimeError(
            f"browser_static helper failed: {message}. "
            "Run `npm install` in job-search/ and retry."
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"browser_static helper returned invalid JSON: {error}") from error

    company = str(source.get("company") or "Unknown Company")
    candidates: dict[str, dict[str, Any]] = {}
    for item in payload.get("jobs", []):
        if not isinstance(item, dict):
            continue
        url = normalize_job_url(str(item.get("url") or ""))
        role = str(item.get("role") or "").strip()
        if not url or not role:
            continue
        posted_at = normalize_datetime(item.get("posted_at"))
        detected_platform = detect_platform(url)
        external_job_id = str(item.get("external_job_id") or "").strip()
        candidates[url] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": detected_platform if detected_platform != "custom" else "browser_static",
            "location": str(item.get("location") or source.get("default_location") or "").strip(),
            "job_number": external_job_id,
            "external_job_id": external_job_id,
            "posted_at": posted_at,
            "updated_at": "",
            "source": str(source.get("url") or item.get("source_url") or ""),
            "source_query": str(source.get("source_query") or ""),
            "freshness_source": "browser_static_posted_date" if posted_at else "first_seen",
            "notes": (
                "Configured browser-rendered public listing adapter. "
                + (
                    "The page exposed an official posted date."
                    if posted_at
                    else "The page did not expose a reliable posted date; freshness uses first_seen."
                )
            ),
            "_jd_text": str(item.get("description") or "").strip(),
        }
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    return list(candidates.values())


def keyword_matches_title(keyword: str, title: str) -> bool:
    keyword_terms = [term for term in re.split(r"[^a-z0-9]+", keyword.lower()) if len(term) > 1]
    title_terms = set(term for term in re.split(r"[^a-z0-9]+", title.lower()) if term)
    if not keyword_terms:
        return True
    if len(keyword_terms) == 1:
        return keyword_terms[0] in title_terms
    return all(term in title_terms for term in keyword_terms)


def discover_salesforce_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Salesforce")
    base_url = str(source.get("url") or "https://careers.salesforce.com/en/jobs/").split("?")[0].rstrip("/")
    keywords = source.get("keywords") or ["Software Engineer", "Backend Engineer", "AI Engineer", "QA Engineer", "SDET"]
    if isinstance(keywords, str):
        keywords = [keywords]
    page_size = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 3))
    detail_limit = int(source.get("max_detail_pages", 40))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for page_index in range(1, max_pages + 1):
            params = {"search": keyword, "pagesize": str(page_size)}
            if page_index > 1:
                params["page"] = str(page_index)
            search_url = f"{base_url}/?{urllib.parse.urlencode(params)}"
            try:
                raw = fetch_url(search_url)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Salesforce jobs page for {keyword}: {error}", file=sys.stderr)
                break
            blocks = re.findall(r'<div class=["\']card card-job["\']>(.*?)</div>\s*</div>', raw, flags=re.I | re.S)
            if not blocks:
                break
            for block in blocks:
                link_match = re.search(r'<h3[^>]*class=["\']card-title["\'][^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block, flags=re.I | re.S)
                if not link_match:
                    continue
                role = html_to_text(link_match.group(2))
                if not keyword_matches_title(keyword, role):
                    continue
                url = normalize_job_url(urllib.parse.urljoin(search_url, html.unescape(link_match.group(1))))
                location_items = re.findall(r'<li[^>]*class=["\']list-inline-item["\'][^>]*>(.*?)</li>', block, flags=re.I | re.S)
                location = "; ".join(merge_unique([html_to_text(item) for item in location_items if html_to_text(item)], []))
                candidates[url] = {
                    "company": company,
                    "role": role or infer_role_from_url(url),
                    "url": url,
                    "platform": "salesforce_jobs",
                    "location": location,
                    "posted_at": "",
                    "updated_at": "",
                    "source": source.get("url", base_url),
                    "source_query": keyword,
                    "notes": "Salesforce careers card adapter; detail page JSON-LD may add posted_at.",
                }
            if f"page={page_index + 1}" not in raw:
                break
    for candidate in list(candidates.values())[:detail_limit]:
        try:
            detail_raw = fetch_url(candidate["url"])
        except Exception:
            continue
        details = parse_json_ld_jobs(detail_raw, candidate["url"], str(company))
        if details:
            detail = details[0]
            for key in ["role", "location", "job_number", "external_job_id", "posted_at", "updated_at"]:
                if detail.get(key):
                    candidate[key] = detail[key]
        apply_match = re.search(r'<a[^>]+id=["\']js-apply-external["\'][^>]+href=["\']([^"\']+)["\']', detail_raw, flags=re.I | re.S)
        if apply_match:
            candidate["apply_url"] = normalize_job_url(html.unescape(apply_match.group(1)))
    return list(candidates.values())


def discover_icims_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    base_url = str(source.get("url") or source.get("base_url") or "").rstrip("/")
    if not base_url:
        return []
    if "/jobs/search" in base_url:
        search_base = base_url
    else:
        search_base = urllib.parse.urljoin(base_url + "/", "jobs/search")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    search_params = source.get("search_params") if isinstance(source.get("search_params"), dict) else {}
    query_keywords = [""] if truthy_source_flag(source.get("search_all"), default=False) else [
        str(item) for item in keywords if str(item).strip()
    ]
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in query_keywords:
        for page_index in range(max_pages):
            params = {
                **{str(key): str(value) for key, value in search_params.items() if str(key).strip()},
                "ss": "1",
                "pr": str(page_index),
            }
            if keyword:
                params["searchKeyword"] = keyword
            search_url = f"{search_base}?{urllib.parse.urlencode(params)}"
            try:
                raw = fetch_url(search_url)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch iCIMS search for {company}: {error}", file=sys.stderr)
                break
            for candidate in parse_json_ld_jobs(raw, search_url, str(company)):
                candidate["company"] = str(company)
                candidate["platform"] = "icims"
                candidate["source_query"] = keyword
                candidates[candidate["url"]] = candidate
            for href, label in re.findall(r'href=["\']([^"\']*/jobs/\d+[^"\']*)["\'][^>]*>(.*?)</a>', raw, flags=re.I | re.S):
                url = normalize_job_url(urllib.parse.urljoin(search_url, html.unescape(href)))
                role = html_to_text(label) or infer_role_from_url(url)
                candidates.setdefault(url, {
                    "company": company,
                    "role": role,
                    "url": url,
                    "platform": "icims",
                    "location": "",
                    "posted_at": "",
                    "updated_at": "",
                    "source": source.get("url", base_url),
                    "source_query": keyword,
                    "notes": "iCIMS HTML search adapter; posted_at may require detail page JSON-LD.",
                })
            if f"pr={page_index + 1}" not in raw and "iCIMS_Paginator" not in raw:
                break
    detail_limit = int(source.get("max_detail_pages", 0))
    if detail_limit > 0:
        for candidate in list(candidates.values())[:detail_limit]:
            if candidate.get("posted_at"):
                continue
            try:
                detail_raw = fetch_url(candidate["url"])
                if "icimsFrame.src" in detail_raw and "application/ld+json" not in detail_raw:
                    iframe_match = re.search(r"icimsFrame\.src\s*=\s*['\"]([^'\"]+)['\"]", detail_raw)
                    if iframe_match:
                        detail_raw = fetch_url(html.unescape(iframe_match.group(1)).replace("\\/", "/"))
            except Exception:
                continue
            detail_jobs = parse_json_ld_jobs(detail_raw, candidate["url"], str(company))
            if not detail_jobs:
                continue
            detail = detail_jobs[0]
            for key in ["role", "location", "job_number", "external_job_id", "posted_at", "updated_at"]:
                if detail.get(key):
                    candidate[key] = detail[key]
            candidate["notes"] = "iCIMS search adapter enriched from detail page JSON-LD."
    return list(candidates.values())


def oracle_cx_api_url(source: dict[str, Any]) -> str:
    if source.get("api_url"):
        return str(source["api_url"]).rstrip("/")
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    return f"{parsed.scheme}://{parsed.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


def oracle_site_number(source: dict[str, Any]) -> str:
    if source.get("site_number"):
        return str(source["site_number"])
    match = re.search(r"/sites/([^/?#]+)", str(source.get("url") or ""), flags=re.I)
    return match.group(1) if match else "CX_1"


def oracle_cx_job_url(source: dict[str, Any], req_id: str) -> str:
    source_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(source_url)
    if not parsed.scheme or not parsed.netloc or not req_id:
        return source_url
    base_path = re.sub(
        r"/(?:jobs|requisitions)(?:/.*)?$",
        "",
        parsed.path.rstrip("/"),
        flags=re.I,
    )
    detail_path = f"{base_path}/job/{urllib.parse.quote(req_id)}/"
    return normalize_job_url(
        urllib.parse.urlunparse(
            parsed._replace(path=detail_path, query="", fragment="")
        )
    )


def discover_oracle_cx_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    endpoint = oracle_cx_api_url(source)
    site_number = oracle_site_number(source)
    limit = int(source.get("page_size", 25))
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for page_index in range(max_pages):
            find_params = {
                "siteNumber": site_number,
                "limit": str(limit),
                "offset": str(page_index * limit),
                "keyword": keyword,
            }
            finder = "findReqs;" + ",".join(f"{key}={value}" for key, value in find_params.items())
            params = {
                "onlyData": "true",
                "expand": (
                    "requisitionList.workLocation,requisitionList.otherWorkLocations,"
                    "requisitionList.secondaryLocations,flexFieldsFacet.values,"
                    "requisitionList.requisitionFlexFields"
                ),
                "finder": finder,
            }
            api_url = f"{endpoint}?{urllib.parse.urlencode(params)}"
            try:
                data = fetch_json(api_url)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Oracle CX API for {company}: {error}", file=sys.stderr)
                break
            items = data.get("items", []) if isinstance(data, dict) else []
            jobs = []
            if items and isinstance(items[0], dict) and isinstance(items[0].get("requisitionList"), list):
                jobs = items[0].get("requisitionList", [])
            elif isinstance(items, list):
                jobs = items
            if not jobs:
                break
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                req_id = str(job.get("RequisitionId") or job.get("Id") or job.get("requisitionId") or "").strip()
                title = str(job.get("Title") or job.get("ExternalTitle") or job.get("title") or infer_role_from_url(req_id)).strip()
                detail_url = normalize_job_url(str(job.get("ExternalApplyURL") or job.get("ApplyUrl") or ""))
                if not detail_url:
                    detail_url = oracle_cx_job_url(source, req_id)
                location = compact_location_text(job.get("PrimaryLocation") or job.get("Location") or job.get("locations"))
                candidates[detail_url] = {
                    "company": company,
                    "role": title,
                    "url": detail_url,
                    "platform": "oracle_cx",
                    "location": location,
                    "job_number": str(job.get("RequisitionNumber") or job.get("ReqNumber") or req_id),
                    "external_job_id": req_id,
                    "posted_at": normalize_datetime(job.get("PostedDate") or job.get("CreationDate") or job.get("postedDate")),
                    "updated_at": normalize_datetime(job.get("LastUpdateDate") or job.get("UpdatedDate")),
                    "source": source.get("url", endpoint),
                    "source_query": keyword,
                    "notes": f"Oracle Candidate Experience adapter; site_number={site_number}",
                }
            if len(jobs) < limit:
                break
    return list(candidates.values())


def discover_workgr8_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(board_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    endpoint = str(source.get("api_url") or f"{origin}/graphql")
    page_size = max(1, min(int(source.get("page_size", 100)), 100))
    max_pages = max(1, int(source.get("max_pages", 5)))
    candidates: dict[str, dict[str, Any]] = {}

    for page_index in range(max_pages):
        payload = {
            "operationName": "searchJobs",
            "variables": {
                "query": "",
                "first": page_size,
                "start": page_index * page_size,
                "filters": {
                    "location": [],
                    "workplaceType": [],
                    "jobCategory": [],
                    "positionType": [],
                },
            },
            "extensions": {"trustedDocument": {"id": "search-jobs"}},
        }
        data = fetch_json_post(endpoint, payload)
        results = (
            data.get("data", {})
            .get("searchJobs", {})
            .get("results", {})
            if isinstance(data, dict)
            else {}
        )
        jobs = results.get("nodes", []) if isinstance(results, dict) else []
        if not isinstance(jobs, list) or not jobs:
            break
        for job in jobs:
            if not isinstance(job, dict) or str(job.get("status") or "OPEN").upper() != "OPEN":
                continue
            job_id = str(job.get("key") or job.get("number") or job.get("id") or "").strip()
            title = str(job.get("title") or "").strip()
            if not job_id or not title:
                continue
            structured: dict[str, Any] = {}
            if job.get("structuredDataJSON"):
                with contextlib.suppress(TypeError, ValueError, json.JSONDecodeError):
                    parsed_structured = json.loads(str(job["structuredDataJSON"]))
                    if isinstance(parsed_structured, dict):
                        structured = parsed_structured
            detail_url = normalize_job_url(
                f"{origin}/jobs/{urllib.parse.quote(job_id)}/{slugify(title)}"
            )
            location = compact_location_text(job.get("primaryPlace"))
            if not location:
                location = compact_location_text(structured.get("jobLocation"))
            posted_at = normalize_datetime(
                job.get("postedOn") or structured.get("datePosted")
            )
            candidates[detail_url] = {
                "company": company,
                "role": title,
                "url": detail_url,
                "platform": "workgr8",
                "location": location,
                "job_number": str(job.get("number") or job_id),
                "external_job_id": job_id,
                "posted_at": posted_at,
                "updated_at": "",
                "source": board_url,
                "source_query": str(
                    (job.get("positionType") or {}).get("name")
                    if isinstance(job.get("positionType"), dict)
                    else ""
                ),
                "freshness_source": "workgr8_posted_on" if posted_at else "unknown",
                "notes": "WorkGR8 public GraphQL job-board adapter.",
                "_jd_text": html_to_text(
                    str(job.get("descriptionHTML") or structured.get("description") or "")
                ),
            }
        total = int(results.get("totalCount") or 0) if isinstance(results, dict) else 0
        if len(jobs) < page_size or (total and len(candidates) >= total):
            break
    return list(candidates.values())


def talentreef_client_id(source: dict[str, Any]) -> str:
    configured = str(source.get("client_id") or "").strip()
    if configured:
        return configured
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return ""
    try:
        raw = fetch_url(source_url, timeout=20)
    except Exception:  # noqa: BLE001
        return ""
    match = re.search(
        r"(?:marketing-assets|images)\.jobappnetwork\.com/(\d+)(?:/|[\"'])",
        raw,
        flags=re.I,
    )
    return match.group(1) if match else ""


def source_from_talentreef_page(
    company: str,
    url: str,
    raw: str = "",
) -> dict[str, Any] | None:
    if not raw:
        with contextlib.suppress(Exception):
            raw = fetch_url(url, timeout=20)
    match = re.search(
        r"(?:marketing-assets|images)\.jobappnetwork\.com/(\d+)(?:/|[\"'])",
        raw,
        flags=re.I,
    )
    client_id = match.group(1) if match else ""
    if not client_id:
        return None
    parsed = urllib.parse.urlparse(url)
    public_origin = (
        f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else url
    )
    return {
        "company": company or parsed.netloc,
        "platform": "talentreef",
        "url": public_origin.rstrip("/") + "/",
        "client_id": client_id,
        "search_all": True,
        "page_size": 100,
        "max_pages": 3,
    }


def discover_talentreef_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company").strip()
    board_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(board_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("TalentReef source requires a public careers URL")
    client_id = talentreef_client_id(source)
    if not client_id:
        raise ValueError("TalentReef source requires client_id or a detectable client asset")

    public_origin = f"{parsed.scheme}://{parsed.netloc}"
    api_base = str(
        source.get("api_base")
        or "https://prod-kong.internal.talentreef.com/apply"
    ).rstrip("/")
    elastic_index = str(source.get("elastic_index") or "search-en-us").strip()
    endpoint = f"{api_base}/proxy-es/{elastic_index}/posting/_search"
    page_size = max(1, min(int(source.get("page_size", 100)), 100))
    max_pages = max(1, int(source.get("max_pages", 3)))
    state_filters = [
        str(item).strip()
        for item in source.get("state_filters", [])
        if str(item).strip()
    ]
    filters: list[dict[str, Any]] = [
        {"terms": {"clientId.raw": [client_id]}},
        {"terms": {"internalOrExternal": ["externalOnly"]}},
    ]
    if state_filters:
        filters.append({"terms": {"stateOrProvinceFull.raw": state_filters}})
    headers = {
        "Origin": public_origin,
        "Referer": board_url,
    }
    fields = [
        "positionType",
        "category",
        "socialRecruitingAttribute1",
        "description",
        "address",
        "jobId",
        "clientId",
        "clientName",
        "brandId",
        "brand",
        "location",
        "internalOrExternal",
        "url",
        "postingUuid",
        "createdDate",
        "endDate",
        "department",
        "positionId",
    ]
    candidates: dict[str, dict[str, Any]] = {}

    for page_index in range(max_pages):
        payload = {
            "from": page_index * page_size,
            "size": page_size,
            "_source": fields,
            "query": {"bool": {"filter": filters}},
        }
        data = fetch_json_post_with_headers(
            endpoint,
            payload,
            headers,
            timeout=int(source.get("request_timeout", 20)),
        )
        hits_container = data.get("hits", {}) if isinstance(data, dict) else {}
        hits = (
            hits_container.get("hits", [])
            if isinstance(hits_container, dict)
            else []
        )
        if not isinstance(hits, list) or not hits:
            break
        for hit in hits:
            job = (
                hit.get("_source", {})
                if isinstance(hit, dict)
                and isinstance(hit.get("_source"), dict)
                else {}
            )
            job_id = str(job.get("jobId") or "").strip()
            posting_uuid = str(job.get("postingUuid") or "").strip()
            posting_key = posting_uuid or job_id
            title = str(job.get("positionType") or "").strip()
            if not posting_key or not title:
                continue
            template = str(source.get("job_url_template") or "").strip()
            if template:
                detail_url = normalize_job_url(
                    template.format(
                        client_id=urllib.parse.quote(client_id),
                        job_id=urllib.parse.quote(job_id),
                        posting_id=urllib.parse.quote(posting_key),
                    )
                )
            else:
                detail_url = normalize_job_url(
                    f"{public_origin}/clients/{urllib.parse.quote(client_id)}/"
                    f"posting/{urllib.parse.quote(posting_key)}/en"
                )
            address = (
                job.get("address", {})
                if isinstance(job.get("address"), dict)
                else {}
            )
            location_parts = [
                str(address.get("city") or "").strip(),
                str(
                    address.get("stateOrProvince")
                    or address.get("stateProvince")
                    or ""
                ).strip(),
            ]
            country = str(address.get("country") or "").strip()
            if country and country.upper() not in {"US", "USA", "UNITED STATES"}:
                location_parts.append(country)
            location = ", ".join(
                item for item in merge_unique(location_parts, []) if item
            )
            if "remote" in title.lower():
                location = "; ".join(
                    item
                    for item in merge_unique(["Remote", location], [])
                    if item
                )
            posted_at = normalize_datetime(job.get("createdDate"))
            candidates[posting_key] = {
                "company": (
                    str(job.get("clientName") or company).strip()
                    if truthy_source_flag(
                        source.get("use_job_company"),
                        default=False,
                    )
                    else company
                ),
                "role": title,
                "url": detail_url,
                "platform": "talentreef",
                "location": location,
                "job_number": job_id,
                "external_job_id": posting_key,
                "posted_at": posted_at,
                "updated_at": "",
                "source": board_url,
                "source_query": str(job.get("category") or "all_jobs"),
                "freshness_source": (
                    "talentreef_created_date" if posted_at else "unknown"
                ),
                "notes": "TalentReef public Elasticsearch proxy adapter.",
                "_jd_text": html_to_text(str(job.get("description") or "")),
            }
        total_value = (
            hits_container.get("total", 0)
            if isinstance(hits_container, dict)
            else 0
        )
        if isinstance(total_value, dict):
            total_value = total_value.get("value", 0)
        total = int(total_value or 0)
        if len(hits) < page_size or (total and len(candidates) >= total):
            break
    return list(candidates.values())


def clearcompany_short_name(source: dict[str, Any]) -> str:
    configured = str(source.get("api_short_name") or "").strip()
    if configured:
        return configured
    host = urllib.parse.urlparse(str(source.get("url") or "")).netloc.lower()
    return host.split(".", 1)[0]


def discover_clearcompany_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    base_url = str(source.get("url") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    endpoint = str(source.get("api_url") or f"{origin}/api/v1/careers/jobs")
    short_name = clearcompany_short_name(source)
    data = fetch_json_with_headers(endpoint, {"API-ShortName": short_name})
    jobs = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    default_location = str(source.get("default_location") or "").strip()
    candidates = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("Id") or job.get("id") or "").strip()
        title = str(job.get("PositionTitle") or job.get("Title") or job.get("title") or "").strip()
        if not job_id or not title:
            continue
        apply_url = normalize_job_url(str(job.get("ApplyUrl") or job.get("applyUrl") or ""))
        detail_url = normalize_job_url(f"{origin}/careers/jobs/{urllib.parse.quote(job_id)}")
        city = str(job.get("City") or "").strip()
        state = str(job.get("State") or "").strip()
        location = ", ".join(item for item in [city, state] if item) or default_location
        description = html_to_text(str(job.get("Description") or ""))
        candidates.append(
            {
                "company": company,
                "role": title,
                "url": detail_url,
                "apply_url": apply_url,
                "platform": "clearcompany",
                "location": location,
                "job_number": job_id,
                "external_job_id": job_id,
                "posted_at": normalize_datetime(
                    job.get("OpenDate")
                    or job.get("DatePosted")
                    or job.get("PublishedDate")
                    or job.get("CreatedDate")
                ),
                "updated_at": normalize_datetime(job.get("LastModifiedDate") or job.get("UpdatedDate")),
                "source": source.get("url", endpoint),
                "source_query": str(job.get("DepartmentName") or job.get("OfficeName") or ""),
                "notes": f"ClearCompany public careers API; API-ShortName={short_name}.",
                "_jd_text": description,
                "freshness_source": "clearcompany_open_date" if job.get("OpenDate") else "",
            }
        )
    return candidates


def parse_paylocity_page_data(raw: str) -> dict[str, Any]:
    marker = re.search(r"window\.pageData\s*=\s*", raw)
    if not marker:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[marker.end() :])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def discover_paylocity_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").rstrip("/")
    feed_url = str(source.get("feed_url") or "").strip()
    if feed_url:
        feed_data = fetch_json(feed_url)
        jobs = feed_data.get("jobs", []) if isinstance(feed_data, dict) else []
    else:
        raw = fetch_url(board_url)
        page_data = parse_paylocity_page_data(raw)
        jobs = page_data.get("Jobs", []) if isinstance(page_data, dict) else []
    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("JobId") or job.get("Id") or job.get("jobId") or job.get("id") or "").strip()
        title = str(job.get("JobTitle") or job.get("Title") or job.get("title") or "").strip()
        display_url = str(job.get("displayUrl") or job.get("DisplayUrl") or "").strip()
        if not title or (not job_id and not display_url):
            continue
        detail_url = (
            normalize_job_url(display_url)
            if display_url
            else normalize_job_url(
                urllib.parse.urljoin(board_url + "/", f"/Recruiting/Jobs/Details/{urllib.parse.quote(job_id)}")
            )
        )
        job_location = (
            job.get("JobLocation")
            if isinstance(job.get("JobLocation"), dict)
            else job.get("jobLocation")
            if isinstance(job.get("jobLocation"), dict)
            else {}
        )
        city = str(job_location.get("City") or job_location.get("city") or "").strip()
        state = str(job_location.get("State") or job_location.get("state") or "").strip()
        country = str(job_location.get("Country") or job_location.get("country") or "").strip()
        location = str(
            job.get("LocationName")
            or job.get("locationName")
            or job_location.get("locationDisplayName")
            or job_location.get("LocationDisplayName")
            or ""
        ).strip()
        if not location:
            location = ", ".join(item for item in [city, state, country] if item)
        published_date = (
            job.get("PublishedDate")
            or job.get("publishedDate")
            or job.get("datePosted")
            or job.get("DatePosted")
        )
        department = (
            job.get("HiringDepartment")
            or job.get("hiringDepartment")
            or job.get("Department")
            or job.get("department")
            or ""
        )
        candidates[detail_url] = {
            "company": company,
            "role": title,
            "url": detail_url,
            "platform": "paylocity",
            "location": location,
            "job_number": job_id or detail_url,
            "external_job_id": job_id or detail_url,
            "posted_at": normalize_datetime(published_date),
            "updated_at": "",
            "source": board_url or feed_url,
            "source_query": str(department),
            "notes": "Paylocity public careers feed data." if feed_url else "Paylocity public careers page data.",
            "_jd_text": html_to_text(str(job.get("Description") or job.get("description") or "")),
            "freshness_source": "paylocity_published_date" if published_date else "",
        }

    detail_limit = int(source.get("max_detail_pages", len(candidates)))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    selected = list(candidates.values())[:detail_limit]

    def enrich(candidate: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        try:
            detail_raw = fetch_url(candidate["url"])
        except Exception:
            return candidate["url"], None
        parsed = parse_json_ld_jobs(detail_raw, candidate["url"], company)
        if not parsed:
            return candidate["url"], None
        return candidate["url"], parsed[0]

    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            for url, detail in executor.map(enrich, selected):
                if not detail:
                    continue
                candidate = candidates[url]
                for key in ["role", "location", "posted_at", "updated_at"]:
                    if detail.get(key):
                        candidate[key] = detail[key]
                if detail.get("_jd_text"):
                    candidate["_jd_text"] = detail["_jd_text"]
                if detail.get("posted_at"):
                    candidate["freshness_source"] = "paylocity_json_ld_date_posted"
                candidate["notes"] = "Paylocity listing enriched from official detail-page JSON-LD."
    return [
        candidate
        for candidate in candidates.values()
        if source_location_allowed(
            source,
            str(candidate.get("location") or ""),
        )
    ]


def dynamicsats_form_id(source: dict[str, Any]) -> str:
    configured = str(source.get("form_id") or "").strip()
    if configured:
        return configured
    path = urllib.parse.urlparse(str(source.get("url") or "")).path
    match = re.search(r"/JobListing/(?:Details/)?([0-9a-f-]{36})(?:/|$)", path, flags=re.I)
    return match.group(1) if match else ""


def fetch_dynamicsats_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    board_url = str(source.get("url") or "").strip()
    form_id = dynamicsats_form_id(source)
    if not board_url or not form_id:
        return []
    parsed = urllib.parse.urlparse(board_url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    endpoint = urllib.parse.urljoin(origin, "/JobListing/WebForm/JobListing_Read")
    page_size = max(1, int(source.get("page_size", 100)))
    max_pages = max(1, int(source.get("max_pages", 5)))
    timeout = int(source.get("timeout", 25))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 job-search-agent")]
    with opener.open(board_url, timeout=timeout):
        pass

    jobs: list[dict[str, Any]] = []
    for page_index in range(max_pages):
        payload = urllib.parse.urlencode(
            {
                "formId": form_id,
                "page": page_index + 1,
                "pageSize": page_size,
                "skip": page_index * page_size,
                "take": page_size,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": board_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        with opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        page_jobs = data.get("Data", []) if isinstance(data, dict) else []
        if not isinstance(page_jobs, list) or not page_jobs:
            break
        jobs.extend(job for job in page_jobs if isinstance(job, dict))
        total = int(data.get("Total") or 0) if isinstance(data, dict) else 0
        if len(page_jobs) < page_size or (total and len(jobs) >= total):
            break
    return jobs


def parse_dynamicsats_detail(raw: str) -> str:
    match = re.search(
        r'<div class="col-sm-12 col-md-8 col-md-pull-4">\s*(.*?)\s*</div>\s*</div>\s*</div>',
        raw,
        flags=re.I | re.S,
    )
    return html_to_text(match.group(1)) if match else ""


def discover_dynamicsats_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    candidates: dict[str, dict[str, Any]] = {}
    for job in fetch_dynamicsats_jobs(source):
        job_id = str(job.get("Id") or "").strip()
        title = str(job.get("dcrs_jobtitle") or "").strip()
        url = normalize_job_url(str(job.get("JobUrl") or ""))
        if not job_id or not title or not url:
            continue
        candidates[url] = {
            "company": company,
            "role": title,
            "url": url,
            "platform": "dynamicsats",
            "location": str(job.get("dcrs_location") or "").strip(),
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": "",
            "updated_at": "",
            "source": board_url,
            "source_query": str(job.get("dcrs_category") or "").strip(),
            "notes": "DynamicsATS public listing API; no reliable posting date exposed.",
            "_jd_text": html_to_text(str(job.get("dcrs_jobdescription") or "")),
            "freshness_source": "first_seen",
        }

    detail_limit = max(0, int(source.get("max_detail_pages", len(candidates))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    selected = list(candidates.values())[:detail_limit]

    def enrich(candidate: dict[str, Any]) -> tuple[str, str]:
        try:
            return candidate["url"], parse_dynamicsats_detail(fetch_url(candidate["url"]))
        except Exception:
            return candidate["url"], ""

    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            for url, description in executor.map(enrich, selected):
                if description:
                    candidates[url]["_jd_text"] = description
    return list(candidates.values())


def hanford_detail_field(raw: str, field_id: str) -> str:
    match = re.search(
        rf'id=["\'][^"\']*{re.escape(field_id)}["\'][^>]*>(.*?)</span>',
        raw,
        flags=re.I | re.S,
    )
    return html_to_text(match.group(1)) if match else ""


def discover_hanford_bms_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    listing_url = str(source.get("url") or "").strip()
    if not listing_url:
        return []
    raw = fetch_url(listing_url)
    candidates: dict[str, dict[str, Any]] = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", raw, flags=re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)
        if len(cells) < 8:
            continue
        link_match = re.search(r'href=["\']([^"\']*JobDetail\.aspx[^"\']*)["\'][^>]*>(.*?)</a>', cells[1], flags=re.I | re.S)
        if not link_match:
            continue
        detail_url = normalize_job_url(
            urllib.parse.urljoin(listing_url, html.unescape(link_match.group(1)))
        )
        job_number = html_to_text(cells[2])
        posted_raw = html_to_text(cells[7])
        candidates[detail_url] = {
            "company": company,
            "role": html_to_text(link_match.group(2)) or infer_role_from_url(detail_url),
            "url": detail_url,
            "platform": "hanford_bms",
            "location": str(source.get("default_location") or "Richland, WA"),
            "job_number": job_number,
            "external_job_id": job_number,
            "posted_at": normalize_datetime(posted_raw),
            "updated_at": "",
            "source": listing_url,
            "source_query": html_to_text(cells[0]),
            "notes": "Official Hanford contractor external jobs portal.",
            "_jd_text": html_to_text(row),
            "freshness_source": "hanford_posted_date" if posted_raw else "unknown",
        }

    detail_limit = max(0, int(source.get("max_detail_pages", len(candidates))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    selected = list(candidates.values())[:detail_limit]

    def enrich(candidate: dict[str, Any]) -> tuple[str, str, str, str]:
        try:
            detail_raw = fetch_url(candidate["url"])
        except Exception:
            return candidate["url"], "", "", ""
        return (
            candidate["url"],
            hanford_detail_field(detail_raw, "lblCityState"),
            hanford_detail_field(detail_raw, "lblOPEN_DT"),
            html_to_text(detail_raw),
        )

    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            for url, location, posted_raw, description in executor.map(enrich, selected):
                candidate = candidates[url]
                if location:
                    candidate["location"] = location
                if posted_raw:
                    candidate["posted_at"] = normalize_datetime(posted_raw)
                    candidate["freshness_source"] = "hanford_posted_date"
                if description:
                    candidate["_jd_text"] = description
    return list(candidates.values())


def applicantpro_domain_id(source: dict[str, Any], board_html: str = "") -> str:
    configured = str(source.get("domain_id") or "").strip()
    if configured:
        return configured
    for pattern in (
        r"\bdomainId\s*:\s*[\"']?(\d+)",
        r"[\"']domain_id[\"']\s*:\s*[\"'](\d+)",
    ):
        match = re.search(pattern, board_html)
        if match:
            return match.group(1)
    return ""


def discover_applicantpro_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    if not board_url:
        return []
    parsed_board = urllib.parse.urlparse(board_url)
    origin = f"{parsed_board.scheme or 'https'}://{parsed_board.netloc}"
    domain_id = str(source.get("domain_id") or "").strip()
    if not domain_id:
        domain_id = applicantpro_domain_id(source, fetch_url(board_url))
    if not domain_id:
        raise RuntimeError(f"ApplicantPro domain_id not found for {company}")

    api_url = f"{origin}/core/jobs/{urllib.parse.quote(domain_id)}"
    query = urllib.parse.urlencode({"getParams": "{}"})
    data = fetch_json(f"{api_url}?{query}", timeout=int(source.get("timeout", 25)))
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"ApplicantPro jobs API failed for {company}")
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []

    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "").strip()
        title = str(job.get("title") or "").strip()
        if not job_id or not title:
            continue
        detail_url = normalize_job_url(
            str(job.get("jobUrl") or f"{origin}/jobs/{urllib.parse.quote(job_id)}")
        )
        city = str(job.get("city") or "").strip()
        state = str(job.get("abbreviation") or job.get("stateName") or "").strip()
        location = str(job.get("jobLocation") or "").strip()
        if not location:
            location = ", ".join(item for item in [city, state] if item)
        department = str(
            job.get("classification")
            or job.get("orgTitle")
            or job.get("parentTitle")
            or ""
        ).strip()
        posted_at = normalize_datetime(job.get("startDateRef"))
        candidates[detail_url] = {
            "company": company,
            "role": title,
            "url": detail_url,
            "platform": "applicantpro",
            "location": location,
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": posted_at,
            "updated_at": "",
            "source": board_url,
            "source_query": department,
            "freshness_source": "applicantpro_start_date" if posted_at else "",
            "notes": f"ApplicantPro public jobs API; domain_id={domain_id}.",
            "_jd_text": "",
        }

    detail_limit = max(0, int(source.get("max_detail_pages", len(candidates))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    selected = sorted(
        candidates.values(),
        key=lambda candidate: (
            0 if unclassified_technical_title_relevant(candidate) else 1,
            str(candidate.get("role") or "").lower(),
        ),
    )[:detail_limit]

    def enrich(candidate: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        try:
            detail_raw = fetch_url(candidate["url"])
        except Exception:
            return candidate["url"], None
        parsed = parse_json_ld_jobs(detail_raw, candidate["url"], company)
        return candidate["url"], parsed[0] if parsed else None

    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            for url, detail in executor.map(enrich, selected):
                if not detail:
                    continue
                candidate = candidates[url]
                for key in ["role", "location", "posted_at", "updated_at"]:
                    if detail.get(key):
                        candidate[key] = detail[key]
                if detail.get("_jd_text"):
                    candidate["_jd_text"] = detail["_jd_text"]
                if detail.get("posted_at"):
                    candidate["freshness_source"] = "applicantpro_json_ld_date_posted"
                candidate["notes"] = (
                    f"ApplicantPro public jobs API and detail-page JSON-LD; domain_id={domain_id}."
                )
    return list(candidates.values())


def dayforce_board_parts(source: dict[str, Any]) -> tuple[str, str]:
    client_namespace = str(source.get("client_namespace") or "").strip()
    job_board_code = str(source.get("job_board_code") or "").strip()
    parts = [
        urllib.parse.unquote(part)
        for part in urllib.parse.urlparse(str(source.get("url") or "")).path.split("/")
        if part
    ]
    if not client_namespace and len(parts) >= 2 and parts[-1].lower() == "candidateportal":
        client_namespace = parts[-2]
    if not client_namespace and len(parts) >= 3 and parts[0].lower() == "candidateportal":
        client_namespace = parts[-1]
    if not client_namespace and len(parts) >= 3 and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        client_namespace = parts[1]
        if not job_board_code:
            job_board_code = parts[2]
    if not job_board_code:
        job_board_code = "CANDIDATEPORTAL"
    return client_namespace, job_board_code


def discover_dayforce_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    client_namespace, job_board_code = dayforce_board_parts(source)
    if not client_namespace:
        return []

    api_origin = str(source.get("api_origin") or "https://jobs.dayforcehcm.com").rstrip("/")
    culture_code = str(source.get("culture_code") or "en-US")
    page_size = 25
    max_pages = max(1, int(source.get("max_pages", 10)))
    timeout = int(source.get("timeout", 25))
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    common_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json,*/*;q=0.8",
    }
    csrf_data = fetch_json_with_opener(
        opener,
        f"{api_origin}/api/auth/csrf",
        common_headers,
        timeout=timeout,
    )
    csrf_token = str(csrf_data.get("csrfToken") or "") if isinstance(csrf_data, dict) else ""
    if not csrf_token:
        raise RuntimeError(f"Dayforce CSRF token unavailable for {company}")

    search_url = f"{api_origin}/api/geo/{urllib.parse.quote(client_namespace)}/jobposting/search"
    post_headers = {
        **common_headers,
        "Content-Type": "application/json",
        "X-CSRF-TOKEN": csrf_token,
    }
    candidates: dict[str, dict[str, Any]] = {}
    for page_index in range(max_pages):
        payload = {
            "clientNamespace": client_namespace,
            "jobBoardCode": job_board_code,
            "cultureCode": culture_code,
            "paginationStart": page_index * page_size,
        }
        data = fetch_json_post_with_opener(
            opener,
            search_url,
            payload,
            post_headers,
            timeout=timeout,
        )
        jobs = data.get("jobPostings", []) if isinstance(data, dict) else []
        if not isinstance(jobs, list) or not jobs:
            break
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("jobPostingId") or "").strip()
            title = str(job.get("jobTitle") or "").strip()
            if not job_id or not title:
                continue
            locations = []
            for posting_location in job.get("postingLocations") or []:
                if not isinstance(posting_location, dict):
                    continue
                formatted = str(posting_location.get("formattedAddress") or "").strip()
                if formatted:
                    locations.append(formatted)
            if truthy_source_flag(job.get("hasVirtualLocation"), default=False):
                locations.insert(0, "Remote")
            location = "; ".join(merge_unique([], locations))
            detail_url = normalize_job_url(
                f"{api_origin}/{urllib.parse.quote(culture_code)}/"
                f"{urllib.parse.quote(client_namespace)}/{urllib.parse.quote(job_board_code)}/"
                f"jobs/{urllib.parse.quote(job_id)}"
            )
            posted_at = normalize_datetime(job.get("postingStartTimestampUTC"))
            candidates[detail_url] = {
                "company": company,
                "role": title,
                "url": detail_url,
                "platform": "dayforce",
                "location": location,
                "job_number": str(job.get("jobReqId") or job_id),
                "external_job_id": job_id,
                "posted_at": posted_at,
                "updated_at": "",
                "source": source.get("url", search_url),
                "source_query": "",
                "freshness_source": "dayforce_posting_start" if posted_at else "",
                "notes": (
                    f"Dayforce public Candidate Portal API; "
                    f"client_namespace={client_namespace}; job_board_code={job_board_code}."
                ),
                "_jd_text": html_to_text(str(job.get("jobDescription") or "")),
            }
        max_count = int(data.get("maxCount") or 0) if isinstance(data, dict) else 0
        if len(jobs) < page_size or (max_count and (page_index + 1) * page_size >= max_count):
            break
    return list(candidates.values())


def adp_board_parts(source: dict[str, Any]) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    query = urllib.parse.parse_qs(parsed.query)
    cid = str(source.get("cid") or (query.get("cid") or [""])[0]).strip()
    career_center_id = str(
        source.get("career_center_id")
        or source.get("cc_id")
        or (query.get("ccId") or query.get("ccid") or ["19000101_000001"])[0]
    ).strip()
    locale = str(source.get("locale") or (query.get("lang") or ["en_US"])[0]).strip()
    return cid, career_center_id, locale


def adp_location_text(job: dict[str, Any]) -> str:
    locations = []
    for location in job.get("requisitionLocations") or []:
        if not isinstance(location, dict):
            continue
        name_code = location.get("nameCode") if isinstance(location.get("nameCode"), dict) else {}
        short_name = str(name_code.get("shortName") or "").strip()
        if short_name:
            locations.append(short_name)
            continue
        address = location.get("address") if isinstance(location.get("address"), dict) else {}
        subdivision = (
            address.get("countrySubdivisionLevel1")
            if isinstance(address.get("countrySubdivisionLevel1"), dict)
            else {}
        )
        locations.append(
            ", ".join(
                item
                for item in [
                    str(address.get("cityName") or "").strip(),
                    str(subdivision.get("codeValue") or "").strip(),
                    str(address.get("postalCode") or "").strip(),
                ]
                if item
            )
        )
    return "; ".join(merge_unique([], [item for item in locations if item]))


def adp_source_query(job: dict[str, Any]) -> str:
    custom_fields = job.get("customFieldGroup") if isinstance(job.get("customFieldGroup"), dict) else {}
    values = []
    for field in custom_fields.get("codeFields") or []:
        if not isinstance(field, dict):
            continue
        name_code = field.get("nameCode") if isinstance(field.get("nameCode"), dict) else {}
        if str(name_code.get("codeValue") or "") in {"JobClass", "HomeDepartment"}:
            value = str(field.get("shortName") or field.get("codeValue") or "").strip()
            if value:
                values.append(value)
    return " / ".join(merge_unique([], values))


def discover_adp_workforce_now_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    cid, career_center_id, locale = adp_board_parts(source)
    if not cid:
        return []
    board_origin = ""
    if board_url:
        parsed_board = urllib.parse.urlparse(board_url)
        if parsed_board.scheme and parsed_board.netloc:
            board_origin = f"{parsed_board.scheme}://{parsed_board.netloc}"
    api_origin = str(source.get("api_origin") or board_origin or "https://workforcenow.adp.com").rstrip("/")
    api_host = urllib.parse.urlparse(api_origin).netloc or "workforcenow.adp.com"
    endpoint = (
        f"{api_origin}/mascsr/default/careercenter/public/events/staffing/"
        "v1/job-requisitions"
    )
    page_size = max(1, int(source.get("page_size", 20)))
    max_pages = max(1, int(source.get("max_pages", 10)))
    timeout = int(source.get("timeout", 25))
    headers = {
        "Accept-Language": locale,
        "locale": locale,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
        "x-forwarded-host": api_host,
    }
    candidates: dict[str, dict[str, Any]] = {}
    detail_ids: dict[str, str] = {}
    for page_index in range(max_pages):
        params = {
            "cid": cid,
            "ccId": career_center_id,
            "lang": locale,
            "locale": locale,
            "$skip": str(page_index * page_size),
            "$top": str(page_size),
            "userQuery": "",
        }
        data = fetch_json_with_headers(
            f"{endpoint}?{urllib.parse.urlencode(params)}",
            headers,
            timeout=timeout,
        )
        jobs = data.get("jobRequisitions", []) if isinstance(data, dict) else []
        if not isinstance(jobs, list) or not jobs:
            break
        for job in jobs:
            if not isinstance(job, dict):
                continue
            item_id = str(job.get("itemID") or "").strip()
            title = str(job.get("requisitionTitle") or "").strip()
            if not item_id or not title:
                continue
            parsed_board = urllib.parse.urlparse(board_url)
            detail_params = dict(urllib.parse.parse_qsl(parsed_board.query, keep_blank_values=True))
            detail_params.update(
                {
                    "cid": cid,
                    "ccId": career_center_id,
                    "lang": locale,
                    "jobId": item_id,
                }
            )
            detail_url = normalize_job_url(
                urllib.parse.urlunparse(parsed_board._replace(query=urllib.parse.urlencode(detail_params)))
            )
            candidates[detail_url] = {
                "company": company,
                "role": title,
                "url": detail_url,
                "platform": "adp_workforce_now",
                "location": adp_location_text(job),
                "job_number": str(job.get("clientRequisitionID") or item_id),
                "external_job_id": item_id,
                "posted_at": normalize_datetime(job.get("postDate")),
                "updated_at": "",
                "source": board_url,
                "source_query": adp_source_query(job),
                "freshness_source": "adp_post_date" if job.get("postDate") else "",
                "notes": (
                    f"ADP Workforce Now public career center API; "
                    f"cid={cid}; ccId={career_center_id}."
                ),
                "_jd_text": html_to_text(str(job.get("requisitionDescription") or "")),
            }
            detail_ids[detail_url] = item_id
        total = int((data.get("meta") or {}).get("totalNumber") or 0) if isinstance(data, dict) else 0
        if len(jobs) < page_size or (total and (page_index + 1) * page_size >= total):
            break

    detail_limit = max(0, int(source.get("max_detail_pages", len(candidates))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    selected = sorted(
        candidates.values(),
        key=lambda candidate: (
            0 if unclassified_technical_title_relevant(candidate) else 1,
            str(candidate.get("role") or "").lower(),
        ),
    )[:detail_limit]

    def enrich(candidate: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        item_id = detail_ids.get(candidate["url"], "")
        params = {
            "cid": cid,
            "ccId": career_center_id,
            "lang": locale,
            "locale": locale,
        }
        try:
            detail = fetch_json_with_headers(
                f"{endpoint}/{urllib.parse.quote(item_id)}?{urllib.parse.urlencode(params)}",
                headers,
                timeout=timeout,
            )
        except Exception:
            return candidate["url"], None
        return candidate["url"], detail if isinstance(detail, dict) else None

    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            for url, detail in executor.map(enrich, selected):
                if not detail:
                    continue
                candidate = candidates[url]
                candidate["role"] = str(detail.get("requisitionTitle") or candidate["role"]).strip()
                candidate["location"] = adp_location_text(detail) or candidate["location"]
                candidate["posted_at"] = normalize_datetime(detail.get("postDate")) or candidate["posted_at"]
                candidate["source_query"] = adp_source_query(detail) or candidate["source_query"]
                candidate["_jd_text"] = html_to_text(
                    str(detail.get("requisitionDescription") or candidate["_jd_text"])
                )
    return list(candidates.values())


def adp_myjobs_domain(source: dict[str, Any]) -> str:
    if source.get("domain"):
        return str(source["domain"]).strip()
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parsed.netloc.lower() == "myjobs.adp.com" and parts else ""


def discover_adp_myjobs_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    domain = adp_myjobs_domain(source)
    if not domain:
        return []
    board_url = str(source.get("url") or f"https://myjobs.adp.com/{domain}").rstrip("/")
    timeout = int(source.get("timeout", 25))
    config_url = f"https://myjobs.adp.com/public/staffing/v1/career-site/{urllib.parse.quote(domain)}"
    config = fetch_json(config_url, timeout=timeout)
    if not isinstance(config, dict):
        return []
    token = str(config.get("myJobsToken") or "").strip()
    if not token:
        return []
    properties = config.get("properties") if isinstance(config.get("properties"), dict) else {}
    api_origin = str(source.get("api_origin") or properties.get("myadpUrl") or "https://my.adp.com").rstrip("/")
    endpoint = (
        f"{api_origin}/myadp_prefix/mycareer/public/staffing/v1/"
        "job-requisitions/apply-custom-filters"
    )
    headers = {
        "MyJobsToken": token,
        "rolecode": "manager",
        "Referer": f"https://myjobs.adp.com/{domain}/",
    }
    selected_fields = ",".join(
        [
            "reqId",
            "jobTitle",
            "publishedJobTitle",
            "type",
            "jobDescription",
            "jobQualifications",
            "workLocations",
            "workLevelCode",
            "clientRequisitionID",
            "postingDate",
            "requisitionLocations",
        ]
    )
    page_size = max(1, int(source.get("page_size", 50)))
    max_pages = max(1, int(source.get("max_pages", 10)))
    candidates: dict[str, dict[str, Any]] = {}
    for page_index in range(max_pages):
        params = {
            "$orderby": "postingDate desc",
            "$select": selected_fields,
            "$top": str(page_size),
            "$skip": str(page_index * page_size),
            "tz": str(source.get("timezone") or "America/Los_Angeles"),
        }
        data = fetch_json_with_headers(
            f"{endpoint}?{urllib.parse.urlencode(params)}",
            headers,
            timeout=timeout,
        )
        jobs = data.get("jobRequisitions", []) if isinstance(data, dict) else []
        if not isinstance(jobs, list) or not jobs:
            break
        for job in jobs:
            if not isinstance(job, dict):
                continue
            requisition_id = str(job.get("reqId") or "").strip()
            title = str(job.get("publishedJobTitle") or job.get("jobTitle") or "").strip()
            if not requisition_id or not title:
                continue
            detail_url = normalize_job_url(
                f"https://myjobs.adp.com/{domain}/cx/job-details?"
                f"{urllib.parse.urlencode({'reqId': requisition_id})}"
            )
            description = "\n\n".join(
                item
                for item in [
                    html_to_text(str(job.get("jobDescription") or "")),
                    html_to_text(str(job.get("jobQualifications") or "")),
                ]
                if item
            )
            candidates[detail_url] = {
                "company": company,
                "role": title,
                "url": detail_url,
                "platform": "adp_myjobs",
                "location": adp_location_text(job),
                "job_number": str(job.get("clientRequisitionID") or requisition_id),
                "external_job_id": requisition_id,
                "posted_at": normalize_datetime(job.get("postingDate")),
                "updated_at": "",
                "source": board_url,
                "source_query": adp_source_query(job),
                "freshness_source": "adp_myjobs_posting_date" if job.get("postingDate") else "",
                "notes": f"ADP MyJobs public API; career_site={domain}.",
                "_jd_text": description,
            }
        total = int(data.get("count") or 0) if isinstance(data, dict) else 0
        if len(jobs) < page_size or (total and (page_index + 1) * page_size >= total):
            break
    return list(candidates.values())


def jubilant_careers_api_base(source: dict[str, Any]) -> str:
    return str(
        source.get("api_base")
        or "https://jubilantcareer.jubl.com/JubilantCareersPortal/rest/Portal/"
    ).rstrip("/") + "/"


def jubilant_careers_posted_at(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for date_format in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            parsed = dt.datetime.strptime(raw, date_format).replace(tzinfo=dt.timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    return normalize_datetime(raw)


def discover_jubilant_careers_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Jubilant")
    source_url = str(source.get("url") or "https://jubilantcareer.jubl.com/explorejobs").strip()
    api_base = jubilant_careers_api_base(source)
    timeout = int(source.get("timeout", 25))
    data = fetch_json(f"{api_base}getAllJobs", timeout=timeout)
    jobs = data.get("jobList", []) if isinstance(data, dict) else []
    if not isinstance(jobs, list):
        return []

    location_keywords = source.get("location_keywords") or []
    if isinstance(location_keywords, str):
        location_keywords = [location_keywords]
    location_keywords = [str(item).strip().lower() for item in location_keywords if str(item).strip()]
    company_contains = str(source.get("company_contains") or "").strip().lower()
    functional_areas = source.get("functional_areas") or []
    if isinstance(functional_areas, str):
        functional_areas = [functional_areas]
    functional_areas = [str(item).strip().lower() for item in functional_areas if str(item).strip()]
    title_keywords = source.get("title_keywords") or []
    if isinstance(title_keywords, str):
        title_keywords = [title_keywords]
    title_keywords = [str(item).strip().lower() for item in title_keywords if str(item).strip()]
    selected_jobs: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("jobId") or "").strip()
        role = str(job.get("jobTitle") or "").strip()
        location = str(job.get("locationDescription") or "").strip()
        listed_company = str(job.get("company") or "").strip()
        if not job_id or not role:
            continue
        if company_contains and company_contains not in listed_company.lower():
            continue
        if location_keywords and not any(keyword in location.lower() for keyword in location_keywords):
            continue
        functional_area = str(job.get("functionalArea") or "").strip().lower()
        area_match = any(area in functional_area for area in functional_areas)
        title_match = any(keyword in role.lower() for keyword in title_keywords)
        if (functional_areas or title_keywords) and not (area_match or title_match):
            continue
        selected_jobs.append(job)

    max_details = max(0, int(source.get("max_detail_pages", len(selected_jobs))))
    selected_jobs = selected_jobs[:max_details]
    detail_workers = max(1, int(source.get("detail_workers", 10)))

    def load_detail(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        job_id = str(job.get("jobId") or "").strip()
        try:
            detail = fetch_json(f"{api_base}getJobDetails/{urllib.parse.quote(job_id)}", timeout=timeout)
        except Exception:
            return job, None
        return job, detail if isinstance(detail, dict) else None

    candidates: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
        for job, detail in executor.map(load_detail, selected_jobs):
            if not detail or (detail.get("status") and str(detail.get("status")) != "010"):
                continue
            job_id = str(detail.get("jobOpeningId") or job.get("jobId") or "").strip()
            role = str(detail.get("jobtitle") or job.get("jobTitle") or "").strip()
            location = str(
                detail.get("locationdescr") or job.get("locationDescription") or ""
            ).strip()
            listed_company = str(
                detail.get("companydescr") or job.get("company") or company
            ).strip()
            description = html_to_text(str(detail.get("jobdescr") or ""))
            posted_at = jubilant_careers_posted_at(detail.get("jobpostingdate"))
            candidates.append(
                {
                    "company": company,
                    "role": role,
                    "url": normalize_job_url(
                        f"https://jubilantcareer.jubl.com/jobprofile/{urllib.parse.quote(job_id)}/home"
                    ),
                    "platform": "jubilant_careers",
                    "location": location,
                    "job_number": job_id,
                    "external_job_id": job_id,
                    "posted_at": posted_at,
                    "updated_at": "",
                    "source": source_url,
                    "source_query": str(
                        detail.get("funct") or job.get("functionalArea") or listed_company
                    ).strip(),
                    "freshness_source": (
                        "jubilant_official_posting_date" if posted_at else "unknown"
                    ),
                    "notes": (
                        "Jubilant public careers API; "
                        f"listed company={listed_company or company}."
                    ),
                    "_jd_text": "\n\n".join(
                        part for part in [role, location, description] if part
                    ),
                }
            )
    return candidates


def discover_boa_careers_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Bank of America")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    max_pages = int(source.get("max_pages", 1))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in [str(item).strip() for item in keywords if str(item).strip()]:
        slug = urllib.parse.quote(keyword.lower().replace(" ", "-"))
        for page_index in range(max_pages):
            page_suffix = f"/{page_index + 1}" if page_index else ""
            url = f"https://careers.bankofamerica.com/en-us/job-search/q-{slug}{page_suffix}"
            try:
                raw = fetch_url(url, timeout=20)
            except urllib.error.HTTPError as error:
                if error.code == 404 and page_index > 0:
                    break
                print(f"Could not fetch Bank of America careers page for {keyword}: {error}", file=sys.stderr)
                break
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Bank of America careers page for {keyword}: {error}", file=sys.stderr)
                break
            matches = list(re.finditer(r'href=["\'](?P<href>[^"\']*/job-detail/[^"\']+)["\'][^>]*>(?P<title>.*?)</a>', raw, flags=re.I | re.S))
            if not matches:
                break
            for index, match in enumerate(matches):
                next_start = matches[index + 1].start() if index + 1 < len(matches) else min(len(raw), match.end() + 2500)
                block = html_to_text(raw[match.start():next_start])
                detail_url = normalize_job_url(urllib.parse.urljoin(url, html.unescape(match.group("href"))))
                title = html_to_text(match.group("title")).strip() or infer_role_from_url(detail_url)
                location_match = re.search(r"\bLocation\s+(.+?)(?:\s+Date\s+Posted|\s+Travel:|\s+Shift:|$)", block, flags=re.I | re.S)
                date_match = re.search(r"\bDate\s+Posted\s+(\d{1,2}/\d{1,2}/\d{4})", block, flags=re.I)
                location = re.sub(r"\s+", " ", location_match.group(1)).strip() if location_match else ""
                posted_at = ""
                if date_match:
                    with contextlib.suppress(ValueError):
                        posted_at = dt.datetime.strptime(date_match.group(1), "%m/%d/%Y").replace(tzinfo=dt.timezone.utc).isoformat()
                candidates[detail_url] = {
                    "company": company,
                    "role": title,
                    "url": detail_url,
                    "platform": "boa_careers",
                    "location": location,
                    "posted_at": posted_at,
                    "updated_at": "",
                    "source": source.get("url", "https://careers.bankofamerica.com/en-us/job-search"),
                    "source_query": keyword,
                    "notes": "Bank of America careers page adapter.",
                }
            if len(matches) < int(source.get("page_size", 20)):
                break
    return list(candidates.values())


def jobvite_company_id(source: dict[str, Any]) -> str:
    if source.get("company_id"):
        return str(source["company_id"]).strip()
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if parts:
        return parts[0]
    host_parts = parsed.netloc.split(".")
    return host_parts[0] if host_parts and host_parts[0] != "jobs" else slugify(str(source.get("company") or ""))


def jobvite_candidate_matches_source(
    source: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    required_locations = source.get("required_locations") or []
    if isinstance(required_locations, str):
        required_locations = [required_locations]
    required_locations = [
        str(item).strip().lower()
        for item in required_locations
        if str(item).strip()
    ]
    if not required_locations:
        return True
    location = str(candidate.get("location") or "").strip().lower()
    return any(required_location in location for required_location in required_locations)


def discover_jobvite_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    base_url = str(source.get("url") or "").rstrip("/")
    if not base_url:
        account = jobvite_company_id(source)
        base_url = f"https://jobs.jobvite.com/{account}"
    listing_url = str(source.get("listing_url") or "").strip()
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    queries = (
        [""]
        if truthy_source_flag(source.get("scan_all_jobs"), default=False)
        else [str(item) for item in keywords if str(item).strip()]
    )
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in queries:
        params = {"nl": "1", "fr": "false"}
        if keyword:
            params["q"] = keyword
        if listing_url:
            search_url = listing_url
            if keyword:
                separator = "&" if "?" in search_url else "?"
                search_url = f"{search_url}{separator}{urllib.parse.urlencode(params)}"
        else:
            search_url = f"{base_url}/jobs?{urllib.parse.urlencode(params)}"
        try:
            raw = fetch_url(search_url)
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch Jobvite page for {company}: {error}", file=sys.stderr)
            continue
        for candidate in parse_json_ld_jobs(raw, search_url, str(company)):
            candidate["company"] = str(company)
            candidate["platform"] = "jobvite"
            candidate["source_query"] = keyword
            candidates[candidate["url"]] = candidate
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", raw, flags=re.I | re.S):
            link_match = re.search(
                r'href=["\']([^"\']*/job/[^"\']+)["\'][^>]*>(.*?)</a>',
                row,
                flags=re.I | re.S,
            )
            if not link_match:
                continue
            url = normalize_job_url(urllib.parse.urljoin(search_url, html.unescape(link_match.group(1))))
            location_match = re.search(
                r'<td\b[^>]*class=["\'][^"\']*jv-job-list-location[^"\']*["\'][^>]*>(.*?)</td>',
                row,
                flags=re.I | re.S,
            )
            candidate = candidates.setdefault(
                url,
                {
                    "company": company,
                    "role": html_to_text(link_match.group(2)) or infer_role_from_url(url),
                    "url": url,
                    "platform": "jobvite",
                    "location": "",
                    "posted_at": "",
                    "updated_at": "",
                    "source": source.get("url", base_url),
                    "source_query": keyword or "all_jobs",
                    "freshness_source": "unknown",
                    "notes": "Jobvite official listing; detail JSON-LD can supply the official posted date.",
                    "_jd_text": "",
                },
            )
            if location_match and not candidate.get("location"):
                candidate["location"] = html_to_text(location_match.group(1))
        for href, label in re.findall(r'href=["\']([^"\']*/job/[^"\']+)["\'][^>]*>(.*?)</a>', raw, flags=re.I | re.S):
            url = normalize_job_url(urllib.parse.urljoin(search_url, html.unescape(href)))
            candidates.setdefault(url, {
                "company": company,
                "role": html_to_text(label) or infer_role_from_url(url),
                "url": url,
                "platform": "jobvite",
                "location": "",
                "posted_at": "",
                "updated_at": "",
                "source": source.get("url", base_url),
                "source_query": keyword or "all_jobs",
                "freshness_source": "unknown",
                "notes": "Jobvite HTML adapter; posted_at may require detail page JSON-LD.",
                "_jd_text": "",
            })

    candidates = {
        url: candidate
        for url, candidate in candidates.items()
        if jobvite_candidate_matches_source(source, candidate)
    }
    detail_limit = min(len(candidates), int(source.get("fetch_detail_limit", 20)))
    detail_workers = max(1, int(source.get("detail_workers", 6)))
    detail_candidates = [
        candidate
        for candidate in candidates.values()
        if unclassified_technical_title_relevant(candidate)
    ][:detail_limit]

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            raw = fetch_url(str(candidate["url"]), timeout=int(source.get("detail_timeout", 20)))
        except Exception:
            return
        details = parse_json_ld_jobs(raw, str(candidate["url"]), str(company))
        if not details:
            return
        detail = details[0]
        for key in ["role", "location", "job_number", "external_job_id", "posted_at", "updated_at", "_jd_text"]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["platform"] = "jobvite"
        candidate["freshness_source"] = "jobvite_json_ld_date_posted" if candidate.get("posted_at") else "unknown"
        candidate["notes"] = "Jobvite official listing enriched from detail-page JobPosting JSON-LD."

    with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
        list(executor.map(enrich, detail_candidates))
    return list(candidates.values())


def appone_meta_value(raw: str, itemprop: str) -> str:
    for tag in re.findall(r"<meta\b[^>]*>", raw, flags=re.I | re.S):
        prop_match = re.search(
            r'\bitemprop\s*=\s*["\']([^"\']+)["\']',
            tag,
            flags=re.I,
        )
        if not prop_match or prop_match.group(1).lower() != itemprop.lower():
            continue
        content_match = re.search(
            r'\bcontent\s*=\s*["\'](.*?)["\']',
            tag,
            flags=re.I | re.S,
        )
        if content_match:
            return html_to_text(html.unescape(content_match.group(1)))
    return ""


def discover_appone_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    listing_url = str(source.get("listing_url") or source_url).strip()
    if not listing_url:
        return []
    raw = fetch_url(listing_url, timeout=int(source.get("timeout", 25)))
    links: dict[str, dict[str, Any]] = {}
    for href, label in re.findall(
        r'href=["\']([^"\']*MainInfoReq\.asp\?[^"\']*\bR_ID=\d+[^"\']*)["\'][^>]*>(.*?)</a>',
        raw,
        flags=re.I | re.S,
    ):
        url = normalize_job_url(
            urllib.parse.urljoin(listing_url, html.unescape(href))
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        job_id = str((query.get("R_ID") or query.get("r_id") or [""])[0]).strip()
        label_text = html_to_text(label)
        role = re.sub(
            r"\s+-\s+[^-]+(?:,\s*[^-]+)?\s+-\s+Job\s*$",
            "",
            label_text,
            flags=re.I,
        ).strip()
        links[url] = {
            "company": company,
            "role": role or infer_role_from_url(url),
            "url": url,
            "platform": "appone",
            "location": "",
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": "",
            "updated_at": "",
            "source": source_url or listing_url,
            "source_query": "all_jobs",
            "freshness_source": "unknown",
            "notes": "AppOne official listing; detail page supplies official job metadata.",
            "_jd_text": "",
        }

    max_details = max(0, int(source.get("max_detail_pages", len(links))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    selected = list(links.values())[:max_details]

    def enrich(candidate: dict[str, Any]) -> tuple[str, dict[str, str]]:
        try:
            detail_raw = fetch_url(
                str(candidate["url"]),
                timeout=int(source.get("detail_timeout", 20)),
            )
        except Exception:
            return str(candidate["url"]), {}
        city = appone_meta_value(detail_raw, "addressLocality")
        region = appone_meta_value(detail_raw, "addressRegion")
        return str(candidate["url"]), {
            "role": appone_meta_value(detail_raw, "title"),
            "location": ", ".join(item for item in [city, region] if item),
            "posted_at": normalize_datetime(
                appone_meta_value(detail_raw, "datePosted")
            ),
            "_jd_text": appone_meta_value(detail_raw, "description"),
        }

    if selected:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=detail_workers
        ) as executor:
            for url, details in executor.map(enrich, selected):
                if not details:
                    continue
                candidate = links[url]
                for key in ["role", "location", "posted_at", "_jd_text"]:
                    if details.get(key):
                        candidate[key] = details[key]
                candidate["freshness_source"] = (
                    "appone_date_posted" if candidate.get("posted_at") else "unknown"
                )
                candidate["notes"] = (
                    "AppOne official listing enriched from detail-page "
                    "JobPosting microdata."
                )
    return list(links.values())


def avature_list_config(raw: str) -> tuple[str, str, str, dict[str, Any]] | None:
    portal_id_match = re.search(
        r'<meta\s+name=["\']avature\.portal\.id["\']\s+content=["\']([^"\']+)["\']',
        raw,
        flags=re.I,
    )
    portal_path_match = re.search(
        r'<meta\s+name=["\']avature\.portal\.urlPath["\']\s+content=["\']([^"\']+)["\']',
        raw,
        flags=re.I,
    )
    portal_lang_match = re.search(
        r'<meta\s+name=["\']avature\.portal\.lang["\']\s+content=["\']([^"\']+)["\']',
        raw,
        flags=re.I,
    )
    if not portal_id_match:
        return None
    for match in re.finditer(
        r"<list\b[^>]*\bdata-props=(?P<quote>[\"'])(?P<props>.*?)(?P=quote)",
        raw,
        flags=re.I | re.S,
    ):
        try:
            props = json.loads(html.unescape(match.group("props")))
        except (TypeError, ValueError):
            continue
        if str(props.get("listType") or "").lower() == "joblist":
            return (
                html.unescape(portal_id_match.group(1)).strip(),
                html.unescape(portal_path_match.group(1)).strip()
                if portal_path_match
                else "",
                html.unescape(portal_lang_match.group(1)).strip()
                if portal_lang_match
                else "",
                props,
            )
    return None


def avature_query_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def avature_field(record: dict[str, Any], code: str) -> str:
    field = (record.get("fields") or {}).get(code)
    if not isinstance(field, dict):
        return ""
    value = field.get("stringValue")
    if value is None:
        value = field.get("jsonValue")
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or ""
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value if str(item).strip())
    return html_to_text(str(value or ""))


def avature_longest_field(
    record: dict[str, Any],
    excluded_codes: set[str],
) -> str:
    values: list[str] = []
    for code in (record.get("fields") or {}):
        if code in excluded_codes:
            continue
        value = avature_field(record, code)
        if len(value) >= 200:
            values.append(value)
    return max(values, key=len, default="")


def discover_avature_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    raw = fetch_url_with_opener(
        opener,
        source_url,
        timeout=int(source.get("timeout", 25)),
    )
    config = avature_list_config(raw)
    if not config:
        raise ValueError(f"Could not find Avature JobList configuration at {source_url}")
    portal_id, portal_path, portal_lang, props = config

    parsed_source = urllib.parse.urlparse(source_url)
    base_url = f"{parsed_source.scheme or 'https'}://{parsed_source.netloc}"
    api_url = urllib.parse.urljoin(base_url, f"/{portal_id}/_portalList")
    page_size = max(1, min(int(source.get("page_size", 50)), 50))
    max_pages = max(1, int(source.get("max_pages", 10)))
    search_queries = [
        str(item).strip()
        for item in source.get("search_queries", [])
        if str(item).strip()
    ] or [""]
    location_pattern = str(source.get("location_include_regex") or "").strip()
    location_regex = (
        re.compile(location_pattern, flags=re.I) if location_pattern else None
    )
    role_field = str(source.get("role_field") or "name")
    posted_field = str(source.get("posted_field") or "postedDate")
    requisition_field = str(source.get("requisition_field") or "req")
    description_field = str(source.get("description_field") or "")
    location_fields = [
        str(item)
        for item in source.get("location_fields", [])
        if str(item).strip()
    ]
    department_field = str(source.get("department_field") or "")
    candidates: dict[str, dict[str, Any]] = {}

    shared_keys = [
        "uuid",
        "hasToIncludePaginationOptions",
        "allowListSorting",
        "fetchJobIdInPeopleLists",
        "listType",
    ]
    structured_keys = [
        "firstColumnLinks",
        "additionalColumnLinks",
        "allowFilteringFromUrlParams",
        "layout",
        "links",
        "dynamicValueConfigs",
        "shouldAddBase64FileFields",
        "searchMode",
        "conditionalLinkConfig",
        "qtvc",
    ]
    for query in search_queries:
        offset = 0
        for _page_index in range(max_pages):
            params = {
                key: avature_query_value(props.get(key))
                for key in shared_keys
            }
            params.update(
                {
                    "offset": str(offset),
                    "filters": json.dumps(
                        {"search": query},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "sort": "",
                    "sortDirection": "DESC",
                    "recordsPerPage": str(page_size),
                    "token": "",
                }
            )
            params.update(
                {
                    key: avature_query_value(props.get(key))
                    for key in structured_keys
                }
            )
            params["pageUrlParams"] = "{}"
            params["formId"] = str(props.get("formId") or "")
            request_url = f"{api_url}?{urllib.parse.urlencode(params)}"
            payload = fetch_json_with_opener(
                opener,
                request_url,
                {
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json,*/*;q=0.8",
                    "Referer": source_url,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=int(source.get("api_timeout", 25)),
            )
            records = payload.get("results") or []
            links = payload.get("links") or []
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    continue
                external_id = str(record.get("id") or record.get("entityId") or "")
                role = avature_field(record, role_field)
                if not role:
                    continue
                location_values = [
                    avature_field(record, field_code)
                    for field_code in location_fields
                ]
                location = " | ".join(
                    value for value in dict.fromkeys(location_values) if value
                )
                if location_regex and not location_regex.search(location):
                    continue
                detail_url = ""
                if index < len(links) and isinstance(links[index], dict):
                    detail_url = str(
                        links[index].get("detailPage")
                        or links[index].get("jobDetailUrlCode")
                        or ""
                    )
                if not detail_url and external_id:
                    detail_path = "/".join(
                        item
                        for item in [portal_lang, portal_path, "JobDetail"]
                        if item
                    )
                    detail_url = (
                        f"{base_url}/{detail_path}?"
                        f"{urllib.parse.urlencode({'jobId': external_id})}"
                    )
                detail_url = normalize_job_url(detail_url or source_url)
                requisition = avature_field(record, requisition_field)
                description = (
                    avature_field(record, description_field)
                    if description_field
                    else avature_longest_field(
                        record,
                        {
                            role_field,
                            posted_field,
                            requisition_field,
                            *location_fields,
                        },
                    )
                )
                department = (
                    avature_field(record, department_field)
                    if department_field
                    else ""
                )
                posted_at = normalize_datetime(
                    avature_field(record, posted_field)
                )
                candidates[detail_url] = {
                    "company": company,
                    "role": role,
                    "url": detail_url,
                    "platform": "avature",
                    "location": location,
                    "job_number": requisition or external_id,
                    "external_job_id": external_id or requisition,
                    "posted_at": posted_at,
                    "updated_at": "",
                    "source": source_url,
                    "source_query": query or "all_jobs",
                    "freshness_source": (
                        "avature_posted_date" if posted_at else "unknown"
                    ),
                    "notes": (
                        "Avature official public job-list API."
                        + (f" Department: {department}." if department else "")
                    ),
                    "_jd_text": description,
                }
            total = int(payload.get("total") or 0)
            offset += len(records)
            if not records or len(records) < page_size or offset >= total:
                break
    return list(candidates.values())


def parse_pageup_listing(
    raw: str,
    listing_url: str,
    company: str,
    source_url: str,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for match in re.finditer(
        r'<a\b[^>]*class=["\'][^"\']*\bjob-link\b[^"\']*["\'][^>]*'
        r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        raw,
        flags=re.I | re.S,
    ):
        detail_url = normalize_job_url(
            urllib.parse.urljoin(listing_url, html.unescape(match.group(1)))
        )
        role = html_to_text(match.group(2))
        if not role or not detail_url:
            continue
        row_start = raw.rfind("<tr", 0, match.start())
        row_end = raw.find("</tr>", match.end())
        row = raw[row_start : row_end + 5] if row_start >= 0 and row_end >= 0 else ""
        location_match = re.search(
            r'class=["\'][^"\']*\blocation\b[^"\']*["\'][^>]*>(.*?)</',
            row,
            flags=re.I | re.S,
        )
        closing_match = re.search(
            r'class=["\'][^"\']*\bclosing-date\b[^"\']*["\'][^>]*>.*?'
            r'<time\b[^>]*datetime=["\']([^"\']+)',
            row,
            flags=re.I | re.S,
        )
        job_id_match = re.search(r"/job/(\d+)(?:/|$)", detail_url)
        job_id = job_id_match.group(1) if job_id_match else ""
        candidates[detail_url] = {
            "company": company,
            "role": role,
            "url": detail_url,
            "platform": "pageup",
            "location": html_to_text(location_match.group(1)) if location_match else "",
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": "",
            "updated_at": normalize_datetime(
                closing_match.group(1) if closing_match else ""
            ),
            "source": source_url,
            "source_query": "all_open_postings",
            "freshness_source": "unknown",
            "notes": "PageUp official open-postings list; detail page supplies the advertised date.",
            "_jd_text": "",
        }
    return list(candidates.values())


def parse_pageup_detail(raw: str, url: str, company: str) -> dict[str, Any]:
    role_match = re.search(
        r'<div\b[^>]*id=["\']job-content["\'][^>]*>.*?<h2\b[^>]*>(.*?)</h2>',
        raw,
        flags=re.I | re.S,
    )
    location_match = re.search(
        r'class=["\'][^"\']*\blocation\b[^"\']*["\'][^>]*>(.*?)</',
        raw,
        flags=re.I | re.S,
    )
    job_number_match = re.search(
        r'class=["\'][^"\']*\bjob-externalJobNo\b[^"\']*["\'][^>]*>(.*?)</',
        raw,
        flags=re.I | re.S,
    )
    advertised_match = re.search(
        r'class=["\'][^"\']*\bopen-date\b[^"\']*["\'][^>]*>.*?'
        r'<time\b[^>]*datetime=["\']([^"\']+)',
        raw,
        flags=re.I | re.S,
    )
    close_match = re.search(
        r'(?:Applications close|Closing date).*?<time\b[^>]*datetime=["\']([^"\']+)',
        raw,
        flags=re.I | re.S,
    )
    details_match = re.search(
        r'<div\b[^>]*id=["\']job-details["\'][^>]*>(.*?)</div>\s*<p>',
        raw,
        flags=re.I | re.S,
    )
    job_id_match = re.search(r"/job/(\d+)(?:/|$)", url)
    job_id = (
        html_to_text(job_number_match.group(1))
        if job_number_match
        else (job_id_match.group(1) if job_id_match else "")
    )
    posted_at = normalize_datetime(
        advertised_match.group(1) if advertised_match else ""
    )
    return {
        "company": company,
        "role": (
            html_to_text(role_match.group(1))
            if role_match
            else infer_role_from_url(url)
        ),
        "url": normalize_job_url(url),
        "platform": "pageup",
        "location": html_to_text(location_match.group(1)) if location_match else "",
        "job_number": job_id,
        "external_job_id": job_id,
        "posted_at": posted_at,
        "updated_at": normalize_datetime(close_match.group(1) if close_match else ""),
        "source": url,
        "source_query": "all_open_postings",
        "freshness_source": "pageup_advertised_date" if posted_at else "unknown",
        "notes": "PageUp official posting detail.",
        "_jd_text": html_to_text(details_match.group(1) if details_match else raw),
    }


def discover_pageup_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    listing_url = str(source.get("listing_url") or source_url).strip()
    parsed = urllib.parse.urlparse(listing_url)
    if "/listing" not in parsed.path.lower():
        listing_url = urllib.parse.urljoin(listing_url.rstrip("/") + "/", "cw/en-us/listing/")

    max_pages = max(1, int(source.get("max_pages", 5)))
    candidates: dict[str, dict[str, Any]] = {}
    pending = [listing_url]
    seen_pages: set[str] = set()
    while pending and len(seen_pages) < max_pages:
        page_url = pending.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        try:
            raw = fetch_url(page_url, timeout=int(source.get("timeout", 30)))
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch PageUp board for {company}: {error}", file=sys.stderr)
            break
        for candidate in parse_pageup_listing(raw, page_url, company, source_url):
            candidates[candidate["url"]] = candidate
        for anchor in re.finditer(r"<a\b([^>]*)>", raw, flags=re.I | re.S):
            attrs = anchor.group(1)
            if not re.search(
                r'class=["\'][^"\']*\bmore-link\b[^"\']*["\']',
                attrs,
                flags=re.I,
            ):
                continue
            href_match = re.search(r'href=["\']([^"\']+)', attrs, flags=re.I)
            if not href_match:
                continue
            next_url = normalize_job_url(
                urllib.parse.urljoin(page_url, html.unescape(href_match.group(1)))
            )
            if next_url not in seen_pages and next_url not in pending:
                pending.append(next_url)

    detail_limit = min(
        len(candidates),
        max(0, int(source.get("max_detail_pages", len(candidates)))),
    )
    detail_workers = max(1, int(source.get("detail_workers", 8)))

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(
                str(candidate["url"]),
                timeout=int(source.get("detail_timeout", 20)),
            )
        except Exception:
            return
        detail = parse_pageup_detail(detail_raw, str(candidate["url"]), company)
        for key in [
            "role",
            "location",
            "job_number",
            "external_job_id",
            "posted_at",
            "updated_at",
            "_jd_text",
        ]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["freshness_source"] = detail["freshness_source"]
        candidate["notes"] = detail["notes"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
        list(executor.map(enrich, list(candidates.values())[:detail_limit]))
    location_map = source.get("location_map")
    if isinstance(location_map, dict):
        for candidate in candidates.values():
            location = str(candidate.get("location") or "").strip()
            mapped = location_map.get(location)
            if mapped:
                candidate["location"] = str(mapped).strip()
    return list(candidates.values())


def normalize_taleo_location(value: str) -> str:
    locations: list[str] = []
    for item in value.split(","):
        location = item.strip()
        match = re.fullmatch(r"USA-([A-Z]{2})-(.+)", location)
        if match:
            city = match.group(2).replace("-", " ").strip()
            locations.append(f"{city}, {match.group(1)}")
        elif location:
            locations.append(location.replace("USA-", "").replace("-", " "))
    return "; ".join(locations)


def parse_taleo_recent_listing(
    raw: str,
    board_url: str,
    company: str,
) -> list[dict[str, Any]]:
    history_match = re.search(
        r'<input\b[^>]*\bid=["\']initialHistory["\'][^>]*'
        r'\bvalue=["\']([^"\']*)["\']',
        raw,
        flags=re.I | re.S,
    )
    if not history_match:
        return []
    values = [
        urllib.parse.unquote(item)
        for item in html.unescape(history_match.group(1)).split("!|!")
    ]
    parsed_board = urllib.parse.urlparse(board_url)
    detail_path = re.sub(
        r"jobsearch\.ftl$",
        "jobdetail.ftl",
        parsed_board.path,
        flags=re.I,
    )
    detail_base = urllib.parse.urlunparse(
        parsed_board._replace(path=detail_path, query="", fragment="")
    )
    candidates: dict[str, dict[str, Any]] = {}
    for index in range(max(0, len(values) - 18)):
        internal_id = values[index].strip()
        role = html_to_text(values[index + 1])
        if (
            not re.fullmatch(r"\d{5,}", internal_id)
            or values[index + 2].strip() != internal_id
            or html_to_text(values[index + 3]) != role
            or any(values[index + offset].strip() != internal_id for offset in range(4, 9))
        ):
            continue
        requisition = values[index + 17].strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,}", requisition):
            continue
        posted_at = normalize_datetime(values[index + 15])
        close_at = normalize_datetime(values[index + 16])
        detail_url = normalize_job_url(
            f"{detail_base}?{urllib.parse.urlencode({'job': requisition, 'lang': 'en'})}"
        )
        candidates[requisition] = {
            "company": company,
            "role": role,
            "url": detail_url,
            "platform": "taleo",
            "location": normalize_taleo_location(values[index + 9]),
            "job_number": requisition,
            "external_job_id": internal_id,
            "posted_at": posted_at,
            "updated_at": close_at,
            "source": board_url,
            "source_query": "latest_open_postings",
            "freshness_source": "taleo_posting_date" if posted_at else "unknown",
            "notes": (
                "Oracle Taleo official latest-results page. The board exposes only its "
                "newest page in the initial response; daily runs provide incremental coverage."
            ),
            "_jd_text": "",
        }
    return list(candidates.values())


def discover_taleo_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    if not board_url:
        return []
    if "/ats/careers/v2/" in urllib.parse.urlparse(board_url).path.lower():
        return discover_taleo_v2_jobs(source)
    try:
        raw = fetch_url(board_url, timeout=int(source.get("timeout", 30)))
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Taleo board for {company}: {error}", file=sys.stderr)
        return []
    return parse_taleo_recent_listing(raw, board_url, company)


def taleo_v2_search_url(source: dict[str, Any]) -> str:
    board_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(board_url)
    path = re.sub(
        r"/(?:jobSearch|searchResults)$",
        "/searchResults",
        parsed.path,
        flags=re.I,
    )
    return urllib.parse.urlunparse(
        parsed._replace(path=path, fragment="")
    )


def parse_taleo_v2_results(
    raw: str,
    source_url: str,
    company: str,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    pattern = (
        r'<h4\b[^>]*class=["\'][^"\']*oracletaleocwsv2-head-title'
        r'[^"\']*["\'][^>]*>\s*'
        r'<a\b[^>]*href=["\']([^"\']*viewRequisition[^"\']*)'
        r'["\'][^>]*>(.*?)</a>\s*</h4>\s*'
        r'<div\b[^>]*>(.*?)</div>\s*'
        r'<div\b[^>]*>(.*?)</div>'
    )
    for href, role_html, location_html, date_html in re.findall(
        pattern,
        raw,
        flags=re.I | re.S,
    ):
        url = normalize_job_url(
            urllib.parse.urljoin(
                source_url,
                href.replace("&amp;", "&"),
            )
        )
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query
        )
        requisition_id = str(
            (query.get("rid") or [""])[0]
        ).strip()
        role = html_to_text(role_html).strip()
        if not requisition_id or not role:
            continue
        posted_at = normalize_datetime(html_to_text(date_html))
        candidates[requisition_id] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "taleo",
            "location": html_to_text(location_html).strip(),
            "job_number": requisition_id,
            "external_job_id": requisition_id,
            "posted_at": posted_at,
            "updated_at": "",
            "source": source_url,
            "source_query": "all_open_postings",
            "freshness_source": (
                "taleo_v2_posted_date" if posted_at else "first_seen"
            ),
            "notes": (
                "Oracle Taleo Business Edition v2 official careers board."
            ),
            "_jd_text": "",
        }
    return list(candidates.values())


def discover_taleo_v2_jobs(
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    search_url = taleo_v2_search_url(source)
    if not search_url:
        return []
    timeout = int(source.get("timeout", 30))
    max_pages = min(max(int(source.get("max_pages", 25)), 1), 100)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    candidates: dict[str, dict[str, Any]] = {}
    visited: set[str] = set()
    page_url = search_url
    for _page_index in range(max_pages):
        if page_url in visited:
            break
        visited.add(page_url)
        raw = fetch_url_with_opener(
            opener,
            page_url,
            timeout=timeout,
        )
        for candidate in parse_taleo_v2_results(
            raw,
            search_url,
            company,
        ):
            candidates[str(candidate["external_job_id"])] = candidate
        next_match = re.search(
            (
                r'<a\b[^>]*href=["\']([^"\']+)["\']'
                r'[^>]*class=["\'][^"\']*jscroll-next'
            ),
            raw,
            flags=re.I | re.S,
        )
        if not next_match:
            break
        page_url = urllib.parse.urljoin(
            page_url,
            next_match.group(1).replace("&amp;", "&"),
        )

    location_pattern = str(
        source.get("location_include_regex") or ""
    ).strip()
    if location_pattern:
        candidates = {
            key: candidate
            for key, candidate in candidates.items()
            if re.search(
                location_pattern,
                str(candidate.get("location") or ""),
                flags=re.I,
            )
        }

    detail_limit = max(
        0,
        min(len(candidates), int(source.get("max_detail_pages", 40))),
    )
    detail_workers = min(max(int(source.get("detail_workers", 8)), 1), 16)
    selected = list(candidates.values())
    if truthy_source_flag(
        source.get("prioritize_technical_titles"),
        default=True,
    ):
        selected.sort(
            key=lambda candidate: (
                0 if unclassified_technical_title_relevant(candidate) else 1,
                str(candidate.get("role") or "").casefold(),
            )
        )

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            raw = fetch_url(candidate["url"], timeout=timeout)
        except Exception:  # noqa: BLE001
            return
        candidate["_jd_text"] = html_to_text(raw)

    if detail_limit:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=detail_workers
        ) as executor:
            list(executor.map(enrich, selected[:detail_limit]))
    return list(candidates.values())


def parse_peopleadmin_detail(raw: str, url: str, company: str) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for label, value in re.findall(
        r"<tr\b[^>]*>\s*<th\b[^>]*>(.*?)</th>\s*<td\b[^>]*>(.*?)</td>\s*</tr>",
        raw,
        flags=re.I | re.S,
    ):
        key = re.sub(r"[^a-z0-9]+", " ", html_to_text(label).lower()).strip()
        if key and key not in fields:
            fields[key] = html_to_text(value)
    heading = re.search(r"<h2\b[^>]*>(.*?)</h2>", raw, flags=re.I | re.S)
    heading_role = html_to_text(heading.group(1)) if heading else ""
    role = (
        fields.get("working title")
        or fields.get("position title")
        or fields.get("job title")
        or fields.get("title")
        or heading_role
        or infer_role_from_url(url)
    )
    posted_at = normalize_datetime(
        fields.get("posting date")
        or fields.get("open date")
        or fields.get("date posted")
    )
    close_date = normalize_datetime(
        fields.get("close date")
        or fields.get("closing date")
    )
    return {
        "company": company,
        "role": role,
        "url": normalize_job_url(url),
        "platform": "peopleadmin",
        "location": (
            fields.get("location")
            or fields.get("campus")
            or fields.get("work location")
            or ""
        ),
        "job_number": (
            fields.get("posting number")
            or fields.get("position number")
            or urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
        ),
        "external_job_id": urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1],
        "posted_at": posted_at,
        "updated_at": close_date,
        "source": url,
        "source_query": "all_open_postings",
        "freshness_source": "peopleadmin_posting_date" if posted_at else "unknown",
        "notes": "PeopleAdmin official posting detail.",
        "_jd_text": html_to_text(raw),
    }


def discover_peopleadmin_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    if not board_url:
        return []
    if "/postings/search" not in urllib.parse.urlparse(board_url).path:
        board_url = urllib.parse.urljoin(board_url.rstrip("/") + "/", "postings/search")
    try:
        raw = fetch_url(board_url, timeout=int(source.get("timeout", 30)))
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch PeopleAdmin board for {company}: {error}", file=sys.stderr)
        return []

    candidates: dict[str, dict[str, Any]] = {}
    for match in re.finditer(
        r'<a\b[^>]*href=["\']([^"\']*/postings/(\d+))["\'][^>]*>(.*?)</a>',
        raw,
        flags=re.I | re.S,
    ):
        role = html_to_text(match.group(3))
        if not role or role.lower() in {"view details", "apply for this job"}:
            continue
        url = normalize_job_url(urllib.parse.urljoin(board_url, html.unescape(match.group(1))))
        candidates.setdefault(
            url,
            {
                "company": company,
                "role": role,
                "url": url,
                "platform": "peopleadmin",
                "location": "",
                "job_number": match.group(2),
                "external_job_id": match.group(2),
                "posted_at": "",
                "updated_at": "",
                "source": source.get("url", board_url),
                "source_query": "all_open_postings",
                "freshness_source": "unknown",
                "notes": "PeopleAdmin official open-postings list; detail page supplies official dates.",
                "_jd_text": "",
            },
        )

    detail_limit = min(len(candidates), int(source.get("max_detail_pages", 100)))
    detail_workers = max(1, int(source.get("detail_workers", 8)))

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(
                str(candidate["url"]),
                timeout=int(source.get("detail_timeout", 20)),
            )
        except Exception:
            return
        detail = parse_peopleadmin_detail(detail_raw, str(candidate["url"]), company)
        for key in [
            "role",
            "location",
            "job_number",
            "external_job_id",
            "posted_at",
            "updated_at",
            "_jd_text",
        ]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["freshness_source"] = detail["freshness_source"]
        candidate["notes"] = detail["notes"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
        list(executor.map(enrich, list(candidates.values())[:detail_limit]))
    return list(candidates.values())


def paycom_client_key(source: dict[str, Any]) -> str:
    configured = str(source.get("client_key") or source.get("clientkey") or "").strip()
    if configured:
        return configured
    url = str(source.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("clientkey", "clientKey"):
        values = query.get(key) or []
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    match = re.search(r"/portal/([A-Za-z0-9_-]+)/", parsed.path, flags=re.I)
    return match.group(1) if match else ""


def paycom_portal_url(source: dict[str, Any]) -> str:
    client_key = paycom_client_key(source)
    if not client_key:
        return str(source.get("url") or "")
    return f"https://www.paycomonline.net/v4/ats/web.php/portal/{urllib.parse.quote(client_key)}/career-page"


def source_from_paycom_url(company: str, url: str) -> dict[str, Any] | None:
    client_key = paycom_client_key({"url": url})
    if not client_key:
        return None
    return {
        "company": company,
        "platform": "paycom",
        "client_key": client_key,
        "url": paycom_portal_url({"client_key": client_key}),
    }


def ultipro_board_url(source: dict[str, Any]) -> str:
    raw_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(raw_url)
    match = re.match(
        r"(?P<path>/[^/]+/JobBoard/[0-9a-f-]+)",
        parsed.path,
        flags=re.I,
    )
    if not parsed.netloc or not match:
        return raw_url.split("?", 1)[0].rstrip("/") + "/"
    return urllib.parse.urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc,
            match.group("path").rstrip("/") + "/",
            "",
            "",
            "",
        )
    )


def source_from_ultipro_url(company: str, url: str) -> dict[str, Any] | None:
    board_url = ultipro_board_url({"url": url})
    if detect_platform(board_url) != "ultipro":
        return None
    return {
        "company": company,
        "platform": "ultipro",
        "url": board_url,
        "page_size": 50,
        "max_pages": 10,
    }


def parse_ultipro_board_config(raw: str, board_url: str) -> dict[str, str]:
    token_match = re.search(
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
        raw,
        flags=re.I,
    )
    load_match = re.search(r'\bloadUrl\s*:\s*["\']([^"\']*LoadSearchResults[^"\']*)', raw, flags=re.I)
    detail_match = re.search(
        r'\bopportunityLinkUrl\s*:\s*["\']([^"\']*OpportunityDetail[^"\']*)',
        raw,
        flags=re.I,
    )
    return {
        "request_token": html.unescape(token_match.group(1)) if token_match else "",
        "load_url": urllib.parse.urljoin(board_url, html.unescape(load_match.group(1))) if load_match else "",
        "detail_url": urllib.parse.urljoin(board_url, html.unescape(detail_match.group(1))) if detail_match else "",
    }


def initialize_ultipro_session(
    source: dict[str, Any],
) -> tuple[urllib.request.OpenerDirector, dict[str, str]]:
    board_url = ultipro_board_url(source)
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    request = urllib.request.Request(
        board_url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with opener.open(request, timeout=int(source.get("timeout", 25))) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read().decode(charset, errors="replace")
    config = parse_ultipro_board_config(raw, board_url)
    if not config["request_token"] or not config["load_url"] or not config["detail_url"]:
        raise ValueError("UKG/UltiPro board did not expose public search configuration")
    return opener, config


def ultipro_search_payload(skip: int, top: int) -> dict[str, Any]:
    return {
        "opportunitySearch": {
            "QueryString": "",
            "LocationIds": [],
            "JobCategoryIds": [],
            "FullTime": None,
            "OrderBy": [
                {
                    "Value": "postedDateDesc",
                    "PropertyName": "PostedDate",
                    "Ascending": False,
                }
            ],
            "ProximitySearchType": 0,
            "Top": top,
            "Skip": skip,
        }
    }


def fetch_ultipro_search_page(
    opener: urllib.request.OpenerDirector,
    url: str,
    request_token: str,
    payload: dict[str, Any],
    *,
    referer: str,
    timeout: int = 25,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "X-RequestVerificationToken": request_token,
            "Referer": referer,
        },
        method="POST",
    )
    with opener.open(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        data = json.loads(response.read().decode(charset, errors="replace"))
    return data if isinstance(data, dict) else {}


def parse_ultipro_detail(raw: str) -> dict[str, Any]:
    marker = re.search(r"\bCandidateOpportunityDetail\s*\(\s*", raw)
    if not marker:
        return {}
    try:
        detail, _ = json.JSONDecoder().raw_decode(raw[marker.end():].lstrip())
    except (json.JSONDecodeError, TypeError):
        return {}
    return detail if isinstance(detail, dict) else {}


def ultipro_location_text(locations: Any) -> str:
    if not isinstance(locations, list):
        return compact_location_text(locations)
    values: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            value = compact_location_text(location)
        else:
            address = location.get("Address") or {}
            if not isinstance(address, dict):
                address = {}
            state = address.get("State") or {}
            country = address.get("Country") or {}
            state_code = state.get("Code") if isinstance(state, dict) else state
            country_code = country.get("Code") if isinstance(country, dict) else country
            address_parts = [
                str(address.get("City") or "").strip(),
                str(state_code or "").strip(),
            ]
            value = ", ".join(part for part in address_parts if part)
            if country_code and str(country_code).upper() not in {"US", "USA", "UNITED STATES"}:
                value = ", ".join(part for part in [value, str(country_code).strip()] if part)
            if not value:
                value = str(
                    location.get("LocalizedDescription")
                    or location.get("LocalizedName")
                    or ""
                ).strip()
        if value and value not in values:
            values.append(value)
    return " | ".join(values)


def discover_ultipro_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = ultipro_board_url(source)
    try:
        opener, config = initialize_ultipro_session(source)
    except Exception as error:  # noqa: BLE001
        print(f"Could not initialize UKG/UltiPro board for {company}: {error}", file=sys.stderr)
        return []

    page_size = min(max(int(source.get("page_size", 50)), 1), 50)
    max_pages = max(int(source.get("max_pages", 10)), 1)
    previews_by_id: dict[str, dict[str, Any]] = {}
    for page_index in range(max_pages):
        try:
            data = fetch_ultipro_search_page(
                opener,
                config["load_url"],
                config["request_token"],
                ultipro_search_payload(page_index * page_size, page_size),
                referer=board_url,
                timeout=int(source.get("timeout", 25)),
            )
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch UKG/UltiPro jobs for {company}: {error}", file=sys.stderr)
            break
        opportunities = data.get("opportunities") or []
        if not isinstance(opportunities, list) or not opportunities:
            break
        for opportunity in opportunities:
            if not isinstance(opportunity, dict):
                continue
            opportunity_id = str(opportunity.get("Id") or "").strip()
            location_pattern = str(
                source.get("location_include_regex") or ""
            ).strip()
            preview_location = ultipro_location_text(
                opportunity.get("Locations") or []
            )
            if location_pattern and not re.search(
                location_pattern,
                preview_location,
                flags=re.I,
            ):
                continue
            if opportunity_id:
                previews_by_id[opportunity_id] = opportunity
        total = int(data.get("totalCount") or len(previews_by_id))
        if len(previews_by_id) >= total or len(opportunities) < page_size:
            break

    detail_limit = max(int(source.get("max_detail_pages", len(previews_by_id))), 0)
    detail_ids = list(previews_by_id)[:detail_limit]
    detail_workers = min(max(int(source.get("detail_workers", 8)), 1), 16)
    zero_id = "00000000-0000-0000-0000-000000000000"

    def fetch_detail(opportunity_id: str) -> tuple[str, dict[str, Any], str]:
        detail_url = normalize_job_url(config["detail_url"].replace(zero_id, opportunity_id))
        try:
            raw = fetch_url(detail_url, timeout=int(source.get("detail_timeout", 25)))
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch UKG/UltiPro detail for {company} job {opportunity_id}: {error}", file=sys.stderr)
            return opportunity_id, {}, detail_url
        return opportunity_id, parse_ultipro_detail(raw), detail_url

    details_by_id: dict[str, tuple[dict[str, Any], str]] = {}
    if detail_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            for opportunity_id, detail, detail_url in executor.map(fetch_detail, detail_ids):
                details_by_id[opportunity_id] = (detail, detail_url)

    candidates: list[dict[str, Any]] = []
    for opportunity_id, preview in previews_by_id.items():
        detail, detail_url = details_by_id.get(
            opportunity_id,
            ({}, normalize_job_url(config["detail_url"].replace(zero_id, opportunity_id))),
        )
        role = str(detail.get("Title") or preview.get("Title") or f"Job {opportunity_id}").strip()
        posted_at = normalize_datetime(detail.get("PostedDate") or preview.get("PostedDate"))
        updated_at = normalize_datetime(detail.get("UpdatedDate"))
        requisition_number = str(
            detail.get("RequisitionNumber")
            or preview.get("RequisitionNumber")
            or ""
        ).strip()
        description = html_to_text(
            str(
                detail.get("Description")
                or preview.get("BriefDescription")
                or ""
            )
        )
        locations = detail.get("Locations") or preview.get("Locations") or []
        candidates.append(
            {
                "company": company,
                "role": role,
                "url": detail_url,
                "platform": "ultipro",
                "location": ultipro_location_text(locations),
                "job_number": requisition_number,
                "external_job_id": opportunity_id,
                "posted_at": posted_at,
                "updated_at": updated_at,
                "source": board_url,
                "source_query": str(
                    detail.get("JobCategoryName")
                    or preview.get("JobCategoryName")
                    or ""
                ).strip(),
                "freshness_source": "ultipro_posted_date" if posted_at else "unknown",
                "notes": "UKG/UltiPro public job board API and detail adapter.",
                "_jd_text": "\n\n".join(
                    part
                    for part in [
                        role,
                        description,
                        html_to_text(str(preview.get("BriefDescription") or "")),
                    ]
                    if part
                ),
            }
        )
    return candidates


def zoho_recruit_jobs_from_html(raw: str) -> list[dict[str, Any]]:
    tag_match = re.search(
        r'<input\b(?=[^>]*\bid=["\']jobs["\'])[^>]*>',
        raw,
        flags=re.I | re.S,
    )
    if not tag_match:
        return []
    value_match = re.search(r'\bvalue=(["\'])(.*?)\1', tag_match.group(0), flags=re.I | re.S)
    if not value_match:
        return []
    try:
        jobs = json.loads(html.unescape(value_match.group(2)))
    except (json.JSONDecodeError, TypeError):
        return []
    return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []


def zoho_recruit_location(job: dict[str, Any]) -> str:
    location = ", ".join(
        str(job.get(key) or "").strip()
        for key in ["City", "State", "Country"]
        if str(job.get(key) or "").strip()
    )
    if truthy_source_flag(job.get("Remote_Job"), default=False):
        return f"Remote | {location}" if location else "Remote, United States"
    return location


def discover_zoho_recruit_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").split("?", 1)[0].rstrip("/")
    try:
        raw = fetch_url(board_url, timeout=int(source.get("timeout", 30)))
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Zoho Recruit board for {company}: {error}", file=sys.stderr)
        return []
    candidates: list[dict[str, Any]] = []
    for job in zoho_recruit_jobs_from_html(raw):
        if job.get("Publish") is False or job.get("Is_Locked") is True:
            continue
        job_id = str(job.get("id") or "").strip()
        role = str(job.get("Posting_Title") or job.get("Job_Opening_Name") or "").strip()
        if not job_id or not role:
            continue
        detail_url = normalize_job_url(
            f"{board_url}/{urllib.parse.quote(job_id)}/{urllib.parse.quote(slugify(role))}"
        )
        department = job.get("Department_Name") or {}
        if isinstance(department, dict):
            department = department.get("name") or ""
        posted_at = normalize_datetime(job.get("Date_Opened"))
        description = html_to_text(str(job.get("Job_Description") or ""))
        candidates.append(
            {
                "company": company,
                "role": role,
                "url": detail_url,
                "platform": "zoho_recruit",
                "location": zoho_recruit_location(job),
                "job_number": job_id,
                "external_job_id": job_id,
                "posted_at": posted_at,
                "updated_at": "",
                "source": board_url,
                "source_query": str(department).strip(),
                "freshness_source": "zoho_recruit_date_opened" if posted_at else "unknown",
                "notes": "Zoho Recruit public career-site structured jobs adapter.",
                "_jd_text": "\n\n".join(part for part in [role, description] if part),
            }
        )
    return candidates


def parse_paycom_portal_config(raw: str) -> dict[str, str]:
    marker = re.search(r"\bvar\s+configsFromHost\s*=\s*", raw)
    if not marker:
        return {}
    try:
        config, _ = json.JSONDecoder().raw_decode(raw[marker.end():].lstrip())
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(config, dict):
        return {}
    lib_config = config.get("libConfig") or {}
    if isinstance(lib_config, str):
        with contextlib.suppress(json.JSONDecodeError):
            lib_config = json.loads(lib_config)
    if not isinstance(lib_config, dict):
        lib_config = {}
    return {
        "session_jwt": str(config.get("sessionJWT") or ""),
        "service_url": str(lib_config.get("atsPortalMantleServiceUrl") or ""),
    }


def fetch_paycom_json(
    url: str,
    session_jwt: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,*/*;q=0.8",
            "Content-Type": "application/json",
            "Authorization": session_jwt,
            "Locale": "en-US",
            "Translation-Highlights": "false",
        },
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def paycom_search_payload(skip: int, take: int) -> dict[str, Any]:
    return {
        "skip": skip,
        "take": take,
        "filtersForQuery": {
            "distanceFrom": 0,
            "workEnvironments": [],
            "positionTypes": [],
            "educationLevels": [],
            "categories": [],
            "travelTypes": [],
            "shiftTypes": [],
            "otherFilters": [],
            "keywordSearchText": "",
            "location": "",
            "sortOption": "",
        },
    }


def discover_paycom_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    client_key = paycom_client_key(source)
    portal_url = paycom_portal_url(source)
    if not client_key or not portal_url:
        return []
    try:
        portal_raw = fetch_url(portal_url, timeout=int(source.get("timeout", 20)))
        portal_config = parse_paycom_portal_config(portal_raw)
        session_jwt = portal_config["session_jwt"]
        service_url = portal_config["service_url"].rstrip("/") + "/"
        if not session_jwt or not service_url.startswith("http"):
            raise ValueError("Paycom portal did not expose a public session or API URL")
    except Exception as error:  # noqa: BLE001
        print(f"Could not initialize Paycom API for {company}: {error}", file=sys.stderr)
        return []

    page_size = min(max(int(source.get("page_size", 100)), 1), 100)
    max_pages = max(int(source.get("max_pages", 5)), 1)
    search_url = urllib.parse.urljoin(service_url, "api/ats/job-posting-previews/search")
    previews_by_id: dict[str, dict[str, Any]] = {}
    for page_index in range(max_pages):
        try:
            data = fetch_paycom_json(
                search_url,
                session_jwt,
                payload=paycom_search_payload(page_index * page_size, page_size),
                timeout=int(source.get("timeout", 20)),
            )
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch Paycom jobs for {company}: {error}", file=sys.stderr)
            break
        previews = data.get("jobPostingPreviews") or []
        if not isinstance(previews, list) or not previews:
            break
        for preview in previews:
            if not isinstance(preview, dict):
                continue
            job_id = str(preview.get("jobId") or "").strip()
            if job_id:
                previews_by_id[job_id] = preview
        total = int(data.get("jobPostingPreviewsCount") or len(previews_by_id))
        if len(previews_by_id) >= total or len(previews) < page_size:
            break

    detail_limit = max(int(source.get("max_detail_pages", len(previews_by_id))), 0)
    detail_ids = list(previews_by_id)[:detail_limit]
    detail_workers = min(max(int(source.get("detail_workers", 8)), 1), 16)
    detail_timeout = int(source.get("detail_timeout", source.get("timeout", 20)))

    def fetch_detail(job_id: str) -> tuple[str, dict[str, Any]]:
        detail_url = urllib.parse.urljoin(
            service_url,
            f"api/ats/job-postings/{urllib.parse.quote(job_id)}",
        )
        try:
            detail_data = fetch_paycom_json(detail_url, session_jwt, timeout=detail_timeout)
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch Paycom detail for {company} job {job_id}: {error}", file=sys.stderr)
            return job_id, {}
        detail = detail_data.get("jobPosting") or {}
        return job_id, detail if isinstance(detail, dict) else {}

    details_by_id: dict[str, dict[str, Any]] = {}
    if detail_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            for job_id, detail in executor.map(fetch_detail, detail_ids):
                details_by_id[job_id] = detail

    candidates: list[dict[str, Any]] = []
    for job_id, preview in previews_by_id.items():
        detail = details_by_id.get(job_id, {})
        google_job: dict[str, Any] = {}
        raw_google_job = detail.get("googleJobJson")
        if isinstance(raw_google_job, str) and raw_google_job.strip():
            with contextlib.suppress(json.JSONDecodeError):
                parsed_google_job = json.loads(raw_google_job)
                if isinstance(parsed_google_job, dict):
                    google_job = parsed_google_job
        role = str(detail.get("jobTitle") or preview.get("jobTitle") or f"Job {job_id}").strip()
        location = compact_location_text(
            preview.get("locations")
            or detail.get("location")
            or google_job.get("jobLocation")
        )
        url = normalize_job_url(
            str(google_job.get("url") or f"{portal_url.rsplit('/career-page', 1)[0]}/jobs/{job_id}")
        )
        description = "\n".join(
            part
            for part in [
                html_to_text(str(detail.get("description") or preview.get("description") or "")),
                html_to_text(str(detail.get("qualifications") or "")),
            ]
            if part
        )
        candidates.append(
            {
                "company": company,
                "role": role,
                "url": url,
                "platform": "paycom",
                "location": location,
                "job_number": job_id,
                "external_job_id": job_id,
                "posted_at": normalize_datetime(
                    google_job.get("datePosted") or preview.get("postedOn")
                ),
                "updated_at": "",
                "source": source.get("url", portal_url),
                "source_query": "all_jobs",
                "freshness_source": "paycom_json_ld_date_posted" if google_job.get("datePosted") else "unknown",
                "notes": "Paycom public career portal API adapter.",
                "_jd_text": description,
            }
        )
    location_pattern = str(
        source.get("location_include_regex") or ""
    ).strip()
    if location_pattern:
        candidates = [
            candidate
            for candidate in candidates
            if re.search(
                location_pattern,
                str(candidate.get("location") or ""),
                flags=re.I,
            )
        ]
    return candidates


def workable_account(source: dict[str, Any]) -> str:
    if source.get("account"):
        return str(source["account"]).strip()
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if "apply.workable.com" in parsed.netloc.lower() and parts:
        return parts[0]
    return slugify(str(source.get("company") or ""))


def discover_workable_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    account = workable_account(source)
    if not account:
        return []
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    candidates: dict[str, dict[str, Any]] = {}
    endpoints = [
        f"https://apply.workable.com/api/v3/accounts/{urllib.parse.quote(account)}/jobs",
        f"https://apply.workable.com/api/v1/accounts/{urllib.parse.quote(account)}/jobs",
    ]
    max_pages = int(source.get("max_pages", 10))
    for keyword in [str(item) for item in keywords if str(item).strip()]:
        for endpoint in endpoints:
            token = ""
            endpoint_succeeded = False
            for _page_index in range(max_pages):
                payload = {"query": keyword}
                if token:
                    payload["token"] = token
                try:
                    data = fetch_json_post(endpoint, payload)
                except Exception:
                    break
                endpoint_succeeded = True
                jobs = data.get("results") or data.get("jobs") or data.get("content") or []
                if not isinstance(jobs, list):
                    break
                for job in jobs:
                    if not isinstance(job, dict):
                        continue
                    shortcode = str(job.get("shortcode") or job.get("id") or "").strip()
                    title = str(job.get("title") or job.get("name") or infer_role_from_url(shortcode)).strip()
                    url = normalize_job_url(str(job.get("url") or job.get("application_url") or ""))
                    if not url and shortcode:
                        url = normalize_job_url(
                            f"https://apply.workable.com/{account}/j/{urllib.parse.quote(shortcode)}"
                        )
                    candidates[url] = {
                        "company": company,
                        "role": title,
                        "url": url,
                        "platform": "workable",
                        "location": compact_location_text(job.get("location") or job.get("locations")),
                        "job_number": shortcode,
                        "external_job_id": shortcode,
                        "posted_at": normalize_datetime(
                            job.get("published")
                            or job.get("published_on")
                            or job.get("created_at")
                            or job.get("created")
                        ),
                        "updated_at": normalize_datetime(job.get("updated_at")),
                        "source": source.get("url", f"https://apply.workable.com/{account}/"),
                        "source_query": keyword,
                        "notes": f"Workable direct adapter; account={account}",
                    }
                next_token = str(data.get("nextPage") or data.get("next_page") or "").strip()
                if not next_token or next_token == token:
                    break
                token = next_token
            if endpoint_succeeded:
                break
    return list(candidates.values())


def bamboohr_subdomain(source: dict[str, Any]) -> str:
    if source.get("subdomain"):
        return str(source["subdomain"]).strip()
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    host = parsed.netloc.lower()
    if host.endswith(".bamboohr.com"):
        return host.split(".")[0]
    return slugify(str(source.get("company") or ""))


def discover_bamboohr_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    subdomain = bamboohr_subdomain(source)
    if not subdomain:
        return []
    url = f"https://{subdomain}.bamboohr.com/careers/list"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Referer": f"https://{subdomain}.bamboohr.com/careers/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch BambooHR careers for {company}: {error}", file=sys.stderr)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        candidates = parse_json_ld_jobs(raw, url, str(company))
        for candidate in candidates:
            candidate["platform"] = "bamboohr"
        return candidates
    jobs = data.get("result") or data.get("jobs") or data if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or job.get("jobOpeningId") or "").strip()
        title = str(job.get("jobOpeningName") or job.get("title") or infer_role_from_url(job_id)).strip()
        job_url = normalize_job_url(str(job.get("url") or ""))
        if not job_url and job_id:
            job_url = normalize_job_url(f"https://{subdomain}.bamboohr.com/careers/{urllib.parse.quote(job_id)}")
        candidates[job_url] = {
            "company": company,
            "role": title,
            "url": job_url,
            "platform": "bamboohr",
            "location": compact_location_text(job.get("location")),
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": normalize_datetime(job.get("datePosted") or job.get("postedDate")),
            "updated_at": normalize_datetime(job.get("updatedAt")),
            "source": source.get("url", url),
            "notes": f"BambooHR direct adapter; subdomain={subdomain}",
        }
    return list(candidates.values())


def yc_page_job_postings(url: str, company: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = ""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = fetch_url(url, timeout=30)
            break
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt == 0:
                time.sleep(1)
    if not raw:
        print(f"Could not fetch YC jobs for {company} at {url}: {last_error}", file=sys.stderr)
        return {}, []
    match = re.search(r'data-page=["\']([^"\']+)["\']', raw, flags=re.I | re.S)
    if not match:
        return {}, []
    try:
        page = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as error:
        print(f"Could not parse YC job payload for {company}: {error}", file=sys.stderr)
        return {}, []
    props = page.get("props", {}) if isinstance(page, dict) else {}
    jobs = props.get("jobPostings") or []
    if not isinstance(jobs, list):
        jobs = []
    return props, [job for job in jobs if isinstance(job, dict)]


def yc_candidate_from_posting(job: dict[str, Any], source_url: str, company: str, platform: str, note: str) -> dict[str, Any] | None:
    job_id = str(job.get("id") or "").strip()
    title = str(job.get("title") or infer_role_from_url(str(job.get("url") or job_id))).strip()
    job_url = normalize_job_url(urllib.parse.urljoin(source_url, str(job.get("url") or "")))
    if not job_url:
        return None
    created_at = parse_workday_posted_on(str(job.get("createdAt") or ""))
    last_active = parse_workday_posted_on(str(job.get("lastActive") or ""))
    skills = job.get("skills") if isinstance(job.get("skills"), list) else []
    details = [
        f"YC role={job.get('role', '')}",
        f"role_type={job.get('roleSpecificType', '')}",
        f"min_experience={job.get('minExperience', '')}",
        f"visa={job.get('visa', '')}",
        f"skills={', '.join(str(skill) for skill in skills)}" if skills else "",
        note,
    ]
    return {
        "company": str(job.get("companyName") or company),
        "role": title,
        "url": job_url,
        "platform": platform,
        "location": compact_location_text(job.get("location")),
        "job_number": job_id,
        "external_job_id": job_id,
        "posted_at": created_at,
        "updated_at": last_active,
        "source": source_url,
        "source_query": str(job.get("role") or ""),
        "freshness_source": "official_relative_posted_at" if created_at else "unknown",
        "notes": "; ".join(item for item in details if item),
    }


def discover_yc_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    props, jobs = yc_page_job_postings(source_url, str(company))
    company_data = props.get("company", {}) if isinstance(props.get("company"), dict) else {}
    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs:
        candidate = yc_candidate_from_posting(
            job,
            source_url,
            str(job.get("companyName") or company_data.get("name") or company),
            "yc_jobs",
            "Y Combinator Work at a Startup company jobs adapter.",
        )
        if not candidate:
            continue
        candidates[candidate["url"]] = candidate
    return list(candidates.values())


def discover_yc_job_board_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Y Combinator Jobs")
    urls = source.get("urls") or source.get("url") or "https://www.ycombinator.com/jobs"
    if isinstance(urls, str):
        urls = [urls]
    candidates: dict[str, dict[str, Any]] = {}
    for source_url in [str(item).strip() for item in urls if str(item).strip()]:
        _, jobs = yc_page_job_postings(source_url, str(company))
        for job in jobs:
            if source.get("role") and str(job.get("role") or "") != str(source["role"]):
                continue
            candidate = yc_candidate_from_posting(
                job,
                source_url,
                str(job.get("companyName") or company),
                "yc_job_board",
                "Y Combinator Work at a Startup job board adapter.",
            )
            if not candidate:
                continue
            candidates[candidate["url"]] = candidate
    return list(candidates.values())


def hn_latest_who_is_hiring_story() -> dict[str, Any] | None:
    params = urllib.parse.urlencode(
        {
            "query": "Ask HN: Who is hiring?",
            "tags": "story",
            "hitsPerPage": 10,
        }
    )
    data = fetch_json(f"https://hn.algolia.com/api/v1/search_by_date?{params}")
    hits = data.get("hits", []) if isinstance(data, dict) else []
    for hit in hits:
        title = str(hit.get("title") or "")
        if re.match(r"Ask HN:\s*Who is hiring\?\s*\([^)]+\)", title, flags=re.I):
            return hit
    return None


def hn_story_id_from_source(source: dict[str, Any]) -> str:
    if source.get("story_id"):
        return str(source["story_id"]).strip()
    url = str(source.get("url") or "").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    item_id = str((query.get("id") or [""])[0]).strip()
    if item_id.isdigit():
        return item_id
    latest = hn_latest_who_is_hiring_story()
    return str(latest.get("objectID") or "").strip() if latest else ""


def hn_comment_url(comment_id: str) -> str:
    return f"https://news.ycombinator.com/item?id={urllib.parse.quote(comment_id)}"


def hn_comment_text(raw_html: str) -> str:
    with_breaks = re.sub(r"(?i)<p\s*/?>|<br\s*/?>|</p>", "\n", raw_html)
    with_breaks = re.sub(r"(?is)<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 (\1)", with_breaks)
    with_breaks = re.sub(r"(?is)<script.*?</script>", " ", with_breaks)
    with_breaks = re.sub(r"(?is)<style.*?</style>", " ", with_breaks)
    with_breaks = re.sub(r"(?s)<[^>]+>", " ", with_breaks)
    with_breaks = html.unescape(with_breaks)
    lines = [re.sub(r"\s+", " ", line).strip() for line in with_breaks.splitlines()]
    return "\n".join(line for line in lines if line)


def hn_comment_links(raw_html: str) -> list[str]:
    links: list[str] = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', raw_html, flags=re.I):
        href = html.unescape(href)
        if href.startswith("item?"):
            continue
        if href.startswith("//"):
            href = f"https:{href}"
        if not href.startswith(("http://", "https://")):
            continue
        parsed = urllib.parse.urlparse(href)
        if parsed.netloc.lower() in {"news.ycombinator.com", "www.ycombinator.com"} and parsed.path in {"", "/item"}:
            continue
        if re.search(r"(?:unsubscribe|mailto:)", href, flags=re.I):
            continue
        links.append(normalize_job_url(href))
    return list(dict.fromkeys(links))


def hn_probable_apply_url(links: list[str]) -> str:
    priority_patterns = [
        r"(?:greenhouse\.io|lever\.co|ashbyhq\.com|jobs\.gem\.com|workdayjobs\.com|workdaysite\.com)",
        r"(?:/careers?|/jobs?|/apply|/positions?|/openings?)",
    ]
    for pattern in priority_patterns:
        for link in links:
            if re.search(pattern, link, flags=re.I):
                return link
    return links[0] if links else ""


def hn_company_and_roles(text: str) -> tuple[str, list[str], str]:
    lines = [line.strip(" -\t") for line in text.splitlines() if line.strip()]
    first_line = lines[0] if lines else ""
    segments = [segment.strip() for segment in re.split(r"\s+\|\s+|\s+[-–]\s+", first_line) if segment.strip()]
    company = segments[0] if segments else "HN Who is Hiring"
    company = re.sub(r"\s+https?://\S+", "", company).strip()
    company = re.sub(r"\s*\([^)]*\)\s*$", "", company).strip() or "HN Who is Hiring"
    title_terms = DEFAULT_DISCOVERY_TITLE_KEYWORDS + ["sdet", "qa", "quality", "founding engineer", "engineer"]
    roles: list[str] = []
    for segment in segments[1:]:
        if any(re.search(rf"\b{re.escape(term)}\b", segment, flags=re.I) for term in title_terms):
            roles.append(segment)
    if not roles:
        for line in lines[:6]:
            for title in re.findall(
                r"\b(?:Senior |Staff |Junior |Founding |Backend |Frontend |Full[- ]Stack |AI |ML |Platform |DevOps |QA |SDET |Mobile )?(?:Software Engineer|Backend Engineer|Frontend Engineer|Full[- ]Stack Engineer|AI Engineer|ML Engineer|Machine Learning Engineer|Platform Engineer|DevOps Engineer|QA Engineer|SDET|Mobile Engineer)\b",
                line,
                flags=re.I,
            ):
                roles.append(title)
    roles = list(dict.fromkeys(role.strip() for role in roles if role.strip()))
    if not roles:
        roles = ["Software Engineer / Engineering Roles"]
    location = ""
    for segment in segments[1:] + lines[:8]:
        if re.search(r"\b(contact|email|mailto|apply)\b|@|https?://", segment, flags=re.I):
            continue
        if re.search(r"\b(remote|seattle|bellevue|washington|wa|san francisco|sf|bay area|california|ca|los angeles|la|nyc|new york)\b", segment, flags=re.I):
            location = segment
            break
    return company, roles[:4], location


def hn_comment_has_job_shape(text: str) -> bool:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    segments = [segment.strip() for segment in re.split(r"\s+\|\s+|\s+[-–]\s+", first_line) if segment.strip()]
    if len(segments) >= 3:
        roleish = any(re.search(r"\b(engineer|developer|devops|sdet|qa|machine learning|ml|ai|backend|frontend|full.?stack)\b", segment, flags=re.I) for segment in segments[1:])
        locationish = any(re.search(r"\b(remote|onsite|hybrid|seattle|bellevue|wa|washington|sf|san francisco|bay area|ca|california|usa|us)\b", segment, flags=re.I) for segment in segments[1:])
        if roleish and locationish:
            return True
    return bool(re.search(r"\b(we(?:'re| are) hiring|is hiring|now hiring|apply here|apply at|careers?|job openings?|open roles?|we(?:'re| are) looking for)\b", text, flags=re.I))


def hn_comment_is_hiring_post(hit: dict[str, Any], story_id: str, text: str) -> bool:
    if str(hit.get("parent_id") or "") != story_id:
        return False
    lowered = text.lower()
    reject_patterns = [
        r"\bi['’]?d like to apply\b",
        r"\binterested in applying\b",
        r"\bto anyone who considers applying\b",
        r"\bhow do i apply\b",
        r"\bseeking work\b",
        r"\blooking for work\b",
        r"\bavailable for hire\b",
        r"\bnot hiring\b",
        r"\bnot currently hiring\b",
        r"\bgreat thread\b",
        r"\bhere are the listings\b",
        r"\bi['’]?m a\b.*\bdeveloper\b",
    ]
    if any(re.search(pattern, lowered) for pattern in reject_patterns):
        return False
    return hn_comment_has_job_shape(text)


def discover_hn_who_is_hiring_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    story_id = hn_story_id_from_source(source)
    if not story_id:
        print("Could not locate latest HN Who is Hiring story.", file=sys.stderr)
        return []
    max_pages = int(source.get("max_pages", 8))
    hits_per_page = min(max(int(source.get("page_size", 100)), 1), 100)
    candidates: dict[str, dict[str, Any]] = {}
    for page in range(max_pages):
        params = urllib.parse.urlencode(
            {
                "tags": f"comment,story_{story_id}",
                "hitsPerPage": hits_per_page,
                "page": page,
            }
        )
        try:
            data = fetch_json(f"https://hn.algolia.com/api/v1/search_by_date?{params}", timeout=30)
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch HN Who is Hiring page {page} for story {story_id}: {error}", file=sys.stderr)
            break
        hits = data.get("hits", []) if isinstance(data, dict) else []
        if not hits:
            break
        for hit in hits:
            raw_comment = str(hit.get("comment_text") or "")
            text = hn_comment_text(raw_comment)
            if not hn_comment_is_hiring_post(hit, story_id, text):
                continue
            comment_id = str(hit.get("objectID") or "").strip()
            company, roles, location = hn_company_and_roles(text)
            links = hn_comment_links(raw_comment)
            apply_url = hn_probable_apply_url(links) or hn_comment_url(comment_id)
            created_at = normalize_datetime(hit.get("created_at") or hit.get("created_at_i"))
            for role in roles:
                url = apply_url
                if len(roles) > 1 and apply_url == hn_comment_url(comment_id):
                    url = f"{apply_url}&role={urllib.parse.quote(slugify(role))}"
                normalized_url = normalize_job_url(url)
                candidates[normalized_url] = {
                    "company": company,
                    "role": role,
                    "url": normalized_url,
                    "platform": detect_platform(normalized_url) if apply_url != hn_comment_url(comment_id) else "hn_who_is_hiring",
                    "location": location or "Unknown (HN)",
                    "posted_at": created_at,
                    "updated_at": created_at,
                    "source": hn_comment_url(comment_id),
                    "source_query": str(hit.get("story_title") or "Ask HN: Who is hiring?"),
                    "freshness_source": "hn_comment_created_at",
                    "external_job_id": comment_id,
                    "notes": f"HN Who is Hiring comment; story_id={story_id}. Review original comment for exact role/location. {text[:500]}",
                }
    return list(candidates.values())


def rss_feed_url(source: dict[str, Any]) -> str:
    return str(source.get("feed_url") or source.get("url") or "").strip()


def parse_rss_title(title: str) -> tuple[str, str]:
    cleaned = title.strip()
    match = re.search(r"\(([^()]+)\)\s*$", cleaned)
    if not match:
        return cleaned, ""
    location = match.group(1).strip()
    role = cleaned[: match.start()].strip()
    return role or cleaned, location


def rss_block_text(block: str, tag: str) -> str:
    pattern = rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>"
    match = re.search(pattern, block, flags=re.I | re.S)
    if not match:
        return ""
    value = match.group(1).strip()
    cdata = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", value, flags=re.S)
    if cdata:
        value = cdata.group(1)
    return html.unescape(value).strip()


def rss_block_texts(block: str, tag: str) -> list[str]:
    pattern = rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>"
    values: list[str] = []
    for raw_value in re.findall(pattern, block, flags=re.I | re.S):
        value = html.unescape(str(raw_value)).strip()
        cdata = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", value, flags=re.S)
        if cdata:
            value = cdata.group(1).strip()
        if value and value not in values:
            values.append(value)
    return values


def rss_items_from_regex(raw: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for block in re.findall(r"<item\b[^>]*>(.*?)</item>", raw, flags=re.I | re.S):
        description = rss_block_text(block, "description") or rss_block_text(block, "content:encoded")
        cities = merge_unique(
            rss_block_texts(block, "tt:city"),
            rss_block_texts(block, "job:city"),
        )
        states = merge_unique(
            rss_block_texts(block, "tt:state"),
            rss_block_texts(block, "job:state"),
        )
        countries = merge_unique(
            rss_block_texts(block, "tt:country"),
            rss_block_texts(block, "job:country"),
        )
        items.append(
            {
                "title": rss_block_text(block, "title"),
                "link": rss_block_text(block, "link") or rss_block_text(block, "guid"),
                "guid": rss_block_text(block, "guid"),
                "description": description,
                "pubDate": rss_block_text(block, "pubDate"),
                "cities": cities,
                "states": states,
                "countries": countries,
                "department": (
                    rss_block_text(block, "tt:department")
                    or rss_block_text(block, "job:category")
                ),
                "categories": rss_block_texts(block, "category"),
            }
        )
    for block in re.findall(r"<entry\b[^>]*>(.*?)</entry>", raw, flags=re.I | re.S):
        link_match = re.search(
            r"<link\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*/?>",
            block,
            flags=re.I | re.S,
        )
        link = html.unescape(link_match.group(1)).strip() if link_match else rss_block_text(block, "link")
        items.append(
            {
                "title": rss_block_text(block, "title"),
                "link": link,
                "guid": rss_block_text(block, "id") or link,
                "description": (
                    rss_block_text(block, "content")
                    or rss_block_text(block, "summary")
                ),
                "pubDate": (
                    rss_block_text(block, "published")
                    or rss_block_text(block, "updated")
                ),
                "cities": [],
                "states": [],
                "countries": [],
                "department": "",
                "categories": rss_block_texts(block, "category"),
            }
        )
    return items


def rss_xml_texts_by_local_name(item: Any, local_name: str) -> list[str]:
    values: list[str] = []
    for element in item.iter():
        tag = str(getattr(element, "tag", ""))
        if tag.rsplit("}", 1)[-1] != local_name:
            continue
        value = str(getattr(element, "text", "") or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def sitemap_entries_from_regex(raw: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for block in re.findall(r"<url\b[^>]*>(.*?)</url>", raw, flags=re.I | re.S):
        entries.append(
            {
                "loc": rss_block_text(block, "loc"),
                "lastmod": rss_block_text(block, "lastmod"),
            }
        )
    return entries


def sitemap_pretty_phrase(value: str) -> str:
    acronyms = {
        "ai": "AI",
        "api": "API",
        "apis": "APIs",
        "aws": "AWS",
        "ciso": "CISO",
        "cx": "CX",
        "devops": "DevOps",
        "fp": "FP",
        "gtm": "GTM",
        "ml": "ML",
        "qa": "QA",
        "rag": "RAG",
        "sde": "SDE",
        "sre": "SRE",
        "ui": "UI",
        "ux": "UX",
        "zp3": "ZP3",
    }
    words = [word for word in re.split(r"[-_\s]+", value.strip()) if word]
    return " ".join(acronyms.get(word.lower(), word.capitalize()) for word in words)


def sitemap_role_location_from_url(source: dict[str, Any], url: str) -> tuple[str, str]:
    slug = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", "", slug, flags=re.I)
    location_slug = ""
    for marker in source.get("location_markers", []):
        marker_slug = slugify(str(marker))
        index = slug.find(f"-{marker_slug}")
        if index >= 0:
            location_slug = slug[index + 1 :]
            slug = slug[:index]
            break
    role = sitemap_pretty_phrase(slug) or infer_role_from_url(url)
    location = sitemap_pretty_phrase(location_slug)
    return role, location


def discover_sitemap_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    sitemap_url = str(source.get("sitemap_url") or source.get("url") or "").strip()
    if not sitemap_url:
        return []
    try:
        raw = fetch_url(sitemap_url, timeout=30)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch sitemap for {company}: {error}", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(raw)
        entries = [
            {
                "loc": str((url_node.findtext("{*}loc") or url_node.findtext("loc") or "")).strip(),
                "lastmod": str((url_node.findtext("{*}lastmod") or url_node.findtext("lastmod") or "")).strip(),
            }
            for url_node in root.findall(".//{*}url")
        ]
    except ImportError:
        entries = sitemap_entries_from_regex(raw)
    except ET.ParseError as error:
        print(f"Could not parse sitemap for {company} with XML parser, using fallback: {error}", file=sys.stderr)
        entries = sitemap_entries_from_regex(raw)
    include_regex = str(source.get("include_url_regex") or "")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    keyword_values = [str(item) for item in keywords if str(item).strip()]
    candidates: dict[str, dict[str, Any]] = {}
    for entry in entries:
        url = normalize_job_url(str(entry.get("loc") or ""))
        if not url:
            continue
        if include_regex and not re.search(include_regex, url):
            continue
        role, location = sitemap_role_location_from_url(source, url)
        location = location or str(source.get("default_location") or "").strip()
        if keyword_values and not any(keyword_matches_title(keyword, role) for keyword in keyword_values):
            continue
        lastmod = str(entry.get("lastmod") or "")
        candidates[url] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": source.get("target_platform") or detect_platform(url),
            "location": location,
            "posted_at": normalize_datetime(lastmod),
            "updated_at": normalize_datetime(lastmod),
            "source": sitemap_url,
            "source_query": "sitemap",
            "freshness_source": "sitemap_lastmod" if lastmod else "unknown",
            "external_job_id": slugify(url),
            "_jd_text": "\n\n".join(block for block in [role, location] if block),
            "notes": f"Sitemap adapter; lastmod is used as freshness proxy. sitemap={sitemap_url}",
        }
    if truthy_source_flag(source.get("fetch_details"), default=False):
        detail_limit = max(0, int(source.get("max_detail_pages", len(candidates))))
        detail_workers = max(1, int(source.get("detail_workers", 8)))
        selected = sorted(
            candidates.values(),
            key=lambda candidate: (
                0 if unclassified_technical_title_relevant(candidate) else 1,
                str(candidate.get("role") or "").lower(),
            ),
        )[:detail_limit]

        def enrich(candidate: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            try:
                detail_raw = fetch_url(candidate["url"])
            except Exception:
                return candidate["url"], None
            parsed = parse_json_ld_jobs(detail_raw, candidate["url"], str(company))
            return candidate["url"], parsed[0] if parsed else None

        if selected:
            with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
                for url, detail in executor.map(enrich, selected):
                    if not detail:
                        continue
                    candidate = candidates[url]
                    for key in ["role", "location", "posted_at", "updated_at"]:
                        if detail.get(key):
                            candidate[key] = detail[key]
                    if detail.get("_jd_text"):
                        candidate["_jd_text"] = detail["_jd_text"]
                    if detail.get("posted_at"):
                        candidate["freshness_source"] = "sitemap_json_ld_date_posted"
                    candidate["notes"] = (
                        f"Sitemap listing enriched from official detail-page JSON-LD. sitemap={sitemap_url}"
                    )
    return list(candidates.values())


def governmentjobs_agency(source: dict[str, Any]) -> str:
    if source.get("agency"):
        return str(source["agency"]).strip("/")
    path_parts = [part for part in urllib.parse.urlparse(str(source.get("url") or "")).path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0].lower() == "careers":
        return path_parts[1]
    return ""


def governmentjobs_search_url(source: dict[str, Any], keyword: str, page: int, category: str = "") -> str:
    parsed = urllib.parse.urlparse(str(source.get("url") or "https://www.governmentjobs.com/careers"))
    scheme = parsed.scheme or "https"
    host = parsed.netloc or "www.governmentjobs.com"
    agency = governmentjobs_agency(source)
    params = {
        "agency": agency,
        "keyword": keyword,
        "page": str(page),
        "sort": str(source.get("sort") or "PostingDate"),
        "isDescendingSort": "true" if truthy_source_flag(source.get("is_descending_sort"), default=True) else "false",
    }
    if source.get("department_folder"):
        params["departmentFolder"] = str(source["department_folder"])
    if category:
        params["category[0]"] = category
    return f"{scheme}://{host}/careers/home/index?{urllib.parse.urlencode(params)}"


def governmentjobs_query_plan(source: dict[str, Any]) -> list[dict[str, str]]:
    mode = str(source.get("governmentjobs_query_mode") or "keywords").strip().lower()
    configured_keywords = source.get("keywords")
    if configured_keywords is None:
        keywords = list(DEFAULT_WORKDAY_KEYWORDS)
    elif isinstance(configured_keywords, str):
        keywords = [configured_keywords]
    else:
        keywords = [str(item) for item in configured_keywords if str(item).strip()]
    if mode == "keywords" and str(source.get("track_hint") or "") == "traditional_it_wa":
        keywords = merge_unique(keywords, DEFAULT_GOVERNMENTJOBS_TRADITIONAL_IT_KEYWORDS)

    configured_categories = source.get("categories") or []
    if isinstance(configured_categories, str):
        configured_categories = [configured_categories]
    categories = [str(item).strip() for item in configured_categories if str(item).strip()]

    plan: list[dict[str, str]] = []
    if mode in {"all", "all_recent", "category_plus_keywords"}:
        if mode in {"all", "all_recent"} or truthy_source_flag(source.get("scan_all_jobs"), default=False):
            plan.append({"keyword": "", "category": "", "label": "all_recent_jobs", "kind": "all"})
        for category in categories:
            plan.append({"keyword": "", "category": category, "label": f"category:{category}", "kind": "category"})
    if mode in {"keywords", "category_plus_keywords"}:
        plan.extend(
            {"keyword": keyword, "category": "", "label": keyword, "kind": "keyword"}
            for keyword in keywords
            if keyword.strip()
        )
    return plan or [{"keyword": "", "category": "", "label": "all_recent_jobs", "kind": "all"}]


def governmentjobs_page_is_older_than(candidates: list[dict[str, Any]], recent_days: int) -> bool:
    if recent_days <= 0 or not candidates:
        return False
    dates = [parse_datetime(candidate.get("posted_at")) for candidate in candidates]
    if any(value is None for value in dates):
        return False
    cutoff_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=recent_days)
    return all(value.date() < cutoff_date for value in dates if value is not None)


def fetch_governmentjobs_search(url: str, source: dict[str, Any], timeout: int = 30) -> str:
    parsed = urllib.parse.urlparse(str(source.get("url") or "https://www.governmentjobs.com/careers"))
    referer = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def governmentjobs_newprint_url(job_url: str, job_id: str) -> str:
    parsed = urllib.parse.urlparse(job_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    agency = ""
    if len(path_parts) >= 2 and path_parts[0].lower() == "careers":
        agency = path_parts[1]
    if not agency:
        if parsed.netloc.lower().endswith("governmentjobs.com"):
            return urllib.parse.urlunparse(parsed._replace(path=f"/jobs/newprint/{job_id}", query="", fragment=""))
        return job_url
    return urllib.parse.urlunparse(parsed._replace(path=f"/careers/{agency}/jobs/newprint/{job_id}", query="", fragment=""))


def parse_governmentjobs_newprint(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    title_match = re.search(r'<h1[^>]*class=["\'][^"\']*job-title[^"\']*["\'][^>]*>(.*?)</h1>', raw, flags=re.I | re.S)
    if title_match:
        result["role"] = html_to_text(title_match.group(1))
    for match in re.finditer(
        r'<div[^>]*class=["\'][^"\']*term-description[^"\']*["\'][^>]*>(.*?)</div>\s*</div>\s*<div[^>]*class=["\'][^"\']*span8[^"\']*["\'][^>]*>\s*<p[^>]*>(.*?)</p>',
        raw,
        flags=re.I | re.S,
    ):
        key = html_to_text(match.group(1)).strip().lower()
        value = html_to_text(match.group(2)).strip()
        if key and value:
            result[key] = value
    if result.get("opening date"):
        result["posted_at"] = normalize_datetime(result["opening date"])
    if result.get("closing date"):
        result["updated_at"] = normalize_datetime(result["closing date"])
    if result.get("job number"):
        result["job_number"] = result["job number"]
    if result.get("location"):
        result["location"] = result["location"]
    result["_jd_text"] = html_to_text(raw)
    return result


def parse_governmentjobs_listing(raw: str, source: dict[str, Any], search_url: str, keyword: str) -> list[dict[str, Any]]:
    company = str(source.get("company") or "GovernmentJobs")
    candidates: list[dict[str, Any]] = []
    block_starts = list(
        re.finditer(
            r'<li[^>]*class=["\'][^"\']*list-item[^"\']*["\'][^>]*data-job-id=["\']([^"\']+)["\'][^>]*>',
            raw,
            flags=re.I | re.S,
        )
    )
    for index, block_match in enumerate(block_starts):
        job_id = html.unescape(block_match.group(1)).strip()
        next_start = block_starts[index + 1].start() if index + 1 < len(block_starts) else len(raw)
        block = raw[block_match.end() : next_start]
        link_match = re.search(r'<a[^>]*class=["\'][^"\']*item-details-link[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block, flags=re.I | re.S)
        if not link_match:
            continue
        url = normalize_job_url(urllib.parse.urljoin(search_url, html.unescape(link_match.group(1))))
        role = html_to_text(link_match.group(2)) or infer_role_from_url(url)
        meta_items = [html_to_text(item) for item in re.findall(r"<li[^>]*>(.*?)</li>", block, flags=re.I | re.S)]
        location = meta_items[0] if meta_items else ""
        category = next((item.replace("Category:", "").strip() for item in meta_items if "Category:" in item), "")
        department = next((item.replace("Department:", "").strip() for item in meta_items if "Department:" in item), "")
        summary_match = re.search(r'<div[^>]*class=["\'][^"\']*list-entry[^"\']*["\'][^>]*>(.*?)</div>', block, flags=re.I | re.S)
        summary = html_to_text(summary_match.group(1)) if summary_match else ""
        posted_text_match = re.search(r'<span[^>]*class=["\'][^"\']*list-entry-starts[^"\']*["\'][^>]*>\s*<span>(.*?)</span>', block, flags=re.I | re.S)
        posted_note = html_to_text(posted_text_match.group(1)) if posted_text_match else ""
        posted_at = parse_workday_posted_on(posted_note) if posted_note else ""
        notes = "GovernmentJobs/NEOGOV search adapter."
        if category:
            notes += f" Category: {category}."
        if department:
            notes += f" Department: {department}."
        if posted_note:
            notes += f" Listing says: {posted_note}."
        candidates.append(
            {
                "company": company,
                "role": role,
                "url": url,
                "platform": "governmentjobs",
                "location": location,
                "job_number": job_id,
                "external_job_id": job_id,
                "posted_at": posted_at,
                "updated_at": "",
                "source": source.get("url", search_url),
                "source_query": keyword,
                "freshness_source": "governmentjobs_listing_relative_posted_at" if posted_at else "unknown",
                "_jd_text": "\n\n".join(part for part in [role, location, summary] if part),
                "notes": notes,
            }
        )
    return candidates


def discover_governmentjobs_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    max_pages = int(source.get("max_pages", 2))
    max_all_pages = int(source.get("max_all_pages", max_pages))
    max_keyword_pages = int(source.get("max_keyword_pages", max_pages))
    max_detail_pages = int(source.get("max_detail_pages", 10))
    recent_days = int(source.get("crawl_recent_days", 14))
    candidates_by_url: dict[str, dict[str, Any]] = {}
    for query in governmentjobs_query_plan(source):
        keyword = query["keyword"]
        category = query["category"]
        if query.get("kind") == "all":
            page_limit = max_all_pages
        elif query.get("kind") == "keyword":
            page_limit = max_keyword_pages
        else:
            page_limit = max_pages
        for page in range(1, page_limit + 1):
            search_url = governmentjobs_search_url(source, keyword, page, category)
            try:
                raw = fetch_governmentjobs_search(search_url, source, timeout=30)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch GovernmentJobs search for {source.get('company', 'GovernmentJobs')}: {error}", file=sys.stderr)
                break
            page_candidates = parse_governmentjobs_listing(raw, source, search_url, query["label"])
            if not page_candidates:
                break
            for candidate in page_candidates:
                candidates_by_url[candidate["url"]] = candidate
            if governmentjobs_page_is_older_than(page_candidates, recent_days):
                break
            if "next-page" not in raw.lower() and f"page={page + 1}" not in raw:
                break
    detail_candidates = list(candidates_by_url.values())
    if str(source.get("track_hint") or "") == "traditional_it_wa":
        profile = {"_track": {"id": "traditional_it_wa"}}
        detail_candidates.sort(
            key=lambda candidate: (
                0 if maybe_backlog_title_relevant(candidate, profile) else 1,
                str(candidate.get("role") or "").lower(),
            )
        )
    for candidate in detail_candidates[:max_detail_pages]:
        job_id = str(candidate.get("external_job_id") or candidate.get("job_number") or "")
        if not job_id:
            continue
        detail_url = governmentjobs_newprint_url(str(candidate["url"]), job_id)
        try:
            detail_raw = fetch_url(detail_url, timeout=30)
        except Exception:
            continue
        detail = parse_governmentjobs_newprint(detail_raw)
        for key in ["role", "location", "job_number", "posted_at", "updated_at", "_jd_text"]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["freshness_source"] = "governmentjobs_newprint_opening_date" if candidate.get("posted_at") else candidate.get("freshness_source", "unknown")
        candidate["notes"] = f"{candidate.get('notes', '').rstrip()} Detail page: {detail_url}."
    return list(candidates_by_url.values())


def governmentjobs_global_search_url(
    source: dict[str, Any],
    page: int,
    keyword: str = "",
    category: str = "",
) -> str:
    parsed = urllib.parse.urlparse(str(source.get("url") or "https://www.governmentjobs.com/jobs"))
    params = {
        "keyword": keyword,
        "location": str(source.get("location") or "Washington"),
        "page": str(page),
        "sort": str(source.get("sort") or "PostingDate"),
        "isDescendingSort": "true" if truthy_source_flag(source.get("is_descending_sort"), default=True) else "false",
        "isPromotional": "False",
        "isTransfer": "False",
    }
    if category:
        params["category[0]"] = category
    organizations = source.get("organizations") or source.get("organization") or []
    if isinstance(organizations, str):
        organizations = [organizations]
    for index, organization in enumerate(organizations):
        value = str(organization).strip()
        if value:
            params[f"organization[{index}]"] = value
    return urllib.parse.urlunparse(
        parsed._replace(path="/jobs", query=urllib.parse.urlencode(params), fragment="")
    )


def governmentjobs_global_query_plan(source: dict[str, Any]) -> list[dict[str, str]]:
    categories = source.get("categories") or ["IT and Computers"]
    if isinstance(categories, str):
        categories = [categories]
    keywords = source.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    plan = [
        {"keyword": "", "category": str(category).strip(), "label": f"category:{str(category).strip()}", "kind": "category"}
        for category in categories
        if str(category).strip()
    ]
    plan.extend(
        {"keyword": str(keyword).strip(), "category": "", "label": str(keyword).strip(), "kind": "keyword"}
        for keyword in keywords
        if str(keyword).strip()
    )
    return plan


def parse_governmentjobs_global_listing(
    raw: str,
    source: dict[str, Any],
    search_url: str,
    source_query: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    block_starts = list(
        re.finditer(
            r'<li[^>]*class=["\'][^"\']*job-item[^"\']*["\'][^>]*data-job-id=["\']([^"\']+)["\'][^>]*>',
            raw,
            flags=re.I | re.S,
        )
    )
    for index, block_match in enumerate(block_starts):
        raw_job_id = html.unescape(block_match.group(1)).strip()
        job_id_match = re.match(r"(\d+)", raw_job_id)
        if not job_id_match:
            continue
        job_id = job_id_match.group(1)
        next_start = block_starts[index + 1].start() if index + 1 < len(block_starts) else len(raw)
        block = raw[block_match.end() : next_start]
        link_match = re.search(
            r'<a[^>]*class=["\'][^"\']*job-details-link[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            block,
            flags=re.I | re.S,
        )
        if not link_match:
            continue
        role = html_to_text(link_match.group(2)) or infer_role_from_url(link_match.group(1))
        organization_match = re.search(
            r'<div[^>]*class=["\'][^"\']*job-organization[^"\']*["\'][^>]*>(.*?)</div>',
            block,
            flags=re.I | re.S,
        )
        location_match = re.search(
            r'<span[^>]*class=["\'][^"\']*job-location[^"\']*["\'][^>]*>(.*?)</span>',
            block,
            flags=re.I | re.S,
        )
        candidates.append(
            {
                "company": html_to_text(organization_match.group(1)) if organization_match else str(source.get("company") or "GovernmentJobs"),
                "role": role,
                "url": normalize_job_url(urllib.parse.urljoin(search_url, html.unescape(link_match.group(1)))),
                "platform": "governmentjobs_global",
                "location": html_to_text(location_match.group(1)) if location_match else "",
                "job_number": job_id,
                "external_job_id": job_id,
                "posted_at": "",
                "updated_at": "",
                "source": source.get("url", search_url),
                "source_query": source_query,
                "freshness_source": "unknown",
                "_jd_text": "\n\n".join(part for part in [role, html_to_text(block)] if part),
                "notes": "GovernmentJobs statewide search listing; detail JSON-LD supplies the official posting date.",
            }
        )
    return candidates


def governmentjobs_global_detail(candidate: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    raw = fetch_url(str(candidate["url"]), timeout=timeout)
    parsed_jobs = parse_json_ld_jobs(raw, str(candidate["url"]), str(candidate.get("company") or "GovernmentJobs"))
    if not parsed_jobs:
        return candidate
    detail = parsed_jobs[0]
    for key in ["company", "role", "location", "posted_at", "updated_at", "_jd_text"]:
        if detail.get(key):
            candidate[key] = detail[key]
    candidate["freshness_source"] = "governmentjobs_json_ld_datePosted" if candidate.get("posted_at") else "unknown"
    candidate["notes"] = f"{candidate.get('notes', '').rstrip()} Parsed official JobPosting JSON-LD."
    return candidate


def discover_governmentjobs_global_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    max_pages = int(source.get("max_pages", 10))
    max_keyword_pages = int(source.get("max_keyword_pages", 1))
    candidates_by_url: dict[str, dict[str, Any]] = {}
    for query in governmentjobs_global_query_plan(source):
        page_limit = max_keyword_pages if query["kind"] == "keyword" else max_pages
        for page in range(1, page_limit + 1):
            search_url = governmentjobs_global_search_url(
                source,
                page,
                keyword=query["keyword"],
                category=query["category"],
            )
            try:
                raw = fetch_url(search_url, timeout=30)
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch GovernmentJobs statewide search: {error}", file=sys.stderr)
                break
            page_candidates = parse_governmentjobs_global_listing(raw, source, search_url, query["label"])
            if not page_candidates:
                break
            for candidate in page_candidates:
                candidates_by_url[candidate["url"]] = candidate
            if f"page={page + 1}" not in html.unescape(raw):
                break

    candidates = list(candidates_by_url.values())
    detail_limit = min(len(candidates), int(source.get("max_detail_pages", 120)))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    detail_timeout = int(source.get("detail_timeout", 20))
    failed_details = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
        future_to_candidate = {
            executor.submit(governmentjobs_global_detail, candidate, detail_timeout): candidate
            for candidate in candidates[:detail_limit]
        }
        for future in concurrent.futures.as_completed(future_to_candidate):
            try:
                future.result()
            except Exception:  # noqa: BLE001
                failed_details += 1
    if failed_details:
        print(
            f"GovernmentJobs statewide detail fetch failed for {failed_details}/{detail_limit} candidates.",
            file=sys.stderr,
        )
    return candidates


def discover_rss_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    feed_url = rss_feed_url(source)
    if not feed_url:
        return []
    try:
        raw = fetch_url(feed_url, timeout=30)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch RSS feed for {company}: {error}", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(raw)
        channel = root.find("channel")
        if channel is not None:
            items: list[Any] = channel.findall("item")
        else:
            items = [
                element
                for element in root.iter()
                if str(getattr(element, "tag", "")).rsplit("}", 1)[-1] in {"item", "entry"}
            ]
    except ImportError:
        items = rss_items_from_regex(raw)
    except ET.ParseError as error:
        print(f"Could not parse RSS feed for {company} with XML parser, using fallback: {error}", file=sys.stderr)
        items = rss_items_from_regex(raw)
    candidates: dict[str, dict[str, Any]] = {}
    for item in items:
        feed_location = ""
        feed_department = ""
        feed_categories: list[str] = []
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            link = str(item.get("link") or item.get("guid") or "").strip()
            description_raw = str(item.get("description") or "")
            pub_date = str(item.get("pubDate") or "")
            guid = str(item.get("guid") or link)
            cities = item.get("cities") if isinstance(item.get("cities"), list) else []
            states = item.get("states") if isinstance(item.get("states"), list) else []
            countries = item.get("countries") if isinstance(item.get("countries"), list) else []
            default_state = (
                str(states[0]).strip()
                if states
                else str(source.get("default_state") or "").strip()
            )
            feed_location = "; ".join(
                ", ".join(
                    part
                    for part in [str(city).strip(), default_state, str(countries[0]).strip() if countries else ""]
                    if part
                )
                for city in cities
                if str(city).strip()
            )
            feed_department = str(item.get("department") or "").strip()
            feed_categories = (
                [str(value).strip() for value in item.get("categories", []) if str(value).strip()]
                if isinstance(item.get("categories"), list)
                else []
            )
        else:
            title_values = rss_xml_texts_by_local_name(item, "title")
            title = title_values[0] if title_values else ""
            link = ""
            for element in item.iter():
                if str(getattr(element, "tag", "")).rsplit("}", 1)[-1] != "link":
                    continue
                link = str(getattr(element, "attrib", {}).get("href") or getattr(element, "text", "") or "").strip()
                if link:
                    break
            guid_values = merge_unique(
                rss_xml_texts_by_local_name(item, "guid"),
                rss_xml_texts_by_local_name(item, "id"),
            )
            guid = guid_values[0] if guid_values else link
            link = link or guid
            description_values = merge_unique(
                merge_unique(
                    rss_xml_texts_by_local_name(item, "description"),
                    rss_xml_texts_by_local_name(item, "content"),
                ),
                rss_xml_texts_by_local_name(item, "summary"),
            )
            description_raw = description_values[0] if description_values else ""
            publication_values = merge_unique(
                merge_unique(
                    rss_xml_texts_by_local_name(item, "pubDate"),
                    rss_xml_texts_by_local_name(item, "published"),
                ),
                rss_xml_texts_by_local_name(item, "updated"),
            )
            pub_date = publication_values[0] if publication_values else ""
            cities = rss_xml_texts_by_local_name(item, "city")
            states = rss_xml_texts_by_local_name(item, "state")
            countries = rss_xml_texts_by_local_name(item, "country")
            default_state = (
                states[0] if states else str(source.get("default_state") or "").strip()
            )
            location_parts = []
            for city in cities:
                location_parts.append(
                    ", ".join(
                        part
                        for part in [city, default_state, countries[0] if countries else ""]
                        if part
                    )
                )
            feed_location = "; ".join(location_parts)
            departments = rss_xml_texts_by_local_name(item, "department")
            feed_department = departments[0] if departments else ""
            feed_categories = rss_xml_texts_by_local_name(item, "category")
        category_field = str(source.get("rss_category_field") or "").strip().lower()
        if category_field == "location" and feed_categories and not feed_location:
            feed_location = "; ".join(feed_categories)
        elif category_field in {"department", "source_query"} and feed_categories and not feed_department:
            feed_department = "; ".join(feed_categories)
        if not title or not link:
            continue
        role, location = parse_rss_title(title)
        title_location_regex = str(source.get("title_location_regex") or "").strip()
        if title_location_regex:
            with contextlib.suppress(re.error):
                title_location_match = re.match(
                    title_location_regex,
                    title,
                    flags=re.I,
                )
                if title_location_match:
                    groups = title_location_match.groupdict()
                    role = str(groups.get("role") or role).strip()
                    location = str(groups.get("location") or location).strip()
        location = location or feed_location or str(source.get("default_location") or "").strip()
        location_pattern = str(
            source.get("location_include_regex") or ""
        ).strip()
        if location_pattern and not re.search(
            location_pattern,
            location,
            flags=re.I,
        ):
            continue
        description = html_to_text(description_raw)
        url = normalize_job_url(link)
        job_id_match = re.search(r"/jobs/(\d+)(?:[-/?#]|$)", link) or re.search(
            r"/(\d+)/(?:$|[?#])",
            link,
        )
        candidates[url] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": source.get("target_platform") or detect_platform(url),
            "location": location,
            "posted_at": normalize_datetime(pub_date),
            "updated_at": "",
            "source": source.get("url", feed_url),
            "source_query": source.get("category") or feed_department,
            "freshness_source": "rss_pubDate" if pub_date else "unknown",
            "job_number": job_id_match.group(1) if job_id_match else "",
            "external_job_id": job_id_match.group(1) if job_id_match else normalize_job_url(guid),
            "_jd_text": "\n\n".join(block for block in [role, location, description] if block),
            "notes": f"RSS feed adapter; feed={feed_url}",
        }
    if truthy_source_flag(source.get("fetch_details"), default=False):
        detail_limit = max(0, int(source.get("max_detail_pages", len(candidates))))
        detail_workers = max(1, int(source.get("detail_workers", 8)))
        selected = sorted(
            candidates.values(),
            key=lambda candidate: (
                0 if unclassified_technical_title_relevant(candidate) else 1,
                str(candidate.get("role") or "").lower(),
            ),
        )[:detail_limit]

        def enrich(candidate: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            try:
                detail_raw = fetch_url(candidate["url"])
            except Exception:
                return candidate["url"], None
            parsed = parse_json_ld_jobs(detail_raw, candidate["url"], str(company))
            return candidate["url"], parsed[0] if parsed else None

        if selected:
            with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
                for url, detail in executor.map(enrich, selected):
                    if not detail:
                        continue
                    candidate = candidates[url]
                    for key in ["role", "location", "posted_at", "updated_at"]:
                        if detail.get(key):
                            candidate[key] = detail[key]
                    if detail.get("_jd_text"):
                        candidate["_jd_text"] = detail["_jd_text"]
                    if detail.get("posted_at"):
                        candidate["freshness_source"] = "rss_json_ld_date_posted"
                    candidate["notes"] = (
                        f"RSS listing enriched from official detail-page JSON-LD; feed={feed_url}"
                    )
    return list(candidates.values())


def jibe_api_url(source: dict[str, Any]) -> str:
    if source.get("api_url"):
        return str(source["api_url"]).strip()
    source_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme and parsed.netloc:
        return urllib.parse.urljoin(f"{parsed.scheme}://{parsed.netloc}", "/api/jobs")
    return ""


def discover_jibe_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    api_url = jibe_api_url(source)
    if not api_url:
        return []
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    categories = source.get("categories") or []
    if isinstance(categories, str):
        categories = [categories]
    search_params = source.get("search_params") if isinstance(source.get("search_params"), dict) else {}
    query_keywords = [""] if truthy_source_flag(source.get("search_all"), default=False) else [
        str(item) for item in keywords if str(item).strip()
    ]
    page_size = max(1, int(source.get("page_size", 100)))
    max_pages = max(1, int(source.get("max_pages", 3)))
    candidates: dict[str, dict[str, Any]] = {}
    for keyword in query_keywords:
        for page in range(1, max_pages + 1):
            params = {
                **{str(key): str(value) for key, value in search_params.items() if str(key).strip()},
                "sortBy": "posted_date",
                "limit": page_size,
                "numRows": page_size,
                "page": page,
            }
            if keyword:
                params["keywords"] = keyword
            if categories:
                params["categories"] = "|".join(str(item) for item in categories if str(item).strip())
            try:
                data = fetch_json(f"{api_url}?{urllib.parse.urlencode(params)}")
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch Jibe API for {company}: {error}", file=sys.stderr)
                break
            jobs = data.get("jobs") if isinstance(data, dict) else []
            if not isinstance(jobs, list):
                break
            for wrapper in jobs:
                job = wrapper.get("data") if isinstance(wrapper, dict) else wrapper
                if not isinstance(job, dict):
                    continue
                title = str(job.get("title") or infer_role_from_url(str(job.get("slug") or ""))).strip()
                job_url = str(job.get("meta_data", {}).get("canonical_url") if isinstance(job.get("meta_data"), dict) else "")
                if not job_url:
                    source_url = str(source.get("url") or api_url)
                    job_url = urllib.parse.urljoin(source_url, f"/jobs/{urllib.parse.quote(str(job.get('slug') or job.get('req_id') or ''))}")
                job_url = normalize_job_url(job_url)
                location = compact_location_text(
                    {
                        "city": job.get("city"),
                        "state": job.get("state"),
                        "country": job.get("country"),
                        "name": job.get("location_name"),
                    }
                )
                job_id = str(job.get("req_id") or job.get("slug") or "").strip()
                meta_data = job.get("meta_data", {}) if isinstance(job.get("meta_data"), dict) else {}
                icims_meta = meta_data.get("icims", {}) if isinstance(meta_data.get("icims"), dict) else {}
                candidates[job_url] = {
                    "company": company,
                    "role": title,
                    "url": job_url,
                    "platform": "jibe",
                    "location": location or compact_location_text(job.get("locations")),
                    "job_number": job_id,
                    "external_job_id": str(icims_meta.get("uuid") or job_id),
                    "posted_at": normalize_datetime(job.get("posted_date") or job.get("create_date")),
                    "updated_at": normalize_datetime(job.get("update_date") or meta_data.get("last_mod")),
                    "source": source.get("url", api_url),
                    "source_query": keyword or "all",
                    "notes": "Jibe/iCIMS hosted careers API adapter.",
                    "_jd_text": html_to_text(str(job.get("description") or "")),
                }
            total_count = int(data.get("totalCount") or data.get("count") or 0) if isinstance(data, dict) else 0
            if not jobs or len(jobs) < page_size or (total_count and page * page_size >= total_count):
                break
    return list(candidates.values())


def discover_jazzhr_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    if not board_url:
        return []
    if not board_url.rstrip("/").endswith("/apply/jobs"):
        parsed = urllib.parse.urlparse(board_url)
        board_url = urllib.parse.urlunparse(parsed._replace(path="/apply/jobs", params="", query="", fragment=""))
    try:
        raw = fetch_url(board_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch JazzHR board for {company}: {error}", file=sys.stderr)
        return []

    candidates: dict[str, dict[str, Any]] = {}
    link_pattern = re.compile(
        r'<a[^>]+class=["\'][^"\']*job_title_link[^"\']*["\'][^>]+href=["\']([^"\']*/apply/jobs/details/([^?"\']+)[^"\']*)["\'][^>]*>(.*?)</a>',
        flags=re.I | re.S,
    )
    for match in link_pattern.finditer(raw):
        detail_url = normalize_job_url(urllib.parse.urljoin(board_url, html.unescape(match.group(1))))
        job_id = match.group(2).strip()
        row_end = raw.find("</tr>", match.end())
        row = raw[match.start() : row_end + 5] if row_end >= 0 else raw[max(0, match.start() - 400) : match.end() + 800]
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)
        location = html_to_text(cells[-1]) if cells else ""
        candidates[detail_url] = {
            "company": company,
            "role": html_to_text(match.group(3)) or infer_role_from_url(detail_url),
            "url": detail_url,
            "platform": "jazzhr",
            "location": location,
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": "",
            "updated_at": "",
            "source": source.get("url", board_url),
            "source_query": "all_jobs",
            "freshness_source": "unknown",
            "notes": "JazzHR public board; detail JSON-LD may add the official posted date.",
        }

    detail_limit = int(source.get("max_detail_pages", len(candidates)))
    for candidate in list(candidates.values())[:detail_limit]:
        try:
            detail_raw = fetch_url(candidate["url"])
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch JazzHR detail for {company} job {candidate['external_job_id']}: {error}", file=sys.stderr)
            continue
        details = parse_json_ld_jobs(detail_raw, candidate["url"], str(company))
        if not details:
            continue
        detail = details[0]
        for key in ["role", "location", "job_number", "external_job_id", "posted_at", "updated_at", "_jd_text"]:
            if detail.get(key):
                candidate[key] = detail[key]
        if detail.get("url"):
            candidate["url"] = detail["url"]
        candidate["platform"] = "jazzhr"
        candidate["freshness_source"] = "jazzhr_json_ld_date_posted" if candidate.get("posted_at") else "unknown"
        candidate["notes"] = "JazzHR public board enriched from detail-page JobPosting JSON-LD."
    return list(candidates.values())


def discover_hiringthing_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    if not board_url:
        return []
    try:
        raw = fetch_url(board_url, timeout=int(source.get("board_timeout", 25)))
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch HiringThing board for {company}: {error}", file=sys.stderr)
        return []

    block_starts = list(
        re.finditer(
            r'<div[^>]*class=["\'][^"\']*\bjob-container\b[^"\']*["\'][^>]*data-job-id=["\']([^"\']+)["\'][^>]*>',
            raw,
            flags=re.I | re.S,
        )
    )
    candidates: dict[str, dict[str, Any]] = {}
    for index, block_match in enumerate(block_starts):
        external_id = html.unescape(block_match.group(1)).strip()
        next_start = block_starts[index + 1].start() if index + 1 < len(block_starts) else len(raw)
        block = raw[block_match.start() : next_start]
        link_match = re.search(
            rf'<a[^>]+href=["\']([^"\']*/job/{re.escape(external_id)}/[^"\']*)["\']',
            block,
            flags=re.I | re.S,
        )
        title_match = re.search(r"<h2\b[^>]*>(.*?)</h2>", block, flags=re.I | re.S)
        if not link_match or not title_match:
            continue
        detail_url = normalize_job_url(
            urllib.parse.urljoin(board_url, html.unescape(link_match.group(1)))
        )
        location_match = re.search(
            r'<div[^>]*class=["\'][^"\']*\bjob-location\b[^"\']*["\'][^>]*>(.*?)</div>',
            block,
            flags=re.I | re.S,
        )
        category_match = re.search(
            r'<div[^>]*class=["\'][^"\']*\bjob-category\b[^"\']*["\'][^>]*>.*?<span[^>]*>(.*?)</span>',
            block,
            flags=re.I | re.S,
        )
        summary_match = re.search(
            r'<div[^>]*class=["\'][^"\']*\bjob-description\b[^"\']*["\'][^>]*>(.*?)</div>',
            block,
            flags=re.I | re.S,
        )
        category = html_to_text(category_match.group(1)) if category_match else ""
        candidates[detail_url] = {
            "company": company,
            "role": html_to_text(title_match.group(1)) or infer_role_from_url(detail_url),
            "url": detail_url,
            "platform": "hiringthing",
            "location": html_to_text(location_match.group(1)) if location_match else "",
            "job_number": external_id,
            "external_job_id": external_id,
            "posted_at": "",
            "updated_at": "",
            "source": board_url,
            "source_query": category or "all_jobs",
            "freshness_source": "unknown",
            "notes": "HiringThing public board; detail JSON-LD supplies the official posted date.",
            "_jd_text": html_to_text(summary_match.group(1)) if summary_match else "",
        }

    detail_limit = max(0, min(len(candidates), int(source.get("max_detail_pages", 100))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    detail_timeout = int(source.get("detail_timeout", 20))

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(str(candidate["url"]), timeout=detail_timeout)
        except Exception as error:  # noqa: BLE001
            print(
                f"Could not fetch HiringThing detail for {company} job "
                f"{candidate['external_job_id']}: {error}",
                file=sys.stderr,
            )
            return
        details = parse_json_ld_jobs(detail_raw, str(candidate["url"]), company)
        if not details:
            return
        detail = details[0]
        for key in ["role", "location", "posted_at", "updated_at", "_jd_text"]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["platform"] = "hiringthing"
        candidate["freshness_source"] = (
            "hiringthing_json_ld_date_posted" if candidate.get("posted_at") else "unknown"
        )
        candidate["notes"] = (
            "HiringThing public board enriched from detail-page JobPosting JSON-LD."
        )

    detail_candidates = list(candidates.values())[:detail_limit]
    if detail_candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            list(executor.map(enrich, detail_candidates))
    return list(candidates.values())


def discover_paycor_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    if not board_url:
        return []
    raw = fetch_url(board_url, timeout=int(source.get("timeout", 25)))
    row_pattern = re.compile(
        r'<div[^>]*class=["\'][^"\']*\bgnewtonCareerGroupRowClass\b[^"\']*["\'][^>]*>'
        r'.*?<a[^>]+href=["\'](?P<url>[^"\']*JobIntroduction\.action[^"\']*)["\'][^>]*>'
        r'(?P<title>.*?)</a>'
        r'.*?<div[^>]*class=["\'][^"\']*\bgnewtonCareerGroupJobDescriptionClass\b'
        r'[^"\']*["\'][^>]*>(?P<location>.*?)</div>',
        flags=re.I | re.S,
    )
    candidates: dict[str, dict[str, Any]] = {}
    for match in row_pattern.finditer(raw):
        detail_url = normalize_job_url(
            urllib.parse.urljoin(board_url, html.unescape(match.group("url")))
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(detail_url).query)
        external_id = str((query.get("id") or [detail_url])[0]).strip()
        candidates[detail_url] = {
            "company": company,
            "role": html_to_text(match.group("title")) or infer_role_from_url(detail_url),
            "url": detail_url,
            "platform": "paycor",
            "location": html_to_text(match.group("location")),
            "job_number": external_id,
            "external_job_id": external_id,
            "posted_at": "",
            "updated_at": "",
            "source": board_url,
            "source_query": "all_jobs",
            "freshness_source": "unknown",
            "notes": "Paycor/Newton public careers board; freshness uses first_seen.",
        }

    detail_limit = max(0, min(len(candidates), int(source.get("max_detail_pages", 40))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    detail_timeout = int(source.get("detail_timeout", 20))

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(str(candidate["url"]), timeout=detail_timeout)
        except Exception as error:  # noqa: BLE001
            print(
                f"Could not fetch Paycor detail for {company} job "
                f"{candidate['external_job_id']}: {error}",
                file=sys.stderr,
            )
            return
        description_match = re.search(
            r'<td[^>]*id=["\']gnewtonJobDescriptionText["\'][^>]*>(.*?)</td>',
            detail_raw,
            flags=re.I | re.S,
        )
        if description_match:
            candidate["_jd_text"] = html_to_text(description_match.group(1))
        location_match = re.search(
            r"<b>\s*Location:\s*</b>\s*(.*?)(?:</div>|<br\s*/?>)",
            detail_raw,
            flags=re.I | re.S,
        )
        if location_match:
            candidate["location"] = html_to_text(location_match.group(1))

    selected = list(candidates.values())[:detail_limit]
    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            list(executor.map(enrich, selected))
    return list(candidates.values())


def prismhr_client_ids(source: dict[str, Any]) -> list[str]:
    configured = source.get("client_ids")
    if isinstance(configured, list):
        client_ids = [
            str(item).strip()
            for item in configured
            if str(item).strip()
        ]
    else:
        client_id = str(
            source.get("client_id") or source.get("clientId") or ""
        ).strip()
        client_ids = [client_id] if client_id else []
    if client_ids:
        return list(dict.fromkeys(client_ids))

    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    raw = fetch_url(source_url, timeout=int(source.get("timeout", 25)))
    matches = re.findall(
        r'id=["\']agileHrJobList["\'][^>]*data-id=["\']([^"\']+)',
        raw,
        flags=re.I,
    )
    discovered = [
        item.strip()
        for match in matches
        for item in match.split("|")
        if item.strip()
    ]
    return list(dict.fromkeys(discovered))


def discover_prismhr_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    client_ids = prismhr_client_ids(source)
    if not client_ids:
        return []
    endpoint = str(
        source.get("api_url")
        or "https://setup.prismhrtalent.com/public/api/JobPosting/RequisitionsByClient"
    ).strip()
    timeout = int(source.get("timeout", 25))
    candidates: dict[str, dict[str, Any]] = {}
    for client_id in client_ids:
        data = fetch_json_form_post(
            endpoint,
            {"clientIds[]": client_id},
            timeout=timeout,
        )
        if not isinstance(data, dict) or not data.get("Success"):
            continue
        records = data.get("ResultList")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            external_id = str(record.get("Id") or "").strip()
            title = html_to_text(str(record.get("Title") or ""))
            apply_url = normalize_job_url(str(record.get("ApplyUrl") or ""))
            if not external_id or not title or not apply_url:
                continue
            location = html_to_text(
                str(record.get("Location") or record.get("City") or "")
            )
            state = str(record.get("State") or "").strip()
            if not location and state:
                location = state
            location_filter = str(
                source.get("location_include_regex") or ""
            ).strip()
            location_haystack = " | ".join(
                str(record.get(key) or "")
                for key in ["Location", "City", "State", "LongDescriptionFlat"]
            )
            if location_filter and not re.search(
                location_filter,
                location_haystack,
                flags=re.I,
            ):
                continue
            description = "\n".join(
                html_to_text(str(record.get(key) or ""))
                for key in [
                    "Description",
                    "AdditionalInformation",
                    "Disclaimer",
                ]
                if record.get(key)
            )
            candidates[external_id] = {
                "company": company,
                "role": title,
                "url": apply_url,
                "platform": "prismhr",
                "location": location,
                "job_number": external_id,
                "external_job_id": external_id,
                "posted_at": normalize_datetime(record.get("OpenDate")),
                "updated_at": "",
                "source": source_url or endpoint,
                "source_query": html_to_text(
                    str(record.get("Department") or record.get("Division") or "all_jobs")
                ),
                "freshness_source": "prismhr_open_date",
                "notes": "PrismHR Talent public JobPosting API adapter.",
                "_jd_text": description,
            }
    return list(candidates.values())


def discover_wp_search_index_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    api_url = str(source.get("api_url") or source_url).strip()
    if not api_url:
        return []
    records = fetch_json(api_url)
    if not isinstance(records, list):
        return []
    required_terms = [
        str(item).strip().casefold()
        for item in source.get("required_terms", [])
        if str(item).strip()
    ]
    base_url = str(source.get("base_url") or source_url or api_url)
    location_taxonomies = [
        str(item).strip()
        for item in source.get("location_taxonomies", [])
        if str(item).strip()
    ]
    candidates: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        terms = record.get("terms") if isinstance(record.get("terms"), dict) else {}
        term_names = [
            html_to_text(str(term.get("name") or ""))
            for values in terms.values()
            if isinstance(values, list)
            for term in values
            if isinstance(term, dict) and term.get("name")
        ]
        searchable = " ".join(
            [
                json.dumps(record.get("title") or "", ensure_ascii=False),
                json.dumps(record.get("excerpt") or "", ensure_ascii=False),
                *term_names,
            ]
        ).casefold()
        if required_terms and not all(term in searchable for term in required_terms):
            continue
        title_value = record.get("title")
        if isinstance(title_value, dict):
            title_value = title_value.get("rendered")
        role = html_to_text(str(title_value or ""))
        link = normalize_job_url(
            urllib.parse.urljoin(base_url, html.unescape(str(record.get("link") or "")))
        )
        if not role or not link:
            continue
        location_names: list[str] = []
        for taxonomy, values in terms.items():
            if location_taxonomies:
                is_location = taxonomy in location_taxonomies
            else:
                lowered = taxonomy.casefold()
                is_location = any(token in lowered for token in ("location", "site", "geographical"))
            if not is_location or not isinstance(values, list):
                continue
            location_names.extend(
                html_to_text(str(term.get("name") or ""))
                for term in values
                if isinstance(term, dict) and term.get("name")
            )
        excerpt = record.get("excerpt")
        if isinstance(excerpt, dict):
            excerpt = excerpt.get("rendered")
        content = record.get("content")
        if isinstance(content, dict):
            content = content.get("rendered")
        external_id = str(record.get("reference") or record.get("id") or link)
        candidates[link] = {
            "company": company,
            "role": role,
            "url": link,
            "platform": "wp_search_index",
            "location": ", ".join(merge_unique(location_names, [])),
            "job_number": external_id,
            "external_job_id": external_id,
            "posted_at": normalize_datetime(
                record.get("date_formatted")
                or record.get("date")
                or record.get("published_at")
            ),
            "updated_at": normalize_datetime(record.get("modified") or record.get("updated_at")),
            "source": source_url or api_url,
            "source_query": ", ".join(required_terms) or "all_jobs",
            "freshness_source": "wp_search_index_official_date",
            "notes": "Official WordPress job search index.",
            "_jd_text": "\n\n".join(
                item for item in [html_to_text(str(excerpt or "")), html_to_text(str(content or ""))] if item
            ),
        }
    return list(candidates.values())


def discover_joveo_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    api_url = str(source.get("api_url") or "").strip()
    if not api_url:
        return []
    search_terms = source.get("search_terms", [""])
    if isinstance(search_terms, str):
        search_terms = [search_terms]
    page_size = max(1, min(int(source.get("page_size", 100)), 100))
    max_pages = max(1, int(source.get("max_pages", 5)))
    timeout = int(source.get("timeout", 30))
    public_base_url = str(source.get("public_base_url") or source.get("url") or "").rstrip("/")
    filters = source.get("filters") if isinstance(source.get("filters"), list) else []
    if not filters and source.get("latitude") is not None and source.get("longitude") is not None:
        filters = [
            {
                "field": "point",
                "type": "GEO_LOCATION",
                "entity": "job",
                "operator": "GEO_LOCATION_WITHIN",
                "combination": "OR",
                "filterValue": {
                    "latitude": float(source["latitude"]),
                    "longitude": float(source["longitude"]),
                    "distance": float(source.get("distance", 50)),
                    "distanceUnit": str(source.get("distance_unit") or "Miles"),
                },
            }
        ]
    candidates: dict[str, dict[str, Any]] = {}
    for raw_term in search_terms:
        search_term = str(raw_term).strip()
        for page_number in range(max_pages):
            payload = {
                "searchTerm": search_term,
                "orderBy": [
                    {
                        "key": "startDate",
                        "entity": "job",
                        "order": "DESC",
                        "type": "DATE",
                    }
                ],
                "searchFields": source.get(
                    "search_fields",
                    {"title": 10, "country": 9, "category": 8},
                ),
                "filters": filters,
                "facetFields": source.get(
                    "facet_fields",
                    ["category", "normalisedFields.city", "normalisedFields.state"],
                ),
                "pageSize": page_size,
                "pageNumber": page_number,
                "filterIds": source.get("filter_ids", []),
            }
            data = fetch_json_post(api_url, payload, timeout=timeout)
            records = data.get("records", []) if isinstance(data, dict) else []
            if not isinstance(records, list) or not records:
                break
            for record in records:
                if not isinstance(record, dict):
                    continue
                normalized = (
                    record.get("normalisedFields")
                    if isinstance(record.get("normalisedFields"), dict)
                    else {}
                )
                external_id = str(
                    record.get("externalId")
                    or record.get("referenceNumber")
                    or record.get("id")
                    or ""
                ).strip()
                slug = str(record.get("urlSlug") or "").strip()
                public_url = (
                    normalize_job_url(f"{public_base_url}/job/{urllib.parse.quote(slug)}")
                    if public_base_url and slug
                    else normalize_job_url(str(record.get("url") or ""))
                )
                if not public_url:
                    public_url = normalize_job_url(
                        str(record.get("externalUrl") or record.get("careerSiteApplyUrl") or "")
                    )
                if not public_url or not external_id:
                    continue
                city = str(normalized.get("city") or record.get("city") or "").strip()
                state = str(
                    normalized.get("stateCode")
                    or normalized.get("state")
                    or record.get("state")
                    or ""
                ).strip()
                country = str(
                    normalized.get("countryCode")
                    or normalized.get("country")
                    or record.get("country")
                    or ""
                ).strip()
                apply_url = normalize_job_url(
                    str(
                        record.get("careerSiteApplyUrl")
                        or record.get("externalApplyUrl")
                        or record.get("externalUrl")
                        or record.get("applyUrl")
                        or ""
                    )
                )
                candidates[external_id] = {
                    "company": company,
                    "role": str(normalized.get("title") or record.get("title") or "").strip(),
                    "url": public_url,
                    "apply_url": apply_url,
                    "platform": "joveo",
                    "location": ", ".join(part for part in [city, state, country] if part),
                    "job_number": str(record.get("referenceNumber") or external_id),
                    "external_job_id": external_id,
                    "posted_at": normalize_datetime(
                        record.get("startDate")
                        or record.get("createdAt")
                    ),
                    "updated_at": normalize_datetime(record.get("updatedAt")),
                    "source": source.get("url", api_url),
                    "source_query": search_term or "all_jobs",
                    "freshness_source": "joveo_start_date",
                    "notes": "Joveo public careers search API.",
                    "_jd_text": "\n\n".join(
                        text
                        for text in [
                            html_to_text(str(record.get("description") or "")),
                            html_to_text(str(record.get("responsibilities") or "")),
                            html_to_text(str(record.get("qualifications") or "")),
                        ]
                        if text
                    ),
                }
            total_pages = int(data.get("totalPages") or 0) if isinstance(data, dict) else 0
            if page_number + 1 >= total_pages or len(records) < page_size:
                break
    return list(candidates.values())


def regex_group_text(match: re.Match[str] | None) -> str:
    if match is None:
        return ""
    groups = match.groupdict()
    value = groups.get("title") or (match.group(1) if match.lastindex else match.group(0))
    return html_to_text(html.unescape(value))


def discover_embedded_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    title_regex = str(source.get("title_regex") or "").strip()
    url_regex = str(source.get("job_url_regex") or "").strip()
    job_url_template = str(source.get("job_url_template") or "").strip()
    if not source_url or not title_regex or (not url_regex and not job_url_template):
        return []
    try:
        raw = fetch_url(source_url, timeout=int(source.get("timeout", 25)))
        title_pattern = re.compile(title_regex, flags=re.I | re.S)
        url_pattern = re.compile(url_regex, flags=re.I | re.S) if url_regex else None
        date_pattern = (
            re.compile(str(source["date_regex"]), flags=re.I | re.S)
            if source.get("date_regex")
            else None
        )
        location_pattern = (
            re.compile(str(source["location_regex"]), flags=re.I | re.S)
            if source.get("location_regex")
            else None
        )
        description_pattern = (
            re.compile(str(source["description_regex"]), flags=re.I | re.S)
            if source.get("description_regex")
            else None
        )
        id_pattern = (
            re.compile(str(source["job_id_regex"]), flags=re.I | re.S)
            if source.get("job_id_regex")
            else None
        )
    except (re.error, urllib.error.URLError, TimeoutError) as error:
        print(f"Could not parse embedded jobs for {company}: {error}", file=sys.stderr)
        return []

    title_matches = list(title_pattern.finditer(raw))
    default_location = str(source.get("default_location") or "").strip()
    candidates: dict[str, dict[str, Any]] = {}
    for index, title_match in enumerate(title_matches):
        block_start = (
            title_matches[index - 1].end()
            if index
            else max(0, title_match.start() - int(source.get("first_block_lookbehind", 1500)))
        )
        block_end = (
            title_matches[index + 1].start()
            if index + 1 < len(title_matches)
            else min(len(raw), title_match.end() + int(source.get("last_block_lookahead", 6000)))
        )
        block = raw[block_start:block_end]
        title_offset = title_match.start() - block_start
        after_title = block[title_offset:]
        title_groups = title_match.groupdict()
        external_id = html.unescape(str(title_groups.get("job_id") or "")).strip()
        if job_url_template and external_id:
            raw_detail_url = job_url_template.format(
                job_id=urllib.parse.quote(external_id),
                job_id_raw=external_id,
            )
        else:
            url_match = url_pattern.search(after_title) if url_pattern else None
            if not url_match:
                continue
            raw_detail_url = html.unescape(url_match.group(1))
        role = regex_group_text(title_match)
        detail_url = normalize_job_url(
            urllib.parse.urljoin(source_url, raw_detail_url)
        )
        if not role or not detail_url:
            continue

        posted_at = normalize_datetime(
            html.unescape(
                str(title_groups.get("posted_at") or title_groups.get("date") or "")
            ).strip()
        )
        if date_pattern and not posted_at:
            date_matches = list(date_pattern.finditer(block[:title_offset]))
            if date_matches:
                posted_at = normalize_datetime(regex_group_text(date_matches[-1]))
        location = html_to_text(str(title_groups.get("location") or "")).strip()
        location = location or default_location
        if location_pattern and not location:
            location_match = location_pattern.search(after_title)
            if location_match:
                location = regex_group_text(location_match)
        description = html_to_text(str(title_groups.get("description") or "")).strip()
        if description_pattern and not description:
            description_match = description_pattern.search(after_title)
            if description_match:
                description = regex_group_text(description_match)
        if not external_id and id_pattern:
            id_match = id_pattern.search(detail_url)
            if id_match:
                external_id = html.unescape(id_match.group(1) if id_match.lastindex else id_match.group(0))
        candidates[detail_url] = {
            "company": company,
            "role": role,
            "url": detail_url,
            "platform": "embedded_jobs",
            "location": location,
            "job_number": external_id,
            "external_job_id": external_id,
            "posted_at": posted_at,
            "updated_at": "",
            "source": source_url,
            "source_query": str(source.get("source_query") or "company_careers"),
            "freshness_source": "company_careers_posted_date" if posted_at else "unknown",
            "notes": "Structured job card parsed from the official company careers page.",
            "_jd_text": description,
        }

    if not truthy_source_flag(source.get("fetch_details"), default=False):
        return list(candidates.values())
    detail_limit = max(0, min(len(candidates), int(source.get("max_detail_pages", 40))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    detail_timeout = int(source.get("detail_timeout", 20))

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(str(candidate["url"]), timeout=detail_timeout)
        except Exception:
            return
        details = parse_json_ld_jobs(detail_raw, str(candidate["url"]), company)
        if not details:
            return
        detail = details[0]
        for key in ["role", "location", "posted_at", "updated_at", "_jd_text"]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["platform"] = "embedded_jobs"
        if candidate.get("posted_at"):
            candidate["freshness_source"] = "company_detail_json_ld_date_posted"
        candidate["notes"] = (
            "Structured company careers card enriched from detail-page JobPosting JSON-LD."
        )

    detail_candidates = list(candidates.values())[:detail_limit]
    if detail_candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            list(executor.map(enrich, detail_candidates))
    return list(candidates.values())


def discover_wordpress_taleo_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    if not board_url:
        return []
    try:
        raw = fetch_url(board_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch WordPress Taleo board for {company}: {error}", file=sys.stderr)
        return []

    nonce_id = str(source.get("nonce_input_id") or "nonce4")
    nonce_match = re.search(
        rf'<input[^>]+id=["\']{re.escape(nonce_id)}["\'][^>]+value=["\']([^"\']+)["\']',
        raw,
        flags=re.I,
    )
    if not nonce_match:
        print(f"WordPress Taleo board for {company} did not expose nonce input {nonce_id}", file=sys.stderr)
        return []
    parsed = urllib.parse.urlparse(board_url)
    ajax_url = str(source.get("api_url") or urllib.parse.urljoin(f"{parsed.scheme}://{parsed.netloc}", "/wp-admin/admin-ajax.php"))
    action = str(source.get("ajax_action") or "get_jobs_data")
    try:
        data = fetch_json_form_post(ajax_url, {"action": action, "nonce": nonce_match.group(1)}, timeout=30)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch WordPress Taleo jobs for {company}: {error}", file=sys.stderr)
        return []

    raw_locations = data.get("jobLocations", []) if isinstance(data, dict) else []
    locations = {
        str(item.get("id")): compact_location_text(
            {
                "name": item.get("locationName"),
                "city": item.get("city"),
                "state": item.get("state"),
                "country": item.get("countryCode"),
            }
        )
        for item in raw_locations
        if isinstance(item, dict) and item.get("id") is not None
    }
    candidates: dict[str, dict[str, Any]] = {}
    for job in data.get("jobs", []) if isinstance(data, dict) else []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "").strip()
        url = normalize_job_url(str(job.get("link") or ""))
        if not url and job_id:
            url = normalize_job_url(urllib.parse.urljoin(board_url.rstrip("/") + "/", job_id))
        if not url:
            continue
        location = locations.get(str(job.get("location")), compact_location_text(job.get("location")))
        families = job.get("jobFamily") if isinstance(job.get("jobFamily"), list) else []
        candidates[url] = {
            "company": company,
            "role": str(job.get("title") or infer_role_from_url(url)).strip(),
            "url": url,
            "platform": "wordpress_taleo",
            "location": location,
            "job_number": job_id,
            "external_job_id": job_id,
            "posted_at": normalize_datetime(job.get("posted_at") or job.get("postedDate")),
            "updated_at": normalize_datetime(job.get("updated_at") or job.get("updatedDate")),
            "source": source.get("url", board_url),
            "source_query": "; ".join(str(item) for item in families if item),
            "freshness_source": "wordpress_taleo_date" if job.get("posted_at") or job.get("postedDate") else "unknown",
            "notes": "Public WordPress wrapper around a Taleo job feed.",
            "_jd_text": html_to_text(str(job.get("description") or "")),
        }
    return list(candidates.values())


def talentbrew_results_url(source: dict[str, Any]) -> str:
    if source.get("results_url"):
        return str(source["results_url"]).strip()
    source_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme and parsed.netloc:
        return urllib.parse.urljoin(f"{parsed.scheme}://{parsed.netloc}", "/en/search-jobs/results")
    return ""


def talentbrew_detail_from_url(url: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = fetch_url(url, timeout=15)
    except Exception:
        return {}
    jobs = parse_json_ld_jobs(raw, url, str(fallback.get("company") or "Unknown Company"))
    if not jobs:
        return {}
    detail = jobs[0]
    return {
        "posted_at": detail.get("posted_at", ""),
        "updated_at": detail.get("updated_at", ""),
        "job_number": detail.get("job_number", "") or fallback.get("job_number", ""),
        "external_job_id": detail.get("external_job_id", "") or fallback.get("external_job_id", ""),
        "_jd_text": html_to_text(raw),
    }


def parse_talentbrew_result_items(
    result_html: str,
    base_url: str,
    company: str,
    source_url: str,
    source_query: str,
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    item_matches = list(
        re.finditer(
            r'<li\b[^>]*class=["\'][^"\']*branded-list__list-item[^"\']*["\'][^>]*>(.*?)</li>',
            result_html,
            flags=re.I | re.S,
        )
    )
    if not item_matches:
        item_matches = list(re.finditer(r"<li\b[^>]*>(.*?)</li>", result_html, flags=re.I | re.S))
    for item_match in item_matches:
        item = item_match.group(1)
        link_match = re.search(
            r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*data-job-id=["\']([^"\']+)["\']',
            item,
            flags=re.I | re.S,
        )
        if not link_match:
            continue
        url = normalize_job_url(urllib.parse.urljoin(base_url, html.unescape(link_match.group(1))))
        role_match = re.search(r"<h[23]\b[^>]*>(.*?)</h[23]>", item, flags=re.I | re.S)
        role = html_to_text(role_match.group(1)) if role_match else infer_role_from_url(url)
        location_match = re.search(
            r'<span\b[^>]*class=["\'][^"\']*job-location[^"\']*["\'][^>]*>(.*?)</span>',
            item,
            flags=re.I | re.S,
        )
        if not location_match:
            location_match = re.search(
                r'<div\b[^>]*class=["\'][^"\']*job-result[^"\']*["\'][^>]*>'
                r'.*?</h[23]>\s*<p\b[^>]*>(.*?)</p>',
                item,
                flags=re.I | re.S,
            )
        list_posted_at_match = re.search(
            r'<span\b[^>]*class=["\'][^"\']*job-date-posted[^"\']*["\'][^>]*>(.*?)</span>',
            item,
            flags=re.I | re.S,
        )
        list_posted_at = (
            normalize_datetime(html_to_text(list_posted_at_match.group(1)))
            if list_posted_at_match
            else ""
        )
        candidates[url] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "talentbrew",
            "location": html_to_text(location_match.group(1)) if location_match else "",
            "job_number": str(link_match.group(2)),
            "external_job_id": str(link_match.group(2)),
            "posted_at": list_posted_at,
            "updated_at": "",
            "source": source_url,
            "source_query": source_query,
            "freshness_source": "talentbrew_result_date" if list_posted_at else "unknown",
            "notes": "TalentBrew search adapter; detail pages can supply JobPosting JSON-LD.",
            "_jd_text": "",
        }
    return candidates


def talentbrew_candidate_matches_source(
    source: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    required_locations = source.get("required_locations") or []
    if isinstance(required_locations, str):
        required_locations = [required_locations]
    required_locations = [
        str(item).strip().lower()
        for item in required_locations
        if str(item).strip()
    ]
    if not required_locations:
        return True
    location = str(candidate.get("location") or "").strip().lower()
    return any(required_location in location for required_location in required_locations)


def talentbrew_browse_page_url(source: dict[str, Any], page: int) -> str:
    template = str(source.get("browse_url_template") or "").strip()
    if template:
        return template.format(page=page)
    source_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(source_url)
    path = re.sub(r"/\d+/?$", f"/{page}", parsed.path.rstrip("/"))
    return urllib.parse.urlunparse(parsed._replace(path=path, query="", fragment=""))


def discover_talentbrew_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    results_url = talentbrew_results_url(source)
    if not results_url:
        return []
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    organization_ids = str(source.get("organization_ids") or source.get("org_id") or "").strip()
    page_size = int(source.get("page_size", 15))
    max_pages = int(source.get("max_pages", 3))
    candidates: dict[str, dict[str, Any]] = {}

    if truthy_source_flag(source.get("browse_pages"), default=False):
        for page_index in range(1, max_pages + 1):
            page_url = talentbrew_browse_page_url(source, page_index)
            try:
                result_html = fetch_url(page_url, timeout=int(source.get("timeout", 30)))
            except Exception as error:  # noqa: BLE001
                print(f"Could not fetch TalentBrew browse page for {company}: {error}", file=sys.stderr)
                break
            raw_page_candidates = parse_talentbrew_result_items(
                result_html,
                page_url,
                str(company),
                str(source.get("url") or page_url),
                str(source.get("browse_query") or "regional_listing"),
            )
            page_candidates = {
                url: candidate
                for url, candidate in raw_page_candidates.items()
                if talentbrew_candidate_matches_source(source, candidate)
            }
            if not raw_page_candidates:
                break
            before = len(candidates)
            candidates.update(page_candidates)
            if len(candidates) == before and not source.get("required_locations"):
                break
    else:
        for keyword in [str(item) for item in keywords if str(item).strip()]:
            total_pages = max_pages
            for page_index in range(1, max_pages + 1):
                params = {
                    "ActiveFacetID": "0",
                    "CurrentPage": str(page_index),
                    "RecordsPerPage": str(page_size),
                    "Distance": str(source.get("distance", 50)),
                    "RadiusUnitType": "0",
                    "Keywords": keyword,
                    "Location": str(source.get("location", "")),
                    "Latitude": "",
                    "Longitude": "",
                    "ShowRadius": "False",
                    "CustomFacetName": "",
                    "FacetTerm": "",
                    "FacetType": "0",
                    "SearchResultsModuleName": "Search Results",
                    "SortCriteria": str(source.get("sort_criteria", 1)),
                    "SortDirection": str(source.get("sort_direction", 0)),
                    "SearchType": "1",
                }
                if organization_ids:
                    params["OrganizationIds"] = organization_ids
                try:
                    data = fetch_json(f"{results_url}?{urllib.parse.urlencode(params)}")
                except Exception as error:  # noqa: BLE001
                    print(f"Could not fetch TalentBrew API for {company}: {error}", file=sys.stderr)
                    break
                result_html = str(data.get("results") or "") if isinstance(data, dict) else ""
                if not result_html:
                    break
                pages_match = re.search(r'data-total-pages=["\'](\d+)["\']', result_html, flags=re.I)
                if pages_match:
                    total_pages = min(max_pages, int(pages_match.group(1)))
                raw_page_candidates = parse_talentbrew_result_items(
                    result_html,
                    results_url,
                    str(company),
                    str(source.get("url") or results_url),
                    keyword,
                )
                page_candidates = {
                    url: candidate
                    for url, candidate in raw_page_candidates.items()
                    if talentbrew_candidate_matches_source(source, candidate)
                }
                for url, candidate in page_candidates.items():
                    if url in candidates:
                        continue
                    fallback = {
                        "company": company,
                        "job_number": candidate["job_number"],
                        "external_job_id": candidate["external_job_id"],
                    }
                    detail = (
                        talentbrew_detail_from_url(url, fallback)
                        if truthy_source_flag(source.get("fetch_details"), default=True)
                        else {}
                    )
                    if detail:
                        candidate["job_number"] = detail.get("job_number") or candidate["job_number"]
                        candidate["external_job_id"] = detail.get("external_job_id") or candidate["external_job_id"]
                        candidate["posted_at"] = detail.get("posted_at", "") or candidate["posted_at"]
                        candidate["updated_at"] = detail.get("updated_at", "")
                        candidate["_jd_text"] = detail.get("_jd_text", "")
                        if detail.get("posted_at"):
                            candidate["freshness_source"] = "json_ld_datePosted"
                    candidates[url] = candidate
                if not raw_page_candidates or page_index >= total_pages:
                    break
    detail_limit = int(source.get("fetch_detail_limit", 0) or 0)
    if not truthy_source_flag(source.get("fetch_details"), default=True) and detail_limit > 0:
        relevant = [candidate for candidate in candidates.values() if unclassified_technical_title_relevant(candidate)]
        for candidate in relevant[:detail_limit]:
            detail = talentbrew_detail_from_url(candidate["url"], candidate)
            if not detail:
                continue
            candidate["posted_at"] = detail.get("posted_at", "") or candidate.get("posted_at", "")
            candidate["updated_at"] = detail.get("updated_at", "") or candidate.get("updated_at", "")
            candidate["job_number"] = detail.get("job_number", "") or candidate.get("job_number", "")
            candidate["external_job_id"] = detail.get("external_job_id", "") or candidate.get("external_job_id", "")
            candidate["_jd_text"] = detail.get("_jd_text", "") or candidate.get("_jd_text", "")
            if detail.get("posted_at"):
                candidate["freshness_source"] = "json_ld_datePosted"
    return list(candidates.values())


def careerpuck_board(source: dict[str, Any]) -> str:
    board = str(source.get("board") or source.get("job_board") or "").strip()
    if board:
        return board
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if "job-board" in parts:
        index = parts.index("job-board")
        if index + 1 < len(parts):
            return parts[index + 1]
    return slugify(str(source.get("company") or "")).replace("-", "").upper()


def discover_careerpuck_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    board = careerpuck_board(source)
    api_url = str(source.get("api_url") or f"https://api.careerpuck.com/v1/public/job-boards/{urllib.parse.quote(board)}")
    try:
        data = fetch_json(api_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch CareerPuck API for {company}: {error}", file=sys.stderr)
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else []
    if not isinstance(jobs, list):
        return []
    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or "").strip()
        if not title:
            continue
        public_url = str(job.get("publicUrl") or job.get("publicShareableUrl") or job.get("applyUrl") or "").strip()
        if not public_url:
            public_url = f"https://app.careerpuck.com/job-board/{urllib.parse.quote(board)}/job/{urllib.parse.quote(str(job.get('permalink') or job.get('atsSourceId') or slugify(title)))}"
        url = normalize_job_url(public_url)
        location = compact_location_text(job.get("location") or job.get("offices") or job.get("office"))
        departments = compact_location_text(job.get("departments") or job.get("department"))
        candidates[url] = {
            "company": company,
            "role": title,
            "url": url,
            "platform": "careerpuck",
            "location": location,
            "job_number": str(job.get("requisitionId") or job.get("atsSourceId") or job.get("permalink") or ""),
            "external_job_id": str(job.get("atsSourceId") or job.get("permalink") or ""),
            "posted_at": normalize_datetime(job.get("postedAt")),
            "updated_at": "",
            "source": source.get("url", api_url),
            "source_query": departments,
            "freshness_source": "careerpuck_postedAt" if job.get("postedAt") else "unknown",
            "notes": f"CareerPuck public job board adapter; board={board}; ats={job.get('atsSourcePlatform') or ''}".strip(),
            "_jd_text": "\n\n".join(
                block
                for block in [
                    title,
                    location,
                    departments,
                    html_to_text(str(job.get("content") or "")),
                    str(job.get("salaryDescription") or ""),
                ]
                if block
            ),
        }
    return list(candidates.values())


def pinpoint_jobs_url(source: dict[str, Any]) -> str:
    if source.get("jobs_url"):
        return str(source["jobs_url"]).strip()
    source_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme and parsed.netloc:
        prefix = "/en"
        match = re.match(r"^/(en|us|gb|au|ca)(?:/|$)", parsed.path, flags=re.I)
        if match:
            prefix = f"/{match.group(1)}"
        return urllib.parse.urljoin(f"{parsed.scheme}://{parsed.netloc}", f"{prefix}/jobs.json")
    return ""


def discover_pinpoint_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    jobs_url = pinpoint_jobs_url(source)
    if not jobs_url:
        return []
    try:
        data = fetch_json(jobs_url)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Pinpoint API for {company}: {error}", file=sys.stderr)
        return []
    jobs = data.get("data") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    candidates: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or "").strip()
        if not title:
            continue
        url = normalize_job_url(str(job.get("url") or urllib.parse.urljoin(jobs_url, f"jobs/{job.get('id')}")))
        location = compact_location_text(job.get("location"))
        department = compact_location_text(job.get("department") or job.get("division"))
        text_blocks = [
            title,
            location,
            department,
            html_to_text(str(job.get("description") or "")),
            html_to_text(str(job.get("key_responsibilities") or "")),
            html_to_text(str(job.get("skills_knowledge_expertise") or "")),
            str(job.get("employment_type_text") or ""),
            str(job.get("workplace_type_text") or ""),
            str(job.get("compensation") or ""),
        ]
        candidates[url] = {
            "company": company,
            "role": title,
            "url": url,
            "platform": "pinpoint",
            "location": location,
            "job_number": str(job.get("requisition_id") or job.get("id") or ""),
            "external_job_id": str(job.get("id") or ""),
            "posted_at": normalize_datetime(job.get("posted_at") or job.get("published_at") or job.get("created_at")),
            "updated_at": normalize_datetime(job.get("updated_at")),
            "source": source.get("url", jobs_url),
            "source_query": department,
            "freshness_source": "pinpoint_posted_at" if job.get("posted_at") or job.get("published_at") or job.get("created_at") else "unknown",
            "notes": "Pinpoint jobs.json adapter. Some Pinpoint boards do not expose official posted dates.",
            "_jd_text": "\n\n".join(block for block in text_blocks if block),
        }
    return list(candidates.values())


def brassring_detail_from_url(url: str, company: str = "Unknown Company") -> dict[str, Any]:
    try:
        raw = fetch_url(url, timeout=20)
    except Exception:
        return {}
    title = html_attr(r'<meta(?=[^>]+(?:property|name)=["\']og:title["\'])(?=[^>]+content=["\']([^"\']+)["\'])[^>]*>', raw)
    if not title:
        title = html_attr(r"<title[^>]*>(.*?)</title>", raw)
    if title:
        title = re.sub(r"\s+(?:at|\|)\s+.*$", "", title, flags=re.I).strip()
        title = re.sub(r"\s+-\s+.*?-\s+Job Details\s*$", "", title, flags=re.I).strip()
    description = html_attr(r'<meta(?=[^>]+(?:property|name)=["\']og:description["\'])(?=[^>]+content=["\']([^"\']+)["\'])[^>]*>', raw)
    req_id = html_attr(r'<meta(?=[^>]+name=["\']gtm_reqid["\'])(?=[^>]+content=["\']([^"\']+)["\'])[^>]*>', raw)
    posted_at = extract_first_datetime(
        raw,
        [
            r'"datePosted"\s*:\s*"([^"]+)"',
        ],
    )
    return {
        "company": company,
        "role": title or infer_role_from_url(url),
        "url": normalize_job_url(url),
        "platform": "brassring",
        "location": extract_location(raw),
        "job_number": req_id,
        "external_job_id": req_id or urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("jobid", [""])[0],
        "posted_at": posted_at,
        "updated_at": "",
        "source": url,
        "freshness_source": "brassring_meta_date" if posted_at else "unknown",
        "notes": "BrassRing detail parser. Prefer upstream ATS/source for broad discovery when available.",
        "_jd_text": "\n\n".join(block for block in [title, description, html_to_text(raw)] if block),
    }


def discover_brassring_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    urls = source.get("job_urls") or source.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    candidates = {}
    for url in urls:
        detail = brassring_detail_from_url(str(url), str(source.get("company") or "Unknown Company"))
        if detail:
            detail["source"] = source.get("url", str(url))
            candidates[detail["url"]] = detail
    return list(candidates.values())


def discover_kula_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    try:
        raw = fetch_url(source_url, timeout=25)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch Kula careers page for {company}: {error}", file=sys.stderr)
        return []
    candidates: dict[str, dict[str, Any]] = {}
    for link_match in re.finditer(r'href=["\'](/[^"\']+/\d+/\?jobs=true)["\']', raw, flags=re.I):
        card_start = raw.rfind('<div class="chakra-card', 0, link_match.start())
        card = raw[card_start : link_match.end()] if card_start >= 0 else raw[max(0, link_match.start() - 2500) : link_match.end()]
        title_match = re.search(r'<p\b[^>]*class=["\'][^"\']*css-f8zk62[^"\']*["\'][^>]*>(.*?)</p>', card, flags=re.I | re.S)
        role = html_to_text(title_match.group(1)) if title_match else infer_role_from_url(link_match.group(1))
        department_match = re.search(r'<span\b[^>]*class=["\'][^"\']*css-ypynmf[^"\']*["\'][^>]*>(.*?)</span>', card, flags=re.I | re.S)
        department = html_to_text(department_match.group(1)) if department_match else ""
        text_values = [
            html_to_text(match.group(1))
            for match in re.finditer(r'<p\b[^>]*class=["\'][^"\']*css-de2tee[^"\']*["\'][^>]*>(.*?)</p>', card, flags=re.I | re.S)
        ]
        location = ""
        for value in text_values:
            normalized = value.lower()
            if not value or normalized in {"usd", "full time", "part time", "contract", "remote", "hybrid", "on-site"}:
                continue
            if re.fullmatch(r"[\d,]+(?:-[\d,]+)?\s*/\s*year", value, flags=re.I):
                continue
            location = value
            break
        url = normalize_job_url(urllib.parse.urljoin(source_url, html.unescape(link_match.group(1))))
        candidates[url] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "kula",
            "location": location,
            "job_number": re.search(r"/(\d+)/", link_match.group(1)).group(1) if re.search(r"/(\d+)/", link_match.group(1)) else "",
            "external_job_id": re.search(r"/(\d+)/", link_match.group(1)).group(1) if re.search(r"/(\d+)/", link_match.group(1)) else "",
            "posted_at": "",
            "updated_at": "",
            "source": source_url,
            "source_query": department,
            "freshness_source": "unknown",
            "notes": "Kula careers page adapter. Kula pages often do not expose official posted dates; use first_seen for freshness.",
            "_jd_text": html_to_text(card),
        }
    return list(candidates.values())


def discover_cadient_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    raw = fetch_url(source_url, timeout=int(source.get("timeout", 30)))
    cards = re.findall(
        r'(?is)<li\b[^>]*class="[^"]*\bborder\b[^"]*\bborder-solid\b[^"]*">.*?</li>',
        raw,
    )
    candidates: dict[str, dict[str, Any]] = {}
    for card in cards:
        role_match = re.search(r"(?is)<h3\b[^>]*>(.*?)</h3>", card)
        link_match = re.search(
            r'(?is)href="([^"]*cta\.cadienttalent\.com[^"]*(?:SEQ|seq)=jobDetails[^"]*)"',
            card,
        )
        if not role_match or not link_match:
            continue
        role = html_to_text(role_match.group(1))
        url = normalize_job_url(html.unescape(link_match.group(1)))
        if not role or not url:
            continue
        paragraphs = re.findall(r'(?is)<p\b[^>]*class="[^"]*\bmicro\b[^"]*"[^>]*>(.*?)</p>', card)
        details = re.findall(r"(?is)<dd\b[^>]*>(.*?)</dd>", card)
        category = html_to_text(paragraphs[0]) if paragraphs else ""
        location = html_to_text(details[0]) if details else ""
        location = re.sub(r"\s*,\s*", ", ", location)
        date_posted = html_to_text(details[1]) if len(details) > 1 else ""
        posted_at = normalize_datetime(date_posted)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        posting_id = str((query.get("POSTING_ID") or query.get("posting_id") or [""])[0])
        candidates[url] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "cadient",
            "location": location,
            "job_number": posting_id,
            "external_job_id": posting_id,
            "posted_at": posted_at,
            "updated_at": "",
            "source": source_url,
            "source_query": category,
            "freshness_source": "cadient_listing_date_posted" if posted_at else "unknown",
            "notes": "Official employer careers page backed by Cadient Talent.",
            "_jd_text": "\n\n".join(
                part
                for part in [
                    role,
                    f"Category: {category}" if category else "",
                    f"Location: {location}" if location else "",
                    f"Date Posted: {date_posted}" if date_posted else "",
                ]
                if part
            ),
        }
    return list(candidates.values())


def discover_breezy_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    feed_url = str(source.get("feed_url") or f"{source_url.rstrip('/')}/json")
    data = fetch_json(feed_url, timeout=int(source.get("timeout", 30)))
    rows = data if isinstance(data, list) else []
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = str(row.get("name") or "").strip()
        url = normalize_job_url(str(row.get("url") or "").strip())
        if not role or not url:
            continue
        locations = row.get("locations") if isinstance(row.get("locations"), list) else []
        location_names = [
            str(location.get("name") or "").strip()
            for location in locations
            if isinstance(location, dict) and str(location.get("name") or "").strip()
        ]
        primary_location = row.get("location") if isinstance(row.get("location"), dict) else {}
        location = "; ".join(dict.fromkeys(location_names))
        if not location:
            location = str(primary_location.get("name") or "").strip()
        if bool(primary_location.get("is_remote")) and "remote" not in location.lower():
            location = "; ".join(part for part in [location, "Remote"] if part)
        department = row.get("department")
        if isinstance(department, dict):
            department = department.get("name")
        department = str(department or "").strip()
        employment_type = row.get("type")
        if isinstance(employment_type, dict):
            employment_type = employment_type.get("name")
        employment_type = str(employment_type or "").strip()
        salary = str(row.get("salary") or "").strip()
        posted_at = normalize_datetime(row.get("published_date"))
        external_id = str(row.get("id") or "").strip()
        candidates[url] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "breezy",
            "location": location,
            "job_number": external_id,
            "external_job_id": external_id,
            "posted_at": posted_at,
            "updated_at": "",
            "source": source_url,
            "source_query": department,
            "freshness_source": "breezy_published_date" if posted_at else "unknown",
            "notes": "Official Breezy public jobs feed.",
            "_jd_text": "\n\n".join(
                part
                for part in [
                    role,
                    f"Department: {department}" if department else "",
                    f"Location: {location}" if location else "",
                    f"Employment Type: {employment_type}" if employment_type else "",
                    f"Salary: {salary}" if salary else "",
                ]
                if part
            ),
        }
    return list(candidates.values())


def discover_clinch_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    search_url = str(source.get("url") or "").strip()
    if not search_url:
        return []
    max_pages = max(1, int(source.get("max_pages", 5)))
    timeout = int(source.get("timeout", 30))
    base_params = source.get("search_params")
    if not isinstance(base_params, dict):
        base_params = {}
    candidates: dict[str, dict[str, Any]] = {}

    for page in range(1, max_pages + 1):
        parsed_search_url = urllib.parse.urlparse(search_url)
        params: list[tuple[str, str]] = urllib.parse.parse_qsl(
            parsed_search_url.query,
            keep_blank_values=True,
        )
        params = [(key, value) for key, value in params if key != "page"]
        for key, value in base_params.items():
            values = value if isinstance(value, list) else [value]
            params.extend((str(key), str(item)) for item in values if str(item).strip())
        params.append(("page", str(page)))
        page_url = urllib.parse.urlunparse(
            parsed_search_url._replace(
                query=urllib.parse.urlencode(params, doseq=True)
            )
        )
        try:
            raw = fetch_url(page_url, timeout=timeout)
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch Clinch board for {company}: {error}", file=sys.stderr)
            break

        rows = list(
            re.finditer(
                r'<tr\b[^>]*data-job-url=["\'](?P<url>[^"\']+)["\'][^>]*>(?P<body>.*?)</tr>',
                raw,
                flags=re.I | re.S,
            )
        )
        if not rows:
            break
        for row in rows:
            body = row.group("body")
            detail_url = normalize_job_url(
                urllib.parse.urljoin(search_url, html.unescape(row.group("url")))
            )
            title_match = re.search(
                r'<td\b[^>]*class=["\'][^"\']*\bjob-search-results-title\b[^"\']*["\'][^>]*>'
                r'.*?<a\b[^>]*>(.*?)</a>',
                body,
                flags=re.I | re.S,
            )
            requisition_match = re.search(
                r'aria-label=["\']Requisition Identifier:\s*([^"\']+)["\']',
                body,
                flags=re.I,
            )
            locations = [
                html_to_text(value)
                for value in re.findall(
                    r'aria-label=["\']Location:\s*([^"\']+)["\']',
                    body,
                    flags=re.I,
                )
                if html_to_text(value)
            ]
            external_id = (
                html_to_text(requisition_match.group(1))
                if requisition_match
                else detail_url
            )
            candidates[detail_url] = {
                "company": company,
                "role": (
                    html_to_text(title_match.group(1))
                    if title_match
                    else infer_role_from_url(detail_url)
                ),
                "url": detail_url,
                "platform": "clinch",
                "location": "; ".join(merge_unique(locations, [])),
                "job_number": external_id,
                "external_job_id": external_id,
                "posted_at": "",
                "updated_at": "",
                "source": search_url,
                "source_query": urllib.parse.urlencode(params, doseq=True),
                "freshness_source": "unknown",
                "notes": "ClinchTalent public board; detail JSON-LD supplies the official posted date.",
            }
        if not re.search(r'<a\b[^>]*\brel=["\']next["\']', raw, flags=re.I):
            break

    detail_limit = max(0, min(len(candidates), int(source.get("max_detail_pages", 100))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    detail_timeout = int(source.get("detail_timeout", 20))

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(str(candidate["url"]), timeout=detail_timeout)
        except Exception as error:  # noqa: BLE001
            print(
                f"Could not fetch Clinch detail for {company} job "
                f"{candidate['external_job_id']}: {error}",
                file=sys.stderr,
            )
            return
        details = parse_json_ld_jobs(detail_raw, str(candidate["url"]), company)
        if not details:
            return
        detail = details[0]
        for key in ["role", "location", "posted_at", "updated_at", "_jd_text"]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["platform"] = "clinch"
        candidate["freshness_source"] = (
            "clinch_json_ld_date_posted" if candidate.get("posted_at") else "unknown"
        )
        candidate["notes"] = (
            "ClinchTalent public board enriched from detail-page JobPosting JSON-LD."
        )

    selected = list(candidates.values())[:detail_limit]
    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            list(executor.map(enrich, selected))
    return list(candidates.values())


def discover_atkins_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "AtkinsRéalis")
    api_base = str(
        source.get("api_base") or "https://atkinsats-prod-api.connectid.cloud"
    ).rstrip("/")
    timeout = int(source.get("timeout", 30))
    page_size = max(1, min(int(source.get("page_size", 50)), 50))
    max_pages = max(1, int(source.get("max_pages", 10)))
    country = str(source.get("country") or "United States of America")
    required_location_terms = [
        str(item).strip().casefold()
        for item in source.get("required_location_terms", ["Washington"])
        if str(item).strip()
    ]
    required_location_patterns = [
        str(item).strip()
        for item in source.get("required_location_patterns", [])
        if str(item).strip()
    ]
    try:
        token_data = fetch_json(f"{api_base}/api/jobs/token", timeout=timeout)
        token = str(token_data.get("token") or "").strip()
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch AtkinsRéalis jobs token: {error}", file=sys.stderr)
        return []
    if not token:
        return []

    candidates: dict[str, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        payload = {
            "limit": page_size,
            "page": page,
            "language": str(source.get("language") or "en"),
            "country": country,
        }
        try:
            data = fetch_json_post_with_headers(
                f"{api_base}/api/jobs/jobs",
                payload,
                {"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch AtkinsRéalis jobs page {page}: {error}", file=sys.stderr)
            break
        records = data.get("jobs", []) if isinstance(data, dict) else []
        if not isinstance(records, list) or not records:
            break
        for job in records:
            if not isinstance(job, dict):
                continue
            locations = job.get("location_mappings")
            if not isinstance(locations, list):
                locations = []
            locations = [str(item).strip() for item in locations if str(item).strip()]
            location_text = "; ".join(locations)
            location_folded = location_text.casefold()
            if required_location_patterns and not any(
                re.search(pattern, location_text, flags=re.I)
                for pattern in required_location_patterns
            ):
                continue
            if not required_location_patterns and required_location_terms and not any(
                term in location_folded for term in required_location_terms
            ):
                continue
            requisition_id = str(job.get("job_requisition_id") or job.get("id") or "").strip()
            role = str(job.get("job_posting_title") or "").strip()
            if not requisition_id or not role:
                continue
            career_base = str(
                source.get("public_base_url") or source.get("url") or "https://careers.atkinsrealis.com"
            ).rstrip("/")
            if career_base.endswith("/search-results"):
                career_base = career_base.rsplit("/", 1)[0]
            detail_url = normalize_job_url(
                f"{career_base}/jobs/{slugify(role)}-{slugify(requisition_id)}"
            )
            candidates[detail_url] = {
                "company": company,
                "role": role,
                "url": detail_url,
                "apply_url": normalize_job_url(str(job.get("external_posting_url") or "")),
                "platform": "atkins_jobs",
                "location": location_text,
                "job_number": requisition_id,
                "external_job_id": requisition_id,
                "posted_at": normalize_datetime(
                    job.get("start_date") or job.get("created_at")
                ),
                "updated_at": normalize_datetime(
                    job.get("last_functionally_updated") or job.get("updated_at")
                ),
                "source": source.get("url", career_base),
                "source_query": f"country={country}",
                "freshness_source": "atkins_official_start_date",
                "notes": "AtkinsRéalis official careers API with a public short-lived access token.",
                "_jd_text": "\n\n".join(
                    item
                    for item in [
                        html_to_text(str(job.get("job_overview") or "")),
                        html_to_text(str(job.get("job_description") or "")),
                        html_to_text(str(job.get("job_responsibilities") or "")),
                        html_to_text(str(job.get("person_requirements") or "")),
                        html_to_text(str(job.get("additional_information") or "")),
                    ]
                    if item
                ),
            }
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        total_pages = int(meta.get("totalPages") or 0) if isinstance(meta, dict) else 0
        if len(records) < page_size or (total_pages and page >= total_pages):
            break
    return list(candidates.values())


def kronos_careers_company_code(source: dict[str, Any]) -> str:
    if source.get("company_code"):
        return str(source["company_code"]).strip()
    path = urllib.parse.urlparse(str(source.get("url") or "")).path
    match = re.search(r"/ta/([^/.]+)\.careers", path, flags=re.I)
    return match.group(1) if match else ""


def discover_kronos_careers_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    board_url = str(source.get("url") or "").strip()
    company_code = kronos_careers_company_code(source)
    if not board_url or not company_code:
        return []
    parsed = urllib.parse.urlparse(board_url)
    base_url = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json,*/*;q=0.8",
        "Referer": board_url,
    }
    try:
        fetch_url_with_opener(opener, board_url, timeout=int(source.get("timeout", 25)))
    except Exception as error:  # noqa: BLE001
        print(f"Could not initialize Kronos careers session for {company}: {error}", file=sys.stderr)
        return []

    encoded_company = urllib.parse.quote(f"|{company_code}", safe="")
    page_size = min(max(1, int(source.get("page_size", 100))), 100)
    max_pages = max(1, int(source.get("max_pages", 5)))
    candidates: dict[str, dict[str, Any]] = {}
    for page_index in range(max_pages):
        params = {
            "offset": str(page_index * page_size),
            "size": str(page_size),
            "sort": str(source.get("sort") or "desc"),
            "lang": str(source.get("lang") or "en-US"),
        }
        api_url = (
            f"{base_url}/ta/rest/ui/recruitment/companies/{encoded_company}/job-requisitions?"
            f"{urllib.parse.urlencode(params)}"
        )
        try:
            data = fetch_json_with_opener(opener, api_url, headers, timeout=int(source.get("timeout", 25)))
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch Kronos careers jobs for {company}: {error}", file=sys.stderr)
            break
        jobs = data.get("job_requisitions", []) if isinstance(data, dict) else []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("id") or "").strip()
            role = str(job.get("job_title") or "").strip()
            if not job_id or not role:
                continue
            location = compact_location_text(job.get("location"))
            public_url = normalize_job_url(
                f"{board_url.split('?', 1)[0]}?ShowJob={urllib.parse.quote(job_id)}"
            )
            candidates[public_url] = {
                "company": company,
                "role": role,
                "url": public_url,
                "platform": "kronos_careers",
                "location": location,
                "job_number": job_id,
                "external_job_id": job_id,
                "posted_at": "",
                "updated_at": "",
                "source": board_url,
                "source_query": "all_jobs",
                "freshness_source": "first_seen",
                "notes": "Kronos/UKG Ready public careers API; publication date is not exposed.",
                "_jd_text": html_to_text(str(job.get("job_description") or "")),
            }
        paging = data.get("_paging", {}) if isinstance(data, dict) else {}
        total = int(paging.get("total") or 0) if isinstance(paging, dict) else 0
        if len(jobs) < page_size or (total and (page_index + 1) * page_size >= total):
            break

    detail_limit = max(0, min(len(candidates), int(source.get("max_detail_pages", 40))))
    detail_workers = max(1, int(source.get("detail_workers", 6)))
    detail_candidates = sorted(
        candidates.values(),
        key=lambda candidate: (
            0 if unclassified_technical_title_relevant(candidate) else 1,
            str(candidate.get("role") or "").lower(),
        ),
    )[:detail_limit]

    def enrich(candidate: dict[str, Any]) -> None:
        job_id = str(candidate.get("external_job_id") or "")
        detail_url = (
            f"{base_url}/ta/rest/ui/recruitment/companies/{encoded_company}/"
            f"job-requisitions/{urllib.parse.quote(job_id)}?"
            f"{urllib.parse.urlencode({'showMap': '1', 'lang': str(source.get('lang') or 'en-US')})}"
        )
        try:
            detail = fetch_json_with_opener(opener, detail_url, headers, timeout=int(source.get("detail_timeout", 20)))
        except Exception:
            return
        if not isinstance(detail, dict):
            return
        candidate["role"] = str(detail.get("job_title") or candidate["role"]).strip()
        candidate["location"] = compact_location_text(detail.get("location")) or candidate["location"]
        candidate["_jd_text"] = "\n\n".join(
            text
            for text in [
                html_to_text(str(detail.get("job_description") or "")),
                html_to_text(str(detail.get("job_requirement") or "")),
                html_to_text(str(detail.get("job_preview") or "")),
            ]
            if text
        )

    if detail_candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            list(executor.map(enrich, detail_candidates))
    return list(candidates.values())


def healthcaresource_site_id(source: dict[str, Any]) -> str:
    configured = str(source.get("site_id") or "").strip()
    if configured:
        return configured
    path = urllib.parse.urlparse(str(source.get("url") or "")).path
    match = re.search(r"/CS/([^/?#]+)", path, flags=re.I)
    return urllib.parse.unquote(match.group(1)).strip() if match else ""


def discover_healthcaresource_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    site_id = healthcaresource_site_id(source)
    if not source_url or not site_id:
        return []
    parsed = urllib.parse.urlparse(source_url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    careers_url = f"{origin}/CS/{urllib.parse.quote(site_id)}/"
    endpoint_base = (
        f"{origin}/JobseekerSearchAPI/{urllib.parse.quote(site_id)}/api/Search"
    )
    page_size = min(max(1, int(source.get("page_size", 100))), 500)
    max_pages = max(1, int(source.get("max_pages", 5)))
    candidates: dict[str, dict[str, Any]] = {}
    for page_index in range(max_pages):
        offset = page_index * page_size
        endpoint = f"{endpoint_base}?{urllib.parse.urlencode({'size': page_size})}"
        payload = {
            "query": {"bool": {"must": {"match_all": {}}}},
            "from": offset,
            "size": page_size,
        }
        try:
            data = fetch_json_post_with_headers(
                endpoint,
                payload,
                {"Referer": careers_url},
                timeout=int(source.get("timeout", 25)),
            )
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch HealthcareSource jobs for {company}: {error}", file=sys.stderr)
            break
        hits_block = data.get("hits", {}) if isinstance(data, dict) else {}
        hits = hits_block.get("hits", []) if isinstance(hits_block, dict) else []
        if not isinstance(hits, list) or not hits:
            break
        for hit in hits:
            record = hit.get("_source", {}) if isinstance(hit, dict) else {}
            if not isinstance(record, dict):
                continue
            user_area = record.get("userArea", {})
            if not isinstance(user_area, dict):
                user_area = {}
            if user_area.get("active") is False or user_area.get("isHiddenOnCareerSite") is True:
                continue
            role = str(record.get("title") or record.get("name") or "").strip()
            job_id = str(
                user_area.get("jobPostingID")
                or record.get("documentId")
                or (hit.get("_id") if isinstance(hit, dict) else "")
                or ""
            ).strip()
            if not role or not job_id:
                continue
            address = record.get("jobLocation", {})
            address = address.get("address", {}) if isinstance(address, dict) else {}
            if not isinstance(address, dict):
                address = {}
            location = str(address.get("addressLocalityRegion") or "").strip()
            if not location:
                location = ", ".join(
                    part
                    for part in [
                        str(address.get("addressLocality") or "").strip(),
                        str(address.get("addressRegion") or "").strip(),
                    ]
                    if part
                )
            public_url = normalize_job_url(
                f"{careers_url}#/job/{urllib.parse.quote(job_id)}"
            )
            posted_at = normalize_datetime(record.get("datePosted"))
            updated_at = normalize_datetime(
                user_area.get("jobPostingModifiedDate")
                or record.get("lastIndexedDate")
            )
            department = str(
                user_area.get("bELevel3")
                or record.get("occupationalCategory")
                or ""
            ).strip()
            description = html_to_text(
                str(user_area.get("jobSummary") or record.get("description") or "")
            )
            candidates[public_url] = {
                "company": company,
                "role": role,
                "url": public_url,
                "platform": "healthcaresource",
                "location": location,
                "job_number": str(user_area.get("requisitionNumber") or job_id),
                "external_job_id": job_id,
                "posted_at": posted_at,
                "updated_at": updated_at,
                "source": source_url,
                "source_query": department,
                "freshness_source": (
                    "healthcaresource_date_posted" if posted_at else "first_seen"
                ),
                "notes": "HealthcareSource public Jobseeker Search API.",
                "_jd_text": description,
            }
        total_value = hits_block.get("total", 0) if isinstance(hits_block, dict) else 0
        if isinstance(total_value, dict):
            total = int(total_value.get("value") or 0)
        else:
            total = int(total_value or 0)
        if len(hits) < page_size or (total and offset + len(hits) >= total):
            break
    return list(candidates.values())


def parse_paradox_preload_state(raw: str) -> dict[str, Any]:
    marker = re.search(r"window\.__PRELOAD_STATE__\s*=\s*", raw)
    if not marker:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[marker.end() :])
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def discover_paradox_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip().rstrip("/")
    if not source_url:
        return []
    max_pages = max(1, int(source.get("max_pages", 20)))
    candidates: dict[str, dict[str, Any]] = {}
    total_jobs = 0

    for page in range(1, max_pages + 1):
        page_url = source_url if page == 1 else f"{source_url}/page/{page}"
        try:
            raw = fetch_url(page_url, timeout=int(source.get("timeout", 25)))
        except Exception as error:  # noqa: BLE001
            print(f"Could not fetch Paradox jobs for {company}: {error}", file=sys.stderr)
            break
        state = parse_paradox_preload_state(raw)
        search = state.get("jobSearch") if isinstance(state.get("jobSearch"), dict) else {}
        jobs = search.get("jobs") if isinstance(search.get("jobs"), list) else []
        if not jobs:
            break
        total_jobs = int(search.get("totalJob") or total_jobs or 0)
        for job in jobs:
            if not isinstance(job, dict):
                continue
            external_id = str(
                job.get("reference") or job.get("uniqueID") or job.get("sourceID") or ""
            ).strip()
            original_url = str(job.get("originalURL") or "").strip()
            detail_url = normalize_job_url(urllib.parse.urljoin(f"{source_url}/", original_url))
            role = html_to_text(str(job.get("title") or "")).strip()
            if not role or not detail_url:
                continue
            locations = job.get("locations") if isinstance(job.get("locations"), list) else []
            location = ""
            if locations and isinstance(locations[0], dict):
                first_location = locations[0]
                location = str(
                    first_location.get("locationParsedText")
                    or first_location.get("locationText")
                    or first_location.get("cityStateAbbr")
                    or first_location.get("cityState")
                    or ""
                ).strip()
            if not location and job.get("isRemote"):
                location = "Remote"
            location = location or str(source.get("default_location") or "").strip()
            candidates[external_id or detail_url] = {
                "company": company,
                "role": role,
                "url": detail_url,
                "platform": "paradox",
                "location": location,
                "job_number": external_id,
                "external_job_id": external_id,
                "posted_at": "",
                "updated_at": "",
                "source": source_url,
                "source_query": str(source.get("source_query") or "company_careers"),
                "freshness_source": "first_seen",
                "notes": "Paradox career-site preload data.",
                "_jd_text": "",
            }
        if total_jobs and len(candidates) >= total_jobs:
            break

    if not truthy_source_flag(source.get("fetch_details"), default=True):
        return list(candidates.values())
    detail_limit = max(0, min(len(candidates), int(source.get("max_detail_pages", 100))))
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    detail_timeout = int(source.get("detail_timeout", 20))

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(str(candidate["url"]), timeout=detail_timeout)
        except Exception:
            return
        details = parse_json_ld_jobs(detail_raw, str(candidate["url"]), company)
        if not details:
            return
        detail = details[0]
        for key in ["role", "location", "posted_at", "updated_at", "_jd_text"]:
            if detail.get(key):
                candidate[key] = detail[key]
        candidate["platform"] = "paradox"
        if candidate.get("posted_at"):
            candidate["freshness_source"] = "paradox_json_ld_date_posted"
        candidate["notes"] = "Paradox preload listing enriched from detail-page JobPosting JSON-LD."

    detail_candidates = list(candidates.values())[:detail_limit]
    if detail_candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            list(executor.map(enrich, detail_candidates))
    return list(candidates.values())


def infor_cloudsuite_parts(source: dict[str, Any]) -> tuple[str, str, str, str]:
    source_url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(source_url)
    path_match = re.search(r"(?i)(.*?/hcm/jobs)(?:/|$)", parsed.path)
    app_path = path_match.group(1) if path_match else "/hcm/Jobs"
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    board = str(source.get("job_board") or query.get("csk.JobBoard") or "EXTERNAL").strip()
    organization = str(
        source.get("hr_organization")
        or query.get("csk.HROrganization")
        or "1"
    ).strip()
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    return origin, app_path, board, organization


def infor_cloudsuite_field(record: dict[str, Any], field_name: str) -> Any:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    field = fields.get(field_name)
    return field.get("value") if isinstance(field, dict) else ""


def infor_cloudsuite_date(value: Any) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return normalize_datetime(raw)


def infor_cloudsuite_location(value: Any) -> str:
    raw = str(value or "").strip()
    parts = [part.strip() for part in raw.split(":") if part.strip()]
    if len(parts) >= 3 and parts[0].upper() == "US":
        return ", ".join([":".join(parts[2:]), parts[1]])
    return raw


def infor_cloudsuite_job_url(source: dict[str, Any], job_id: str) -> str:
    origin, app_path, board, organization = infor_cloudsuite_parts(source)
    resource = urllib.parse.quote(
        f"JobPosting[JobPostingSet](1,{job_id},1).JobPostingDisplayNav",
        safe=".",
    )
    query = urllib.parse.urlencode(
        {
            "csk.HROrganization": organization,
            "csk.JobBoard": board,
        }
    )
    return normalize_job_url(
        f"{origin}{app_path}/navigation/{resource}?{query}"
    )


def infor_cloudsuite_request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    referer: str,
    timeout: int,
) -> Any:
    raw = fetch_url_with_opener(
        opener,
        url,
        headers={
            "Accept": "application/json,*/*;q=0.8",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=timeout,
    )
    return json.loads(raw)


def discover_infor_cloudsuite_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    origin, app_path, board, organization = infor_cloudsuite_parts(source)
    timeout = max(5, int(source.get("timeout", 25)))
    page_size = max(1, min(int(source.get("page_size", 100)), 100))
    max_pages = max(1, int(source.get("max_pages", 10)))
    common_query = {
        "csk.JobBoard": board,
        "csk.HROrganization": organization,
    }
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    fetch_url_with_opener(opener, source_url, timeout=timeout)

    list_endpoint = (
        f"{origin}{app_path}/list/JobPosting.SearchForJobsResults"
    )
    params: dict[str, Any] = {
        "pageop": "load",
        "pagesize": page_size,
        "pagepanel": "JobsHomePage.Jobs.NewJobsForAnonymous",
        **common_query,
    }
    candidates: dict[str, dict[str, Any]] = {}
    for _page_index in range(max_pages):
        data = infor_cloudsuite_request_json(
            opener,
            f"{list_endpoint}?{urllib.parse.urlencode(params)}",
            source_url,
            timeout,
        )
        data_view = data.get("dataViewSet") if isinstance(data, dict) else {}
        records = data_view.get("data") if isinstance(data_view, dict) else []
        if not isinstance(records, list) or not records:
            break
        for record in records:
            if not isinstance(record, dict):
                continue
            job_id = str(
                infor_cloudsuite_field(record, "JobId")
                or infor_cloudsuite_field(record, "JobRequisition")
                or ""
            ).strip()
            title = str(
                infor_cloudsuite_field(record, "Description")
                or infor_cloudsuite_field(
                    record,
                    "_op_Description_spc_translation_cp_",
                )
                or ""
            ).strip()
            if not job_id or not title:
                continue
            url = infor_cloudsuite_job_url(source, job_id)
            posted_raw = infor_cloudsuite_field(record, "PostingDateRange")
            category = str(
                infor_cloudsuite_field(record, "CategoryDescriptionForSort")
                or infor_cloudsuite_field(
                    record,
                    "_op_Category_prd_Description_spc_translation_cp_",
                )
                or ""
            ).strip()
            work_type = str(
                infor_cloudsuite_field(record, "WorkType") or ""
            ).strip()
            candidates[url] = {
                "company": company,
                "role": title,
                "url": url,
                "platform": "infor_cloudsuite",
                "location": infor_cloudsuite_location(
                    infor_cloudsuite_field(
                        record,
                        "LocationOfJobDescriptionForSort",
                    )
                    or infor_cloudsuite_field(record, "LocationOfJob")
                ),
                "job_number": job_id,
                "external_job_id": job_id,
                "posted_at": infor_cloudsuite_date(posted_raw),
                "updated_at": "",
                "source": source_url,
                "source_query": " / ".join(
                    item for item in [category, work_type] if item
                ),
                "freshness_source": (
                    "infor_cloudsuite_posting_date" if posted_raw else ""
                ),
                "notes": "Infor CloudSuite public candidate experience API.",
                "_jd_text": "",
            }

        paging = (
            data_view.get("pagingInfo")
            if isinstance(data_view, dict)
            and isinstance(data_view.get("pagingInfo"), dict)
            else {}
        )
        if not truthy_source_flag(paging.get("hasNext"), default=False):
            break
        params = {
            "pagepanel": "JobsHomePage.Jobs.NewJobsForAnonymous",
            "sortOrderName": paging.get("sortOrderName", ""),
            "previousDisabled": str(
                bool(paging.get("previousDisabled"))
            ).lower(),
            "fk": paging.get("fk", ""),
            "pageop": "next",
            "pagesize": page_size,
            "hasPrevious": str(bool(paging.get("hasPrevious"))).lower(),
            "hasNext": str(bool(paging.get("hasNext"))).lower(),
            "isAscending": str(bool(paging.get("isAscending"))).lower(),
            "lk": paging.get("lk", ""),
            **common_query,
        }

    detail_limit = max(0, int(source.get("max_detail_pages", 20)))
    selected = sorted(
        candidates.values(),
        key=lambda candidate: (
            0 if unclassified_technical_title_relevant(candidate) else 1,
            str(candidate.get("role") or "").lower(),
        ),
    )[:detail_limit]
    for candidate in selected:
        job_id = str(candidate.get("external_job_id") or "").strip()
        resource = urllib.parse.quote(
            f"JobPosting[JobPostingSet](1,{job_id},1)",
            safe="",
        )
        navigation = urllib.parse.quote(
            f"JobPosting[JobPostingSet](1,{job_id},1).JobPostingDisplayNav",
            safe=".",
        )
        detail_params = {
            "pageop": "load",
            "pagesize": 1,
            "navigation": urllib.parse.unquote(navigation),
            "fromlist": "JobPosting.SearchForJobsResults",
            **common_query,
        }
        detail_url = (
            f"{origin}{app_path}/form/{resource}.JobPostingDisplay?"
            f"{urllib.parse.urlencode(detail_params)}"
        )
        try:
            detail = infor_cloudsuite_request_json(
                opener,
                detail_url,
                source_url,
                timeout,
            )
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(detail, dict):
            continue
        description = infor_cloudsuite_field(
            detail,
            "_op_PositionDescription_spc_translation_cp_",
        )
        if description:
            candidate["_jd_text"] = html_to_text(str(description))
        detail_title = infor_cloudsuite_field(
            detail,
            "_op_Description_spc_translation_cp_",
        )
        if detail_title:
            candidate["role"] = str(detail_title).strip()
        detail_posted = infor_cloudsuite_field(detail, "PostingDateRange")
        if detail_posted:
            candidate["posted_at"] = infor_cloudsuite_date(detail_posted)
            candidate["freshness_source"] = "infor_cloudsuite_posting_date"
    return list(candidates.values())


def viewpoint_for_cloud_origin(source: dict[str, Any]) -> str:
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    host = parsed.netloc.lower()
    if not host.endswith(".viewpointforcloud.com"):
        return ""
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def viewpoint_for_cloud_date(value: Any) -> str:
    match = re.search(r"/Date\((\d+)", str(value or ""))
    if not match:
        return normalize_datetime(value)
    timestamp = int(match.group(1)) / 1000
    return (
        dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def discover_viewpoint_for_cloud_jobs(
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    origin = viewpoint_for_cloud_origin(source)
    if not origin:
        return []
    timeout = int(source.get("timeout", 20))
    search_url = (
        f"{origin}/Careers/GetJobReqSearchExternal?"
        "searchString=&cityString=&templateIDString=&companyString=&categoryIDString="
    )
    data = fetch_json(search_url, timeout=timeout)
    if not isinstance(data, list):
        raise ValueError(
            f"Viewpoint for Cloud returned an unexpected jobs payload for {company}"
        )

    candidates: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        req_id = str(item.get("ReqID") or "").strip()
        role = html_to_text(str(item.get("PositionTitle") or "")).strip()
        if not req_id or not role:
            continue
        url = normalize_job_url(
            f"{origin}/careers/jobdetails/{urllib.parse.quote(req_id)}?openModal=N"
        )
        location = ", ".join(
            str(value).strip()
            for value in [item.get("City"), item.get("State")]
            if str(value or "").strip()
        )
        posted_at = viewpoint_for_cloud_date(
            item.get("DatePosted") or item.get("DatePostedDisplayValue")
        )
        candidates[req_id] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "viewpoint_for_cloud",
            "location": location,
            "job_number": str(item.get("ReqNum") or ""),
            "external_job_id": req_id,
            "posted_at": posted_at,
            "updated_at": "",
            "source": source.get("url", search_url),
            "source_query": "all_open_postings",
            "freshness_source": (
                "viewpoint_for_cloud_date_posted" if posted_at else "first_seen"
            ),
            "notes": (
                "Viewpoint for Cloud official public job search API; "
                "detail endpoint supplies the complete posting."
            ),
            "_jd_text": "",
        }

    location_filter = str(
        source.get("location_include_regex") or ""
    ).strip()
    if location_filter:
        candidates = {
            req_id: candidate
            for req_id, candidate in candidates.items()
            if re.search(
                location_filter,
                " | ".join(
                    [
                        str(candidate.get("role") or ""),
                        str(candidate.get("location") or ""),
                    ]
                ),
                flags=re.I,
            )
        }

    detail_limit = max(
        0,
        min(len(candidates), int(source.get("max_detail_pages", len(candidates)))),
    )
    detail_workers = min(max(int(source.get("detail_workers", 8)), 1), 16)

    def enrich(candidate: dict[str, Any]) -> None:
        req_id = str(candidate["external_job_id"])
        detail_url = (
            f"{origin}/Careers/GetReqDetails?"
            f"{urllib.parse.urlencode({'reqID': req_id, 'reqApplyToken': ''})}"
        )
        try:
            detail = fetch_json(detail_url, timeout=timeout)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(detail, dict):
            return
        role = html_to_text(str(detail.get("PositionTitle") or "")).strip()
        if role:
            candidate["role"] = role
        location = ", ".join(
            str(value).strip()
            for value in [detail.get("City"), detail.get("State")]
            if str(value or "").strip()
        )
        if location:
            candidate["location"] = location
        posted_at = viewpoint_for_cloud_date(detail.get("DatePosted"))
        if posted_at:
            candidate["posted_at"] = posted_at
            candidate["freshness_source"] = "viewpoint_for_cloud_date_posted"
        description = "\n\n".join(
            part
            for part in [
                html_to_text(str(detail.get("PositionDesc") or "")),
                html_to_text(str(detail.get("PositionRequirements") or "")),
                html_to_text(str(detail.get("PositionInstructions") or "")),
                html_to_text(str(detail.get("PositionNotes") or "")),
            ]
            if part
        )
        if description:
            candidate["_jd_text"] = description

    selected = list(candidates.values())[:detail_limit]
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=detail_workers
        ) as executor:
            list(executor.map(enrich, selected))
    return list(candidates.values())


def hireology_careers_path(source: dict[str, Any]) -> str:
    configured = str(source.get("careers_path") or "").strip().strip("/")
    if configured:
        return configured
    parsed = urllib.parse.urlparse(str(source.get("url") or ""))
    host = parsed.netloc.lower().split(":", 1)[0]
    if host == "careers.hireology.com":
        return parsed.path.strip("/").split("/", 1)[0]
    if host.endswith(".hireology.com"):
        subdomain = host[: -len(".hireology.com")]
        if subdomain not in {"api", "app", "careers", "www"}:
            return subdomain
    return ""


def hireology_widget_url(source: dict[str, Any]) -> str:
    careers_path = hireology_careers_path(source)
    if not careers_path:
        return ""
    return (
        "https://careers.hireology.com/"
        f"{urllib.parse.quote(careers_path)}/widget"
    )


def hireology_location(job: dict[str, Any]) -> str:
    locations: list[str] = []
    for item in job.get("locations") or []:
        if not isinstance(item, dict):
            continue
        city = str(item.get("city") or "").strip()
        state = str(item.get("state") or "").strip()
        if city and state and not re.search(
            rf"\b{re.escape(state)}\b",
            city,
            flags=re.I,
        ):
            value = f"{city}, {state}"
        else:
            value = city or state
        if value and value not in locations:
            locations.append(value)
    if bool(job.get("remote")):
        locations.insert(0, "Remote")
    return "; ".join(dict.fromkeys(locations))


def discover_hireology_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    widget_url = hireology_widget_url(source)
    if not widget_url:
        return []
    timeout = int(source.get("timeout", 20))
    raw = fetch_url(widget_url, timeout=timeout)
    match = re.search(
        r"\bvar\s+startingData\s*=\s*(\{.*?\});\s*</script>",
        raw,
        flags=re.I | re.S,
    )
    if not match:
        raise ValueError(
            f"Hireology startingData was not found for {company}"
        )
    starting_data = json.loads(match.group(1))
    api_url = str(starting_data.get("apiUrl") or "").rstrip("/")
    token = str(starting_data.get("apiToken") or "")
    careers_path = str(
        starting_data.get("careersPath")
        or hireology_careers_path(source)
    ).strip("/")
    if not api_url or not token or not careers_path:
        raise ValueError(
            f"Hireology bootstrap data was incomplete for {company}"
        )

    page_size = min(max(int(source.get("page_size", 500)), 1), 500)
    max_pages = min(max(int(source.get("max_pages", 10)), 1), 50)
    headers = {
        "Authorization": f"Bearer {token}",
        "Referer": widget_url,
    }
    candidates: dict[str, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode(
            {"page": page, "page_size": page_size}
        )
        endpoint = (
            f"{api_url}/public/careers/"
            f"{urllib.parse.quote(careers_path)}?{query}"
        )
        payload = fetch_json_with_headers(
            endpoint,
            headers,
            timeout=timeout,
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("data"),
            list,
        ):
            raise ValueError(
                f"Hireology returned an unexpected jobs payload for {company}"
            )
        rows = payload["data"]
        for job in rows:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("id") or "").strip()
            role = html_to_text(str(job.get("name") or "")).strip()
            if not job_id or not role:
                continue
            url = normalize_job_url(
                str(job.get("career_site_url") or "")
                or (
                    "https://careers.hireology.com/"
                    f"{urllib.parse.quote(careers_path)}/"
                    f"{urllib.parse.quote(job_id)}/description"
                )
            )
            posted_at = normalize_datetime(job.get("created_at"))
            candidates[job_id] = {
                "company": company,
                "role": role,
                "url": url,
                "platform": "hireology",
                "location": hireology_location(job),
                "external_job_id": job_id,
                "posted_at": posted_at,
                "updated_at": normalize_datetime(job.get("updated_at")),
                "source": source.get("url", widget_url),
                "source_query": "all_open_postings",
                "freshness_source": (
                    "hireology_created_at" if posted_at else "first_seen"
                ),
                "notes": (
                    "Hireology official public careers API; the listing "
                    "payload includes the complete job description."
                ),
                "_jd_text": html_to_text(
                    str(job.get("job_description") or "")
                ),
            }
        total = int(payload.get("count") or len(candidates))
        if not rows or page * page_size >= total:
            break
    return list(candidates.values())


def discover_applicantstack_jobs(
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    timeout = int(source.get("timeout", 20))
    raw = fetch_url(source_url, timeout=timeout)
    candidates: dict[str, dict[str, Any]] = {}
    for href, job_token, label in re.findall(
        (
            r'href=["\']([^"\']*/x/detail/'
            r'([^"\'/?#]+)[^"\']*)["\'][^>]*>(.*?)</a>'
        ),
        raw,
        flags=re.I | re.S,
    ):
        url = normalize_job_url(
            urllib.parse.urljoin(source_url, html.unescape(href))
        )
        role = html_to_text(label).strip() or infer_role_from_url(url)
        candidates[job_token] = {
            "company": company,
            "role": role,
            "url": url,
            "platform": "applicantstack",
            "location": str(source.get("default_location") or ""),
            "external_job_id": job_token,
            "posted_at": "",
            "updated_at": "",
            "source": source_url,
            "source_query": "all_open_postings",
            "freshness_source": "first_seen",
            "notes": (
                "ApplicantStack official server-rendered job board; "
                "detail pages supply JobPosting JSON-LD."
            ),
            "_jd_text": "",
        }

    detail_limit = max(
        0,
        min(len(candidates), int(source.get("max_detail_pages", 100))),
    )
    detail_workers = min(max(int(source.get("detail_workers", 8)), 1), 16)

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            detail_raw = fetch_url(candidate["url"], timeout=timeout)
        except Exception:  # noqa: BLE001
            return
        parsed = parse_json_ld_jobs(
            detail_raw,
            candidate["url"],
            fallback_company=company,
        )
        if parsed:
            detail = parsed[0]
            candidate["role"] = str(
                detail.get("role") or candidate["role"]
            ).strip()
            candidate["location"] = str(
                source.get("location_override")
                or detail.get("location")
                or candidate["location"]
            ).strip()
            candidate["external_job_id"] = str(
                detail.get("external_job_id")
                or candidate["external_job_id"]
            ).strip()
            candidate["job_number"] = str(
                detail.get("job_number") or ""
            ).strip()
            candidate["posted_at"] = str(
                detail.get("posted_at") or ""
            ).strip()
            candidate["_jd_text"] = str(
                detail.get("_jd_text") or ""
            ).strip()
        else:
            title = extract_html_title(
                detail_raw,
                company,
                candidate["url"],
            )
            if title:
                candidate["role"] = title
            candidate["location"] = str(
                source.get("location_override")
                or extract_location(detail_raw)
                or candidate["location"]
            ).strip()
            candidate["_jd_text"] = html_to_text(detail_raw)
        if candidate.get("posted_at"):
            candidate["freshness_source"] = (
                "applicantstack_json_ld_date_posted"
            )

    selected = list(candidates.values())[:detail_limit]
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=detail_workers
        ) as executor:
            list(executor.map(enrich, selected))
    return list(candidates.values())


def discover_cyber_recruiter_jobs(
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    timeout = int(source.get("timeout", 20))
    max_list_pages = max(1, int(source.get("max_list_pages", 50)))
    queue = [source_url]
    visited: set[str] = set()
    candidates: dict[str, dict[str, Any]] = {}

    while queue and len(visited) < max_list_pages:
        page_url = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        raw = fetch_url(page_url, timeout=timeout)

        for href in re.findall(
            r'href=["\']([^"\']*type=DRAWSINGLEGROUPLIST2?[^"\']*)["\']',
            raw,
            flags=re.I,
        ):
            next_url = normalize_job_url(
                urllib.parse.urljoin(
                    page_url,
                    html.unescape(href).replace("&amp;", "&"),
                )
            )
            if (
                next_url
                and urllib.parse.urlparse(next_url).netloc
                == urllib.parse.urlparse(source_url).netloc
                and next_url not in visited
                and next_url not in queue
            ):
                queue.append(next_url)

        for row_match in re.finditer(
            r"<tr\b[^>]*>(.*?)</tr>",
            raw,
            flags=re.I | re.S,
        ):
            row = row_match.group(1)
            job_match = re.search(
                (
                    r'<a\b[^>]*href=["\']([^"\']*'
                    r'type=JOBDESCR[^"\']*)["\'][^>]*>(.*?)</a>'
                ),
                row,
                flags=re.I | re.S,
            )
            if not job_match:
                continue
            url = normalize_job_url(
                urllib.parse.urljoin(
                    page_url,
                    html.unescape(job_match.group(1)).replace("&amp;", "&"),
                )
            )
            if not url:
                continue
            role = html_to_text(job_match.group(2)).strip()
            cells = re.findall(
                r"<td\b[^>]*>(.*?)</td>",
                row,
                flags=re.I | re.S,
            )
            location = html_to_text(cells[-1]).strip() if cells else ""
            if not source_location_allowed(source, location):
                continue
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(url).query
            )
            job_id = str((query.get("req") or [""])[0]).strip()
            key = job_id or url
            candidates[key] = {
                "company": company,
                "role": role or infer_role_from_url(url),
                "url": url,
                "platform": "cyber_recruiter",
                "location": location,
                "external_job_id": job_id,
                "job_number": job_id,
                "posted_at": "",
                "updated_at": "",
                "source": source_url,
                "source_query": "all_open_postings",
                "freshness_source": "first_seen",
                "notes": (
                    "Cyber Recruiter official server-rendered job board; "
                    "the site does not expose an authoritative posting date."
                ),
                "_jd_text": "",
            }

    detail_limit = max(
        0,
        min(len(candidates), int(source.get("max_detail_pages", 100))),
    )
    detail_workers = min(max(int(source.get("detail_workers", 8)), 1), 16)

    def enrich(candidate: dict[str, Any]) -> None:
        try:
            raw = fetch_url(candidate["url"], timeout=timeout)
        except Exception:  # noqa: BLE001
            return
        title_match = re.search(
            (
                r'<td\b[^>]*class=["\'][^"\']*HeaderStyle[^"\']*["\']'
                r'[^>]*>(.*?)(?:<br\b[^>]*>|</td>)'
            ),
            raw,
            flags=re.I | re.S,
        )
        if title_match:
            candidate["role"] = (
                html_to_text(title_match.group(1)).strip()
                or candidate["role"]
            )
        location_match = re.search(
            (
                r'<td\b[^>]*class=["\'][^"\']*CaptionStyle[^"\']*["\']'
                r'[^>]*>\s*Location:\s*</td>\s*'
                r'<td\b[^>]*>(.*?)</td>'
            ),
            raw,
            flags=re.I | re.S,
        )
        if location_match:
            candidate["location"] = (
                html_to_text(location_match.group(1)).strip()
                or candidate["location"]
            )
        candidate["_jd_text"] = html_to_text(raw)

    selected = list(candidates.values())[:detail_limit]
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=detail_workers
        ) as executor:
            list(executor.map(enrich, selected))
    return list(candidates.values())


def discover_source_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    platform = source_platform(source)
    if platform == "greenhouse":
        return discover_greenhouse_jobs(source)
    if platform == "lever":
        return discover_lever_jobs(source)
    if platform == "ashby":
        return discover_ashby_jobs(source)
    if platform == "gem":
        return discover_gem_jobs(source)
    if platform == "rippling_jobs":
        return discover_rippling_jobs(source)
    if platform == "ripplehire":
        return discover_ripplehire_jobs(source)
    if platform == "workday":
        return discover_workday_jobs(source)
    if platform == "bytedance_jobs":
        return discover_bytedance_jobs(source)
    if platform == "shein_jobs":
        return discover_shein_jobs(source)
    if platform == "dji_jobs":
        return discover_dji_jobs(source)
    if platform == "alibaba_jobs":
        return discover_alibaba_jobs(source)
    if platform == "pdd_globalhr_jobs":
        return discover_pdd_globalhr_jobs(source)
    if platform == "huawei_jobs":
        return discover_huawei_jobs(source)
    if platform == "phenom":
        return discover_phenom_jobs(source)
    if platform == "m_cloud":
        return discover_m_cloud_jobs(source)
    if platform == "hirebridge":
        return discover_hirebridge_jobs(source)
    if platform == "successfactors":
        return discover_successfactors_jobs(source)
    if platform == "isg_poweredby":
        return discover_isg_poweredby_jobs(source)
    if platform == "microsoft_jobs":
        return discover_microsoft_jobs(source)
    if platform == "amazon_jobs":
        return discover_amazon_jobs(source)
    if platform == "google_jobs":
        return discover_google_jobs(source)
    if platform == "meta_jobs":
        return discover_meta_jobs(source)
    if platform == "eightfold":
        return discover_eightfold_jobs(source)
    if platform == "apple_jobs":
        return discover_apple_jobs(source)
    if platform == "providence_jobs":
        return discover_providence_jobs(source)
    if platform == "jobsyn":
        return discover_jobsyn_jobs(source)
    if platform == "salesforce_jobs":
        return discover_salesforce_jobs(source)
    if platform == "smartrecruiters":
        return discover_smartrecruiters_jobs(source)
    if platform == "topechelon":
        return discover_topechelon_jobs(source)
    if platform == "icims":
        return discover_icims_jobs(source)
    if platform == "oracle_cx":
        return discover_oracle_cx_jobs(source)
    if platform == "workgr8":
        return discover_workgr8_jobs(source)
    if platform == "talentreef":
        return discover_talentreef_jobs(source)
    if platform == "clearcompany":
        return discover_clearcompany_jobs(source)
    if platform == "paylocity":
        return discover_paylocity_jobs(source)
    if platform == "dynamicsats":
        return discover_dynamicsats_jobs(source)
    if platform == "hanford_bms":
        return discover_hanford_bms_jobs(source)
    if platform == "applicantpro":
        return discover_applicantpro_jobs(source)
    if platform == "dayforce":
        return discover_dayforce_jobs(source)
    if platform == "kronos_careers":
        return discover_kronos_careers_jobs(source)
    if platform == "healthcaresource":
        return discover_healthcaresource_jobs(source)
    if platform == "paradox":
        return discover_paradox_jobs(source)
    if platform == "infor_cloudsuite":
        return discover_infor_cloudsuite_jobs(source)
    if platform == "viewpoint_for_cloud":
        return discover_viewpoint_for_cloud_jobs(source)
    if platform == "hireology":
        return discover_hireology_jobs(source)
    if platform == "applicantstack":
        return discover_applicantstack_jobs(source)
    if platform == "cyber_recruiter":
        return discover_cyber_recruiter_jobs(source)
    if platform == "adp_myjobs":
        return discover_adp_myjobs_jobs(source)
    if platform == "adp_workforce_now":
        return discover_adp_workforce_now_jobs(source)
    if platform == "appone":
        return discover_appone_jobs(source)
    if platform == "avature":
        return discover_avature_jobs(source)
    if platform == "jubilant_careers":
        return discover_jubilant_careers_jobs(source)
    if platform == "boa_careers":
        return discover_boa_careers_jobs(source)
    if platform == "jobvite":
        return discover_jobvite_jobs(source)
    if platform == "cadient":
        return discover_cadient_jobs(source)
    if platform == "breezy":
        return discover_breezy_jobs(source)
    if platform == "taleo":
        return discover_taleo_jobs(source)
    if platform == "pageup":
        return discover_pageup_jobs(source)
    if platform == "peopleadmin":
        return discover_peopleadmin_jobs(source)
    if platform == "paycom":
        return discover_paycom_jobs(source)
    if platform == "ultipro":
        return discover_ultipro_jobs(source)
    if platform == "zoho_recruit":
        return discover_zoho_recruit_jobs(source)
    if platform == "workable":
        return discover_workable_jobs(source)
    if platform == "bamboohr":
        return discover_bamboohr_jobs(source)
    if platform == "yc_jobs":
        return discover_yc_jobs(source)
    if platform == "yc_job_board":
        return discover_yc_job_board_jobs(source)
    if platform == "startup_jobs":
        return discover_startup_jobs(source)
    if platform == "builtin_jobs":
        return discover_builtin_jobs(source)
    if platform == "getro_jobs":
        return discover_getro_jobs(source)
    if platform == "consider_jobs":
        return discover_consider_jobs(source)
    if platform == "hn_who_is_hiring":
        return discover_hn_who_is_hiring_jobs(source)
    if platform == "sitemap":
        return discover_sitemap_jobs(source)
    if platform == "governmentjobs":
        return discover_governmentjobs_jobs(source)
    if platform == "governmentjobs_global":
        return discover_governmentjobs_global_jobs(source)
    if platform == "rss":
        return discover_rss_jobs(source)
    if platform == "jibe":
        return discover_jibe_jobs(source)
    if platform == "talentbrew":
        return discover_talentbrew_jobs(source)
    if platform == "ttcportals":
        return discover_ttcportals_jobs(source)
    if platform == "browser_static":
        return discover_browser_static_jobs(source)
    if platform == "careerpuck":
        return discover_careerpuck_jobs(source)
    if platform == "pinpoint":
        return discover_pinpoint_jobs(source)
    if platform == "brassring":
        return discover_brassring_jobs(source)
    if platform == "kula":
        return discover_kula_jobs(source)
    if platform == "jazzhr":
        return discover_jazzhr_jobs(source)
    if platform == "hiringthing":
        return discover_hiringthing_jobs(source)
    if platform == "paycor":
        return discover_paycor_jobs(source)
    if platform == "prismhr":
        return discover_prismhr_jobs(source)
    if platform == "wp_search_index":
        return discover_wp_search_index_jobs(source)
    if platform == "joveo":
        return discover_joveo_jobs(source)
    if platform == "clinch":
        return discover_clinch_jobs(source)
    if platform == "atkins_jobs":
        return discover_atkins_jobs(source)
    if platform == "embedded_jobs":
        return discover_embedded_jobs(source)
    if platform == "wordpress_taleo":
        return discover_wordpress_taleo_jobs(source)
    if platform == "static_html":
        return discover_static_job_board_jobs(
            source,
            "static_html",
            "Official server-rendered job board adapter.",
        )
    return find_links_for_source(source)


def greenhouse_candidate_from_url(url: str) -> dict[str, Any] | None:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "greenhouse.io" not in parsed.netloc.lower() or len(parts) < 3 or parts[-2] != "jobs":
        return None
    board, job_id = parts[0], parts[-1]
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs/{urllib.parse.quote(job_id)}"
    try:
        data = fetch_json(api_url)
    except Exception:
        return None
    normalized_url = normalize_job_url(str(data.get("absolute_url") or url))
    company = data.get("company_name") or board
    location = data.get("location", {}).get("name", "") if isinstance(data.get("location"), dict) else data.get("location", "")
    posted_at = normalize_datetime(data.get("first_published") or data.get("published_at"))
    if not posted_at:
        posted_at = parse_greenhouse_published_at(normalized_url)
    return {
        "company": company,
        "role": data.get("title") or infer_role_from_url(normalized_url),
        "url": normalized_url,
        "platform": "greenhouse",
        "location": location or "",
        "posted_at": posted_at,
        "updated_at": normalize_datetime(data.get("updated_at")),
        "source": f"https://job-boards.greenhouse.io/{board}",
        "notes": "",
    }


def lever_candidate_from_url(url: str) -> dict[str, Any] | None:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "lever.co" not in parsed.netloc.lower() or len(parts) < 2:
        return None
    site, posting_id = parts[0], parts[1]
    api_url = f"https://api.lever.co/v0/postings/{urllib.parse.quote(site)}?mode=json"
    try:
        data = fetch_json(api_url)
    except Exception:
        return None
    for job in data:
        hosted_url = normalize_job_url(str(job.get("hostedUrl") or ""))
        apply_url = normalize_job_url(str(job.get("applyUrl") or ""))
        if posting_id not in hosted_url and posting_id not in apply_url:
            continue
        categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        return {
            "company": site,
            "role": job.get("text") or infer_role_from_url(hosted_url or url),
            "url": hosted_url or apply_url or normalize_job_url(url),
            "platform": "lever",
            "location": categories.get("location", "") or "",
            "posted_at": normalize_datetime(job.get("createdAt")),
            "updated_at": normalize_datetime(job.get("updatedAt")),
            "source": f"https://jobs.lever.co/{site}",
            "notes": "",
        }
    return None


def ashby_candidate_from_url(url: str) -> dict[str, Any] | None:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "ashbyhq.com" not in parsed.netloc.lower() or len(parts) < 2:
        return None
    board, job_id = parts[0], parts[1]
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"
    try:
        data = fetch_json(api_url)
    except Exception:
        return None
    for job in data.get("jobs", []):
        job_url = normalize_job_url(str(job.get("jobUrl") or ""))
        if job.get("id") != job_id and job_id not in job_url:
            continue
        return {
            "company": board,
            "role": job.get("title") or infer_role_from_url(job_url or url),
            "url": job_url or normalize_job_url(url),
            "platform": "ashby",
            "location": job.get("location", "") or "",
            "posted_at": normalize_datetime(job.get("publishedDate") or job.get("publishedAt") or job.get("createdAt")),
            "updated_at": normalize_datetime(job.get("updatedAt")),
            "source": f"https://jobs.ashbyhq.com/{board}",
            "notes": "",
        }
    return None


def workday_candidate_from_url(url: str) -> dict[str, Any] | None:
    parts = workday_source_parts({"url": url})
    if not parts:
        return None
    host, tenant, site = parts
    parsed = urllib.parse.urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    site_index = next((index for index, part in enumerate(path_parts) if part == site), -1)
    if site_index < 0 or site_index + 1 >= len(path_parts):
        return None
    external_path = "/" + "/".join(path_parts[site_index + 1 :])
    if not external_path.startswith("/job/"):
        return None
    try:
        data = fetch_json(workday_api_url(host, tenant, site, external_path))
    except Exception:
        return None
    info = data.get("jobPostingInfo", {}) if isinstance(data, dict) else {}
    normalized_url = workday_human_url(host, site, external_path, tenant=tenant)
    return {
        "company": data.get("hiringOrganization", {}).get("name") if isinstance(data.get("hiringOrganization"), dict) else tenant,
        "role": info.get("title") or infer_role_from_url(normalized_url),
        "url": normalized_url,
        "platform": "workday",
        "location": workday_location_text(info),
        "posted_at": parse_workday_posted_on(info.get("postedOn") or info.get("startDate")),
        "updated_at": normalize_datetime(info.get("startDate")),
        "source": workday_board_url(host, tenant, site),
        "job_number": info.get("jobReqId", ""),
        "external_job_id": info.get("jobPostingId", ""),
        "notes": "",
    }


def ats_candidate_from_url(url: str) -> dict[str, Any] | None:
    platform = detect_platform(url)
    if platform == "greenhouse":
        return greenhouse_candidate_from_url(url)
    if platform == "lever":
        return lever_candidate_from_url(url)
    if platform == "ashby":
        return ashby_candidate_from_url(url)
    if platform == "workday":
        return workday_candidate_from_url(url)
    if platform == "ripplehire":
        parsed = parse_ripplehire_url(url)
        if not parsed:
            return None
        base_url, token, source_name, job_seq = parsed
        try:
            job = ripplehire_detail({"url": base_url, "token": token, "ripplehire_source": source_name}, job_seq)
        except Exception:
            return None
        if not job:
            return None
        role = str(job.get("jobTitle") or infer_role_from_url(url)).strip()
        company = "UST / USource" if "usource.ripplehire.com" in urllib.parse.urlparse(base_url).netloc.lower() else "Unknown Company"
        return {
            "company": company,
            "role": role,
            "url": url,
            "platform": "ripplehire",
            "location": compact_location_text(job.get("locations") or job.get("jobLocation")),
            "job_number": str(job.get("jobCode") or job_seq),
            "external_job_id": job_seq,
            "posted_at": ripplehire_posted_at(job.get("jobPostingDate") or job.get("careerSiteDate")),
            "updated_at": ripplehire_posted_at(job.get("updatedDate") or job.get("modifiedDttm")),
            "notes": f"RippleHire/USource direct URL; source={source_name}",
            "_jd_text": ripplehire_job_text(job),
        }
    return None


def discovery_title_matches(candidate: dict[str, Any], profile: dict[str, Any]) -> bool:
    role = str(candidate.get("role", "")).lower()
    if not role:
        return False
    combined = f"{candidate.get('role', '')} {candidate.get('url', '')}".lower()
    if re.search(r"\b(senior|sr\.?|staff|principal|distinguished|manager|director|lead|head|vp|chief|cto|faculty|professor|intern|internship)\b", combined):
        return False
    if re.search(r"\b(canada|france|india|united kingdom|uk|london|paris|toronto|vancouver)\b", combined):
        return False
    profile_terms = [
        str(item).lower()
        for item in profile.get("targets", {}).get("roles", [])
        if str(item).strip()
    ]
    track_terms = [
        str(item).lower()
        for item in profile.get("_track", {}).get("discovery_title_keywords", [])
        if str(item).strip()
    ]
    terms = profile_terms + track_terms
    if not track_terms:
        terms += DEFAULT_DISCOVERY_TITLE_KEYWORDS
    return any(re.search(rf"\b{re.escape(term)}\b", role) for term in terms)


MAYBE_TITLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "general_sde": (
        r"\bsoftware\s+(?:development\s+)?engineer\b",
        r"\bsoftware\s+developer\b",
        r"\b(?:back[- ]?end|front[- ]?end|full[- ]?stack|product|platform|devops|developer tools?)\s+(?:software\s+)?engineer\b",
        r"\b(?:founding|integration|integrations)\s+engineer\b",
        r"\bsite reliability engineer\b|\bsre\b",
    ),
    "qa_engineer": (
        r"\bqa\b|\bquality assurance\b|\bsdet\b",
        r"\bsoftware (?:development )?engineer in test\b",
        r"\b(?:software )?test automation engineer\b",
        r"\bautomation qa engineer\b|\bqa automation engineer\b",
        r"\bsoftware quality engineer\b",
    ),
    "fde_ai_engineer": (
        r"\bforward deployed (?:software |ai )?engineer\b",
        r"\b(?:applied |generative |gen)?ai (?:solutions |product )?engineer\b",
        r"\bmachine learning engineer\b",
        r"\b(?:solutions|customer|implementation|integration) engineer\b",
        r"\bai (?:transformation|operations|program|enablement) (?:coordinator|specialist|analyst)\b",
    ),
    "traditional_it_wa": (
        r"\b(?:technical|application|production|desktop|computer|technology|it|integration|integrations) support (?:engineer|analyst|specialist|technician)\b",
        r"\b(?:help|service) desk (?:engineer|analyst|specialist|technician)\b",
        r"\bit operations (?:engineer|analyst|specialist|technician)\b",
        r"\b(?:systems?|business systems|application systems|it systems) (?:analyst|administrator|specialist)\b",
        r"\b(?:network|cloud|server|infrastructure) (?:analyst|administrator|specialist|engineer)\b",
        r"\b(?:information|cyber) security (?:analyst|administrator|specialist|engineer)\b",
        r"\b(?:database|data systems|data management|business intelligence|reporting|technology) analyst\b",
        r"\b(?:application|applications|data systems|database|report|bi|integration|gis) developer\b",
        r"\b(?:workday|erp|gis) (?:developer|analyst|administrator|specialist)\b",
        r"\b(?:devops|cloud operations) (?:engineer|analyst|specialist)\b",
        r"\b(?:qa|quality assurance) analyst\b",
        r"\b(?:technology|it|ai transformation|ai operations|ai enablement) coordinator\b",
        r"\b(?:implementation|integration|integrations|technical support|customer support) specialist\b",
        r"\bit (?:application development|business analysis|customer support|data management|network and "
        r"telecommunications|project management|security|system administration)(?:\s*[-–]\s*(?:entry|journey|expert))?\b",
        r"\bforms? (?:and|&) records? analyst\b",
    ),
    "data_center_infra": (
        r"\bdata cent(?:er|re) (?:operations )?(?:engineer|technician|specialist)\b",
        r"\bnetwork (?:operations |development )?(?:engineer|technician|specialist)\b",
        r"\binfrastructure operations (?:engineer|technician|specialist)\b",
        r"\bsystems? (?:operations )?(?:engineer|technician|administrator)\b",
    ),
}

MAYBE_TITLE_NEGATIVE_PATTERN = re.compile(
    r"\b(?:nurse|rn|physician|therapist|clinician|medical assistant|patient services|pharmacy|pharmacist|"
    r"surgical|radiologic|dental|dietitian|food service|cook|custodian|security officer|sales|marketing|"
    r"recruiter|account executive|attorney|paralegal|social worker|counselor|teacher|mechanical|electrical|"
    r"construction|facilities|warehouse|driver)\b",
    flags=re.I,
)


def maybe_backlog_title_relevant(candidate: dict[str, Any], profile: dict[str, Any]) -> bool:
    """Keep fuzzy candidates only when the title still belongs to the active track."""
    role = re.sub(r"\s+", " ", str(candidate.get("role") or "")).strip().lower()
    if not role or MAYBE_TITLE_NEGATIVE_PATTERN.search(role):
        return False
    if re.search(r"\b(?:senior|sr\.?|staff|principal|distinguished|lead|manager|director|head|vp|chief|cto|intern|internship)\b", role):
        return False
    track_id = str(profile.get("_track", {}).get("id") or "").strip()
    patterns = MAYBE_TITLE_PATTERNS.get(track_id)
    if not patterns:
        patterns = tuple(pattern for values in MAYBE_TITLE_PATTERNS.values() for pattern in values)
    return any(re.search(pattern, role, flags=re.I) for pattern in patterns)


def unclassified_technical_title_relevant(candidate: dict[str, Any]) -> bool:
    role = re.sub(r"\s+", " ", str(candidate.get("role") or "")).strip().lower()
    if not role or MAYBE_TITLE_NEGATIVE_PATTERN.search(role):
        return False
    if re.search(r"\b(?:senior|sr\.?|staff|principal|distinguished|lead|manager|director|head|vp|chief|cto|intern|internship)\b", role):
        return False
    return bool(
        re.search(
            r"\b(?:software|developer|data|cloud|platform|infrastructure|systems?|application|technical|"
            r"technology|it|qa|test|automation|integration|support|network|database|devops|sre)\b",
            role,
        )
    )


US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
}
US_STATE_ABBREVIATIONS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia",
    "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt",
    "va", "wa", "wv", "wi", "wy", "dc",
}
WA_LOCATION_PATTERN = re.compile(
    r"\b(?:washington state|seattle|bellevue|redmond|kirkland|tacoma|everett|renton|bothell|olympia|"
    r"spokane|spokane valley|liberty lake|pullman|cheney|lacey|tumwater|bellingham|mount vernon|"
    r"bremerton|silverdale|poulsbo|yakima|richland|kennewick|pasco|tri[- ]cities|wenatchee|"
    r"ellensburg|moses lake|quincy|longview|centralia|aberdeen|walla walla|sunnyside|"
    r"vancouver,?\s+wa|washington,?\s+(?:united states|usa|u\.s\.)|"
    r"wa,?\s+(?:united states|usa|u\.s\.))\b",
    flags=re.I,
)
REMOTE_US_PATTERN = re.compile(
    r"\b(?:remote\s*(?:[-,(]|in\s+)?\s*(?:united states|usa|u\.s\.|us)\b|"
    r"(?:united states|usa|u\.s\.|us)\s*[-,()]?\s*remote\b|us[- ]based remote|remote us[- ]based)\b",
    flags=re.I,
)
FOREIGN_LOCATION_PATTERN = re.compile(
    r"(?:\b(?:canada|toronto|montreal|calgary|edmonton|british columbia|ontario|quebec|india|argentina|"
    r"colombia|mexico|spain|germany|poland|egypt|japan|france|ireland|netherlands|singapore|australia|"
    r"new zealand|brazil|vancouver|london|paris|berlin|latam|latin america|emea|apac|europe|"
    r"european union|united kingdom|uk only|eu only)\b|\bremote\s*\(ca\))",
    flags=re.I,
)
US_LOCATION_PATTERN = re.compile(
    r"(?:\b(?:united states(?: of america)?|usa|us[- ]based)\b|\bu\.s\.?(?:\b|$))",
    flags=re.I,
)


def location_preference_bucket(location: str, profile: dict[str, Any] | None = None, context: str = "") -> str:
    """Classify location without treating accepted US relocation roles as dealbreakers."""
    value = re.sub(r"\s+", " ", str(location or "")).strip()
    context_value = re.sub(r"\s+", " ", str(context or "")).strip()
    if not value:
        return "maybe"

    lower = value.lower()
    is_washington_dc = bool(
        re.search(r"\b(?:washington,?\s+(?:district of columbia|dc)|district of columbia|washington dc)\b", lower)
    )
    explicit_us = bool(US_LOCATION_PATTERN.search(lower)) or lower in {"us", "u.s", "u.s."}
    if REMOTE_US_PATTERN.search(lower):
        return "preferred"
    if not is_washington_dc and (
        lower == "washington"
        or WA_LOCATION_PATTERN.search(lower)
        or re.search(r"(?:^|[,;/])\s*wa(?:\s+state)?\s*(?:[,;/]|$)", lower)
    ):
        return "preferred"

    state_name = any(re.search(rf"\b{re.escape(state)}\b", lower) for state in US_STATE_NAMES)
    state_code = bool(
        re.search(
            rf"(?:^|[,;/])\s*(?:{'|'.join(sorted(US_STATE_ABBREVIATIONS))})\s*(?:[,;/]|$)",
            lower,
        )
    )
    explicit_us_location = explicit_us or state_name or state_code
    foreign_location = bool(FOREIGN_LOCATION_PATTERN.search(lower)) or bool(
        re.search(r"\b(?:ab|bc|on|qc),\s*ca\b", lower)
    )
    if foreign_location and not explicit_us_location:
        return "rejected"
    if explicit_us_location:
        return "relocation"

    if context_value and re.search(
        r"\b(?:must|need to|required to)\s+(?:already\s+)?(?:have|hold)\s+(?:the\s+)?(?:right|authorization)\s+to\s+work\s+in\s+(?!the united states|united states|usa|u\.s\.)",
        context_value,
        flags=re.I,
    ):
        return "rejected"
    if re.search(r"\bremote\b", lower):
        return "maybe"
    return "maybe"


def location_allowed(location: str, profile: dict[str, Any]) -> bool:
    """Backward-compatible boolean gate; only clearly non-US locations are rejected."""
    return location_preference_bucket(location, profile) != "rejected"


def hn_review_decision(app: dict[str, Any]) -> tuple[str, int, list[str]]:
    text = " ".join(str(app.get(key, "")) for key in ["company", "role", "location", "url"]).lower()
    role_text = " ".join(str(app.get(key, "")) for key in ["company", "role"]).lower()
    location = str(app.get("location", "")).lower()
    reasons: list[str] = []
    score = 0

    explicit_allowed_location = re.search(
        r"\b(remote \(us|remote us|remote usa|remote \(usa|us only|usa|united states|us-based|us based|seattle|bellevue|washington|"
        r"san francisco|sf|bay area|palo alto|sunnyvale|san carlos|san jose|california|ca\b)",
        location,
    )
    ca_wa_location = re.search(
        r"\b(seattle|bellevue|redmond|kirkland|washington|wa\b|san francisco|sf|bay area|palo alto|sunnyvale|san carlos|san jose|california|ca\b)",
        location,
    )
    remote_unspecified = bool(re.search(r"\bremote\b", location)) and not explicit_allowed_location
    foreign_or_ambiguous = [
        "eu only",
        "europe",
        "emea",
        "apac",
        "aus/nz",
        "australia",
        "new zealand",
        "uk",
        "london",
        "paris",
        "berlin",
        "germany",
        "poland",
        "cest",
        "cet",
        "canada",
        "worldwide",
        "world",
        "anywhere",
    ]
    if any(marker in location for marker in foreign_or_ambiguous) and not explicit_allowed_location:
        reasons.append("location outside WA/CA/Remote US preference")
        return "skip", -10, reasons
    if "unknown (hn)" in location or not location.strip():
        reasons.append("HN location is unknown")
        return "review", -1, reasons
    if explicit_allowed_location:
        score += 5
        reasons.append("location matches WA/CA/Remote US")
    elif remote_unspecified:
        score += 1
        reasons.append("remote is unspecified; verify US eligibility")
    elif not ca_wa_location:
        reasons.append("location needs manual verification")
        return "review", 0, reasons

    bad_role_markers = [
        "seeking work",
        "seeking freelancer",
        "freelancer",
        "faculty",
        "professor",
        "developer advocate",
    ]
    if any(marker in role_text for marker in bad_role_markers):
        reasons.append("role type is outside target")
        return "skip", score - 5, reasons
    if re.search(r"\b(senior|staff|principal|lead|manager|director|head|vp|chief|cto)\b", role_text):
        reasons.append("senior/leadership signal")
        score -= 3

    strong_terms = [
        "software engineer",
        "backend",
        "full stack",
        "full-stack",
        "ai engineer",
        "applied ai",
        "platform",
        "infra",
        "infrastructure",
        "devops",
        "sre",
        "mobile",
    ]
    if any(term in text for term in strong_terms):
        score += 3
        reasons.append("role matches target engineering keywords")
    else:
        reasons.append("role fit needs review")
        return "review", score, reasons

    return ("keep" if score >= 5 else "review"), score, reasons


def command_review_hn(args: argparse.Namespace) -> None:
    require_person_files()
    tracker = load_tracker()
    apps = tracker.get("applications", [])
    target_statuses = set(args.status or ["found", "needs_review", "scored"])
    rows: list[tuple[str, int, list[str], dict[str, Any]]] = []
    for app in apps:
        if app.get("source_query") != "Ask HN: Who is hiring? (May 2026)":
            continue
        if app.get("status") not in target_statuses:
            continue
        decision, score, reasons = hn_review_decision(app)
        rows.append((decision, score, reasons, app))

    order = {"keep": 0, "review": 1, "skip": 2}
    rows.sort(key=lambda item: (order[item[0]], -item[1], str(item[3].get("company", "")).lower()))
    print(f"HN review candidates: {len(rows)}")
    print(f"Keep: {sum(1 for row in rows if row[0] == 'keep')}")
    print(f"Review: {sum(1 for row in rows if row[0] == 'review')}")
    print(f"Skip: {sum(1 for row in rows if row[0] == 'skip')}")

    if args.apply:
        changed = 0
        for decision, _score, reasons, app in rows:
            if decision != "skip":
                continue
            note = f"Skipped by HN review: {'; '.join(reasons)}."
            app["status"] = "skipped"
            existing_notes = str(app.get("notes", "")).strip()
            app["notes"] = f"{existing_notes}; {note}" if existing_notes else note
            changed += 1
        if changed:
            save_tracker(tracker)
        print(f"Applied skips: {changed}")

    limit = args.limit
    for group in ["keep", "review", "skip"]:
        group_rows = [row for row in rows if row[0] == group]
        print(f"\n=== {group.upper()} ({len(group_rows)}) ===")
        for index, (_decision, score, reasons, app) in enumerate(group_rows[:limit], 1):
            print(f"{index:02d}. [{score:+}] {app.get('company')} — {app.get('role')}")
            print(f"    loc: {app.get('location') or 'Unknown'}")
            print(f"    url: {app.get('url')}")
            print(f"    why: {'; '.join(reasons)}")
        if len(group_rows) > limit:
            print(f"    ... {len(group_rows) - limit} more")


def empty_discovery_stats() -> dict[str, int]:
    return {
        "discovered": 0,
        "added": 0,
        "existing": 0,
        "maybe_backlog": 0,
        "skipped_old": 0,
        "skipped_unknown_date": 0,
        "skipped_title": 0,
        "skipped_location": 0,
        "maybe_scored": 0,
        "scoring_failed": 0,
    }


def process_discovered_candidates(
    candidates: list[dict[str, Any]],
    args: argparse.Namespace,
    profile: dict[str, Any],
    seen: dict[str, Any],
    cutoff: dt.datetime | None,
    current_seen_at: str,
) -> dict[str, int]:
    stats = empty_discovery_stats()
    seen_jobs = seen.setdefault("jobs", {})
    existing_apps_by_url = {
        normalize_job_url(app.get("url", "")): app
        for app in load_tracker().get("applications", [])
        if app.get("url")
    }
    track = load_track(getattr(args, "track", None))
    track_id = str(track.get("id", "")).strip()
    track_resume = path_from_track(track, "resume_file") if track else None
    maybe_score_limit = max(0, int(getattr(args, "score_maybe_limit", 3) or 0))
    for candidate in candidates:
        stats["discovered"] += 1
        if track_id:
            candidate["target_track"] = track_id
            candidate["matched_tracks"] = [track_id]
            if track_resume:
                candidate["resume_file"] = str(track_resume)
        candidate["url"] = normalize_job_url(candidate["url"])
        key = candidate["url"]
        existing_app = existing_apps_by_url.get(key)
        needs_existing_cross_track_evaluation = bool(
            track_id
            and existing_app
            and track_evaluation_key(track_id) not in track_evaluations_with_legacy(existing_app)
        )
        seen_record = seen_jobs.get(key)
        seen_created = seen_record is None
        if seen_record is None:
            seen_record = {
                "company": candidate.get("company", ""),
                "role": candidate.get("role", ""),
                "url": key,
                "first_seen": current_seen_at,
                "last_seen": current_seen_at,
            }
            seen_jobs[key] = seen_record
        for field in [
            "company",
            "role",
            "platform",
            "location",
            "posted_at",
            "updated_at",
            "source",
            "source_query",
            "freshness_source",
            "job_number",
            "external_job_id",
        ]:
            value = candidate.get(field)
            if value and seen_record.get(field) != value:
                seen_record[field] = value
        if track_id:
            seen_record["matched_tracks"] = merge_unique(seen_record.get("matched_tracks", []), [track_id])
            if not seen_record.get("target_track"):
                seen_record["target_track"] = track_id
            if candidate.get("resume_file") and (
                not seen_record.get("resume_file") or seen_record.get("target_track") == track_id
            ):
                seen_record["resume_file"] = candidate["resume_file"]
        candidate["first_seen"] = seen_record.get("first_seen", current_seen_at)
        candidate["last_seen"] = current_seen_at

        posted_at = parse_datetime(candidate.get("posted_at"))
        maybe_reasons: list[str] = []
        if not posted_at:
            if not args.include_unknown_posted_date:
                if getattr(args, "include_maybe_backlog", False):
                    maybe_reasons.append("unknown_posted_at")
                else:
                    stats["skipped_unknown_date"] += 1
                    continue
        elif cutoff and posted_at < cutoff:
            if (
                getattr(args, "include_maybe_backlog", False)
                and getattr(args, "maybe_old_posted_date", False)
                and (seen_created or needs_existing_cross_track_evaluation)
            ):
                maybe_reasons.append(
                    "old_posted_at_new_track"
                    if needs_existing_cross_track_evaluation and not seen_created
                    else "old_posted_at_new_to_us"
                )
            else:
                stats["skipped_old"] += 1
                continue
        exact_title_match = args.no_role_filter or discovery_title_matches(candidate, profile)
        if not exact_title_match:
            if (
                getattr(args, "include_maybe_backlog", False)
                and maybe_backlog_title_relevant(candidate, profile)
            ):
                maybe_reasons.append("fuzzy_title")
            else:
                stats["skipped_title"] += 1
                continue
        location_bucket = location_preference_bucket(candidate.get("location", ""), profile)
        candidate["location_bucket"] = location_bucket
        seen_record["location_bucket"] = location_bucket
        if location_bucket == "rejected":
            stats["skipped_location"] += 1
            continue
        if location_bucket == "maybe":
            maybe_reasons.append("unknown_location")
        if maybe_reasons:
            candidate["review_bucket"] = "maybe"
            candidate["discovery_bucket"] = "maybe_backlog"
            existing_notes = str(candidate.get("notes") or "").strip()
            reason_text = "maybe_backlog: " + ", ".join(maybe_reasons)
            candidate["notes"] = "; ".join(item for item in [existing_notes, reason_text] if item)
            stats["maybe_backlog"] += 1

        app, created = upsert_application(candidate)
        existing_apps_by_url[key] = app
        if created:
            stats["added"] += 1
        else:
            stats["existing"] += 1
        if maybe_reasons:
            app = update_application(
                app["id"],
                {
                    "status": "needs_review" if app.get("status") == "found" else app.get("status", "needs_review"),
                    "review_bucket": "maybe",
                    "discovery_bucket": "maybe_backlog",
                    "location_bucket": candidate.get("location_bucket", "maybe"),
                    "notes": candidate.get("notes", app.get("notes", "")),
                },
            )
        evaluation_track_id = track_evaluation_key(track_id)
        current_track_evaluated = evaluation_track_id in track_evaluations_with_legacy(app)
        scoreable_status = app.get("status") in {"found", "needs_review", "needs_retry", "scored", "skipped"}
        needs_track_evaluation = not current_track_evaluated
        should_score_maybe = (
            bool(maybe_reasons)
            and bool(args.score)
            and stats["maybe_scored"] < maybe_score_limit
            and (exact_title_match or maybe_backlog_title_relevant(candidate, profile))
            and scoreable_status
            and (needs_track_evaluation or app.get("status") in {"found", "needs_retry"})
            and not app.get("date_applied")
        )
        should_score_strict = (
            bool(args.score)
            and not maybe_reasons
            and scoreable_status
            and (needs_track_evaluation or app.get("status") in {"found", "needs_retry"})
            and not app.get("date_applied")
        )
        if should_score_strict or should_score_maybe:
            try:
                command_score_job(
                    argparse.Namespace(
                        id=app["id"],
                        jd_file=None,
                        track=track_id or None,
                        quiet=bool(getattr(args, "quiet", False)),
                    )
                )
                if should_score_maybe:
                    stats["maybe_scored"] += 1
            except Exception as error:  # noqa: BLE001
                stats["scoring_failed"] += 1
                update_application(app["id"], {"status": "needs_review", "notes": f"Scoring failed: {error}"})
    return stats


def process_discovered_candidates_all_tracks(
    candidates: list[dict[str, Any]],
    args: argparse.Namespace,
    profiles: dict[str, dict[str, Any]],
    seen: dict[str, Any],
    cutoff: dt.datetime | None,
    current_seen_at: str,
    score_queue: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    stats = empty_discovery_stats()
    seen_jobs = seen.setdefault("jobs", {})
    tracker_apps_by_url = {
        normalize_job_url(app.get("url", "")): app
        for app in load_tracker().get("applications", [])
        if app.get("url")
    }
    maybe_score_limit = max(0, int(getattr(args, "score_maybe_limit", 3) or 0))
    for candidate in candidates:
        stats["discovered"] += 1
        candidate["url"] = normalize_job_url(candidate["url"])
        key = candidate["url"]
        existing_app = tracker_apps_by_url.get(key)
        seen_record = seen_jobs.get(key)
        seen_created = seen_record is None
        if seen_record is None:
            seen_record = {
                "company": candidate.get("company", ""),
                "role": candidate.get("role", ""),
                "url": key,
                "first_seen": current_seen_at,
                "last_seen": current_seen_at,
            }
            seen_jobs[key] = seen_record
        for field in [
            "company",
            "role",
            "platform",
            "location",
            "posted_at",
            "updated_at",
            "source",
            "source_query",
            "freshness_source",
            "job_number",
            "external_job_id",
        ]:
            value = candidate.get(field)
            if value and seen_record.get(field) != value:
                seen_record[field] = value
        candidate["first_seen"] = seen_record.get("first_seen", current_seen_at)
        candidate["last_seen"] = current_seen_at

        exact_tracks: list[str] = []
        fuzzy_tracks: list[str] = []
        for track_id, profile in profiles.items():
            if args.no_role_filter or discovery_title_matches(candidate, profile):
                exact_tracks.append(track_id)
            elif getattr(args, "include_maybe_backlog", False) and maybe_backlog_title_relevant(candidate, profile):
                fuzzy_tracks.append(track_id)
        matched_tracks = merge_unique(exact_tracks, fuzzy_tracks)
        unclassified_technical = bool(
            not matched_tracks
            and getattr(args, "include_maybe_backlog", False)
            and unclassified_technical_title_relevant(candidate)
        )
        if not matched_tracks and not unclassified_technical:
            stats["skipped_title"] += 1
            continue
        location_bucket = location_preference_bucket(
            candidate.get("location", ""),
            next(iter(profiles.values()), {}),
        )
        candidate["location_bucket"] = location_bucket
        seen_record["location_bucket"] = location_bucket
        if location_bucket == "rejected":
            stats["skipped_location"] += 1
            continue

        evaluation_keys = matched_tracks or ["default"]
        existing_evaluations = track_evaluations_with_legacy(existing_app or {})
        needs_cross_track_evaluation = any(
            key_name not in existing_evaluations or existing_evaluations[key_name].get("status") == "needs_retry"
            for key_name in evaluation_keys
        )
        maybe_reasons: list[str] = []
        posted_at = parse_datetime(candidate.get("posted_at"))
        if not posted_at:
            if not args.include_unknown_posted_date:
                if getattr(args, "include_maybe_backlog", False):
                    maybe_reasons.append("unknown_posted_at")
                else:
                    stats["skipped_unknown_date"] += 1
                    continue
        elif cutoff and posted_at < cutoff:
            if (
                getattr(args, "include_maybe_backlog", False)
                and getattr(args, "maybe_old_posted_date", False)
                and (seen_created or existing_app is None or needs_cross_track_evaluation)
            ):
                maybe_reasons.append(
                    "old_posted_at_new_track"
                    if existing_app is not None and needs_cross_track_evaluation
                    else "old_posted_at_new_to_us"
                )
            else:
                stats["skipped_old"] += 1
                continue
        if not exact_tracks and fuzzy_tracks:
            maybe_reasons.append("fuzzy_title")
        if unclassified_technical:
            maybe_reasons.append("unclassified_technical")
        if location_bucket == "maybe":
            maybe_reasons.append("unknown_location")

        candidate["matched_tracks"] = matched_tracks
        selected_track = str((existing_app or {}).get("target_track") or "")
        if not selected_track and matched_tracks:
            selected_track = exact_tracks[0] if exact_tracks else matched_tracks[0]
        candidate["target_track"] = selected_track
        if selected_track in profiles:
            selected_track_config = profiles[selected_track].get("_track", {})
            selected_resume = path_from_track(selected_track_config, "resume_file")
            if selected_resume:
                candidate["resume_file"] = str(selected_resume)
        seen_record["matched_tracks"] = merge_unique(seen_record.get("matched_tracks", []), matched_tracks)
        if selected_track and not seen_record.get("target_track"):
            seen_record["target_track"] = selected_track

        if maybe_reasons:
            candidate["review_bucket"] = "maybe"
            candidate["discovery_bucket"] = "maybe_backlog"
            existing_notes = str(candidate.get("notes") or "").strip()
            reason_text = "maybe_backlog: " + ", ".join(maybe_reasons)
            candidate["notes"] = "; ".join(item for item in [existing_notes, reason_text] if item)
            stats["maybe_backlog"] += 1

        app, created = upsert_application(candidate)
        tracker_apps_by_url[key] = app
        if created:
            stats["added"] += 1
        else:
            stats["existing"] += 1
        if maybe_reasons:
            app = update_application(
                app["id"],
                {
                    "status": "needs_review" if app.get("status") == "found" else app.get("status", "needs_review"),
                    "review_bucket": "maybe",
                    "discovery_bucket": "maybe_backlog",
                    "location_bucket": candidate.get("location_bucket", "maybe"),
                    "notes": candidate.get("notes", app.get("notes", "")),
                },
            )
            tracker_apps_by_url[key] = app

        if not args.score or app.get("date_applied"):
            continue
        scoreable_status = app.get("status") in {"found", "needs_review", "needs_retry", "scored", "skipped"}
        if not scoreable_status:
            continue
        evaluations = track_evaluations_with_legacy(app)
        tracks_to_score = [
            key_name
            for key_name in evaluation_keys
            if key_name not in evaluations or evaluations[key_name].get("status") == "needs_retry"
        ]
        if not tracks_to_score:
            continue
        if score_queue is None and maybe_reasons and stats["maybe_scored"] >= maybe_score_limit:
            continue

        if score_queue is not None:
            posted_timestamp = posted_at.timestamp() if posted_at else 0.0
            maybe_priority = (
                (4.0 if created else 0.0)
                + (2.0 * len(exact_tracks))
                + (0.5 * len(fuzzy_tracks))
                + (1.0 if posted_timestamp else 0.0)
                - (2.0 if unclassified_technical else 0.0)
                - (1.0 if "old_posted_at_new_to_us" in maybe_reasons else 0.0)
            )
            for evaluation_key in tracks_to_score:
                score_queue.append(
                    {
                        "app_id": app["id"],
                        "track": None if evaluation_key == "default" else evaluation_key,
                        "maybe": bool(maybe_reasons),
                        "priority": maybe_priority,
                        "posted_timestamp": posted_timestamp,
                    }
                )
            continue

        scored_any = False
        for evaluation_key in tracks_to_score:
            try:
                command_score_job(
                    argparse.Namespace(
                        id=app["id"],
                        jd_file=None,
                        track=None if evaluation_key == "default" else evaluation_key,
                        quiet=bool(getattr(args, "quiet", False)),
                    )
                )
                scored_any = True
            except Exception as error:  # noqa: BLE001
                stats["scoring_failed"] += 1
                update_application(app["id"], {"status": "needs_review", "notes": f"Scoring failed: {error}"})
        if maybe_reasons and scored_any:
            stats["maybe_scored"] += 1
    return stats


def deduplicate_score_queue(score_queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for task in score_queue:
        key = (str(task.get("app_id") or ""), str(task.get("track") or ""))
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = dict(task)
            continue
        existing["maybe"] = bool(existing.get("maybe")) and bool(task.get("maybe"))
        existing["priority"] = max(float(existing.get("priority") or 0), float(task.get("priority") or 0))
        existing["posted_timestamp"] = max(
            float(existing.get("posted_timestamp") or 0),
            float(task.get("posted_timestamp") or 0),
        )
    return list(deduplicated.values())


def select_discovery_score_tasks(
    score_queue: list[dict[str, Any]],
    max_maybe_scores: int,
) -> tuple[list[dict[str, Any]], int, int]:
    tasks = deduplicate_score_queue(score_queue)
    strict_tasks = [task for task in tasks if not task.get("maybe")]
    maybe_tasks = [task for task in tasks if task.get("maybe")]
    maybe_apps: dict[str, dict[str, Any]] = {}
    for task in maybe_tasks:
        app_id = str(task.get("app_id") or "")
        candidate = maybe_apps.setdefault(
            app_id,
            {
                "priority": float(task.get("priority") or 0),
                "posted_timestamp": float(task.get("posted_timestamp") or 0),
            },
        )
        candidate["priority"] = max(candidate["priority"], float(task.get("priority") or 0))
        candidate["posted_timestamp"] = max(
            candidate["posted_timestamp"],
            float(task.get("posted_timestamp") or 0),
        )
    ranked_maybe_apps = sorted(
        maybe_apps,
        key=lambda app_id: (
            maybe_apps[app_id]["priority"],
            maybe_apps[app_id]["posted_timestamp"],
            app_id,
        ),
        reverse=True,
    )
    selected_maybe_apps = set(ranked_maybe_apps[: max(0, max_maybe_scores)])
    selected_maybe_tasks = [task for task in maybe_tasks if str(task.get("app_id") or "") in selected_maybe_apps]
    return strict_tasks + selected_maybe_tasks, len(maybe_apps), len(selected_maybe_apps)


def prefetch_job_description(app_id: str) -> str:
    app = get_application(app_id)
    output_dir = app_output_dir(app)
    output_dir.mkdir(parents=True, exist_ok=True)
    jd_path = output_dir / "jd.md"
    jd_text = cached_job_text_for_scoring(app)
    jd_path.write_text(jd_text.rstrip() + "\n", encoding="utf-8")
    return str(jd_path)


def execute_discovery_score_queue(
    score_queue: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, int]:
    max_maybe_scores = max(0, int(getattr(args, "max_maybe_scores", 20) or 0))
    selected_tasks, maybe_candidates, maybe_selected = select_discovery_score_tasks(
        score_queue,
        max_maybe_scores,
    )
    summary = {
        "queued_tasks": len(deduplicate_score_queue(score_queue)),
        "selected_tasks": len(selected_tasks),
        "unique_apps": len({str(task.get("app_id") or "") for task in selected_tasks}),
        "maybe_candidates": maybe_candidates,
        "maybe_selected": maybe_selected,
        "maybe_scored": 0,
        "scoring_failed": 0,
    }
    if not selected_tasks:
        return summary

    score_workers = max(1, int(getattr(args, "score_workers", 4) or 1))
    app_ids = sorted({str(task.get("app_id") or "") for task in selected_tasks})
    jd_paths: dict[str, str] = {}
    if score_workers == 1 or len(app_ids) <= 1:
        for app_id in app_ids:
            try:
                jd_paths[app_id] = prefetch_job_description(app_id)
            except Exception:  # noqa: BLE001 - scoring records the retry state below.
                jd_paths[app_id] = ""
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(score_workers, len(app_ids))
        ) as executor:
            future_to_app_id = {
                executor.submit(prefetch_job_description, app_id): app_id
                for app_id in app_ids
            }
            for future in concurrent.futures.as_completed(future_to_app_id):
                app_id = future_to_app_id[future]
                try:
                    jd_paths[app_id] = future.result()
                except Exception:  # noqa: BLE001 - scoring records the retry state below.
                    jd_paths[app_id] = ""

    maybe_scored_apps: set[str] = set()
    for task in selected_tasks:
        app_id = str(task.get("app_id") or "")
        try:
            command_score_job(
                argparse.Namespace(
                    id=app_id,
                    jd_file=jd_paths.get(app_id) or None,
                    track=task.get("track"),
                    quiet=True,
                    preserve_notes=bool(getattr(args, "preserve_notes", False)),
                )
            )
            if task.get("maybe"):
                maybe_scored_apps.add(app_id)
        except Exception as error:  # noqa: BLE001
            summary["scoring_failed"] += 1
            failure_note = f"Scoring failed: {error}"
            updates = {"status": "needs_review", "notes": failure_note}
            if bool(getattr(args, "preserve_notes", False)):
                existing_notes = str(get_application(app_id).get("notes") or "").strip()
                if existing_notes and failure_note not in existing_notes:
                    updates["notes"] = f"{existing_notes}\n{failure_note}"
                elif existing_notes:
                    updates["notes"] = existing_notes
            update_application(app_id, updates)
    summary["maybe_scored"] = len(maybe_scored_apps)
    return summary


def add_stats(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def non_benign_discovery_warnings(warnings: str) -> str:
    lines: list[str] = []
    for line in str(warnings or "").splitlines():
        if "with XML parser, using fallback" in line:
            continue
        lines.append(line)
    return "\n".join(line for line in lines if line.strip()).strip()


def discovery_source_status(candidates_count: int, stats: dict[str, int], warnings: str) -> str:
    effective_warnings = non_benign_discovery_warnings(warnings)
    if effective_warnings and candidates_count == 0:
        return "failed"
    if effective_warnings:
        return "partial_success"
    if stats.get("added", 0) > 0:
        return "new_jobs_added"
    if candidates_count == 0:
        return "searched_no_jobs_returned"
    return "searched_no_new_matches"


def source_failure_category(source: dict[str, Any], text: str) -> str:
    platform = source_platform(source)
    lower = text.lower()
    if not lower.strip():
        return ""
    if "source subprocess produced invalid json" in lower or "unterminated string" in lower:
        return "invalid_json_payload"
    if "exceeded" in lower and "s" in lower:
        return "timeout"
    if "certificate verify failed" in lower or "ssl:" in lower:
        return "ssl_certificate"
    if platform == "meta_jobs" or "did not expose static job results" in lower:
        return "meta_static_unavailable"
    if platform == "greenhouse" and ("http error 404" in lower or "not found" in lower):
        return "greenhouse_404"
    if platform == "workday" and ("http error 410" in lower or " gone" in lower):
        return "workday_410"
    if "http error 404" in lower or "not found" in lower:
        return "http_404"
    if "http error 410" in lower or " gone" in lower:
        return "http_410"
    if "http error 5" in lower or "internal server error" in lower:
        return "http_5xx"
    if "urlopen error" in lower or "could not fetch" in lower or "failed to fetch" in lower:
        return "fetch_error"
    return "unknown_failure"


def source_health_from_category(category: str, status: str, result_status: str, stats: dict[str, int], warnings: str) -> str:
    if stats.get("added", 0) > 0:
        return "new_jobs_found"
    if status == "retry_success":
        return "success_no_new" if result_status != "partial_success" else "partial_success"
    if status == "partial_success" or result_status == "partial_success":
        return "partial_success"
    if category in {"greenhouse_404", "workday_410", "http_404", "http_410", "meta_static_unavailable"}:
        return "config_broken"
    if category in {"invalid_json_payload"}:
        return "adapter_broken"
    if category in {"ssl_certificate", "timeout", "http_5xx", "fetch_error", "unknown_failure"}:
        return "fetch_failed"
    if status in {"failed", "failed_after_retries"} or result_status == "failed" or warnings.strip():
        return "fetch_failed"
    return "success_no_new"


def annotate_source_health(source_report: dict[str, Any], source: dict[str, Any]) -> None:
    stats = source_report.get("stats", {})
    effective_warnings = non_benign_discovery_warnings(str(source_report.get("warnings", "")))
    failure_text = "\n".join(
        str(item)
        for item in [
            source_report.get("error", ""),
            effective_warnings,
            "\n".join(str(attempt.get("error", "")) for attempt in source_report.get("attempts", []) if attempt.get("error")),
        ]
        if str(item).strip()
    )
    category = source_failure_category(source, failure_text)
    source_report["failure_category"] = category
    source_report["health"] = source_health_from_category(
        category,
        str(source_report.get("status", "")),
        str(source_report.get("result_status", "")),
        stats,
        effective_warnings,
    )


def ensure_source_health_annotations(sources: list[dict[str, Any]]) -> None:
    for source in sources:
        if source.get("health") and "failure_category" in source:
            continue
        annotate_source_health(source, source)


def discovery_run_id(started_at: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "-", started_at.replace("+00:00", "Z")).strip("-")


def write_discovery_run_report(report: dict[str, Any]) -> Path:
    run_id = str(report["run_id"])
    json_path = DISCOVERY_RUNS_DIR / f"{run_id}.json"
    md_path = DISCOVERY_RUNS_DIR / f"{run_id}.md"
    write_json(json_path, report)

    totals = report.get("totals", {})
    lines = [
        f"# Discovery Run {run_id}",
        "",
        f"- Started: {report.get('started_at', '')}",
        f"- Finished: {report.get('finished_at', '')}",
        f"- Track: {report.get('track') or 'default'}",
        f"- Cutoff: {report.get('cutoff', '')}",
        f"- Sources attempted: {totals.get('sources_attempted', 0)} / {totals.get('sources_planned', 0)}",
        f"- Failed sources: {totals.get('failed_sources', 0)}",
        f"- Retried sources: {totals.get('retried_sources', 0)}",
        f"- Retry recovered sources: {totals.get('retry_recovered_sources', 0)}",
        f"- Failed after retries: {totals.get('failed_after_retries', 0)}",
        f"- Discovered: {totals.get('discovered', 0)}",
        f"- Added: {totals.get('added', 0)}",
        f"- Existing: {totals.get('existing', 0)}",
        "",
        "| Status | Result | Health | Failure | Attempts | Company | Platform | Candidates | Added | Existing | Maybe | Old | Unknown date | Title | Location | Error / warnings |",
        "|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for source in report.get("sources", []):
        stats = source.get("stats", {})
        warning = source.get("error") or source.get("warnings") or ""
        warning = " ".join(str(warning).split())[:180]
        lines.append(
            "| {status} | {result} | {health} | {failure} | {attempts} | {company} | {platform} | {candidates} | {added} | {existing} | {maybe} | {old} | {unknown} | {title} | {location} | {warning} |".format(
                status=source.get("status", ""),
                result=source.get("result_status", ""),
                health=source.get("health", ""),
                failure=source.get("failure_category", ""),
                attempts=len(source.get("attempts", [])) or 1,
                company=str(source.get("company", "")).replace("|", "\\|"),
                platform=source.get("platform", ""),
                candidates=source.get("candidates_returned", 0),
                added=stats.get("added", 0),
                existing=stats.get("existing", 0),
                maybe=stats.get("maybe_backlog", 0),
                old=stats.get("skipped_old", 0),
                unknown=stats.get("skipped_unknown_date", 0),
                title=stats.get("skipped_title", 0),
                location=stats.get("skipped_location", 0),
                warning=warning.replace("|", "\\|"),
            )
        )
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path


def latest_discovery_run_path() -> Path:
    if not DISCOVERY_RUNS_DIR.exists():
        raise SystemExit(f"No discovery run reports found at {DISCOVERY_RUNS_DIR}")
    paths = sorted(DISCOVERY_RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        raise SystemExit(f"No discovery run reports found at {DISCOVERY_RUNS_DIR}")
    return paths[0]


def discovery_run_path(run_id: str) -> Path:
    value = str(run_id or "").strip()
    if not value:
        return latest_discovery_run_path()
    path = Path(value).expanduser()
    if path.exists():
        return path
    candidate = DISCOVERY_RUNS_DIR / f"{value.removesuffix('.json')}.json"
    if candidate.exists():
        return candidate
    raise SystemExit(f"Discovery run report not found: {value}")


def top_sources_by_filter(sources: list[dict[str, Any]], key: str, limit: int = 8) -> list[dict[str, Any]]:
    return sorted(
        [source for source in sources if source.get("stats", {}).get(key, 0) > 0],
        key=lambda source: source.get("stats", {}).get(key, 0),
        reverse=True,
    )[:limit]


def print_source_rows(title: str, rows: list[dict[str, Any]], limit: int = 12) -> None:
    print(f"\n{title}")
    if not rows:
        print("- None")
        return
    for source in rows[:limit]:
        stats = source.get("stats", {})
        details = (
            f"candidates={source.get('candidates_returned', 0)}, "
            f"added={stats.get('added', 0)}, existing={stats.get('existing', 0)}, "
            f"maybe={stats.get('maybe_backlog', 0)}, "
            f"old={stats.get('skipped_old', 0)}, title={stats.get('skipped_title', 0)}, "
            f"location={stats.get('skipped_location', 0)}"
        )
        reason = source.get("error") or source.get("warnings") or ""
        suffix = f" | {reason.strip()[:160]}" if reason else ""
        health = source.get("health", "")
        failure = source.get("failure_category", "")
        health_text = f", health={health}" if health else ""
        failure_text = f", failure={failure}" if failure else ""
        print(f"- {source.get('company', 'Unknown')} ({source.get('platform', '')}): {source.get('status', '')}{health_text}{failure_text}; {details}{suffix}")


def command_discovery_summary(args: argparse.Namespace) -> None:
    require_person_files()
    path = latest_discovery_run_path() if args.latest or not args.run_id else discovery_run_path(args.run_id)
    report = load_json(path)
    sources = report.get("sources", [])
    ensure_source_health_annotations(sources)
    totals = report.get("totals", {})
    failed = [source for source in sources if source.get("status") in {"failed", "failed_after_retries"}]
    partial = [source for source in sources if source.get("status") == "partial_success"]
    added = [source for source in sources if source.get("stats", {}).get("added", 0) > 0]
    no_jobs = [source for source in sources if source.get("status") == "searched_no_jobs_returned"]
    no_new = [source for source in sources if source.get("status") == "searched_no_new_matches"]
    health_counts = collections.Counter(source.get("health") or "unknown" for source in sources)
    attempted = int(totals.get("sources_attempted", 0))
    planned = int(totals.get("sources_planned", 0))

    print(f"Discovery Summary: {report.get('run_id', path.stem)}")
    print(f"- Report: {path}")
    print(f"- Started: {report.get('started_at', '')}")
    print(f"- Finished: {report.get('finished_at', '')}")
    print(f"- Track: {report.get('track') or 'default'}")
    print(f"- Cutoff: {report.get('cutoff', '')}")
    print(f"- Sources attempted: {attempted} / {planned}")
    print(f"- Failed sources: {len(failed)}")
    print(f"- Partial success sources: {len(partial)}")
    print(f"- Candidates returned: {totals.get('discovered', 0)}")
    print(f"- Added: {totals.get('added', 0)}")
    print(f"- Existing: {totals.get('existing', 0)}")
    print(
        "- Filtered: "
        f"old={totals.get('skipped_old', 0)}, "
        f"unknown_date={totals.get('skipped_unknown_date', 0)}, "
        f"title={totals.get('skipped_title', 0)}, "
        f"location={totals.get('skipped_location', 0)}, "
        f"maybe={totals.get('maybe_backlog', 0)}"
    )
    if health_counts:
        print("- Health: " + ", ".join(f"{key}={value}" for key, value in sorted(health_counts.items())))

    if planned and attempted < planned:
        print(f"\nCoverage warning: {planned - attempted} configured sources were not attempted.")
    elif planned:
        print("\nCoverage: every selected source was attempted.")

    print_source_rows("Failures", failed + partial)
    print_source_rows("Sources With New Jobs Added", sorted(added, key=lambda s: s.get("stats", {}).get("added", 0), reverse=True))
    print_source_rows("No Jobs Returned", no_jobs)

    print("\nTop Filter Reasons")
    old_rows = top_sources_by_filter(sources, "skipped_old", args.limit)
    title_rows = top_sources_by_filter(sources, "skipped_title", args.limit)
    location_rows = top_sources_by_filter(sources, "skipped_location", args.limit)
    unknown_rows = top_sources_by_filter(sources, "skipped_unknown_date", args.limit)
    print_source_rows("Old Postings", old_rows, args.limit)
    print_source_rows("Title Mismatch", title_rows, args.limit)
    print_source_rows("Location Mismatch", location_rows, args.limit)
    print_source_rows("Unknown Posted Date", unknown_rows, args.limit)

    print_source_rows("Sample Searched With No New Matches", no_new, args.limit)

    if failed:
        print("\nRecommendation: fix failed sources first; these are crawler or platform issues.")
    elif totals.get("added", 0):
        print("\nRecommendation: review newly added jobs and prepare applications for the highest scores.")
    elif totals.get("discovered", 0):
        print("\nRecommendation: search ran successfully; most candidates were already seen or filtered out.")
    else:
        print("\nRecommendation: if this was a broad run, expand or repair sources because no candidates were returned.")


def source_health_needs_attention(source: dict[str, Any]) -> bool:
    health = str(source.get("health") or "")
    if health in {"config_broken", "adapter_broken", "fetch_failed"}:
        return True
    if source.get("status") in {"failed", "failed_after_retries"}:
        return True
    return bool(source.get("failure_category"))


def source_issue_identity(source: dict[str, Any]) -> tuple[str, str]:
    return (
        str(source.get("company") or "").strip().lower(),
        str(source.get("platform") or "").strip().lower(),
    )


def source_health_is_success(source: dict[str, Any]) -> bool:
    if source.get("status") in {"failed", "failed_after_retries", "partial_success"}:
        return False
    if str(source.get("health") or "") in {"success_no_new", "new_jobs_found"}:
        return True
    return not source_health_needs_attention(source)


def source_issue_resolved_later(issue_source: dict[str, Any], issue_report: dict[str, Any], reports: list[dict[str, Any]]) -> bool:
    issue_key = source_issue_identity(issue_source)
    if not issue_key[0]:
        return False
    issue_started_at = str(issue_report.get("started_at") or issue_report.get("run_id") or "")
    for report in reports:
        report_started_at = str(report.get("started_at") or report.get("run_id") or "")
        if report_started_at <= issue_started_at:
            continue
        for source in report.get("sources", []):
            if source_issue_identity(source) == issue_key and source_health_is_success(source):
                return True
    return False


def command_source_health(args: argparse.Namespace) -> None:
    require_person_files()
    path = latest_discovery_run_path() if args.latest or not args.run_id else discovery_run_path(args.run_id)
    report = load_json(path)
    ensure_source_health_annotations(report.get("sources", []))
    sources = [source for source in report.get("sources", []) if source_health_needs_attention(source)]
    sources.sort(
        key=lambda source: (
            str(source.get("health") or ""),
            str(source.get("failure_category") or ""),
            str(source.get("company") or "").lower(),
        )
    )
    if args.limit and args.limit > 0:
        sources = sources[: args.limit]

    print(f"Source Health: {report.get('run_id', path.stem)}")
    print(f"- Report: {path}")
    if not sources:
        print("- No source health issues found.")
        return
    for source in sources:
        reason = source.get("error") or source.get("warnings") or ""
        reason = " ".join(str(reason).split())[:180]
        print(
            "- "
            f"{source.get('company', 'Unknown')} ({source.get('platform', '')}) "
            f"health={source.get('health', '') or 'unknown'} "
            f"failure={source.get('failure_category', '') or 'unknown'} "
            f"status={source.get('status', '')} "
            f"attempts={len(source.get('attempts', [])) or 1}"
            + (f" | {reason}" if reason else "")
        )


def fetch_ashby_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "ashbyhq.com" not in parsed.netloc.lower() or len(parts) < 2:
        return None
    board, job_id = parts[0], parts[1]
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"
    data = fetch_json(api_url)
    for job in data.get("jobs", []):
        if job.get("id") != job_id and job_id not in str(job.get("jobUrl", "")):
            continue
        blocks = [
            job.get("title", ""),
            job.get("location", ""),
            job.get("employmentType", ""),
            job.get("workplaceType", ""),
            job.get("department", ""),
            job.get("team", ""),
            job.get("descriptionPlain", "") or html_to_text(job.get("descriptionHtml", "")),
        ]
        compensation = job.get("compensation")
        if compensation:
            blocks.append(json.dumps(compensation, ensure_ascii=False))
        return "\n\n".join(str(block) for block in blocks if block)
    return None


def fetch_greenhouse_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "greenhouse.io" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[-2] != "jobs":
        return None
    board, job_id = parts[0], parts[-1]
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs/{urllib.parse.quote(job_id)}"
    data = fetch_json(api_url)
    blocks = [
        data.get("title", ""),
        data.get("company_name", ""),
        data.get("location", {}).get("name", "") if isinstance(data.get("location"), dict) else data.get("location", ""),
        html_to_text(data.get("content", "")),
    ]
    return "\n\n".join(str(block) for block in blocks if block)


def fetch_microsoft_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "microsoft.com" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    position_id = parts[-1] if parts else ""
    if not position_id.isdigit():
        return None
    opener, headers = microsoft_pcsx_session()
    params = {
        "position_id": position_id,
        "domain": "microsoft.com",
        "hl": "en",
    }
    api_url = f"https://apply.careers.microsoft.com/api/pcsx/position_details?{urllib.parse.urlencode(params)}"
    data = fetch_json_with_opener(opener, api_url, headers)
    job = data.get("data", {}) if isinstance(data, dict) else {}
    location_values = job.get("standardizedLocations") or job.get("locations") or []
    if isinstance(location_values, str):
        location_text = location_values
    else:
        location_text = "; ".join(str(item) for item in location_values if item)
    blocks = [
        job.get("name", ""),
        job.get("displayJobId", ""),
        location_text,
        job.get("department", ""),
        html_to_text(job.get("jobDescription", "")),
    ]
    return "\n\n".join(str(block) for block in blocks if block)


def fetch_amazon_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "amazon.jobs" not in parsed.netloc.lower():
        return None
    raw = fetch_url(url)
    return html_to_text(raw)


def fetch_google_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "google.com" not in parsed.netloc.lower() and "careers.google.com" not in parsed.netloc.lower():
        return None
    raw = fetch_url(url)
    candidates = parse_google_jobs_from_html(raw, url)
    path_parts = [part for part in parsed.path.split("/") if part]
    job_id = ""
    if path_parts:
        id_match = re.match(r"(\d{10,})", path_parts[-1])
        if id_match:
            job_id = id_match.group(1)
    for candidate in candidates:
        if not job_id or candidate.get("external_job_id") == job_id:
            text = candidate.get("_jd_text")
            if text:
                return str(text)
    return html_to_text(raw)


def fetch_meta_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "metacareers.com" not in parsed.netloc.lower():
        return None
    return html_to_text(fetch_url(url))


def fetch_workday_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "myworkdayjobs.com" not in parsed.netloc.lower() and "myworkdaysite.com" not in parsed.netloc.lower():
        return None
    candidate = workday_candidate_from_url(url)
    if not candidate:
        return html_to_text(fetch_url(url))
    parts = workday_source_parts({"url": url})
    if not parts:
        return html_to_text(fetch_url(url))
    host, tenant, site = parts
    path_parts = [part for part in parsed.path.split("/") if part]
    site_index = next((index for index, part in enumerate(path_parts) if part == site), -1)
    if site_index < 0:
        return html_to_text(fetch_url(url))
    external_path = "/" + "/".join(path_parts[site_index + 1 :])
    data = fetch_json(workday_api_url(host, tenant, site, external_path))
    info = data.get("jobPostingInfo", {}) if isinstance(data, dict) else {}
    blocks = [
        info.get("title", ""),
        info.get("jobReqId", ""),
        info.get("location", ""),
        html_to_text(info.get("jobDescription", "")),
    ]
    return "\n\n".join(str(block) for block in blocks if block)


def fetch_m_cloud_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "m-cloud.io" not in parsed.netloc.lower() and "careers.remitly.com" not in parsed.netloc.lower():
        return None
    return html_to_text(fetch_url(url))


def fetch_hirebridge_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "hirebridge.com" not in parsed.netloc.lower():
        return None
    return html_to_text(fetch_url(url))


def fetch_successfactors_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "successfactors.com" not in parsed.netloc.lower() and "careers.qualitestgroup.com" not in parsed.netloc.lower():
        return None
    return html_to_text(fetch_url(url))


def fetch_eightfold_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "eightfold.ai" not in parsed.netloc.lower() and "jobs.nvidia.com" not in parsed.netloc.lower():
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    try:
        job_id = path_parts[path_parts.index("job") + 1]
    except (ValueError, IndexError):
        return html_to_text(fetch_url(url))
    host = parsed.netloc.lower()
    if "nvidia" in host:
        base_url = "https://nvidia.eightfold.ai"
        domain = "nvidia.com"
    elif "starbucks" in host:
        base_url = "https://starbucks.eightfold.ai"
        domain = "starbucks.com"
    else:
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        domain = urllib.parse.parse_qs(parsed.query).get("domain", [parsed.netloc.replace(".eightfold.ai", ".com")])[0]
    params = {"position_id": job_id, "domain": domain, "hl": "en"}
    api_url = f"{base_url}/api/pcsx/position_details?{urllib.parse.urlencode(params)}"
    data = fetch_json(api_url)
    job = data.get("data", {}) if isinstance(data, dict) else {}
    location_values = job.get("standardizedLocations") or job.get("locations") or []
    location_text = location_values if isinstance(location_values, str) else "; ".join(str(item) for item in location_values if item)
    blocks = [
        job.get("name", ""),
        job.get("displayJobId", ""),
        location_text,
        job.get("department", ""),
        html_to_text(job.get("jobDescription", "")),
    ]
    return "\n\n".join(str(block) for block in blocks if block)


def fetch_apple_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "jobs.apple.com" not in parsed.netloc.lower():
        return None
    return html_to_text(fetch_url(url))


def fetch_providence_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "providence.jobs" not in parsed.netloc.lower() and "jobsyn.org" not in parsed.netloc.lower():
        return None
    return html_to_text(fetch_url(url))


def fetch_jobsyn_job_text(app: dict[str, Any]) -> str | None:
    if str(app.get("platform") or "") != "jobsyn":
        return None
    sources = load_json(SOURCES_PATH).get("sources", [])
    company = str(app.get("company") or "").strip().lower()
    source = next(
        (
            item
            for item in sources
            if source_platform(item) == "jobsyn"
            and str(item.get("company") or "").strip().lower() == company
        ),
        None,
    )
    if not source:
        return None
    probe = dict(source)
    probe["keywords"] = [str(app.get("role") or app.get("job_number") or app.get("external_job_id") or "").strip()]
    probe["max_pages"] = 1
    for candidate in discover_jobsyn_jobs(probe):
        if str(candidate.get("job_number") or "") == str(app.get("job_number") or ""):
            return str(candidate.get("_jd_text") or "").strip() or None
        if normalize_job_url(str(candidate.get("url") or "")) == normalize_job_url(str(app.get("url") or "")):
            return str(candidate.get("_jd_text") or "").strip() or None
    return None


def parse_ripplehire_url(url: str) -> tuple[str, str, str] | None:
    parsed = urllib.parse.urlparse(url)
    if "ripplehire.com" not in parsed.netloc.lower() or "/candidate" not in parsed.path.lower():
        return None
    query = urllib.parse.parse_qs(parsed.query)
    token = (query.get("token") or [""])[0].strip()
    source_name = (query.get("source") or ["CAREERSITE"])[0].strip() or "CAREERSITE"
    match = re.search(r"(?:detail|apply)/job/([^/?#]+)", urllib.parse.unquote(parsed.fragment or ""))
    job_seq = match.group(1).strip() if match else ""
    if not token or not job_seq:
        return None
    base_url = urllib.parse.urlunparse(parsed._replace(query="", fragment="", params=""))
    return ripplehire_base_url({"url": base_url}), token, source_name, job_seq


def fetch_ripplehire_job_text(url: str) -> str | None:
    parsed = parse_ripplehire_url(url)
    if not parsed:
        return None
    base_url, token, source_name, job_seq = parsed
    job = ripplehire_detail({"url": base_url, "token": token, "ripplehire_source": source_name}, job_seq)
    return ripplehire_job_text(job or {}) if job else None


def fetch_jubilant_careers_job_text(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != "jubilantcareer.jubl.com":
        return None
    match = re.search(r"/jobprofile/([^/?#]+)", parsed.path, flags=re.I)
    if not match:
        return None
    detail = fetch_json(
        "https://jubilantcareer.jubl.com/JubilantCareersPortal/rest/Portal/"
        f"getJobDetails/{urllib.parse.quote(match.group(1))}"
    )
    if not isinstance(detail, dict):
        return None
    return html_to_text(str(detail.get("jobdescr") or "")) or None


def fetch_direct_platform_job_text(url: str) -> str | None:
    platform = detect_platform(url)
    if platform not in {"smartrecruiters", "icims", "oracle_cx", "clearcompany", "paycor", "paylocity", "jobvite", "workable", "bamboohr", "yc_jobs", "yc_job_board", "jibe", "talentbrew", "careerpuck", "pinpoint", "brassring"}:
        return None
    return html_to_text(fetch_url(url))


def infer_role_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
    if parts:
        candidate = parts[-1]
        candidate = re.sub(r"[-_]+", " ", candidate)
        candidate = re.sub(r"\b[a-f0-9]{8,}\b", "", candidate, flags=re.I)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate:
            return candidate.title()
    return "Unknown Role"


def find_links_for_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = source.get("company", "Unknown Company")
    platform = source.get("platform") or detect_platform(source["url"])
    base_url = source["url"].rstrip("/")
    base_parsed = urllib.parse.urlparse(base_url)
    try:
        raw = fetch_url(base_url)
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"Could not fetch {company} source {base_url}: {error}", file=sys.stderr)
        if truthy_source_flag(
            source.get("fail_on_fetch_error"),
            default=False,
        ):
            raise RuntimeError(
                f"Could not fetch {company} source {base_url}: {error}"
            ) from error
        return []

    links: list[dict[str, str]] = []
    custom_job_link_regex = str(source.get("job_link_regex") or "").strip()
    if platform == "greenhouse":
        pattern = r'href=["\']([^"\']*(?:boards\.greenhouse\.io|/jobs/)[^"\']+)["\'][^>]*>(.*?)</a>'
    elif platform == "lever":
        pattern = r'href=["\']([^"\']*(?:jobs\.lever\.co|/[^"\']+/[^"\']+)[^"\']*)["\'][^>]*>(.*?)</a>'
    elif platform == "ashby":
        pattern = r'href=["\']([^"\']*(?:jobs\.ashbyhq\.com|/[^"\']+/[^"\']+)[^"\']*)["\'][^>]*>(.*?)</a>'
    elif platform == "custom" and custom_job_link_regex:
        pattern = r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    else:
        pattern = r'href=["\']([^"\']*(?:job|position|opening|requisition|posting)[^"\']*)["\'][^>]*>(.*?)</a>'

    for href, label in re.findall(pattern, raw, flags=re.I | re.S):
        url = urllib.parse.urljoin(base_url + "/", html.unescape(href))
        if url.rstrip("/") == base_url:
            continue
        parsed_link = urllib.parse.urlparse(url)
        if parsed_link.scheme not in {"http", "https"}:
            continue
        if re.search(r"\.(?:css|js|map|png|jpe?g|gif|svg|ico|pdf|zip|atom|rss|xml)(?:$|[?#])", parsed_link.path, flags=re.I):
            continue
        if platform == "custom":
            if (
                parsed_link.netloc.lower() == base_parsed.netloc.lower()
                and parsed_link.path.rstrip("/") == base_parsed.path.rstrip("/")
            ):
                continue
            detail_hint = re.search(
                custom_job_link_regex
                or r"(?:/job/|/jobs/|/position/|/positions/|/opening/|/openings/|requisition|posting)",
                parsed_link.path,
                flags=re.I,
            )
            external_board = re.search(
                r"(?:greenhouse\.io|lever\.co|ashbyhq\.com|jobs\.gem\.com|myworkdayjobs\.com)",
                parsed_link.netloc,
                flags=re.I,
            )
            if not detail_hint and not external_board:
                continue
        text = html_to_text(label)
        configured_link_role_pattern = str(
            source.get("link_role_regex") or ""
        ).strip()
        configured_link_role_match = (
            re.search(
                configured_link_role_pattern,
                label,
                flags=re.I | re.S,
            )
            if configured_link_role_pattern
            else None
        )
        configured_link_role = (
            html_to_text(configured_link_role_match.group(1))
            if configured_link_role_match
            else ""
        )
        role = (
            configured_link_role
            or (text if 4 <= len(text) <= 120 else infer_role_from_url(url))
        )
        if not role or role.lower() in {
            "apply",
            "learn more",
            "read more",
            "view job",
            "view details",
            "applicant portal",
        }:
            role = infer_role_from_url(url)
        links.append(
            {
                "company": company,
                "role": role,
                "url": url,
                "platform": detect_platform(url) if platform == "custom" else platform,
                "location": "",
                "notes": "",
            }
        )

    unique: dict[str, dict[str, Any]] = {}
    for link in links:
        existing = unique.get(link["url"])
        generic_roles = {"apply", "learn more", "read more", "view job", "view details", "applicant portal"}
        existing_role = str((existing or {}).get("role") or "").strip().lower()
        new_role = str(link.get("role") or "").strip().lower()
        if existing and existing_role not in generic_roles and new_role in generic_roles:
            continue
        if existing and existing_role and not re.fullmatch(r"(?:Posting )?\d+", existing_role, flags=re.I) and re.fullmatch(
            r"(?:Posting )?\d+", new_role, flags=re.I
        ):
            continue
        unique[link["url"]] = link
    if (
        platform == "custom"
        and truthy_source_flag(source.get("empty_is_failure"), default=False)
        and not unique
    ):
        raise RuntimeError(
            f"No configured job links were found for {company} at {base_url}."
        )
    if platform != "custom" or source.get("parse_job_details", True) is False:
        return list(unique.values())

    all_links = list(unique.values())
    if truthy_source_flag(source.get("prioritize_technical_titles"), default=True):
        all_links.sort(
            key=lambda item: (
                0 if unclassified_technical_title_relevant(item) else 1,
                str(item.get("role") or "").lower(),
            )
        )
    max_detail_pages = int(source.get("max_detail_pages", 40))
    detail_links = all_links[:max_detail_pages]
    enriched: list[dict[str, Any]] = []
    for link in detail_links:
        try:
            detail_raw = fetch_url(link["url"], timeout=12)
        except Exception:  # noqa: BLE001
            enriched.append(link)
            continue
        posted_patterns = [
                r'"datePosted"\s*:\s*"([^"]+)"',
                r'"datePublished"\s*:\s*"([^"]+)"',
                r'"postedDate"\s*:\s*"([^"]+)"',
                r'"published_at"\s*:\s*"([^"]+)"',
                r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
                r'Date\s*Posted\s*</[^>]+>\s*<[^>]+>\s*([^<]+)',
        ]
        configured_posted_pattern = str(
            source.get("posted_at_regex") or ""
        ).strip()
        if configured_posted_pattern:
            posted_patterns.insert(0, configured_posted_pattern)
        posted_at = extract_first_datetime(detail_raw, posted_patterns)
        posted_text_pattern = str(
            source.get("posted_at_text_regex") or ""
        ).strip()
        if not posted_at and posted_text_pattern:
            posted_match = re.search(
                posted_text_pattern,
                html_to_text(detail_raw),
                flags=re.I,
            )
            if posted_match:
                posted_at = normalize_datetime(posted_match.group(1))
        updated_at = extract_first_datetime(
            detail_raw,
            [
                r'"dateModified"\s*:\s*"([^"]+)"',
                r'"updated_at"\s*:\s*"([^"]+)"',
                r'<meta[^>]+property=["\']article:modified_time["\'][^>]+content=["\']([^"\']+)["\']',
            ],
        )
        title = extract_html_title(detail_raw, company, link["url"])
        configured_role_pattern = str(
            source.get("detail_role_regex") or ""
        ).strip()
        if configured_role_pattern:
            configured_role_match = re.search(
                configured_role_pattern,
                detail_raw,
                flags=re.I | re.S,
            )
            if configured_role_match:
                title = html_to_text(configured_role_match.group(1))
        if truthy_source_flag(source.get("preserve_link_title"), default=False):
            title = ""
        visible_location_match = re.search(
            r'<(?:th|dt)[^>]*>\s*Location\s*</(?:th|dt)>\s*<[^>]+>\s*(.*?)</(?:td|dd|span|div)>',
            detail_raw,
            flags=re.I | re.S,
        )
        visible_location = html_to_text(visible_location_match.group(1)) if visible_location_match else ""
        location_override = str(source.get("location_override") or "").strip()
        link.update(
            {
                "role": title or link.get("role", ""),
                "location": (
                    location_override
                    or visible_location
                    or extract_location(detail_raw)
                    or link.get("location", "")
                ),
                "posted_at": posted_at,
                "updated_at": updated_at,
                "source": source.get("url", ""),
                "freshness_source": "official_posted_at" if posted_at else "unknown",
            }
        )
        if truthy_source_flag(
            source.get("include_detail_text"),
            default=False,
        ):
            link["_jd_text"] = html_to_text(detail_raw)
        enriched.append(link)
    enriched_urls = {str(link.get("url") or "") for link in enriched}
    return enriched + [link for link in all_links if str(link.get("url") or "") not in enriched_urls]


def discover_static_job_board_jobs(source: dict[str, Any], platform: str, note: str) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Unknown Company")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    try:
        raw = fetch_url(source_url, timeout=int(source.get("timeout", 25)))
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch {platform} source for {company}: {error}", file=sys.stderr)
        return []

    candidates: dict[str, dict[str, Any]] = {}
    for candidate in parse_json_ld_jobs(raw, source_url, company):
        if source_platform({"url": candidate.get("url", ""), "platform": candidate.get("platform", "")}) == "custom":
            candidate["platform"] = platform
        candidate["source"] = source_url
        candidate["source_query"] = str(source.get("source_query") or source.get("role") or "")
        candidate["notes"] = "; ".join(item for item in [candidate.get("notes", ""), note] if item)
        candidates[candidate["url"]] = candidate

    link_source = dict(source)
    link_source["platform"] = "custom"
    link_source.setdefault(
        "parse_job_details",
        not truthy_source_flag(
            source.get("prefer_structured_listing"),
            default=False,
        ),
    )
    for candidate in find_links_for_source(link_source):
        detected = detect_platform(candidate.get("url", ""))
        candidate["platform"] = detected if detected != "custom" else platform
        candidate["source"] = source_url
        candidate["source_query"] = str(source.get("source_query") or source.get("role") or "")
        candidate["notes"] = "; ".join(item for item in [candidate.get("notes", ""), note] if item)
        existing = candidates.get(candidate["url"])
        if existing:
            for key in [
                "location",
                "posted_at",
                "updated_at",
                "job_number",
                "external_job_id",
                "_jd_text",
            ]:
                if not existing.get(key) and candidate.get(key):
                    existing[key] = candidate[key]
            existing["notes"] = "; ".join(
                dict.fromkeys(
                    item
                    for item in [existing.get("notes", ""), candidate.get("notes", "")]
                    if item
                )
            )
            continue
        candidates[candidate["url"]] = candidate
    return list(candidates.values())


def discover_startup_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    return discover_static_job_board_jobs(source, "startup_jobs", "Startup.jobs static job board adapter.")


def discover_builtin_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    return discover_static_job_board_jobs(source, "builtin_jobs", "Built In static job board adapter.")


def discover_getro_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Portfolio Jobs")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    raw = fetch_url(source_url, timeout=int(source.get("timeout", 25)))

    collection_id = str(source.get("collection_id") or "").strip()
    if not collection_id:
        next_data_match = re.search(
            r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            raw,
            flags=re.I | re.S,
        )
        if next_data_match:
            try:
                next_data = json.loads(html.unescape(next_data_match.group(1)))
            except (TypeError, ValueError, json.JSONDecodeError):
                next_data = {}
            collection_id = str(
                (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("network", {})
                    .get("id", "")
                )
            ).strip()

    api_candidates: dict[str, dict[str, Any]] = {}
    if collection_id:
        page_size = 20
        max_pages = max(1, int(source.get("max_pages", 100)))
        api_workers = max(1, int(source.get("api_workers", 10)))
        api_url = (
            f"https://api.getro.com/api/v2/collections/"
            f"{urllib.parse.quote(collection_id)}/search/jobs"
        )
        parsed_source_url = urllib.parse.urlparse(source_url)
        source_origin = urllib.parse.urlunparse(
            (
                parsed_source_url.scheme or "https",
                parsed_source_url.netloc,
                "",
                "",
                "",
                "",
            )
        )

        def fetch_page(page_index: int) -> tuple[int, dict[str, Any]]:
            data = fetch_json_post_with_headers(
                api_url,
                {
                    "hitsPerPage": page_size,
                    "page": page_index,
                    "filters": {"page": page_index},
                    "query": "",
                },
                {
                    "Accept": "application/json",
                    "Origin": source_origin,
                    "Referer": source_url,
                },
                timeout=int(source.get("api_timeout", 20)),
            )
            return page_index, data if isinstance(data, dict) else {}

        try:
            _, first_page = fetch_page(0)
        except Exception:
            first_page = {}
        first_results = first_page.get("results", {}) if isinstance(first_page, dict) else {}
        first_jobs = first_results.get("jobs", []) if isinstance(first_results, dict) else []
        total = int(first_results.get("count") or len(first_jobs)) if isinstance(first_results, dict) else 0
        page_count = min(max_pages, max(1, math.ceil(total / page_size))) if total else 1
        pages: dict[int, dict[str, Any]] = {0: first_page} if first_page else {}
        if page_count > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=api_workers) as executor:
                future_pages = {
                    executor.submit(fetch_page, page_index): page_index
                    for page_index in range(1, page_count)
                }
                for future in concurrent.futures.as_completed(future_pages):
                    try:
                        page_index, page_data = future.result()
                    except Exception:
                        continue
                    pages[page_index] = page_data

        source_query = str(source.get("source_query") or source.get("role") or "")
        for page_index in sorted(pages):
            results = pages[page_index].get("results", {})
            jobs = results.get("jobs", []) if isinstance(results, dict) else []
            for job in jobs if isinstance(jobs, list) else []:
                if not isinstance(job, dict):
                    continue
                organization = job.get("organization") or {}
                if not isinstance(organization, dict):
                    organization = {}
                role = str(job.get("title") or "").strip()
                url = normalize_job_url(str(job.get("url") or "").strip())
                if not role or not url:
                    continue
                locations = job.get("locations") or job.get("searchable_locations") or []
                if isinstance(locations, str):
                    locations = [locations]
                location = "; ".join(
                    merge_unique([], [str(item).strip() for item in locations if str(item).strip()])
                )
                company_slug = str(organization.get("slug") or "").strip()
                job_slug = str(job.get("slug") or "").strip()
                getro_detail_url = ""
                if company_slug and job_slug:
                    getro_detail_url = normalize_job_url(
                        urllib.parse.urljoin(
                            source_url,
                            f"/companies/{urllib.parse.quote(company_slug)}/jobs/"
                            f"{urllib.parse.quote(job_slug)}",
                        )
                    )
                posted_at = normalize_datetime(job.get("created_at"))
                detected_platform = detect_platform(url)
                api_candidates[url] = {
                    "company": str(organization.get("name") or company).strip(),
                    "role": role,
                    "url": url,
                    "platform": detected_platform if detected_platform != "custom" else "getro_jobs",
                    "location": location,
                    "posted_at": posted_at,
                    "updated_at": "",
                    "source": source_url,
                    "source_query": source_query,
                    "freshness_source": "getro_created_at" if posted_at else "unknown",
                    "external_job_id": str(job.get("id") or ""),
                    "notes": (
                        f"Getro public portfolio API; collection_id={collection_id}; "
                        f"source={job.get('source') or 'unknown'}."
                    ),
                    "_jd_text": "",
                    "_getro_detail_url": getro_detail_url,
                }

        location_pattern = str(source.get("location_include_regex") or "").strip()
        if location_pattern:
            try:
                api_candidates = {
                    url: candidate
                    for url, candidate in api_candidates.items()
                    if re.search(location_pattern, str(candidate.get("location") or ""), flags=re.I)
                }
            except re.error:
                pass

        max_details = max(
            0,
            min(len(api_candidates), int(source.get("max_detail_pages", 100))),
        )
        detail_workers = max(1, int(source.get("detail_workers", 8)))
        detail_candidates = sorted(
            (
                candidate
                for candidate in api_candidates.values()
                if candidate.get("_getro_detail_url")
                and unclassified_technical_title_relevant(candidate)
            ),
            key=lambda candidate: parse_datetime(candidate.get("posted_at"))
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            reverse=True,
        )[:max_details]

        def enrich_api_candidate(candidate: dict[str, Any]) -> None:
            try:
                detail_raw = fetch_url(
                    str(candidate["_getro_detail_url"]),
                    timeout=int(source.get("detail_timeout", 20)),
                )
            except Exception:
                return
            parsed = parse_json_ld_jobs(
                detail_raw,
                str(candidate["_getro_detail_url"]),
                str(candidate.get("company") or company),
            )
            if not parsed:
                return
            detail = parsed[0]
            for key in ["role", "location", "posted_at", "updated_at", "_jd_text"]:
                if detail.get(key):
                    candidate[key] = detail[key]
            if detail.get("posted_at"):
                candidate["freshness_source"] = "official_posted_at"
            candidate["notes"] = "; ".join(
                item
                for item in [
                    candidate.get("notes", ""),
                    "Enriched from Getro detail-page JobPosting JSON-LD.",
                ]
                if item
            )

        if detail_candidates:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=detail_workers
            ) as executor:
                list(executor.map(enrich_api_candidate, detail_candidates))
        for candidate in api_candidates.values():
            candidate.pop("_getro_detail_url", None)
        if api_candidates:
            return list(api_candidates.values())

    detail_urls: list[str] = []
    for href in re.findall(
        r'href=["\']([^"\']*/companies/[^"\']+/jobs/[^"\']+)["\']',
        raw,
        flags=re.I,
    ):
        url = normalize_job_url(
            urllib.parse.urljoin(source_url, html.unescape(href))
        )
        if url not in detail_urls:
            detail_urls.append(url)
    max_details = max(
        0,
        min(len(detail_urls), int(source.get("max_detail_pages", len(detail_urls)))),
    )
    detail_urls = detail_urls[:max_details]
    detail_workers = max(1, int(source.get("detail_workers", 8)))
    source_query = str(source.get("source_query") or source.get("role") or "")

    def fetch_candidate(url: str) -> dict[str, Any]:
        try:
            detail_raw = fetch_url(
                url,
                timeout=int(source.get("detail_timeout", 20)),
            )
        except Exception:
            detail_raw = ""
        parsed = parse_json_ld_jobs(detail_raw, url, company) if detail_raw else []
        if parsed:
            candidate = parsed[0]
            candidate["platform"] = "getro_jobs"
            candidate["source"] = source_url
            candidate["source_query"] = source_query
            candidate["freshness_source"] = (
                "official_posted_at" if candidate.get("posted_at") else "unknown"
            )
            candidate["notes"] = "; ".join(
                item
                for item in [
                    candidate.get("notes", ""),
                    "Getro portfolio job adapter enriched from detail-page JobPosting JSON-LD.",
                ]
                if item
            )
            return candidate

        parsed_url = urllib.parse.urlparse(url)
        path_parts = [part for part in parsed_url.path.split("/") if part]
        company_slug = ""
        role = infer_role_from_url(url)
        if "companies" in path_parts:
            company_index = path_parts.index("companies")
            if company_index + 1 < len(path_parts):
                company_slug = path_parts[company_index + 1]
        if path_parts:
            role_slug = re.sub(r"^\d+-", "", path_parts[-1])
            role = re.sub(r"[-_]+", " ", role_slug).title() or role
        return {
            "company": re.sub(r"[-_]+", " ", company_slug).title() or company,
            "role": role,
            "url": url,
            "platform": "getro_jobs",
            "location": "",
            "posted_at": "",
            "updated_at": "",
            "source": source_url,
            "source_query": source_query,
            "freshness_source": "unknown",
            "notes": "Getro portfolio job adapter; detail metadata unavailable.",
            "_jd_text": "",
        }

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=detail_workers
    ) as executor:
        return list(executor.map(fetch_candidate, detail_urls))


def discover_consider_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    company = str(source.get("company") or "Portfolio Jobs")
    source_url = str(source.get("url") or "").strip()
    if not source_url:
        return []
    raw = fetch_url(source_url, timeout=int(source.get("timeout", 25)))
    board_id = str(source.get("board_id") or "").strip()
    if not board_id:
        board_match = re.search(
            r'"clientConfig"\s*:\s*\{.*?"id"\s*:\s*"([^"]+)"',
            raw,
            flags=re.I | re.S,
        )
        if board_match:
            board_id = html.unescape(board_match.group(1)).strip()

    if board_id:
        parsed_source = urllib.parse.urlparse(source_url)
        origin = urllib.parse.urlunparse(
            (parsed_source.scheme or "https", parsed_source.netloc, "", "", "", "")
        )
        api_url = urllib.parse.urljoin(origin, "/api-boards/search-jobs")
        try:
            data = fetch_json_post_with_headers(
                api_url,
                {
                    "meta": {"size": max(15, int(source.get("max_results", 250)))},
                    "board": {"id": board_id, "isParent": True},
                    "query": {"promoteFeatured": True},
                },
                {
                    "Accept": "application/json",
                    "Referer": urllib.parse.urljoin(origin, "/jobs"),
                },
                timeout=int(source.get("api_timeout", 25)),
            )
        except Exception:
            data = {}
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        candidates: dict[str, dict[str, Any]] = {}
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict):
                continue
            role = str(job.get("title") or "").strip()
            url = normalize_job_url(
                str(job.get("url") or job.get("applyUrl") or "").strip()
            )
            if not role or not url:
                continue
            locations = job.get("locations") or []
            if isinstance(locations, str):
                locations = [locations]
            location = "; ".join(
                merge_unique([], [str(item).strip() for item in locations if str(item).strip()])
            )
            if truthy_source_flag(job.get("remote"), default=False) and "remote" not in location.lower():
                location = "; ".join(item for item in [location, "Remote"] if item)
            skills = []
            for skill in job.get("skills") or []:
                if isinstance(skill, dict):
                    label = str(skill.get("label") or skill.get("value") or "").strip()
                else:
                    label = str(skill).strip()
                if label:
                    skills.append(label)
            min_years = job.get("minYearsExp")
            metadata_text = " ".join(
                item
                for item in [
                    f"Minimum experience: {min_years} years." if min_years not in (None, "") else "",
                    f"Skills: {', '.join(merge_unique([], skills))}." if skills else "",
                ]
                if item
            )
            posted_at = normalize_datetime(job.get("timeStamp"))
            detected_platform = detect_platform(url)
            candidates[url] = {
                "company": str(job.get("companyName") or company).strip(),
                "role": role,
                "url": url,
                "platform": detected_platform if detected_platform != "custom" else "consider_jobs",
                "location": location,
                "posted_at": posted_at,
                "updated_at": "",
                "source": source_url,
                "source_query": str(source.get("source_query") or ""),
                "freshness_source": "consider_timestamp" if posted_at else "unknown",
                "external_job_id": str(job.get("jobId") or ""),
                "notes": (
                    f"Consider public portfolio API; board_id={board_id}; "
                    f"min_years={min_years if min_years not in (None, '') else 'unknown'}."
                ),
                "_jd_text": metadata_text,
            }

        location_pattern = str(source.get("location_include_regex") or "").strip()
        if location_pattern:
            try:
                candidates = {
                    url: candidate
                    for url, candidate in candidates.items()
                    if re.search(location_pattern, str(candidate.get("location") or ""), flags=re.I)
                }
            except re.error:
                pass

        detail_limit = max(
            0,
            min(len(candidates), int(source.get("max_detail_pages", 40))),
        )
        detail_workers = max(1, int(source.get("detail_workers", 8)))
        detail_candidates = sorted(
            (
                candidate
                for candidate in candidates.values()
                if unclassified_technical_title_relevant(candidate)
            ),
            key=lambda candidate: parse_datetime(candidate.get("posted_at"))
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            reverse=True,
        )[:detail_limit]

        def enrich(candidate: dict[str, Any]) -> None:
            try:
                detail_raw = fetch_url(
                    str(candidate["url"]),
                    timeout=int(source.get("detail_timeout", 20)),
                )
            except Exception:
                return
            parsed = parse_json_ld_jobs(
                detail_raw,
                str(candidate["url"]),
                str(candidate.get("company") or company),
            )
            if not parsed:
                return
            detail = parsed[0]
            for key in ["role", "location", "posted_at", "updated_at", "_jd_text"]:
                if detail.get(key):
                    candidate[key] = detail[key]
            if detail.get("posted_at"):
                candidate["freshness_source"] = "official_posted_at"
            candidate["notes"] = "; ".join(
                item
                for item in [
                    candidate.get("notes", ""),
                    "Enriched from the destination ATS JobPosting JSON-LD.",
                ]
                if item
            )

        if detail_candidates:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=detail_workers
            ) as executor:
                list(executor.map(enrich, detail_candidates))
        if candidates:
            return list(candidates.values())

    return discover_static_job_board_jobs(
        source,
        "consider_jobs",
        "Portfolio job board adapter for Consider-style pages.",
    )


def upsert_application(candidate: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    tracker = load_tracker()
    apps = tracker.setdefault("applications", [])
    normalized_url = normalize_job_url(candidate["url"])
    for app in apps:
        if normalize_job_url(app.get("url", "")) == normalized_url:
            changed = False
            for field in [
                "posted_at",
                "updated_at",
                "first_seen",
                "last_seen",
                "source",
                "source_query",
                "freshness_source",
                "job_number",
                "external_job_id",
                "review_bucket",
                "discovery_bucket",
                "location_bucket",
            ]:
                if candidate.get(field) and app.get(field) != candidate[field]:
                    app[field] = candidate[field]
                    changed = True
            if candidate.get("target_track") and not app.get("target_track"):
                app["target_track"] = candidate["target_track"]
                changed = True
            if candidate.get("resume_file") and (not app.get("resume_file") or app.get("target_track") == candidate.get("target_track")):
                app["resume_file"] = candidate["resume_file"]
                changed = True
            if candidate.get("matched_tracks"):
                merged = merge_unique(app.get("matched_tracks", []), candidate.get("matched_tracks", []))
                if merged != app.get("matched_tracks", []):
                    app["matched_tracks"] = merged
                    changed = True
            if changed:
                save_tracker(tracker)
            return app, False

    company = candidate.get("company") or "Unknown Company"
    role = candidate.get("role") or infer_role_from_url(candidate["url"])
    app = {
        "id": make_id(company, role, normalized_url),
        "company": company,
        "role": role,
        "url": normalized_url,
        "platform": candidate.get("platform") or detect_platform(normalized_url),
        "location": candidate.get("location", ""),
        "status": "found",
        "fit_score": "",
        "ats_score": "",
        "date_found": today(),
        "posted_at": candidate.get("posted_at", ""),
        "updated_at": candidate.get("updated_at", ""),
        "job_number": candidate.get("job_number", ""),
        "external_job_id": candidate.get("external_job_id", ""),
        "first_seen": candidate.get("first_seen", ""),
        "last_seen": candidate.get("last_seen", ""),
        "source": candidate.get("source", ""),
        "source_query": candidate.get("source_query", ""),
        "freshness_source": candidate.get("freshness_source", ""),
        "review_bucket": candidate.get("review_bucket", ""),
        "discovery_bucket": candidate.get("discovery_bucket", ""),
        "location_bucket": candidate.get("location_bucket", ""),
        "target_track": candidate.get("target_track", ""),
        "matched_tracks": candidate.get("matched_tracks", []),
        "track_evaluations": candidate.get("track_evaluations", {}),
        "resume_file": candidate.get("resume_file", ""),
        "date_applied": "",
        "resume_path": "",
        "cover_letter_path": "",
        "screenshot_path": "",
        "notes": candidate.get("notes", ""),
        "dealbreakers": [],
        "action_items": [],
    }
    apps.append(app)
    save_tracker(tracker)
    return app, True


def get_application(identifier: str) -> dict[str, Any]:
    tracker = load_tracker()
    for app in tracker.get("applications", []):
        if app.get("id") == identifier or app.get("url") == identifier:
            return app
    raise SystemExit(f"No application found for {identifier}")


def update_application(app_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    tracker = load_tracker()
    for app in tracker.get("applications", []):
        if app.get("id") == app_id:
            app.update(updates)
            save_tracker(tracker)
            return app
    raise SystemExit(f"No application found for {app_id}")


def numeric_score(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return [keyword for keyword in keywords if re.search(rf"\b{re.escape(keyword.lower())}\b", lower)]


def extract_years(text: str) -> list[int]:
    """Return minimum required years signals, treating ranges by their lower bound."""
    value = normalize_experience_text(text)
    years: list[int] = []
    range_spans: list[tuple[int, int]] = []
    range_pattern = re.compile(r"\b(\d+)\s*(?:-|–|—|\bto\b)\s*(\d+)\+?\s*(?:years|yrs)\b", flags=re.I)
    for match in range_pattern.finditer(value):
        lower_bound = int(match.group(1))
        if lower_bound <= 15 and experience_year_context(value, *match.span()):
            years.append(lower_bound)
        range_spans.append(match.span())
    if range_spans:
        chars = list(value)
        for start, end in range_spans:
            chars[start:end] = " " * (end - start)
        value = "".join(chars)
    for match in re.finditer(r"\b(\d+)\+?\s*(?:years|yrs)\b", value, flags=re.I):
        year_value = int(match.group(1))
        if year_value <= 15 and experience_year_context(value, *match.span()):
            years.append(year_value)
    return years


def extract_year_requirements(text: str) -> list[dict[str, Any]]:
    """Return structured experience requirements from JD/application text."""
    value = normalize_experience_text(text)
    requirements: list[dict[str, Any]] = []
    range_spans: list[tuple[int, int]] = []
    range_pattern = re.compile(r"\b(\d+)\s*(?:-|–|—|\bto\b)\s*(\d+)\+?\s*(?:years|yrs)\b", flags=re.I)
    for match in range_pattern.finditer(value):
        lower_bound = int(match.group(1))
        upper_bound = int(match.group(2))
        if lower_bound <= 15 and upper_bound <= 20 and experience_year_context(value, *match.span()):
            requirements.append(
                {
                    "min": lower_bound,
                    "max": upper_bound,
                    "plus": False,
                    "text": match.group(0),
                }
            )
        range_spans.append(match.span())
    if range_spans:
        chars = list(value)
        for start, end in range_spans:
            chars[start:end] = " " * (end - start)
        value = "".join(chars)
    for match in re.finditer(r"\b(\d+)(\+)?\s*(?:years|yrs)\b", value, flags=re.I):
        year_value = int(match.group(1))
        if year_value <= 15 and experience_year_context(value, *match.span()):
            requirements.append(
                {
                    "min": year_value,
                    "max": None if match.group(2) else year_value,
                    "plus": bool(match.group(2)),
                    "text": match.group(0),
                }
            )
    return requirements


def extract_month_requirements(text: str) -> list[int]:
    value = normalize_experience_text(text)
    word_numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "eighteen": 18,
        "twenty four": 24,
        "twenty-four": 24,
    }
    months: list[int] = []
    numeric_or_parenthetical = re.compile(
        r"\b(?:(?P<word>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|eighteen|twenty[-\s]four)\s*\((?P<paren>\d+)\)|(?P<num>\d+))\s*(?:months?|mos?)\b",
        flags=re.I,
    )
    for match in numeric_or_parenthetical.finditer(value):
        if match.group("paren"):
            month_value = int(match.group("paren"))
        elif match.group("num"):
            month_value = int(match.group("num"))
        else:
            month_value = word_numbers.get(str(match.group("word") or "").lower(), 0)
        if 0 < month_value <= 60:
            months.append(month_value)
    return months


def normalize_experience_text(text: str) -> str:
    return html.unescape(str(text or "")).replace("\xa0", " ")


def age_year_context(text: str, start: int, end: int) -> bool:
    """Ignore age/legal eligibility phrases such as 18+ years old/of age."""
    context = text[max(0, start - 80) : min(len(text), end + 80)].lower()
    if re.search(r"\byears?\s+(?:old|of\s+age|or\s+older)\b", context):
        return True
    if re.search(r"\b(?:age|aged)\s*(?:of\s*)?\d+\+?\b", context):
        return True
    if re.search(r"\b(?:must|should|need|required)\s+be\s+(?:at\s+least\s+)?\d+\+?\s+years?\b", context):
        return True
    return False


def experience_year_context(text: str, start: int, end: int) -> bool:
    """Return true only when a year expression is plausibly an experience requirement."""
    if age_year_context(text, start, end):
        return False
    context = text[max(0, start - 140) : min(len(text), end + 180)].lower()
    near_context = text[max(0, start - 80) : min(len(text), end + 100)].lower()
    benefit_markers = (
        "vacation",
        "paid time off",
        "pto",
        "sick leave",
        "parental leave",
        "retirement",
        "vesting",
        "service award",
        "employee benefit",
    )
    if any(marker in near_context for marker in benefit_markers) and "experience" not in near_context:
        return False
    experience_markers = (
        "experience",
        "qualification",
        "required",
        "requires",
        "requiring",
        "minimum",
        "must have",
        "you have",
        "you bring",
        "you possess",
        "candidate has",
        "applicant has",
        "professional background",
        "hands-on",
        "working with",
        "developing",
        "building",
        "supporting",
    )
    return any(marker in context for marker in experience_markers)


def minimum_experience_years(requirements: list[dict[str, Any]]) -> int:
    values = [int(item.get("min") or 0) for item in requirements if int(item.get("min") or 0) > 0]
    return min(values) if values else 0


def score_text(app: dict[str, Any], jd_text: str, profile: dict[str, Any]) -> dict[str, Any]:
    target_keywords = [str(item) for item in profile.get("targets", {}).get("keywords", [])]
    track_keywords = [str(item) for item in profile.get("_track", {}).get("scoring_keywords", [])]
    matched = sorted(set(keyword_matches(jd_text, TECH_KEYWORDS + [k.lower() for k in target_keywords + track_keywords])))
    resume_text = master_resume_path(profile).read_text(encoding="utf-8", errors="replace")
    resume_matches = sorted(set(keyword_matches(resume_text, matched)))
    ats_score = round((len(resume_matches) / len(matched)) * 100) if matched else 0

    role_text = f"{app.get('role', '')} {jd_text}".lower()
    role_score = 2.5 if any(role.lower() in role_text for role in profile.get("targets", {}).get("roles", [])) else 1.0
    tech_score = min(4.0, len(matched) * 0.45)
    location_bucket = location_preference_bucket(app.get("location", ""), profile, jd_text)
    location_score = {
        "preferred": 1.5,
        "relocation": 1.0,
        "maybe": 0.7,
        "rejected": 0.0,
    }[location_bucket]
    level_score = 2.0

    dealbreakers: list[str] = []
    action_items = []
    if re.search(r"security clearance|active clearance|secret clearance|top secret", jd_text, re.I):
        dealbreakers.append("Security clearance appears required.")
    if re.search(r"\b(senior|staff|principal|lead)\b", app.get("role", ""), re.I):
        dealbreakers.append("Role title appears senior/staff/principal/lead.")
    experience_requirements = extract_year_requirements(jd_text)
    month_requirements = extract_month_requirements(jd_text)
    experience_app = dict(app)
    experience_app["notes"] = jd_text
    experience_app["experience_bucket"] = ""
    experience_app["experience_requirements"] = []
    experience_app["action_items"] = []
    experience_app["dealbreakers"] = []
    experience_app["jd_path"] = ""
    experience_bucket = experience_requirement_bucket(experience_app)
    required_years = minimum_experience_years(experience_requirements)
    dealbreaker_config = profile.get("dealbreakers", {})
    threshold = int(dealbreaker_config.get("minimum_years_over", 5))
    skip_from = dealbreaker_config.get("skip_minimum_years_from")
    skip_from = int(skip_from) if skip_from not in (None, "") else 0
    if app.get("platform") == "amazon_jobs":
        threshold = 2
    if skip_from and required_years >= skip_from:
        dealbreakers.append(f"JD mentions {required_years}+ years, at or above skip threshold {skip_from}.")
    elif required_years > threshold:
        dealbreakers.append(f"JD mentions {required_years}+ years, above threshold {threshold}.")
    penalty_from = int(dealbreaker_config.get("lower_weight_minimum_years_from", 3))
    if required_years >= penalty_from:
        level_score = max(0.4, level_score - 1.4)
        action_items.append(
            f"JD mentions {required_years}+ years; lower priority for 0-2 years experience target."
        )
    track_id = str(profile.get("_track", {}).get("id") or "")
    if track_id == "qa_engineer":
        automation_terms = re.search(r"automation|automated|sdet|api|playwright|selenium|pytest|jest|ci/cd|pipeline", role_text)
        manual_heavy = re.search(r"manual qa|manual testing|test cases?|test scripts?|game tester|localization qa", role_text)
        hardware_heavy = re.search(r"hardware|firmware|electrical|mechanical|lab equipment|oscilloscope|manufacturing test|board bring-up", role_text)
        if manual_heavy and not automation_terms:
            level_score = max(0.4, level_score - 0.8)
            action_items.append("QA role appears manual-heavy; lower priority than automation/SDET roles.")
        if hardware_heavy and not automation_terms:
            level_score = max(0.4, level_score - 0.8)
            action_items.append("QA role appears hardware/lab-heavy; lower priority unless software automation is central.")
    if re.search(r"we do not sponsor|no sponsorship|unable to sponsor", jd_text, re.I):
        if profile.get("work_authorization", {}).get("requires_sponsorship"):
            dealbreakers.append("JD says sponsorship is unavailable.")
    if location_bucket == "rejected":
        dealbreakers.append(f"Location appears outside the United States: {app.get('location')}.")
    elif location_bucket == "relocation":
        action_items.append("US relocation role; rank below Washington and Remote US opportunities.")
    elif location_bucket == "maybe":
        action_items.append("Location is unclear; confirm US eligibility and onsite expectations.")

    fit_score = 0.0 if dealbreakers else round(min(10.0, role_score + tech_score + location_score + level_score), 1)
    status = "skipped" if dealbreakers else ("needs_review" if fit_score < 6.0 or ats_score < 60 else "scored")
    if ats_score < 60:
        action_items.append("Review ATS keyword gap before applying.")
    if fit_score < 6.0 and not dealbreakers:
        action_items.append("Low fit score; review before preparing application.")

    return {
        "fit_score": fit_score,
        "ats_score": ats_score,
        "status": status,
        "location_bucket": location_bucket,
        "experience_bucket": experience_bucket,
        "experience_requirements": [str(item.get("text") or "") for item in experience_requirements if item.get("text")]
        + [f"{months} months" for months in month_requirements],
        "matched_keywords": matched,
        "resume_keyword_matches": resume_matches,
        "missing_keywords": [keyword for keyword in matched if keyword not in resume_matches],
        "dealbreakers": dealbreakers,
        "action_items": action_items,
        "jd_text": jd_text,
    }


def track_evaluation_key(track_id: str | None) -> str:
    return str(track_id or "").strip() or "default"


def track_evaluations_with_legacy(app: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_evaluations = app.get("track_evaluations")
    evaluations = {
        str(key): dict(value)
        for key, value in (raw_evaluations.items() if isinstance(raw_evaluations, dict) else [])
        if isinstance(value, dict)
    }
    legacy_track = str(app.get("target_track") or "").strip()
    if not legacy_track or legacy_track in evaluations:
        return evaluations
    has_legacy_score = isinstance(app.get("fit_score"), (int, float)) or isinstance(app.get("ats_score"), (int, float))
    if not has_legacy_score:
        return evaluations
    legacy_status = str(app.get("status") or "needs_review")
    legacy_dealbreakers = list(app.get("dealbreakers") or [])
    evaluations[legacy_track] = {
        "track_id": legacy_track,
        "fit_score": app.get("fit_score", ""),
        "ats_score": app.get("ats_score", ""),
        "status": legacy_status,
        "eligible": legacy_status not in {"skipped", "needs_retry"} and not legacy_dealbreakers,
        "location_bucket": app.get("location_bucket", ""),
        "experience_bucket": app.get("experience_bucket", ""),
        "experience_requirements": list(app.get("experience_requirements") or []),
        "dealbreakers": legacy_dealbreakers,
        "action_items": list(app.get("action_items") or []),
        "matched_keywords": list(app.get("matched_keywords") or []),
        "resume_keyword_matches": list(app.get("resume_keyword_matches") or []),
        "missing_keywords": list(app.get("missing_keywords") or []),
        "resume_file": app.get("resume_file", ""),
        "score_report_path": app.get("score_report_path", ""),
        "evaluated_at": app.get("last_scored_at", ""),
        "legacy_imported": True,
    }
    return evaluations


def build_track_evaluation(
    track_id: str,
    score: dict[str, Any],
    resume_file: str,
    report_path: Path,
) -> dict[str, Any]:
    dealbreakers = list(score.get("dealbreakers") or [])
    status = str(score.get("status") or "needs_review")
    return {
        "track_id": track_id,
        "fit_score": score.get("fit_score", ""),
        "ats_score": score.get("ats_score", ""),
        "status": status,
        "eligible": status not in {"skipped", "needs_retry"} and not dealbreakers,
        "location_bucket": score.get("location_bucket", ""),
        "experience_bucket": score.get("experience_bucket", ""),
        "experience_requirements": list(score.get("experience_requirements") or []),
        "dealbreakers": dealbreakers,
        "action_items": list(score.get("action_items") or []),
        "matched_keywords": list(score.get("matched_keywords") or []),
        "resume_keyword_matches": list(score.get("resume_keyword_matches") or []),
        "missing_keywords": list(score.get("missing_keywords") or []),
        "resume_file": resume_file,
        "score_report_path": str(report_path),
        "evaluated_at": now_utc_iso(),
    }


def best_track_evaluation(
    evaluations: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    successful = [
        (track_id, evaluation)
        for track_id, evaluation in evaluations.items()
        if isinstance(evaluation, dict) and evaluation.get("status") != "needs_retry"
    ]
    if not successful:
        return None, None
    eligible = [item for item in successful if bool(item[1].get("eligible"))]
    candidates = eligible or successful
    experience_priority = {
        "new_grad": 5,
        "0_1": 5,
        "1_2": 4,
        "2_plus": 3,
        "2_range": 2,
        "unknown": 1,
        "3_plus": 0,
    }

    def rank(item: tuple[str, dict[str, Any]]) -> tuple[float, float, int, str]:
        track_id, evaluation = item
        return (
            numeric_score(evaluation.get("fit_score")),
            numeric_score(evaluation.get("ats_score")),
            experience_priority.get(str(evaluation.get("experience_bucket") or "unknown"), 1),
            track_id,
        )

    return max(candidates, key=rank)


def cached_job_text_for_scoring(app: dict[str, Any], jd_file: str | None = None) -> str:
    if jd_file:
        return Path(jd_file).read_text(encoding="utf-8", errors="replace")
    cached_path = str(app.get("jd_path") or "").strip()
    if cached_path:
        path = Path(cached_path)
        if path.exists():
            cached_text = path.read_text(encoding="utf-8", errors="replace").strip()
            if cached_text and not job_text_fetch_failure_reason(cached_text):
                return cached_text
    return read_job_text(app)


def location_matches(app: dict[str, Any], jd_text: str, profile: dict[str, Any]) -> bool:
    return location_preference_bucket(app.get("location", ""), profile, jd_text) == "preferred"


def app_output_dir(app: dict[str, Any]) -> Path:
    app_id = str(app.get("id") or "")
    suffix = app_id.rsplit("-", 1)[-1] if "-" in app_id else ""
    role_slug = slugify(app.get("role", "unknown-role"))
    if re.fullmatch(r"[0-9a-f]{8}", suffix):
        role_slug = f"{role_slug}-{suffix}"
    return OUTPUT_DIR / slugify(app.get("company", "unknown")) / role_slug


def master_resume_path(profile: dict[str, Any] | None = None) -> Path:
    track = (profile or {}).get("_track", {})
    track_resume = path_from_track(track, "master_resume") if track else None
    if track_resume and track_resume.exists():
        return track_resume
    person_resume = PERSON_ROOT / "resume" / "master_resume.md"
    if person_resume.exists():
        return person_resume
    return ROOT / "examples" / "master_resume.example.md"


def template_path(name: str) -> Path:
    person_template = PERSON_ROOT / "templates" / name
    if person_template.exists():
        return person_template
    return ROOT / "templates" / name


def read_job_text(app: dict[str, Any], jd_file: str | None = None) -> str:
    if jd_file:
        return Path(jd_file).read_text(encoding="utf-8", errors="replace")
    try:
        jobsyn_text = fetch_jobsyn_job_text(app)
        if jobsyn_text:
            return jobsyn_text
    except Exception:
        pass
    for fetcher in [
        fetch_ashby_job_text,
        fetch_greenhouse_job_text,
        fetch_microsoft_job_text,
        fetch_amazon_job_text,
        fetch_google_job_text,
        fetch_meta_job_text,
        fetch_m_cloud_job_text,
        fetch_hirebridge_job_text,
        fetch_successfactors_job_text,
        fetch_eightfold_job_text,
        fetch_apple_job_text,
        fetch_providence_job_text,
        fetch_ripplehire_job_text,
        fetch_jubilant_careers_job_text,
        fetch_direct_platform_job_text,
        fetch_workday_job_text,
    ]:
        try:
            text = fetcher(app["url"])
            if text:
                return text
        except Exception:
            pass
    try:
        return html_to_text(fetch_url(app["url"]))
    except urllib.error.HTTPError as error:
        if detect_platform(app["url"]) == "lever" and error.code == 404 and not app["url"].rstrip("/").endswith("/apply"):
            try:
                return html_to_text(fetch_url(app["url"].rstrip("/") + "/apply"))
            except Exception:
                pass
        return f"Unable to fetch job description from {app['url']}. Error: {error}"
    except Exception as error:  # noqa: BLE001 - preserve the message for review.
        return f"Unable to fetch job description from {app['url']}. Error: {error}"


def search_serpapi(query: str, since_days: float | None, limit: int, pages: int = 1) -> list[str]:
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        raise SystemExit("Set SERPAPI_API_KEY or use --provider bing with BING_SEARCH_API_KEY.")
    urls: list[str] = []
    seen_urls: set[str] = set()
    per_page = min(limit, 100)
    for page_index in range(max(1, pages)):
        params = {
            "engine": "google",
            "q": query,
            "api_key": api_key,
            "num": str(per_page),
        }
        if page_index:
            params["start"] = str(page_index * per_page)
        if since_days is not None:
            params["tbs"] = "qdr:w" if since_days <= 7 else "qdr:m"
        try:
            data = fetch_json(f"https://serpapi.com/search.json?{urllib.parse.urlencode(params)}")
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise SearchRateLimited("SerpAPI returned 429 Too Many Requests. Stop this run and retry later or use a smaller batch.") from error
            raise

        before = len(urls)
        for item in data.get("organic_results", []):
            url = item.get("link", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                urls.append(url)
        if len(urls) == before:
            break
    return urls


def search_bing(query: str, since_days: float | None, limit: int) -> list[str]:
    api_key = os.environ.get("BING_SEARCH_API_KEY")
    if not api_key:
        raise SystemExit("Set BING_SEARCH_API_KEY or use --provider serpapi with SERPAPI_API_KEY.")
    params = {
        "q": query,
        "count": str(min(limit, 50)),
        "responseFilter": "Webpages",
    }
    if since_days is not None:
        params["freshness"] = "Week" if since_days <= 7 else "Month"
    request = urllib.request.Request(
        f"https://api.bing.microsoft.com/v7.0/search?{urllib.parse.urlencode(params)}",
        headers={"Ocp-Apim-Subscription-Key": api_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    return [item.get("url", "") for item in data.get("webPages", {}).get("value", []) if item.get("url")]


def web_search_urls(query: str, args: argparse.Namespace) -> list[str]:
    since_days = relative_search_days(args)
    if args.provider == "serpapi":
        return search_serpapi(query, since_days, args.results_per_query, args.pages_per_query)
    if args.provider == "bing":
        return search_bing(query, since_days, args.results_per_query)
    raise SystemExit(f"Unsupported search provider: {args.provider}")


def build_web_discovery_queries(args: argparse.Namespace) -> list[str]:
    track = load_track(getattr(args, "track", None))
    roles = args.role or track.get("web_discovery_roles") or DEFAULT_WEB_DISCOVERY_ROLES
    locations = args.location or DEFAULT_WEB_DISCOVERY_LOCATIONS
    queries = []
    for site in ATS_SEARCH_SITES:
        for role in roles:
            for location in locations:
                queries.append(f'site:{site} "{role}" "{location}"')
    grouped: dict[str, list[str]] = {}
    for query in queries:
        site = query.split(" ", 1)[0]
        grouped.setdefault(site, []).append(query)
    balanced: list[str] = []
    while any(grouped.values()):
        for site in list(grouped):
            if grouped[site]:
                balanced.append(grouped[site].pop(0))
    return balanced


def wait_between_search_queries(args: argparse.Namespace, query_index: int) -> None:
    delay = float(getattr(args, "search_delay_seconds", 0) or 0)
    if query_index > 0 and delay > 0:
        time.sleep(delay)


def load_watchlist() -> dict[str, Any]:
    if not WATCHLIST_PATH.exists():
        return {"companies": []}
    data = load_json(WATCHLIST_PATH)
    data.setdefault("companies", [])
    return data


def list_or_default(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list) and value:
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return default


def build_watchlist_queries(args: argparse.Namespace) -> list[dict[str, str]]:
    watchlist = load_watchlist()
    track = load_track(getattr(args, "track", None))
    default_roles = args.role or track.get("watchlist_roles") or track.get("web_discovery_roles") or watchlist.get("default_roles") or DEFAULT_WEB_DISCOVERY_ROLES
    default_locations = args.location or watchlist.get("default_locations") or DEFAULT_WEB_DISCOVERY_LOCATIONS
    default_exclusions = [str(item).strip() for item in watchlist.get("default_exclusions", []) if str(item).strip()]
    query_items: list[dict[str, str]] = []

    for company_config in watchlist.get("companies", []):
        if not company_config.get("active", True):
            continue
        company = str(company_config.get("company", "")).strip()
        if not company:
            continue
        sites = list_or_default(company_config.get("career_sites"), [])
        roles = list_or_default(company_config.get("roles"), default_roles)
        locations = list_or_default(company_config.get("locations"), default_locations)
        templates = list_or_default(company_config.get("query_templates"), [])
        exclusions = [str(item).strip() for item in company_config.get("exclude_terms", default_exclusions) if str(item).strip()]
        exclusion_suffix = " ".join(f"-{term}" for term in exclusions)

        if templates:
            for template in templates:
                has_placeholder = "{" in template and "}" in template
                site_values = sites or [""]
                role_values = roles if "{role}" in template else [""]
                location_values = locations if "{location}" in template else [""]
                for site in site_values:
                    for role in role_values:
                        for location in location_values:
                            try:
                                query = template.format(company=company, site=site, role=role, location=location)
                            except KeyError as error:
                                raise SystemExit(f"Bad query template for {company}: missing {{{error.args[0]}}}") from error
                            query = re.sub(r"\s+", " ", f"{query} {exclusion_suffix}").strip()
                            if query:
                                query_items.append({"company": company, "site": site, "query": query})
                if not has_placeholder and template.strip():
                    query = re.sub(r"\s+", " ", f"{template.strip()} {exclusion_suffix}").strip()
                    query_items.append({"company": company, "site": sites[0] if sites else "", "query": query})
            continue

        for site in sites:
            for role in roles:
                for location in locations:
                    query = f'site:{site} "{role}" "{location}" {exclusion_suffix}'.strip()
                    query_items.append(
                        {
                            "company": company,
                            "site": site,
                            "query": query,
                        }
                    )

    deduped: list[dict[str, str]] = []
    seen_queries: set[str] = set()
    for item in query_items:
        if item["query"] in seen_queries:
            continue
        seen_queries.add(item["query"])
        deduped.append(item)

    grouped: dict[str, list[dict[str, str]]] = {}
    for item in deduped:
        grouped.setdefault(item["company"], []).append(item)
    balanced: list[dict[str, str]] = []
    while any(grouped.values()):
        for company in list(grouped):
            if grouped[company]:
                balanced.append(grouped[company].pop(0))
    return balanced[: args.max_queries]


def html_attr(pattern: str, raw: str) -> str:
    match = re.search(pattern, raw, flags=re.I | re.S)
    return html.unescape(match.group(1)).strip() if match else ""


def extract_html_title(raw: str, company: str, url: str) -> str:
    title = (
        html_attr(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', raw)
        or html_attr(r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']', raw)
        or html_attr(r"<title[^>]*>(.*?)</title>", raw)
        or html_attr(r"<h1[^>]*>(.*?)</h1>", raw)
    )
    title = html_to_text(title)
    for separator in [" | ", " - ", " – ", " — ", " at "]:
        if separator in title:
            parts = [part.strip() for part in title.split(separator) if part.strip()]
            if parts:
                title = parts[0]
                break
    if company:
        title = re.sub(rf"\b{re.escape(company)}\b", "", title, flags=re.I).strip(" -|")
    return title or infer_role_from_url(url)


def extract_first_datetime(raw: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I | re.S)
        if not match:
            continue
        normalized = normalize_datetime(html.unescape(match.group(1)))
        if normalized:
            return normalized
    return ""


def extract_location(raw: str) -> str:
    text = html_to_text(raw[:80_000])
    patterns = [
        r"\bLocation\s*[:\-]\s*([^|•\n]{2,90})",
        r"\bLocations\s*[:\-]\s*([^|•\n]{2,90})",
        r"\bWork Location\s*[:\-]\s*([^|•\n]{2,90})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).strip()
    for location in DEFAULT_WEB_DISCOVERY_LOCATIONS + ["Redmond", "Mountain View", "Sunnyvale", "Menlo Park"]:
        if re.search(rf"\b{re.escape(location)}\b", text, flags=re.I):
            return location
    return ""


def candidate_from_watchlist_url(
    url: str,
    company: str,
    query: str,
    current_seen_at: str,
    use_search_seen_date: bool,
) -> dict[str, Any] | None:
    normalized = normalize_job_url(url)
    ats_candidate = ats_candidate_from_url(normalized)
    if ats_candidate:
        ats_candidate["source_query"] = query
        ats_candidate["freshness_source"] = "official_posted_at" if ats_candidate.get("posted_at") else "unknown"
        if not ats_candidate.get("posted_at") and use_search_seen_date:
            ats_candidate["posted_at"] = current_seen_at
            ats_candidate["freshness_source"] = "search_seen_at"
            ats_candidate["notes"] = "No official posted date parsed; using first search-seen time for freshness."
        return ats_candidate

    try:
        raw = fetch_url(normalized)
    except Exception as error:  # noqa: BLE001
        print(f"Could not fetch watchlist URL {normalized}: {error}", file=sys.stderr)
        return None

    posted_at = extract_first_datetime(
        raw,
        [
            r'"datePosted"\s*:\s*"([^"]+)"',
            r'"datePublished"\s*:\s*"([^"]+)"',
            r'"postedDate"\s*:\s*"([^"]+)"',
            r'"published_at"\s*:\s*"([^"]+)"',
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
        ],
    )
    updated_at = extract_first_datetime(
        raw,
        [
            r'"dateModified"\s*:\s*"([^"]+)"',
            r'"updated_at"\s*:\s*"([^"]+)"',
            r'<meta[^>]+property=["\']article:modified_time["\'][^>]+content=["\']([^"\']+)["\']',
        ],
    )
    freshness_source = "official_posted_at" if posted_at else "unknown"
    notes = ""
    if not posted_at and use_search_seen_date:
        posted_at = current_seen_at
        freshness_source = "search_seen_at"
        notes = "Watchlist search result; no official posted date parsed; using first search-seen time for freshness."

    return {
        "company": company,
        "role": extract_html_title(raw, company, normalized),
        "url": normalized,
        "platform": detect_platform(normalized),
        "location": extract_location(raw),
        "posted_at": posted_at,
        "updated_at": updated_at,
        "source": f"watchlist:{company}",
        "source_query": query,
        "freshness_source": freshness_source,
        "notes": notes,
    }


def source_from_candidate(candidate: dict[str, Any]) -> dict[str, Any] | None:
    source_url = candidate.get("source") or candidate.get("url")
    platform = detect_platform(source_url)
    if platform == "greenhouse":
        board = greenhouse_board_from_source({"url": source_url})
        if not board:
            return None
        return {
            "company": candidate.get("company") or board,
            "platform": "greenhouse",
            "board": board,
            "url": f"https://job-boards.greenhouse.io/{board}",
        }
    if platform == "lever":
        site = lever_site_from_source({"url": source_url})
        if not site:
            return None
        return {
            "company": candidate.get("company") or site,
            "platform": "lever",
            "site": site,
            "url": f"https://jobs.lever.co/{site}",
        }
    if platform == "ashby":
        board = ashby_board_from_source({"url": source_url})
        if not board:
            return None
        return {
            "company": candidate.get("company") or board,
            "platform": "ashby",
            "board": board,
            "url": f"https://jobs.ashbyhq.com/{board}",
        }
    if platform == "gem":
        board = gem_board_from_source({"url": source_url})
        if not board:
            return None
        return {
            "company": candidate.get("company") or board,
            "platform": "gem",
            "board": board,
            "url": f"https://jobs.gem.com/{board}",
        }
    if platform == "workday":
        parts = workday_source_parts({"url": source_url})
        if not parts:
            return None
        host, tenant, site = parts
        return {
            "company": candidate.get("company") or tenant,
            "platform": "workday",
            "host": host,
            "tenant": tenant,
            "site": site,
            "url": workday_board_url(host, tenant, site),
        }
    return None


def update_sources_from_candidates(candidates: list[dict[str, Any]]) -> int:
    data = load_json(SOURCES_PATH)
    sources = data.setdefault("sources", [])
    existing_urls = {normalize_job_url(source.get("url", "")) for source in sources}
    added = 0
    for candidate in candidates:
        source = source_from_candidate(candidate)
        if not source:
            continue
        source_url = normalize_job_url(source["url"])
        if source_url in existing_urls:
            continue
        sources.append(source)
        existing_urls.add(source_url)
        added += 1
    if added:
        sources.sort(key=lambda item: (str(item.get("company", "")).lower(), str(item.get("platform", "")).lower()))
        write_json(SOURCES_PATH, data)
    return added


def source_from_workday_url(company: str, url: str) -> dict[str, Any] | None:
    parts = workday_source_parts({"url": url})
    if not parts:
        return None
    host, tenant, site = parts
    return {
        "company": company or tenant,
        "platform": "workday",
        "host": host,
        "tenant": tenant,
        "site": site,
        "url": workday_board_url(host, tenant, site),
        "page_size": 20,
        "max_pages": 5,
        "keywords": DEFAULT_WORKDAY_KEYWORDS,
    }


def source_from_phenom_url(company: str, url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else url.rstrip("/")
    return {
        "company": company or parsed.netloc,
        "platform": "phenom",
        "url": base,
        "widgets_url": urllib.parse.urljoin(base.rstrip("/") + "/", "/widgets"),
        "base_url": base,
        "locale_path": "/us/en",
        "page_size": 20,
        "max_pages": 3,
        "keywords": DEFAULT_WORKDAY_KEYWORDS,
    }


def source_from_m_cloud_page(company: str, url: str, raw: str) -> dict[str, Any] | None:
    api_match = re.search(r'"api"\s*:\s*"([^"]*m-cloud\.io\\/api\\/?)"', raw)
    org_match = re.search(r'"org"\s*:\s*"([^"]+)"', raw)
    if not api_match or not org_match:
        return None
    api_url = html.unescape(api_match.group(1)).replace("\\/", "/")
    organization = html.unescape(org_match.group(1)).replace("\\/", "/")
    return {
        "company": company or urllib.parse.urlparse(url).netloc,
        "platform": "m_cloud",
        "url": url,
        "api_url": api_url.rstrip("/"),
        "company_name": organization,
        "page_size": 25,
        "max_pages": 5,
        "keywords": DEFAULT_WORKDAY_KEYWORDS,
    }


def source_from_hirebridge_page(company: str, url: str, raw: str) -> dict[str, Any] | None:
    client_match = re.search(r"hirebridge_client\s*=\s*['\"]([^'\"]+)['\"]", raw)
    if not client_match:
        return None
    client_id = html.unescape(client_match.group(1))
    return {
        "company": company or urllib.parse.urlparse(url).netloc,
        "platform": "hirebridge",
        "url": url,
        "client_id": client_id,
        "feed_url": f"https://rss.hirebridge.com/{urllib.parse.quote(client_id)}.json",
    }


def source_from_talentbrew_url(company: str, url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else url.rstrip("/")
    org_match = re.search(r'(?:orgIds|organizationIds)=([^&#]+)', url, flags=re.I)
    return {
        "company": company or parsed.netloc,
        "platform": "talentbrew",
        "url": normalize_job_url(url),
        "results_url": urllib.parse.urljoin(base.rstrip("/") + "/", "/en/search-jobs/results"),
        "organization_ids": urllib.parse.unquote(org_match.group(1)) if org_match else "",
        "page_size": 15,
        "max_pages": 3,
        "keywords": DEFAULT_WORKDAY_KEYWORDS,
    }


def source_from_careerpuck_url(company: str, url: str) -> dict[str, Any]:
    board = careerpuck_board({"company": company, "url": url})
    return {
        "company": company or board,
        "platform": "careerpuck",
        "board": board,
        "url": url,
        "api_url": f"https://api.careerpuck.com/v1/public/job-boards/{urllib.parse.quote(board)}",
    }


def classify_source(source: dict[str, Any]) -> dict[str, Any]:
    company = str(source.get("company", "")).strip()
    url = str(source.get("url", "")).strip()
    result = {
        "company": company,
        "current_platform": source_platform(source),
        "detected_platform": "",
        "detected_url": "",
        "source": None,
        "notes": "",
    }
    direct_platform = detect_platform(url)
    directly_classifiable = {
        "greenhouse",
        "lever",
        "ashby",
        "gem",
        "workday",
        "eightfold",
        "apple_jobs",
        "providence_jobs",
        "jobsyn",
        "breezy",
        "smartrecruiters",
        "topechelon",
        "icims",
        "oracle_cx",
        "clearcompany",
        "paylocity",
        "dynamicsats",
        "hanford_bms",
        "applicantpro",
        "dayforce",
        "kronos_careers",
        "healthcaresource",
        "paradox",
        "adp_myjobs",
        "adp_workforce_now",
        "appone",
        "avature",
        "jubilant_careers",
        "jobvite",
        "taleo",
        "pageup",
        "talentreef",
        "peopleadmin",
        "paycom",
        "ultipro",
        "zoho_recruit",
        "workable",
        "bamboohr",
        "yc_jobs",
        "yc_job_board",
        "hn_who_is_hiring",
        "rss",
        "jibe",
        "talentbrew",
        "ttcportals",
        "workgr8",
        "infor_cloudsuite",
        "viewpoint_for_cloud",
        "hireology",
        "prismhr",
        "applicantstack",
        "cyber_recruiter",
        "careerpuck",
        "pinpoint",
        "brassring",
        "jazzhr",
        "hiringthing",
        "paycor",
        "wp_search_index",
        "joveo",
        "clinch",
        "atkins_jobs",
        "isg_poweredby",
        "embedded_jobs",
        "wordpress_taleo",
    }
    if direct_platform in directly_classifiable:
        result["detected_platform"] = direct_platform
        result["detected_url"] = url
    else:
        try:
            raw = fetch_url(url, timeout=15)
        except Exception as error:  # noqa: BLE001
            result["notes"] = f"fetch_failed: {error}"
            return result
        links = re.findall(r'https?://[^"\'<>\s)]+', raw, flags=re.I)
        links.extend(re.findall(r'href=["\']([^"\']+)', raw, flags=re.I))
        links = [urllib.parse.urljoin(url, html.unescape(candidate)) for candidate in links if candidate]
        for candidate in links:
            candidate_parsed = urllib.parse.urlparse(candidate)
            candidate_host = candidate_parsed.netloc.lower()
            if candidate_host in {"cms.jibecdn.com", "tbcdn.talentbrew.com", "static.careerpuck.com"}:
                continue
            if re.search(r"\.(?:css|js|map|png|jpe?g|gif|svg|ico|woff2?|ttf)(?:$|[?#])", candidate_parsed.path, flags=re.I):
                continue
            if "careerpuck.com" in candidate_host and not (
                candidate_host.startswith("app.") or candidate_host.startswith("api.")
            ):
                continue
            platform = detect_platform(candidate)
            if platform in directly_classifiable:
                result["detected_platform"] = platform
                result["detected_url"] = normalize_job_url(candidate)
                break
        if not result["detected_platform"] and re.search(r"jibecdn|jibeapply|/api/jobs", raw, flags=re.I):
            result["detected_platform"] = "jibe"
            result["detected_url"] = url
            result["notes"] = "Jibe careers site detected from page assets/API references."
        if not result["detected_platform"] and re.search(
            r'avature\.portal\.id|<list\b[^>]*\blistType[^>]*JobList',
            raw,
            flags=re.I,
        ):
            result["detected_platform"] = "avature"
            result["detected_url"] = url
            result["notes"] = "Avature public job list detected."
        if not result["detected_platform"] and re.search(
            r'HiringThing\.Components\.|assets\.applicant-tracking\.com',
            raw,
            flags=re.I,
        ):
            result["detected_platform"] = "hiringthing"
            result["detected_url"] = url
            result["notes"] = "HiringThing public job board detected."
        if not result["detected_platform"] and re.search(r"\.joveo\.site/jobs-api/", raw, flags=re.I):
            result["detected_platform"] = "joveo"
            result["detected_url"] = url
            result["notes"] = "Joveo public careers API detected; endpoint configuration is required."
        if not result["detected_platform"] and re.search(r"phenom|phenompeople|phenom-people", raw, flags=re.I):
            result["detected_platform"] = "phenom"
            result["detected_url"] = url
            result["notes"] = "Phenom detected."
        if not result["detected_platform"] and re.search(
            r"window\.__PRELOAD_STATE__|cdn\.sites\.paradox\.ai",
            raw,
            flags=re.I,
        ):
            result["detected_platform"] = "paradox"
            result["detected_url"] = url
            result["notes"] = "Paradox career site detected from preload data or page assets."
        if not result["detected_platform"] and re.search(r"jobsapi-[a-z-]*\.m-cloud\.io/api", raw, flags=re.I):
            result["detected_platform"] = "m_cloud"
            result["detected_url"] = url
            result["source"] = source_from_m_cloud_page(company, url, raw)
            result["notes"] = "m-cloud detected." if result["source"] else "m-cloud detected but config could not be parsed."
        if not result["detected_platform"] and re.search(r"hirebridge_client|hirebridge\.com/assets/portal", raw, flags=re.I):
            result["detected_platform"] = "hirebridge"
            result["detected_url"] = url
            result["source"] = source_from_hirebridge_page(company, url, raw)
            result["notes"] = "Hirebridge detected." if result["source"] else "Hirebridge detected but client id could not be parsed."
        if not result["detected_platform"] and re.search(r"talentbrew|tbcdn\.talentbrew\.com|data-ajax-url=[\"'][^\"']*/search-jobs/results", raw, flags=re.I):
            result["detected_platform"] = "talentbrew"
            result["detected_url"] = url
            result["notes"] = "TalentBrew detected."
        if not result["detected_platform"] and re.search(r"careerpuck|static\.careerpuck\.com|api\.careerpuck\.com", raw, flags=re.I):
            result["detected_platform"] = "careerpuck"
            result["detected_url"] = url
            result["notes"] = "CareerPuck detected."
        if not result["detected_platform"]:
            top_echelon_tag = re.search(
                r"<job-board\b[^>]*\bapi-key=[\"']([^\"']+)",
                raw,
                flags=re.I,
            )
            if top_echelon_tag:
                result["detected_platform"] = "topechelon"
                result["detected_url"] = url
                result["source"] = {
                    "company": company,
                    "platform": "topechelon",
                    "url": url,
                    "api_key": html.unescape(top_echelon_tag.group(1)),
                    "max_pages": 25,
                }
                result["notes"] = "Top Echelon public job board detected."
        if not result["detected_platform"] and re.search(
            r"CRCareersPage\.css|CRCareers1_|type=DRAWSINGLEGROUPLIST",
            raw,
            flags=re.I,
        ):
            result["detected_platform"] = "cyber_recruiter"
            result["detected_url"] = url
            result["source"] = {
                "company": company,
                "platform": "cyber_recruiter",
                "url": url,
                "max_list_pages": 50,
                "max_detail_pages": 100,
                "detail_workers": 8,
            }
            result["notes"] = "Cyber Recruiter server-rendered job board detected."
        if not result["detected_platform"] and re.search(
            r"talentReef|marketing-assets\.jobappnetwork\.com|"
            r"prod-kong\.internal\.talentreef\.com",
            raw,
            flags=re.I,
        ):
            result["detected_platform"] = "talentreef"
            result["detected_url"] = url
            result["source"] = source_from_talentreef_page(company, url, raw)
            result["notes"] = (
                "TalentReef detected."
                if result["source"]
                else "TalentReef detected but client id could not be parsed."
            )
        if not result["detected_platform"] and "governmentjobs.com" in urllib.parse.urlparse(url).netloc.lower():
            result["detected_platform"] = "governmentjobs"
            result["detected_url"] = url
            result["notes"] = "GovernmentJobs/NEOGOV detected."

    detected_url = str(result.get("detected_url") or "")
    platform = str(result.get("detected_platform") or "")
    if platform == "greenhouse":
        board = greenhouse_board_from_source({"url": detected_url})
        if board and greenhouse_board_is_fetchable(board):
            result["source"] = {"company": company or board, "platform": "greenhouse", "board": board, "url": f"https://job-boards.greenhouse.io/{board}"}
        elif board:
            result["notes"] = "Greenhouse board candidate was not fetchable."
    elif platform == "lever":
        site = lever_site_from_source({"url": detected_url})
        result["source"] = {"company": company or site, "platform": "lever", "site": site, "url": f"https://jobs.lever.co/{site}"} if site else None
    elif platform == "ashby":
        board = ashby_board_from_source({"url": detected_url})
        result["source"] = {"company": company or board, "platform": "ashby", "board": board, "url": f"https://jobs.ashbyhq.com/{board}"} if board else None
    elif platform == "gem":
        board = gem_board_from_source({"url": detected_url})
        result["source"] = {"company": company or board, "platform": "gem", "board": board, "url": f"https://jobs.gem.com/{board}"} if board else None
    elif platform == "workday":
        result["source"] = source_from_workday_url(company, detected_url)
    elif platform == "phenom":
        result["source"] = source_from_phenom_url(company, detected_url)
    elif platform == "m_cloud" and not result.get("source"):
        result["notes"] = result.get("notes") or "m-cloud detected but config could not be parsed."
    elif platform == "hirebridge" and not result.get("source"):
        result["notes"] = result.get("notes") or "Hirebridge detected but client id could not be parsed."
    elif platform == "eightfold":
        host = urllib.parse.urlparse(detected_url).netloc.lower()
        domain = "nvidia.com" if "nvidia" in host else "starbucks.com" if "starbucks" in host else host.replace(".eightfold.ai", ".com")
        result["source"] = {"company": company or domain, "platform": "eightfold", "url": detected_url, "base_url": f"https://{host}", "domain": domain}
    elif platform == "apple_jobs":
        result["source"] = {"company": company or "Apple", "platform": "apple_jobs", "url": detected_url}
    elif platform == "providence_jobs":
        result["source"] = {"company": company or "Providence", "platform": "providence_jobs", "url": detected_url}
    elif platform == "jobsyn":
        origin = urllib.parse.urlparse(detected_url).netloc.lower()
        result["source"] = {
            "company": company or origin,
            "platform": "jobsyn",
            "url": detected_url,
            "origin": origin,
        }
    elif platform == "breezy":
        parsed = urllib.parse.urlparse(detected_url)
        board_url = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        result["source"] = {
            "company": company or parsed.netloc.removesuffix(".breezy.hr"),
            "platform": "breezy",
            "url": board_url,
            "feed_url": f"{board_url}/json",
        }
    elif platform == "hiringthing":
        result["source"] = {
            "company": company,
            "platform": "hiringthing",
            "url": detected_url,
            "max_detail_pages": 100,
            "detail_workers": 8,
        }
    elif platform == "paycor":
        result["source"] = {
            "company": company,
            "platform": "paycor",
            "url": detected_url,
            "max_detail_pages": 40,
            "detail_workers": 8,
        }
    elif platform == "joveo":
        result["notes"] = result.get("notes") or "Joveo detected; add api_url and optional geographic filters."
    elif platform == "salesforce_jobs":
        result["source"] = {"company": company or "Salesforce", "platform": "salesforce_jobs", "url": "https://careers.salesforce.com/en/jobs/"}
    elif platform == "smartrecruiters":
        identifier = smartrecruiters_identifier({"company": company, "url": detected_url})
        result["source"] = {"company": company or identifier, "platform": "smartrecruiters", "company_identifier": identifier, "url": detected_url}
    elif platform == "topechelon" and not result.get("source"):
        api_key = topechelon_api_key({"url": detected_url})
        result["source"] = {
            "company": company,
            "platform": "topechelon",
            "url": detected_url,
            "api_key": api_key,
            "max_pages": 25,
        } if api_key else None
        if not api_key:
            result["notes"] = (
                "Top Echelon detected but the public board API key was not "
                "present in the URL."
            )
    elif platform == "icims":
        result["source"] = {"company": company, "platform": "icims", "url": detected_url}
    elif platform == "oracle_cx":
        result["source"] = {"company": company, "platform": "oracle_cx", "url": detected_url, "site_number": oracle_site_number({"url": detected_url})}
    elif platform == "clearcompany":
        result["source"] = {
            "company": company,
            "platform": "clearcompany",
            "url": detected_url,
            "api_short_name": clearcompany_short_name({"url": detected_url}),
        }
    elif platform == "paylocity":
        result["source"] = {
            "company": company,
            "platform": "paylocity",
            "url": detected_url,
        }
    elif platform == "applicantpro":
        result["source"] = {
            "company": company,
            "platform": "applicantpro",
            "url": detected_url,
        }
    elif platform == "dayforce":
        client_namespace, job_board_code = dayforce_board_parts({"url": detected_url})
        result["source"] = {
            "company": company,
            "platform": "dayforce",
            "url": detected_url,
            "client_namespace": client_namespace,
            "job_board_code": job_board_code,
        }
    elif platform == "adp_myjobs":
        result["source"] = {
            "company": company,
            "platform": "adp_myjobs",
            "url": detected_url,
            "domain": adp_myjobs_domain({"url": detected_url}),
        }
    elif platform == "adp_workforce_now":
        cid, career_center_id, locale = adp_board_parts({"url": detected_url})
        result["source"] = {
            "company": company,
            "platform": "adp_workforce_now",
            "url": detected_url,
            "cid": cid,
            "career_center_id": career_center_id,
            "locale": locale,
        }
    elif platform == "appone":
        result["source"] = {
            "company": company,
            "platform": "appone",
            "url": detected_url,
        }
    elif platform == "avature":
        result["source"] = {
            "company": company,
            "platform": "avature",
            "url": detected_url,
        }
    elif platform == "jobvite":
        result["source"] = {"company": company, "platform": "jobvite", "company_id": jobvite_company_id({"company": company, "url": detected_url}), "url": detected_url}
    elif platform == "taleo":
        result["source"] = {
            "company": company,
            "platform": "taleo",
            "url": detected_url,
        }
    elif platform == "pageup":
        result["source"] = {
            "company": company,
            "platform": "pageup",
            "url": detected_url,
        }
    elif platform == "talentreef" and not result.get("source"):
        result["source"] = source_from_talentreef_page(
            company,
            detected_url,
        )
        if not result["source"]:
            result["notes"] = (
                result.get("notes")
                or "TalentReef detected but client id could not be parsed."
            )
    elif platform == "peopleadmin":
        result["source"] = {
            "company": company,
            "platform": "peopleadmin",
            "url": detected_url,
        }
    elif platform == "paycom":
        result["source"] = source_from_paycom_url(company, detected_url)
    elif platform == "ultipro":
        result["source"] = source_from_ultipro_url(company, detected_url)
    elif platform == "healthcaresource":
        result["source"] = {
            "company": company,
            "platform": "healthcaresource",
            "url": detected_url,
            "site_id": healthcaresource_site_id({"url": detected_url}),
        }
    elif platform == "zoho_recruit":
        result["source"] = {
            "company": company,
            "platform": "zoho_recruit",
            "url": detected_url,
        }
    elif platform == "workable":
        result["source"] = {"company": company, "platform": "workable", "account": workable_account({"company": company, "url": detected_url}), "url": detected_url}
    elif platform == "bamboohr":
        result["source"] = {"company": company, "platform": "bamboohr", "subdomain": bamboohr_subdomain({"company": company, "url": detected_url}), "url": detected_url}
    elif platform == "yc_jobs":
        result["source"] = {"company": company, "platform": "yc_jobs", "url": detected_url}
    elif platform == "yc_job_board":
        result["source"] = {"company": company or "Y Combinator Jobs", "platform": "yc_job_board", "url": detected_url, "role": "eng"}
    elif platform == "hn_who_is_hiring":
        result["source"] = {"company": company or "Hacker News Who is Hiring", "platform": "hn_who_is_hiring", "url": detected_url}
    elif platform == "rss":
        result["source"] = {"company": company, "platform": "rss", "url": detected_url, "feed_url": detected_url}
    elif platform == "jibe":
        result["source"] = {"company": company, "platform": "jibe", "url": detected_url, "api_url": jibe_api_url({"url": detected_url}), "keywords": DEFAULT_WORKDAY_KEYWORDS}
    elif platform == "talentbrew":
        result["source"] = source_from_talentbrew_url(company, detected_url)
    elif platform == "ttcportals":
        result["source"] = {
            "company": company,
            "platform": "ttcportals",
            "url": detected_url,
            "listing_urls": [detected_url],
            "max_pages": 3,
            "browser_subprocess_timeout": 40,
        }
    elif platform == "workgr8":
        result["source"] = {
            "company": company,
            "platform": "workgr8",
            "url": detected_url,
            "page_size": 100,
            "max_pages": 5,
        }
    elif platform == "infor_cloudsuite":
        _origin, _app_path, board, organization = infor_cloudsuite_parts(
            {"url": detected_url}
        )
        result["source"] = {
            "company": company,
            "platform": "infor_cloudsuite",
            "url": detected_url,
            "job_board": board,
            "hr_organization": organization,
            "page_size": 100,
            "max_pages": 10,
            "max_detail_pages": 20,
        }
    elif platform == "viewpoint_for_cloud":
        result["source"] = {
            "company": company,
            "platform": "viewpoint_for_cloud",
            "url": detected_url,
            "max_detail_pages": 100,
            "detail_workers": 8,
        }
    elif platform == "hireology":
        result["source"] = {
            "company": company,
            "platform": "hireology",
            "url": detected_url,
            "careers_path": hireology_careers_path({"url": detected_url}),
            "page_size": 500,
            "max_pages": 10,
        }
    elif platform == "applicantstack":
        result["source"] = {
            "company": company,
            "platform": "applicantstack",
            "url": detected_url,
            "max_detail_pages": 100,
            "detail_workers": 8,
        }
    elif platform == "cyber_recruiter":
        result["source"] = {
            "company": company,
            "platform": "cyber_recruiter",
            "url": detected_url,
            "max_list_pages": 50,
            "max_detail_pages": 100,
            "detail_workers": 8,
        }
    elif platform == "careerpuck":
        result["source"] = source_from_careerpuck_url(company, detected_url)
    elif platform == "pinpoint":
        result["source"] = {"company": company, "platform": "pinpoint", "url": detected_url, "jobs_url": pinpoint_jobs_url({"url": detected_url})}
    elif platform == "brassring":
        result["source"] = {"company": company, "platform": "brassring", "url": detected_url, "job_urls": [detected_url]}
    elif platform == "governmentjobs":
        result["source"] = {"company": company or "GovernmentJobs", "platform": "governmentjobs", "url": detected_url, "agency": governmentjobs_agency({"url": detected_url}), "keywords": DEFAULT_WORKDAY_KEYWORDS}
    return result


def command_classify_sources(args: argparse.Namespace) -> None:
    require_person_files()
    data = load_json(SOURCES_PATH)
    sources = data.setdefault("sources", [])
    company_filters = {item.lower() for item in (getattr(args, "source_company", None) or [])}
    changed = 0
    for index, source in enumerate(sources):
        if args.custom_only and source_platform(source) != "custom":
            continue
        company = str(source.get("company", ""))
        if company_filters and company.lower() not in company_filters:
            continue
        result = classify_source(source)
        detected = result.get("detected_platform") or "unknown"
        note = f" ({result['notes']})" if result.get("notes") else ""
        print(f"{company}: {source_platform(source)} -> {detected} {result.get('detected_url', '')}{note}")
        replacement = result.get("source")
        if args.apply and isinstance(replacement, dict) and detected:
            if replacement != source:
                sources[index] = replacement
                changed += 1
    if args.apply and changed:
        write_json(SOURCES_PATH, data)
    if args.apply:
        print(f"Updated {changed} sources in {SOURCES_PATH}.")


def source_quality(source: dict[str, Any]) -> tuple[str, str]:
    platform = source_platform(source)
    api_good = {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "microsoft_jobs",
        "amazon_jobs",
        "google_jobs",
        "meta_jobs",
        "eightfold",
        "oracle_cx",
        "clearcompany",
        "paylocity",
        "applicantpro",
        "dayforce",
        "kronos_careers",
        "healthcaresource",
        "paradox",
        "adp_myjobs",
        "adp_workforce_now",
        "appone",
        "avature",
        "jubilant_careers",
        "smartrecruiters",
        "topechelon",
        "workable",
        "bamboohr",
        "jobvite",
        "pageup",
        "talentreef",
        "peopleadmin",
        "paycom",
        "ultipro",
        "zoho_recruit",
        "jobsyn",
        "breezy",
        "hiringthing",
        "wp_search_index",
        "joveo",
        "atkins_jobs",
        "workgr8",
        "infor_cloudsuite",
        "viewpoint_for_cloud",
        "hireology",
        "prismhr",
    }
    api_ok = {
        "phenom",
        "m_cloud",
        "hirebridge",
        "successfactors",
        "icims",
        "jibe",
        "talentbrew",
        "ttcportals",
        "browser_static",
        "careerpuck",
        "pinpoint",
        "governmentjobs",
        "governmentjobs_global",
        "rss",
        "sitemap",
        "yc_jobs",
        "yc_job_board",
        "startup_jobs",
        "builtin_jobs",
        "getro_jobs",
        "consider_jobs",
        "hn_who_is_hiring",
        "kula",
        "jazzhr",
        "hiringthing",
        "paycor",
        "clinch",
        "isg_poweredby",
        "embedded_jobs",
        "wordpress_taleo",
        "dynamicsats",
        "hanford_bms",
        "cadient",
        "taleo",
        "static_html",
        "applicantstack",
        "cyber_recruiter",
    }
    official_posted_at = {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "microsoft_jobs",
        "amazon_jobs",
        "google_jobs",
        "eightfold",
        "oracle_cx",
        "clearcompany",
        "paylocity",
        "dayforce",
        "adp_myjobs",
        "adp_workforce_now",
        "appone",
        "avature",
        "getro_jobs",
        "consider_jobs",
        "phenom",
        "successfactors",
        "talentbrew",
        "smartrecruiters",
        "topechelon",
        "jobvite",
        "pageup",
        "talentreef",
        "peopleadmin",
        "paycom",
        "ultipro",
        "healthcaresource",
        "paradox",
        "zoho_recruit",
        "workable",
        "bamboohr",
        "careerpuck",
        "governmentjobs",
        "governmentjobs_global",
        "jobsyn",
        "jazzhr",
        "hiringthing",
        "wp_search_index",
        "joveo",
        "clinch",
        "atkins_jobs",
        "embedded_jobs",
        "hanford_bms",
        "cadient",
        "taleo",
        "breezy",
        "workgr8",
        "infor_cloudsuite",
        "viewpoint_for_cloud",
        "hireology",
        "applicantstack",
        "prismhr",
    }
    if platform in api_good:
        quality = "api_good"
    elif platform in api_ok:
        quality = "api_ok"
    elif platform == "custom":
        quality = "custom_weak"
    else:
        quality = "manual_only"
    if platform == "rss" and (
        str(source.get("target_platform") or "").lower() == "teamtailor"
        or truthy_source_flag(source.get("official_feed"), default=False)
    ):
        quality = "api_good"
        posted = "official"
    elif platform in official_posted_at:
        posted = "official"
    elif platform in {"sitemap", "rss"}:
        posted = "updated_proxy"
    elif platform in {"kula", "dynamicsats", "paycor", "kronos_careers", "ttcportals", "browser_static", "static_html", "custom", "pinpoint", "brassring", "startup_jobs", "builtin_jobs"}:
        posted = "first_seen_only" if platform in {"kula", "dynamicsats", "paycor", "kronos_careers", "ttcportals", "browser_static", "static_html"} else "unknown"
    else:
        posted = "unknown"
    posted_override = str(source.get("posted_at_quality_override") or "").strip()
    if posted_override in {"official", "updated_proxy", "first_seen_only", "unknown"}:
        posted = posted_override
    return quality, posted


def command_audit_sources(args: argparse.Namespace) -> None:
    require_person_files()
    data = load_json(SOURCES_PATH)
    sources = data.get("sources", [])
    platforms = collections.Counter(source_platform(source) for source in sources)
    qualities = collections.Counter()
    posted_qualities = collections.Counter()
    for source in sources:
        quality, posted = source_quality(source)
        qualities[quality] += 1
        posted_qualities[posted] += 1
        if getattr(args, "write_quality", False):
            source["source_quality"] = quality
            source["posted_at_quality"] = posted
    if getattr(args, "write_quality", False):
        write_json(SOURCES_PATH, data)
    print(f"Sources: {len(sources)} total")
    print(f"Direct/API-backed: {qualities.get('api_good', 0) + qualities.get('api_ok', 0)}")
    print(f"Custom/low-confidence: {platforms.get('custom', 0)}")
    print("By source quality:")
    for quality, count in sorted(qualities.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {quality}: {count}")
    print("By posted_at quality:")
    for quality, count in sorted(posted_qualities.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {quality}: {count}")
    print("By platform:")
    for platform, count in sorted(platforms.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {platform}: {count}")
    custom_sources = [source for source in sources if source_platform(source) == "custom"]
    if custom_sources:
        print("Custom companies:")
        for source in sorted(custom_sources, key=lambda item: str(item.get("company", "")).lower()):
            print(f"  {source.get('company', 'Unknown Company')}: {source.get('url', '')}")
    if getattr(args, "write_quality", False):
        print(f"Wrote source_quality and posted_at_quality to {SOURCES_PATH}.")


def command_find_jobs(args: argparse.Namespace) -> None:
    require_person_files()
    track = load_track(getattr(args, "track", None))
    track_resume = path_from_track(track, "resume_file") if track else None
    sources = load_json(SOURCES_PATH).get("sources", [])
    new_count = 0
    review_count = 0
    for source in sources:
        for candidate in find_links_for_source(source):
            if track.get("id"):
                candidate["target_track"] = track["id"]
                candidate["matched_tracks"] = [track["id"]]
                candidate["resume_file"] = str(track_resume or "")
            app, created = upsert_application(candidate)
            if created:
                new_count += 1
            if app.get("status") == "found":
                review_count += 1
    print(f"Found {new_count} new jobs. {review_count} jobs are ready for scoring.")


def source_report_base(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": source.get("company", ""),
        "platform": source_platform(source),
        "url": source.get("url", ""),
        "status": "started",
        "result_status": "",
        "started_at": now_utc_iso(),
        "finished_at": "",
        "duration_seconds": 0,
        "candidates_returned": 0,
        "stats": empty_discovery_stats(),
        "warnings": "",
        "error": "",
        "health": "",
        "failure_category": "",
        "attempts": [],
        "retry_attempts": 0,
    }


def source_retry_timeout_seconds(timeout_seconds: float | None, retry_timeout_seconds: float | None) -> float | None:
    if retry_timeout_seconds and retry_timeout_seconds > 0:
        if timeout_seconds and timeout_seconds > 0:
            return max(timeout_seconds, retry_timeout_seconds)
        return retry_timeout_seconds
    if timeout_seconds and timeout_seconds > 0:
        return timeout_seconds * 2
    return timeout_seconds


def discover_source_candidates_with_retries(
    source_index: int,
    args: argparse.Namespace,
    timeout_seconds: float | None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    max_retries = max(0, int(getattr(args, "source_retries", 1) or 0))
    retry_timeout = source_retry_timeout_seconds(
        timeout_seconds,
        float(getattr(args, "source_retry_timeout_seconds", 0) or 0),
    )
    attempts: list[dict[str, Any]] = []

    for attempt_index in range(max_retries + 1):
        attempt_number = attempt_index + 1
        attempt_timeout = timeout_seconds if attempt_number == 1 else retry_timeout
        attempt_started = time.time()
        attempt_report = {
            "attempt": attempt_number,
            "timeout_seconds": attempt_timeout if attempt_timeout is not None else 0,
            "started_at": now_utc_iso(),
            "finished_at": "",
            "duration_seconds": 0,
            "status": "started",
            "error": "",
        }
        try:
            candidates, warnings = source_candidates_subprocess(source_index, args, attempt_timeout)
        except Exception as error:  # noqa: BLE001 - retry/finalize per source, not per error type.
            attempt_report["status"] = "failed"
            attempt_report["error"] = str(error)
            attempt_report["finished_at"] = now_utc_iso()
            attempt_report["duration_seconds"] = round(time.time() - attempt_started, 2)
            attempts.append(attempt_report)
            if attempt_index >= max_retries:
                raise SourceDiscoveryFailed(str(error), attempts) from error
            continue

        attempt_report["status"] = "success"
        attempt_report["finished_at"] = now_utc_iso()
        attempt_report["duration_seconds"] = round(time.time() - attempt_started, 2)
        attempts.append(attempt_report)
        return candidates, warnings, attempts

    raise SourceDiscoveryFailed("source discovery failed without recording an attempt", attempts)


def source_candidates_subprocess(
    source_index: int,
    args: argparse.Namespace,
    timeout_seconds: float | None,
) -> tuple[list[dict[str, Any]], str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--person",
        getattr(args, "person", PERSON),
        "discover-source-candidates",
        "--source-index",
        str(source_index),
    ]
    for track_id in getattr(args, "tracks", None) or []:
        command.extend(["--union-track", str(track_id)])
    if not getattr(args, "tracks", None) and getattr(args, "track", None):
        command.extend(["--track", str(args.track)])
    command.append("--payload-file-output")

    timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise SourceTimeout(f"source subprocess exceeded {timeout_seconds:g}s") from error

    if completed.returncode != 0:
        output = "\n".join(part for part in [completed.stderr.strip(), completed.stdout.strip()] if part)
        raise RuntimeError(output or f"source subprocess exited with {completed.returncode}")

    payload_line = ""
    for line in reversed(completed.stdout.splitlines()):
        if line.strip().startswith("{"):
            payload_line = line.strip()
            break
    if not payload_line:
        raise RuntimeError((completed.stderr or completed.stdout or "source subprocess produced no JSON").strip())
    try:
        payload = json.loads(payload_line)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"source subprocess produced invalid JSON: {error}") from error
    payload_path = str(payload.get("payload_path") or "").strip()
    if payload_path:
        try:
            payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"source subprocess payload_path could not be read: {error}") from error
        finally:
            try:
                Path(payload_path).unlink()
            except OSError:
                pass

    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        raise RuntimeError("source subprocess returned non-list candidates")
    warnings = "\n".join(part for part in [payload.get("warnings", ""), completed.stderr.strip()] if str(part).strip())
    return candidates, warnings


def command_discover_source_candidates(args: argparse.Namespace) -> None:
    sources = load_json(SOURCES_PATH).get("sources", [])
    try:
        source = sources[int(args.source_index)]
    except (IndexError, ValueError) as error:
        raise SystemExit(f"Invalid source index: {args.source_index}") from error
    union_tracks = [str(track_id).strip() for track_id in (getattr(args, "union_track", None) or []) if str(track_id).strip()]
    if union_tracks:
        for track_id in union_tracks:
            load_track(track_id)
        source = source_for_tracks(source, union_tracks)
    else:
        track = load_track(getattr(args, "track", None))
        source = source_for_track(source, str(track.get("id", "")).strip())
    warning_buffer = io.StringIO()
    with contextlib.redirect_stderr(warning_buffer):
        candidates = discover_source_jobs(source)
    payload = {
        "company": source.get("company", ""),
        "platform": source_platform(source),
        "url": source.get("url", ""),
        "warnings": warning_buffer.getvalue().strip(),
        "candidates": candidates,
    }
    if getattr(args, "payload_file_output", False):
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="job-search-source-",
            suffix=".json",
            delete=False,
        ) as file:
            json.dump(payload, file, ensure_ascii=False, default=str)
            file.write("\n")
            payload_path = file.name
        print(json.dumps({"payload_path": payload_path}, ensure_ascii=False))
        return
    print(json.dumps(payload, ensure_ascii=False, default=str))


def command_discover_jobs(args: argparse.Namespace) -> None:
    require_person_files()
    track_ids = [
        str(track_id).strip()
        for track_id in (getattr(args, "tracks", None) or [])
        if str(track_id).strip()
    ]
    profiles = {track_id: profile_for_track(track_id) for track_id in track_ids}
    profile = profile_for_track(None) if track_ids else profile_for_track(getattr(args, "track", None))
    all_sources = load_json(SOURCES_PATH).get("sources", [])
    source_company_filters = {item.lower() for item in (getattr(args, "source_company", None) or [])}
    source_pairs = [
        (index, source)
        for index, source in enumerate(all_sources)
        if getattr(args, "include_inactive_sources", False) or source_is_active(source)
    ]
    if source_company_filters:
        source_pairs = [
            (index, source)
            for index, source in source_pairs
            if str(source.get("company", "")).lower() in source_company_filters
        ]
    seen = load_seen_jobs()
    cutoff = discovery_cutoff(args)
    stats = empty_discovery_stats()
    failed_sources = 0
    retried_sources = 0
    retry_recovered_sources = 0
    failed_after_retries = 0
    current_seen_at = now_utc_iso()
    quiet = bool(getattr(args, "quiet", False))
    deferred_score_queue: list[dict[str, Any]] | None = [] if track_ids and bool(args.score) else None
    report = {
        "run_id": discovery_run_id(current_seen_at),
        "started_at": current_seen_at,
        "finished_at": "",
        "track": "all" if track_ids else (getattr(args, "track", None) or ""),
        "tracks": track_ids,
        "cutoff": cutoff.replace(microsecond=0).isoformat(),
        "source_company_filters": sorted(source_company_filters),
        "include_unknown_posted_date": bool(args.include_unknown_posted_date),
        "include_maybe_backlog": bool(getattr(args, "include_maybe_backlog", False)),
        "maybe_old_posted_date": bool(getattr(args, "maybe_old_posted_date", False)),
        "include_inactive_sources": bool(getattr(args, "include_inactive_sources", False)),
        "no_role_filter": bool(args.no_role_filter),
        "score": bool(args.score),
        "score_maybe_limit": int(getattr(args, "score_maybe_limit", 3) or 0),
        "max_maybe_scores": (
            int(getattr(args, "max_maybe_scores", 20) or 0)
            if deferred_score_queue is not None
            else None
        ),
        "score_workers": (
            int(getattr(args, "score_workers", 4) or 1)
            if deferred_score_queue is not None
            else None
        ),
        "quiet": quiet,
        "totals": {
            "sources_planned": len(source_pairs),
            "sources_attempted": 0,
            "failed_sources": 0,
            "retried_sources": 0,
            "retry_recovered_sources": 0,
            "failed_after_retries": 0,
            **empty_discovery_stats(),
        },
        "sources": [],
    }

    track_id = profile.get("_track", {}).get("id")
    timeout_seconds = float(getattr(args, "source_timeout_seconds", 45) or 0)
    worker_count = max(1, int(getattr(args, "workers", 1) or 1))
    report["workers"] = worker_count
    work_items = []
    for ordinal, (source_index, source) in enumerate(source_pairs, 1):
        source = source_for_tracks(source, track_ids) if track_ids else source_for_track(source, track_id)
        work_items.append(
            {
                "ordinal": ordinal,
                "source_index": source_index,
                "source": source,
                "source_started": time.time(),
                "source_report": source_report_base(source),
            }
        )
        if not quiet:
            print(
                f"[{ordinal}/{len(source_pairs)}] {source.get('company', 'Unknown Company')} "
                f"({source_platform(source)}) ...",
                flush=True,
            )
        report["totals"]["sources_attempted"] += 1

    def fetch_source(work_item: dict[str, Any]) -> dict[str, Any]:
        try:
            candidates, warnings, attempts = discover_source_candidates_with_retries(
                int(work_item["source_index"]),
                args,
                timeout_seconds,
            )
            return {
                "ok": True,
                "candidates": candidates,
                "warnings": warnings,
                "attempts": attempts,
                "finished_at": now_utc_iso(),
                "duration_seconds": round(time.time() - float(work_item["source_started"]), 2),
            }
        except Exception as error:  # noqa: BLE001 - one source should not stop the run.
            return {
                "ok": False,
                "error": error,
                "finished_at": now_utc_iso(),
                "duration_seconds": round(time.time() - float(work_item["source_started"]), 2),
            }

    fetch_results: dict[int, dict[str, Any]] = {}
    if worker_count == 1 or len(work_items) <= 1:
        for work_item in work_items:
            fetch_results[int(work_item["ordinal"])] = fetch_source(work_item)
    else:
        max_workers = min(worker_count, len(work_items))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_work_item = {executor.submit(fetch_source, work_item): work_item for work_item in work_items}
            for future in concurrent.futures.as_completed(future_to_work_item):
                work_item = future_to_work_item[future]
                fetch_results[int(work_item["ordinal"])] = future.result()

    for work_item in work_items:
        ordinal = int(work_item["ordinal"])
        source = work_item["source"]
        source_report = work_item["source_report"]
        fetch_result = fetch_results[ordinal]
        try:
            if not fetch_result.get("ok"):
                error = fetch_result.get("error")
                if isinstance(error, BaseException):
                    raise error
                raise RuntimeError(str(error))
            candidates = fetch_result["candidates"]
            warnings = str(fetch_result.get("warnings", ""))
            attempts = fetch_result["attempts"]
            source_report["attempts"] = attempts
            source_report["retry_attempts"] = max(0, len(attempts) - 1)
            source_report["warnings"] = warnings.strip()
        except SourceDiscoveryFailed as error:
            failed_sources += 1
            source_report["attempts"] = error.attempts
            source_report["retry_attempts"] = max(0, len(error.attempts) - 1)
            if source_report["retry_attempts"]:
                retried_sources += 1
                failed_after_retries += 1
            source_report["status"] = "failed_after_retries" if source_report["retry_attempts"] else "failed"
            source_report["error"] = str(error)
            source_report["finished_at"] = str(fetch_result.get("finished_at") or now_utc_iso())
            source_report["duration_seconds"] = fetch_result.get("duration_seconds", 0)
            annotate_source_health(source_report, source)
            report["sources"].append(source_report)
            if not quiet:
                print(
                    f"    failed after {source_report['duration_seconds']}s: {error}",
                    file=sys.stderr,
                    flush=True,
                )
            continue
        except Exception as error:  # noqa: BLE001 - one source should not stop the run.
            failed_sources += 1
            source_report["status"] = "failed"
            source_report["error"] = str(error)
            source_report["finished_at"] = str(fetch_result.get("finished_at") or now_utc_iso())
            source_report["duration_seconds"] = fetch_result.get("duration_seconds", 0)
            annotate_source_health(source_report, source)
            report["sources"].append(source_report)
            if not quiet:
                print(
                    f"    failed after {source_report['duration_seconds']}s: {error}",
                    file=sys.stderr,
                    flush=True,
                )
            continue

        effective_cutoff = source_discovery_cutoff(source, cutoff)
        source_report["cutoff"] = effective_cutoff.replace(microsecond=0).isoformat() if effective_cutoff else ""
        if track_ids:
            source_stats = process_discovered_candidates_all_tracks(
                candidates,
                args,
                profiles,
                seen,
                effective_cutoff,
                current_seen_at,
                score_queue=deferred_score_queue,
            )
        else:
            source_stats = process_discovered_candidates(
                candidates,
                args,
                profile,
                seen,
                effective_cutoff,
                current_seen_at,
            )
        source_report["candidates_returned"] = len(candidates)
        source_report["stats"] = source_stats
        result_status = discovery_source_status(len(candidates), source_stats, str(source_report["warnings"]))
        source_report["result_status"] = result_status
        if source_report["retry_attempts"]:
            retried_sources += 1
            retry_recovered_sources += 1
            source_report["status"] = "retry_success"
        else:
            source_report["status"] = result_status
        source_report["finished_at"] = str(fetch_result.get("finished_at") or now_utc_iso())
        source_report["duration_seconds"] = fetch_result.get("duration_seconds", 0)
        annotate_source_health(source_report, source)
        report["sources"].append(source_report)
        add_stats(stats, source_stats)
        status_detail = source_report["status"]
        if source_report["result_status"] and source_report["status"] != source_report["result_status"]:
            status_detail = f"{source_report['status']} ({source_report['result_status']})"
        if not quiet:
            print(
                f"    {status_detail}: candidates={len(candidates)} "
                f"added={source_stats['added']} existing={source_stats['existing']} "
                f"maybe={source_stats['maybe_backlog']} maybe_scored={source_stats['maybe_scored']} "
                f"old={source_stats['skipped_old']} unknown_date={source_stats['skipped_unknown_date']} "
                f"title={source_stats['skipped_title']} location={source_stats['skipped_location']} "
                f"attempts={len(source_report['attempts']) or 1} ({source_report['duration_seconds']}s)",
                flush=True,
            )

    if deferred_score_queue is not None:
        scoring_summary = execute_discovery_score_queue(deferred_score_queue, args)
        stats["maybe_scored"] += scoring_summary["maybe_scored"]
        stats["scoring_failed"] += scoring_summary["scoring_failed"]
        report["scoring"] = scoring_summary
        if not quiet:
            print(
                "Scoring batch complete. "
                f"Queued tasks: {scoring_summary['queued_tasks']}. "
                f"Selected tasks: {scoring_summary['selected_tasks']}. "
                f"Unique jobs: {scoring_summary['unique_apps']}. "
                f"Maybe selected: {scoring_summary['maybe_selected']}/{scoring_summary['maybe_candidates']}. "
                f"Failures: {scoring_summary['scoring_failed']}.",
                flush=True,
            )

    save_seen_jobs(seen)
    report["finished_at"] = now_utc_iso()
    failed_sources = sum(1 for source in report["sources"] if source.get("status") in {"failed", "failed_after_retries"})
    retried_sources = sum(1 for source in report["sources"] if int(source.get("retry_attempts") or 0) > 0)
    retry_recovered_sources = sum(1 for source in report["sources"] if source.get("status") == "retry_success")
    failed_after_retries = sum(1 for source in report["sources"] if source.get("status") == "failed_after_retries")
    report["totals"]["failed_sources"] = failed_sources
    report["totals"]["retried_sources"] = retried_sources
    report["totals"]["retry_recovered_sources"] = retry_recovered_sources
    report["totals"]["failed_after_retries"] = failed_after_retries
    for key, value in stats.items():
        report["totals"][key] = value
    report_path = write_discovery_run_report(report)
    print(
        ("All-track discovery complete. " if track_ids else "Discovery complete. ")
        +
        f"Cutoff: {cutoff.replace(microsecond=0).isoformat()}. "
        f"Discovered: {stats['discovered']}. Added: {stats['added']}. Existing: {stats['existing']}. "
        f"Maybe backlog: {stats['maybe_backlog']}. Maybe scored: {stats['maybe_scored']}. "
        f"Skipped old: {stats['skipped_old']}. Skipped unknown posted_at: {stats['skipped_unknown_date']}. "
        f"Skipped title: {stats['skipped_title']}. Skipped location: {stats['skipped_location']}. "
        f"Scoring failed: {stats['scoring_failed']}. Failed sources: {failed_sources}. "
        f"Retried sources: {retried_sources}. Retry recovered: {retry_recovered_sources}. "
        f"Failed after retries: {failed_after_retries}."
    )
    print(f"Discovery run report: {report_path}")


def command_discover_all(args: argparse.Namespace) -> None:
    args.track = None
    args.tracks = list(getattr(args, "tracks", None) or DEFAULT_DISCOVER_ALL_TRACKS)
    command_discover_jobs(args)


def application_rescore_datetime(app: dict[str, Any]) -> tuple[dt.datetime | None, str]:
    candidates: list[tuple[dt.datetime, str]] = []
    for field in ("posted_at", "first_seen", "date_found"):
        parsed = parse_datetime(app.get(field))
        if parsed:
            candidates.append((parsed, field))
    if not candidates:
        return None, ""
    return max(candidates, key=lambda item: item[0])


def select_rescore_backlog_applications(
    applications: list[dict[str, Any]],
    cutoff: dt.datetime,
    statuses: set[str],
    limit: int = 0,
) -> list[tuple[dict[str, Any], dt.datetime, str]]:
    selected: list[tuple[dict[str, Any], dt.datetime, str]] = []
    for app in applications:
        if str(app.get("date_applied") or "").strip():
            continue
        if str(app.get("status") or "") not in statuses:
            continue
        reference_at, reference_field = application_rescore_datetime(app)
        if reference_at is None or reference_at < cutoff:
            continue
        selected.append((app, reference_at, reference_field))
    selected.sort(key=lambda item: (item[1], str(item[0].get("id") or "")), reverse=True)
    if limit > 0:
        return selected[:limit]
    return selected


def rescore_tracks_for_application(
    app: dict[str, Any],
    requested_tracks: list[str],
    all_tracks: bool,
) -> list[str | None]:
    if all_tracks:
        return list(DEFAULT_DISCOVER_ALL_TRACKS)
    if requested_tracks:
        return list(dict.fromkeys(requested_tracks))

    tracks: list[str] = []
    tracks.extend(str(item) for item in app.get("matched_tracks", []) if str(item).strip())
    tracks.extend(
        str(item)
        for item in track_evaluations_with_legacy(app)
        if str(item).strip() and str(item) != "default"
    )
    target_track = str(app.get("target_track") or "").strip()
    if target_track:
        tracks.append(target_track)
    deduplicated = list(dict.fromkeys(tracks))
    return deduplicated or [None]


def command_rescore_backlog(args: argparse.Namespace) -> None:
    require_person_files()
    if args.since_hours is None and args.since_days is None:
        args.since_days = 30
    cutoff = discovery_cutoff(args)
    statuses = set(args.status or DEFAULT_RESCORE_BACKLOG_STATUSES)
    requested_tracks = list(args.tracks or [])
    if args.all_tracks and requested_tracks:
        raise SystemExit("Use either --all-tracks or --track, not both.")
    tracks_to_validate = DEFAULT_DISCOVER_ALL_TRACKS if args.all_tracks else requested_tracks
    for track_id in tracks_to_validate:
        load_track(track_id)

    tracker = load_tracker()
    selected = select_rescore_backlog_applications(
        tracker.get("applications", []),
        cutoff,
        statuses,
        max(0, int(args.limit or 0)),
    )
    score_queue: list[dict[str, Any]] = []
    selected_rows: list[tuple[dict[str, Any], dt.datetime, str, list[str | None]]] = []
    for app, reference_at, reference_field in selected:
        tracks = rescore_tracks_for_application(
            app,
            requested_tracks,
            bool(args.all_tracks),
        )
        selected_rows.append((app, reference_at, reference_field, tracks))
        for track_id in tracks:
            score_queue.append(
                {
                    "app_id": app["id"],
                    "track": track_id,
                    "maybe": False,
                    "priority": reference_at.timestamp(),
                    "posted_timestamp": reference_at.timestamp(),
                }
            )

    if args.dry_run:
        print(
            "Backlog rescore dry run. "
            f"Cutoff: {cutoff.replace(microsecond=0).isoformat()}. "
            f"Statuses: {', '.join(sorted(statuses))}. "
            f"Jobs: {len(selected_rows)}. Track evaluations: {len(score_queue)}."
        )
        if not args.quiet:
            for app, reference_at, reference_field, tracks in selected_rows:
                track_text = ", ".join(track or "default" for track in tracks)
                print(
                    f"- {app.get('company', 'Unknown')} - {app.get('role', '')} "
                    f"[{reference_field}={reference_at.date().isoformat()}] "
                    f"tracks={track_text} id={app.get('id', '')}"
                )
        return

    args.max_maybe_scores = 0
    args.preserve_notes = True
    summary = execute_discovery_score_queue(score_queue, args)
    print(
        "Backlog rescore complete. "
        f"Cutoff: {cutoff.replace(microsecond=0).isoformat()}. "
        f"Jobs: {len(selected_rows)}. "
        f"Track evaluations: {summary['selected_tasks']}. "
        f"Failures: {summary['scoring_failed']}."
    )


def command_discover_web_jobs(args: argparse.Namespace) -> None:
    require_person_files()
    if args.since_hours is None and args.since_days is None:
        args.since_days = 7
    profile = profile_for_track(getattr(args, "track", None))
    seen = load_seen_jobs()
    cutoff = discovery_cutoff(args)
    current_seen_at = now_utc_iso()
    queries = build_web_discovery_queries(args)[: args.max_queries]
    urls: dict[str, str] = {}
    failed_queries = 0
    candidates: dict[str, dict[str, Any]] = {}

    rate_limited = False
    for index, query in enumerate(queries):
        wait_between_search_queries(args, index)
        try:
            for url in web_search_urls(query, args):
                normalized = normalize_job_url(url)
                if detect_platform(normalized) in {"greenhouse", "lever", "ashby"}:
                    urls.setdefault(normalized, query)
        except SearchRateLimited as error:
            failed_queries += 1
            rate_limited = True
            print(f"Search provider rate limited; stopping remaining queries: {error}", file=sys.stderr)
            break
        except Exception as error:  # noqa: BLE001
            failed_queries += 1
            print(f"Search query failed: {query}: {error}", file=sys.stderr)

    for url, query in urls.items():
        candidate = ats_candidate_from_url(url)
        if not candidate:
            continue
        candidate["source_query"] = query
        candidate["notes"] = candidate.get("notes", "")
        candidates[candidate["url"]] = candidate

    added_sources = update_sources_from_candidates(list(candidates.values())) if args.update_sources else 0
    stats = process_discovered_candidates(list(candidates.values()), args, profile, seen, cutoff, current_seen_at)
    save_seen_jobs(seen)
    print(
        "Web discovery complete. "
        f"Provider: {args.provider}. Queries: {len(queries)}. Failed queries: {failed_queries}. "
        f"Rate limited: {rate_limited}. "
        f"Search URLs: {len(urls)}. ATS jobs parsed: {len(candidates)}. Added sources: {added_sources}. "
        f"Cutoff: {cutoff.replace(microsecond=0).isoformat()}. "
        f"Added: {stats['added']}. Existing: {stats['existing']}. "
        f"Skipped old: {stats['skipped_old']}. Skipped unknown posted_at: {stats['skipped_unknown_date']}. "
        f"Skipped title: {stats['skipped_title']}. Skipped location: {stats['skipped_location']}. "
        f"Scoring failed: {stats['scoring_failed']}."
    )


def command_discover_watchlist_jobs(args: argparse.Namespace) -> None:
    require_person_files()
    if args.since_hours is None and args.since_days is None:
        args.since_days = 7
    profile = profile_for_track(getattr(args, "track", None))
    seen = load_seen_jobs()
    cutoff = discovery_cutoff(args)
    current_seen_at = now_utc_iso()
    query_items = build_watchlist_queries(args)
    urls: dict[str, dict[str, str]] = {}
    failed_queries = 0
    candidates: dict[str, dict[str, Any]] = {}

    if not query_items:
        raise SystemExit(f"No active companies found in {WATCHLIST_PATH}.")

    rate_limited = False
    for index, item in enumerate(query_items):
        wait_between_search_queries(args, index)
        query = item["query"]
        try:
            for url in web_search_urls(query, args):
                normalized = normalize_job_url(url)
                if normalized:
                    urls.setdefault(normalized, item)
        except SearchRateLimited as error:
            failed_queries += 1
            rate_limited = True
            print(f"Search provider rate limited; stopping remaining watchlist queries: {error}", file=sys.stderr)
            break
        except Exception as error:  # noqa: BLE001
            failed_queries += 1
            print(f"Watchlist search query failed: {query}: {error}", file=sys.stderr)

    for url, item in urls.items():
        candidate = candidate_from_watchlist_url(
            url,
            item["company"],
            item["query"],
            current_seen_at,
            args.use_search_seen_date,
        )
        if not candidate:
            continue
        candidates[candidate["url"]] = candidate

    stats = process_discovered_candidates(list(candidates.values()), args, profile, seen, cutoff, current_seen_at)
    save_seen_jobs(seen)
    print(
        "Watchlist discovery complete. "
        f"Provider: {args.provider}. Queries: {len(query_items)}. Failed queries: {failed_queries}. "
        f"Rate limited: {rate_limited}. "
        f"Search URLs: {len(urls)}. Jobs parsed: {len(candidates)}. "
        f"Cutoff: {cutoff.replace(microsecond=0).isoformat()}. "
        f"Added: {stats['added']}. Existing: {stats['existing']}. "
        f"Skipped old: {stats['skipped_old']}. Skipped unknown posted_at: {stats['skipped_unknown_date']}. "
        f"Skipped title: {stats['skipped_title']}. Skipped location: {stats['skipped_location']}. "
        f"Scoring failed: {stats['scoring_failed']}."
    )


def command_add_url(args: argparse.Namespace) -> None:
    track = load_track(getattr(args, "track", None))
    candidate = {
        "company": args.company or "Unknown Company",
        "role": args.role or infer_role_from_url(args.url),
        "url": args.url,
        "platform": args.platform or detect_platform(args.url),
        "location": args.location or "",
        "notes": args.notes or "",
        "target_track": track.get("id", ""),
        "matched_tracks": [track["id"]] if track.get("id") else [],
        "resume_file": str(path_from_track(track, "resume_file") or "") if track else "",
    }
    app, created = upsert_application(candidate)
    state = "created" if created else "already existed"
    print(f"{state}: {app['id']} {app['company']} - {app['role']}")


def command_score_job(args: argparse.Namespace) -> None:
    app = get_application(args.id)
    requested_track = getattr(args, "track", None) or app.get("target_track")
    profile = profile_for_track(requested_track)
    track = profile.get("_track", {})
    track_id = track_evaluation_key(track.get("id") or requested_track)
    matched_track_id = "" if track_id == "default" else track_id
    matched_tracks = merge_unique(app.get("matched_tracks", []), [matched_track_id] if matched_track_id else [])
    track_resume = path_from_track(track, "resume_file") if track else None
    resume_file = str(track_resume or app.get("resume_file") or "")
    jd_text = cached_job_text_for_scoring(app, args.jd_file)
    output_dir = app_output_dir(app)
    output_dir.mkdir(parents=True, exist_ok=True)
    jd_path = output_dir / "jd.md"
    canonical_report_path = output_dir / "score_report.md"
    report_path = output_dir / f"score_report.{slugify(track_id)}.md"
    jd_path.write_text(jd_text + "\n", encoding="utf-8")
    fetch_failure_reason = job_text_fetch_failure_reason(jd_text)
    evaluations = track_evaluations_with_legacy(app)
    if fetch_failure_reason:
        report = render_fetch_failure_report(app, fetch_failure_reason)
        report_path.write_text(report, encoding="utf-8")
        evaluations[track_id] = {
            "track_id": track_id,
            "fit_score": "",
            "ats_score": "",
            "status": "needs_retry",
            "eligible": False,
            "experience_bucket": "",
            "experience_requirements": [],
            "dealbreakers": [],
            "action_items": [
                "Job description fetch failed; retry scoring after the ATS recovers or with a manual JD file."
            ],
            "matched_keywords": [],
            "resume_keyword_matches": [],
            "missing_keywords": [],
            "resume_file": resume_file,
            "score_report_path": str(report_path),
            "evaluated_at": now_utc_iso(),
            "failure_reason": fetch_failure_reason,
        }
        failure_note = f"fetch_failed: {fetch_failure_reason}"
        existing_notes = str(app.get("notes") or "").strip()
        notes = existing_notes
        if failure_note not in existing_notes:
            notes = failure_note if not existing_notes else f"{existing_notes}\n{failure_note}"
        best_track_id, best_evaluation = best_track_evaluation(evaluations)
        if best_track_id and best_evaluation:
            selected_report_value = str(best_evaluation.get("score_report_path") or "").strip()
            selected_report_path = Path(selected_report_value) if selected_report_value else None
            if selected_report_path and selected_report_path.is_file() and selected_report_path != canonical_report_path:
                canonical_report_path.write_text(
                    selected_report_path.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
            elif not canonical_report_path.exists():
                selected_app = dict(app)
                selected_app["target_track"] = "" if best_track_id == "default" else best_track_id
                canonical_report_path.write_text(
                    render_score_report(selected_app, best_evaluation),
                    encoding="utf-8",
                )
            selected_status = str(best_evaluation.get("status") or "needs_review")
            if app.get("status") in {"applied", "prepared"}:
                selected_status = str(app["status"])
            updates = {
                "track_evaluations": evaluations,
                "target_track": "" if best_track_id == "default" else best_track_id,
                "matched_tracks": matched_tracks,
                "jd_path": str(jd_path),
                "score_report_path": str(canonical_report_path),
                "fit_score": best_evaluation.get("fit_score", ""),
                "ats_score": best_evaluation.get("ats_score", ""),
                "experience_bucket": best_evaluation.get("experience_bucket", ""),
                "experience_requirements": list(best_evaluation.get("experience_requirements") or []),
                "location_bucket": best_evaluation.get("location_bucket", app.get("location_bucket", "")),
                "status": selected_status,
                "dealbreakers": list(best_evaluation.get("dealbreakers") or []),
                "action_items": list(best_evaluation.get("action_items") or []),
                "matched_keywords": list(best_evaluation.get("matched_keywords") or []),
                "resume_keyword_matches": list(best_evaluation.get("resume_keyword_matches") or []),
                "missing_keywords": list(best_evaluation.get("missing_keywords") or []),
                "resume_file": best_evaluation.get("resume_file", ""),
                "notes": notes,
                "last_scored_at": now_utc_iso(),
            }
        else:
            canonical_report_path.write_text(report, encoding="utf-8")
            updates = {
                "fit_score": "",
                "ats_score": "",
                "location_bucket": location_preference_bucket(app.get("location", ""), profile),
                "status": "needs_retry",
                "dealbreakers": [],
                "action_items": [
                    "Job description fetch failed; retry scoring after the ATS recovers or with a manual JD file."
                ],
                "matched_keywords": [],
                "resume_keyword_matches": [],
                "missing_keywords": [],
                "jd_path": str(jd_path),
                "score_report_path": str(canonical_report_path),
                "target_track": app.get("target_track", ""),
                "matched_tracks": matched_tracks,
                "resume_file": resume_file,
                "notes": notes,
                "track_evaluations": evaluations,
                "last_scored_at": now_utc_iso(),
            }
        update_application(app["id"], updates)
        if not bool(getattr(args, "quiet", False)):
            print(report)
        return

    score_app = dict(app)
    score_app["target_track"] = "" if track_id == "default" else track_id
    score = score_text(score_app, jd_text, profile)
    report = render_score_report(score_app, score)
    report_path.write_text(report, encoding="utf-8")
    evaluations[track_id] = build_track_evaluation(track_id, score, resume_file, report_path)
    best_track_id, best_evaluation = best_track_evaluation(evaluations)
    if not best_track_id or not best_evaluation:
        raise RuntimeError(f"No usable track evaluation produced for {app['id']}")
    selected_report_value = str(best_evaluation.get("score_report_path") or "").strip()
    selected_report_path = Path(selected_report_value) if selected_report_value else None
    if selected_report_path and selected_report_path.is_file():
        canonical_report_path.write_text(
            selected_report_path.read_text(encoding="utf-8", errors="replace"),
            encoding="utf-8",
        )
    else:
        selected_app = dict(app)
        selected_app["target_track"] = "" if best_track_id == "default" else best_track_id
        canonical_report_path.write_text(
            render_score_report(selected_app, best_evaluation),
            encoding="utf-8",
        )
    selected_status = str(best_evaluation.get("status") or "needs_review")
    if app.get("status") in {"applied", "prepared"}:
        selected_status = str(app["status"])
    score_notes = "; ".join(
        list(best_evaluation.get("dealbreakers") or [])
        or list(best_evaluation.get("action_items") or [])
    )
    existing_notes = str(app.get("notes") or "")
    notes = existing_notes if bool(getattr(args, "preserve_notes", False)) and existing_notes else score_notes
    update_application(
        app["id"],
        {
            "fit_score": best_evaluation.get("fit_score", ""),
            "ats_score": best_evaluation.get("ats_score", ""),
            "experience_bucket": best_evaluation.get("experience_bucket", ""),
            "experience_requirements": list(best_evaluation.get("experience_requirements") or []),
            "location_bucket": best_evaluation.get("location_bucket", app.get("location_bucket", "")),
            "status": selected_status,
            "dealbreakers": list(best_evaluation.get("dealbreakers") or []),
            "action_items": list(best_evaluation.get("action_items") or []),
            "matched_keywords": list(best_evaluation.get("matched_keywords") or []),
            "resume_keyword_matches": list(best_evaluation.get("resume_keyword_matches") or []),
            "missing_keywords": list(best_evaluation.get("missing_keywords") or []),
            "jd_path": str(jd_path),
            "score_report_path": str(canonical_report_path),
            "target_track": "" if best_track_id == "default" else best_track_id,
            "matched_tracks": matched_tracks,
            "resume_file": best_evaluation.get("resume_file", ""),
            "notes": notes,
            "track_evaluations": evaluations,
            "last_scored_at": now_utc_iso(),
        },
    )
    if not bool(getattr(args, "quiet", False)):
        print(report)


def job_text_fetch_failure_reason(jd_text: str) -> str:
    text = re.sub(r"\s+", " ", str(jd_text or "")).strip()
    if not text:
        return "empty job description"
    lower = text.lower()
    if lower.startswith("unable to fetch job description"):
        return text[:240]
    if re.fullmatch(r"internal server error\.?\s*(?:\(id:\s*[^)]*\))?", text, flags=re.I):
        return "job board returned Internal Server Error"
    if len(text) <= 300:
        transient_markers = [
            "workday is currently unavailable",
            "javascript is disabled",
            "verify that you're not a robot",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "temporarily unavailable",
        ]
        if any(marker in lower for marker in transient_markers):
            return text[:240]
    return ""


def render_fetch_failure_report(app: dict[str, Any], reason: str) -> str:
    return textwrap.dedent(
        f"""\
        # Score Report: {app.get('role', '')} at {app.get('company', '')}

        - URL: {app.get('url', '')}
        - Platform: {app.get('platform', '')}
        - Status: needs_retry

        ## Fetch Failure

        {reason}

        ## Action Items

        - Retry scoring after the ATS recovers, or run `score-job --jd-file` with a manually saved JD.
        - Do not use this record for ranking until a real job description is available.
        """
    )


def render_score_report(app: dict[str, Any], score: dict[str, Any]) -> str:
    missing = ", ".join(score["missing_keywords"]) or "None"
    matched = ", ".join(score["matched_keywords"]) or "None"
    experience_requirements = ", ".join(score.get("experience_requirements", [])) or "None"
    dealbreakers = "\n".join(f"- {item}" for item in score["dealbreakers"]) or "- None"
    actions = "\n".join(f"- {item}" for item in score["action_items"]) or "- None"
    return textwrap.dedent(
        f"""\
        # Score Report: {app.get('role')} at {app.get('company')}

        - URL: {app.get('url')}
        - Platform: {app.get('platform')}
        - Track: {app.get('target_track') or 'default'}
        - Fit score: {score['fit_score']}/10
        - ATS score: {score['ats_score']}/100
        - Status: {score['status']}
        - Location bucket: {score.get('location_bucket', 'maybe')}
        - Experience bucket: {score.get('experience_bucket', 'unknown')}
        - Experience requirements: {experience_requirements}

        ## Matched Keywords

        {matched}

        ## Missing Resume Keywords

        {missing}

        ## Dealbreakers

        {dealbreakers}

        ## Action Items

        {actions}
        """
    )


def command_prepare_application(args: argparse.Namespace) -> None:
    app = get_application(args.id)
    if app.get("dealbreakers"):
        raise SystemExit("This job has dealbreakers. Override by editing tracker status before preparing.")

    profile = profile_for_track(getattr(args, "track", None) or app.get("target_track"))
    track = profile.get("_track", {})
    output_dir = app_output_dir(app)
    output_dir.mkdir(parents=True, exist_ok=True)

    jd_text = ""
    jd_path = app.get("jd_path")
    if jd_path and Path(jd_path).exists():
        jd_text = Path(jd_path).read_text(encoding="utf-8", errors="replace")
    else:
        jd_text = read_job_text(app)
        (output_dir / "jd.md").write_text(jd_text + "\n", encoding="utf-8")

    resume_master = master_resume_path(profile).read_text(encoding="utf-8")
    missing = app.get("missing_keywords", [])
    resume_path = output_dir / "resume_tailored.md"
    resume_path.write_text(render_tailored_resume(resume_master, app, jd_text, missing), encoding="utf-8")

    cover_template = template_path("cover_letter.md").read_text(encoding="utf-8")
    cover_path = output_dir / "cover_letter.md"
    cover_path.write_text(render_cover_letter(cover_template, app, profile, jd_text), encoding="utf-8")

    screening_template = template_path("screening_answers.md").read_text(encoding="utf-8")
    screening_path = output_dir / "screening_answers.md"
    screening_path.write_text(render_screening_answers(screening_template, app, profile), encoding="utf-8")

    action_items = ["Review materials, then run fill-form. Final submit must be manual."]
    extra_updates: dict[str, Any] = {}
    if app.get("platform") == "microsoft_jobs":
        extra_updates["portal_mode"] = "manual"
        action_items = [
            "Microsoft Careers uses a shared candidate portal. Open the original job URL and verify the role/job number before applying.",
            "Do not continue if the portal shows a different Microsoft role or an old application draft.",
            "Final submit must be manual.",
        ]

    update_application(
        app["id"],
        {
            "status": app.get("status") if app.get("status") == "applied" else "prepared",
            "resume_path": str(resume_path),
            "resume_file": str(path_from_track(track, "resume_file") or app.get("resume_file", "")),
            "target_track": track.get("id", app.get("target_track", "")),
            "cover_letter_path": str(cover_path),
            "screening_answers_path": str(screening_path),
            "action_items": action_items,
            **extra_updates,
        },
    )
    print(f"Prepared application materials in {output_dir}")


def render_tailored_resume(master: str, app: dict[str, Any], jd_text: str, missing: list[str]) -> str:
    keywords = sorted(set(keyword_matches(jd_text, TECH_KEYWORDS)))
    keyword_line = ", ".join(keywords[:16]) or "Review JD manually"
    missing_line = ", ".join(missing[:16]) if missing else "None"
    note = textwrap.dedent(
        f"""\
        <!--
        Tailoring notes for {app.get('company')} - {app.get('role')}:
        - Emphasize truthful experience matching: {keyword_line}
        - Missing JD keywords not found in resume: {missing_line}
        - Do not add skills, employers, projects, metrics, education, or authorization claims that are not true.
        -->

        """
    )
    return note + master


def rank_keywords_for_text(keywords: list[Any], text: str, limit: int = 6) -> list[str]:
    lower = text.lower()
    ranked: list[str] = []
    for keyword in keywords:
        value = str(keyword).strip()
        if not value:
            continue
        if re.search(rf"\b{re.escape(value.lower())}\b", lower):
            ranked.append(value)
    return merge_unique([], ranked)[:limit]


def display_keywords(keywords: list[str]) -> str:
    display_names = {
        "aws": "AWS",
        "gcp": "GCP",
        "azure": "Azure",
        "docker": "Docker",
        "github actions": "GitHub Actions",
        "pytest": "pytest",
        "python": "Python",
        "ci": "CI",
        "ci/cd": "CI/CD",
        "qa": "QA",
        "sdet": "SDET",
        "api": "API",
        "llm": "LLM",
        "rag": "RAG",
        "ai": "AI",
        "artificial intelligence": "Artificial Intelligence",
        "machine learning": "Machine Learning",
        "genai": "GenAI",
        "agentic ai": "Agentic AI",
        "multi-agent": "Multi-Agent",
        "fastapi": "FastAPI",
        "sqs": "SQS",
        "lambda": "Lambda",
        "step functions": "Step Functions",
        "dynamodb": "DynamoDB",
        "s3": "S3",
        "cloudfront": "CloudFront",
    }
    return ", ".join(display_names.get(keyword.lower(), keyword) for keyword in keywords)


def cover_letter_context(app: dict[str, Any], profile: dict[str, Any], jd_text: str) -> dict[str, str]:
    track_id = profile.get("_track", {}).get("id") or app.get("target_track") or ""
    matched = app.get("matched_keywords", [])
    target_keywords = profile.get("targets", {}).get("keywords", [])
    track_keywords = profile.get("_track", {}).get("scoring_keywords", [])
    relevant = rank_keywords_for_text(matched + track_keywords + target_keywords, jd_text, 6)
    if not relevant:
        relevant = [str(item) for item in target_keywords[:5]]

    role = f"{app.get('role', '')} {jd_text}".lower()
    if track_id == "qa_engineer":
        if re.search(r"security|cloud|terraform|kubernetes", role):
            hook = "the role connects quality engineering with cloud automation, reliability, and practical security-focused engineering work"
            project = (
                "my QA work at Youmigo validating auth-sensitive API flows, building pytest/FastAPI TestClient regression coverage, "
                "and using GitHub Actions CI"
            )
            focus = "reliable automated validation, clear defect isolation, and maintainable cloud-facing workflows"
        elif re.search(r"automation|sdet|software development engineer in test|test automation", role):
            hook = "the role emphasizes test automation, regression coverage, and reliable engineering workflows"
            project = (
                "my Youmigo QA work using Postman, pytest, FastAPI TestClient, mocked services, and GitHub Actions CI to validate "
                "checkout, ticketing, webhook, refund, and host-management flows"
            )
            focus = "maintainable test automation, accurate defect isolation, and reliable releases"
        elif re.search(r"mobile|ios|android", role):
            hook = "the role emphasizes mobile quality, user-facing reliability, and careful validation of real application behavior"
            project = (
                "my Youmigo QA work reproducing iOS UX issues, validating Firebase Analytics funnels, and tracing frontend state, "
                "API responses, logs, and persistence behavior"
            )
            focus = "high-quality mobile releases and practical regression coverage"
        else:
            hook = "the role emphasizes test automation, regression coverage, and product quality for real users"
            project = (
                "my Youmigo QA work using Postman, pytest, FastAPI TestClient, mocked services, and GitHub Actions CI to validate "
                "checkout, ticketing, webhook, refund, and host-management flows"
            )
            focus = "strong regression coverage, accurate bug reproduction, and reliable releases"
    elif track_id == "fde_ai_engineer":
        if re.search(r"forward deployed|fde|customer|solutions engineer|field engineer|implementation|professional services", role):
            hook = (
                "the role combines hands-on AI engineering with customer-facing problem solving, fast iteration, "
                "and production rollout in ambiguous environments"
            )
            project = (
                "my Youmigo work building a production AWS-based multi-agent content ingestion pipeline with LLM extraction, "
                "SQS/Lambda/Step Functions orchestration, DynamoDB deduplication, and reliability controls for messy real-world sources"
            )
            focus = "turning ambiguous customer or product needs into reliable GenAI workflows that can be deployed, debugged, and improved"
        elif re.search(r"genai|generative ai|llm|agent|rag|retrieval", role):
            hook = (
                "the role aligns with practical GenAI systems, retrieval quality, evaluation, and production-oriented AI engineering"
            )
            project = (
                "my multi-modal RAG project with hybrid retrieval, RRF fusion, Qdrant, reranking, answer synthesis, and LLM/rule-based evaluation, "
                "alongside Youmigo's LLM-powered ingestion workflow"
            )
            focus = "shipping measurable AI systems that balance quality, cost, latency, and reliability"
        else:
            hook = (
                "the role fits my interest in applied AI engineering, rapid prototyping, customer-oriented debugging, and production delivery"
            )
            project = (
                "my Youmigo engineering work across LLM workflows, AWS deployment, backend APIs, frontend integration, and production debugging"
            )
            focus = "building practical AI products that solve real user and business problems"
    else:
        if re.search(r"backend|api|distributed|platform|infrastructure|cloud|aws", role):
            hook = "the role aligns with backend systems, cloud infrastructure, and production reliability work"
            project = (
                "my Youmigo work building AWS-based ingestion, ticketing, and image delivery systems with FastAPI, Lambda, SQS, "
                "Step Functions, DynamoDB, S3, and CloudFront"
            )
            focus = "scalable backend systems and reliable software for users"
        elif re.search(r"ai|machine learning|llm|rag|retrieval", role):
            hook = "the role aligns with practical AI systems, retrieval quality, and production-oriented software engineering"
            project = (
                "my multi-modal RAG project and Youmigo LLM-powered ingestion work, including retrieval evaluation, "
                "LLM workflows, and production constraints"
            )
            focus = "practical AI features that are reliable, measurable, and useful to users"
        else:
            hook = "the role aligns with practical software engineering, production constraints, and user-facing impact"
            project = (
                "my Youmigo engineering work across backend services, automation, mobile integration, and cloud delivery"
            )
            focus = "shipping reliable software for users"

    return {
        "hook": hook,
        "skills": ", ".join(relevant),
        "project": project,
        "focus": focus,
    }


def render_cover_letter(template: str, app: dict[str, Any], profile: dict[str, Any], jd_text: str = "") -> str:
    personal = profile.get("personal", {})
    links = profile.get("links", {})
    context = cover_letter_context(app, profile, jd_text)
    replacements = {
        "[Your Name]": personal.get("name", ""),
        "[Your Email]": personal.get("email", ""),
        "[Your Phone]": personal.get("phone", ""),
        "[Your Location]": personal.get("location", ""),
        "[Your LinkedIn]": links.get("linkedin", ""),
        "[Your Website]": links.get("website", ""),
        "[Your GitHub]": links.get("github", ""),
        "[Date]": today(),
        "[Company]": app.get("company", ""),
        "[Role]": app.get("role", ""),
        "[Company Hook]": context["hook"],
        "[Relevant Skills]": display_keywords(context["skills"].split(", ")) if context["skills"] else "",
        "[Relevant Project]": context["project"],
        "[Role Focus]": context["focus"],
    }
    result = template
    for old, new in replacements.items():
        result = result.replace(old, str(new))
    return result


def render_screening_answers(template: str, app: dict[str, Any], profile: dict[str, Any]) -> str:
    defaults = profile.get("application_defaults", {})
    track_id = profile.get("_track", {}).get("id") or app.get("target_track") or ""
    result = template.replace("[Company]", app.get("company", "the company"))
    result = result.replace("[role need]", app.get("role", "the role"))
    result = result.replace("[Role]", app.get("role", "the role"))
    result = result.replace("[skills]", ", ".join(profile.get("targets", {}).get("keywords", [])[:5]) or "relevant skills")
    result = result.replace("[relevant project/skill]", "my production GenAI and backend engineering work")
    if track_id == "fde_ai_engineer":
        result += textwrap.dedent(
            """

            ## Production Agentic AI / GenAI Application

            I designed and built a production-grade multi-agent content ingestion pipeline for Youmigo, an event discovery app. The business use case was to replace manual event curation with an automated system that could discover local events from aggregator pages and original source pages, extract structured event data, deduplicate results, and prepare reliable content for the mobile app.

            The system used LLM-based extraction and validation prompts, a two-hop crawler, AWS Lambda workers, SQS queues, Step Functions orchestration, DynamoDB for deduplication/state tracking, and S3/CloudFront for media assets. It continuously processed thousands of events per batch, supported configurable LLM token budgets, and reduced duplicate processing by about 40%.

            The biggest technical challenge was making the pipeline reliable across messy and inconsistent websites while controlling LLM cost. I solved this with source-page fallback, structured validation, idempotent processing, retry paths, and monitoring around failed extraction cases.

            ## AWS GenAI Deployment

            I operated the Youmigo GenAI ingestion workflow on AWS. It was primarily a batch/asynchronous deployment rather than real-time inference: crawler and extraction jobs were placed onto SQS, processed by Lambda workers, coordinated with Step Functions, and persisted in DynamoDB and S3. FastAPI services then exposed curated results to the application layer, while CloudFront served optimized media assets.

            For reliability, I used idempotent job handling, DynamoDB state tracking, retry paths, and failure logging so failed pages or LLM extraction errors could be replayed without corrupting production data. The main scaling and cost challenge was processing large event batches while controlling LLM token usage and avoiding duplicate work. Configurable token budgets, queue-based concurrency, and deduplication reduced duplicate processing by about 40% and kept the workflow stable across messy source websites.
            """
        )
    result += "\n\n## Profile Defaults\n\n"
    for key, value in defaults.items():
        result += f"- {key}: {value}\n"
    return result


def command_application_backlog(args: argparse.Namespace) -> None:
    bucket = getattr(args, "bucket", "") or ""
    requested_track = str(getattr(args, "track", "") or "").strip()
    if requested_track:
        load_track(requested_track)
    if not args.status and bucket == "retry":
        statuses = {"needs_retry"}
    elif not args.status and bucket == "skipped":
        statuses = {"skipped"}
    elif not args.status and bucket == "rejected":
        statuses = {"found", "prepared", "needs_review", "scored", "skipped"}
    else:
        statuses = set(args.status or ["prepared", "needs_review", "scored"])
    tracker = load_tracker()
    apps: list[dict[str, Any]] = []
    for app in tracker.get("applications", []):
        if str(app.get("date_applied") or "").strip():
            continue
        candidate_app = app
        if requested_track:
            evaluation = track_evaluations_with_legacy(app).get(requested_track)
            if not evaluation:
                continue
            candidate_app = dict(app)
            for field in [
                "fit_score",
                "ats_score",
                "status",
                "experience_bucket",
                "experience_requirements",
                "dealbreakers",
                "action_items",
                "matched_keywords",
                "resume_keyword_matches",
                "missing_keywords",
                "resume_file",
                "score_report_path",
            ]:
                if field in evaluation:
                    candidate_app[field] = evaluation[field]
            candidate_app["target_track"] = requested_track
        location_bucket = application_location_bucket(candidate_app)
        rejection_reasons = recommendation_rejection_reasons(candidate_app)
        if bucket == "maybe" and not is_maybe_backlog_app(app) and location_bucket != "maybe":
            continue
        if bucket == "maybe" and rejection_reasons:
            continue
        if bucket == "retry" and candidate_app.get("status") != "needs_retry":
            continue
        if bucket == "skipped" and candidate_app.get("status") != "skipped":
            continue
        if bucket == "rejected" and not rejection_reasons:
            continue
        if bucket in {"priority", "relocation"} and (
            app.get("review_bucket") == "maybe"
            or app.get("discovery_bucket") == "maybe_backlog"
            or candidate_app.get("status") in {"needs_retry", "skipped"}
        ):
            continue
        if bucket == "priority" and location_bucket != "preferred":
            continue
        if bucket == "relocation" and location_bucket != "relocation":
            continue
        if candidate_app.get("status") not in statuses:
            continue
        min_fit = 0.0 if bucket in {"maybe", "retry", "skipped", "rejected"} else args.min_fit
        if numeric_score(candidate_app.get("fit_score")) < min_fit:
            continue
        filter_text = application_filter_text(candidate_app)
        if args.preferred_locations and location_bucket != "preferred":
            continue
        if args.exclude_years and has_year_requirement(filter_text, args.exclude_years):
            continue
        if bucket in {"priority", "relocation"} and experience_requirement_bucket(candidate_app) == "3_plus":
            continue
        if bucket in {"priority", "relocation"} and has_seniority_title_signal(candidate_app):
            continue
        if bucket in {"priority", "relocation"} and has_phd_signal(candidate_app):
            continue
        if args.hide_intern and re.search(r"\bintern(ship)?\b", filter_text):
            continue
        apps.append(candidate_app)

    apps.sort(key=recommendation_sort_key, reverse=True)
    if bucket in {"priority", "relocation"}:
        apps = cap_recommendations_by_company(apps, getattr(args, "company_limit", DEFAULT_RECOMMENDATION_COMPANY_LIMIT))
    if args.limit and args.limit > 0:
        apps = apps[: args.limit]

    rows = [
        "| Fit | ATS | Experience | Status | Review Bucket | Location Bucket | Company | Role | Location | Posted/Found | ID |",
        "| ---: | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for app in apps:
        posted = app.get("posted_at") or app.get("date_found") or ""
        rows.append(
            "| "
            + " | ".join(
                [
                    str(app.get("fit_score", "")),
                    str(app.get("ats_score", "")),
                    experience_requirement_bucket(app),
                    str(app.get("status", "")),
                    str(app.get("review_bucket") or app.get("discovery_bucket") or "").replace("|", "\\|"),
                    application_location_bucket(app),
                    str(app.get("company", "")).replace("|", "\\|"),
                    str(app.get("role", "")).replace("|", "\\|"),
                    str(app.get("location", "")).replace("|", "\\|"),
                    str(posted).replace("|", "\\|"),
                    str(app.get("id", "")).replace("|", "\\|"),
                ]
            )
            + " |"
        )

    output = textwrap.dedent(
        f"""\
        # Application Backlog

        - min_fit: {args.min_fit}
        - statuses: {", ".join(sorted(statuses))}
        - preferred_locations: {bool(args.preferred_locations)}
        - exclude_years: {args.exclude_years or ""}
        - hide_intern: {bool(args.hide_intern)}
        - bucket: {getattr(args, "bucket", "") or "default"}
        - track: {requested_track or "all"}
        - company_limit: {getattr(args, "company_limit", DEFAULT_RECOMMENDATION_COMPANY_LIMIT) if bucket in {"priority", "relocation"} else "none"}
        - count: {len(apps)}

        """
    ) + "\n".join(rows) + "\n"
    if args.output:
        path = Path(args.output).expanduser()
        if not path.is_absolute():
            path = PRIVATE_BASE_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        print(f"Wrote backlog report to {path}")
    else:
        print(output)


def application_filter_text(app: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ["company", "role", "location", "notes", "posted_at", "date_found", "experience_bucket"]:
        parts.append(str(app.get(field, "")))
    for field in ["action_items", "dealbreakers", "matched_keywords", "missing_keywords", "experience_requirements"]:
        value = app.get(field, [])
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def matches_preferred_location(text: str) -> bool:
    return location_preference_bucket(text, {}) == "preferred"


def application_location_bucket(app: dict[str, Any]) -> str:
    stored = str(app.get("location_bucket") or "").strip().lower()
    if stored in {"preferred", "relocation", "maybe", "rejected"}:
        return stored
    return location_preference_bucket(str(app.get("location") or ""), {})


def location_bucket_priority_score(bucket: str) -> int:
    return {"preferred": 3, "relocation": 2, "maybe": 1, "rejected": 0}.get(bucket, 1)


def recommendation_rejection_reasons(app: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    filter_text = application_filter_text(app)
    if application_location_bucket(app) == "rejected":
        reasons.append("outside_us")
    if has_seniority_title_signal(app):
        reasons.append("senior_or_level_iii")
    if has_year_requirement(filter_text, 3) or experience_requirement_bucket(app) == "3_plus":
        reasons.append("3_plus_years")
    if has_phd_signal(app):
        reasons.append("phd")
    if re.search(r"\b(?:security clearance|active clearance|secret clearance|top secret|u\.s\. citizen|us citizen)\b", filter_text):
        reasons.append("clearance_or_citizenship")
    if app.get("dealbreakers") and not reasons:
        reasons.append("dealbreaker")
    return reasons


def has_year_requirement(text: str, minimum: int) -> bool:
    required_years = minimum_experience_years(extract_year_requirements(text))
    return bool(required_years and required_years >= minimum)


PROMOTED_MAYBE_MIN_ATS = 70.0
DEFAULT_RECOMMENDATION_COMPANY_LIMIT = 3


def level_two_title_signal(app: dict[str, Any]) -> bool:
    role = str(app.get("role", "") or "").lower()
    role_families = (
        r"sde|software\s+development\s+engineer|software\s+engineer|software\s+developer|"
        r"frontend\s+(?:software\s+)?engineer|backend\s+(?:software\s+)?engineer|full[-\s]?stack\s+(?:software\s+)?engineer|"
        r"machine\s+learning\s+engineer|ml\s+engineer|data\s+engineer|platform\s+engineer|sdet|qa\s+engineer"
    )
    return bool(
        re.search(rf"\b(?:{role_families})\s*(?:ii|2)\b", role)
        or re.search(r"\b(?:sde|se)\s*2\b", role)
        or re.search(r"\bengineer\s*(?:ii|2)\b", role)
    )


def experience_requirement_bucket(app: dict[str, Any]) -> str:
    existing = str(app.get("experience_bucket") or "").strip()
    if existing:
        return existing
    role = str(app.get("role") or "")
    tracker_text = application_filter_text(app)
    filter_text = f"{tracker_text} {recommendation_jd_text(app)}"
    title_and_tracker_text = f"{role} {tracker_text}".lower()
    if re.search(r"\b(new\s+grad|new\s+college|early\s+career|entry[-\s]?level|apprentice(ship)?|junior)\b", title_and_tracker_text):
        return "new_grad"
    requirements = extract_year_requirements(filter_text)
    if requirements:
        minimum_years = minimum_experience_years(requirements)
        minimum_requirements = [
            requirement
            for requirement in requirements
            if int(requirement.get("min") or 0) == minimum_years
        ]
        if minimum_years >= 3:
            return "3_plus"
        if minimum_years == 2 and any(
            req.get("max") and int(req.get("max") or 0) >= 3
            for req in minimum_requirements
        ):
            return "2_range"
        if minimum_years == 2 and any(req.get("plus") for req in minimum_requirements):
            return "2_plus"
        if minimum_years <= 1 and any(
            req.get("max") and int(req.get("max") or 0) <= 3
            for req in minimum_requirements
        ):
            return "1_2"
        if minimum_years <= 2:
            return "1_2"
    month_requirements = extract_month_requirements(filter_text)
    if month_requirements:
        min_months = min(month_requirements)
        if min_months <= 12:
            return "0_1"
        if min_months <= 24:
            return "1_2"
    if level_two_title_signal(app):
        return "2_plus"
    return "unknown"


def recommendation_jd_text(app: dict[str, Any], max_chars: int = 60000) -> str:
    path_value = str(app.get("jd_path") or "").strip()
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = PRIVATE_BASE_ROOT / path
    try:
        if not path.exists() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""


def experience_bucket_priority_score(bucket: str) -> int:
    return {
        "new_grad": 5,
        "0_1": 5,
        "1_2": 4,
        "unknown": 3,
        "2_plus": 2,
        "2_range": 1,
        "3_plus": 0,
    }.get(bucket, 3)


def has_seniority_title_signal(app: dict[str, Any]) -> bool:
    role = str(app.get("role", "") or "").lower()
    if re.search(r"\b(senior|sr\.?|staff|principal|distinguished|lead|leader|manager|director|head|vp|chief|cto)\b", role):
        return True
    level_three_plus = r"(?:iii|iv|v|3|4|5)"
    role_families = (
        r"sde|software\s+development\s+engineer|software\s+engineer|software\s+developer|"
        r"frontend\s+(?:software\s+)?engineer|backend\s+(?:software\s+)?engineer|full[-\s]?stack\s+(?:software\s+)?engineer|"
        r"machine\s+learning\s+engineer|ml\s+engineer|data\s+engineer|platform\s+engineer|sdet|qa\s+engineer"
    )
    if re.search(rf"\b(?:{role_families})\s*{level_three_plus}\b", role):
        return True
    if re.search(rf"\bengineer\s*{level_three_plus}\b", role):
        return True
    return False


def has_phd_signal(app: dict[str, Any]) -> bool:
    role = str(app.get("role", "") or "").lower()
    return bool(re.search(r"\bph\.?\s*d\b|\bdoctorate\b", role))


def recommendation_risk_penalty(app: dict[str, Any]) -> int:
    filter_text = application_filter_text(app)
    penalty = 0
    if has_seniority_title_signal(app):
        penalty += 2
    if has_phd_signal(app):
        penalty += 2
    if has_year_requirement(filter_text, 3):
        penalty += 2
    bucket = experience_requirement_bucket(app)
    if bucket == "2_plus":
        penalty += 1
    elif bucket == "2_range":
        penalty += 2
    if re.search(r"\bintern(ship)?\b", filter_text):
        penalty += 2
    return penalty


def recommendation_sort_key(app: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -recommendation_risk_penalty(app),
        experience_bucket_priority_score(experience_requirement_bucket(app)),
        location_bucket_priority_score(application_location_bucket(app)),
        numeric_score(app.get("fit_score")),
        numeric_score(app.get("ats_score")),
        str(app.get("posted_at") or app.get("date_found") or ""),
    )


def cap_recommendations_by_company(apps: list[dict[str, Any]], company_limit: int) -> list[dict[str, Any]]:
    if company_limit <= 0:
        return apps
    counts: dict[str, int] = {}
    capped: list[dict[str, Any]] = []
    for app in apps:
        company_key = re.sub(r"\s+", " ", str(app.get("company") or "unknown").strip().lower())
        count = counts.get(company_key, 0)
        if count >= company_limit:
            continue
        counts[company_key] = count + 1
        capped.append(app)
    return capped


def is_maybe_backlog_app(app: dict[str, Any]) -> bool:
    return app.get("review_bucket") == "maybe" or app.get("discovery_bucket") == "maybe_backlog"


def daily_review_app_rows(
    apps: list[dict[str, Any]],
    bucket: str,
    min_fit: float,
    limit: int,
    company_limit: int = DEFAULT_RECOMMENDATION_COMPANY_LIMIT,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for app in apps:
        if str(app.get("date_applied") or "").strip():
            continue
        location_bucket = application_location_bucket(app)
        rejection_reasons = recommendation_rejection_reasons(app)
        if bucket in {"priority", "relocation"}:
            if app.get("status") not in {"prepared", "needs_review", "scored"}:
                continue
            if is_maybe_backlog_app(app):
                continue
            if app.get("dealbreakers"):
                continue
            if numeric_score(app.get("fit_score")) < min_fit:
                continue
            expected_location_bucket = "preferred" if bucket == "priority" else "relocation"
            if location_bucket != expected_location_bucket:
                continue
            filter_text = application_filter_text(app)
            if has_year_requirement(filter_text, 3) or experience_requirement_bucket(app) == "3_plus":
                continue
            if has_seniority_title_signal(app):
                continue
            if has_phd_signal(app):
                continue
            if re.search(r"\bintern(ship)?\b", filter_text):
                continue
        elif bucket == "promoted_maybe":
            if app.get("status") not in {"prepared", "needs_review", "scored"}:
                continue
            if not is_maybe_backlog_app(app):
                continue
            if app.get("dealbreakers"):
                continue
            if rejection_reasons:
                continue
            if numeric_score(app.get("fit_score")) < min_fit:
                continue
            if numeric_score(app.get("ats_score")) < PROMOTED_MAYBE_MIN_ATS:
                continue
            filter_text = application_filter_text(app)
            if has_year_requirement(filter_text, 3) or experience_requirement_bucket(app) == "3_plus":
                continue
            if has_seniority_title_signal(app):
                continue
            if has_phd_signal(app):
                continue
            if re.search(r"\bintern(ship)?\b", filter_text):
                continue
        elif bucket == "maybe":
            if rejection_reasons:
                continue
            if not is_maybe_backlog_app(app) and location_bucket != "maybe":
                continue
        elif bucket == "rejected":
            if app.get("status") not in {"found", "prepared", "needs_review", "scored", "skipped"}:
                continue
            if not rejection_reasons:
                continue
        elif bucket == "retry":
            if app.get("status") != "needs_retry":
                continue
            filter_text = application_filter_text(app)
            if has_year_requirement(filter_text, 3) or experience_requirement_bucket(app) == "3_plus":
                continue
            if has_seniority_title_signal(app):
                continue
            if has_phd_signal(app):
                continue
            if re.search(r"\bintern(ship)?\b", filter_text):
                continue
        else:
            continue
        rows.append(app)
    rows.sort(key=recommendation_sort_key, reverse=True)
    if bucket in {"priority", "relocation", "promoted_maybe"}:
        rows = cap_recommendations_by_company(rows, company_limit)
    return rows[:limit] if limit > 0 else rows


def render_daily_review_app_section(title: str, apps: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not apps:
        lines.extend(["- None", ""])
        return lines
    for index, app in enumerate(apps, 1):
        posted = app.get("posted_at") or app.get("date_found") or ""
        lines.append(
            f"{index}. [{app.get('fit_score', '')}/10 fit, {app.get('ats_score', '')}/100 ATS] "
            f"{app.get('company', '')} - {app.get('role', '')}"
        )
        lines.append(
            f"   - Status: {app.get('status', '')}; review bucket: "
            f"{app.get('review_bucket') or app.get('discovery_bucket') or ''}; "
            f"location bucket: {application_location_bucket(app)}"
        )
        rejection_reasons = recommendation_rejection_reasons(app)
        if rejection_reasons:
            lines.append(f"   - Rejected because: {', '.join(rejection_reasons)}")
        lines.append(f"   - Experience: {experience_requirement_bucket(app)}")
        lines.append(f"   - Location: {app.get('location', '')}; posted/found: {posted}")
        lines.append(f"   - ID: {app.get('id', '')}")
        lines.append(f"   - URL: {app.get('url', '')}")
    lines.append("")
    return lines


def discovery_reports_for_date(date_value: str, latest_only: bool = True) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    if not DISCOVERY_RUNS_DIR.exists():
        return reports
    for path in sorted(DISCOVERY_RUNS_DIR.glob("*.json"), key=lambda item: item.name):
        try:
            report = load_json(path)
        except Exception:  # noqa: BLE001 - skip malformed historical reports.
            continue
        if str(report.get("started_at") or "").startswith(date_value):
            report["_path"] = str(path)
            reports.append(report)
    if latest_only and reports:
        def report_source_count(report: dict[str, Any]) -> int:
            totals = report.get("totals", {}) if isinstance(report.get("totals"), dict) else {}
            attempted = totals.get("sources_attempted", 0)
            try:
                return int(attempted)
            except (TypeError, ValueError):
                return len(report.get("sources", []) if isinstance(report.get("sources"), list) else [])

        max_source_count = max(report_source_count(report) for report in reports)
        broad_threshold = max_source_count * 0.8
        selection_pool = [report for report in reports if report_source_count(report) >= broad_threshold]
        if not selection_pool:
            selection_pool = reports
        return [
            max(
                selection_pool,
                key=lambda report: (
                    str(report.get("started_at") or ""),
                    str(report.get("run_id") or ""),
                    str(report.get("_path") or ""),
                ),
            )
        ]
    return reports


def command_daily_review(args: argparse.Namespace) -> None:
    require_person_files()
    review_date = args.date or today()
    apps = load_tracker().get("applications", [])
    all_reports = bool(getattr(args, "all_reports", False))
    reports = discovery_reports_for_date(review_date, latest_only=not all_reports)
    reports_for_resolution = discovery_reports_for_date(review_date, latest_only=False) if not all_reports else reports
    priority = daily_review_app_rows(apps, "priority", args.min_fit, args.limit, args.company_limit)
    relocation = daily_review_app_rows(apps, "relocation", args.min_fit, args.limit, args.company_limit)
    promoted_maybe = daily_review_app_rows(apps, "promoted_maybe", max(args.min_fit, 9.0), args.limit, args.company_limit)
    promoted_ids = {str(app.get("id", "")) for app in promoted_maybe}
    maybe = daily_review_app_rows(apps, "maybe", 0, args.limit)
    maybe = [app for app in maybe if str(app.get("id", "")) not in promoted_ids]
    rejected = daily_review_app_rows(apps, "rejected", 0, args.limit)
    retry = daily_review_app_rows(apps, "retry", 0, args.limit)
    source_issues: list[dict[str, Any]] = []
    for report in reports:
        for source in report.get("sources", []):
            if source_health_needs_attention(source):
                if not all_reports and source_issue_resolved_later(source, report, reports_for_resolution):
                    continue
                item = dict(source)
                item["_run_id"] = report.get("run_id", "")
                source_issues.append(item)

    lines = [
        f"# Daily Job Review - {review_date}",
        "",
        f"- Discovery reports: {len(reports)}",
        f"- Priority candidates: {len(priority)}",
        f"- Relocation candidates: {len(relocation)}",
        f"- Promoted maybe: {len(promoted_maybe)}",
        f"- Maybe backlog: {len(maybe)}",
        f"- Rejected candidates: {len(rejected)}",
        f"- Retry needed: {len(retry)}",
        f"- Source issues: {len(source_issues)}",
        f"- Company limit: {args.company_limit if args.company_limit > 0 else 'none'}",
        "",
    ]
    lines.extend(render_daily_review_app_section("Priority", priority))
    lines.extend(render_daily_review_app_section("Relocation", relocation))
    lines.extend(render_daily_review_app_section("Promoted Maybe", promoted_maybe))
    lines.extend(render_daily_review_app_section("Maybe Backlog", maybe))
    lines.extend(render_daily_review_app_section("Rejected", rejected))
    lines.extend(render_daily_review_app_section("Retry Needed", retry))
    lines.extend(["## Source Health Issues", ""])
    if not source_issues:
        lines.extend(["- None", ""])
    else:
        selected_issues = source_issues[: args.limit] if args.limit > 0 else source_issues
        for source in selected_issues:
            reason = source.get("error") or source.get("warnings") or ""
            reason = " ".join(str(reason).split())[:180]
            lines.append(
                f"- {source.get('company', '')} ({source.get('platform', '')}) "
                f"run={source.get('_run_id', '')} health={source.get('health', '') or 'unknown'} "
                f"failure={source.get('failure_category', '') or 'unknown'} status={source.get('status', '')}"
                + (f" | {reason}" if reason else "")
            )
        lines.append("")

    if args.output:
        path = Path(args.output).expanduser()
        if not path.is_absolute():
            path = PRIVATE_BASE_ROOT / path
    else:
        path = PRIVATE_BASE_ROOT / "data" / "daily_review" / f"{review_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote daily review to {path}")


def command_notify(args: argparse.Namespace) -> None:
    summary_path = write_notification()
    print(f"Wrote notification summary to {summary_path}")
    if args.send_email or args.send_gmail or args.send_outlook:
        run_email_notify(summary_path, args)


def write_notification() -> Path:
    tracker = load_tracker()
    profile = load_profile()
    NOTIFICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    dated_path = NOTIFICATIONS_DIR / f"{today()}.md"
    latest_path = NOTIFICATIONS_DIR / "latest.md"
    content = render_notification(tracker, profile)
    dated_path.write_text(content, encoding="utf-8")
    latest_path.write_text(content, encoding="utf-8")
    return latest_path


def render_notification(tracker: dict[str, Any], profile: dict[str, Any]) -> str:
    apps = tracker.get("applications", [])
    today_apps = [app for app in apps if app.get("date_found") == today()]
    skipped = [app for app in apps if app.get("status") in {"skipped", "needs_review"}]
    recommended = daily_review_app_rows(apps, "priority", 8.0, 5, DEFAULT_RECOMMENDATION_COMPANY_LIMIT)

    top_jobs = "\n".join(
        f"- [{app.get('fit_score')}/10 fit, {app.get('ats_score')}/100 ATS] "
        f"{app.get('company')} - {app.get('role')} ({app.get('platform')}, {experience_requirement_bucket(app)})\n  {app.get('url')}"
        for app in recommended
    ) or "- No scored recommendations yet."
    action_items = collect_action_items(apps)
    action_text = "\n".join(f"- {item}" for item in action_items) or "- No action items."
    name = profile.get("personal", {}).get("name", "there")
    return (
        f"Subject: Job Search Summary - {today()}\n\n"
        f"Hi {name},\n\n"
        f"Here is your job search summary for {today()}.\n\n"
        f"- New jobs found today: {len(today_apps)}\n"
        f"- Skipped/review jobs: {len(skipped)}\n"
        f"- Recommended applications: {len(recommended)}\n\n"
        "## Top Jobs\n\n"
        f"{top_jobs}\n\n"
        "## Needs Your Attention\n\n"
        f"{action_text}\n\n"
        "## Local Files\n\n"
        f"- Tracker JSON: {APPLICATIONS_JSON}\n"
        f"- Tracker CSV: {APPLICATIONS_CSV}\n"
        f"- Output directory: {OUTPUT_DIR}\n"
    )


def collect_action_items(apps: list[dict[str, Any]]) -> list[str]:
    items = []
    for app in apps:
        for item in app.get("action_items", []):
            items.append(f"{app.get('company')} - {app.get('role')}: {item}")
        if app.get("status") == "prepared" and not app.get("action_items"):
            items.append(f"{app.get('company')} - {app.get('role')}: Review materials and run fill-form.")
        if app.get("status") == "needs_review":
            items.append(f"{app.get('company')} - {app.get('role')}: Needs manual review before preparing.")
    deduped = list(dict.fromkeys(items))
    return deduped[:12]


def run_email_notify(summary_path: Path, args: argparse.Namespace | None = None) -> None:
    provider = "gmail"
    if args and getattr(args, "send_outlook", False):
        provider = "outlook"
    elif args and getattr(args, "send_gmail", False):
        provider = "gmail"
    else:
        profile = load_profile()
        provider = profile.get("notifications", {}).get("provider", "gmail")

    script_name = "outlook_notify.js" if provider == "outlook" else "gmail_notify.js"
    script = ROOT / "scripts" / script_name
    command = ["node", str(script), "--person", PERSON, "--summary", str(summary_path)]
    result = subprocess.run(command, cwd=ROOT, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{provider.title()} notification did not send. Summary remains at {summary_path}")


def command_run(args: argparse.Namespace) -> None:
    command_find_jobs(args)
    tracker = load_tracker()
    for app in tracker.get("applications", []):
        if app.get("status") in {"found", "needs_retry"}:
            try:
                score_args = argparse.Namespace(id=app["id"], jd_file=None, track=getattr(args, "track", None))
                command_score_job(score_args)
            except Exception as error:  # noqa: BLE001
                update_application(app["id"], {"status": "needs_review", "notes": f"Scoring failed: {error}"})
    write_notification()
    if args.send_email or args.send_gmail or args.send_outlook:
        run_email_notify(NOTIFICATIONS_DIR / "latest.md", args)
    print("Run complete.")


def command_migrate_seen_jobs(args: argparse.Namespace) -> None:
    if not SEEN_JOBS_PATH.exists() and (SEEN_JOBS_INDEX_PATH.exists() or SEEN_JOBS_SHARDS_DIR.exists()):
        seen = load_seen_jobs()
        print(f"Seen jobs already use sharded storage: {len(seen.get('jobs', {}))} records.")
        return
    if not SEEN_JOBS_PATH.exists():
        seen = {"jobs": {}, "_seen_jobs_format": "sharded"}
        write_sharded_seen_jobs(seen)
        print(f"Initialized sharded seen jobs storage: {SEEN_JOBS_INDEX_PATH}")
        return

    legacy = load_json(SEEN_JOBS_PATH)
    legacy_jobs = legacy.get("jobs", {})
    if not isinstance(legacy_jobs, dict):
        raise SystemExit(f"Invalid legacy seen jobs file: expected object at jobs in {SEEN_JOBS_PATH}")
    seen = {"jobs": legacy_jobs, "_seen_jobs_format": "sharded"}
    write_sharded_seen_jobs(seen)
    migrated = load_sharded_seen_jobs()
    migrated_count = len(migrated.get("jobs", {}))
    legacy_count = len(legacy_jobs)
    if migrated_count != legacy_count:
        raise SystemExit(f"Migration count mismatch: legacy={legacy_count}, sharded={migrated_count}")
    if not args.keep_legacy:
        SEEN_JOBS_PATH.unlink()
    action = "kept" if args.keep_legacy else "removed"
    print(
        f"Migrated {migrated_count} seen jobs to {SEEN_JOBS_DIR}. "
        f"Legacy file {action}: {SEEN_JOBS_PATH}"
    )


def command_init_person(_args: argparse.Namespace) -> None:
    create_from_template(ROOT / "examples" / "profile.example.json", PROFILE_PATH)
    create_from_template(ROOT / "examples" / "sources.example.json", SOURCES_PATH)
    create_from_template(ROOT / "examples" / "company_watchlist.example.json", WATCHLIST_PATH)
    create_from_template(ROOT / "examples" / "applications.example.json", APPLICATIONS_JSON)
    create_from_template(ROOT / "examples" / "applications.example.csv", APPLICATIONS_CSV)
    save_seen_jobs({"jobs": {}, "_seen_jobs_format": "sharded"})
    create_from_template(ROOT / "examples" / "master_resume.example.md", PERSON_ROOT / "resume" / "master_resume.md")
    create_from_template(ROOT / "examples" / "tracks" / "qa_engineer" / "track.json", TRACKS_DIR / "qa_engineer" / "track.json")
    create_from_template(ROOT / "examples" / "tracks" / "qa_engineer" / "master_resume.md", TRACKS_DIR / "qa_engineer" / "master_resume.md")
    create_from_template(ROOT / "examples" / "tracks" / "fde_ai_engineer" / "track.json", TRACKS_DIR / "fde_ai_engineer" / "track.json")
    create_from_template(ROOT / "examples" / "tracks" / "fde_ai_engineer" / "master_resume.md", TRACKS_DIR / "fde_ai_engineer" / "master_resume.md")
    for template in ["cover_letter.md", "screening_answers.md", "notification_email.md"]:
        create_from_template(ROOT / "templates" / template, PERSON_ROOT / "templates" / template)
    profile = load_json(PROFILE_PATH)
    profile["resume_file"] = str(PERSON_ROOT / "resume" / "master_resume.md")
    write_json(PROFILE_PATH, profile)
    print(f"Initialized person workspace: {PERSON_ROOT}")


def create_from_template(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local ATS job search automation.")
    parser.add_argument("--person", default=os.environ.get("JOB_SEARCH_PERSON", "default"), help="Person partition name.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("init-person", help="Create an isolated profile/resume/tracker partition.")
    find_jobs = subcommands.add_parser("find-jobs", help="Fetch ATS source pages and add job links to the tracker.")
    find_jobs.add_argument("--track", help="Target track for discovered applications.")

    discover = subcommands.add_parser("discover-jobs", help="Discover jobs with ATS APIs and filter by posted date.")
    discover.add_argument("--since-hours", type=float, help="Only add jobs posted within this many hours. Defaults to 24.")
    discover.add_argument("--since-days", type=float, help="Only add jobs posted within this many days.")
    discover.add_argument("--track", help="Target track, such as qa_engineer, mobile_engineer, or backend_sde.")
    discover.add_argument(
        "--include-unknown-posted-date",
        action="store_true",
        help="Add jobs even when the source does not expose a posted date.",
    )
    discover.add_argument(
        "--include-maybe-backlog",
        action="store_true",
        help="Add unknown-date or fuzzy-title candidates as needs_review with review_bucket=maybe. Default keeps old strict behavior.",
    )
    discover.add_argument(
        "--maybe-old-posted-date",
        action="store_true",
        help="With --include-maybe-backlog, keep newly seen candidates whose posted_at is older than the cutoff in the maybe bucket.",
    )
    discover.add_argument(
        "--include-inactive-sources",
        action="store_true",
        help="Also run sources marked active=false. Default skips inactive sources.",
    )
    discover.add_argument("--no-role-filter", action="store_true", help="Add all fresh jobs regardless of title.")
    discover.add_argument("--score", action="store_true", help="Score newly added found jobs after discovery.")
    discover.add_argument(
        "--score-maybe-limit",
        type=int,
        default=3,
        help="With --score, fetch and score at most this many relevant maybe candidates per source. Use 0 to disable.",
    )
    discover.add_argument(
        "--source-timeout-seconds",
        type=float,
        default=45,
        help="Maximum seconds to spend on one source before marking it failed. Use 0 to disable.",
    )
    discover.add_argument(
        "--source-retries",
        type=int,
        default=1,
        help="Retry a failed source this many times before marking it failed_after_retries. Use 0 to disable.",
    )
    discover.add_argument(
        "--source-retry-timeout-seconds",
        type=float,
        default=0,
        help="Timeout for retry attempts. Defaults to twice --source-timeout-seconds.",
    )
    discover.add_argument(
        "--source-company",
        action="append",
        help="Only run discovery for a specific source company. Repeat for multiple companies.",
    )
    discover.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of source fetch workers. Tracker, seen-jobs, scoring, and report writes remain single-threaded.",
    )
    discover.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-source progress and score reports; print only the final discovery summary.",
    )

    discover_all = subcommands.add_parser(
        "discover-all",
        help="Crawl each source once, then route and score jobs across multiple tracks.",
    )
    discover_all.add_argument("--since-hours", type=float, help="Only add jobs posted within this many hours. Defaults to 24.")
    discover_all.add_argument("--since-days", type=float, help="Only add jobs posted within this many days.")
    discover_all.add_argument(
        "--track",
        dest="tracks",
        action="append",
        help="Track to include. Repeat for multiple tracks. Defaults to all configured primary tracks.",
    )
    discover_all.add_argument(
        "--include-unknown-posted-date",
        action="store_true",
        help="Add jobs even when the source does not expose a posted date.",
    )
    discover_all.add_argument(
        "--include-maybe-backlog",
        action="store_true",
        help="Keep fuzzy-title, unknown-date, or unclassified technical candidates in the maybe bucket.",
    )
    discover_all.add_argument(
        "--maybe-old-posted-date",
        action="store_true",
        help="With --include-maybe-backlog, keep newly seen old-post candidates for review.",
    )
    discover_all.add_argument(
        "--include-inactive-sources",
        action="store_true",
        help="Also run sources marked active=false. Default skips inactive sources.",
    )
    discover_all.add_argument("--no-role-filter", action="store_true", help="Route every fresh job to all selected tracks.")
    discover_all.add_argument("--score", action="store_true", help="Score routed jobs against every matched track.")
    discover_all.add_argument(
        "--score-maybe-limit",
        type=int,
        default=3,
        help="Legacy compatibility option. Use --max-maybe-scores to control unified discovery scoring.",
    )
    discover_all.add_argument(
        "--max-maybe-scores",
        type=int,
        default=20,
        help="With --score, score at most this many maybe candidates globally. Use 0 to disable.",
    )
    discover_all.add_argument(
        "--score-workers",
        type=int,
        default=4,
        help="Number of concurrent JD fetch workers before score calculations and tracker writes run serially.",
    )
    discover_all.add_argument(
        "--source-timeout-seconds",
        type=float,
        default=45,
        help="Maximum seconds to spend on one source before marking it failed. Use 0 to disable.",
    )
    discover_all.add_argument(
        "--source-retries",
        type=int,
        default=1,
        help="Retry a failed source this many times before marking it failed_after_retries.",
    )
    discover_all.add_argument(
        "--source-retry-timeout-seconds",
        type=float,
        default=0,
        help="Timeout for retry attempts. Defaults to twice --source-timeout-seconds.",
    )
    discover_all.add_argument(
        "--source-company",
        action="append",
        help="Only run discovery for a specific source company. Repeat for multiple companies.",
    )
    discover_all.add_argument(
        "--workers",
        type=int,
        default=12,
        help="Number of source fetch workers. Classification and tracker writes remain single-threaded.",
    )
    discover_all.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-source progress and score reports; print only the final discovery summary.",
    )

    discover_source = subcommands.add_parser("discover-source-candidates", help=argparse.SUPPRESS)
    discover_source.add_argument("--source-index", required=True)
    discover_source.add_argument("--track", help=argparse.SUPPRESS)
    discover_source.add_argument("--union-track", action="append", help=argparse.SUPPRESS)
    discover_source.add_argument("--payload-file-output", action="store_true", help=argparse.SUPPRESS)

    classify = subcommands.add_parser("classify-sources", help="Detect direct ATS platforms behind configured sources.")
    classify.add_argument("--apply", action="store_true", help="Rewrite detected sources in sources.json.")
    classify.add_argument("--custom-only", action="store_true", help="Only classify sources currently marked custom.")
    classify.add_argument(
        "--source-company",
        action="append",
        help="Only classify a specific source company. Repeat for multiple companies.",
    )

    web_discover = subcommands.add_parser(
        "discover-web-jobs",
        help="Use a search API to find fresh public ATS job URLs, then parse and filter them.",
    )
    web_discover.add_argument("--provider", choices=["serpapi", "bing"], default="serpapi")
    web_discover.add_argument("--since-hours", type=float, help="Only add jobs posted within this many hours.")
    web_discover.add_argument("--since-days", type=float, help="Only add jobs posted within this many days. Defaults to 7.")
    web_discover.add_argument("--results-per-query", type=int, default=10)
    web_discover.add_argument("--pages-per-query", type=int, default=1, help="SerpAPI only: follow this many Google result pages per query.")
    web_discover.add_argument("--max-queries", type=int, default=48)
    web_discover.add_argument("--search-delay-seconds", type=float, default=2.0, help="Delay between search API queries to avoid provider rate limits.")
    web_discover.add_argument("--track", help="Target track, such as qa_engineer, mobile_engineer, or backend_sde.")
    web_discover.add_argument("--role", action="append", help="Role query term. Repeat to add multiple roles.")
    web_discover.add_argument("--location", action="append", help="Location query term. Repeat to add multiple locations.")
    web_discover.add_argument(
        "--include-unknown-posted-date",
        action="store_true",
        help="Add jobs even when the source does not expose a posted date.",
    )
    web_discover.add_argument("--no-role-filter", action="store_true", help="Add all fresh jobs regardless of title.")
    web_discover.add_argument("--update-sources", action="store_true", help="Add newly discovered ATS boards to sources.json.")
    web_discover.add_argument("--score", action="store_true", help="Score newly added found jobs after discovery.")

    watchlist_discover = subcommands.add_parser(
        "discover-watchlist-jobs",
        help="Use company_watchlist.json and a search API to discover jobs on company career sites.",
    )
    watchlist_discover.add_argument(
        "--provider",
        choices=["serpapi", "bing"],
        default=os.environ.get("JOB_SEARCH_WATCHLIST_PROVIDER", "bing"),
    )
    watchlist_discover.add_argument("--since-hours", type=float, help="Only add jobs posted within this many hours.")
    watchlist_discover.add_argument("--since-days", type=float, help="Only add jobs posted within this many days. Defaults to 7.")
    watchlist_discover.add_argument("--results-per-query", type=int, default=10)
    watchlist_discover.add_argument("--pages-per-query", type=int, default=2, help="SerpAPI only: follow this many Google result pages per query.")
    watchlist_discover.add_argument("--max-queries", type=int, default=80)
    watchlist_discover.add_argument("--search-delay-seconds", type=float, default=2.0, help="Delay between search API queries to avoid provider rate limits.")
    watchlist_discover.add_argument("--track", help="Target track, such as qa_engineer, mobile_engineer, or backend_sde.")
    watchlist_discover.add_argument("--role", action="append", help="Role query term. Repeat to add multiple roles.")
    watchlist_discover.add_argument("--location", action="append", help="Location query term. Repeat to add multiple locations.")
    watchlist_discover.add_argument(
        "--include-unknown-posted-date",
        action="store_true",
        help="Add jobs even when neither official posted date nor search-seen fallback is available.",
    )
    watchlist_discover.add_argument(
        "--no-search-seen-date",
        dest="use_search_seen_date",
        action="store_false",
        help="Do not use first search-seen time as a fallback posted_at for self-hosted career pages.",
    )
    watchlist_discover.set_defaults(use_search_seen_date=True)
    watchlist_discover.add_argument("--no-role-filter", action="store_true", help="Add all fresh jobs regardless of title.")
    watchlist_discover.add_argument("--score", action="store_true", help="Score newly added found jobs after discovery.")

    add = subcommands.add_parser("add-url", help="Manually add one job URL.")
    add.add_argument("url")
    add.add_argument("--company")
    add.add_argument("--role")
    add.add_argument("--platform")
    add.add_argument("--location")
    add.add_argument("--notes")
    add.add_argument("--track", help="Target track for this application.")

    score = subcommands.add_parser("score-job", help="Score one job by id or URL.")
    score.add_argument("--id", required=True)
    score.add_argument("--jd-file")
    score.add_argument("--track", help="Override the application target track while scoring.")

    rescore = subcommands.add_parser(
        "rescore-backlog",
        help="Re-score recent unsubmitted tracker jobs without changing daily discovery behavior.",
    )
    rescore.add_argument("--since-hours", type=float, help="Re-score jobs posted or first found within this many hours.")
    rescore.add_argument(
        "--since-days",
        type=float,
        help="Re-score jobs posted or first found within this many days. Defaults to 30.",
    )
    rescore.add_argument(
        "--status",
        action="append",
        help="Application status to include. Repeatable. Defaults to found, needs_review, needs_retry, and scored.",
    )
    rescore.add_argument(
        "--track",
        dest="tracks",
        action="append",
        help="Track to refresh. Repeatable. Defaults to each application's existing matched/evaluated tracks.",
    )
    rescore.add_argument(
        "--all-tracks",
        action="store_true",
        help="Refresh every configured primary track for each selected job.",
    )
    rescore.add_argument("--limit", type=int, default=0, help="Maximum jobs to re-score. Use 0 for no limit.")
    rescore.add_argument(
        "--score-workers",
        type=int,
        default=4,
        help="Number of concurrent JD fetch workers. Tracker writes remain serial.",
    )
    rescore.add_argument("--dry-run", action="store_true", help="Print selected jobs and tracks without changing files.")
    rescore.add_argument("--quiet", action="store_true", help="Suppress per-job dry-run rows.")

    prepare = subcommands.add_parser("prepare-application", help="Generate local application materials.")
    prepare.add_argument("--id", required=True)
    prepare.add_argument("--track", help="Override the application target track while preparing materials.")

    notify = subcommands.add_parser("notify", help="Write and optionally send the run summary.")
    notify.add_argument("--send-email", action="store_true", help="Send using profile.notifications.provider.")
    notify.add_argument("--send-gmail", action="store_true")
    notify.add_argument("--send-outlook", action="store_true")

    run = subcommands.add_parser("run", help="Find jobs, score unscored jobs, and write notification.")
    run.add_argument("--track", help="Target track for found and scored applications.")
    run.add_argument("--send-email", action="store_true", help="Send using profile.notifications.provider.")
    run.add_argument("--send-gmail", action="store_true")
    run.add_argument("--send-outlook", action="store_true")

    audit = subcommands.add_parser("audit-sources", help="Summarize configured sources by platform and confidence.")
    audit.add_argument("--write-quality", action="store_true", help="Write source_quality and posted_at_quality fields to sources.json.")
    discovery_summary = subcommands.add_parser("discovery-summary", help="Summarize a discovery run report.")
    discovery_summary.add_argument("--latest", action="store_true", help="Summarize the most recent discovery run report.")
    discovery_summary.add_argument("--run-id", help="Run id or JSON report path. Defaults to latest.")
    discovery_summary.add_argument("--limit", type=int, default=8, help="Maximum rows to show in each grouped section.")
    source_health = subcommands.add_parser("source-health", help="List source health issues from a discovery run report.")
    source_health.add_argument("--latest", action="store_true", help="Inspect the most recent discovery run report.")
    source_health.add_argument("--run-id", help="Run id or JSON report path. Defaults to latest.")
    source_health.add_argument("--limit", type=int, default=100, help="Maximum source issues to print. Use 0 for no limit.")
    review_hn = subcommands.add_parser("review-hn", help="Review HN Who is Hiring tracker entries and optionally skip bad fits.")
    review_hn.add_argument("--apply", action="store_true", help="Mark obvious skip entries as skipped in the tracker.")
    review_hn.add_argument("--limit", type=int, default=20, help="Maximum rows to print per group.")
    review_hn.add_argument("--status", action="append", help="Only review applications with this status. Repeatable.")
    migrate_seen = subcommands.add_parser(
        "migrate-seen-jobs",
        help="Migrate data/seen_jobs.json to sharded JSONL storage.",
    )
    migrate_seen.add_argument(
        "--keep-legacy",
        action="store_true",
        help="Keep data/seen_jobs.json after writing data/seen_jobs/ shards.",
    )
    backlog = subcommands.add_parser(
        "application-backlog",
        help="List high-fit unsubmitted applications that are prepared, needs_review, or scored.",
    )
    backlog.add_argument("--min-fit", type=float, default=8.0, help="Minimum fit score to include.")
    backlog.add_argument(
        "--status",
        action="append",
        help="Application status to include. Repeatable. Defaults to prepared, needs_review, and scored.",
    )
    backlog.add_argument("--limit", type=int, default=50, help="Maximum rows to print. Use 0 for no limit.")
    backlog.add_argument(
        "--track",
        help="Use one track's independent evaluation instead of the selected top-level score.",
    )
    backlog.add_argument(
        "--preferred-locations",
        action="store_true",
        help="Only include preferred WA or Remote US roles.",
    )
    backlog.add_argument(
        "--exclude-years",
        type=int,
        help="Exclude jobs whose text mentions this many required years or more, e.g. --exclude-years 3.",
    )
    backlog.add_argument("--hide-intern", action="store_true", help="Hide internship/intern roles.")
    backlog.add_argument(
        "--bucket",
        choices=["priority", "relocation", "maybe", "rejected", "retry", "skipped"],
        help="Optional compatibility bucket view. Defaults to the original high-fit backlog behavior.",
    )
    backlog.add_argument(
        "--company-limit",
        type=int,
        default=DEFAULT_RECOMMENDATION_COMPANY_LIMIT,
        help="Maximum recommendations per company for --bucket priority. Use 0 for no cap.",
    )
    backlog.add_argument("--output", help="Optional Markdown output path. Relative paths are under the private repo.")
    daily_review = subcommands.add_parser("daily-review", help="Write a daily review from tracker and discovery reports.")
    daily_review.add_argument("--date", help="Date to review in YYYY-MM-DD. Defaults to today.")
    daily_review.add_argument("--min-fit", type=float, default=8.0, help="Minimum fit score for priority candidates.")
    daily_review.add_argument("--limit", type=int, default=30, help="Maximum rows per section. Use 0 for no limit.")
    daily_review.add_argument(
        "--company-limit",
        type=int,
        default=DEFAULT_RECOMMENDATION_COMPANY_LIMIT,
        help="Maximum priority/promoted maybe recommendations per company. Use 0 for no cap.",
    )
    daily_review.add_argument(
        "--all-reports",
        action="store_true",
        help="Include every discovery report from the date. Default uses the latest report to avoid stale failed runs.",
    )
    daily_review.add_argument("--output", help="Optional Markdown output path. Relative paths are under the private repo.")
    subcommands.add_parser("sync-csv", help="Regenerate applications.csv from applications.json.")
    return parser


def main() -> None:
    os.chdir(ROOT.parent)
    parser = build_parser()
    args = parser.parse_args()
    configure_person(args.person)
    if args.command == "init-person":
        command_init_person(args)
    elif args.command == "find-jobs":
        command_find_jobs(args)
    elif args.command == "discover-jobs":
        command_discover_jobs(args)
    elif args.command == "discover-all":
        command_discover_all(args)
    elif args.command == "discover-source-candidates":
        command_discover_source_candidates(args)
    elif args.command == "classify-sources":
        command_classify_sources(args)
    elif args.command == "audit-sources":
        command_audit_sources(args)
    elif args.command == "discovery-summary":
        command_discovery_summary(args)
    elif args.command == "source-health":
        command_source_health(args)
    elif args.command == "review-hn":
        command_review_hn(args)
    elif args.command == "migrate-seen-jobs":
        command_migrate_seen_jobs(args)
    elif args.command == "application-backlog":
        command_application_backlog(args)
    elif args.command == "daily-review":
        command_daily_review(args)
    elif args.command == "discover-web-jobs":
        command_discover_web_jobs(args)
    elif args.command == "discover-watchlist-jobs":
        command_discover_watchlist_jobs(args)
    elif args.command == "add-url":
        command_add_url(args)
    elif args.command == "score-job":
        command_score_job(args)
    elif args.command == "rescore-backlog":
        command_rescore_backlog(args)
    elif args.command == "prepare-application":
        command_prepare_application(args)
    elif args.command == "notify":
        command_notify(args)
    elif args.command == "run":
        command_run(args)
    elif args.command == "sync-csv":
        sync_csv()
        print(f"Synced {APPLICATIONS_CSV}")
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
