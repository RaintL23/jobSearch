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

from backend.analysis.local import linkedin_hiring_location_ok
from backend.core.dates import parse_published_at, parse_relative_published
from backend.core.query_match import matches_search_queries
from backend.scraping.browser import (
    _gentle_pause,
    _linkedin_session_ready,
    _looks_like_linkedin_authwall,
    _new_page,
)
from backend.scraping.constants import BrowserTarget
from backend.scraping.filters import _locations, _normalize_filters, _search_queries
from backend.scraping.sources.linkedin_hiring_js import _LINKEDIN_HIRING_EXTRACT_JS
from backend.scraping.sources.linkedin_hiring_permalink import (
    _ACTIVITY_LOOSE_RE,
    _ACTIVITY_RE,
    _POSTS_ID_RE,
    _UGC_POST_RE,
    _extract_hiring_permalink,
    _linkedin_activity_published_at,
    _linkedin_hiring_card_scopes,
    is_linkedin_hiring_permalink,
)

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

# Señales FUERTES de EMPLEADOR contratando (no basta el hashtag #Hiring solo).
LINKEDIN_HIRING_HINTS = (
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
    "hiring a ",
    "hiring an ",
    "hiring –",
    "hiring -",
    "we are looking to hire",
    "looking to hire",
)

# Candidatos buscando trabajo (misma búsqueda #Hiring).
# Cualquier señal de candidato descarta el post, aunque el permalink traiga
# _hiring- (muchos job-seekers hashean #Hiring) o haya texto ambiguo.
LINKEDIN_OPEN_TO_WORK_HINTS = (
    "is open to work",
    "open to work",  # "Open to Work | .NET Developer | Immediate Joiner"
    "#opentowork",
    "#open to work",
    " currently open to new opportunities",
    "looking for new opportunities as ",
    "looking for new opportunities as a",
    "looking for new opportunities",
    "actively looking for new opportunities",
    "currently looking for new opportunities",
    "i'm actively looking",
    "i am actively looking",
    "i'm looking for new",
    "i am looking for new",
    "i'm looking for a ",
    "i am looking for a ",
    "i'm looking for an ",
    "seeking new opportunities",
    "seeking opportunities as",
    "available for new opportunities",
    "available for opportunities",
    "available to join immediately",
    "view job preferences",
    "i'm currently open",
    "i am currently open",
    "open to work as",
    "hire me",
    "please hire me",
    "estoy en búsqueda activa",
    "estoy en busqueda activa",
    "estoy buscando trabajo",
    "estoy buscando empleo",
    "busco empleo",
    "busco trabajo",
    "busco oportunidades como",
    "busco oportunidades",
    "me encuentro en búsqueda",
    "me encuentro en busqueda",
    "en búsqueda de nuevas oportunidades",
    "en busqueda de nuevas oportunidades",
    "disponible para nuevas oportunidades",
)

LINKEDIN_HIRING_SOFT_CAP = 25

