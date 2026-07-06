from __future__ import annotations

from typing import Any


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in phrases)


def review_application_materials(
    *,
    jd_text: str,
    profile_text: str,
    cover_letter: str,
    screening_answers: str,
) -> dict[str, Any]:
    material_text = "\n".join([cover_letter or "", screening_answers or ""])
    profile_lower = (profile_text or "").lower()
    issues: list[dict[str, str]] = []

    if not (cover_letter or "").strip():
        issues.append({"code": "missing_cover_letter", "message": "Cover letter is empty."})

    citizen_claims = ("u.s. citizen", "us citizen", "united states citizen")
    if _contains_any(material_text, citizen_claims) and not _contains_any(profile_lower, citizen_claims):
        issues.append(
            {
                "code": "sensitive_claim_not_grounded",
                "message": "Material claims U.S. citizenship, but profile text does not support that claim.",
            }
        )

    green_card_claims = ("green card holder", "lawful permanent resident")
    if _contains_any(material_text, green_card_claims) and not _contains_any(profile_lower, green_card_claims):
        issues.append(
            {
                "code": "sensitive_claim_not_grounded",
                "message": "Material claims green-card/LPR status, but profile text does not support that claim.",
            }
        )

    if "security clearance" in material_text.lower() and "security clearance" not in profile_lower:
        issues.append(
            {
                "code": "sensitive_claim_not_grounded",
                "message": "Material references security clearance without profile support.",
            }
        )

    if len((jd_text or "").strip()) < 80:
        issues.append({"code": "jd_context_too_short", "message": "JD text is too short for reliable grounding review."})

    return {
        "pass": not issues,
        "issues": issues,
        "hallucination_risk": "low" if not issues else "medium",
        "needs_manual_review": True,
        "reviewer": "application_qa_agent_rules_v1",
    }

