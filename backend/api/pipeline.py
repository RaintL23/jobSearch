"""
Pipeline de análisis (PASO 3–4) desacoplado de FastAPI.

Resuelve filtros efectivos del perfil, clasifica ofertas (país, idioma,
salario, match) y aplica el batch de IA opcional para casos ambiguos.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from backend.ai.engine import batch_analyze_relevance
from backend.analysis.local import (
    analyze_job_local,
    linkedin_hiring_location_ok,
    passes_gob_country_filter,
    passes_language_filters,
    salary_in_range,
)
from backend.core.config import get_settings
from backend.core.dates import hours_since_published
from backend.core.query_match import extract_location
from backend.core.runtime_key import has_runtime_key
from backend.scraping import SOURCE_LATAM_RANK, is_linkedin_hiring_permalink
from backend.scraping.filters import merge_profile_filters
from backend.api.schemas import SearchRequest

logger = logging.getLogger(__name__)


def _filters_from_payload(payload: SearchRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Perfil + filtros efectivos (`profile.filters` como defaults del backend)."""
    profile_dict = payload.profile.model_dump()
    filters_dict = merge_profile_filters(profile_dict, payload.filters.model_dump())

    queries = list(filters_dict.get("queries") or [])
    if filters_dict.get("query"):
        queries.append(filters_dict["query"])
    queries = [q.strip() for q in queries if str(q).strip()]
    filters_dict["queries"] = queries
    return profile_dict, filters_dict


