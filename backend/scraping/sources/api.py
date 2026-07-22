"""
Fuentes de ofertas vía API pública HTTP (sin Playwright).

GetOnBoard (LATAM tech), Remotive, RemoteOK y Jobicy.

=============================================================================
PASO 1 · BÚSQUEDA  +  PASO 2 · EXTRACCIÓN CRUDA  (este módulo)
PASO 3–4 · revisión / clasificación / email → analysis.local + api.app
(mismo pipeline que LinkedIn / Computrabajo; facilita filtrar después)
=============================================================================
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import socket
import urllib.parse
import urllib.request
from typing import Any

from backend.core.config import get_settings
from backend.core.dates import (
    parse_published_at,
    within_posted_window,
)
from backend.core.query_match import matches_search_queries

logger = logging.getLogger(__name__)

USER_AGENT = get_settings().user_agent

PER_SOURCE_CAP = get_settings().per_source_cap
_gob_company_cache: dict[int, str] = {}

# Fuentes cuyo feed público suele tener delay >24 h (Remotive lo documenta explícitamente).
_DELAYED_FEED_SOURCES = frozenset({"remotive", "remoteok", "jobicy"})

_SEARCH_SYNONYMS: tuple[tuple[str, str], ...] = (
    (".net", "dotnet"),
    ("c#", "csharp"),
    ("full stack", "fullstack"),
    ("full-stack", "fullstack"),
    ("node.js", "nodejs"),
    ("asp.net", "aspnet"),
    ("react.js", "react"),
    ("vue.js", "vue"),
)

_STOP_WORDS = frozenset(
    {
        "and",
        "or",
        "the",
        "a",
        "an",
        "for",
        "with",
        "de",
        "del",
        "la",
        "el",
        "en",
        "y",
        "o",
        "con",
        "the",
    }
)

# Términos genéricos: solos no bastan para RemoteOK/Remotive (evita falsos positivos).
_WEAK_SEARCH_TERMS = frozenset(
    {
        "developer",
        "engineer",
        "software",
        "backend",
        "frontend",
        "fullstack",
        "stack",
        "remote",
        "senior",
        "junior",
        "lead",
        "core",
        "api",
        "apis",
        "rest",
        "web",
    }
)

# Fragmentos al separar skills compuestos ("Entity Framework", "SQL Server", etc.).
_SKILL_WEAK_FRAGMENTS = frozenset(
    {
        "entity",
        "framework",
        "server",
        "event",
        "driven",
        "architecture",
        "database",
        "migration",
        "packaging",
        "documentation",
        "technical",
        "oriented",
        "driven",
        "driven",
        "nuget",
        "apis",
        "api",
    }
)

# Jobicy rechaza algunos slugs; mapear a tags válidos (tag= en la API v2).
_JOBICY_TAG_MAP: dict[str, str | None] = {
    "dotnet": ".net",
    "nodejs": "node",
    "aspnet": "asp.net",
    "aspdotnet": "asp.net",
    "csharp": None,
    "c#": None,
}

# Slugs válidos en Jobicy (?get=locations / ?get=industries)
_JOBICY_GEO: dict[str, str] = {
    "ar": "argentina",
    "mx": "mexico",
    "co": "colombia",
    "br": "brazil",
    "cl": "chile",
    "pe": "peru",
    "uy": "uruguay",
    "cr": "costa-rica",
    "pa": "panama",
    "ec": "ecuador",
    "ve": "venezuela",
    "bo": "bolivia",
    "py": "paraguay",
    "do": "dominican-republic",
    "pr": "puerto-rico",
}


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw or "")
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _http_json(url: str, *, timeout: float | None = None) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(
        req, timeout=timeout or get_settings().http_timeout_sec
    ) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _host_is_public(host: str) -> bool:
    """
    True solo si `host` resuelve a direcciones públicas.

    Defensa SSRF: las URLs a canonicalizar provienen de una API externa
    (GetOnBoard); un redirect hacia loopback/red interna (127.0.0.1,
    169.254.169.254, 10.x, …) podría sondear servicios locales.
    """
    host = (host or "").strip().strip("[]")
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0].split("%", 1)[0]  # descarta zona IPv6 (fe80::1%eth0)
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """No sigue redirects hacia hosts no públicos (defensa SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme not in ("http", "https") or not _host_is_public(parts.hostname or ""):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_PublicOnlyRedirectHandler())


