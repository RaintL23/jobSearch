"""LinkedIn Jobs — PASO 1 (búsqueda) + PASO 2 (cards / detalle)."""

from __future__ import annotations

import logging
import re
from threading import Event
from typing import Any
from urllib.parse import quote_plus

from playwright.sync_api import Page

from backend.core.dates import parse_published_at, parse_relative_published
from backend.scraping.browser import (
    _first_text,
    _gentle_pause,
    _linkedin_session_ready,
    _new_page,
)
from backend.scraping.constants import (
    COUNTRY_META,
    LINKEDIN_F_E,
    LINKEDIN_F_TPR,
    LINKEDIN_F_WT,
    SAFETY_CAP,
    BrowserTarget,
)
from backend.scraping.filters import (
    _country_codes,
    _locations,
    _normalize_filters,
    _search_queries,
)

logger = logging.getLogger(__name__)

def _is_country_name_location(location: str) -> bool:
    """True si el texto es solo el nombre de un país conocido (redundant con geoId)."""
    low = location.strip().lower()
    if not low:
        return False
    return any(meta["name"].lower() == low for meta in COUNTRY_META.values())


def _linkedin_search_locations(
    locations: list[str],
    *,
    has_country: bool,
) -> list[str]:
    """
    Ubicaciones a iterar en LinkedIn Jobs.

    - ``""`` → usar geoId del país (búsqueda en todo el país).
    - texto → param ``location=`` (ciudad, Remoto LATAM, etc.).

    Si hay país explícito (filtro legacy), incluimos la búsqueda country-wide
    (geoId) y sumamos las ubicaciones de texto. Sin país, solo se usan las
    ubicaciones del textarea (separadas por comas/líneas).
    """
    explicit = [loc.strip() for loc in locations if loc and str(loc).strip()]
    if has_country:
        out: list[str] = [""]
        for loc in explicit:
            if _is_country_name_location(loc):
                continue
            if loc not in out:
                out.append(loc)
        return out[:8]
    return explicit[:6] if explicit else [""]

LINKEDIN_CARD_SELECTORS = (
    "li.jobs-search-results__list-item",
    "div.job-card-container",
    "div.job-card-list",
    "div.base-card",
    "div.base-search-card",
    "div.job-search-card",
    "ul.jobs-search__results-list li",
)

LINKEDIN_TITLE_SELECTORS = (
    "a.job-card-list__title",
    ".job-card-list__title",
    "a.job-card-container__link",
    ".base-search-card__title",
    "h3",
)

# Orden importa: primero los contenedores dedicados de empresa, luego el link
# a /company/ (muy confiable), y recién al final los genéricos (h4/subtitle).
LINKEDIN_COMPANY_SELECTORS = (
    ".job-card-container__company-name",
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__primary-description",
    "a.job-card-container__company-name",
    "span.job-card-container__primary-description",
    ".base-search-card__subtitle",
    ".base-search-card__subtitle a",
    "a[href*='/company/']",
    "h4",
)

# Link directo a la página de empresa: su texto es el nombre más fiable.
LINKEDIN_COMPANY_LINK_SELECTORS = (
    "a.job-card-container__company-name",
    ".artdeco-entity-lockup__subtitle a[href*='/company/']",
    "a[href*='/company/']",
    ".base-search-card__subtitle a[href*='/company/']",
)

LINKEDIN_LOCATION_SELECTORS = (
    ".job-card-container__metadata-item",
    ".job-card-container__metadata-wrapper li",
    ".job-search-card__location",
    ".base-search-card__metadata",
)

LINKEDIN_LINK_SELECTORS = (
    "a.job-card-list__title",
    "a.job-card-container__link",
    "a.base-card__full-link",
    "a[href*='/jobs/view/']",
)

LINKEDIN_DATE_SELECTORS = (
    "time",
    ".job-card-container__footer-item",
    ".job-search-card__listdate",
    ".base-search-card__metadata",
)

# PASO 2b · selectores de la PÁGINA DE DETALLE de una oferta (no del listado).
# El listado solo trae un snippet; abrir la oferta da requisitos/salario reales.
LINKEDIN_DETAIL_DESC_SELECTORS = (
    "div.jobs-description__content",
    "div.jobs-box__html-content",
    "article.jobs-description__container",
    "div.jobs-description-content__text",
    "#job-details",
    "div.show-more-less-html__markup",
    "section.description",
    "div.description__text",
)

