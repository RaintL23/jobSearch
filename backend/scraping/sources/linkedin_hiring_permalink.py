"""
Detección y extracción de permalinks de posts LinkedIn #Hiring.

Regexes de urn:li:activity / ugcPost, validación de que una URL es un post
individual (no empresa/showcase/búsqueda), extracción del permalink desde el
DOM de la card (Playwright) y fecha inferida del snowflake del activity id.

Funciones puras / DOM-only: no dependen del resto del scraper.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


_ACTIVITY_RE = re.compile(r"urn:li:activity:(\d{6,})")


_UGC_POST_RE = re.compile(r"urn:li:ugcPost:(\d{6,})")


_ACTIVITY_LOOSE_RE = re.compile(r"activity[:\-](\d{6,})")


_POSTS_ID_RE = re.compile(r"-(\d{15,})-[A-Za-z0-9_]+/?$")


_FEED_POST_PATH_RE = re.compile(
    r"^/feed/update/urn:li:(?:activity|ugcPost):\d{6,}/?$",
    re.IGNORECASE,
)


def _linkedin_activity_published_at(permalink: str) -> str | None:
    """
    Fallback: el ID de activity/ugcPost de LinkedIn es un snowflake
    (timestamp_ms = id >> 22). Sirve cuando el DOM no expone '16m'/'2d'.
    """
    blob = permalink or ""
    m = (
        _ACTIVITY_RE.search(blob)
        or _UGC_POST_RE.search(blob)
        or _ACTIVITY_LOOSE_RE.search(blob)
        or _POSTS_ID_RE.search(blob)
    )
    if not m:
        return None
    try:
        activity_id = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if activity_id < 1_000_000_000_000_000:  # IDs reales ~18 dígitos
        return None
    try:
        from datetime import datetime, timezone

        ts_ms = activity_id >> 22
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        if not (2015 <= dt.year <= 2100):
            return None
        return dt.isoformat()
    except (OverflowError, OSError, ValueError):
        return None


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

    Prioridad (solo DOM, sin clicks):
      1) href /posts/… ya presente en el DOM (formato canónico de compartir).
      2) permalink construido desde el id de activity → /feed/update/…
      3) href /feed/update/… presente en el DOM.
      4) scan JS profundo del nodo (componentkey / ancestros / timestamp).

    Si esto falla, el caller puede usar `_permalink_via_copy_link_menu`
    (⋯ → Copy link to post), que es más lento pero fiable.
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