# Diagnóstico del último scrape #Hiring (para el mensaje UI si vuelve 0).
_linkedin_hiring_last_diag: dict[str, Any] = {}


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
        _linkedin_hiring_expand_all(page, rounds=2)
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
    # Por si este card todavía muestra «...more».
    try:
        card.evaluate(
            """(root) => {
              const DOTS_MORE = /(?:\\.{3}|…|\\u2026)\\s*more\\s*$/i;
              const ONLY = /^(?:\\.{3}|…|\\u2026)?\\s*(see more|show more|ver m[aá]s|more|m[aá]s)\\s*$/i;
              const SKIP = /see less|show less|ver menos|mostrar menos/i;
              const norm = (el) => (el.innerText || el.textContent || '')
                .replace(/[\\u200b\\ufeff]/g, '').replace(/\\s+/g, ' ').trim();
              const list = [];
              for (const el of root.querySelectorAll(
                'button, span[role="button"], a[role="button"], span, a'
              )) {
                const t = norm(el);
                if (!t || t.length > 16 || SKIP.test(t)) continue;
                if (ONLY.test(t) || DOTS_MORE.test(t)) list.push(el);
              }
              list.sort((a, b) => norm(a).length - norm(b).length);
              for (const el of list.slice(0, 3)) {
                try { el.click(); } catch (_) {}
              }
            }"""
        )
    except Exception:  # noqa: BLE001
        pass
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
                return re.sub(
                    r"\s*[….]*\s*(see more|show more|ver más|ver mas|more|más)\s*$",
                    "",
                    t,
                    flags=re.I,
                ).strip()
    try:
        t = (card.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(
        r"\s*[….]*\s*(see more|show more|ver más|ver mas|more|más)\s*$",
        "",
        t,
        flags=re.I,
    ).strip()


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


def _linkedin_hiring_expand(page: Page) -> int:
    """
    PASO 2 · click en '...more' / '…more' / 'ver más' para sacar el texto completo.

    En search/content LinkedIn muestra el control inline como «...more» (tres
    puntos + more) al final de la línea truncada, a menudo en gris.
    """
    clicked = 0

    # 1) Selectores conocidos (legacy + SDUI).
    for sel in (
        'button[aria-label*="see more" i]',
        'button[aria-label*="show more" i]',
        'button[aria-label*="ver más" i]',
        'button[aria-label*="ver mas" i]',
        'button[aria-label*="mostrar más" i]',
        "button.feed-shared-inline-show-more-text__see-more-less-toggle",
        "button.feed-shared-inline-show-more-text__button",
        "button.inline-show-more-text__button",
        '[data-testid="expandable-text-button"]',
        "button.feed-shared-update-v2__commentary-button",
        ".update-components-text button",
        ".feed-shared-update-v2__description button",
        '[data-testid="expandable-text-box"] button',
        '[data-testid="expandable-text-box"] span[role="button"]',
        # El control típico actual: texto visible "...more"
        "button.feed-shared-inline-show-more-text__see-more-less-toggle span",
    ):
        try:
            buttons = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for btn in buttons[:30]:
            try:
                if not btn.is_visible():
                    continue
                label = (
                    (btn.get_attribute("aria-label") or "")
                    + " "
                    + (btn.inner_text() or "")
                ).lower()
                if "less" in label or "menos" in label:
                    continue
                btn.click(timeout=900, force=True)
                clicked += 1
            except Exception:  # noqa: BLE001
                pass

    # 2) Click directo por texto "...more" / "…more" (como se ve en el browser).
    for pattern in (
        re.compile(r"^\.\.\.\s*more$", re.I),
        re.compile(r"^…\s*more$", re.I),
        re.compile(r"^\.\.\.\s*más$", re.I),
        re.compile(r"^…\s*más$", re.I),
    ):
        try:
            loc = page.get_by_text(pattern)
            count = min(loc.count(), 25)
            for i in range(count):
                try:
                    item = loc.nth(i)
                    if item.is_visible(timeout=300):
                        item.click(timeout=900, force=True)
                        clicked += 1
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            continue

    # 3) Fallback DOM: el nodo más chico cuyo texto es/termina en "...more".
    try:
        n = int(
            page.evaluate(
                """() => {
                  // Caso real del UI: "...more" o "…more" (a veces con espacio).
                  const DOTS_MORE = /(?:\\.{3}|…|\\u2026)\\s*more\\s*$/i;
                  const DOTS_MAS = /(?:\\.{3}|…|\\u2026)\\s*m[aá]s\\s*$/i;
                  const ONLY_MORE = /^(?:\\.{3}|…|\\u2026)?\\s*(see more|show more|ver m[aá]s|mostrar m[aá]s|more|m[aá]s)\\s*$/i;
                  const SKIP = /see less|show less|ver menos|mostrar menos/i;

                  const roots = Array.from(document.querySelectorAll(
                    'div.feed-shared-update-v2, div.reusable-search__result-container, '
                    + 'li.reusable-search__result-container, div[role="listitem"], '
                    + '[data-testid="expandable-text-box"], .update-components-text, '
                    + '.feed-shared-update-v2__description, .feed-shared-text, .break-words'
                  ));
                  const scope = roots.length ? roots : [document.body];

                  function norm(el) {
                    return (el.innerText || el.textContent || '')
                      .replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '')
                      .replace(/\\s+/g, ' ')
                      .trim();
                  }

                  function isExpandControl(el) {
                    const raw = norm(el);
                    if (!raw || SKIP.test(raw)) return false;
                    // Preferir el control corto "...more" (no el párrafo entero).
                    if (raw.length <= 16 && (ONLY_MORE.test(raw) || DOTS_MORE.test(raw) || DOTS_MAS.test(raw))) {
                      return true;
                    }
                    // A veces el botón trae solo "more" y los puntos están en un hermano.
                    if (raw.length <= 8 && /^more$/i.test(raw)) {
                      const prev = (el.previousSibling && (el.previousSibling.textContent || '')) || '';
                      if (/(?:\\.{3}|…|\\u2026)\\s*$/.test(prev)) return true;
                      const parent = norm(el.parentElement || el);
                      if (DOTS_MORE.test(parent) || DOTS_MAS.test(parent)) return true;
                    }
                    return false;
                  }

                  const candidates = [];
                  for (const root of scope) {
                    for (const el of root.querySelectorAll(
                      'button, span[role="button"], a[role="button"], span, a, em, strong'
                    )) {
                      if (el.closest('header, nav, [data-test-global-nav], .global-nav')) {
                        continue;
                      }
                      if (isExpandControl(el)) candidates.push(el);
                    }
                  }
                  // Más específicos primero (nodos chicos / profundos).
                  candidates.sort((a, b) => norm(a).length - norm(b).length);

                  let n = 0;
                  const seen = new Set();
                  for (const el of candidates) {
                    if (n >= 40) break;
                    if (seen.has(el)) continue;
                    // Si un ancestro ya se clickeó, skip.
                    let skip = false;
                    for (const s of seen) {
                      if (s.contains && s.contains(el)) { skip = true; break; }
                    }
                    if (skip) continue;
                    seen.add(el);
                    try {
                      el.click();
                      n++;
                    } catch (_) {
                      try {
                        el.dispatchEvent(new MouseEvent('click', {
                          bubbles: true, cancelable: true, view: window
                        }));
                        n++;
                      } catch (_) {}
                    }
                  }
                  return n;
                }"""
            )
            or 0
        )
        clicked += n
    except Exception:  # noqa: BLE001
        pass

    if clicked:
        _gentle_pause(0.35, 0.6)
        logger.info("LinkedIn #Hiring: expandí %d «...more»", clicked)
    return clicked


def _linkedin_hiring_expand_all(page: Page, *, rounds: int = 3) -> int:
    """Varias pasadas: al expandir un post a veces aparece otro '…more'."""
    total = 0
    for i in range(max(1, rounds)):
        n = _linkedin_hiring_expand(page)
        total += n
        if n == 0:
            break
        _gentle_pause(0.25, 0.45)
        if i == 0:
            # Un scroll corto ayuda a hidratar line-clamp diferido.
            try:
                page.mouse.wheel(0, 400)
            except Exception:  # noqa: BLE001
                pass
            _gentle_pause(0.2, 0.35)
    return total


def _linkedin_hiring_parse_published(raw: str) -> str | None:
    """Normaliza '7h', '2d', datetime ISO, etc. a published_at."""
    text = " ".join(str(raw or "").replace("•", " ").split()).strip()
    if not text:
        return None
    # Preferir un stamp corto si el blob trae basura alrededor.
    m = re.search(
        r"\b(\d+\s*(?:m|min|mins|h|hr|hrs|d|w|wk|mo|y|hora|horas|dia|días|dias|sem(?:ana)?s?))\b",
        text,
        flags=re.I,
    )
    probe = m.group(1) if m else text
    return parse_published_at(probe) or parse_relative_published(probe) or (
        parse_published_at(text) or parse_relative_published(text)
    )


def _linkedin_hiring_location_from_text(text: str) -> str:
    """Best-effort: saca ubicación explícita del cuerpo del post."""
    blob = text or ""
    patterns = (
        r"(?i)\blocation\s*[:\-]\s*([^\n|;]{2,60})",
        r"(?i)ubicaci[oó]n\s*[:\-]\s*([^\n|;]{2,60})",
        r"(?i)\b(?:based in|located in|en)\s+"
        r"(m[eé]xico|mexico|argentina|colombia|chile|per[uú]|brasil|brazil|"
        r"uruguay|ecuador|latam|latin america|latinoam[eé]rica|"
        r"india|bangalore|bengaluru|hyderabad|remoto|remote|worldwide)"
        r"([^\n|;]{0,40})",
        r"(?i)\b(cdmx|ciudad de m[eé]xico|buenos aires|caba|bogot[aá]|"
        r"santiago|lima|remoto latam|remote latam|"
        r"bangalore|bengaluru|hyderabad|pune|chennai|mumbai|"
        r"across india|pan[\s\-]?india)\b",
        # Título tipo "📍 Bangalore | …" / "… Across India"
        r"(?i)(?:^|\||📍)\s*(bangalore|bengaluru|hyderabad|pune|chennai|"
        r"mumbai|noida|gurgaon|gurugram|delhi|india)\b",
    )
    for pat in patterns:
        m = re.search(pat, blob)
        if not m:
            continue
        if m.lastindex and m.lastindex >= 1:
            bit = " ".join(g for g in m.groups() if g).strip(" .,-")
        else:
            bit = m.group(0).strip(" .,-")
        bit = " ".join(bit.split()).strip(" .,-!|📍")
        if 2 <= len(bit) <= 80:
            return bit[:120]
    # Señal corta al final del título: "... México! MX"
    m = re.search(
        r"(?i)\b(m[eé]xico|argentina|colombia|chile|per[uú]|brasil|latam)\b"
        r"(?:\s*[!|]?\s*mx|\s*ar|\s*co)?\s*$",
        blob.split("\n", 1)[0],
    )
    if m:
        return " ".join(m.group(0).replace("!", " ").split()).strip(" .,-|")[:120]
    return ""


def _linkedin_hiring_intent(text: str, *, permalink: str = "") -> bool:
    """
    True si el post parece oferta de empleador (no candidato open-to-work).

    Regla clave: el hashtag #Hiring solo NO alcanza. Muchos candidatos lo usan
    («Open to Work | .NET… #Hiring»). Cualquier señal de job-seeker descarta,
    aunque el slug del permalink lleve `_hiring-`.
    """
    low = f" {(text or '').lower()} "
    slug = (permalink or "").lower()

    def _strong_employer(blob: str) -> bool:
        if any(k in blob for k in LINKEDIN_HIRING_HINTS if k != "view job"):
            return True
        return "view job" in blob and "view job preferences" not in blob

    def _is_seeker(blob: str) -> bool:
        return any(h in blob for h in LINKEDIN_OPEN_TO_WORK_HINTS)

    # Candidato → siempre afuera (gana sobre señales de empleador / slug).
    if _is_seeker(low):
        return False

    if _strong_employer(low):
        return True

    # Permalink canónico a veces incluye hashtags del post (_hiring-…).
    if "hiring" in slug or "contrat" in slug or "vacante" in slug:
        return True

    # Solo #Hiring / token débil sin más contexto → insuficiente.
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
    js_no_permalink = int(payload.get("noPermalink") or 0)
    js_too_short = int(payload.get("tooShort") or 0)
    js_expanded = int(payload.get("expanded") or 0)
    if not isinstance(posts, list):
        return [], roots
    cleaned: list[dict[str, Any]] = []
    skipped_bad_link = 0
    for item in posts:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        permalink = str(item.get("permalink") or "").strip()
        if len(text) < 30 or not permalink:
            continue
        if not is_linkedin_hiring_permalink(permalink):
            skipped_bad_link += 1
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
    if roots or posts or js_no_permalink:
        logger.info(
            "LinkedIn #Hiring JS: roots=%d con_permalink=%d ok=%d "
            "−sin_permalink_dom=%d −texto_corto=%d −permalink_inválido=%d "
            "expanded=%d",
            roots,
            len(posts),
            len(cleaned),
            js_no_permalink,
            js_too_short,
            skipped_bad_link,
            js_expanded,
        )
    return cleaned, roots


# --- PASO 2 · EXTRACCIÓN PRIMARIA POR RED (Voyager / GraphQL) ----------------
# LinkedIn pinta la búsqueda con JSON de su API interna. Interceptar esas
# respuestas es más preciso que raspar el DOM: trae el texto COMPLETO (no el
# "…more" truncado) y el urn del post, con lo que el permalink es fiable.
# Aceptamos cualquier respuesta /voyager/api/*; el extract filtra por commentary.


def _attach_linkedin_voyager_capture(page: Page) -> dict[str, Any]:
    """
    Engancha un listener de red y acumula los JSON internos de LinkedIn.

    Devuelve un dict mutable: payloads (lista), stats (contadores).
    El caller limpia payloads por término ANTES de navegar.
    """
    state: dict[str, Any] = {
        "payloads": [],
        "seen_urls": 0,
        "captured": 0,
        "json_fail": 0,
        "sample_urls": [],
    }

    def _on_response(resp: Any) -> None:
        try:
            url = (resp.url or "").lower()
        except Exception:  # noqa: BLE001
            return
        if "voyager/api" not in url:
            return
        state["seen_urls"] += 1
        try:
            if resp.status != 200:
                return
        except Exception:  # noqa: BLE001
            pass
        if len(state["sample_urls"]) < 8:
            # Guardar path corto para diagnóstico (sin query enorme).
            short = url.split("?", 1)[0]
            if short not in state["sample_urls"]:
                state["sample_urls"].append(short)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            try:
                raw = resp.body()
                data = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                state["json_fail"] += 1
                return
        if isinstance(data, (dict, list)):
            state["payloads"].append(data)
            state["captured"] += 1

    try:
        page.on("response", _on_response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LinkedIn #Hiring: no se pudo enganchar la red: %s", exc)
    return state


def _voyager_capture_reset(state: dict[str, Any]) -> None:
    """Limpia payloads/contadores de un término (antes de navegar)."""
    state["payloads"].clear()
    state["seen_urls"] = 0
    state["captured"] = 0
    state["json_fail"] = 0
    state["sample_urls"] = []


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
    conserva company/location/published del DOM si la red no los trajo.
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
            # Voyager a menudo mete el headline del actor en subDescription;
            # el stamp relativo (16m / 2d) suele venir solo del DOM.
            if not _linkedin_hiring_parse_published(str(post.get("published") or "")):
                if existing.get("published"):
                    post["published"] = existing["published"]
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


_CONTROL_MENU_SELECTORS = (
    "button.feed-shared-control-menu__trigger",
    'button[aria-label*="Open control menu" i]',
    'button[aria-label*="control menu" i]',
    'button[aria-label*="Más acciones" i]',
    'button[aria-label*="More actions" i]',
    'button[aria-label*="Más" i]',
    'button[aria-label*="More" i]',
    "button.artdeco-dropdown__trigger",
)

_COPY_LINK_TEXT_RES = (
    re.compile(r"Copy link to post", re.I),
    re.compile(r"Copiar enlace de la publicaci[oó]n", re.I),
    re.compile(r"Copiar enlace al post", re.I),
    re.compile(r"Copiar enlace del post", re.I),
    re.compile(r"Copiar enlace", re.I),
)


def _install_linkedin_clipboard_hook(page: Page) -> None:
    """
    Intercepta navigator.clipboard.writeText para leer el URL que LinkedIn
    copia al hacer «Copy link to post», sin depender de permisos de lectura.
    """
    try:
        page.evaluate(
            """() => {
              if (window.__jobsearchCopyHook === 'ok') return;
              window.__jobsearchCopiedUrl = '';
              try {
                const clip = navigator.clipboard;
                if (!clip || typeof clip.writeText !== 'function') {
                  window.__jobsearchCopyHook = 'missing';
                  return;
                }
                const orig = clip.writeText.bind(clip);
                clip.writeText = async (text) => {
                  window.__jobsearchCopiedUrl = String(text || '');
                  try { return await orig(text); } catch (_) {}
                };
                window.__jobsearchCopyHook = 'ok';
              } catch (_) {
                window.__jobsearchCopyHook = 'error';
              }
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("LinkedIn #Hiring: clipboard hook falló: %s", exc)


def _dismiss_linkedin_menus(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    _gentle_pause(0.12, 0.22)


def _normalize_copied_permalink(raw: str) -> str:
    href = (raw or "").strip().split("?", 1)[0].split("#", 1)[0]
    if href.startswith("/"):
        href = "https://www.linkedin.com" + href
    if is_linkedin_hiring_permalink(href):
        return href if href.endswith("/") else href + "/"
    m = _ACTIVITY_RE.search(href) or _ACTIVITY_LOOSE_RE.search(href)
    if m:
        return (
            "https://www.linkedin.com/feed/update/urn:li:activity:"
            + m.group(1)
            + "/"
        )
    m = _UGC_POST_RE.search(href)
    if m:
        return (
            "https://www.linkedin.com/feed/update/urn:li:ugcPost:"
            + m.group(1)
            + "/"
        )
    m = _POSTS_ID_RE.search(urlsplit(href).path or "")
    if m and "/posts/" in href:
        path = urlsplit(href).path or ""
        return "https://www.linkedin.com" + path.rstrip("/") + "/"
    return ""


def _find_control_menu_button(card: Any) -> Any | None:
    for scope in _linkedin_hiring_card_scopes(card):
        for sel in _CONTROL_MENU_SELECTORS:
            try:
                btn = scope.query_selector(sel)
            except Exception:  # noqa: BLE001
                btn = None
            if btn:
                return btn
        # Último recurso: botón con menú en la esquina del post.
        try:
            btn = scope.query_selector(
                'button[aria-haspopup="menu"], button[aria-expanded]'
            )
        except Exception:  # noqa: BLE001
            btn = None
        if btn:
            return btn
    return None


def _permalink_via_copy_link_menu(page: Page, card: Any) -> str:
    """
    Igual que un humano: ⋯ → «Copy link to post» / «Copiar enlace…».

    LinkedIn a menudo no expone el urn en el DOM de search/content; el menú
    sí conoce el permalink canónico y lo escribe al clipboard.
    """
    _install_linkedin_clipboard_hook(page)
    try:
        page.evaluate("() => { window.__jobsearchCopiedUrl = ''; }")
    except Exception:  # noqa: BLE001
        pass

    btn = _find_control_menu_button(card)
    if not btn:
        return ""

    try:
        btn.scroll_into_view_if_needed(timeout=1500)
    except Exception:  # noqa: BLE001
        pass
    try:
        btn.click(timeout=2000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("LinkedIn #Hiring: no se abrió menú ⋯: %s", exc)
        return ""
    _gentle_pause(0.35, 0.6)

    clicked = False
    for pattern in _COPY_LINK_TEXT_RES:
        try:
            item = page.get_by_text(pattern).first
            if item.is_visible(timeout=800):
                item.click(timeout=1500)
                clicked = True
                break
        except Exception:  # noqa: BLE001
            continue

    if not clicked:
        try:
            clicked = bool(
                page.evaluate(
                    """() => {
                      const re = /copy\\s+link|copiar\\s+enlace/i;
                      const nodes = Array.from(document.querySelectorAll(
                        '[role="menuitem"], [role="option"], button, '
                        + 'div.artdeco-dropdown__item, li.artdeco-dropdown__item, '
                        + 'div[role="button"]'
                      ));
                      for (const el of nodes) {
                        const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (!re.test(t) || t.length > 90) continue;
                        try { el.click(); return true; } catch (_) {}
                      }
                      return false;
                    }"""
                )
            )
        except Exception:  # noqa: BLE001
            clicked = False

    if not clicked:
        _dismiss_linkedin_menus(page)
        return ""

    _gentle_pause(0.3, 0.55)
    try:
        raw = page.evaluate("() => window.__jobsearchCopiedUrl || ''") or ""
    except Exception:  # noqa: BLE001
        raw = ""
    # Fallback: leer clipboard si el hook no capturó writeText.
    if not raw:
        try:
            page.context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://www.linkedin.com",
            )
            raw = page.evaluate(
                """async () => {
                  try { return await navigator.clipboard.readText(); }
                  catch (_) { return ''; }
                }"""
            ) or ""
        except Exception:  # noqa: BLE001
            raw = ""

    _dismiss_linkedin_menus(page)
    return _normalize_copied_permalink(str(raw))


def _linkedin_hiring_extract_via_copy_link_menu(
    page: Page,
    *,
    limit: int = 20,
    cancel_event: Event | None = None,
) -> list[dict[str, Any]]:
    """
    PASO 2 (rescate) · para posts sin urn en el DOM, usa el menú ⋯ → Copy link.
    Más lento que el evaluate masivo, pero es el mismo link que ve el usuario.
    """
    cards = _linkedin_hiring_collect_cards(page)
    if not cards:
        return []

    _install_linkedin_clipboard_hook(page)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    tried = 0
    for card in cards:
        if cancel_event and cancel_event.is_set():
            break
        if len(out) >= limit:
            break
        if tried >= limit * 2:
            break
        text = _linkedin_hiring_card_text(card)
        if not text or len(text) < 30:
            continue
        # Si el DOM ya trae permalink, no hace falta abrir el menú.
        existing = _extract_hiring_permalink(card)
        if existing and is_linkedin_hiring_permalink(existing):
            continue
        tried += 1
        href = _permalink_via_copy_link_menu(page, card)
        if not href:
            logger.info(
                "LinkedIn #Hiring: − menú ⋯ sin link | %s",
                " ".join(text.split())[:90],
            )
            continue
        if href in seen:
            continue
        seen.add(href)

        actor = None
        try:
            actor = (
                card.query_selector(".update-components-actor__name")
                or card.query_selector(".feed-shared-actor__name")
                or card.query_selector('a[href*="/company/"]')
                or card.query_selector('a[href*="/in/"]')
            )
        except Exception:  # noqa: BLE001
            actor = None
        company = ""
        if actor:
            try:
                company = " ".join((actor.inner_text() or "").split())[:150]
            except Exception:  # noqa: BLE001
                company = ""
        published = ""
        try:
            time_el = card.query_selector("time")
            if time_el:
                published = time_el.get_attribute("datetime") or (
                    time_el.inner_text() or ""
                )
        except Exception:  # noqa: BLE001
            published = ""

        out.append(
            {
                "text": text[:4000],
                "company": company,
                "location": _linkedin_hiring_card_location(card),
                "permalink": href,
                "published": " ".join(str(published).split())[:80],
            }
        )
        logger.info(
            "LinkedIn #Hiring: ✓ permalink vía «Copy link to post» | %s | %s",
            (company or "?")[:40],
            href[:90],
        )

    logger.info(
        "LinkedIn #Hiring: menú ⋯ → %d permalink(s) (intentos=%d, cards=%d)",
        len(out),
        tried,
        len(cards),
    )
    return out


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
    user_locations = [loc for loc in _locations(profile, filters) if str(loc).strip()]
    user_country = ""
    countries = filters.get("countries") or []
    if countries:
        user_country = str(countries[0]).strip().lower()
    elif profile.get("country"):
        user_country = str(profile.get("country") or "").strip().lower()
    page = _new_page(browser, site="linkedin_hiring")
    try:
        page.context.grant_permissions(
            ["clipboard-read", "clipboard-write"],
            origin="https://www.linkedin.com",
        )
    except Exception:  # noqa: BLE001
        pass
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    hit_authwall = False
    last_url = ""
    cards_seen = 0
    skipped_no_intent = 0
    skipped_open_to_work = 0
    skipped_query = 0
    skipped_location = 0
    skipped_no_permalink = 0
    skipped_dup = 0
    js_roots = 0
    voyager_posts_seen = 0
    bad_permalink_samples: list[str] = []

    global _linkedin_hiring_last_diag
    _linkedin_hiring_last_diag = {}

    # PASO 2 (primario): captura de red. Se engancha ya para no perder XHR.
    voyager_state = _attach_linkedin_voyager_capture(page)

    try:
        # Warm-up: entrar al feed con la sesión antes de buscar
        try:
            logger.info("LinkedIn #Hiring: abriendo feed para validar sesión…")
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
            logger.info("LinkedIn #Hiring: sesión OK en feed (%s)", last_url[:80])
        except Exception as exc:  # noqa: BLE001
            logger.warning("LinkedIn #Hiring warm-up falló: %s", exc)

        # --- PASO 1 · BÚSQUEDA: keywords + filtro tiempo + sorted by Latest ---
        date_param = _linkedin_hiring_date_param(filters)
        logger.info(
            "LinkedIn #Hiring: %d keyword(s), datePosted=%s, soft_cap=%s",
            len(queries[:4]),
            date_param or "—",
            LINKEDIN_HIRING_SOFT_CAP,
        )

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
                # Limpiar ANTES del goto: los XHR de resultados llegan al cargar.
                _voyager_capture_reset(voyager_state)
                logger.info("LinkedIn #Hiring: buscando %r → %s", term, url)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    _gentle_pause(1.4, 2.2)
                    # Dar tiempo a que terminen los XHR Voyager del primer paint.
                    try:
                        page.wait_for_load_state("networkidle", timeout=4500)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LinkedIn #Hiring navegación falló: %s", exc)
                    continue

                last_url = page.url or ""
                if _looks_like_linkedin_authwall(last_url):
                    hit_authwall = True
                    logger.warning("LinkedIn #Hiring authwall en %s", last_url)
                    continue

                # --- PASO 2 · EXTRACCIÓN CRUDA ---
                # Scroll + expandir "...more" hidrata el DOM y dispara más XHR.
                _linkedin_hiring_collect_cards(page)
                # Pasada extra de «ver más» antes de leer el texto (por si quedó
                # algún line-clamp tras el scroll).
                expanded = _linkedin_hiring_expand_all(page, rounds=3)
                if expanded:
                    logger.info(
                        "LinkedIn #Hiring: texto expandido (%d clicks) antes de extraer",
                        expanded,
                    )
                voyager_posts = _linkedin_hiring_extract_via_voyager(
                    voyager_state["payloads"]
                )
                voyager_posts_seen += len(voyager_posts)
                logger.info(
                    "LinkedIn #Hiring: red voyager urls=%d json=%d fail=%d "
                    "posts=%d samples=%s",
                    voyager_state["seen_urls"],
                    voyager_state["captured"],
                    voyager_state["json_fail"],
                    len(voyager_posts),
                    voyager_state["sample_urls"][:4] or "—",
                )

                # Fuente secundaria: lectura del DOM (SDUI) por si la red no llegó.
                dom_posts, roots = _linkedin_hiring_extract_via_js(page)
                js_roots = max(js_roots, roots)

                # Rescate: posts sin urn en el DOM → ⋯ → «Copy link to post».
                missing_est = max(0, roots - len(dom_posts))
                if missing_est > 0 or not dom_posts:
                    menu_limit = min(20, max(missing_est, 12 if not dom_posts else 8))
                    logger.info(
                        "LinkedIn #Hiring: %d post(s) sin permalink en DOM; "
                        "probando menú ⋯ «Copy link to post» (hasta %d)…",
                        missing_est or roots,
                        menu_limit,
                    )
                    menu_posts = _linkedin_hiring_extract_via_copy_link_menu(
                        page,
                        limit=menu_limit,
                        cancel_event=cancel_event,
                    )
                    if menu_posts:
                        dom_posts = _linkedin_hiring_merge_posts(dom_posts, menu_posts)

                raw_posts = _linkedin_hiring_merge_posts(dom_posts, voyager_posts)
                cards_seen += len(raw_posts) if raw_posts else roots
                logger.info(
                    "LinkedIn #Hiring: término %r → voyager=%d js=%d roots=%d "
                    "merge=%d (acum. guardados=%d)",
                    term,
                    len(voyager_posts),
                    len(dom_posts),
                    roots,
                    len(raw_posts),
                    len(jobs),
                )

                # Fallback card-by-card si ni red ni evaluate dieron permalinks.
                if not raw_posts:
                    cards = _linkedin_hiring_collect_cards(page)
                    cards_seen += len(cards)
                    logger.info(
                        "LinkedIn #Hiring: fallback card-by-card (%d cards)",
                        len(cards),
                    )
                    for card in cards[:40]:
                        try:
                            text = _linkedin_hiring_card_text(card)
                            if not text or len(text) < 30:
                                continue
                            href = _extract_hiring_permalink(card)
                            if not href:
                                href = _permalink_via_copy_link_menu(page, card)
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
                    logger.info(
                        "LinkedIn #Hiring: fallback extrajo %d posts "
                        "(sin permalink=%d en este paso)",
                        len(raw_posts),
                        skipped_no_permalink,
                    )

                term_kept = 0
                term_skip_intent = 0
                term_skip_otw = 0
                term_skip_query = 0
                term_skip_location = 0
                term_skip_permalink = 0
                term_skip_dup = 0

                def _log_discard(reason: str, detail: str, count: int) -> None:
                    # Primeras 5 de cada motivo por término; el resto va al resumen.
                    if count <= 5:
                        logger.info(
                            "LinkedIn #Hiring: − %s | %s",
                            reason,
                            detail,
                        )

                for item in raw_posts:
                    if len(jobs) >= LINKEDIN_HIRING_SOFT_CAP:
                        break
                    text = str(item.get("text") or "")
                    href = str(item.get("permalink") or "")
                    preview = " ".join(text.split())[:90]
                    if not _linkedin_hiring_intent(text, permalink=href):
                        # Distinguir open-to-work vs sin señales de hiring
                        low = f" {text.lower()} "
                        if any(h in low for h in LINKEDIN_OPEN_TO_WORK_HINTS):
                            skipped_open_to_work += 1
                            term_skip_otw += 1
                            _log_discard("open-to-work", preview, term_skip_otw)
                        else:
                            skipped_no_intent += 1
                            term_skip_intent += 1
                            _log_discard(
                                "sin intención hiring", preview, term_skip_intent
                            )
                        continue
                    if not _linkedin_hiring_query_ok(
                        text, queries, search_already_scoped=True
                    ):
                        skipped_query += 1
                        term_skip_query += 1
                        _log_discard("no matchea query", preview, term_skip_query)
                        continue

                    # Filtro geográfico temprano (India/Bangalore / LATAM estricto)
                    # para no llenar el soft_cap con posts irrelevantes.
                    loc_verdict = linkedin_hiring_location_ok(
                        text, user_country, user_locations
                    )
                    if loc_verdict is False:
                        skipped_location += 1
                        term_skip_location += 1
                        _log_discard("ubicación fuera de alcance", preview, term_skip_location)
                        continue

                    if not href or not is_linkedin_hiring_permalink(href):
                        skipped_no_permalink += 1
                        term_skip_permalink += 1
                        if href and len(bad_permalink_samples) < 6:
                            bad_permalink_samples.append(href[:160])
                        _log_discard(
                            f"sin permalink válido ({(href[:80] if href else 'vacío')})",
                            preview,
                            term_skip_permalink,
                        )
                        continue
                    if href in seen:
                        skipped_dup += 1
                        term_skip_dup += 1
                        _log_discard("duplicado", href[:90], term_skip_dup)
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
                                    "we're hiring",
                                    "we are hiring",
                                    "contrat",
                                    "buscamos",
                                    "vacante",
                                    "oportunidad",
                                    "sumate",
                                    "sumáte",
                                    "view job",
                                    "hiring a",
                                    "hiring an",
                                )
                            )
                        ),
                        text.splitlines()[0].strip()
                        if text.splitlines()
                        else keyword,
                    )
                    published_raw = str(item.get("published") or "")
                    published_at = (
                        _linkedin_hiring_parse_published(published_raw)
                        or _linkedin_activity_published_at(href)
                    )
                    location = (
                        str(item.get("location") or "").strip()
                        or _linkedin_hiring_location_from_text(text)
                    )[:120]

                    jobs.append(
                        {
                            "title": f"[#Hiring] {title_line}"[:200],
                            "company": company,
                            "location": location,
                            "description": (
                                "Post de LinkedIn con intención de contratación. "
                                f"Búsqueda: {keyword}.\n\n{text[:4000]}"
                            ),
                            "url": href,
                            "source": "linkedin_hiring",
                            "published_at": published_at,
                        }
                    )
                    term_kept += 1
                    logger.info(
                        "LinkedIn #Hiring: ✓ guardado #%d | %s | %s | "
                        "loc=%s | pub=%s | %s",
                        len(jobs),
                        company[:40],
                        title_line[:60],
                        location or "—",
                        published_raw or published_at or "—",
                        href[:90],
                    )

                logger.info(
                    "LinkedIn #Hiring: resumen %r → +%d | −intent=%d −otw=%d "
                    "−query=%d −loc=%d −permalink=%d −dup=%d | total=%d",
                    term,
                    term_kept,
                    term_skip_intent,
                    term_skip_otw,
                    term_skip_query,
                    term_skip_location,
                    term_skip_permalink,
                    term_skip_dup,
                    len(jobs),
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
        "skip_location": skipped_location,
        "skip_permalink": skipped_no_permalink,
        "skip_dup": skipped_dup,
        "bad_permalink_samples": bad_permalink_samples,
        "authwall": hit_authwall,
        "url": last_url,
        "kept": len(jobs),
    }

    if not jobs:
        logger.info(
            "LinkedIn #Hiring vacío (cards=%s, roots=%s, voyager=%s, "
            "skip_intent=%s, skip_open_to_work=%s, skip_query=%s, "
            "skip_location=%s, skip_permalink=%s, skip_dup=%s, "
            "authwall=%s, url=%s, sesión=%s)%s",
            cards_seen,
            js_roots,
            voyager_posts_seen,
            skipped_no_intent,
            skipped_open_to_work,
            skipped_query,
            skipped_location,
            skipped_no_permalink,
            skipped_dup,
            hit_authwall,
            last_url,
            _linkedin_session_ready(),
            (
                f" bad_permalinks={bad_permalink_samples}"
                if bad_permalink_samples
                else ""
            ),
        )
    else:
        logger.info(
            "LinkedIn #Hiring: listo — %d oferta(s) "
            "(voyager=%d, −intent=%d −otw=%d −query=%d −loc=%d "
            "−permalink=%d −dup=%d)",
            len(jobs),
            voyager_posts_seen,
            skipped_no_intent,
            skipped_open_to_work,
            skipped_query,
            skipped_location,
            skipped_no_permalink,
            skipped_dup,
        )
    if hit_authwall and jobs:
        logger.info(
            "LinkedIn #Hiring: authwall parcial (última url=%s, sesión=%s)",
            last_url,
            _linkedin_session_ready(),
        )
    return jobs
