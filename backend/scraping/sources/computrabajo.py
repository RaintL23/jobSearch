"""Computrabajo — PASO 1 (URL) + PASO 2 (listado / detalle)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Page

from backend.core.dates import parse_published_at, parse_relative_published
from backend.core.utils import slugify
from backend.scraping.browser import (
    _first_text,
    _gentle_pause,
    _looks_blocked,
    _new_page,
)
from backend.scraping.constants import COUNTRY_META, SAFETY_CAP, BrowserTarget
from backend.scraping.filters import (
    _country_codes,
    _enrich_keyword,
    _normalize_filters,
    _search_queries,
)

logger = logging.getLogger(__name__)

def scrape_computrabajo(
    browser: BrowserTarget,
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Computrabajo — PASO 1 (URL de búsqueda) + PASO 2 (listado y detalle opcional).
    PASO 3–4: mismos que el resto (analyze_job_local / _analyze_raw_jobs).
    """
    filters = _normalize_filters(filters)
    countries = _country_codes(profile, filters)
    queries = _search_queries(profile, filters)

    page = _new_page(browser, site="computrabajo")
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        # --- PASO 1 · BÚSQUEDA (Computrabajo: slug por keyword/país) ---
        for country in countries:
            if len(jobs) >= SAFETY_CAP:
                break
            base = f"https://{COUNTRY_META[country]['ct']}.computrabajo.com"
            for keyword in queries:
                if len(jobs) >= SAFETY_CAP:
                    break
                kw = _enrich_keyword(keyword, filters)
                search_url = f"{base}/trabajo-de-{slugify(kw)}"
                logger.info("Computrabajo: %s", search_url)
                try:
                    resp = page.goto(search_url, wait_until="domcontentloaded")
                    _gentle_pause(0.35, 0.7)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Computrabajo navegación falló: %s", exc)
                    continue

                status = resp.status if resp else 0
                list_text = ""
                try:
                    list_text = page.inner_text("body") or ""
                except Exception:  # noqa: BLE001
                    pass
                if status >= 400 or _looks_blocked(list_text[:1500]):
                    logger.warning(
                        "Computrabajo listado bloqueado (HTTP %s) en %s",
                        status,
                        search_url,
                    )
                    continue

                # --- PASO 2 · EXTRACCIÓN CRUDA (listado; detalle solo si aporta) ---
                # Preferir tarjetas del listado (evita 403 al martillar detalles)
                cards = _parse_computrabajo_list_cards(page, base)
                if not cards:
                    logger.info("Computrabajo: sin tarjetas en listado, sin detalle.")
                    continue

                for card in cards:
                    if len(jobs) >= SAFETY_CAP:
                        break
                    url = card.get("url") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)

                    job = dict(card)
                    # Enriquecer con detalle solo si el listado no alcanza; si hay 403, conservar card
                    try:
                        detailed = _parse_computrabajo_detail(page, url)
                        if detailed:
                            # Mantener published_at del listado si el detalle no lo trae
                            if not detailed.get("published_at") and job.get("published_at"):
                                detailed["published_at"] = job["published_at"]
                            job = detailed
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Computrabajo detalle omitido (%s): %s", url, exc)

                    if _looks_blocked(
                        f"{job.get('title', '')} {job.get('description', '')}"
                    ):
                        logger.warning("Computrabajo descartó página de error: %s", url)
                        continue

                    # Guardamos todo el listado; el filtrado con motivos es post-scrape.
                    if job:
                        jobs.append(job)
                    _gentle_pause(0.25, 0.55)
    finally:
        try:
            page.context.close()
        except Exception:  # noqa: BLE001
            pass

    return jobs


def _parse_computrabajo_list_cards(page: Page, base: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    articles = page.query_selector_all("article.box_offer") or page.query_selector_all(
        "article"
    )
    for art in articles:
        try:
            link = art.query_selector("a.js-o-link") or art.query_selector(
                "a[href*='/ofertas-de-trabajo/']"
            )
            if not link:
                continue
            href = (link.get_attribute("href") or "").strip()
            if not href:
                continue
            full = urljoin(base, href.split("#", 1)[0])
            title = (link.inner_text() or "").strip()
            company_el = art.query_selector("a[href*='/empresas/']") or art.query_selector(
                "p a.fc_base"
            )
            company = (company_el.inner_text() if company_el else "").strip()
            text = (art.inner_text() or "").strip()
            published_at = parse_relative_published(text) or parse_published_at(text)

            if not title or _looks_blocked(title) or _looks_blocked(text[:500]):
                continue

            cards.append(
                {
                    "title": title[:200],
                    "company": (company or "Empresa no indicada")[:150],
                    "description": (
                        f"Oferta en Computrabajo (listado). {text[:2500]}"
                    ).strip()[:10000],
                    "url": full,
                    "source": "computrabajo",
                    "published_at": published_at,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Computrabajo card skip: %s", exc)
    return cards


def _parse_computrabajo_detail(page: Page, url: str) -> dict[str, Any] | None:
    resp = page.goto(url, wait_until="domcontentloaded")
    _gentle_pause(0.2, 0.4)

    status = resp.status if resp else 0
    body = ""
    try:
        body = page.inner_text("body") if page.query_selector("body") else ""
    except Exception:  # noqa: BLE001
        body = ""

    if status >= 400 or _looks_blocked(body[:2000]):
        logger.warning(
            "Computrabajo detalle bloqueado (HTTP %s): %s",
            status,
            url,
        )
        return None

    title = _first_text(page, ["h1", ".box_detail h1", "[class*='title'] h1", "header h1"])
    company = _first_text(
        page,
        ["a[href*='/empresas/']", ".fc_base.mt5", "[class*='company']", "h1 + p a"],
    )
    description = _first_text(
        page,
        ["#jobDescription", ".box_detail .mb40", "[class*='description']", "article", "main"],
        long=True,
    )

    if _looks_blocked(f"{title} {description}"):
        return None

    if not title and not description:
        # No usar body completo: suele ser chrome del sitio o páginas de error
        return None

    if not title:
        title = "Oferta Computrabajo"

    published_at = _extract_page_published_at(page)

    return {
        "title": title.strip()[:200],
        "company": (company or "Empresa no indicada").strip()[:150],
        "description": (description or "").strip()[:10000],
        "url": url.split("#", 1)[0],
        "source": "computrabajo",
        "published_at": published_at,
    }


def _extract_page_published_at(page: Page) -> str | None:
    for sel in ("time[datetime]", "time", "[datetime]", "[data-date]"):
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            raw = (
                el.get_attribute("datetime")
                or el.get_attribute("data-date")
                or (el.inner_text() or "")
            )
            parsed = parse_published_at(raw) or parse_relative_published(raw)
            if parsed:
                return parsed
        except Exception:  # noqa: BLE001
            continue

    try:
        body = (page.inner_text("body") or "")[:2500]
    except Exception:  # noqa: BLE001
        return None
    m = re.search(
        r"(hace\s+\d+\s+(?:minuto|hora|d[ií]a|semana|mes)s?"
        r"|\d+\s+(?:minutes?|hours?|days?|weeks?|months?)\s+ago"
        r"|publicado\s*[:\-]?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
        r"|ayer|hoy|yesterday|today)",
        body,
        re.I,
    )
    if m:
        return parse_published_at(m.group(0)) or parse_relative_published(m.group(0))
    return None
