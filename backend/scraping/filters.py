"""Normalización de filtros, descarte y partición de ofertas crudas."""

from __future__ import annotations

import re
from typing import Any

from backend.core.config import get_settings
from backend.core.dates import within_posted_window
from backend.core.query_match import matches_search_queries
from backend.scraping.constants import (
    ALL_SOURCES,
    COUNTRY_META,
    DISCARD_REASON_LABELS,
    EXPERIENCE_KEYWORDS,
    WORK_MODE_KEYWORDS,
    _BOARD_SCOPED_SOURCES,
)

def _split_multi(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [p.strip() for p in re.split(r"[\n,;|]+", text) if p.strip()]


def _normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    f = dict(filters or {})
    queries = _split_multi(f.get("queries") or f.get("query"))
    locations = _split_multi(f.get("locations") or f.get("location"))

    def _opt_float(key: str) -> float | None:
        v = f.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _multi(primary: str, legacy: str | None = None) -> list[str]:
        vals = _split_multi(f.get(primary))
        if not vals and legacy:
            vals = _split_multi(f.get(legacy))
        return [v.lower() for v in vals if v.lower() not in ("", "any")]

    sources_raw = _split_multi(f.get("sources") or f.get("source"))
    sources = [s.lower() for s in sources_raw if s.lower() in ALL_SOURCES]

    return {
        "queries": queries,
        "locations": locations,
        "posted_within": _multi("posted_within"),
        "experience_levels": _multi("experience_levels", "experience_level"),
        "work_modes": _multi("work_modes", "work_mode"),
        "countries": _multi("countries", "country"),
        "sources": sources,
        "salary_min_usd": _opt_float("salary_min_usd"),
        "salary_max_usd": _opt_float("salary_max_usd"),
        "posting_languages": _multi("posting_languages", "posting_language"),
        "required_languages": _multi("required_languages", "required_language"),
    }


def _country_codes(profile: dict[str, Any], filters: dict[str, Any]) -> list[str]:
    codes = [c for c in (filters.get("countries") or []) if c in COUNTRY_META]
    if codes:
        return codes[:8]
    raw = str(
        profile.get("country") or get_settings().default_country
    ).lower().strip()
    return [raw if raw in COUNTRY_META else "mx"]


def _search_queries(profile: dict[str, Any], filters: dict[str, Any]) -> list[str]:
    queries = list(filters.get("queries") or [])
    if queries:
        return queries[:8]
    roles = profile.get("roles") or []
    if isinstance(roles, list) and roles:
        return [str(r).strip() for r in roles if str(r).strip()][:5]
    skills = profile.get("skills") or []
    if isinstance(skills, list) and skills:
        return [str(skills[0]).strip()]
    return ["desarrollador"]


def _locations(profile: dict[str, Any], filters: dict[str, Any]) -> list[str]:
    locs = list(filters.get("locations") or [])
    if locs:
        return locs[:6]
    hint = str(profile.get("location") or "").strip()
    return [hint] if hint else [""]

def _matches_soft_filters(job: dict[str, Any], filters: dict[str, Any]) -> bool:
    return _discard_reason(job, filters) is None


def _discard_reason(
    job: dict[str, Any],
    filters: dict[str, Any],
    *,
    skip_query: bool = False,
    source: str = "",
) -> str | None:
    """
    Motivo de descarte local, o None si la oferta se conserva.
    Códigos: query | date | work_mode | experience
    """
    if not skip_query and not matches_search_queries(job, filters.get("queries") or []):
        return "query"

    date_filters = filters
    if source:
        # Remotive/RemoteOK/Jobicy: si el usuario pide 24h, ampliar a semana
        from backend.scraping.sources.api import _posted_filters_for_source

        date_filters = _posted_filters_for_source(filters, source)
    if not within_posted_window(job.get("published_at"), date_filters):
        return "date"

    blob = " ".join(
        [
            str(job.get("title") or ""),
            str(job.get("company") or ""),
            str(job.get("description") or ""),
            str(job.get("location") or ""),
        ]
    ).lower()

    modes = filters.get("work_modes") or []
    if modes:
        keys: list[str] = []
        for mode in modes:
            keys.extend(WORK_MODE_KEYWORDS.get(mode, []))
        any_mode_mentioned = any(
            k in blob for ks in WORK_MODE_KEYWORDS.values() for k in ks
        )
        if any_mode_mentioned and keys and not any(k in blob for k in keys):
            return "work_mode"

    levels = filters.get("experience_levels") or []
    if levels:
        keys = []
        for level in levels:
            keys.extend(EXPERIENCE_KEYWORDS.get(level, []))
        any_level_mentioned = any(
            k in blob for ks in EXPERIENCE_KEYWORDS.values() for k in ks
        )
        if any_level_mentioned and keys and not any(k in blob for k in keys):
            return "experience"

    return None

def _partition_jobs(
    jobs: list[dict[str, Any]],
    filters: dict[str, Any],
    *,
    source: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Separa lista cruda → (kept, discarded_samples, counts_by_reason)."""
    skip_query = source in _BOARD_SCOPED_SOURCES
    kept: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for job in jobs:
        if source == "linkedin_hiring":
            from backend.scraping.sources.linkedin_hiring import (
                is_linkedin_hiring_permalink,
            )

            if not is_linkedin_hiring_permalink(str(job.get("url") or "")):
                reason = "invalid_link"
            else:
                reason = _discard_reason(
                    job, filters, skip_query=skip_query, source=source
                )
        else:
            reason = _discard_reason(
                job, filters, skip_query=skip_query, source=source
            )
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
            if len(discarded) < 12:
                discarded.append(
                    {
                        "title": str(job.get("title") or "")[:120],
                        "company": str(job.get("company") or "")[:80],
                        "reason": reason,
                        "reason_label": DISCARD_REASON_LABELS.get(reason, reason),
                    }
                )
            continue
        kept.append(job)
    return kept, discarded, counts


def _format_source_filter_message(
    *,
    raw_count: int,
    kept_count: int,
    reason_counts: dict[str, int],
) -> str:
    if raw_count <= 0:
        return "OK · 0 ofertas en el listado."
    discarded_n = raw_count - kept_count
    parts = [f"{raw_count} en listado", f"{kept_count} guardada(s)"]
    if discarded_n > 0 and reason_counts:
        detail = ", ".join(
            f"{DISCARD_REASON_LABELS.get(k, k)}: {v}"
            for k, v in sorted(reason_counts.items(), key=lambda kv: -kv[1])
        )
        parts.append(f"{discarded_n} descartada(s) ({detail})")
    elif discarded_n > 0:
        parts.append(f"{discarded_n} descartada(s)")
    return "OK · " + " · ".join(parts)


def _enrich_keyword(keyword: str, filters: dict[str, Any]) -> str:
    modes = filters.get("work_modes") or []
    if len(modes) == 1:
        mode = modes[0]
        if mode == "remote":
            keyword = f"{keyword} remoto"
        elif mode == "hybrid":
            keyword = f"{keyword} hibrido"
        elif mode == "onsite":
            keyword = f"{keyword} presencial"
    levels = filters.get("experience_levels") or []
    if len(levels) == 1:
        level = levels[0]
        if level in ("entry", "internship"):
            keyword = f"{keyword} junior"
        elif level == "senior":
            keyword = f"{keyword} senior"
    return keyword
