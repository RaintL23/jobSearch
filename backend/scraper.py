"""
Scraping de ofertas con Playwright (headless) + APIs públicas.

Soporta múltiples textos de búsqueda y ubicaciones.
Sin tope artificial de ofertas (sí hay SAFETY_CAP para no colgar el proceso).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from urllib.parse import quote_plus, urljoin

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from backend.api_sources import SOURCE_SCRAPERS
from backend.auth_sessions import (
    dedicated_profile_for_scrape_source,
    preferred_system_channel,
    storage_state_for_scrape_source,
)
from backend.config import get_settings
from backend.date_utils import parse_published_at, parse_relative_published, within_posted_window
from backend.query_match import extract_location, matches_search_queries
from backend.utils import slugify

logger = logging.getLogger(__name__)

# Tope de seguridad (páginas públicas); no se expone como filtro de UI
SAFETY_CAP = get_settings().scrape_safety_cap

ProgressCb = Callable[[dict[str, Any]], None]
BrowserTarget = Browser | BrowserContext

# Chromium bloquea un user-data-dir mientras está en uso. Las fuentes Playwright
# corren en paralelo, por eso comparten este lock al usar el perfil JobSearch.
_DEDICATED_PROFILE_LOCK = Lock()

SOURCE_LABELS = {
    "computrabajo": "Computrabajo",
    "linkedin": "LinkedIn Jobs",
    "linkedin_hiring": "LinkedIn #Hiring",
    "getonboard": "GetOnBoard",
    "remotive": "Remotive",
    "remoteok": "RemoteOK",
    "jobicy": "Jobicy",
}

# Popularidad en comunidad tech LATAM (menor = más prioritario al ordenar)
SOURCE_LATAM_RANK: dict[str, int] = {
    "linkedin": 0,
    "getonboard": 1,
    "computrabajo": 2,
    "linkedin_hiring": 3,
    "remotive": 4,
    "jobicy": 5,
    "remoteok": 6,
}

PLAYWRIGHT_SOURCES = ("computrabajo", "linkedin", "linkedin_hiring")
API_SOURCES = tuple(SOURCE_SCRAPERS.keys())
ALL_SOURCES = PLAYWRIGHT_SOURCES + API_SOURCES

USER_AGENT = get_settings().user_agent

# Mapeo país ISO2 → nombre LinkedIn + dominio Computrabajo
COUNTRY_META: dict[str, dict[str, str]] = {
    "mx": {"name": "Mexico", "ct": "mx", "geo": "103323778"},
    "co": {"name": "Colombia", "ct": "co", "geo": "100876405"},
    "ar": {"name": "Argentina", "ct": "ar", "geo": "100446943"},
    "pe": {"name": "Peru", "ct": "pe", "geo": "102890719"},
    "cl": {"name": "Chile", "ct": "cl", "geo": "104621616"},
    "ec": {"name": "Ecuador", "ct": "ec", "geo": "106373116"},
    "uy": {"name": "Uruguay", "ct": "uy", "geo": "100867946"},
    "ve": {"name": "Venezuela", "ct": "ve", "geo": "101490751"},
    "cr": {"name": "Costa Rica", "ct": "cr", "geo": "101174742"},
    "pa": {"name": "Panama", "ct": "pa", "geo": "100808673"},
    "gt": {"name": "Guatemala", "ct": "gt", "geo": "100247235"},
    "bo": {"name": "Bolivia", "ct": "bo", "geo": "104383590"},
    "py": {"name": "Paraguay", "ct": "py", "geo": "104065273"},
    "do": {"name": "Dominican Republic", "ct": "do", "geo": "109705310"},
    "hn": {"name": "Honduras", "ct": "hn", "geo": "101733784"},
    "sv": {"name": "El Salvador", "ct": "sv", "geo": "106693272"},
    "ni": {"name": "Nicaragua", "ct": "ni", "geo": "105531867"},
    "cu": {"name": "Cuba", "ct": "cu", "geo": "106670759"},
    "pr": {"name": "Puerto Rico", "ct": "pr", "geo": "105556783"},
}

LINKEDIN_F_TPR = {
    "24h": "r86400",
    "week": "r604800",
    "month": "r2592000",
}

LINKEDIN_F_E = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid": "4",
    "senior": "4",
    "director": "5",
}

LINKEDIN_F_WT = {
    "onsite": "1",
    "remote": "2",
    "hybrid": "3",
}

WORK_MODE_KEYWORDS = {
    "remote": ["remoto", "remote", "teletrabajo", "home office", "work from home"],
    "hybrid": ["híbrido", "hibrido", "hybrid"],
    "onsite": ["presencial", "on-site", "onsite", "oficina"],
}

EXPERIENCE_KEYWORDS = {
    "internship": ["internship", "becario", "pasante", "prácticas", "practicas"],
    "entry": ["junior", "entry", "jr", "trainee", "sin experiencia"],
    "associate": ["semi", "ssr", "mid-level", "intermedio"],
    "mid": ["semi", "ssr", "mid", "intermedio", "pleno"],
    "senior": ["senior", "sr", "lead", "principal"],
    "director": ["director", "head of", "gerente"],
}


def _gentle_pause(lo: float = 0.15, hi: float = 0.45) -> None:
    time.sleep(random.uniform(lo, hi))


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


def _new_page(browser: BrowserTarget, *, site: str | None = None) -> Page:
    """
    Crea página. Si `site` es linkedin/computrabajo y hay storage_state local,
    reutiliza la sesión (sin contraseña en el proyecto).
    """
    ctx_kwargs: dict[str, Any] = {
        "locale": "es-AR",
        "viewport": {"width": 1365, "height": 900},
    }
    # linkedin_hiring comparte sesión con linkedin
    state_key = "linkedin" if site == "linkedin_hiring" else site
    state = storage_state_for_scrape_source(state_key or "") if site else None

    # Con sesión de LinkedIn usamos Chrome/Edge real (headed); forzar un
    # User-Agent/Accept-Language sintéticos ahí desincroniza el navegador real
    # de sus propios Client Hints (Sec-CH-UA) y es justo el tipo de señal que
    # el anti-bot de LinkedIn usa para invalidar la sesión y mostrar contenido
    # de invitado aunque las cookies (li_at) sean válidas. Solo forzamos estos
    # headers cuando NO hay navegador real detrás (headless Chromium genérico).
    real_browser_session = bool(state) and state_key in ("linkedin",)
    if not real_browser_session:
        ctx_kwargs["user_agent"] = USER_AGENT
        ctx_kwargs["extra_http_headers"] = {
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }

    if state and isinstance(browser, Browser):
        ctx_kwargs["storage_state"] = state
        logger.info("Usando sesión guardada para %s (%s)", site, state)

    if isinstance(browser, BrowserContext):
        context = browser
        logger.info("Usando directamente el perfil persistente para %s", site)
        # Migración de sesiones creadas por versiones anteriores: si el perfil
        # todavía no tiene la cookie de LinkedIn, sembrarla una sola vez desde
        # el storage_state que produjo el login. Después Chromium la persiste.
        if state and state_key == "linkedin":
            try:
                cookie_names = {
                    str(cookie.get("name") or "")
                    for cookie in context.cookies("https://www.linkedin.com")
                }
                if "li_at" not in cookie_names:
                    saved = json.loads(Path(state).read_text(encoding="utf-8"))
                    cookies = saved.get("cookies") or []
                    if cookies:
                        context.add_cookies(cookies)
                        logger.info("Sesión anterior migrada al perfil persistente")
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo migrar la sesión al perfil: %s", exc)
    else:
        context = browser.new_context(**ctx_kwargs)
    page = context.new_page()
    page.set_default_timeout(30000)
    # Reduce señales de automatización
    try:
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
    except Exception:  # noqa: BLE001
        pass
    return page


def _linkedin_session_ready() -> bool:
    return bool(storage_state_for_scrape_source("linkedin"))


def _looks_like_linkedin_authwall(url: str) -> bool:
    low = (url or "").lower()
    if "authwall" in low or "checkpoint" in low or "challenge" in low:
        return True
    if "/uas/login" in low or "/login" in low:
        # /feed/login no es típico; login real sí
        if "/feed" in low or "/jobs" in low or "/search" in low:
            return False
        return True
    return False


def _launch_browser_for_source(
    p: Any,
    source: str,
    dedicated_profile: tuple[str, str] | None = None,
) -> BrowserTarget:
    """
    LinkedIn con sesión: usa Edge/Chrome headed (headless suele disparar authwall
    aunque las cookies sean válidas).
    """
    if dedicated_profile:
        channel, profile_dir = dedicated_profile
        logger.info(
            "Abriendo %s con el perfil persistente JobSearch (%s)",
            source,
            channel,
        )
        return p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel=channel,
            headless=False,
            locale="es-AR",
            viewport={"width": 1365, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )

    linkedin_like = source in ("linkedin", "linkedin_hiring")
    has_session = linkedin_like and _linkedin_session_ready()
    # site="linkedin": prioriza el navegador (chrome/msedge) con el que
    # efectivamente se guardó la sesión, en vez de asumir Chrome por defecto.
    channel = preferred_system_channel(site="linkedin") if has_session else None
    headed = bool(has_session)

    launch_kwargs: dict[str, Any] = {
        "headless": not headed,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    if channel:
        launch_kwargs["channel"] = channel
        logger.info(
            "Lanzando %s headed=%s channel=%s (sesión LinkedIn)",
            source,
            headed,
            channel,
        )
    elif headed:
        logger.info("Lanzando %s headed Chromium (sesión LinkedIn)", source)

    return p.chromium.launch(**launch_kwargs)


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
        from backend.api_sources import _posted_filters_for_source

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


DISCARD_REASON_LABELS = {
    "query": "texto de búsqueda",
    "date": "antigüedad",
    "work_mode": "modalidad",
    "experience": "experiencia",
    "country": "país",
    "language": "idioma",
    "salary": "salario",
    "duplicate": "duplicada",
}


# Fuentes cuyo board ya filtró por keywords en la URL/API: no re-matamos
# por matches_search_queries (el listado visible es la verdad).
_BOARD_SCOPED_SOURCES = frozenset({"linkedin", "linkedin_hiring"})


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


# ---------------------------------------------------------------------------
# Computrabajo
# ---------------------------------------------------------------------------

_BLOCKED_MARKERS = (
    "403 forbidden",
    "403 error",
    "access denied",
    "access forbidden",
    "request blocked",
    "attention required",
    "just a moment",
    "cf-browser-verification",
    "enable javascript and cookies",
    "sorry, you have been blocked",
)


def _looks_blocked(text: str) -> bool:
    low = (text or "").lower()
    if not low.strip():
        return False
    if any(m in low for m in _BLOCKED_MARKERS):
        return True
    # Página de error corta típica
    if "forbidden" in low and len(low) < 400:
        return True
    return False


def scrape_computrabajo(
    browser: BrowserTarget,
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    filters = _normalize_filters(filters)
    countries = _country_codes(profile, filters)
    queries = _search_queries(profile, filters)

    page = _new_page(browser, site="computrabajo")
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
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


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------

# LinkedIn tiene dos layouts de DOM completamente distintos según el estado
# de sesión de la página que se scrapea:
#   - Público/anónimo (SEO, sin login): div.base-card, base-search-card__title…
#   - Con sesión activa (SPA autenticada): div.job-card-container,
#     job-card-list__title, job-card-container__metadata-item…
# Si solo se soportan los selectores públicos, una sesión logueada headed
# (que es justamente el camino recomendado para evitar el authwall) devuelve
# 0 o 1 tarjetas "por casualidad" sin importar la búsqueda.
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

LINKEDIN_COMPANY_SELECTORS = (
    ".job-card-container__primary-description",
    ".job-card-container__company-name",
    "h4",
    ".base-search-card__subtitle",
    "a[href*='/company/']",
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


def _linkedin_query_params(
    keyword: str,
    location: str,
    filters: dict[str, Any],
    *,
    geo_id: str | None = None,
    start: int = 0,
) -> str:
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

    for level in filters.get("experience_levels") or []:
        if level in LINKEDIN_F_E:
            parts.append(f"f_E={LINKEDIN_F_E[level]}")

    for mode in filters.get("work_modes") or []:
        if mode in LINKEDIN_F_WT:
            parts.append(f"f_WT={LINKEDIN_F_WT[mode]}")

    if start:
        parts.append(f"start={start}")

    return "&".join(parts)


def scrape_linkedin(
    browser: BrowserTarget,
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    filters = _normalize_filters(filters)
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
            if len(jobs) >= SAFETY_CAP:
                break
            meta = COUNTRY_META.get(country) if country else None
            locs = _linkedin_search_locations(locations, has_country=has_country)
            for keyword in queries:
                if len(jobs) >= SAFETY_CAP:
                    break
                for loc in locs:
                    if len(jobs) >= SAFETY_CAP:
                        break
                    # "" → geoId del país (toda Argentina, no solo AMBA).
                    # Texto → location= libre (ciudad / Remoto LATAM / etc.).
                    location = loc.strip()
                    geo_id = meta["geo"] if meta and not location else None
                    display_location = location or (meta["name"] if meta else "")

                    for page_idx in range(LINKEDIN_MAX_PAGES):
                        if len(jobs) >= SAFETY_CAP:
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
                        for job in raw_cards:
                            key = (
                                job.get("url")
                                or f"{job.get('title')}|{job.get('company')}"
                            )
                            if key in seen:
                                continue
                            seen.add(key)
                            new_on_page += 1
                            # Guardamos toda la lista visible; filtros + motivos después.
                            jobs.append(job)
                            if len(jobs) >= SAFETY_CAP:
                                break

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
    """Devuelve las tarjetas crudas (sin aplicar filtros blandos ni de fecha)."""
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
            company_el = _query_first(card, LINKEDIN_COMPANY_SELECTORS)
            loc_el = _query_first(card, LINKEDIN_LOCATION_SELECTORS)
            link_el = _query_first(card, LINKEDIN_LINK_SELECTORS)

            title = (title_el.inner_text() if title_el else "").strip()
            company = (company_el.inner_text() if company_el else "").strip()
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


def _first_text(page: Page, selectors: list[str], *, long: bool = False) -> str:
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            text = (el.inner_text() or "").strip()
            if text:
                return text if long else text.split("\n")[0].strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


# ---------------------------------------------------------------------------
# LinkedIn #Hiring (posts del feed — best-effort)
# ---------------------------------------------------------------------------

LINKEDIN_HIRING_CARD_SELECTORS = (
    "div.feed-shared-update-v2",
    'div[data-id^="urn:li:activity"]',
    'div[data-urn*="activity"]',
    'div[data-urn*="ugcPost"]',
    'div[role="listitem"][componentkey]',
    "div.update-components-actor",
    "div.search-results-container div.reusable-search__result-container",
    "div.reusable-search__result-container",
    "li.reusable-search__result-container",
)

LINKEDIN_HIRING_TEXT_SELECTORS = (
    '[data-testid="expandable-text-box"]',
    ".update-components-text",
    ".feed-shared-update-v2__description",
    ".feed-shared-text",
    ".break-words",
)

LINKEDIN_HIRING_HINTS = (
    "#hiring",
    "hiring",
    "we're hiring",
    "we are hiring",
    "we're looking",
    "looking for",
    "join our team",
    "open role",
    "open position",
    "job opening",
    "estamos contratando",
    "estamos buscando",
    "se busca",
    "buscamos",
    "contratando",
    "vacante",
    "oportunidad laboral",
    "sumate",
    "sumáte",
    "incorporamos",
)

LINKEDIN_HIRING_SOFT_CAP = 25


def _linkedin_hiring_date_param(filters: dict[str, Any]) -> str | None:
    """Filtro de fecha para búsqueda de contenido (si LinkedIn lo acepta)."""
    posted_rank = {"24h": 1, "week": 2, "month": 3}
    posted_list = filters.get("posted_within") or []
    if not posted_list:
        return None
    widest = max(posted_list, key=lambda x: posted_rank.get(x, 0))
    mapping = {
        "24h": '["past-24h"]',
        "week": '["past-week"]',
        "month": '["past-month"]',
    }
    return mapping.get(widest)


def _linkedin_hiring_expand(page: Page) -> None:
    """Click en 'ver más' / expandir texto truncado de posts."""
    for sel in (
        'button[aria-label*="see more" i]',
        'button[aria-label*="ver más" i]',
        'button[aria-label*="ver mas" i]',
        "button.feed-shared-inline-show-more-text__see-more-less-toggle",
        '[data-testid="expandable-text-button"]',
        "button.inline-show-more-text__button",
    ):
        try:
            buttons = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for btn in buttons[:12]:
            try:
                if btn.is_visible():
                    btn.click(timeout=800)
            except Exception:  # noqa: BLE001
                pass


def _linkedin_hiring_collect_cards(page: Page) -> list[Any]:
    """Espera hidratación SPA + scroll y devuelve nodos de posts (legacy o SDUI)."""
    cards: list[Any] = []
    for sel in LINKEDIN_HIRING_CARD_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=3500)
        except Exception:  # noqa: BLE001
            pass
        try:
            found = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            found = []
        if found:
            cards = found
            break

    # Varios scrolls: el resultado de contenido carga en lazy chunks
    for _ in range(3):
        try:
            page.mouse.wheel(0, 2200)
        except Exception:  # noqa: BLE001
            pass
        _gentle_pause(0.55, 0.95)
        _linkedin_hiring_expand(page)
        _gentle_pause(0.25, 0.45)
        if not cards:
            for sel in LINKEDIN_HIRING_CARD_SELECTORS:
                try:
                    found = page.query_selector_all(sel)
                except Exception:  # noqa: BLE001
                    found = []
                if found:
                    cards = found
                    break

    if cards:
        # Refrescar lista tras scrolls
        for sel in LINKEDIN_HIRING_CARD_SELECTORS:
            try:
                found = page.query_selector_all(sel)
            except Exception:  # noqa: BLE001
                found = []
            if found and len(found) >= len(cards):
                cards = found
                break
        return cards

    # Fallback SDUI: cajas de texto expandibles sin wrapper clásico
    try:
        texts = page.query_selector_all('[data-testid="expandable-text-box"]')
    except Exception:  # noqa: BLE001
        texts = []
    return list(texts)


def _linkedin_hiring_card_text(card: Any) -> str:
    for sel in LINKEDIN_HIRING_TEXT_SELECTORS:
        try:
            el = card.query_selector(sel)
        except Exception:  # noqa: BLE001
            el = None
        if el:
            try:
                t = (el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                t = ""
            if t and len(t) >= 20:
                return t
    try:
        return (card.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _linkedin_hiring_intent(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in LINKEDIN_HIRING_HINTS)


def _linkedin_hiring_query_ok(
    text: str,
    queries: list[str],
    *,
    search_already_scoped: bool,
) -> bool:
    """
    Si la URL de LinkedIn ya incluye el keyword, confiamos más en el ranking
    de LinkedIn y solo pedimos intención de hiring (evita descartar posts
    en español cuando el texto de búsqueda está en inglés, etc.).
    """
    if not queries or search_already_scoped:
        return True
    from backend.query_match import normalize_query_text, query_tokens

    probe = {
        "title": text.splitlines()[0][:200] if text.splitlines() else "",
        "company": "",
        "description": text[:4000],
    }
    if matches_search_queries(probe, queries):
        return True
    blob = normalize_query_text(text)
    for q in queries:
        toks = [t for t in query_tokens(q) if len(t) >= 3]
        if any(t in blob for t in toks):
            return True
    return False


def scrape_linkedin_hiring(
    browser: BrowserTarget,
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Posts de contenido LinkedIn con #Hiring / hiring.

    Requiere sesión. En headless LinkedIn suele mostrar authwall aunque
    las cookies sean válidas; el launcher usa headed + Edge/Chrome si hay sesión.
    """
    filters = _normalize_filters(filters)
    queries = _search_queries(profile, filters)
    page = _new_page(browser, site="linkedin_hiring")
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    hit_authwall = False
    last_url = ""
    cards_seen = 0
    skipped_no_intent = 0
    skipped_query = 0

    try:
        # Warm-up: entrar al feed con la sesión antes de buscar
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            _gentle_pause(1.0, 1.6)
            last_url = page.url or ""
            if _looks_like_linkedin_authwall(last_url):
                hit_authwall = True
                logger.warning(
                    "LinkedIn #Hiring: authwall al abrir /feed/ (url=%s). "
                    "Sesión inválida o bloqueo anti-bot.",
                    last_url,
                )
                return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("LinkedIn #Hiring warm-up falló: %s", exc)

        date_param = _linkedin_hiring_date_param(filters)

        for keyword in queries[:4]:
            if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP:
                break
            # Búsquedas de contenido (logueado). El feed de hashtag suele
            # redirigir; content search es más estable.
            terms = [
                f"#Hiring {keyword}",
                f"hiring {keyword}",
                f"estamos contratando {keyword}",
                f"buscamos {keyword}",
            ]
            for term in terms:
                if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP:
                    break
                parts = [
                    f"keywords={quote_plus(term)}",
                    "origin=GLOBAL_SEARCH_HEADER",
                ]
                if date_param:
                    parts.append(f"datePosted={quote_plus(date_param)}")
                url = (
                    "https://www.linkedin.com/search/results/content/?"
                    + "&".join(parts)
                )
                logger.info("LinkedIn #Hiring: %s", url)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    _gentle_pause(1.0, 1.5)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LinkedIn #Hiring navegación falló: %s", exc)
                    continue

                last_url = page.url or ""
                if _looks_like_linkedin_authwall(last_url):
                    hit_authwall = True
                    logger.warning("LinkedIn #Hiring authwall en %s", last_url)
                    continue

                cards = _linkedin_hiring_collect_cards(page)
                cards_seen += len(cards)
                logger.info("LinkedIn #Hiring: %d cards para %r", len(cards), term)

                for card in cards[:40]:
                    try:
                        text = _linkedin_hiring_card_text(card)
                        if not text or len(text) < 30:
                            continue
                        if not _linkedin_hiring_intent(text):
                            skipped_no_intent += 1
                            continue
                        if not _linkedin_hiring_query_ok(
                            text, queries, search_already_scoped=True
                        ):
                            skipped_query += 1
                            continue

                        href = ""
                        try:
                            urn = (
                                card.get_attribute("data-urn")
                                or card.get_attribute("data-id")
                                or ""
                            )
                        except Exception:  # noqa: BLE001
                            urn = ""
                        if urn and "activity:" in urn:
                            # Permalink sintético estable cuando el DOM SDUI
                            # no expone <a href="/posts/...">
                            act = urn.split("activity:")[-1].split(",")[0].strip()
                            if act.isdigit():
                                href = f"https://www.linkedin.com/feed/update/urn:li:activity:{act}"

                        if not href:
                            link_el = (
                                card.query_selector("a[href*='/posts/']")
                                or card.query_selector("a[href*='/feed/update/']")
                                or card.query_selector("a[href*='/recent-activity/']")
                                or card.query_selector("a.app-aware-link[href*='/in/']")
                            )
                            href = (
                                (link_el.get_attribute("href") if link_el else "")
                                or ""
                            )
                        if href and href.startswith("/"):
                            href = "https://www.linkedin.com" + href
                        if "?" in href:
                            href = href.split("?", 1)[0]
                        key = href or text[:160]
                        if key in seen:
                            continue
                        seen.add(key)

                        actor = (
                            card.query_selector(".update-components-actor__name")
                            or card.query_selector(".feed-shared-actor__name")
                            or card.query_selector(".update-components-actor__title")
                            or card.query_selector('a[href*="/in/"]')
                            or card.query_selector('a[href*="/company/"]')
                        )
                        company = (
                            (actor.inner_text() if actor else "").strip()
                            or "Publicación LinkedIn"
                        )
                        # Limpiar saltos raros del actor
                        company = " ".join(company.split())[:150] or "Publicación LinkedIn"

                        title_line = next(
                            (
                                ln.strip()
                                for ln in text.splitlines()
                                if ln.strip()
                                and any(
                                    h in ln.lower()
                                    for h in (
                                        "hiring",
                                        "contrat",
                                        "buscamos",
                                        "vacante",
                                        "looking",
                                        "sumate",
                                        "sumáte",
                                    )
                                )
                            ),
                            text.splitlines()[0].strip()
                            if text.splitlines()
                            else keyword,
                        )

                        time_el = card.query_selector("time")
                        published_at = None
                        if time_el:
                            raw = time_el.get_attribute("datetime") or (
                                time_el.inner_text() or ""
                            )
                            published_at = parse_published_at(
                                raw
                            ) or parse_relative_published(raw)

                        job = {
                            "title": f"[#Hiring] {title_line}"[:200],
                            "company": company[:150],
                            "location": "",
                            "description": (
                                "Post de LinkedIn con intención de contratación. "
                                f"Búsqueda: {keyword}.\n\n{text[:4000]}"
                            ),
                            "url": href or url,
                            "source": "linkedin_hiring",
                            "published_at": published_at,
                        }
                        # Lista completa primero; filtros + motivos en search_jobs.
                        jobs.append(job)
                        if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP:
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("LinkedIn #Hiring card skip: %s", exc)

                # Seguir con más términos hasta el soft-cap (no cortar al primer hit)
                if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP:
                    break
    finally:
        try:
            page.context.close()
        except Exception:  # noqa: BLE001
            pass

    if not jobs:
        logger.info(
            "LinkedIn #Hiring vacío (cards=%s, skip_intent=%s, skip_query=%s, "
            "authwall=%s, url=%s, sesión=%s)",
            cards_seen,
            skipped_no_intent,
            skipped_query,
            hit_authwall,
            last_url,
            _linkedin_session_ready(),
        )
    elif hit_authwall:
        logger.info(
            "LinkedIn #Hiring: authwall parcial (última url=%s, sesión=%s)",
            last_url,
            _linkedin_session_ready(),
        )
    return jobs


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
) -> list[dict[str, Any]]:
    if source in SOURCE_SCRAPERS:
        return SOURCE_SCRAPERS[source](profile, filters)

    dedicated_profile = dedicated_profile_for_scrape_source(source)
    profile_guard = _DEDICATED_PROFILE_LOCK if dedicated_profile else nullcontext()
    with profile_guard:
        with sync_playwright() as p:
            browser = _launch_browser_for_source(p, source, dedicated_profile)
            try:
                if source == "computrabajo":
                    return scrape_computrabajo(browser, profile, filters=filters)
                if source == "linkedin_hiring":
                    return scrape_linkedin_hiring(browser, profile, filters=filters)
                return scrape_linkedin(browser, profile, filters=filters)
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
) -> dict[str, Any]:
    """
    Varias fuentes en paralelo; fusiona y deduplica.
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

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(active_sources)))) as pool:
        futures = {
            pool.submit(_scrape_source_isolated, source, profile, filters): source
            for source in active_sources
        }
        for fut in as_completed(futures):
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
                            msg = (
                                "LinkedIn #Hiring: hay sesión guardada pero no se "
                                "obtuvieron posts guardables (DOM distinto, anti-bot, "
                                "o el feed no hidrató a tiempo). Reintentá; si sigue "
                                "en 0, renová la sesión de LinkedIn."
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
            + ". Analizando match…"
        ),
        count=len(collected),
    )
    return {"jobs": collected, "sources": sources}