LINKEDIN_DETAIL_LOCATION_SELECTORS = (
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".job-details-jobs-unified-top-card__bullet",
    ".jobs-unified-top-card__bullet",
    ".topcard__flavor--bullet",
    ".jobs-unified-top-card__primary-description",
)

# LinkedIn pagina de a 25 resultados vía ?start=; limitamos páginas por
# combinación de keyword/ubicación/país para no disparar rate-limit.
LINKEDIN_PAGE_SIZE = 25
LINKEDIN_MAX_PAGES = 4


def _query_first(card: Any, selectors: tuple[str, ...]) -> Any:
    for sel in selectors:
        try:
            el = card.query_selector(sel)
        except Exception:  # noqa: BLE001
            continue
        if el:
            return el
    return None


def _clean_company_text(raw: str) -> str:
    """
    Normaliza el nombre de empresa que LinkedIn suele ensuciar:
    - Líneas/palabras duplicadas por spans de accesibilidad ("Google\nGoogle").
    - Sufijos de ubicación tras separadores ("Empresa · Buenos Aires").
    - Ruido tipo "Verificación" / "con conexiones".
    """
    if not raw:
        return ""
    text = " ".join(raw.split())
    # Quitar sufijo de ubicación/metadatos tras separadores comunes.
    for sep in (" · ", " • ", " — ", " – ", " | "):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
    # LinkedIn repite el nombre (accesibilidad): "Acme Acme" o "Acme\nAcme".
    parts = [p.strip() for p in re.split(r"[\n\r]+", raw) if p.strip()]
    if parts:
        first = " ".join(parts[0].split())
        if first and (first == text or text.startswith(first)):
            text = first
    # Colapsar duplicado exacto "Acme Acme" → "Acme".
    half = len(text) // 2
    if half and text[:half].strip() == text[half:].strip():
        text = text[:half].strip()
    return text.strip(" ·•—–|").strip()


def _extract_linkedin_company(card: Any) -> str:
    """
    Devuelve el nombre de empresa de una card de LinkedIn Jobs.

    Prioriza el link a /company/ (texto = nombre real), luego selectores
    dedicados, y aplica limpieza para evitar duplicados/ubicaciones.
    """
    for sel in LINKEDIN_COMPANY_LINK_SELECTORS:
        try:
            el = card.query_selector(sel)
        except Exception:  # noqa: BLE001
            el = None
        if el:
            name = _clean_company_text((el.inner_text() or "").strip())
            if not name:
                # Algunos links traen el nombre solo en aria-label.
                name = _clean_company_text((el.get_attribute("aria-label") or "").strip())
            if name and len(name) >= 2:
                return name

    el = _query_first(card, LINKEDIN_COMPANY_SELECTORS)
    if el:
        name = _clean_company_text((el.inner_text() or "").strip())
        if name and len(name) >= 2:
            return name
    return ""


def _linkedin_query_params(
    keyword: str,
    location: str,
    filters: dict[str, Any],
    *,
    geo_id: str | None = None,
    start: int = 0,
) -> str:
    # --- PASO 1 · BÚSQUEDA (LinkedIn Jobs): tiempo + sortBy=DD (Latest) ---
    # urlencode no soporta bien multi-valores; armamos query a mano
    parts: list[str] = [f"keywords={quote_plus(keyword)}"]
    if geo_id:
        parts.append(f"geoId={quote_plus(geo_id)}")
    elif location:
        parts.append(f"location={quote_plus(location)}")

    # Antigüedad: si hay varias, usar la ventana más amplia
    posted_rank = {"24h": 1, "week": 2, "month": 3}
    posted_list = filters.get("posted_within") or []
    if posted_list:
        widest = max(posted_list, key=lambda x: posted_rank.get(x, 0))
        if widest in LINKEDIN_F_TPR:
            parts.append(f"f_TPR={LINKEDIN_F_TPR[widest]}")

    # Orden «Latest» (Date descending) — mismo criterio en #Hiring y Jobs.
    parts.append("sortBy=DD")

    for level in filters.get("experience_levels") or []:
        if level in LINKEDIN_F_E:
            parts.append(f"f_E={LINKEDIN_F_E[level]}")

    for mode in filters.get("work_modes") or []:
        if mode in LINKEDIN_F_WT:
            parts.append(f"f_WT={LINKEDIN_F_WT[mode]}")

    if start:
        parts.append(f"start={start}")

    return "&".join(parts)


