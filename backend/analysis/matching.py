"""
Cálculo de match de skills/roles y generación de consejos (advice) sin LLM.
"""

from __future__ import annotations

from typing import Any

from backend.analysis.text import _norm


def compute_match(profile: dict[str, Any], job: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    """Retorna (percent, matched_skills, missing_skills)."""
    blob = _norm(
        " ".join(
            [
                str(job.get("title") or ""),
                str(job.get("description") or ""),
                str(job.get("requirements") or ""),
            ]
        )
    )
    skills = [_norm(s) for s in (profile.get("skills") or []) if str(s).strip()]
    roles = [_norm(r) for r in (profile.get("roles") or []) if str(r).strip()]

    matched: list[str] = []
    missing: list[str] = []
    for s in skills:
        if len(s) < 2:
            continue
        if s in blob or s.replace(".", "") in blob:
            matched.append(s)
        else:
            missing.append(s)

    role_hit = any(r in blob for r in roles if len(r) > 2)
    skill_score = (len(matched) / len(skills) * 70) if skills else 35
    role_score = 25 if role_hit else 8
    years = float(profile.get("experience_years") or 0)
    exp_score = 5 if years >= 1 else 0
    percent = int(max(5, min(98, round(skill_score + role_score + exp_score))))
    return percent, matched[:12], missing[:8]


def build_advice(matched: list[str], missing: list[str], job: dict[str, Any]) -> str:
    lines: list[str] = []
    if matched:
        lines.append(f"• Destaca en tu CV/perfil: {', '.join(matched[:6])}.")
    if missing:
        lines.append(
            f"• Cubre o menciona honestamente gaps: {', '.join(missing[:5])}."
        )
    else:
        lines.append("• Buen solapamiento de skills; personaliza ejemplos recientes.")
    title = job.get("title") or "el puesto"
    lines.append(f"• Adapta la postulación al título «{title}» y a la empresa.")
    if job.get("url"):
        lines.append("• Revisa la oferta original antes de enviar (requisitos pueden cambiar).")
    if job.get("contact_email"):
        lines.append(
            f"• Email de contacto ({job['contact_email']}): "
            "generá asunto + cuerpo con IA y recordá adjuntar el CV."
        )
    return "\n".join(lines)
