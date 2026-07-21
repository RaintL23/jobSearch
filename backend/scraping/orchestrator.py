"""Orquestador multi-fuente: scrapea en paralelo, merge y dedupe."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from threading import Event
from typing import Any

from playwright.sync_api import sync_playwright

from backend.auth.sessions import dedicated_profile_for_scrape_source
from backend.core.query_match import extract_location, matches_search_queries
from backend.scraping.browser import (
    _DEDICATED_PROFILE_LOCK,
    _launch_browser_for_source,
    _linkedin_session_ready,
)
from backend.scraping.constants import (
    ALL_SOURCES,
    SAFETY_CAP,
    SOURCE_LABELS,
    SOURCE_LATAM_RANK,
    ProgressCb,
    _BOARD_SCOPED_SOURCES,
)
from backend.scraping.filters import (
    _format_source_filter_message,
    _normalize_filters,
    _partition_jobs,
)
from backend.scraping.sources.api import SOURCE_SCRAPERS
from backend.scraping.sources.computrabajo import scrape_computrabajo
from backend.scraping.sources.linkedin import scrape_linkedin
from backend.scraping.sources.linkedin_hiring import (
    _linkedin_hiring_last_diag,
    scrape_linkedin_hiring,
)

logger = logging.getLogger(__name__)

def _job_dedupe_key(job: dict[str, Any]) -> str:
    """
    Clave de deduplicación. Si la URL es genérica (búsqueda de contenido /
    sin permalink de post), no usamos solo la URL: colapsaría 25 posts en 1.
    """
    url = str(job.get("url") or "").strip()
    low = url.lower()
    generic_url = (
        not url
        or "/search/results/" in low
        or low.rstrip("/").endswith("linkedin.com")
        or "/feed/" == low.rstrip("/").split("linkedin.com")[-1]
    )
    if not generic_url:
        return url
    desc = str(job.get("description") or "")[:120]
    return "|".join(
        [
            str(job.get("source") or ""),
            str(job.get("title") or "")[:120],
            str(job.get("company") or "")[:80],
            desc,
        ]
    )


def _emit(on_progress: ProgressCb | None, **payload: Any) -> None:
    if on_progress:
        try:
            on_progress(payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_progress error: %s", exc)


def _scrape_source_isolated(
    source: str,
    profile: dict[str, Any],
    filters: dict[str, Any],
    cancel_event: Event | None = None,
) -> list[dict[str, Any]]:
    if cancel_event and cancel_event.is_set():
        return []
    if source in SOURCE_SCRAPERS:
        return SOURCE_SCRAPERS[source](profile, filters)

    dedicated_profile = dedicated_profile_for_scrape_source(source)
    profile_guard = _DEDICATED_PROFILE_LOCK if dedicated_profile else nullcontext()
    with profile_guard:
        with sync_playwright() as p:
            if cancel_event and cancel_event.is_set():
                return []
            browser = _launch_browser_for_source(p, source, dedicated_profile)
            try:
                if source == "computrabajo":
                    return scrape_computrabajo(
                        browser,
                        profile,
                        filters=filters,
                        cancel_event=cancel_event,
                    )
                if source == "linkedin_hiring":
                    return scrape_linkedin_hiring(
                        browser,
                        profile,
                        filters=filters,
                        cancel_event=cancel_event,
                    )
                return scrape_linkedin(
                    browser,
                    profile,
                    filters=filters,
                    cancel_event=cancel_event,
                )
            finally:
                browser.close()


def _empty_source_status(source: str) -> dict[str, Any]:
    label = SOURCE_LABELS.get(source, source)
    return {
        "ok": False,
        "count": 0,
        "message": f"No se ejecutó el scrape de {label}.",
    }


def search_jobs(
    profile: dict[str, Any],
    max_jobs: int | None = None,  # ignorado; se mantienen todas hasta SAFETY_CAP
    filters: dict[str, Any] | None = None,
    on_progress: ProgressCb | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """
    Orquestador multi-fuente (PASO 1–2 por fuente en paralelo).

    Cada scraper hace BÚSQUEDA + EXTRACCIÓN CRUDA. Luego merge/dedupe.
    PASO 3–4 (revisión + clasificación + email) → main._analyze_raw_jobs.

    Devuelve {jobs, sources} con estado/disclaimer por fuente.
    on_progress recibe dicts {event, source?, message, count?} para UI en vivo.
    """
    del max_jobs  # compat
    filters = _normalize_filters(filters)
    active_sources = tuple(filters.get("sources") or ALL_SOURCES)
    if not active_sources:
        active_sources = ALL_SOURCES

    by_source: dict[str, list[dict[str, Any]]] = {s: [] for s in ALL_SOURCES}
    sources: dict[str, dict[str, Any]] = {s: _empty_source_status(s) for s in ALL_SOURCES}
    for skipped in ALL_SOURCES:
        if skipped not in active_sources:
            sources[skipped] = {
                "ok": True,
                "count": 0,
                "message": f"{SOURCE_LABELS.get(skipped, skipped)} omitida por filtro de fuentes.",
            }

    _emit(
        on_progress,
        event="progress",
        source="all",
        message=f"Iniciando búsqueda en {len(active_sources)} fuente(s)…",
    )

    pool = ThreadPoolExecutor(max_workers=min(4, max(1, len(active_sources))))
    futures = {
        pool.submit(
            _scrape_source_isolated,
            source,
            profile,
            filters,
            cancel_event,
        ): source
        for source in active_sources
    }
    pending = set(futures)
    try:
        while pending:
            if cancel_event and cancel_event.is_set():
                break
            try:
                fut = next(as_completed(pending, timeout=0.25))
            except TimeoutError:
                continue
            pending.remove(fut)
            source = futures[fut]
            label = SOURCE_LABELS.get(source, source)
            _emit(
                on_progress,
                event="progress",
                source=source,
                message=f"Procesando resultados de {label}…",
            )
            try:
                raw_jobs = fut.result() or []
                kept, discarded_sample, reason_counts = _partition_jobs(
                    raw_jobs, filters, source=source
                )
                by_source[source] = kept
                if raw_jobs:
                    msg = _format_source_filter_message(
                        raw_count=len(raw_jobs),
                        kept_count=len(kept),
                        reason_counts=reason_counts,
                    )
                    sources[source] = {
                        "ok": True,
                        "count": len(kept),
                        "raw_count": len(raw_jobs),
                        "discarded_by_reason": reason_counts,
                        "discarded_sample": discarded_sample,
                        "message": msg,
                    }
                    _emit(
                        on_progress,
                        event="source_done",
                        source=source,
                        ok=True,
                        count=len(kept),
                        raw_count=len(raw_jobs),
                        discarded_by_reason=reason_counts,
                        discarded_sample=discarded_sample,
                        message=f"{label}: {msg}",
                    )
                else:
                    if source == "linkedin_hiring":
                        if _linkedin_session_ready():
                            diag = _linkedin_hiring_last_diag or {}
                            bits = []
                            if diag.get("js_roots") or diag.get("cards_seen"):
                                bits.append(
                                    f"DOM={diag.get('js_roots') or diag.get('cards_seen')}"
                                )
                            if diag.get("voyager_posts"):
                                bits.append(f"red={diag['voyager_posts']}")
                            if diag.get("skip_permalink"):
                                bits.append(
                                    f"sin permalink={diag['skip_permalink']}"
                                )
                            if diag.get("skip_open_to_work"):
                                bits.append(
                                    f"open-to-work={diag['skip_open_to_work']}"
                                )
                            if diag.get("skip_intent"):
                                bits.append(
                                    f"sin intención hiring={diag['skip_intent']}"
                                )
                            detail = (
                                f" ({', '.join(bits)})" if bits else ""
                            )
                            msg = (
                                "LinkedIn #Hiring: la página mostró resultados pero "
                                f"ningún post quedó guardable{detail}. "
                                "Si ves posts en el browser y sigue en 0, renová la "
                                "sesión de LinkedIn."
                            )
                        else:
                            msg = (
                                "LinkedIn #Hiring sin sesión. Usá «Iniciar sesión» en LinkedIn "
                                "y volvé a buscar. Es una fuente experimental."
                            )
                    elif source == "linkedin":
                        if _linkedin_session_ready():
                            msg = (
                                "LinkedIn Jobs: sesión presente pero 0 ofertas (filtros muy "
                                "estrictos, anti-bot o cambio de HTML)."
                            )
                        else:
                            msg = (
                                "LinkedIn Jobs no devolvió ofertas. Causas frecuentes: muro de "
                                "login/authwall, bloqueo anti-bot, listado vacío o cambio de HTML."
                            )
                    elif source == "computrabajo":
                        msg = (
                            "Computrabajo no devolvió ofertas. Causas frecuentes: sin resultados "
                            "para la búsqueda/país, selectores HTML cambiados o bloqueo temporal."
                        )
                    elif source in ("remotive", "remoteok", "jobicy"):
                        posted = filters.get("posted_within") or []
                        delay_hint = (
                            " Estas fuentes publican con delay (>24 h); prueba «Última semana» o «Último mes»."
                            if "24h" in posted
                            else ""
                        )
                        msg = (
                            f"{label} no devolvió ofertas para estos filtros "
                            f"(keywords, antigüedad o API vacía).{delay_hint}"
                        )
                    else:
                        msg = (
                            f"{label} no devolvió ofertas para estos filtros "
                            "(sin match de keywords o API vacía)."
                        )
                    sources[source] = {"ok": False, "count": 0, "message": msg}
                    _emit(
                        on_progress,
                        event="source_done",
                        source=source,
                        ok=False,
                        count=0,
                        message=f"{label}: 0 ofertas. {msg}",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s no disponible: %s", source, exc)
                msg = f"{label} falló al scrapear: {exc}"
                sources[source] = {"ok": False, "count": 0, "message": msg}
                by_source[source] = []
                _emit(
                    on_progress,
                    event="source_done",
                    source=source,
                    ok=False,
                    count=0,
                    message=msg,
                )
    finally:
        cancelled = bool(cancel_event and cancel_event.is_set())
        if cancelled:
            for fut in pending:
                fut.cancel()
        pool.shutdown(wait=not cancelled, cancel_futures=cancelled)

    # Orden de fusión: popularidad LATAM (LinkedIn primero)
    order = tuple(
        sorted(ALL_SOURCES, key=lambda s: SOURCE_LATAM_RANK.get(s, 99))
    )
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    queries = list(filters.get("queries") or [])
    dropped_query = 0
    dropped_dup = 0
    for source in order:
        if cancel_event and cancel_event.is_set():
            break
        for job in by_source.get(source) or []:
            # Board-scoped ya pasó partition; otras fuentes: red de seguridad.
            if (
                source not in _BOARD_SCOPED_SOURCES
                and queries
                and not matches_search_queries(job, queries)
            ):
                dropped_query += 1
                continue
            if not job.get("location"):
                job["location"] = extract_location(job)
            key = _job_dedupe_key(job)
            if key in seen:
                dropped_dup += 1
                continue
            seen.add(key)
            collected.append(job)
            if len(collected) >= SAFETY_CAP:
                break
        if len(collected) >= SAFETY_CAP:
            break

    counts = {s: len(by_source[s]) for s in ALL_SOURCES}
    logger.info(
        "Total ofertas scrapadas: %d · %s (descartadas query=%s dup=%s)",
        len(collected),
        counts,
        dropped_query,
        dropped_dup,
    )
    discard_bits: list[str] = []
    if dropped_query:
        discard_bits.append(f"{dropped_query} fuera de tus textos")
    if dropped_dup:
        discard_bits.append(f"{dropped_dup} duplicadas")
    _emit(
        on_progress,
        event="progress",
        source="all",
        message=(
            f"Scraping listo · {len(collected)} oferta(s) únicas"
            + (f" ({', '.join(discard_bits)})" if discard_bits else "")
            + ". Procesando resultados…"
        ),
        count=len(collected),
    )
    return {"jobs": collected, "sources": sources}