def _linkedin_job_detail(page: Page, url: str) -> dict[str, Any] | None:
    """
    PASO 2b · abre la oferta de LinkedIn y extrae la DESCRIPCIÓN COMPLETA.

    LinkedIn Jobs solo muestra un snippet en el listado; el match, salario y
    skills se calculan mucho mejor con el texto real de la oferta. Best-effort:
    ante authwall o DOM sin descripción, devuelve None y se conserva el snippet.
    """
    if not url or "/jobs/view/" not in url and "/jobs/" not in url:
        return None
    try:
        page.goto(url, wait_until="domcontentloaded")
        _gentle_pause(0.4, 0.8)
    except Exception as exc:  # noqa: BLE001
        logger.debug("LinkedIn detalle navegación falló (%s): %s", url, exc)
        return None

    current = (page.url or "").lower()
    if "authwall" in current or "/login" in current:
        return None

    # Expandir "ver más" / "show more" de la descripción del detalle.
    for sel in (
        "button.show-more-less-html__button",
        'button[aria-label*="see more" i]',
        'button[aria-label*="ver más" i]',
        'button[aria-label*="ver mas" i]',
        "button.jobs-description__footer-button",
    ):
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click(timeout=800)
                _gentle_pause(0.15, 0.35)
        except Exception:  # noqa: BLE001
            pass

    description = _first_text(page, list(LINKEDIN_DETAIL_DESC_SELECTORS), long=True)
    location = _first_text(page, list(LINKEDIN_DETAIL_LOCATION_SELECTORS))
    if not description or len(description) < 60:
        return None

    out: dict[str, Any] = {"description": description[:10000]}
    if location:
        out["location"] = " ".join(location.split())[:120]
    return out


def scrape_linkedin(
    browser: BrowserTarget,
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
    cancel_event: Event | None = None,
) -> list[dict[str, Any]]:
    """
    LinkedIn Jobs — PASO 1 (búsqueda con f_TPR + sortBy=DD) y PASO 2 (cards).

    PASO 2b: por cada oferta se abre el detalle para traer la descripción
    completa (mejor precisión de match/salario/skills). Se puede desactivar
    con filters["linkedin_fetch_detail"] = False. PASO 3–4 en analysis/api.
    """
    filters = _normalize_filters(filters)
    fetch_detail = filters.get("linkedin_fetch_detail", True)
    queries = _search_queries(profile, filters)
    locations = _locations(profile, filters)
    explicit_countries = [c for c in (filters.get("countries") or []) if c in COUNTRY_META]
    has_explicit_locations = any(str(loc).strip() for loc in locations)

    # Sin dropdown de países: si hay ubicaciones de texto, buscamos solo por ellas.
    # Si no hay ubicaciones, caemos al país del perfil / default (geoId).
    if explicit_countries:
        countries: list[str | None] = explicit_countries[:8]
        has_country = True
    elif has_explicit_locations:
        countries = [None]
        has_country = False
    else:
        countries = list(_country_codes(profile, filters))
        has_country = True

    page = _new_page(browser, site="linkedin")
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    logged_in = _linkedin_session_ready()

    try:
        for country in countries:
            if len(jobs) >= SAFETY_CAP or (cancel_event and cancel_event.is_set()):
                break
            meta = COUNTRY_META.get(country) if country else None
            locs = _linkedin_search_locations(locations, has_country=has_country)
            for keyword in queries:
                if len(jobs) >= SAFETY_CAP or (cancel_event and cancel_event.is_set()):
                    break
                for loc in locs:
                    if len(jobs) >= SAFETY_CAP or (cancel_event and cancel_event.is_set()):
                        break
                    # "" → geoId del país (toda Argentina, no solo AMBA).
                    # Texto → location= libre (ciudad / Remoto LATAM / etc.).
                    location = loc.strip()
                    geo_id = meta["geo"] if meta and not location else None
                    display_location = location or (meta["name"] if meta else "")

                    for page_idx in range(LINKEDIN_MAX_PAGES):
                        if len(jobs) >= SAFETY_CAP or (
                            cancel_event and cancel_event.is_set()
                        ):
                            break
                        start = page_idx * LINKEDIN_PAGE_SIZE
                        url = (
                            "https://www.linkedin.com/jobs/search/?"
                            + _linkedin_query_params(
                                keyword,
                                display_location,
                                filters,
                                geo_id=geo_id,
                                start=start,
                            )
                        )
                        logger.info("LinkedIn: %s", url)
                        try:
                            raw_cards = _linkedin_extract_cards(
                                page, url, logged_in=logged_in
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "LinkedIn falló (%s / %s, start=%s): %s",
                                keyword,
                                display_location,
                                start,
                                exc,
                            )
                            raw_cards = []

                        new_on_page = 0
                        # Dedupe primero: no gastamos navegaciones de detalle en
                        # tarjetas repetidas de otras keywords/páginas.
                        fresh_cards = []
                        for job in raw_cards:
                            key = (
                                job.get("url")
                                or f"{job.get('title')}|{job.get('company')}"
                            )
                            if key in seen:
                                continue
                            seen.add(key)
                            new_on_page += 1
                            fresh_cards.append(job)

                        for job in fresh_cards:
                            if cancel_event and cancel_event.is_set():
                                break
                            # PASO 2b · entrar a la oferta para la descripción real.
                            if fetch_detail and job.get("url"):
                                detail = _linkedin_job_detail(page, job["url"])
                                if detail:
                                    job["description"] = detail["description"]
                                    if detail.get("location") and not job.get("location"):
                                        job["location"] = detail["location"]
                                _gentle_pause(0.3, 0.7)
                            # Guardamos toda la lista visible; filtros + motivos después.
                            jobs.append(job)
                            if len(jobs) >= SAFETY_CAP:
                                break

                        # Tras abrir detalles, la página quedó en una oferta:
                        # la próxima iteración vuelve a la URL de búsqueda.
                        _gentle_pause(0.25, 0.55)
                        # Seguir paginando mientras haya cards nuevas (no cortar
                        # por páginas "cortas" del layout guest/SPA).
                        if not raw_cards or new_on_page == 0:
                            break
    finally:
        try:
            page.context.close()
        except Exception:  # noqa: BLE001
            pass

    return jobs


