"""
Scraping de ofertas con Playwright + APIs públicas.

Pipeline unificado (todas las fuentes):
  PASO 1–2 · este paquete (búsqueda + extracción cruda)
  PASO 3   · backend.analysis.local.analyze_job_local
  PASO 4   · backend.api.app (_analyze_raw_jobs)
"""

from backend.scraping.constants import (
    ALL_SOURCES,
    API_SOURCES,
    PLAYWRIGHT_SOURCES,
    SOURCE_LABELS,
    SOURCE_LATAM_RANK,
)
from backend.scraping.filters import (
    _discard_reason,
    _format_source_filter_message,
    _partition_jobs,
)
from backend.scraping.orchestrator import search_jobs
from backend.scraping.sources.linkedin import (
    _is_country_name_location,
    _linkedin_search_locations,
)
from backend.scraping.sources.linkedin_hiring import (
    _extract_hiring_permalink,
    _linkedin_hiring_intent,
    is_linkedin_hiring_permalink,
)

__all__ = [
    "ALL_SOURCES",
    "API_SOURCES",
    "PLAYWRIGHT_SOURCES",
    "SOURCE_LABELS",
    "SOURCE_LATAM_RANK",
    "search_jobs",
    "is_linkedin_hiring_permalink",
    "_discard_reason",
    "_format_source_filter_message",
    "_partition_jobs",
    "_is_country_name_location",
    "_linkedin_search_locations",
    "_extract_hiring_permalink",
    "_linkedin_hiring_intent",
]