def _resolve_final_url(url: str, *, timeout: float = 15.0) -> str:
    """Sigue redirects (p. ej. GetOnBoard agrega /programming/ en la URL canónica)."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    # Solo canonicalizamos URLs http(s) hacia hosts públicos (evita SSRF local).
    if parts.scheme not in ("http", "https") or not _host_is_public(parts.hostname or ""):
        return url
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        method="HEAD",
    )
    try:
        with _SAFE_OPENER.open(req, timeout=timeout) as resp:
            return str(resp.geturl() or url)
    except Exception:  # noqa: BLE001
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            )
            with _SAFE_OPENER.open(req, timeout=timeout) as resp:
                # Leer poco para completar redirects
                resp.read(256)
                return str(resp.geturl() or url)
        except Exception:  # noqa: BLE001
            return url


def _normalize_getonboard_url(url: str) -> str:
    """
    Unifica host a www.getonbrd.com y resuelve la URL canónica.
    Evita www vs sin-www (cookies de sesión no compartidas) y paths cortos de la API.
    """
    if not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in ("getonbrd.com", "www.getonbrd.com", "getonboard.com", "www.getonboard.com"):
            path = parts.path or "/"
            rebuilt = urllib.parse.urlunsplit(
                ("https", "www.getonbrd.com", path, parts.query, "")
            )
            return _resolve_final_url(rebuilt)
    except Exception:  # noqa: BLE001
        pass
    return url

def _queries(profile: dict[str, Any], filters: dict[str, Any]) -> list[str]:
    queries = list(filters.get("queries") or [])
    if queries:
        return queries[:5]
    roles = profile.get("roles") or []
    if isinstance(roles, list) and roles:
        return [str(r).strip() for r in roles if str(r).strip()][:3]
    skills = profile.get("skills") or []
    if isinstance(skills, list) and skills:
        return [str(skills[0]).strip()]
    return ["developer"]


def _normalize_search_text(text: str) -> str:
    low = str(text or "").lower().strip()
    for src, dst in _SEARCH_SYNONYMS:
        low = low.replace(src, dst)
    return low


def _extract_search_terms(profile: dict[str, Any], filters: dict[str, Any]) -> list[str]:
    """Términos aptos para APIs remotas; prioriza skills y tokens técnicos fuertes."""
    terms: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        norm = _normalize_search_text(term)
        if not norm or len(norm) < 2 or norm in _STOP_WORDS or norm in seen:
            return
        if re.fullmatch(r"[\d./]+", norm):
            return
        seen.add(norm)
        terms.append(norm)

    skills = profile.get("skills") or []
    if isinstance(skills, list):
        for skill in skills[:14]:
            text = str(skill).strip()
            if not text:
                continue
            norm = _normalize_search_text(text)
            if re.search(r"[\s/\-]", norm):
                _add(norm)
            for tok in re.findall(r"[a-z0-9#.+]+", norm):
                if tok not in _SKILL_WEAK_FRAGMENTS:
                    _add(tok)

    for query in _queries(profile, filters):
        norm = _normalize_search_text(query)
        if re.search(r"[\s/\-]", norm):
            _add(norm)
        for tok in re.findall(r"[a-z0-9#.+]+", norm):
            _add(tok)

    strong = [t for t in terms if _is_strong_term(t)]
    weak = [t for t in terms if not _is_strong_term(t)]
    return (strong + weak)[:20]


def _is_strong_term(term: str) -> bool:
    if " " in term:
        return False
    if term in _WEAK_SEARCH_TERMS or term in _SKILL_WEAK_FRAGMENTS:
        return False
    if term in {".net", "c#", "go", "sql", "php", "rust", "java"}:
        return True
    return len(term) >= 5


def _keyword_hit(blob: str, terms: list[str], *, strict: bool = False) -> bool:
    if not terms:
        return True
    low = _normalize_search_text(blob)
    strong = [t for t in terms if _is_strong_term(t)]
    if strong:
        return any(term in low for term in strong)
    if strict:
        return False
    weak = [t for t in terms if t in _WEAK_SEARCH_TERMS]
    return any(term in low for term in weak if term)


def _jobicy_api_tags(terms: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if " " in term or len(term) < 2:
            continue
        mapped = _JOBICY_TAG_MAP.get(term, term)
        if not mapped or mapped in seen:
            continue
        seen.add(mapped)
        tags.append(mapped)
    return tags


def _posted_filters_for_source(filters: dict[str, Any], source: str) -> dict[str, Any]:
    """
    Remotive/RemoteOK/Jobicy rara vez tienen ofertas <24 h en el feed público.
    Si el usuario pide solo 24 h, ampliamos a semana para esas fuentes.
    """
    if source not in _DELAYED_FEED_SOURCES:
        return filters
    posted = list(filters.get("posted_within") or [])
    if posted == ["24h"]:
        widened = dict(filters)
        widened["posted_within"] = ["week"]
        widened["_posted_widened_from"] = "24h"
        return widened
    return filters


def _jobicy_geo(profile: dict[str, Any], filters: dict[str, Any]) -> str | None:
    countries = [c.lower() for c in (filters.get("countries") or []) if c]
    if countries:
        return _JOBICY_GEO.get(countries[0], "latam")
    country = str(profile.get("country") or "").lower().strip()
    return _JOBICY_GEO.get(country)


def _remotive_category(profile: dict[str, Any], filters: dict[str, Any]) -> str:
    blob = _normalize_search_text(
        " ".join(_queries(profile, filters))
        + " "
        + " ".join(str(s) for s in (profile.get("skills") or [])[:12])
    )
    if any(k in blob for k in ("devops", "sre", "infra", "kubernetes", "aws")):
        return "devops"
    if any(k in blob for k in ("data", "ml", "machine learning", "analytics")):
        return "data"
    return "software-dev"


def _finalize(
    job: dict[str, Any],
    filters: dict[str, Any],
    *,
    source: str = "",
) -> dict[str, Any] | None:
    """
    Compat: antes filtraba query/fecha aquí. Ahora devolvemos el job crudo;
    el descarte con motivos ocurre en search_jobs._partition_jobs.
    """
    del filters, source
    return job


# ---------------------------------------------------------------------------
# GetOnBoard
# ---------------------------------------------------------------------------

def _gob_company_name(company_id: int) -> str:
    if company_id in _gob_company_cache:
        return _gob_company_cache[company_id]
    try:
        data = _http_json(f"https://www.getonbrd.com/api/v0/companies/{company_id}")
        name = (
            ((data.get("data") or {}).get("attributes") or {}).get("name")
            or "Empresa GetOnBoard"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("GetOnBoard company %s: %s", company_id, exc)
        name = "Empresa GetOnBoard"
    _gob_company_cache[company_id] = str(name)[:150]
    return _gob_company_cache[company_id]


def scrape_getonboard(
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """PASO 1–2 GetOnBoard: search API → jobs crudos (PASO 3–4 en analyze)."""
    filters = dict(filters or {})
    queries = _queries(profile, filters)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    # --- PASO 1 · BÚSQUEDA por keyword ---
    for keyword in queries:
        if len(jobs) >= PER_SOURCE_CAP:
            break
        url = (
            "https://www.getonbrd.com/api/v0/search/jobs?"
            + urllib.parse.urlencode({"query": keyword, "per_page": 20})
        )
        try:
            payload = _http_json(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GetOnBoard falló (%s): %s", keyword, exc)
            continue

        for item in payload.get("data") or []:
            if len(jobs) >= PER_SOURCE_CAP:
                break
            attrs = item.get("attributes") or {}
            links = item.get("links") or {}
            job_url = _normalize_getonboard_url(links.get("public_url") or "")
            if not job_url or job_url in seen:
                continue
            seen.add(job_url)

            company_id = ((attrs.get("company") or {}).get("data") or {}).get("id")
            company = (
                _gob_company_name(int(company_id))
                if company_id is not None
                else "Empresa GetOnBoard"
            )

            parts = [
                attrs.get("description") or "",
                attrs.get("functions") or "",
                attrs.get("projects") or "",
                attrs.get("benefits") or "",
                attrs.get("desirable") or "",
            ]
            if attrs.get("min_salary") or attrs.get("max_salary"):
                parts.append(
                    f"Salario USD {attrs.get('min_salary') or '?'}–{attrs.get('max_salary') or '?'}"
                )
            countries = attrs.get("countries") or []
            if countries:
                parts.append("Países: " + ", ".join(str(c) for c in countries))
            modality = attrs.get("remote_modality") or ""
            if modality:
                parts.append(f"Modalidad: {modality}")
            loc_bits = [str(c) for c in countries[:3]] if countries else []
            if modality:
                loc_bits.append(str(modality))
            location = ", ".join(loc_bits) if loc_bits else "LATAM / Remoto"

            # --- PASO 2 · EXTRACCIÓN CRUDA (campos API sin clasificar) ---
            job = {
                "title": str(attrs.get("title") or "Sin título")[:200],
                "company": company,
                "location": location[:120],
                "description": _strip_html(" ".join(parts))[:10000],
                "url": job_url,
                "source": "getonboard",
                "published_at": parse_published_at(attrs.get("published_at")),
                # Campo interno para filtro de país; se elimina antes de devolver al frontend.
                "_countries_raw": [str(c).strip() for c in countries if str(c).strip()],
            }
            finalized = _finalize(job, filters, source="getonboard")
            if finalized:
                jobs.append(finalized)

    return jobs


# ---------------------------------------------------------------------------
# Remotive
# ---------------------------------------------------------------------------

def scrape_remotive(
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """PASO 1–2 Remotive (API). PASO 3–4 en analyze_job_local / _analyze_raw_jobs."""
    filters = dict(filters or {})
    terms = _extract_search_terms(profile, filters)
    category = _remotive_category(profile, filters)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Remotive: search + category + limit (https://remotive.com/api/remote-jobs)
    search_plan: list[dict[str, str | int]] = []
    for term in terms[:6]:
        search_plan.append({"search": term, "category": category, "limit": 40})
    if not search_plan:
        search_plan.append({"category": category, "limit": 40})

    for params in search_plan:
        if len(jobs) >= PER_SOURCE_CAP:
            break
        url = "https://remotive.com/api/remote-jobs?" + urllib.parse.urlencode(params)
        try:
            payload = _http_json(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Remotive falló (%s): %s", params, exc)
            continue

        for item in payload.get("jobs") or []:
            if len(jobs) >= PER_SOURCE_CAP:
                break
            job_url = item.get("url") or ""
            if not job_url or job_url in seen:
                continue

            loc = item.get("candidate_required_location") or ""
            salary = item.get("salary") or ""
            desc = _strip_html(item.get("description") or "")
            title = str(item.get("title") or "")
            company = str(item.get("company_name") or "")

            seen.add(job_url)
            extra = f" Ubicación: {loc}." if loc else ""
            if salary:
                extra += f" Salario: {salary}."

            job = {
                "title": title[:200] or "Sin título",
                "company": company[:150] or "Empresa Remotive",
                "location": (str(loc) or "Remote")[:120],
                "description": (desc + extra)[:10000],
                "url": job_url,
                "source": "remotive",
                "published_at": parse_published_at(item.get("publication_date")),
            }
            finalized = _finalize(job, filters, source="remotive")
            if finalized:
                jobs.append(finalized)

    return jobs


# ---------------------------------------------------------------------------
# RemoteOK
# ---------------------------------------------------------------------------

def scrape_remoteok(
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """PASO 1–2 RemoteOK (API). PASO 3–4 en analyze_job_local / _analyze_raw_jobs."""
    filters = dict(filters or {})
    terms = _extract_search_terms(profile, filters)
    try:
        payload = _http_json("https://remoteok.com/api")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RemoteOK falló: %s", exc)
        raise

    if not isinstance(payload, list):
        return []

    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    # RemoteOK no filtra por query en la API; hay que traer el listado y matchear localmente.
    for item in payload:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if len(jobs) >= PER_SOURCE_CAP:
            break

        title = str(item.get("position") or "")
        company = str(item.get("company") or "Empresa RemoteOK")
        tags = " ".join(str(t) for t in (item.get("tags") or []))
        desc = _strip_html(item.get("description") or "")
        # RemoteOK: tags genéricos en casi todas las ofertas → match estricto en título + tags.
        blob = f"{title} {tags}"
        if terms and not _keyword_hit(blob, terms, strict=True):
            continue

        job_url = item.get("url") or item.get("apply_url") or ""
        if not job_url or job_url in seen:
            continue
        seen.add(job_url)

        loc = item.get("location") or "Remote"
        salary_bits = []
        if item.get("salary_min"):
            salary_bits.append(str(item["salary_min"]))
        if item.get("salary_max"):
            salary_bits.append(str(item["salary_max"]))
        salary_txt = f" Salario: {'–'.join(salary_bits)}." if salary_bits else ""

        job = {
            "title": title[:200] or "Sin título",
            "company": company[:150],
            "location": (str(loc) or "Remote")[:120],
            "description": (desc + f" Ubicación: {loc}.{salary_txt}")[:10000],
            "url": job_url,
            "source": "remoteok",
            "published_at": parse_published_at(item.get("date") or item.get("epoch")),
        }
        finalized = _finalize(job, filters, source="remoteok")
        if finalized:
            jobs.append(finalized)

    return jobs


# ---------------------------------------------------------------------------
# Jobicy
# ---------------------------------------------------------------------------

def scrape_jobicy(
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """PASO 1–2 Jobicy (API). PASO 3–4 en analyze_job_local / _analyze_raw_jobs."""
    filters = dict(filters or {})
    terms = _extract_search_terms(profile, filters)
    geo = _jobicy_geo(profile, filters)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Jobicy: count + tag + industry + geo (https://jobicy.com/api/v2/remote-jobs)
    api_tags = _jobicy_api_tags(terms)
    search_plan: list[dict[str, str | int]] = []
    for tag in api_tags[:8]:
        params: dict[str, str | int] = {"count": 25, "tag": tag, "industry": "dev"}
        if geo:
            params["geo"] = geo
        search_plan.append(params)
    if not search_plan:
        params: dict[str, str | int] = {"count": 25, "industry": "dev"}
        if geo:
            params["geo"] = geo
        search_plan.append(params)

    for params in search_plan:
        if len(jobs) >= PER_SOURCE_CAP:
            break
        url = "https://jobicy.com/api/v2/remote-jobs?" + urllib.parse.urlencode(params)
        try:
            payload = _http_json(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Jobicy falló (%s): %s", params, exc)
            continue

        for item in payload.get("jobs") or []:
            if len(jobs) >= PER_SOURCE_CAP:
                break
            job_url = item.get("url") or ""
            if not job_url or job_url in seen:
                continue

            title = str(item.get("jobTitle") or "")
            company = str(item.get("companyName") or "Empresa Jobicy")
            desc = _strip_html(item.get("jobDescription") or item.get("jobExcerpt") or "")

            seen.add(job_url)
            geo_txt = item.get("jobGeo") or "Remote"
            level = item.get("jobLevel") or ""
            extra = f" Ubicación: {geo_txt}."
            if level:
                extra += f" Nivel: {level}."
            if item.get("salaryMin") or item.get("salaryMax"):
                extra += (
                    f" Salario {item.get('salaryCurrency') or 'USD'} "
                    f"{item.get('salaryMin') or '?'}–{item.get('salaryMax') or '?'}."
                )

            job = {
                "title": title[:200] or "Sin título",
                "company": company[:150],
                "location": (str(geo_txt) or "Remote")[:120],
                "description": (desc + extra)[:10000],
                "url": job_url,
                "source": "jobicy",
                "published_at": parse_published_at(item.get("pubDate")),
            }
            finalized = _finalize(job, filters, source="jobicy")
            if finalized:
                jobs.append(finalized)

    return jobs


SOURCE_SCRAPERS = {
    "getonboard": scrape_getonboard,
    "remotive": scrape_remotive,
    "remoteok": scrape_remoteok,
    "jobicy": scrape_jobicy,
}
