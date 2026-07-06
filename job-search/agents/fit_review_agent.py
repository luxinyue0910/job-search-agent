from __future__ import annotations

import re
from typing import Any


SENIOR_TITLE_WORDS = ("senior", "sr.", "staff", "principal", "lead", "manager", "architect")
PREFERRED_LOCATION_WORDS = (
    "seattle",
    "bellevue",
    "redmond",
    "washington",
    "wa",
    "remote",
    "san francisco",
    "sf",
    "bay area",
    "california",
    "ca",
)


def years_required(text: str) -> int | None:
    matches = re.findall(r"(\d+)\s*\+?\s*(?:years?|yrs?)", text, flags=re.I)
    if not matches:
        return None
    return max(int(item) for item in matches)


def review_fit(job: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    role = str(job.get("role") or job.get("title") or "")
    location = str(job.get("location") or "")
    notes = str(job.get("notes") or "")
    fit_score = float(job.get("fit_score") or 0)
    text = " ".join([role, location, notes]).lower()

    risk_flags: list[str] = []
    positive_signals: list[str] = []
    reasons: list[str] = []

    required_years = years_required(text)
    if any(word in text for word in SENIOR_TITLE_WORDS) or (required_years is not None and required_years >= 3):
        risk_flags.append("too_senior")
        reasons.append("Role appears senior or asks for 3+ years.")

    if location and not any(word in location.lower() for word in PREFERRED_LOCATION_WORDS):
        risk_flags.append("location_risk")
        reasons.append("Location does not clearly match WA, CA, or Remote preference.")
    elif location:
        positive_signals.append("location_match")

    if fit_score >= 8:
        positive_signals.append("fit_score_high")
    elif fit_score and fit_score < 6:
        risk_flags.append("low_fit_score")
        reasons.append("Fit score is below priority threshold.")

    if re.search(r"\b(new grad|junior|entry|associate|apprentice|software engineer i)\b", text, flags=re.I):
        positive_signals.append("level_match")

    if "too_senior" in risk_flags or "location_risk" in risk_flags:
        bucket = "skip"
    elif fit_score >= 8:
        bucket = "priority"
    else:
        bucket = "maybe"

    return {
        "bucket": bucket,
        "fit_score": fit_score,
        "positive_signals": sorted(set(positive_signals)),
        "risk_flags": sorted(set(risk_flags)),
        "reasons": reasons or ["No blocking risk found by rule-based review."],
        "recommended_action": "prepare_application" if bucket == "priority" else "manual_review",
        "reviewer": "fit_review_agent_rules_v1",
    }

