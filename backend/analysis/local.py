"""
Análisis local de ofertas (sin LLM): orquestador.

=============================================================================
PASO 3 · REVISIÓN DE DETALLES  (todas las fuentes — analyze_job_local)
=============================================================================
  A partir de la descripción cruda del PASO 2, extrae:
    - ubicación, salario, requisitos/skills, idiomas, email de contacto
  Misma función para LinkedIn Jobs, #Hiring, Computrabajo y APIs.

PASO 4 · CLASIFICACIÓN  (match skills aquí; filtros + email IA en api.app)
=============================================================================

Los helpers viven en submódulos temáticos y se re-exportan aquí para conservar
la API pública histórica `backend.analysis.local.*`:
  - text      normalización + secciones (requisitos/beneficios) + email
  - salary    parseo y conversión de salarios a USD + filtro por rango
  - languages detección de idioma + filtro por idioma
  - geo       filtros de país / ubicación (GetOnBoard, LinkedIn #Hiring)
  - matching  cálculo de match de skills + advice
"""

from __future__ import annotations

from typing import Any

from backend.analysis.geo import (
    linkedin_hiring_location_ok,
    passes_gob_country_filter,
)
from backend.analysis.languages import (
    detect_posting_language,
    detect_required_languages,
    passes_language_filters,
)
from backend.analysis.matching import build_advice, compute_match
from backend.analysis.salary import extract_salary_usd, salary_in_range
from backend.analysis.text import (
    extract_contact_email,
    extract_offerings,
    extract_requirements,
)
from backend.core.query_match import extract_location


def analyze_job_local(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """
    PASO 3 · REVISIÓN + match de skills local (PASO 4 parcial).

    Misma función para LinkedIn, #Hiring, Computrabajo y APIs:
    ubicación, salario, requisitos, email de contacto, match_percent.
    Filtros de país/idioma y borrador de email con IA → main._analyze_raw_jobs.
    """
    description = str(job.get("description") or "")
    requirements = extract_requirements(description)
    offerings = extract_offerings(description)
    salary = extract_salary_usd(description + " " + offerings)
    posting_lang = detect_posting_language(description or str(job.get("title") or ""))
    required_langs = detect_required_languages(description)
    contact_email = extract_contact_email(
        " ".join(
            [
                description,
                str(job.get("title") or ""),
                str(job.get("company") or ""),
                requirements,
                offerings,
            ]
        )
    )

    enriched = {**job, "requirements": requirements, "contact_email": contact_email}
    match_percent, matched, missing = compute_match(profile, enriched)
    advice = build_advice(matched, missing, enriched)

    salary_label = ""
    if salary.get("min_usd") is not None:
        if salary["min_usd"] == salary.get("max_usd"):
            salary_label = f"≈ USD {salary['min_usd']:,.0f}"
        else:
            salary_label = f"≈ USD {salary['min_usd']:,.0f}–{salary['max_usd']:,.0f}"
        if salary.get("raw"):
            salary_label += f" ({salary['raw']})"

    return {
        "title": job.get("title", "Sin título"),
        "company": job.get("company", "Empresa no indicada"),
        "url": job.get("url", ""),
        "source": job.get("source", ""),
        "location": job.get("location") or extract_location(job) or "",
        "published_at": job.get("published_at"),
        "requirements": requirements,
        "offerings": offerings,
        "match_percent": match_percent,
        "advice": advice,
        "cover_letter": "",
        "application_email": None,
        "contact_email": contact_email,
        "salary_usd": salary_label,
        "salary_min_usd": salary.get("min_usd"),
        "salary_max_usd": salary.get("max_usd"),
        "posting_language": posting_lang,
        "required_languages": required_langs,
        "matched_skills": matched,
        "missing_skills": missing,
    }


__all__ = [
    "analyze_job_local",
    "build_advice",
    "compute_match",
    "detect_posting_language",
    "detect_required_languages",
    "extract_contact_email",
    "extract_offerings",
    "extract_requirements",
    "extract_salary_usd",
    "linkedin_hiring_location_ok",
    "passes_gob_country_filter",
    "passes_language_filters",
    "salary_in_range",
]
