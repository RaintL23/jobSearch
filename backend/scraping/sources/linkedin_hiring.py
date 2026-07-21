"""LinkedIn #Hiring — posts del feed (best-effort)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from threading import Event
from typing import Any
from urllib.parse import quote_plus, urlsplit

from playwright.sync_api import Page

from backend.core.dates import parse_published_at, parse_relative_published
from backend.core.query_match import matches_search_queries
from backend.scraping.browser import (
    _gentle_pause,
    _linkedin_session_ready,
    _looks_like_linkedin_authwall,
    _new_page,
)
from backend.scraping.constants import BrowserTarget
from backend.scraping.filters import _normalize_filters, _search_queries

logger = logging.getLogger(__name__)

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

# Señales de EMPLEADOR contratando (no candidatos “open to work”).
# Incluye frases típicas de recruiters LATAM aunque el “…more” aún no
# expandió el #Hiring del final (ej. post de María Fernanda Spirito).
LINKEDIN_HIRING_HINTS = (
    "#hiring",
    "we're hiring",
    "we are hiring",
    "we’re hiring",  # apostrofe tipográfico
    "we're looking for",
    "we are looking for",
    "is hiring",
    "are hiring",
    "join our team",
    "open role",
    "open position",
    "job opening",
    "view job",
    "apply today",
    "apply now",
    "estamos contratando",
    "estamos buscando",
    "se busca",
    "buscamos",
    "contratando",
    "vacante",
    "nueva vacante",
    "nueva oportunidad",
    "oportunidad internacional",
    "oportunidad laboral",
    "búsqueda laboral",
    "busqueda laboral",
    "postulate",
    "postulá",
    "postula ",
    "sumate",
    "sumáte",
    "incorporamos",
    # "hiring" suelto: cubre "#hiring" y frases sin we're
    " hiring ",
    "hiring a ",
    "hiring an ",
    "hiring –",
    "hiring -",
)

# Candidatos buscando trabajo (aparecen en la misma búsqueda de content).
# Frases fuertes de candidato; NO usar solo "#opentowork" (recruiters lo
# ponen de hashtag en ofertas reales).
LINKEDIN_OPEN_TO_WORK_HINTS = (
    "is open to work",
    "open to work\n",
    " currently open to new opportunities",
    "looking for new opportunities as ",
    "looking for new opportunities as a",
    "view job preferences",
    "i'm currently open",
    "i am currently open",
    "estoy en búsqueda activa",
    "estoy buscando trabajo",
    "busco empleo",
    "busco oportunidades como",
)

LINKEDIN_HIRING_SOFT_CAP = 25

# Diagnóstico del último scrape #Hiring (para el mensaje UI si vuelve 0).
_linkedin_hiring_last_diag: dict[str, Any] = {}

# Extracción masiva en el browser: el DOM SDUI actual suele no exponer
# data-urn en el nodo que Playwright toma como “card”; el permalink está
# en hrefs del timestamp, componentkey o ancestros.
_LINKEDIN_HIRING_EXTRACT_JS = r"""
() => {
  const ACTIVITY = /urn:li:activity:(\d{6,})/i;
  const UGC = /urn:li:ugcPost:(\d{6,})/i;
  const POSTS_PATH = /\/posts\/[^/?#\s]+-(\d{15,})-[A-Za-z0-9_]+/i;
  const FEED_PATH = /\/feed\/update\/urn:li:(?:activity|ugcPost):\d{6,}/i;

  function abs(href) {
    if (!href) return "";
    try {
      const u = new URL(href, "https://www.linkedin.com");
      if (!/(^|\.)linkedin\.com$/i.test(u.hostname)) return "";
      return (u.origin + u.pathname).replace(/\/$/, "") + "/";
    } catch (_) {
      return "";
    }
  }

  function fromBlob(blob) {
    if (!blob) return "";
    const feed = blob.match(FEED_PATH);
    if (feed) return abs(feed[0]);
    const posts = blob.match(POSTS_PATH);
    if (posts) {
      const full = blob.match(/https?:\/\/[^\s"'<>]*\/posts\/[^\s"'<>?]*/i)
        || blob.match(/\/posts\/[^\s"'<>?]*/i);
      if (full) return abs(full[0]);
    }
    const act = blob.match(ACTIVITY);
    if (act) return "https://www.linkedin.com/feed/update/urn:li:activity:" + act[1] + "/";
    const ugc = blob.match(UGC);
    if (ugc) return "https://www.linkedin.com/feed/update/urn:li:ugcPost:" + ugc[1] + "/";
    // activity-1234567890123456789 en URLs de share
    const loose = blob.match(/activity[_:-](\d{15,})/i);
    if (loose) return "https://www.linkedin.com/feed/update/urn:li:activity:" + loose[1] + "/";
    return "";
  }

  function findPermalink(root) {
    const attrs = [
      "data-urn", "data-id", "data-activity-urn", "data-entity-urn",
      "componentkey", "componentKey", "data-finch-component-id",
    ];
    const nodes = [root, ...Array.from(root.querySelectorAll("*")).slice(0, 100)];
    for (const el of nodes) {
      for (const a of attrs) {
        const p = fromBlob(el.getAttribute && el.getAttribute(a));
        if (p) return p;
      }
    }
    for (const a of root.querySelectorAll("a[href]")) {
      const href = a.getAttribute("href") || "";
      // Evitar /company/.../posts/ (página de empresa, no el post).
      if (/\/company\/[^/]+\/posts\/?$/i.test(href.split("?")[0])) continue;
      const p = fromBlob(href);
      if (p) return p;
    }
    let cur = root.parentElement;
    for (let i = 0; i < 10 && cur; i++, cur = cur.parentElement) {
      for (const a of attrs) {
        const p = fromBlob(cur.getAttribute && cur.getAttribute(a));
        if (p) return p;
      }
    }
    return "";
  }

  const rootSelectors = [
    "div.feed-shared-update-v2",
    'div[data-urn*="activity"]',
    'div[data-id*="activity"]',
    'div[data-urn*="ugcPost"]',
    'div[role="listitem"]',
    "div.reusable-search__result-container",
    "li.reusable-search__result-container",
  ];
  let roots = [];
  for (const sel of rootSelectors) {
    const found = Array.from(document.querySelectorAll(sel));
    if (found.length) {
      roots = found;
      break;
    }
  }
  // Evitar nodos anidados (listitem dentro de listitem).
  roots = roots.filter(
    (el) => !roots.some((other) => other !== el && other.contains(el))
  );

  const out = [];
  const seen = new Set();
  for (const root of roots.slice(0, 50)) {
    const textEl = root.querySelector(
      '[data-testid="expandable-text-box"], .update-components-text, ' +
      ".feed-shared-update-v2__description, .feed-shared-text, .break-words"
    );
    let text = ((textEl && textEl.innerText) || root.innerText || "").trim();
    if (text.length < 30) continue;

    const permalink = findPermalink(root);
    if (!permalink || seen.has(permalink)) continue;
    seen.add(permalink);

    const actor = root.querySelector(
      ".update-components-actor__name, .feed-shared-actor__name, " +
      '.update-components-actor__title, a[href*="/company/"], a[href*="/in/"]'
    );
    let company = actor ? actor.innerText.trim() : "";
    company = company.replace(/\s+/g, " ").slice(0, 150);

    const locEl = root.querySelector(
      ".update-components-entity__description, " +
      ".update-components-entity__sub-description, " +
      ".job-card-container__metadata-item, " +
      ".artdeco-entity-lockup__caption"
    );
    const location = locEl
      ? locEl.innerText.replace(/\s+/g, " ").trim().slice(0, 120)
      : "";

    const timeEl = root.querySelector("time, span.feed-shared-actor__sub-description, " +
      'a[href*="activity"] span, a[href*="/posts/"] span');
    let published = "";
    if (timeEl) {
      published =
        (timeEl.getAttribute && timeEl.getAttribute("datetime")) ||
        timeEl.innerText ||
        "";
    }

    out.push({
      text: text.slice(0, 4000),
      company,
      location,
      permalink,
      published: String(published).replace(/\s+/g, " ").trim().slice(0, 80),
    });
  }
  return { posts: out, roots: roots.length };
}
"""


def _linkedin_hiring_date_param(filters: dict[str, Any]) -> str | None:
    """PASO 1 · filtro de tiempo para content search (Past 24h / week / month)."""
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


def _linkedin_hiring_collect_cards(page: Page) -> list[Any]:
    """PASO 2 · espera SPA + scroll y devuelve nodos de posts (legacy o SDUI)."""
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
    """PASO 2 · texto visible del post (ya expandido si hubo '...more')."""
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


def _linkedin_hiring_card_location(card: Any) -> str:
    """
    PASO 2 · ubicación si LinkedIn embebe una job card en el post
    (título / empresa / ciudad On-site|Remote). Best-effort.
    """
    for sel in (
        ".update-components-entity__description",
        ".update-components-entity__sub-description",
        ".job-card-container__metadata-item",
        ".artdeco-entity-lockup__subtitle",
        '[data-testid="job-card-location"]',
    ):
        try:
            el = card.query_selector(sel)
        except Exception:  # noqa: BLE001
            el = None
        if not el:
            continue
        try:
            text = " ".join((el.inner_text() or "").split())
        except Exception:  # noqa: BLE001
            text = ""
        if text and len(text) >= 3:
            return text[:120]
    return ""


def _linkedin_hiring_expand(page: Page) -> None:
    """PASO 2 · click en '...more' / 'ver más' para sacar el texto completo del post."""
    for sel in (
        'button[aria-label*="see more" i]',
        'button[aria-label*="ver más" i]',
        'button[aria-label*="ver mas" i]',
        "button.feed-shared-inline-show-more-text__see-more-less-toggle",
        '[data-testid="expandable-text-button"]',
        "button.inline-show-more-text__button",
        # LinkedIn a veces usa <button>…more</button> o span clickeable
        'button.feed-shared-inline-show-more-text__button',
    ):
        try:
            buttons = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for btn in buttons[:20]:
            try:
                if btn.is_visible():
                    btn.click(timeout=800)
            except Exception:  # noqa: BLE001
                pass
    # Fallback por texto visible ("…more" / "ver más") en resultados de content.
    try:
        page.evaluate(
            """() => {
              const re = /^\\s*([….]{0,3}\\s*)?(see more|ver más|ver mas|more)\\s*$/i;
              const nodes = Array.from(
                document.querySelectorAll('button, span[role="button"], a')
              );
              let n = 0;
              for (const el of nodes) {
                if (n >= 20) break;
                const t = (el.innerText || el.textContent || '').trim();
                if (!re.test(t)) continue;
                try { el.click(); n++; } catch (_) {}
              }
            }"""
        )
    except Exception:  # noqa: BLE001
        pass


def _linkedin_hiring_intent(text: str, *, permalink: str = "") -> bool:
    """
    True si el post parece oferta de empleador (no candidato open-to-work).

    Importante: en search/results/content el texto suele venir TRUNCADO
    (…more). Posts como el de María Fernanda Spirito muestran solo
    «NUEVA OPORTUNIDAD | .NET API DEVELOPER» sin el #Hiring del final;
    por eso también miramos el slug del permalink (_hiring-…-share-ID).
    """
    low = f" {(text or '').lower()} "

    def _has_employer_signal(blob: str) -> bool:
        if any(k in blob for k in LINKEDIN_HIRING_HINTS if k != "view job"):
            return True
        # "View job" (oferta) ≠ "View job preferences" (candidato).
        return "view job" in blob and "view job preferences" not in blob

    if any(h in low for h in LINKEDIN_OPEN_TO_WORK_HINTS):
        # Recruiters a veces agregan #OpenToWork; no descartar si hay oferta clara.
        if not _has_employer_signal(low):
            slug = (permalink or "").lower()
            if not ("hiring" in slug or "contrat" in slug or "vacante" in slug):
                return False
    if _has_employer_signal(low):
        return True
    # Permalink canónico de LinkedIn suele incluir hashtags del post:
    # /posts/user_hiring-dotnet-…-share-7485…-atO7/
    # Ejemplo real: spiritomariafernanda_hiring-dotnetdeveloper-…-7485421228564905985-atO7
    slug = (permalink or "").lower()
    if "hiring" in slug or "contrat" in slug or "vacante" in slug:
        return True
    return False


def _linkedin_hiring_extract_via_js(page: Page) -> tuple[list[dict[str, Any]], int]:
    """
    PASO 2 · extracción primaria: un evaluate() lee posts + permalinks del DOM
    actual (SDUI). Más fiable que card-by-card con Playwright cuando LinkedIn
    esconde el urn fuera del nodo visible.
    """
    try:
        payload = page.evaluate(_LINKEDIN_HIRING_EXTRACT_JS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LinkedIn #Hiring JS extract falló: %s", exc)
        return [], 0
    if not isinstance(payload, dict):
        return [], 0
    posts = payload.get("posts") or []
    roots = int(payload.get("roots") or 0)
    if not isinstance(posts, list):
        return [], roots
    cleaned: list[dict[str, Any]] = []
    for item in posts:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        permalink = str(item.get("permalink") or "").strip()
        if len(text) < 30 or not permalink:
            continue
        if not is_linkedin_hiring_permalink(permalink):
            continue
        cleaned.append(
            {
                "text": text,
                "company": str(item.get("company") or "").strip(),
                "location": str(item.get("location") or "").strip()[:120],
                "permalink": permalink,
                "published": str(item.get("published") or "").strip(),
            }
        )
    return cleaned, roots


# --- PASO 2 · EXTRACCIÓN PRIMARIA POR RED (Voyager / GraphQL) ----------------
# LinkedIn pinta la búsqueda con JSON de su API interna. Interceptar esas
# respuestas es más preciso que raspar el DOM: trae el texto COMPLETO (no el
# "…more" truncado) y el urn del post, con lo que el permalink es fiable.
_LINKEDIN_VOYAGER_URL_HINTS = (
    "voyager/api/graphql",
    "voyager/api/search",
    "voyager/api/feed",
    "voyager/api/voyagersearchdashclusters",
    "voyager/api/voyagerfeeddashmainfeed",
)


def _attach_linkedin_voyager_capture(page: Page) -> list[Any]:
    """
    Engancha un listener de red y acumula los JSON internos de LinkedIn.

    Devuelve una lista mutable que se rellena en segundo plano a medida que la
    página dispara XHR (navegación + scroll). El caller la limpia por término.
    """
    captured: list[Any] = []

    def _on_response(resp: Any) -> None:
        try:
            url = (resp.url or "").lower()
        except Exception:  # noqa: BLE001
            return
        if "voyager/api" not in url:
            return
        if not any(h in url for h in _LINKEDIN_VOYAGER_URL_HINTS):
            return
        try:
            if resp.status != 200:
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(data, (dict, list)):
            captured.append(data)

    try:
        page.on("response", _on_response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LinkedIn #Hiring: no se pudo enganchar la red: %s", exc)
    return captured


def _voyager_attr_text(node: Any, depth: int = 0) -> str:
    """Resuelve las estructuras de texto de LinkedIn (AttributedText) a str plano."""
    if depth > 6 or node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("text", "attributedText", "commentaryText", "value"):
            if key in node:
                t = _voyager_attr_text(node[key], depth + 1)
                if t:
                    return t
        return ""
    if isinstance(node, list):
        parts = [_voyager_attr_text(x, depth + 1) for x in node]
        return " ".join(p for p in parts if p)
    return ""


def _voyager_actor_field(update: dict[str, Any], keys: tuple[str, ...]) -> str:
    actor = update.get("actor")
    if not isinstance(actor, dict):
        return ""
    for key in keys:
        t = _voyager_attr_text(actor.get(key))
        if t:
            return " ".join(t.split())
    return ""


def _voyager_build_permalink(blob: str) -> str:
    m = _ACTIVITY_RE.search(blob) or _ACTIVITY_LOOSE_RE.search(blob)
    if m:
        return (
            "https://www.linkedin.com/feed/update/urn:li:activity:"
            + m.group(1)
            + "/"
        )
    m = _UGC_POST_RE.search(blob)
    if m:
        return (
            "https://www.linkedin.com/feed/update/urn:li:ugcPost:"
            + m.group(1)
            + "/"
        )
    return ""


def _voyager_permalink(update: dict[str, Any]) -> str:
    """
    Permalink del post. Prioriza el urn propio del update (updateMetadata /
    entityUrn) para no confundirlo con posts reshareados anidados.
    """
    candidates: list[str] = []
    meta = update.get("updateMetadata")
    if isinstance(meta, dict):
        for key in ("urn", "shareUrn", "backendUrn"):
            val = meta.get(key)
            if isinstance(val, str):
                candidates.append(val)
    for key in ("entityUrn", "dashEntityUrn", "preDashEntityUrn", "urn"):
        val = update.get(key)
        if isinstance(val, str):
            candidates.append(val)
    for cand in candidates:
        link = _voyager_build_permalink(cand)
        if link:
            return link
    # Último recurso: escanear el objeto entero (puede tomar un urn anidado).
    try:
        return _voyager_build_permalink(json.dumps(update, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return ""


def _voyager_iter_updates(node: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Recorre el JSON y devuelve cada objeto de post (los que traen commentary)."""
    if depth > 8 or node is None:
        return
    if isinstance(node, dict):
        if "commentary" in node:
            yield node
        for val in node.values():
            yield from _voyager_iter_updates(val, depth + 1)
    elif isinstance(node, list):
        for val in node:
            yield from _voyager_iter_updates(val, depth + 1)


def _linkedin_hiring_extract_via_voyager(
    payloads: list[Any],
) -> list[dict[str, Any]]:
    """
    PASO 2 (primario) · convierte los JSON de red en posts estructurados.
    Misma forma que `_linkedin_hiring_extract_via_js` para reutilizar el pipeline.
    """
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        for update in _voyager_iter_updates(payload):
            text = _voyager_attr_text(update.get("commentary")).strip()
            if len(text) < 30:
                continue
            permalink = _voyager_permalink(update)
            if not permalink or not is_linkedin_hiring_permalink(permalink):
                continue
            if permalink in seen:
                continue
            seen.add(permalink)
            cleaned.append(
                {
                    "text": text[:4000],
                    "company": _voyager_actor_field(update, ("name", "title"))[:150],
                    "location": "",
                    "permalink": permalink,
                    "published": _voyager_actor_field(
                        update, ("subDescription", "subtitle")
                    )[:80],
                }
            )
    return cleaned


def _linkedin_hiring_merge_posts(
    dom_posts: list[dict[str, Any]],
    voyager_posts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Combina por permalink. La red gana (texto completo + urn fiable) pero
    conserva company/location del DOM si la red no los trajo.
    """
    merged: dict[str, dict[str, Any]] = {}
    for post in dom_posts:
        merged[post["permalink"]] = post
    for post in voyager_posts:
        existing = merged.get(post["permalink"])
        if existing:
            if not post.get("location") and existing.get("location"):
                post["location"] = existing["location"]
            if not post.get("company") and existing.get("company"):
                post["company"] = existing["company"]
        merged[post["permalink"]] = post
    return list(merged.values())


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
    from backend.core.query_match import normalize_query_text, query_tokens

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


_ACTIVITY_RE = re.compile(r"urn:li:activity:(\d{6,})")
_UGC_POST_RE = re.compile(r"urn:li:ugcPost:(\d{6,})")
_ACTIVITY_LOOSE_RE = re.compile(r"activity[:\-](\d{6,})")
_POSTS_ID_RE = re.compile(r"-(\d{15,})-[A-Za-z0-9_]+/?$")
_FEED_POST_PATH_RE = re.compile(
    r"^/feed/update/urn:li:(?:activity|ugcPost):\d{6,}/?$",
    re.IGNORECASE,
)


def is_linkedin_hiring_permalink(url: str) -> bool:
    """
    True únicamente para una publicación individual de LinkedIn.

    Rechaza expresamente páginas de empresa/showcase, `/company/.../posts/`,
    perfiles y búsquedas aunque contengan la palabra `posts`.
    """
    try:
        parsed = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in {"linkedin.com", "www.linkedin.com"}:
        return False
    path = parsed.path or ""
    if _FEED_POST_PATH_RE.fullmatch(path):
        return True
    return path.startswith("/posts/") and bool(_POSTS_ID_RE.search(path))


def _linkedin_hiring_card_scopes(card: Any) -> list[Any]:
    """
    Devuelve la card y sus contenedores de post cercanos.

    En el layout SDUI, `_linkedin_hiring_collect_cards` puede devolver solo la
    caja de texto o el actor. El permalink/data-urn suele estar en un ancestro.
    """
    scopes: list[Any] = [card]
    closest_selectors = (
        "div.feed-shared-update-v2",
        "div[data-id*='urn:li:activity']",
        "div[data-urn*='activity']",
        "div[data-urn*='ugcPost']",
        "div.reusable-search__result-container",
        "li.reusable-search__result-container",
        "div[role='listitem']",
    )
    for selector in closest_selectors:
        try:
            handle = card.evaluate_handle(
                "(el, selector) => el.closest(selector)", selector
            )
            ancestor = handle.as_element()
        except Exception:  # noqa: BLE001
            ancestor = None
        if ancestor and all(ancestor != scope for scope in scopes):
            scopes.append(ancestor)
    return scopes


def _extract_activity_id(card: Any) -> str:
    """
    Busca el id numérico de la 'activity' del post en atributos data-* y hrefs,
    tanto en la card como en sus descendientes (el DOM SDUI la esconde profundo).
    """
    attr_names = ("data-urn", "data-id", "data-activity-urn", "data-entity-urn")
    nodes: list[Any] = []
    scopes = _linkedin_hiring_card_scopes(card)
    for scope in scopes:
        nodes.append(scope)
        try:
            nodes += scope.query_selector_all(
                "[data-urn], [data-id], [data-activity-urn], [data-entity-urn]"
            )
        except Exception:  # noqa: BLE001
            pass
    for el in nodes[:60]:
        for attr in attr_names:
            try:
                val = el.get_attribute(attr) or ""
            except Exception:  # noqa: BLE001
                val = ""
            if not val:
                continue
            m = _ACTIVITY_RE.search(val) or _ACTIVITY_LOOSE_RE.search(val)
            if m:
                return m.group(1)

    # Buscar en hrefs de anclas relacionadas al post.
    anchors: list[Any] = []
    for scope in scopes:
        try:
            anchors += scope.query_selector_all(
                "a[href*='activity'], a[href*='/posts/'], a[href*='/feed/update/']"
            )
        except Exception:  # noqa: BLE001
            pass
    for a in anchors[:40]:
        try:
            href = a.get_attribute("href") or ""
        except Exception:  # noqa: BLE001
            href = ""
        m = (
            _ACTIVITY_RE.search(href)
            or _ACTIVITY_LOOSE_RE.search(href)
            or _POSTS_ID_RE.search(href)
        )
        if m:
            return m.group(1)
    return ""


def _extract_ugc_post_id(card: Any) -> str:
    """Extrae el id `ugcPost` usado por algunos layouts nuevos de LinkedIn."""
    attrs = ("data-urn", "data-id", "data-activity-urn", "data-entity-urn")
    for scope in _linkedin_hiring_card_scopes(card):
        nodes: list[Any] = [scope]
        try:
            nodes += scope.query_selector_all(
                "[data-urn], [data-id], [data-activity-urn], [data-entity-urn]"
            )
        except Exception:  # noqa: BLE001
            pass
        for node in nodes[:60]:
            for attr in attrs:
                try:
                    value = node.get_attribute(attr) or ""
                except Exception:  # noqa: BLE001
                    value = ""
                match = _UGC_POST_RE.search(value)
                if match:
                    return match.group(1)
    return ""


def _extract_hiring_permalink(card: Any) -> str:
    """
    Devuelve el link al POST individual (lo que LinkedIn ofrece con
    'Copiar enlace de la publicación'), no a la búsqueda ni al perfil.

    Prioridad:
      1) href /posts/… ya presente en el DOM (formato canónico de compartir).
      2) permalink construido desde el id de activity → /feed/update/…
      3) href /feed/update/… presente en el DOM.
      4) scan JS profundo del nodo (componentkey / ancestros / timestamp).
      5) cadena vacía si LinkedIn no expone un identificador directo.

    Nunca devuelve la URL de resultados de búsqueda.
    """
    def _abs(href: str) -> str:
        href = (href or "").strip()
        if href.startswith("/"):
            href = "https://www.linkedin.com" + href
        return href.split("?", 1)[0]

    # 1) /posts/ es exactamente el link que genera el botón de compartir.
    scopes = _linkedin_hiring_card_scopes(card)
    for scope in scopes:
        try:
            el = scope.query_selector("a[href*='/posts/']")
        except Exception:  # noqa: BLE001
            el = None
        if el:
            href = _abs(el.get_attribute("href") or "")
            if is_linkedin_hiring_permalink(href):
                return href

    # 2) Construir permalink canónico y estable desde el id de activity.
    act = _extract_activity_id(card)
    if act:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{act}/"

    # LinkedIn SDUI también identifica publicaciones propias como ugcPost.
    ugc_post = _extract_ugc_post_id(card)
    if ugc_post:
        return f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{ugc_post}/"

    # 3) Último anchor de post en el DOM (evitando perfiles /in/).
    for scope in scopes:
        try:
            el = scope.query_selector("a[href*='/feed/update/']")
        except Exception:  # noqa: BLE001
            el = None
        if el:
            href = _abs(el.get_attribute("href") or "")
            if is_linkedin_hiring_permalink(href):
                return href

    # 4) Scan JS: componentkey / data-* / hrefs del timestamp en SDUI nuevo.
    try:
        found = card.evaluate(
            """(el) => {
              const ACTIVITY = /urn:li:activity:(\\d{6,})/i;
              const UGC = /urn:li:ugcPost:(\\d{6,})/i;
              const FEED = /\\/feed\\/update\\/urn:li:(?:activity|ugcPost):\\d{6,}/i;
              const POSTS = /\\/posts\\/[^/?#]+-(\\d{15,})-[A-Za-z0-9_]+/i;
              const attrs = ['data-urn','data-id','data-activity-urn',
                'data-entity-urn','componentkey','componentKey'];
              const nodes = [el, ...Array.from(el.querySelectorAll('*')).slice(0, 80)];
              let cur = el.parentElement;
              for (let i = 0; i < 8 && cur; i++, cur = cur.parentElement) nodes.push(cur);
              for (const node of nodes) {
                for (const a of attrs) {
                  const v = node.getAttribute && node.getAttribute(a) || '';
                  let m = v.match(ACTIVITY);
                  if (m) return 'https://www.linkedin.com/feed/update/urn:li:activity:' + m[1] + '/';
                  m = v.match(UGC);
                  if (m) return 'https://www.linkedin.com/feed/update/urn:li:ugcPost:' + m[1] + '/';
                }
              }
              for (const a of el.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                if (/\\/company\\/[^/]+\\/posts\\/?$/i.test(href.split('?')[0])) continue;
                if (FEED.test(href) || POSTS.test(href)) {
                  try {
                    const u = new URL(href, 'https://www.linkedin.com');
                    return u.origin + u.pathname.split('?')[0];
                  } catch (_) {}
                }
                let m = href.match(ACTIVITY) || href.match(/activity[_:-](\\d{15,})/i);
                if (m) return 'https://www.linkedin.com/feed/update/urn:li:activity:' + m[1] + '/';
              }
              return '';
            }"""
        )
    except Exception:  # noqa: BLE001
        found = ""
    if found and is_linkedin_hiring_permalink(str(found)):
        return str(found).split("?", 1)[0]

    return ""


def scrape_linkedin_hiring(
    browser: BrowserTarget,
    profile: dict[str, Any],
    filters: dict[str, Any] | None = None,
    cancel_event: Event | None = None,
) -> list[dict[str, Any]]:
    """
    LinkedIn #Hiring — PASO 1 (content search + datePosted + sortBy Latest)
    y PASO 2 (extraer posts crudos: texto, actor, permalink, fecha, ubicación
    embebida). PASO 3–4: analysis / api (skills, ubicación, email IA).

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
    skipped_open_to_work = 0
    skipped_query = 0
    skipped_no_permalink = 0
    js_roots = 0
    voyager_posts_seen = 0

    global _linkedin_hiring_last_diag
    _linkedin_hiring_last_diag = {}

    # PASO 2 (primario): captura de red. Se engancha ya para no perder XHR.
    captured_voyager = _attach_linkedin_voyager_capture(page)

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
                _linkedin_hiring_last_diag = {
                    "authwall": True,
                    "url": last_url,
                    "kept": 0,
                }
                return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("LinkedIn #Hiring warm-up falló: %s", exc)

        # --- PASO 1 · BÚSQUEDA: keywords + filtro tiempo + sorted by Latest ---
        date_param = _linkedin_hiring_date_param(filters)

        for keyword in queries[:4]:
            if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP or (
                cancel_event and cancel_event.is_set()
            ):
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
                if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP or (
                    cancel_event and cancel_event.is_set()
                ):
                    break
                # Latest = date_posted (equivalente al botón «Latest» de LinkedIn)
                sort_latest = quote_plus('["date_posted"]')
                parts = [
                    f"keywords={quote_plus(term)}",
                    "origin=GLOBAL_SEARCH_HEADER",
                    f"sortBy={sort_latest}",
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
                    _gentle_pause(1.4, 2.2)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LinkedIn #Hiring navegación falló: %s", exc)
                    continue

                last_url = page.url or ""
                if _looks_like_linkedin_authwall(last_url):
                    hit_authwall = True
                    logger.warning("LinkedIn #Hiring authwall en %s", last_url)
                    continue

                # --- PASO 2 · EXTRACCIÓN CRUDA ---
                # Fuente primaria: JSON de red (Voyager). Se limpia por término
                # para que los posts capturados correspondan a esta búsqueda.
                captured_voyager.clear()
                # Scroll + expandir "...more" hidrata el DOM y dispara los XHR.
                _linkedin_hiring_collect_cards(page)
                voyager_posts = _linkedin_hiring_extract_via_voyager(captured_voyager)
                voyager_posts_seen += len(voyager_posts)

                # Fuente secundaria: lectura del DOM (SDUI) por si la red no llegó.
                dom_posts, roots = _linkedin_hiring_extract_via_js(page)
                js_roots = max(js_roots, roots)

                raw_posts = _linkedin_hiring_merge_posts(dom_posts, voyager_posts)
                cards_seen += len(raw_posts) if raw_posts else roots
                logger.info(
                    "LinkedIn #Hiring: voyager=%d js_posts=%d roots=%d para %r",
                    len(voyager_posts),
                    len(dom_posts),
                    roots,
                    term,
                )

                # Fallback card-by-card si ni red ni evaluate dieron permalinks.
                if not raw_posts:
                    cards = _linkedin_hiring_collect_cards(page)
                    cards_seen += len(cards)
                    for card in cards[:40]:
                        try:
                            text = _linkedin_hiring_card_text(card)
                            if not text or len(text) < 30:
                                continue
                            href = _extract_hiring_permalink(card)
                            if not href:
                                skipped_no_permalink += 1
                                continue
                            loc = _linkedin_hiring_card_location(card)
                            actor = (
                                card.query_selector(".update-components-actor__name")
                                or card.query_selector(".feed-shared-actor__name")
                                or card.query_selector('a[href*="/company/"]')
                                or card.query_selector('a[href*="/in/"]')
                            )
                            company = (
                                " ".join(
                                    ((actor.inner_text() if actor else "") or "").split()
                                )[:150]
                            )
                            time_el = card.query_selector("time")
                            published = ""
                            if time_el:
                                published = time_el.get_attribute("datetime") or (
                                    time_el.inner_text() or ""
                                )
                            raw_posts.append(
                                {
                                    "text": text,
                                    "company": company,
                                    "location": loc,
                                    "permalink": href,
                                    "published": published,
                                }
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("LinkedIn #Hiring card skip: %s", exc)

                for item in raw_posts:
                    if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP:
                        break
                    text = str(item.get("text") or "")
                    href = str(item.get("permalink") or "")
                    if not _linkedin_hiring_intent(text, permalink=href):
                        # Distinguir open-to-work vs sin señales de hiring
                        low = f" {text.lower()} "
                        if any(h in low for h in LINKEDIN_OPEN_TO_WORK_HINTS):
                            skipped_open_to_work += 1
                        else:
                            skipped_no_intent += 1
                        continue
                    if not _linkedin_hiring_query_ok(
                        text, queries, search_already_scoped=True
                    ):
                        skipped_query += 1
                        continue

                    if not href or not is_linkedin_hiring_permalink(href):
                        skipped_no_permalink += 1
                        continue
                    if href in seen:
                        continue
                    seen.add(href)

                    company = (
                        str(item.get("company") or "").strip()
                        or "Publicación LinkedIn"
                    )[:150]
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
                                    "view job",
                                )
                            )
                        ),
                        text.splitlines()[0].strip()
                        if text.splitlines()
                        else keyword,
                    )
                    published_raw = str(item.get("published") or "")
                    published_at = (
                        parse_published_at(published_raw)
                        or parse_relative_published(published_raw)
                    )

                    jobs.append(
                        {
                            "title": f"[#Hiring] {title_line}"[:200],
                            "company": company,
                            "location": str(item.get("location") or "")[:120],
                            "description": (
                                "Post de LinkedIn con intención de contratación. "
                                f"Búsqueda: {keyword}.\n\n{text[:4000]}"
                            ),
                            "url": href,
                            "source": "linkedin_hiring",
                            "published_at": published_at,
                        }
                    )
    finally:
        try:
            page.context.close()
        except Exception:  # noqa: BLE001
            pass

    _linkedin_hiring_last_diag = {
        "cards_seen": cards_seen,
        "js_roots": js_roots,
        "voyager_posts": voyager_posts_seen,
        "skip_intent": skipped_no_intent,
        "skip_open_to_work": skipped_open_to_work,
        "skip_query": skipped_query,
        "skip_permalink": skipped_no_permalink,
        "authwall": hit_authwall,
        "url": last_url,
        "kept": len(jobs),
    }

    if not jobs:
        logger.info(
            "LinkedIn #Hiring vacío (cards=%s, roots=%s, voyager=%s, "
            "skip_intent=%s, skip_open_to_work=%s, skip_query=%s, "
            "skip_permalink=%s, authwall=%s, url=%s, sesión=%s)",
            cards_seen,
            js_roots,
            voyager_posts_seen,
            skipped_no_intent,
            skipped_open_to_work,
            skipped_query,
            skipped_no_permalink,
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
