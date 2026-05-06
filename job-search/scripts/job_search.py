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
import json
import os
import re
import subprocess
import sys
import textwrap
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
SEEN_JOBS_PATH = PRIVATE_BASE_ROOT / "data" / "seen_jobs.json"
OUTPUT_DIR = PRIVATE_BASE_ROOT / "output"
NOTIFICATIONS_DIR = OUTPUT_DIR / "notifications"

CSV_FIELDS = [
    "id",
    "company",
    "role",
    "url",
    "platform",
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
    "cloud",
    "new grad",
    "junior",
    "entry level",
    "swe",
    "developer",
]


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
    global PERSON, PERSON_ROOT, PROFILE_PATH, APPLICATIONS_JSON, APPLICATIONS_CSV, SOURCES_PATH, SEEN_JOBS_PATH, OUTPUT_DIR, NOTIFICATIONS_DIR

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
    SEEN_JOBS_PATH = PERSON_ROOT / "data" / "seen_jobs.json"
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
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
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
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def normalize_datetime(value: Any) -> str:
    parsed = parse_datetime(value)
    return parsed.replace(microsecond=0).isoformat() if parsed else ""


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


def greenhouse_board_from_source(source: dict[str, Any]) -> str | None:
    if source.get("board"):
        return str(source["board"])
    parsed = urllib.parse.urlparse(source.get("url", ""))
    if "greenhouse.io" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
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


def discover_source_jobs(source: dict[str, Any]) -> list[dict[str, Any]]:
    platform = source_platform(source)
    if platform == "greenhouse":
        return discover_greenhouse_jobs(source)
    if platform == "lever":
        return discover_lever_jobs(source)
    if platform == "ashby":
        return discover_ashby_jobs(source)
    return find_links_for_source(source)


def discovery_title_matches(candidate: dict[str, Any], profile: dict[str, Any]) -> bool:
    role = str(candidate.get("role", "")).lower()
    if not role:
        return False
    profile_terms = [
        str(item).lower()
        for item in profile.get("targets", {}).get("roles", []) + profile.get("targets", {}).get("levels", [])
        if str(item).strip()
    ]
    terms = profile_terms + DEFAULT_DISCOVERY_TITLE_KEYWORDS
    return any(re.search(rf"\b{re.escape(term)}\b", role) for term in terms)


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


def find_links_for_source(source: dict[str, Any]) -> list[dict[str, str]]:
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
        pattern = r'href=["\']([^"\']*(?:job|career|position|opening)[^"\']*)["\'][^>]*>(.*?)</a>'

    for href, label in re.findall(pattern, raw, flags=re.I | re.S):
        url = urllib.parse.urljoin(base_url + "/", html.unescape(href))
        if url.rstrip("/") == base_url:
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

    unique: dict[str, dict[str, str]] = {}
    for link in links:
        unique[link["url"]] = link
    return list(unique.values())