def _linkedin_extract_cards(
    page: Page,
    url: str,
    *,
    logged_in: bool,
) -> list[dict[str, Any]]:
    """
    PASO 2 · EXTRACCIÓN CRUDA (LinkedIn Jobs).

    Solo datos del listado: título, empresa, ubicación, snippet, URL, fecha.
    Sin abrir detalle ni clasificar (eso es PASO 3–4 en analysis/api).
    """
    page.goto(url, wait_until="domcontentloaded")
    _gentle_pause(0.35, 0.7)

    current = page.url.lower()
    if "authwall" in current or ("login" in current and "jobs" not in current):
        return []

    cards = []
    for sel in LINKEDIN_CARD_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=4000)
        except Exception:  # noqa: BLE001
            pass
        cards = page.query_selector_all(sel)
        if cards:
            break

    if not cards and logged_in:
        # La SPA autenticada a veces hidrata la lista de resultados después
        # del primer scroll; reintentamos una vez.
        try:
            page.mouse.wheel(0, 1200)
        except Exception:  # noqa: BLE001
            pass
        _gentle_pause(0.6, 1.0)
        for sel in LINKEDIN_CARD_SELECTORS:
            cards = page.query_selector_all(sel)
            if cards:
                break

    jobs: list[dict[str, Any]] = []
    for card in cards:
        try:
            title_el = _query_first(card, LINKEDIN_TITLE_SELECTORS) or card.query_selector("a")
            loc_el = _query_first(card, LINKEDIN_LOCATION_SELECTORS)
            link_el = _query_first(card, LINKEDIN_LINK_SELECTORS)

            title = (title_el.inner_text() if title_el else "").strip()
            company = _extract_linkedin_company(card)
            location = (loc_el.inner_text() if loc_el else "").strip()
            href = (link_el.get_attribute("href") if link_el else "") or ""
            if href.startswith("/"):
                href = "https://www.linkedin.com" + href
            if "?" in href:
                href = href.split("?", 1)[0]
            if not title:
                continue

            published_at = None
            for sel in LINKEDIN_DATE_SELECTORS:
                date_el = card.query_selector(sel)
                if not date_el:
                    continue
                raw = date_el.get_attribute("datetime") or (date_el.inner_text() or "")
                published_at = parse_published_at(raw) or parse_relative_published(raw)
                if published_at:
                    break

            try:
                card_text = " ".join((card.inner_text() or "").split())[:2000]
            except Exception:  # noqa: BLE001
                card_text = ""

            jobs.append(
                {
                    "title": title[:200],
                    "company": (company or "Empresa no indicada")[:150],
                    "location": (location or "")[:120],
                    "description": (
                        f"Oferta en LinkedIn. Ubicación: {location or 'N/D'}. "
                        f"{card_text or 'Snippet del listado.'}"
                    )[:4000],
                    "url": href or url,
                    "source": "linkedin",
                    "published_at": published_at,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LinkedIn card skip: %s", exc)

    return jobs
