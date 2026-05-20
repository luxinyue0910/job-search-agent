#!/usr/bin/env python3
"""Local, human-in-the-loop job search automation.

The script intentionally keeps external automation conservative:
- Trackers and generated materials are local files.
- Gmail sending is delegated to a separate browser script and only to self.
- ATS form submission is never performed here.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
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
TRACKS_DIR = PRIVATE_BASE_ROOT / "tracks"
OUTPUT_DIR = PRIVATE_BASE_ROOT / "output"
NOTIFICATIONS_DIR = OUTPUT_DIR / "notifications"

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
    "date_found",
    "posted_at",
    "updated_at",
    "first_seen",
    "last_seen",
    "source",
    "source_query",
    "freshness_source",
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

ATS_SEARCH_SITES = [
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
]


class SearchRateLimited(RuntimeError):
    """Raised when a search provider asks us to stop sending requests."""


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
    global PERSON, PERSON_ROOT, PROFILE_PATH, APPLICATIONS_JSON, APPLICATIONS_CSV, SOURCES_PATH, WATCHLIST_PATH, SEEN_JOBS_PATH, TRACKS_DIR, OUTPUT_DIR, NOTIFICATIONS_DIR

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
    TRACKS_DIR = PERSON_ROOT / "tracks"
    OUTPUT_DIR = PERSON_ROOT / "output"
    NOTIFICATIONS_DIR = OUTPUT_DIR / "notifications"


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
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")


def load_seen_jobs() -> dict[str, Any]:
    if not SEEN_JOBS_PATH.exists():
        return {"jobs": {}}
    data = load_json(SEEN_JOBS_PATH)
    data.setdefault("jobs", {})
    return data


def save_seen_jobs(seen: dict[str, Any]) -> None:
    seen["last_updated"] = now_utc_iso()
    write_json(SEEN_JOBS_PATH, seen)


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
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        return "workday"
    if "amazon.jobs" in host:
        return "amazon_jobs"
    if "google.com" in host and "/about/careers/applications" in path:
        return "google_jobs"
    if "careers.google.com" in host:
        return "google_jobs"
    if "metacareers.com" in host:
        return "meta_jobs"
    return "custom"


def make_id(company: str, role: str, url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{slugify(company)}-{slugify(role)}-{digest}"


def fetch_url(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 job-search-workspace/1.0",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 job-search-workspace/1.0",
            "Accept": "application/json,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def fetch_json_post(url: str, payload: dict[str, Any], timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0 job-search-workspace/1.0",
            "Accept": "application/json,*/*;q=0.8",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def fetch_json_with_opener(opener: urllib.request.OpenerDirector, url: str, headers: dict[str, str], timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers=headers)
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
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                parsed = dt.datetime.strptime(raw, fmt)
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
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if not key.lower().startswith("utm_")]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query), fragment="")).rstrip("/")


def discovery_cutoff(args: argparse.Namespace) -> dt.datetime:
    if args.since_hours is not None and args.since_days is not None:
        raise SystemExit("Use either --since-hours or --since-days, not both.")
    if args.since_days is not None:
        delta = dt.timedelta(days=args.since_days)
    else:
        delta = dt.timedelta(hours=args.since_hours if args.since_hours is not None else 24)
    return dt.datetime.now(dt.timezone.utc) - delta


def source_platform(source: dict[str, Any]) -> str:
    return str(source.get("platform") or source.get("type") or detect_platform(source.get("url", ""))).lower()


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
    return parts[0] if parts else None


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
    if "myworkdayjobs.com" not in host and "myworkdaysite.com" not in host:
        return None
    tenant = str(source.get("tenant") or "").strip()
    if not tenant:
        match = re.match(r"([a-zA-Z0-9_-]+)\.wd\d+\.", host)
        if match:
            tenant = match.group(1)
    parts = [part for part in parsed.path.split("/") if part]
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
        candidates.append(
            {
                "company": company,
                "role": job.get("text") or infer_role_from_url(url),
                "url": url,
                "platform": "lever",
                "location": categories.get("location", "") or "",
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
        candidates.append(
            {
                "company": company,
                "role": job.get("title") or infer_role_from_url(url),
                "url": url,
                "platform": "ashby",
                "location": job.get("location", "") or "",
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
    match = re.search(r"(\d+)\+?\s+days?\s+ago", text)
    if match:
        return (now - dt.timedelta(days=int(match.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    match = re.search(r"(\d+)\+?\s+hours?\s+ago", text)
    if match:
        return (now - dt.timedelta(hours=int(match.group(1)))).replace(microsecond=0).isoformat()
    raw = re.sub(r"^posted\s+(on\s+)?", "", raw, flags=re.I).strip()
    return normalize_datetime(raw)


def workday_api_url(host: str, tenant: str, site: str, suffix: str) -> str:
    suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    return f"https://{host}/wday/cxs/{urllib.parse.quote(tenant)}/{urllib.parse.quote(site)}{suffix}"


def workday_human_url(host: str, site: str, external_path: str) -> str:
    if not external_path.startswith("/"):
        external_path = f"/{external_path}"
    return normalize_job_url(f"https://{host}/{urllib.parse.quote(site)}{external_path}")


def discover_workday_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    parts = workday_source_parts(source)
    if not parts:
        return find_links_for_source(source)
    host, tenant, site = parts
    company = source.get("company", "Unknown Company")
    keywords = source.get("keywords") or DEFAULT_WORKDAY_KEYWORDS
    if isinstance(keywords, str):
        keywords = [keywords]
    limit = int(source.get("page_size", 20))
    max_pages = int(source.get("max_pages", 5))
    candidates: dict[str, dict[str, Any]] = {}
    endpoint = workday_api_url(host, tenant, site, "/jobs")

    for keyword in [str(item) for item in keywords if str(item).strip() or len(keywords) == 1]:
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
                url = workday_human_url(host, site, external_path)
                title = job.get("title") or job.get("jobTitle") or infer_role_from_url(url)
                locations = job.get("locationsText") or job.get("locationsDisplayText") or job.get("location") or ""
                posted_at = parse_workday_posted_on(job.get("postedOn") or job.get("postedOnDate"))
                candidates[url] = {
                    "company": company,
                    "role": title,
                    "url": url,
                    "platform": "workday",
                    "location": locations,
                    "posted_at": posted_at,
                    "updated_at": "",
                    "source": source.get("url", ""),
                    "external_job_id": job.get("bulletFields", [""])[0] if isinstance(job.get("bulletFields"), list) else "",
                    "notes": "",
                }
            if len(postings) < limit:
                break
    return list(candidates.values())


def microsoft_pcsx_session() -> tuple[urllib.request.OpenerDirector, dict[str, str]]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    base_headers = {
        "User-Agent": "Mozilla/5.0 job-search-workspace/1.0",
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
    if platform == "workday":
        return discover_workday_jobs(source)
    if platform == "microsoft_jobs":
        return discover_microsoft_jobs(source)
    if platform == "amazon_jobs":
        return discover_amazon_jobs(source)
    if platform == "google_jobs":
        return discover_google_jobs(source)
    if platform == "meta_jobs":
        return discover_meta_jobs(source)
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
    normalized_url = workday_human_url(host, site, external_path)
    return {
        "company": data.get("hiringOrganization", {}).get("name") if isinstance(data.get("hiringOrganization"), dict) else tenant,
        "role": info.get("title") or infer_role_from_url(normalized_url),
        "url": normalized_url,
        "platform": "workday",
        "location": info.get("location", "") or "",
        "posted_at": parse_workday_posted_on(info.get("postedOn") or info.get("startDate")),
        "updated_at": normalize_datetime(info.get("startDate")),
        "source": f"https://{host}/{site}",
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
    return None


def discovery_title_matches(candidate: dict[str, Any], profile: dict[str, Any]) -> bool:
    role = str(candidate.get("role", "")).lower()
    if not role:
        return False
    combined = f"{candidate.get('role', '')} {candidate.get('url', '')}".lower()
    if re.search(r"\b(senior|sr\.?|staff|principal|manager|director|lead|intern|internship)\b", combined):
        return False
    if re.search(r"\b(canada|france|india|united kingdom|uk|london|paris|toronto|vancouver)\b", combined):
        return False
    profile_terms = [
        str(item).lower()
        for item in profile.get("targets", {}).get("roles", []) + profile.get("targets", {}).get("levels", [])
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


def location_allowed(location: str, profile: dict[str, Any]) -> bool:
    value = str(location or "").strip().lower()
    if not value:
        return True
    if re.search(r"\b(remote|us based|us-based)\b", value):
        return True
    if re.fullmatch(r"(united states|usa|u\.s\.|us|united states of america)", value):
        return True

    preferences = profile.get("preferences", {})
    allowed_locations = [
        str(item).lower()
        for item in preferences.get("relocation_allowed_locations", []) + preferences.get("preferred_locations_order", [])
    ]
    if any(item and item in value for item in allowed_locations):
        return True

    allowed_states = {str(item).lower() for item in preferences.get("relocation_allowed_states", [])}
    if "wa" in allowed_states and re.search(r"\b(wa|washington|seattle|bellevue|redmond|kirkland)\b", value):
        return True
    if "ca" in allowed_states and re.search(
        r"\b(ca|california|san francisco|sf|san jose|palo alto|mountain view|sunnyvale|los angeles)\b",
        value,
    ):
        return True
    return False


def empty_discovery_stats() -> dict[str, int]:
    return {
        "discovered": 0,
        "added": 0,
        "existing": 0,
        "skipped_old": 0,
        "skipped_unknown_date": 0,
        "skipped_title": 0,
        "skipped_location": 0,
        "scoring_failed": 0,
    }


def process_discovered_candidates(
    candidates: list[dict[str, Any]],
    args: argparse.Namespace,
    profile: dict[str, Any],
    seen: dict[str, Any],
    cutoff: dt.datetime,
    current_seen_at: str,
) -> dict[str, int]:
    stats = empty_discovery_stats()
    seen_jobs = seen.setdefault("jobs", {})
    track = load_track(getattr(args, "track", None))
    track_id = str(track.get("id", "")).strip()
    track_resume = path_from_track(track, "resume_file") if track else None
    for candidate in candidates:
        stats["discovered"] += 1
        if track_id:
            candidate["target_track"] = track_id
            candidate["matched_tracks"] = [track_id]
            if track_resume:
                candidate["resume_file"] = str(track_resume)
        candidate["url"] = normalize_job_url(candidate["url"])
        key = candidate["url"]
        seen_record = seen_jobs.setdefault(
            key,
            {
                "company": candidate.get("company", ""),
                "role": candidate.get("role", ""),
                "url": key,
                "first_seen": current_seen_at,
            },
        )
        seen_record.update(
            {
                "company": candidate.get("company", seen_record.get("company", "")),
                "role": candidate.get("role", seen_record.get("role", "")),
                "platform": candidate.get("platform", seen_record.get("platform", "")),
                "location": candidate.get("location", seen_record.get("location", "")),
                "posted_at": candidate.get("posted_at", seen_record.get("posted_at", "")),
                "updated_at": candidate.get("updated_at", seen_record.get("updated_at", "")),
                "last_seen": current_seen_at,
                "source": candidate.get("source", seen_record.get("source", "")),
                "source_query": candidate.get("source_query", seen_record.get("source_query", "")),
                "freshness_source": candidate.get("freshness_source", seen_record.get("freshness_source", "")),
                "job_number": candidate.get("job_number", seen_record.get("job_number", "")),
                "external_job_id": candidate.get("external_job_id", seen_record.get("external_job_id", "")),
            }
        )
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
        if not posted_at:
            if not args.include_unknown_posted_date:
                stats["skipped_unknown_date"] += 1
                continue
        elif posted_at < cutoff:
            stats["skipped_old"] += 1
            continue
        if not args.no_role_filter and not discovery_title_matches(candidate, profile):
            stats["skipped_title"] += 1
            continue
        if not location_allowed(candidate.get("location", ""), profile):
            stats["skipped_location"] += 1
            continue

        app, created = upsert_application(candidate)
        if created:
            stats["added"] += 1
        else:
            stats["existing"] += 1
        if args.score and app.get("status") == "found":
            try:
                command_score_job(argparse.Namespace(id=app["id"], jd_file=None))
            except Exception as error:  # noqa: BLE001
                stats["scoring_failed"] += 1
                update_application(app["id"], {"status": "needs_review", "notes": f"Scoring failed: {error}"})
    return stats


def add_stats(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


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
    try:
        raw = fetch_url(base_url)
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"Could not fetch {company} source {base_url}: {error}", file=sys.stderr)
        return []

    links: list[dict[str, str]] = []
    if platform == "greenhouse":
        pattern = r'href=["\']([^"\']*(?:boards\.greenhouse\.io|/jobs/)[^"\']+)["\'][^>]*>(.*?)</a>'
    elif platform == "lever":
        pattern = r'href=["\']([^"\']*(?:jobs\.lever\.co|/[^"\']+/[^"\']+)[^"\']*)["\'][^>]*>(.*?)</a>'
    elif platform == "ashby":
        pattern = r'href=["\']([^"\']*(?:jobs\.ashbyhq\.com|/[^"\']+/[^"\']+)[^"\']*)["\'][^>]*>(.*?)</a>'
    else:
        pattern = r'href=["\']([^"\']*(?:job|position|opening|requisition|posting)[^"\']*)["\'][^>]*>(.*?)</a>'

    for href, label in re.findall(pattern, raw, flags=re.I | re.S):
        url = urllib.parse.urljoin(base_url + "/", html.unescape(href))
        if url.rstrip("/") == base_url:
            continue
        parsed_link = urllib.parse.urlparse(url)
        if parsed_link.scheme not in {"http", "https"}:
            continue
        if re.search(r"\.(?:css|js|map|png|jpe?g|gif|svg|ico|pdf|zip)(?:$|[?#])", parsed_link.path, flags=re.I):
            continue
        if platform == "custom":
            detail_hint = re.search(
                r"(?:/job/|/jobs/|/position/|/positions/|/opening/|/openings/|requisition|posting)",
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
        role = text if 4 <= len(text) <= 120 else infer_role_from_url(url)
        if not role or role.lower() in {"apply", "learn more", "view job"}:
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
        unique[link["url"]] = link
    if platform != "custom" or source.get("parse_job_details", True) is False:
        return list(unique.values())

    max_detail_pages = int(source.get("max_detail_pages", 40))
    enriched: list[dict[str, Any]] = []
    for link in list(unique.values())[:max_detail_pages]:
        try:
            detail_raw = fetch_url(link["url"], timeout=12)
        except Exception:  # noqa: BLE001
            enriched.append(link)
            continue
        posted_at = extract_first_datetime(
            detail_raw,
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
            detail_raw,
            [
                r'"dateModified"\s*:\s*"([^"]+)"',
                r'"updated_at"\s*:\s*"([^"]+)"',
                r'<meta[^>]+property=["\']article:modified_time["\'][^>]+content=["\']([^"\']+)["\']',
            ],
        )
        title = extract_html_title(detail_raw, company, link["url"])
        link.update(
            {
                "role": title or link.get("role", ""),
                "location": extract_location(detail_raw) or link.get("location", ""),
                "posted_at": posted_at,
                "updated_at": updated_at,
                "source": source.get("url", ""),
                "freshness_source": "official_posted_at" if posted_at else "unknown",
            }
        )
        enriched.append(link)
    return enriched


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
        "target_track": candidate.get("target_track", ""),
        "matched_tracks": candidate.get("matched_tracks", []),
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


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return [keyword for keyword in keywords if re.search(rf"\b{re.escape(keyword.lower())}\b", lower)]


def extract_years(text: str) -> list[int]:
    return [int(match) for match in re.findall(r"(\d+)\+?\s*(?:years|yrs)", text, flags=re.I)]


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
    location_score = 1.5 if location_matches(app, jd_text, profile) else 0.7
    level_score = 2.0

    dealbreakers: list[str] = []
    action_items = []
    if re.search(r"security clearance|active clearance|secret clearance|top secret", jd_text, re.I):
        dealbreakers.append("Security clearance appears required.")
    if re.search(r"\b(senior|staff|principal|lead)\b", app.get("role", ""), re.I):
        dealbreakers.append("Role title appears senior/staff/principal/lead.")
    years = extract_years(jd_text)
    max_years = max(years) if years else 0
    dealbreaker_config = profile.get("dealbreakers", {})
    threshold = int(dealbreaker_config.get("minimum_years_over", 5))
    if app.get("platform") == "amazon_jobs":
        threshold = 2
    if max_years > threshold:
        dealbreakers.append(f"JD mentions {max_years}+ years, above threshold {threshold}.")
    penalty_from = int(dealbreaker_config.get("lower_weight_minimum_years_from", 3))
    if max_years >= penalty_from:
        level_score = max(0.4, level_score - 1.4)
        action_items.append(
            f"JD mentions {max_years}+ years; lower priority for 0-2 years experience target."
        )
    if re.search(r"we do not sponsor|no sponsorship|unable to sponsor", jd_text, re.I):
        if profile.get("work_authorization", {}).get("requires_sponsorship"):
            dealbreakers.append("JD says sponsorship is unavailable.")
    if not location_allowed(app.get("location", ""), profile):
        dealbreakers.append(f"Location is outside allowed WA/CA/Remote preferences: {app.get('location')}.")

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
        "matched_keywords": matched,
        "resume_keyword_matches": resume_matches,
        "missing_keywords": [keyword for keyword in matched if keyword not in resume_matches],
        "dealbreakers": dealbreakers,
        "action_items": action_items,
        "jd_text": jd_text,
    }


def location_matches(app: dict[str, Any], jd_text: str, profile: dict[str, Any]) -> bool:
    if not location_allowed(app.get("location", ""), profile):
        return False
    combined = f"{app.get('location', '')} {jd_text}".lower()
    if "remote" in combined or "united states" in combined or "usa" in combined:
        return True
    return location_allowed(app.get("location", ""), profile)


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
    for fetcher in [
        fetch_ashby_job_text,
        fetch_greenhouse_job_text,
        fetch_microsoft_job_text,
        fetch_amazon_job_text,
        fetch_google_job_text,
        fetch_meta_job_text,
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
            "url": f"https://{host}/{site}",
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
        "url": f"https://{host}/{site}",
        "page_size": 20,
        "max_pages": 5,
        "keywords": DEFAULT_WORKDAY_KEYWORDS,
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
    if direct_platform in {"greenhouse", "lever", "ashby", "gem", "workday"}:
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
            platform = detect_platform(candidate)
            if platform in {"greenhouse", "lever", "ashby", "gem", "workday"}:
                result["detected_platform"] = platform
                result["detected_url"] = normalize_job_url(candidate)
                break
        if not result["detected_platform"] and re.search(r"phenom|phenompeople|phenom-people", raw, flags=re.I):
            result["detected_platform"] = "phenom"
            result["detected_url"] = url
            result["notes"] = "Phenom detected; adapter not implemented yet."

    detected_url = str(result.get("detected_url") or "")
    platform = str(result.get("detected_platform") or "")
    if platform == "greenhouse":
        board = greenhouse_board_from_source({"url": detected_url})
        result["source"] = {"company": company or board, "platform": "greenhouse", "board": board, "url": f"https://job-boards.greenhouse.io/{board}"} if board else None
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
        if args.apply and isinstance(replacement, dict) and detected not in {"", "phenom"}:
            if replacement != source:
                sources[index] = replacement
                changed += 1
    if args.apply and changed:
        write_json(SOURCES_PATH, data)
    if args.apply:
        print(f"Updated {changed} sources in {SOURCES_PATH}.")


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


def command_discover_jobs(args: argparse.Namespace) -> None:
    require_person_files()
    profile = profile_for_track(getattr(args, "track", None))
    sources = load_json(SOURCES_PATH).get("sources", [])
    source_company_filters = {item.lower() for item in (getattr(args, "source_company", None) or [])}
    if source_company_filters:
        sources = [source for source in sources if str(source.get("company", "")).lower() in source_company_filters]
    seen = load_seen_jobs()
    cutoff = discovery_cutoff(args)
    stats = empty_discovery_stats()
    failed_sources = 0
    current_seen_at = now_utc_iso()

    track_id = profile.get("_track", {}).get("id")
    for source in sources:
        source = source_for_track(source, track_id)
        try:
            candidates = discover_source_jobs(source)
        except Exception as error:  # noqa: BLE001 - one source should not stop the run.
            failed_sources += 1
            print(f"Could not discover {source.get('company', source.get('url', 'source'))}: {error}", file=sys.stderr)
            continue

        add_stats(stats, process_discovered_candidates(candidates, args, profile, seen, cutoff, current_seen_at))

    save_seen_jobs(seen)
    print(
        "Discovery complete. "
        f"Cutoff: {cutoff.replace(microsecond=0).isoformat()}. "
        f"Discovered: {stats['discovered']}. Added: {stats['added']}. Existing: {stats['existing']}. "
        f"Skipped old: {stats['skipped_old']}. Skipped unknown posted_at: {stats['skipped_unknown_date']}. "
        f"Skipped title: {stats['skipped_title']}. Skipped location: {stats['skipped_location']}. "
        f"Scoring failed: {stats['scoring_failed']}. Failed sources: {failed_sources}."
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
    if track.get("id"):
        app["target_track"] = track["id"]
        app["matched_tracks"] = merge_unique(app.get("matched_tracks", []), [track["id"]])
        track_resume = path_from_track(track, "resume_file")
        if track_resume:
            app["resume_file"] = str(track_resume)
    jd_text = read_job_text(app, args.jd_file)
    score = score_text(app, jd_text, profile)
    output_dir = app_output_dir(app)
    output_dir.mkdir(parents=True, exist_ok=True)
    jd_path = output_dir / "jd.md"
    report_path = output_dir / "score_report.md"
    jd_path.write_text(jd_text + "\n", encoding="utf-8")
    report = render_score_report(app, score)
    report_path.write_text(report, encoding="utf-8")
    update_application(
        app["id"],
        {
            "fit_score": score["fit_score"],
            "ats_score": score["ats_score"],
            "status": score["status"],
            "dealbreakers": score["dealbreakers"],
            "action_items": score["action_items"],
            "matched_keywords": score["matched_keywords"],
            "resume_keyword_matches": score["resume_keyword_matches"],
            "missing_keywords": score["missing_keywords"],
            "jd_path": str(jd_path),
            "score_report_path": str(report_path),
            "target_track": app.get("target_track", ""),
            "matched_tracks": app.get("matched_tracks", []),
            "resume_file": app.get("resume_file", ""),
            "notes": "; ".join(score["dealbreakers"] or score["action_items"]),
        },
    )
    print(report)


def render_score_report(app: dict[str, Any], score: dict[str, Any]) -> str:
    missing = ", ".join(score["missing_keywords"]) or "None"
    matched = ", ".join(score["matched_keywords"]) or "None"
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
    recommended = sorted(
        [app for app in apps if isinstance(app.get("fit_score"), (int, float)) and not app.get("dealbreakers")],
        key=lambda app: (app.get("fit_score", 0), app.get("ats_score", 0)),
        reverse=True,
    )[:5]

    top_jobs = "\n".join(
        f"- [{app.get('fit_score')}/10 fit, {app.get('ats_score')}/100 ATS] "
        f"{app.get('company')} - {app.get('role')} ({app.get('platform')})\n  {app.get('url')}"
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
        if app.get("status") == "found":
            try:
                score_args = argparse.Namespace(id=app["id"], jd_file=None, track=getattr(args, "track", None))
                command_score_job(score_args)
            except Exception as error:  # noqa: BLE001
                update_application(app["id"], {"status": "needs_review", "notes": f"Scoring failed: {error}"})
    write_notification()
    if args.send_email or args.send_gmail or args.send_outlook:
        run_email_notify(NOTIFICATIONS_DIR / "latest.md", args)
    print("Run complete.")


def command_init_person(_args: argparse.Namespace) -> None:
    create_from_template(ROOT / "examples" / "profile.example.json", PROFILE_PATH)
    create_from_template(ROOT / "examples" / "sources.example.json", SOURCES_PATH)
    create_from_template(ROOT / "examples" / "company_watchlist.example.json", WATCHLIST_PATH)
    create_from_template(ROOT / "examples" / "applications.example.json", APPLICATIONS_JSON)
    create_from_template(ROOT / "examples" / "applications.example.csv", APPLICATIONS_CSV)
    create_from_template(ROOT / "examples" / "seen_jobs.example.json", SEEN_JOBS_PATH)
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
    discover.add_argument("--no-role-filter", action="store_true", help="Add all fresh jobs regardless of title.")
    discover.add_argument("--score", action="store_true", help="Score newly added found jobs after discovery.")
    discover.add_argument(
        "--source-company",
        action="append",
        help="Only run discovery for a specific source company. Repeat for multiple companies.",
    )

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
    elif args.command == "classify-sources":
        command_classify_sources(args)
    elif args.command == "discover-web-jobs":
        command_discover_web_jobs(args)
    elif args.command == "discover-watchlist-jobs":
        command_discover_watchlist_jobs(args)
    elif args.command == "add-url":
        command_add_url(args)
    elif args.command == "score-job":
        command_score_job(args)
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