def upsert_application(candidate: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    tracker = load_tracker()
    apps = tracker.setdefault("applications", [])
    normalized_url = normalize_job_url(candidate["url"])
    for app in apps:
        if normalize_job_url(app.get("url", "")) == normalized_url:
            changed = False
            for field in ["posted_at", "updated_at", "first_seen", "last_seen", "source"]:
                if candidate.get(field) and app.get(field) != candidate[field]:
                    app[field] = candidate[field]
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
        "first_seen": candidate.get("first_seen", ""),
        "last_seen": candidate.get("last_seen", ""),
        "source": candidate.get("source", ""),
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
    matched = sorted(set(keyword_matches(jd_text, TECH_KEYWORDS + [k.lower() for k in target_keywords])))
    resume_text = master_resume_path().read_text(encoding="utf-8", errors="replace")
    resume_matches = sorted(set(keyword_matches(resume_text, matched)))
    ats_score = round((len(resume_matches) / len(matched)) * 100) if matched else 0

    role_text = f"{app.get('role', '')} {jd_text}".lower()
    role_score = 2.5 if any(role.lower() in role_text for role in profile.get("targets", {}).get("roles", [])) else 1.0
    tech_score = min(4.0, len(matched) * 0.45)
    location_score = 1.5 if location_matches(app, jd_text, profile) else 0.7
    level_score = 2.0

    dealbreakers: list[str] = []
    if re.search(r"security clearance|active clearance|secret clearance|top secret", jd_text, re.I):
        dealbreakers.append("Security clearance appears required.")
    if re.search(r"\b(senior|staff|principal|lead)\b", app.get("role", ""), re.I):
        dealbreakers.append("Role title appears senior/staff/principal/lead.")
    years = extract_years(jd_text)
    max_years = max(years) if years else 0
    threshold = int(profile.get("dealbreakers", {}).get("minimum_years_over", 5))
    if max_years > threshold:
        dealbreakers.append(f"JD mentions {max_years}+ years, above threshold {threshold}.")
    if re.search(r"we do not sponsor|no sponsorship|unable to sponsor", jd_text, re.I):
        if profile.get("work_authorization", {}).get("requires_sponsorship"):
            dealbreakers.append("JD says sponsorship is unavailable.")

    fit_score = 0.0 if dealbreakers else round(min(10.0, role_score + tech_score + location_score + level_score), 1)
    status = "skipped" if dealbreakers else ("needs_review" if fit_score < 6.0 or ats_score < 60 else "scored")
    action_items = []
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
    combined = f"{app.get('location', '')} {jd_text}".lower()
    if "remote" in combined or "united states" in combined or "usa" in combined:
        return True
    for location in profile.get("preferences", {}).get("locations", []):
        if str(location).lower() in combined:
            return True
    return bool(profile.get("preferences", {}).get("willing_to_relocate", False))


def app_output_dir(app: dict[str, Any]) -> Path:
    return OUTPUT_DIR / slugify(app.get("company", "unknown")) / slugify(app.get("role", "unknown-role"))


def master_resume_path() -> Path:
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
    for fetcher in [fetch_ashby_job_text, fetch_greenhouse_job_text]:
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


def command_find_jobs(_args: argparse.Namespace) -> None:
    require_person_files()
    sources = load_json(SOURCES_PATH).get("sources", [])
    new_count = 0
    review_count = 0
    for source in sources:
        for candidate in find_links_for_source(source):
            app, created = upsert_application(candidate)
            if created:
                new_count += 1
            if app.get("status") == "found":
                review_count += 1
    print(f"Found {new_count} new jobs. {review_count} jobs are ready for scoring.")


def command_discover_jobs(args: argparse.Namespace) -> None:
    require_person_files()
    profile = load_profile()
    sources = load_json(SOURCES_PATH).get("sources", [])
    seen = load_seen_jobs()
    seen_jobs = seen.setdefault("jobs", {})
    cutoff = discovery_cutoff(args)
    discovered = 0
    added = 0
    existing = 0
    skipped_old = 0
    skipped_unknown_date = 0
    skipped_title = 0
    failed_sources = 0
    current_seen_at = now_utc_iso()

    for source in sources:
        try:
            candidates = discover_source_jobs(source)
        except Exception as error:  # noqa: BLE001 - one source should not stop the run.
            failed_sources += 1
            print(f"Could not discover {source.get('company', source.get('url', 'source'))}: {error}", file=sys.stderr)
            continue

        for candidate in candidates:
            discovered += 1
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
                }
            )
            candidate["first_seen"] = seen_record.get("first_seen", current_seen_at)
            candidate["last_seen"] = current_seen_at

            posted_at = parse_datetime(candidate.get("posted_at"))
            if not posted_at:
                if not args.include_unknown_posted_date:
                    skipped_unknown_date += 1
                    continue
            elif posted_at < cutoff:
                skipped_old += 1
                continue
            if not args.no_role_filter and not discovery_title_matches(candidate, profile):
                skipped_title += 1
                continue

            app, created = upsert_application(candidate)
            if created:
                added += 1
            else:
                existing += 1
            if args.score and app.get("status") == "found":
                try:
                    command_score_job(argparse.Namespace(id=app["id"], jd_file=None))
                except Exception as error:  # noqa: BLE001
                    update_application(app["id"], {"status": "needs_review", "notes": f"Scoring failed: {error}"})

    save_seen_jobs(seen)
    print(
        "Discovery complete. "
        f"Cutoff: {cutoff.replace(microsecond=0).isoformat()}. "
        f"Discovered: {discovered}. Added: {added}. Existing: {existing}. "
        f"Skipped old: {skipped_old}. Skipped unknown posted_at: {skipped_unknown_date}. "
        f"Skipped title: {skipped_title}. "
        f"Failed sources: {failed_sources}."
    )


def command_add_url(args: argparse.Namespace) -> None:
    candidate = {
        "company": args.company or "Unknown Company",
        "role": args.role or infer_role_from_url(args.url),
        "url": args.url,
        "platform": args.platform or detect_platform(args.url),
        "location": args.location or "",
        "notes": args.notes or "",
    }
    app, created = upsert_application(candidate)
    state = "created" if created else "already existed"
    print(f"{state}: {app['id']} {app['company']} - {app['role']}")


