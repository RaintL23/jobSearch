"""
Script JS inyectado en la página de resultados de LinkedIn #Hiring para
extraer posts (permalink, texto, timestamp) directamente del DOM/SDUI.

Aislado en su propio módulo por tamaño; lo consume linkedin_hiring.
"""

from __future__ import annotations


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

  function expandInRoot(root) {
    // UI real: control inline "...more" / "…more" al final de la línea.
    const DOTS_MORE = /(?:\\.{3}|…|\\u2026)\\s*more\\s*$/i;
    const DOTS_MAS = /(?:\\.{3}|…|\\u2026)\\s*m[aá]s\\s*$/i;
    const ONLY = /^(?:\\.{3}|…|\\u2026)?\\s*(see more|show more|ver m[aá]s|mostrar m[aá]s|more|m[aá]s)\\s*$/i;
    const SKIP = /see less|show less|ver menos|mostrar menos/i;
    function norm(el) {
      return (el.innerText || el.textContent || '')
        .replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '')
        .replace(/\\s+/g, ' ')
        .trim();
    }
    const candidates = [];
    for (const el of root.querySelectorAll(
      'button, span[role="button"], a[role="button"], span, a, em, strong'
    )) {
      const raw = norm(el);
      if (!raw || SKIP.test(raw)) continue;
      if (raw.length <= 16 && (ONLY.test(raw) || DOTS_MORE.test(raw) || DOTS_MAS.test(raw))) {
        candidates.push(el);
        continue;
      }
      if (raw.length <= 8 && /^more$/i.test(raw)) {
        const prev = (el.previousSibling && (el.previousSibling.textContent || '')) || '';
        if (/(?:\\.{3}|…|\\u2026)\\s*$/.test(prev)) candidates.push(el);
      }
    }
    candidates.sort((a, b) => norm(a).length - norm(b).length);
    let n = 0;
    for (const el of candidates) {
      if (n >= 4) break;
      try { el.click(); n++; } catch (_) {}
    }
    return n;
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
  let noPermalink = 0;
  let tooShort = 0;
  let expanded = 0;
  for (const root of roots.slice(0, 50)) {
    expanded += expandInRoot(root);

    const textEl = root.querySelector(
      '[data-testid="expandable-text-box"], .update-components-text, ' +
      ".feed-shared-update-v2__description, .feed-shared-text, .break-words"
    );
    let text = ((textEl && textEl.innerText) || root.innerText || "").trim();
    // Limpiar restos del control «…more» si quedó pegado al final.
    text = text.replace(/\\s*[….]*\\s*(see more|show more|ver más|ver mas|more|más)\\s*$/i, "").trim();
    if (text.length < 30) { tooShort++; continue; }

    const permalink = findPermalink(root);
    if (!permalink) { noPermalink++; continue; }
    if (seen.has(permalink)) continue;
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

    // Fecha relativa tipo "7h" / "2d" / "3w" junto al actor (no el título del perfil).
    function findPublished(el) {
      const time = el.querySelector("time");
      if (time) {
        const dt = time.getAttribute("datetime");
        if (dt) return dt;
        const t = (time.innerText || "").replace(/\s+/g, " ").trim();
        if (t) return t;
      }
      // Stamps cortos típicos del feed: 7h, 2d, 15m, 3w, 1mo
      const STAMP = /^(?:\d+\s*(?:m|min|mins|h|hr|hrs|d|w|wk|mo|y)|just now|ahora|ayer|yesterday)$/i;
      const STAMP_PREFIX = /^(\d+\s*(?:m|min|mins|h|hr|hrs|d|w|wk|mo|y))\b/i;
      const REL_LONG = /(\d+)\s*(minute|minutes|min|hour|hours|hr|hrs|day|days|week|weeks|month|months|minuto|minutos|hora|horas|d[ií]a|d[ií]as|semana|semanas|mes|meses)\s*(ago|atr[aá]s)?/i;
      const nodes = el.querySelectorAll(
        "span, a, time, div.update-components-actor__sub-description, " +
        "span.feed-shared-actor__sub-description, " +
        'a[href*="activity"] span, a[href*="/posts/"] span, ' +
        'a[href*="activity"], a[href*="/posts/"]'
      );
      for (const node of nodes) {
        const aria = (node.getAttribute && (node.getAttribute("aria-label") || node.getAttribute("title"))) || "";
        if (aria) {
          const am = aria.match(REL_LONG) || aria.match(STAMP_PREFIX);
          if (am) return am[0].replace(/\s+/g, " ").trim();
        }
        let t = (node.innerText || node.textContent || "")
          .replace(/[•·|]/g, " ")
          .replace(/\s+/g, " ")
          .trim();
        if (!t || t.length > 40) continue;
        // Evitar el headline del recruiter ("Talent Acquisition | …").
        if (/talent|recruiter|developer|engineer|hiring manager/i.test(t) && !STAMP.test(t)) {
          continue;
        }
        if (STAMP.test(t)) return t;
        const m = t.match(STAMP_PREFIX);
        if (m) return m[1].replace(/\s+/g, "");
        const rl = t.match(REL_LONG);
        if (rl && t.length <= 28) return rl[0].replace(/\s+/g, " ").trim();
      }
      return "";
    }
    const published = findPublished(root);

    out.push({
      text: text.slice(0, 4000),
      company,
      location,
      permalink,
      published: String(published).replace(/\s+/g, " ").trim().slice(0, 80),
    });
  }
  return { posts: out, roots: roots.length, noPermalink, tooShort, expanded };
}
"""