def _prepare_search(payload: SearchRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    profile_dict, filters_dict = _filters_from_payload(payload)

    has_roles = bool(profile_dict.get("roles"))
    has_skills = bool(profile_dict.get("skills"))
    if not filters_dict.get("queries") and not has_roles and not has_skills:
        raise HTTPException(
            status_code=400,
            detail="Indica al menos un texto de búsqueda, o roles/skills en el perfil.",
        )
    return profile_dict, filters_dict


def _analyze_raw_jobs(
    profile_dict: dict[str, Any],
    filters_dict: dict[str, Any],
    raw_jobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    PASO 3–4 unificado (todas las fuentes).

    PASO 3: analyze_job_local (ubicación, salario, skills, contact_email).
    PASO 4: filtros país/ubicación/idioma/salario + match; IA solo si hace falta
    (ubicación ambigua) o bajo demanda (email / cover letter).
    """
    filter_countries: list[str] = list(filters_dict.get("countries") or [])
    profile_country: str = str(profile_dict.get("country") or "")
    filter_locations: list[str] = list(filters_dict.get("locations") or [])
    if profile_dict.get("location"):
        filter_locations = filter_locations + [str(profile_dict["location"])]
    match_available = bool(profile_dict.get("skills") or profile_dict.get("roles"))

    posting_langs = list(filters_dict.get("posting_languages") or [])
    if filters_dict.get("posting_language") and filters_dict["posting_language"] != "any":
        posting_langs.append(filters_dict["posting_language"])
    required_langs = list(filters_dict.get("required_languages") or [])
    if filters_dict.get("required_language") and filters_dict["required_language"] != "any":
        required_langs.append(filters_dict["required_language"])

    results: list[dict[str, Any]] = []
    discard_counts: dict[str, int] = {}
    discard_sample: list[dict[str, Any]] = []

    def _note_discard(job: dict[str, Any], reason: str) -> None:
        discard_counts[reason] = discard_counts.get(reason, 0) + 1
        if len(discard_sample) < 12:
            discard_sample.append(
                {
                    "title": str(job.get("title") or "")[:120],
                    "company": str(job.get("company") or "")[:80],
                    "reason": reason,
                    "reason_label": {
                        "country": "país",
                        "language": "idioma",
                        "salary": "salario",
                        "invalid_link": "enlace no específico",
                    }.get(reason, reason),
                }
            )

    hiring_user_country = (filter_countries[0] if filter_countries else profile_country) or ""

    for job in raw_jobs:
        # --- PASO 4 · filtro país (GetOnBoard, datos estructurados) ---
        if job.get("source") == "getonboard":
            if not passes_gob_country_filter(job, filter_countries, profile_country):
                logger.debug(
                    "GOB country filter: descartando '%s' (%s) — países oferta: %s",
                    job.get("title"),
                    job.get("company"),
                    job.get("_countries_raw"),
                )
                _note_discard(job, "country")
                continue

        # --- PASO 4 · filtro ubicación (LinkedIn #Hiring) ---
        needs_ai_location = False
        if job.get("source") == "linkedin_hiring":
            if not is_linkedin_hiring_permalink(str(job.get("url") or "")):
                _note_discard(job, "invalid_link")
                continue
            verdict = linkedin_hiring_location_ok(
                str(job.get("description") or ""),
                hiring_user_country,
                filter_locations,
            )
            if verdict is False:
                logger.debug(
                    "LinkedIn #Hiring location filter: descartando '%s' (%s)",
                    job.get("title"),
                    job.get("company"),
                )
                _note_discard(job, "country")
                continue
            needs_ai_location = verdict is None

        # --- PASO 3 · revisar descripción (skills, salario, email, …) ---
        analyzed = analyze_job_local(profile_dict, job)
        if needs_ai_location:
            analyzed["_needs_ai_location"] = True
        if not match_available:
            analyzed["match_percent"] = None
            analyzed["matched_skills"] = []
            analyzed["missing_skills"] = []
            analyzed["advice"] = "Cargá un perfil CV para calcular el match y recibir recomendaciones."

        # --- PASO 4 · clasificación: idioma + salario + skills (match ya calculado) ---
        if not passes_language_filters(analyzed, posting_langs, required_langs):
            _note_discard(job, "language")
            continue
        if not salary_in_range(
            {
                "min_usd": analyzed.get("salary_min_usd"),
                "max_usd": analyzed.get("salary_max_usd"),
            },
            filters_dict.get("salary_min_usd"),
            filters_dict.get("salary_max_usd"),
        ):
            _note_discard(job, "salary")
            continue

        analyzed["description"] = (job.get("description") or "")[:4000]
        analyzed["source"] = job.get("source") or analyzed.get("source") or ""
        analyzed["company"] = job.get("company") or analyzed.get("company") or "Empresa no indicada"
        analyzed["published_at"] = job.get("published_at") or analyzed.get("published_at")
        analyzed["location"] = (
            job.get("location")
            or analyzed.get("location")
            or extract_location(job)
            or ""
        )
        # Pasar datos de países al resultado para el paso de IA (se eliminan al final).
        analyzed["_countries_raw"] = job.get("_countries_raw") or []
        results.append(analyzed)

    # --- PASO 4 · IA batch solo para ubicación ambigua (GOB + #Hiring) ---
    if match_available:
        dropped = _apply_ai_batch(profile_dict, filter_countries, profile_country, results)
        for job in dropped:
            _note_discard(job, "country")

    # Eliminar campos internos antes de devolver al frontend.
    for job in results:
        job.pop("_countries_raw", None)
        job.pop("_needs_ai_location", None)

    def _sort_key(job: dict[str, Any]) -> tuple[int, float, int]:
        hours = hours_since_published(job.get("published_at"))
        return (
            SOURCE_LATAM_RANK.get(str(job.get("source") or ""), 99),
            hours if hours is not None else float("inf"),
            -(int(job.get("match_percent") or 0)),
        )

    results.sort(key=_sort_key)
    analyze_meta = {
        "input_count": len(raw_jobs),
        "kept_count": len(results),
        "match_available": match_available,
        "discarded_by_reason": discard_counts,
        "discarded_sample": discard_sample,
    }
    return results, analyze_meta


def _format_analyze_discard_message(meta: dict[str, Any]) -> str:
    input_n = int(meta.get("input_count") or 0)
    kept_n = int(meta.get("kept_count") or 0)
    counts = meta.get("discarded_by_reason") or {}
    discarded_n = input_n - kept_n
    suffix = "con match calculado" if meta.get("match_available") else "sin cálculo de match"
    msg = f"Listo · {kept_n} oferta(s) {suffix}"
    if discarded_n > 0 and counts:
        labels = {
            "country": "país",
            "language": "idioma",
            "salary": "salario",
            "invalid_link": "enlace no específico",
        }
        detail = ", ".join(
            f"{labels.get(k, k)}: {v}"
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        msg += f" · {discarded_n} descartada(s) en análisis ({detail})"
    elif discarded_n > 0:
        msg += f" · {discarded_n} descartada(s) en análisis"
    return msg + "."


def _apply_ai_batch(
    profile_dict: dict[str, Any],
    filter_countries: list[str],
    profile_country: str,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Analiza en batch los casos de ubicación ambigua y match borderline con IA.

    Cubre dos fuentes:
      - GetOnBoard sin países estructurados (match 30-75).
      - LinkedIn #Hiring marcado como ambiguo por la heurística de ubicación.

    Solo actúa si AI_MATCH_ENABLED=true en .env y hay API key configurada.
    Una sola llamada a Gemini por grupo de hasta 6 ofertas (token-eficiente).
    Modifica `results` en lugar (ajusta match_percent y advice) y devuelve la
    lista de ofertas eliminadas por país no compatible (para el conteo de
    descartes).
    """
    settings = get_settings()
    if not settings.ai_match_enabled or (not settings.has_api_key and not has_runtime_key()):
        return []

    # Ofertas con ubicación ambigua que más se benefician del análisis IA:
    #  - GOB sin países estructurados y con match borderline.
    #  - #Hiring que la heurística no pudo clasificar (posible US-only, etc.).
    ambiguous_idxs = [
        i for i, j in enumerate(results)
        if (
            j.get("source") == "getonboard"
            and not j.get("_countries_raw")
            and 30 <= int(j.get("match_percent") or 0) <= 75
        )
        or (j.get("source") == "linkedin_hiring" and j.get("_needs_ai_location"))
    ]

    if not ambiguous_idxs:
        return []

    user_country = (filter_countries[0] if filter_countries else profile_country) or ""
    batch_jobs = [results[i] for i in ambiguous_idxs]

    try:
        ai_entries = batch_analyze_relevance(profile_dict, batch_jobs, user_country)
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch_analyze_relevance falló (se ignora): %s", exc)
        return []

    drop_idxs: set[int] = set()
    for entry in ai_entries:
        batch_pos = int(entry.get("idx", 0)) - 1
        if batch_pos < 0 or batch_pos >= len(ambiguous_idxs):
            continue
        result_idx = ambiguous_idxs[batch_pos]
        job = results[result_idx]

        country_ok = entry.get("country_ok")
        delta = int(entry.get("match_delta") or 0)
        reason = str(entry.get("reason") or "").strip()

        # #Hiring incompatible según la IA → excluir (validación de ubicación).
        if country_ok is False and job.get("source") == "linkedin_hiring":
            drop_idxs.add(result_idx)
            continue

        # GOB: penalización extra si la IA detecta incompatibilidad de país.
        if country_ok is False:
            delta = min(delta, -15)
            reason = f"⚠️ País posiblemente no disponible. {reason}".strip()

        job["match_percent"] = max(5, min(98, int(job.get("match_percent") or 0) + delta))

        if reason:
            existing_advice = job.get("advice") or ""
            job["advice"] = f"[IA] {reason}\n{existing_advice}".strip()

    dropped = [results[i] for i in sorted(drop_idxs)]
    if drop_idxs:
        results[:] = [r for i, r in enumerate(results) if i not in drop_idxs]
    return dropped