def command_score_job(args: argparse.Namespace) -> None:
    app = get_application(args.id)
    profile = load_profile()
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

    profile = load_profile()
    output_dir = app_output_dir(app)
    output_dir.mkdir(parents=True, exist_ok=True)

    jd_text = ""
    jd_path = app.get("jd_path")
    if jd_path and Path(jd_path).exists():
        jd_text = Path(jd_path).read_text(encoding="utf-8", errors="replace")
    else:
        jd_text = read_job_text(app)
        (output_dir / "jd.md").write_text(jd_text + "\n", encoding="utf-8")

    resume_master = master_resume_path().read_text(encoding="utf-8")
    missing = app.get("missing_keywords", [])
    resume_path = output_dir / "resume_tailored.md"
    resume_path.write_text(render_tailored_resume(resume_master, app, jd_text, missing), encoding="utf-8")

    cover_template = template_path("cover_letter.md").read_text(encoding="utf-8")
    cover_path = output_dir / "cover_letter.md"
    cover_path.write_text(render_cover_letter(cover_template, app, profile), encoding="utf-8")

    screening_template = template_path("screening_answers.md").read_text(encoding="utf-8")
    screening_path = output_dir / "screening_answers.md"
    screening_path.write_text(render_screening_answers(screening_template, app, profile), encoding="utf-8")

    update_application(
        app["id"],
        {
            "status": "prepared",
            "resume_path": str(resume_path),
            "cover_letter_path": str(cover_path),
            "screening_answers_path": str(screening_path),
            "action_items": ["Review materials, then run fill-form. Final submit must be manual."],
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


def render_cover_letter(template: str, app: dict[str, Any], profile: dict[str, Any]) -> str:
    personal = profile.get("personal", {})
    links = profile.get("links", {})
    replacements = {
        "[Your Name]": personal.get("name", ""),
        "[Your Email]": personal.get("email", ""),
        "[Your Phone]": personal.get("phone", ""),
        "[Your Location]": personal.get("location", ""),
        "[Your LinkedIn]": links.get("linkedin", ""),
        "[Your Website]": links.get("website", ""),
        "[Date]": today(),
        "[Company]": app.get("company", ""),
        "[Role]": app.get("role", ""),
        "[Company Hook]": "its products, engineering work, and the impact described in the job posting",
        "[Relevant Skills]": ", ".join(profile.get("targets", {}).get("keywords", [])[:5]),
        "[Relevant Project]": "one of my strongest real projects",
        "[Role Focus]": "shipping reliable software for users",
    }
    result = template
    for old, new in replacements.items():
        result = result.replace(old, str(new))
    return result


def render_screening_answers(template: str, app: dict[str, Any], profile: dict[str, Any]) -> str:
    defaults = profile.get("application_defaults", {})
    result = template.replace("[Company]", app.get("company", "the company"))
    result = result.replace("[role need]", app.get("role", "the role"))
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
                score_args = argparse.Namespace(id=app["id"], jd_file=None)
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
    create_from_template(ROOT / "examples" / "applications.example.json", APPLICATIONS_JSON)
    create_from_template(ROOT / "examples" / "applications.example.csv", APPLICATIONS_CSV)
    create_from_template(ROOT / "examples" / "seen_jobs.example.json", SEEN_JOBS_PATH)
    create_from_template(ROOT / "examples" / "master_resume.example.md", PERSON_ROOT / "resume" / "master_resume.md")
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
    subcommands.add_parser("find-jobs", help="Fetch ATS source pages and add job links to the tracker.")

    discover = subcommands.add_parser("discover-jobs", help="Discover jobs with ATS APIs and filter by posted date.")
    discover.add_argument("--since-hours", type=float, help="Only add jobs posted within this many hours. Defaults to 24.")
    discover.add_argument("--since-days", type=float, help="Only add jobs posted within this many days.")
    discover.add_argument(
        "--include-unknown-posted-date",
        action="store_true",
        help="Add jobs even when the source does not expose a posted date.",
    )
    discover.add_argument("--no-role-filter", action="store_true", help="Add all fresh jobs regardless of title.")
    discover.add_argument("--score", action="store_true", help="Score newly added found jobs after discovery.")

    add = subcommands.add_parser("add-url", help="Manually add one job URL.")
    add.add_argument("url")
    add.add_argument("--company")
    add.add_argument("--role")
    add.add_argument("--platform")
    add.add_argument("--location")
    add.add_argument("--notes")

    score = subcommands.add_parser("score-job", help="Score one job by id or URL.")
    score.add_argument("--id", required=True)
    score.add_argument("--jd-file")

    prepare = subcommands.add_parser("prepare-application", help="Generate local application materials.")
    prepare.add_argument("--id", required=True)

    notify = subcommands.add_parser("notify", help="Write and optionally send the run summary.")
    notify.add_argument("--send-email", action="store_true", help="Send using profile.notifications.provider.")
    notify.add_argument("--send-gmail", action="store_true")
    notify.add_argument("--send-outlook", action="store_true")

    run = subcommands.add_parser("run", help="Find jobs, score unscored jobs, and write notification.")
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
