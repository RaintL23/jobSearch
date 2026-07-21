const API_BASE = (location.hostname === "127.0.0.1" || location.hostname === "localhost")
  ? location.origin : "http://127.0.0.1:8000";
document.getElementById("apiHint").textContent = API_BASE;

const PROFILE_TEMPLATE = {
  name: "Candidato",
  roles: ["Backend Developer", ".NET Engineer"],
  skills: ["C#", ".NET", "SQL", "APIs"],
  experience_years: 3,
  summary: "Desarrollador backend con experiencia en APIs y bases de datos.",
  location: "",
  country: "ar",
};

const step1 = document.getElementById("step1");
const step1Summary = document.getElementById("step1Summary");
const step1Num = document.getElementById("step1Num");
const profileJson = document.getElementById("profileJson");
const profileStatus = document.getElementById("profileStatus");
const btnSearch = document.getElementById("btnSearch");
const btnCancelSearch = document.getElementById("btnCancelSearch");
const btnProcess = document.getElementById("btnProcess");
const resultsBody = document.getElementById("resultsBody");
const resultsWrap = document.getElementById("resultsWrap");
const coverModal = document.getElementById("coverModal");
const coverBody = document.getElementById("coverBody");
const coverTitle = document.getElementById("coverTitle");
const sidebar = document.getElementById("sidebar");
const emptyNote = document.getElementById("emptyNote");
const statusStrip = document.getElementById("statusStrip");
const logPanel = document.getElementById("logPanel");

const DEFAULT_DOC_TITLE = "AI Job Scraper & Matcher";
let titleFlashTimer = null;

let currentJobs = [];
let displayJobs = [];
let profileReady = false;
let lastProfile = null;
let lastSources = {};
let tableFilter = "all";
let tableQuery = "";
let tableSort = "match";
let tableStatusFilter = "all";
let activeSearchController = null;
let activeSearchRunId = null;
let activeSearchCancelled = false;

const PROFILE_STORAGE_KEY = "jobsearch_profile_v1";
const ROW_STATES_KEY = "jobsearch_row_states_v1";

function loadRowStates() {
  try {
    const raw = localStorage.getItem(ROW_STATES_KEY);
    const data = raw ? JSON.parse(raw) : {};
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

let rowStates = loadRowStates();

function persistRowStates() {
  try { localStorage.setItem(ROW_STATES_KEY, JSON.stringify(rowStates)); } catch (e) {}
}

function getRowState(id) {
  const s = rowStates[id];
  if (!s || typeof s !== "object") return { visited: false, status: null };
  return {
    visited: !!s.visited,
    status: s.status === "interested" || s.status === "not_interested" ? s.status : null,
  };
}

function setRowState(id, patch) {
  const prev = getRowState(id);
  const next = { ...prev, ...patch };
  if (!next.visited && !next.status) delete rowStates[id];
  else rowStates[id] = next;
  persistRowStates();
  return next;
}

function rowClassNames(id, tier) {
  const s = getRowState(id);
  const classes = [`tier-${tier}`];
  if (s.visited) classes.push("visited");
  if (s.status === "interested") classes.push("interested");
  if (s.status === "not_interested") classes.push("not-interested");
  return classes.join(" ");
}

function updateRowAfterStateChange(tr, id) {
  if (tableStatusFilter !== "all") {
    applyTableView();
    return;
  }
  applyRowStateToTr(tr, id);
}

function applyRowStateToTr(tr, id) {
  if (!tr) return;
  const tier = tr.className.match(/tier-(high|mid|low)/)?.[1] || "low";
  tr.className = rowClassNames(id, tier);
  const viewBtn = tr.querySelector(".act-btn.view");
  if (viewBtn) viewBtn.classList.toggle("visited", getRowState(id).visited);
  const interestBtn = tr.querySelector("[data-interest]");
  if (interestBtn) interestBtn.classList.toggle("active", getRowState(id).status === "interested");
  const notBtn = tr.querySelector("[data-not-interest]");
  if (notBtn) notBtn.classList.toggle("active", getRowState(id).status === "not_interested");
}

function saveProfileToStorage(obj) {
  try { localStorage.setItem(PROFILE_STORAGE_KEY, JSON.stringify(obj)); } catch (e) {}
}

function show(el, msg) {
  el.textContent = msg || "";
  el.classList.toggle("hidden", !msg);
}

function stopTitleFlash() {
  if (titleFlashTimer) {
    clearInterval(titleFlashTimer);
    titleFlashTimer = null;
  }
  document.title = DEFAULT_DOC_TITLE;
}

/** Titilea el título de la ventana (efecto aviso en barra de tareas / pestaña). */
function startTitleFlash(message) {
  stopTitleFlash();
  let on = true;
  document.title = message;
  titleFlashTimer = setInterval(() => {
    document.title = on ? `● ${message}` : DEFAULT_DOC_TITLE;
    on = !on;
  }, 900);
}

/**
 * Aviso al SO cuando termina la búsqueda:
 * - Notificación nativa de Windows (toast; suele hacer brillar el ícono)
 * - Titileo del título si la ventana está en segundo plano
 */
async function ensureNotificationPermission() {
  if (!("Notification" in window)) return "unsupported";
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}

function notifySearchFinished({ count = 0, ok = true, error = "" } = {}) {
  const title = ok ? "Búsqueda terminada" : "Búsqueda con error";
  const body = ok
    ? `${count} oferta(s) listas para revisar.`
    : String(error || "Revisá el panel de progreso.").slice(0, 180);

  if (document.hidden || !document.hasFocus()) {
    startTitleFlash(ok ? `Listo · ${count} ofertas` : "Error en la búsqueda");
  }

  if (!("Notification" in window) || Notification.permission !== "granted") return;

  try {
    const n = new Notification(title, {
      body,
      tag: "jobsearch-finished",
      renotify: true,
      requireInteraction: true,
      silent: false,
    });
    n.onclick = () => {
      try {
        window.focus();
      } catch {
        /* ignore */
      }
      stopTitleFlash();
      n.close();
    };
    if (!document.hidden && document.hasFocus()) {
      setTimeout(() => {
        try {
          n.close();
        } catch {
          /* ignore */
        }
      }, 8000);
    }
  } catch {
    /* algunos perfiles de Edge/Chrome bloquean Notification */
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) stopTitleFlash();
});
window.addEventListener("focus", () => stopTitleFlash());

function setStepOpen(stepEl, open) {
  stepEl.classList.toggle("open", open);
}

function setStep1Collapsed(c) {
  setStepOpen(step1, !c);
}

document.querySelectorAll(".step-head[data-toggle]").forEach((head) => {
  head.addEventListener("click", () => {
    if (sidebar.classList.contains("collapsed")) {
      sidebar.classList.remove("collapsed");
      document.getElementById("sidebarCollapseBtn").textContent = "⟨";
    }
    const step = document.getElementById(head.dataset.toggle);
    step.classList.toggle("open");
  });
});

function syncFiltersToggle() {
  const toggle = document.getElementById("btnToggleFilters");
  const collapsed = sidebar.classList.contains("collapsed");
  toggle.classList.toggle("hidden", !collapsed);
  toggle.textContent = "Filtros";
}

document.getElementById("sidebarCollapseBtn").addEventListener("click", () => {
  sidebar.classList.toggle("collapsed");
  const collapsed = sidebar.classList.contains("collapsed");
  document.getElementById("sidebarCollapseBtn").textContent = collapsed ? "⟩" : "⟨";
  syncFiltersToggle();
});

statusStrip.addEventListener("click", () => {
  statusStrip.classList.toggle("expanded");
  logPanel.classList.toggle("show");
});

function splitMulti(text) {
  return String(text || "")
    .split(/[\n,;|]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function createMultiSelect(mountEl, { label, options, placeholder }) {
  const wrap = document.createElement("div");
  wrap.className = "ms-wrap field";
  wrap.innerHTML = `
    <span class="ms-label">${label}</span>
    <button type="button" class="ms-btn" aria-expanded="false">
      <span class="ms-summary empty">${placeholder}</span>
      <span class="ms-chevron">▾</span>
    </button>
    <div class="ms-panel" role="listbox">
      <div class="ms-actions">
        <button type="button" data-act="all">Todos</button>
        <button type="button" data-act="clear">Limpiar</button>
      </div>
      ${options.map(([val, text]) => `
        <label class="ms-opt">
          <input type="checkbox" value="${val}" />
          <span>${text}</span>
        </label>`).join("")}
    </div>
    <p class="hint">Sin selección = cualquiera</p>
  `;
  mountEl.replaceWith(wrap);

  const btn = wrap.querySelector(".ms-btn");
  const summary = wrap.querySelector(".ms-summary");
  const panel = wrap.querySelector(".ms-panel");

  function selected() {
    return [...wrap.querySelectorAll('input[type="checkbox"]:checked')].map((c) => c.value);
  }

  function refresh() {
    const vals = selected();
    const labels = vals.map((v) => (options.find((o) => o[0] === v) || [v, v])[1]);
    if (!labels.length) {
      summary.textContent = placeholder;
      summary.classList.add("empty");
    } else {
      summary.textContent = labels.join(", ");
      summary.classList.remove("empty");
    }
    updateFooterNote();
    updateStep2Summary();
  }

  function setOpen(open) {
    wrap.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) {
      wrap.classList.remove("drop-up");
      wrap.style.removeProperty("--ms-max-height");
    }
  }

  function positionPanel() {
    if (!wrap.classList.contains("open")) return;

    const btnRect = btn.getBoundingClientRect();
    const scrollRect = sidebar.querySelector(".sidebar-scroll").getBoundingClientRect();
    const viewportBottom = Math.min(window.innerHeight, scrollRect.bottom);
    const viewportTop = Math.max(0, scrollRect.top);
    const availableBelow = viewportBottom - btnRect.bottom - 8;
    const availableAbove = btnRect.top - viewportTop - 8;
    const openUp = availableBelow < 224 && availableAbove > availableBelow;
    const available = openUp ? availableAbove : availableBelow;

    wrap.classList.toggle("drop-up", openUp);
    wrap.style.setProperty("--ms-max-height", `${Math.max(64, Math.min(224, available))}px`);
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = !wrap.classList.contains("open");
    document.querySelectorAll(".ms-wrap.open").forEach((el) => {
      if (el !== wrap) {
        el.classList.remove("open", "drop-up");
        el.style.removeProperty("--ms-max-height");
        el.querySelector(".ms-btn")?.setAttribute("aria-expanded", "false");
      }
    });
    setOpen(willOpen);
    if (willOpen) positionPanel();
  });

  window.addEventListener("resize", positionPanel);
  sidebar.querySelector(".sidebar-scroll").addEventListener("scroll", positionPanel, { passive: true });

  panel.addEventListener("click", (e) => e.stopPropagation());

  wrap.querySelector('[data-act="all"]').addEventListener("click", () => {
    wrap.querySelectorAll('input[type="checkbox"]').forEach((c) => { c.checked = true; });
    refresh();
  });
  wrap.querySelector('[data-act="clear"]').addEventListener("click", () => {
    wrap.querySelectorAll('input[type="checkbox"]').forEach((c) => { c.checked = false; });
    refresh();
  });
  wrap.querySelectorAll('input[type="checkbox"]').forEach((c) => {
    c.addEventListener("change", refresh);
  });

  return {
    get values() { return selected(); },
    setValues(vals) {
      const set = new Set(vals || []);
      wrap.querySelectorAll('input[type="checkbox"]').forEach((c) => {
        c.checked = set.has(c.value);
      });
      refresh();
    },
  };
}

const multiFilters = {
  posted: createMultiSelect(document.querySelector('[data-ms="posted"]'), {
    label: "Antigüedad",
    placeholder: "Cualquiera",
    options: [
      ["24h", "Últimas 24 h"],
      ["week", "Última semana"],
      ["month", "Último mes"],
    ],
  }),
  sources: createMultiSelect(document.querySelector('[data-ms="sources"]'), {
    label: "Fuentes",
    placeholder: "Todas",
    options: [
      ["linkedin", "LinkedIn"],
      ["getonboard", "GetOnBoard"],
      ["computrabajo", "Computrabajo"],
      ["linkedin_hiring", "LinkedIn #Hiring"],
      ["remotive", "Remotive"],
      ["jobicy", "Jobicy"],
      ["remoteok", "RemoteOK"],
    ],
  }),
  experience: createMultiSelect(document.querySelector('[data-ms="experience"]'), {
    label: "Experiencia",
    placeholder: "Cualquiera",
    options: [
      ["internship", "Prácticas"],
      ["entry", "Junior"],
      ["associate", "Semi"],
      ["mid", "Mid"],
      ["senior", "Senior"],
      ["director", "Director"],
    ],
  }),
  workMode: createMultiSelect(document.querySelector('[data-ms="workMode"]'), {
    label: "Modalidad",
    placeholder: "Cualquiera",
    options: [
      ["remote", "Remoto"],
      ["hybrid", "Híbrido"],
      ["onsite", "Presencial"],
    ],
  }),
  postingLang: createMultiSelect(document.querySelector('[data-ms="postingLang"]'), {
    label: "Idioma de la propuesta",
    placeholder: "Cualquiera",
    options: [["es", "Español"], ["en", "Inglés"], ["pt", "Portugués"]],
  }),
  requiredLang: createMultiSelect(document.querySelector('[data-ms="requiredLang"]'), {
    label: "Idioma requerido",
    placeholder: "Cualquiera",
    options: [["es", "Español"], ["en", "Inglés"], ["pt", "Portugués"]],
  }),
};

document.addEventListener("click", () => {
  document.querySelectorAll(".ms-wrap.open").forEach((el) => {
    el.classList.remove("open", "drop-up");
    el.style.removeProperty("--ms-max-height");
    el.querySelector(".ms-btn")?.setAttribute("aria-expanded", "false");
  });
});

function updateFooterNote() {
  const srcs = multiFilters.sources.values;
  const n = srcs.length || 7;
  document.getElementById("footerNote").textContent = profileReady
    ? `${n} fuente${n === 1 ? "" : "s"} · listo para buscar`
    : `${n} fuente${n === 1 ? "" : "s"} · sin cálculo de match`;
}

function updateStep2Summary() {
  const modes = multiFilters.workMode.values;
  const locs = splitMulti(document.getElementById("filterLocations").value);
  const modeLabels = { remote: "Remoto", hybrid: "Híbrido", onsite: "Presencial" };
  const parts = [];
  if (modes.length) parts.push(modes.map((m) => modeLabels[m] || m).join("/"));
  if (locs.length) parts.push(locs.slice(0, 2).join(", "));
  document.getElementById("step2Summary").textContent = parts.length ? parts.join(" · ") : "Configura fuentes";
}

function setLoading(btn, labelEl, spinnerEl, loading, idle, busy) {
  if (btn === btnSearch) btn.disabled = loading;
  else btn.disabled = loading;
  labelEl.textContent = loading ? busy : idle;
  spinnerEl.classList.toggle("hidden", !loading);
}

function getProfile() {
  const raw = profileJson.value.trim();
  if (!raw) throw new Error("No hay perfil JSON.");
  const data = JSON.parse(raw);
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("El JSON debe ser un objeto.");
  }
  return data;
}

function profileSummary(obj) {
  const name = obj.name || "Sin nombre";
  return `${name}`;
}

function profileInitials(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "—";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function updateProfileChip(obj) {
  const name = (obj && obj.name) || "Sin perfil";
  document.getElementById("profileChipName").textContent = name;
  document.getElementById("profileAvatar").textContent = profileInitials(name);
}

function setProfileReady(ready, statusMsg, { collapse = false } = {}) {
  profileReady = ready;
  profileStatus.textContent = statusMsg || (ready ? "Perfil válido · cargado" : "Sin perfil válido todavía.");
  profileStatus.className = ready ? "json-status" : "json-status idle";
  step1.classList.toggle("complete", ready);
  step1Num.textContent = ready ? "✓" : "1";
  if (ready && collapse) {
    try { step1Summary.textContent = profileSummary(getProfile()); }
    catch { step1Summary.textContent = "Perfil cargado"; }
    setStep1Collapsed(true);
  }
  if (!ready) {
    step1Summary.textContent = "";
    setStep1Collapsed(false);
    updateProfileChip(null);
  }
  updateFooterNote();
}

function applyProfile(obj, okMsg) {
  lastProfile = obj;
  saveProfileToStorage(obj);
  profileJson.value = JSON.stringify(obj, null, 2);
  updateProfileChip(obj);
  const q = document.getElementById("filterQueries");
  if (!q.value.trim() && Array.isArray(obj.roles) && obj.roles.length) {
    q.value = obj.roles.join("\n");
  }
  const loc = document.getElementById("filterLocations");
  if (!loc.value.trim() && obj.location) loc.value = obj.location;
  setProfileReady(true, okMsg || "Perfil válido · cargado", { collapse: true });
  updateStep2Summary();
}

document.getElementById("filterLocations").addEventListener("input", updateStep2Summary);

function getFilters() {
  const minV = document.getElementById("filterSalaryMin").value;
  const maxV = document.getElementById("filterSalaryMax").value;
  return {
    queries: splitMulti(document.getElementById("filterQueries").value),
    locations: splitMulti(document.getElementById("filterLocations").value),
    posted_within: multiFilters.posted.values,
    sources: multiFilters.sources.values,
    experience_levels: multiFilters.experience.values,
    work_modes: multiFilters.workMode.values,
    countries: [],
    salary_min_usd: minV === "" ? null : Number(minV),
    salary_max_usd: maxV === "" ? null : Number(maxV),
    posting_languages: multiFilters.postingLang.values,
    required_languages: multiFilters.requiredLang.values,
  };
}

function tierOf(n) {
  if (n >= 60) return "high";
  if (n >= 35) return "mid";
  return "low";
}

function matchBars(m) {
  const filled = Math.round(m / 20);
  let out = "";
  for (let i = 0; i < 5; i++) out += `<span class="${i < filled ? "fill" : ""}"></span>`;
  return out;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function truncate(str, n) {
  const s = String(str ?? "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function sourceLabel(src) {
  const s = String(src || "").toLowerCase();
  if (s === "computrabajo") return { text: "Computrabajo", cls: "src-computrabajo", key: "computrabajo" };
  if (s === "linkedin") return { text: "LinkedIn", cls: "src-linkedin", key: "linkedin" };
  if (s === "linkedin_hiring") return { text: "LI #Hiring", cls: "src-linkedin-hiring", key: "linkedin_hiring" };
  if (s === "getonboard") return { text: "GetOnBoard", cls: "src-getonboard", key: "getonboard" };
  if (s === "remotive") return { text: "Remotive", cls: "src-remotive", key: "remotive" };
  if (s === "remoteok") return { text: "RemoteOK", cls: "src-remoteok", key: "remoteok" };
  if (s === "jobicy") return { text: "Jobicy", cls: "src-jobicy", key: "jobicy" };
  return { text: src || "—", cls: "src-other", key: s || "other" };
}

function formatPublishedParts(iso) {
  if (!iso) return { abs: "—", rel: "" };
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { abs: "—", rel: "" };
  const abs = d.toLocaleString("es", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  return { abs, rel: relativeFromNow(d) };
}

function relativeFromNow(date) {
  const sec = Math.round((date.getTime() - Date.now()) / 1000);
  const rtf = new Intl.RelativeTimeFormat("es", { numeric: "auto" });
  const divisions = [
    { amount: 60, unit: "second" },
    { amount: 60, unit: "minute" },
    { amount: 24, unit: "hour" },
    { amount: 7, unit: "day" },
    { amount: 4.34524, unit: "week" },
    { amount: 12, unit: "month" },
    { amount: Number.POSITIVE_INFINITY, unit: "year" },
  ];
  let duration = sec;
  for (const div of divisions) {
    if (Math.abs(duration) < div.amount) {
      return rtf.format(Math.round(duration), div.unit);
    }
    duration /= div.amount;
  }
  return rtf.format(Math.round(duration), "year");
}

function salarySortValue(job) {
  const raw = job.salary_usd;
  if (raw == null || raw === "" || raw === "—") return 0;
  if (typeof raw === "number") return raw;
  const m = String(raw).replace(/[^\d.]/g, "");
  const n = Number(m);
  return Number.isFinite(n) ? n : 0;
}

function jobId(job, idx) {
  return job.url || `${job.title}|${job.company}|${idx}`;
}

function isDirectLinkedInPostUrl(url) {
  try {
    const parsed = new URL(String(url || ""));
    if (!["linkedin.com", "www.linkedin.com"].includes(parsed.hostname.toLowerCase())) return false;
    const path = parsed.pathname;
    const feedPost = /^\/feed\/update\/urn:li:(activity|ugcPost):\d{6,}\/?$/i.test(path);
    const sharedPost = /^\/posts\/.+-\d{15,}-[A-Za-z0-9_]+\/?$/.test(path);
    return feedPost || sharedPost;
  } catch (_) {
    return false;
  }
}

function usableJobUrl(job) {
  if (!job?.url) return "";
  if (job.source === "linkedin_hiring" && !isDirectLinkedInPostUrl(job.url)) return "";
  return job.url;
}

function setStatus({ summary, detail, tone = "idle", expandLog = false }) {
  document.getElementById("statusSummary").innerHTML = summary;
  if (detail != null) document.getElementById("statusDetail").innerHTML = detail;
  const dot = document.getElementById("statusDot");
  dot.className = "status-dot" + (tone === "ok" ? " ok" : tone === "warn" ? " warn" : tone === "busy" ? " busy" : "");
  if (expandLog) {
    statusStrip.classList.add("expanded");
    logPanel.classList.add("show");
  }
}

function clearResultsTable() {
  currentJobs = [];
  displayJobs = [];
  lastSources = {};
  resultsBody.textContent = "";
  emptyNote.textContent = "Buscando ofertas…";
  emptyNote.classList.remove("hidden");
  document.getElementById("sourceStatus").innerHTML = "";
  document.getElementById("sourceTally").innerHTML = "";
  clearSearchProgress();
  show(document.getElementById("step2Empty"), "");
  show(document.getElementById("step2Ok"), "");
  document.getElementById("visibleCount").textContent = "0";
  document.getElementById("totalCount").textContent = "0";
  resetSourceFilterChips();
  updatePulse(null);
}

function clearSearchProgress() {
  const box = document.getElementById("searchProgress");
  box.innerHTML = "";
}

function appendSearchProgress(message, { tone } = {}) {
  if (!message) return;
  const box = document.getElementById("searchProgress");
  if (!box.querySelector(".prog-title")) {
    box.innerHTML = `<div class="prog-title">Progreso de la búsqueda</div>`;
  }
  const line = document.createElement("div");
  line.className = "prog-line" + (tone === "ok" ? " ok" : tone === "warn" ? " warn" : "");
  const time = new Date().toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  line.textContent = `[${time}] ${message}`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
  statusStrip.classList.add("expanded");
  logPanel.classList.add("show");
}

function enterResultsFocus() {
  sidebar.classList.add("collapsed");
  document.getElementById("sidebarCollapseBtn").textContent = "⟩";
  setStep1Collapsed(true);
  syncFiltersToggle();
}

const SOURCE_STATUS_ORDER = [
  ["linkedin", "LinkedIn Jobs"],
  ["getonboard", "GetOnBoard"],
  ["computrabajo", "Computrabajo"],
  ["linkedin_hiring", "LinkedIn #Hiring"],
  ["remotive", "Remotive"],
  ["jobicy", "Jobicy"],
  ["remoteok", "RemoteOK"],
];

function renderSourceStatus(sources) {
  lastSources = sources || {};
  const box = document.getElementById("sourceStatus");
  const tally = document.getElementById("sourceTally");
  const selected = multiFilters.sources.values;
  const order = selected.length
    ? SOURCE_STATUS_ORDER.filter(([key]) => selected.includes(key))
    : SOURCE_STATUS_ORDER;

  const html = order.map(([key, name]) => {
    const info = (sources && sources[key]) || {};
    const ok = !!info.ok;
    const raw = typeof info.raw_count === "number" ? info.raw_count : null;
    const count = typeof info.count === "number" ? info.count : null;
    const countLabel =
      raw != null && count != null && raw !== count
        ? ` (${count}/${raw})`
        : count != null
          ? ` (${count})`
          : "";
    let extra = "";
    const sample = Array.isArray(info.discarded_sample) ? info.discarded_sample : [];
    if (sample.length) {
      extra =
        `<div class="log-discard">Ej. descartes: ` +
        sample
          .slice(0, 5)
          .map(
            (d) =>
              `${escapeHtml(d.title || "?")} → ${escapeHtml(d.reason_label || d.reason || "?")}`,
          )
          .join(" · ") +
        `</div>`;
    }
    return `<div class="log-line ${ok ? "ok" : "warn"}"><strong>${ok ? "✓" : "!"} ${name}${countLabel}:</strong> ${escapeHtml(info.message || "Sin información.")}${extra}</div>`;
  }).join("");
  box.innerHTML = `<div class="src-title">Estado del scraping</div>${html}`;

  tally.innerHTML = order.map(([key, name]) => {
    const info = (sources && sources[key]) || {};
    const ok = !!info.ok;
    const count = typeof info.count === "number" ? info.count : 0;
    const raw = typeof info.raw_count === "number" ? info.raw_count : null;
    const label = raw != null && raw !== count ? `${count}/${raw}` : String(count);
    return `<span class="tally-pill"><span class="dot" style="background:${ok ? "var(--green)" : "var(--amber)"}"></span>${escapeHtml(name)} · ${label}</span>`;
  }).join("");

  updatePulse(sources);
}

function updatePulse(sources) {
  const dots = document.getElementById("pulseDots");
  const label = document.getElementById("pulseLabel");
  if (!sources) {
    dots.innerHTML = SOURCE_STATUS_ORDER.map(() => `<span class="off"></span>`).join("");
    label.textContent = "Sin corrida aún";
    return;
  }
  let okCount = 0;
  const selected = multiFilters.sources.values;
  const order = selected.length
    ? SOURCE_STATUS_ORDER.filter(([key]) => selected.includes(key))
    : SOURCE_STATUS_ORDER;
  dots.innerHTML = order.map(([key, name]) => {
    const info = sources[key] || {};
    const ok = !!info.ok && (info.count == null || info.count > 0);
    if (ok) okCount += 1;
    const cls = info.ok === false || (info.ok && info.count === 0) ? "warn" : (info.ok ? "on" : "off");
    return `<span class="${cls}" title="${escapeHtml(name)}"></span>`;
  }).join("");
  label.textContent = `${okCount}/${order.length} fuentes con datos`;
}

function resetSourceFilterChips() {
  const wrap = document.getElementById("sourceFilterChips");
  wrap.innerHTML = `<button type="button" class="fchip active" data-filter="all">Todas</button>`;
  tableFilter = "all";
  bindFilterChips();
}

function rebuildSourceFilterChips(jobs) {
  const wrap = document.getElementById("sourceFilterChips");
  const keys = [...new Set(jobs.map((j) => sourceLabel(j.source).key))];
  const labels = {
    linkedin: "LinkedIn",
    linkedin_hiring: "LI #Hiring",
    getonboard: "GetOnBoard",
    computrabajo: "Computrabajo",
    remotive: "Remotive",
    jobicy: "Jobicy",
    remoteok: "RemoteOK",
  };
  wrap.innerHTML = [
    `<button type="button" class="fchip ${tableFilter === "all" ? "active" : ""}" data-filter="all">Todas</button>`,
    ...keys.map((k) =>
      `<button type="button" class="fchip ${tableFilter === k ? "active" : ""}" data-filter="${escapeHtml(k)}">${escapeHtml(labels[k] || k)}</button>`
    ),
  ].join("");
  bindFilterChips();
}

function bindFilterChips() {
  document.querySelectorAll("#sourceFilterChips .fchip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("#sourceFilterChips .fchip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      tableFilter = chip.dataset.filter;
      applyTableView();
    });
  });
}

function bindStatusFilterChips() {
  document.querySelectorAll("#statusFilterChips .fchip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("#statusFilterChips .fchip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      tableStatusFilter = chip.dataset.status;
      applyTableView();
    });
  });
}

function applyTableView() {
  let list = currentJobs.map((job, idx) => ({ job, idx }));
  if (tableFilter !== "all") {
    list = list.filter(({ job }) => sourceLabel(job.source).key === tableFilter);
  }
  if (tableStatusFilter !== "all") {
    list = list.filter(({ job, idx }) => {
      const s = getRowState(jobId(job, idx));
      if (tableStatusFilter === "pending") return !s.visited && !s.status;
      if (tableStatusFilter === "interested") return s.status === "interested";
      if (tableStatusFilter === "not_interested") return s.status === "not_interested";
      return true;
    });
  }
  if (tableQuery) {
    list = list.filter(({ job }) => {
      const hay = `${job.title || ""} ${job.company || ""}`.toLowerCase();
      return hay.includes(tableQuery);
    });
  }
  list = [...list].sort((a, b) => {
    if (tableSort === "match") {
      return (Number(b.job.match_percent) || 0) - (Number(a.job.match_percent) || 0);
    }
    if (tableSort === "date") {
      const ta = a.job.published_at ? new Date(a.job.published_at).getTime() : 0;
      const tb = b.job.published_at ? new Date(b.job.published_at).getTime() : 0;
      return tb - ta;
    }
    if (tableSort === "salary") {
      return salarySortValue(b.job) - salarySortValue(a.job);
    }
    return 0;
  });
  displayJobs = list;
  renderTableRows(list);
}

function renderTableRows(list) {
  resultsBody.textContent = "";
  document.getElementById("visibleCount").textContent = String(list.length);
  document.getElementById("totalCount").textContent = String(currentJobs.length);

  if (!list.length) {
    emptyNote.textContent = currentJobs.length
      ? "Sin resultados para este filtro. Probá con otro término o fuente."
      : "Sin resultados todavía. Configurá el perfil e iniciá una búsqueda.";
    emptyNote.classList.remove("hidden");
    return;
  }
  emptyNote.classList.add("hidden");

  const frag = document.createDocumentFragment();
  list.forEach(({ job, idx }) => {
    const hasMatch = job.match_percent !== null && job.match_percent !== undefined;
    const pct = hasMatch ? Number(job.match_percent) || 0 : 0;
    const tier = tierOf(pct);
    const src = sourceLabel(job.source);
    const company = job.company || "Empresa no indicada";
    const directUrl = usableJobUrl(job);
    const pub = formatPublishedParts(job.published_at);
    const id = jobId(job, idx);
    const rowState = getRowState(id);
    const tr = document.createElement("tr");
    tr.className = rowClassNames(id, tier);
    tr.dataset.jobIdx = String(idx);
    tr.dataset.jobId = id;
    tr.innerHTML = `
      <td>
        <div class="job-title">${escapeHtml(job.title)}</div>
        <div class="job-company">${escapeHtml(company)}</div>
      </td>
      <td><span class="badge-source ${src.cls}">${escapeHtml(src.text)}</span></td>
      <td class="loc-cell">${escapeHtml(job.location || "—")}</td>
      <td class="time-cell">${escapeHtml(pub.rel || pub.abs)}${pub.rel && pub.abs !== "—" ? `<span class="rel">${escapeHtml(pub.abs)}</span>` : ""}</td>
      <td class="salary-cell">${escapeHtml(job.salary_usd || "—")}</td>
      <td class="req-cell">${escapeHtml(truncate(job.requirements, 140))}</td>
      <td>
        <div class="match">
          ${hasMatch
            ? `<div class="match-bars">${matchBars(pct)}</div><span class="match-pct">${pct}%</span>`
            : `<span class="match-pct unavailable" title="Cargá un perfil CV para calcular el match">—</span>`}
        </div>
      </td>
      <td>
        <div class="actions-cell">
          ${directUrl
            ? `<a class="act-btn view${rowState.visited ? " visited" : ""}" href="${escapeHtml(directUrl)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer" data-visit="${escapeHtml(id)}" title="${rowState.visited ? "Ya visitada" : "Ver oferta"}">↗</a>`
            : `<button type="button" class="act-btn view" disabled title="Sin enlace directo al post">↗</button>`}
          <button type="button" class="act-btn cover" data-cover-gen="${idx}" title="Cover letter">CL</button>
          ${job.contact_email
            ? `<button type="button" class="act-btn email" data-email-gen="${idx}" title="Borrador de email · ${escapeHtml(job.contact_email)}">✉</button>`
            : ""}
          <button type="button" class="act-btn interest${rowState.status === "interested" ? " active" : ""}" data-interest="${escapeHtml(id)}" title="Me interesa">★</button>
          <button type="button" class="act-btn not-interest${rowState.status === "not_interested" ? " active" : ""}" data-not-interest="${escapeHtml(id)}" title="No me interesa">✕</button>
        </div>
      </td>`;
    frag.appendChild(tr);
  });
  resultsBody.appendChild(frag);
}

function renderJobs(jobs, sources) {
  currentJobs = jobs || [];
  show(document.getElementById("step2Empty"), "");
  renderSourceStatus(sources || {});

  const warnCount = SOURCE_STATUS_ORDER.filter(([key]) => {
    const info = (sources || {})[key];
    return info && (!info.ok || info.count === 0);
  }).length;

  if (!currentJobs.length) {
    emptyNote.textContent = "No se encontraron ofertas con esos filtros.";
    emptyNote.classList.remove("hidden");
    resultsBody.textContent = "";
    document.getElementById("visibleCount").textContent = "0";
    document.getElementById("totalCount").textContent = "0";
    resetSourceFilterChips();
    show(document.getElementById("step2Empty"), "No se encontraron ofertas con esos filtros.");
    setStatus({
      summary: `<strong>0 ofertas</strong> · sin resultados tras filtros`,
      detail: warnCount ? `${warnCount} fuente(s) sin datos` : "Revisá filtros o fuentes",
      tone: "warn",
      expandLog: true,
    });
    sidebar.classList.remove("collapsed");
    document.getElementById("sidebarCollapseBtn").textContent = "⟨";
    syncFiltersToggle();
    return;
  }

  rebuildSourceFilterChips(currentJobs);
  applyTableView();
  enterResultsFocus();
  show(document.getElementById("step2Ok"), `${currentJobs.length} oferta(s) encontradas.`);
  setStatus({
    summary: `<strong>${currentJobs.length} ofertas</strong> obtenidas · última corrida ahora`,
    detail: warnCount ? `${warnCount} fuente(s) sin resultados` : "Todas las fuentes respondieron",
    tone: warnCount ? "warn" : "ok",
  });
}

async function readSearchStream(res, onEvent) {
  if (!res.body) {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
      throw new Error(detail || `HTTP ${res.status}`);
    }
    onEvent({ event: "done", ...data });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      const lines = chunk.split("\n").filter((l) => l.startsWith("data:"));
      for (const line of lines) {
        const raw = line.replace(/^data:\s*/, "").trim();
        if (!raw) continue;
        try {
          onEvent(JSON.parse(raw));
        } catch (_) { /* ignore partial */ }
      }
    }
  }
}

document.getElementById("cvFile").addEventListener("change", () => {
  const name = document.getElementById("cvFile").files?.[0]?.name;
  if (name) show(document.getElementById("step1Ok"), `PDF listo: ${name}`);
});

document.getElementById("jsonFile").addEventListener("change", () => {
  const name = document.getElementById("jsonFile").files?.[0]?.name;
  if (name) show(document.getElementById("step1Ok"), `JSON listo: ${name}`);
});

btnProcess.addEventListener("click", async () => {
  show(document.getElementById("step1Error"), "");
  show(document.getElementById("step1Ok"), "");
  const file = document.getElementById("cvFile").files?.[0];
  if (!file) { show(document.getElementById("step1Error"), "Selecciona un PDF."); return; }
  const form = new FormData();
  form.append("file", file);
  setLoading(btnProcess, document.getElementById("processLabel"), document.getElementById("processSpinner"), true, "Procesar CV", "Procesando…");
  try {
    const res = await fetch(`${API_BASE}/upload-cv`, { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    applyProfile(data.profile, "Perfil válido · extraído del CV");
    show(document.getElementById("step1Ok"), "CV procesado.");
  } catch (err) {
    show(document.getElementById("step1Error"), err.message || String(err));
  } finally {
    setLoading(btnProcess, document.getElementById("processLabel"), document.getElementById("processSpinner"), false, "Procesar CV", "Procesando…");
  }
});

document.getElementById("btnLoadJson").addEventListener("click", async () => {
  show(document.getElementById("step1Error"), "");
  const file = document.getElementById("jsonFile").files?.[0];
  if (!file) { show(document.getElementById("step1Error"), "Selecciona un .json."); return; }
  try {
    const data = JSON.parse(await file.text());
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("JSON inválido.");
    applyProfile(data, `Perfil válido · ${file.name}`);
    show(document.getElementById("step1Ok"), "JSON cargado.");
  } catch (err) {
    setProfileReady(false);
    show(document.getElementById("step1Error"), err.message || String(err));
  }
});

document.getElementById("btnTemplate").addEventListener("click", () => {
  applyProfile({ ...PROFILE_TEMPLATE }, "Perfil válido · plantilla");
  show(document.getElementById("step1Ok"), "Plantilla cargada.");
  setStep1Collapsed(false);
});

document.getElementById("btnValidate").addEventListener("click", () => {
  try {
    applyProfile(getProfile(), "Perfil válido · cargado");
    show(document.getElementById("step1Ok"), "Perfil válido.");
    show(document.getElementById("step1Error"), "");
  } catch (err) {
    setProfileReady(false);
    show(document.getElementById("step1Error"), err.message || String(err));
  }
});

let inputTimer;
profileJson.addEventListener("input", () => {
  clearTimeout(inputTimer);
  inputTimer = setTimeout(() => {
    if (!profileJson.value.trim()) { setProfileReady(false); return; }
    try {
      lastProfile = getProfile();
      saveProfileToStorage(lastProfile);
      updateProfileChip(lastProfile);
      setProfileReady(true, "Perfil válido · detectado");
      step1Summary.textContent = profileSummary(lastProfile);
    } catch {
      profileReady = false;
      profileStatus.textContent = "JSON inválido";
      profileStatus.className = "json-status error";
      step1.classList.remove("complete");
      step1Num.textContent = "1";
    }
  }, 250);
});

document.getElementById("btnDownload").addEventListener("click", () => {
  try {
    const p = getProfile();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(p, null, 2)], { type: "application/json" }));
    a.download = "perfil-cv.json";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    show(document.getElementById("step1Error"), err.message || String(err));
  }
});

btnCancelSearch.addEventListener("click", () => {
  if (!activeSearchController || !activeSearchRunId) return;
  activeSearchCancelled = true;
  btnCancelSearch.disabled = true;
  appendSearchProgress("Cancelando búsqueda…", { tone: "warn" });
  setStatus({
    summary: "<strong>Cancelando…</strong>",
    detail: "Deteniendo las fuentes activas",
    tone: "warn",
    expandLog: true,
  });
  fetch(`${API_BASE}/search-jobs/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: activeSearchRunId }),
  }).catch(() => {});
  activeSearchController.abort();
});

btnSearch.addEventListener("click", async () => {
  show(document.getElementById("step2Error"), "");
  show(document.getElementById("step2Ok"), "");
  show(document.getElementById("step2Empty"), "");
  clearResultsTable();

  const filters = getFilters();
  let profile = null;
  if (profileJson.value.trim()) {
    try {
      profile = getProfile();
      lastProfile = profile;
      setProfileReady(true, "Perfil válido · cargado", { collapse: true });
    } catch {
      show(document.getElementById("step2Error"), "El perfil JSON es inválido. Corregilo o vaciá el editor para buscar sin match.");
      setStep1Collapsed(false);
      return;
    }
  }
  if (!profile && !filters.queries.length) {
    show(document.getElementById("step2Error"), "Sin perfil, indicá al menos un texto de búsqueda.");
    setStepOpen(document.getElementById("step2"), true);
    return;
  }
  if (!profile) {
    lastProfile = null;
    profile = {
      name: "Sin perfil",
      roles: [],
      skills: [],
      experience_years: 0,
      summary: "",
      location: "",
      country: "",
    };
  }

  setLoading(btnSearch, document.getElementById("searchLabel"), document.getElementById("searchSpinner"), true, "Iniciar búsqueda", "Buscando…");
  activeSearchController = new AbortController();
  activeSearchRunId =
    globalThis.crypto?.randomUUID?.() ||
    `search-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  activeSearchCancelled = false;
  btnCancelSearch.disabled = false;
  btnCancelSearch.classList.remove("hidden");
  btnProcess.disabled = true;
  // Pedir permiso de notificaciones Windows (toast + brillo en la barra).
  ensureNotificationPermission();
  setStatus({
    summary: `<strong>Buscando…</strong> multi-fuente en curso`,
    detail: `API: <code>${escapeHtml(API_BASE)}</code>`,
    tone: "busy",
    expandLog: true,
  });
  appendSearchProgress("Lanzando búsqueda multi-fuente…");
  let finishedOk = false;
  let finishedCount = 0;
  let finishedError = "";
  try {
    const res = await fetch(`${API_BASE}/search-jobs-stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-Search-Run-Id": activeSearchRunId,
      },
      body: JSON.stringify({ profile, filters }),
      signal: activeSearchController.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
      throw new Error(detail || `HTTP ${res.status}`);
    }

    let finished = false;
    await readSearchStream(res, (evt) => {
      const type = evt.event || "";
      if (type === "progress") {
        appendSearchProgress(evt.message || "Buscando…");
      } else if (type === "source_done") {
        appendSearchProgress(evt.message || `${evt.source}: ${evt.count || 0}`, {
          tone: evt.ok ? "ok" : "warn",
        });
        const sample = Array.isArray(evt.discarded_sample) ? evt.discarded_sample : [];
        if (sample.length) {
          appendSearchProgress(
            `  Descartes: ` +
              sample
                .slice(0, 4)
                .map((d) => `${d.title || "?"} → ${d.reason_label || d.reason || "?"}`)
                .join(" · "),
            { tone: "warn" },
          );
        }
      } else if (type === "error") {
        throw new Error(evt.message || "Error en la búsqueda");
      } else if (type === "cancelled") {
        activeSearchCancelled = true;
        throw new Error(evt.message || "Búsqueda cancelada.");
      } else if (type === "done") {
        finished = true;
        const meta = evt.analyze_meta || {};
        const analyzeMsg =
          meta.discarded_by_reason && Object.keys(meta.discarded_by_reason).length
            ? `Listo · ${evt.count || (evt.jobs || []).length} oferta(s) finales` +
              ` · análisis descartó ${Object.values(meta.discarded_by_reason).reduce((a, b) => a + b, 0)}` +
              ` (${Object.entries(meta.discarded_by_reason)
                .map(([k, v]) => `${k}: ${v}`)
                .join(", ")})`
            : `Listo · ${evt.count || (evt.jobs || []).length} oferta(s) encontradas.`;
        appendSearchProgress(analyzeMsg, { tone: "ok" });
        renderJobs(evt.jobs || [], evt.sources || {});
        finishedOk = true;
        finishedCount = evt.count || (evt.jobs || []).length || 0;
      }
    });
    if (!finished) {
      throw new Error("La búsqueda terminó sin resultados finales.");
    }
  } catch (err) {
    finishedOk = false;
    if (activeSearchCancelled || err?.name === "AbortError") {
      finishedError = "";
      appendSearchProgress("Búsqueda cancelada.", { tone: "warn" });
      show(document.getElementById("step2Empty"), "Búsqueda cancelada.");
      setStatus({
        summary: "<strong>Búsqueda cancelada</strong>",
        detail: "Podés iniciar una nueva búsqueda",
        tone: "warn",
        expandLog: true,
      });
    } else {
      finishedError = err.message || String(err);
      show(document.getElementById("step2Error"), finishedError);
      setStatus({
        summary: `<strong>Error</strong> en la búsqueda`,
        detail: escapeHtml(finishedError),
        tone: "warn",
        expandLog: true,
      });
    }
  } finally {
    setLoading(btnSearch, document.getElementById("searchLabel"), document.getElementById("searchSpinner"), false, "Iniciar búsqueda", "Buscando…");
    btnCancelSearch.classList.add("hidden");
    btnCancelSearch.disabled = false;
    activeSearchController = null;
    activeSearchRunId = null;
    btnProcess.disabled = false;
    if (!activeSearchCancelled) {
      notifySearchFinished({
        ok: finishedOk,
        count: finishedCount,
        error: finishedError,
      });
    }
    activeSearchCancelled = false;
  }
});

document.getElementById("btnToggleFilters").addEventListener("click", () => {
  sidebar.classList.remove("collapsed");
  document.getElementById("sidebarCollapseBtn").textContent = "⟨";
  setStepOpen(document.getElementById("step2"), true);
  syncFiltersToggle();
});

document.getElementById("tableSearch").addEventListener("input", (e) => {
  tableQuery = e.target.value.trim().toLowerCase();
  applyTableView();
});

document.getElementById("sortSelect").addEventListener("change", (e) => {
  tableSort = e.target.value;
  applyTableView();
});

document.querySelectorAll("thead th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    tableSort = th.dataset.sort;
    document.getElementById("sortSelect").value = tableSort;
    applyTableView();
  });
});

resultsBody.addEventListener("click", async (e) => {
  const visitLink = e.target.closest("[data-visit]");
  if (visitLink) {
    const id = visitLink.getAttribute("data-visit");
    setRowState(id, { visited: true });
    updateRowAfterStateChange(visitLink.closest("tr"), id);
    return;
  }

  const interestBtn = e.target.closest("[data-interest]");
  if (interestBtn) {
    const id = interestBtn.getAttribute("data-interest");
    const tr = interestBtn.closest("tr");
    const s = getRowState(id);
    if (s.status === "interested") setRowState(id, { status: null });
    else setRowState(id, { status: "interested" });
    updateRowAfterStateChange(tr, id);
    return;
  }

  const notBtn = e.target.closest("[data-not-interest]");
  if (notBtn) {
    const id = notBtn.getAttribute("data-not-interest");
    const tr = notBtn.closest("tr");
    const s = getRowState(id);
    if (s.status === "not_interested") setRowState(id, { status: null });
    else setRowState(id, { status: "not_interested" });
    updateRowAfterStateChange(tr, id);
    return;
  }

  const btn = e.target.closest("[data-cover-gen]");
  if (btn) {
    const idx = Number(btn.getAttribute("data-cover-gen"));
    const job = currentJobs[idx];
    if (!job) return;

    const profile = lastProfile || (() => { try { return getProfile(); } catch { return null; } })();
    if (!profile) {
      show(document.getElementById("step2Error"), "Necesitas un perfil válido para generar la carta.");
      return;
    }

    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await fetch(`${API_BASE}/generate-cover-letter`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile, job }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
      job.cover_letter = data.cover_letter || "";
      coverTitle.textContent = `Cover Letter · ${job.title || "Oferta"}`;
      coverBody.textContent = job.cover_letter;
      coverModal.classList.add("open");
    } catch (err) {
      show(document.getElementById("step2Error"), err.message || String(err));
      sidebar.classList.remove("collapsed");
      document.getElementById("sidebarCollapseBtn").textContent = "⟨";
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
    return;
  }

  // PASO 4 · borrador de email (asunto + cuerpo + recordatorio CV) si hay contact_email
  const emailBtn = e.target.closest("[data-email-gen]");
  if (!emailBtn) return;
  const emailIdx = Number(emailBtn.getAttribute("data-email-gen"));
  const emailJob = currentJobs[emailIdx];
  if (!emailJob || !emailJob.contact_email) return;

  const emailProfile = lastProfile || (() => { try { return getProfile(); } catch { return null; } })();
  if (!emailProfile) {
    show(document.getElementById("step2Error"), "Necesitas un perfil válido para generar el email.");
    return;
  }

  const emailPrev = emailBtn.textContent;
  emailBtn.disabled = true;
  emailBtn.textContent = "…";
  try {
    const res = await fetch(`${API_BASE}/generate-application-email`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: emailProfile, job: emailJob }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    const draft = data.application_email || {};
    emailJob.application_email = draft;
    coverTitle.textContent = `Email · ${emailJob.title || "Oferta"}`;
    coverBody.textContent = [
      `Para: ${draft.to || emailJob.contact_email}`,
      `Asunto: ${draft.subject || ""}`,
      "",
      draft.body || "",
      "",
      "—",
      draft.cv_reminder || "Recordá adjuntar tu CV en PDF antes de enviar.",
    ].join("\n");
    coverModal.classList.add("open");
  } catch (err) {
    show(document.getElementById("step2Error"), err.message || String(err));
    sidebar.classList.remove("collapsed");
    document.getElementById("sidebarCollapseBtn").textContent = "⟨";
  } finally {
    emailBtn.disabled = false;
    emailBtn.textContent = emailPrev;
  }
});

function closeModal() { coverModal.classList.remove("open"); }
document.getElementById("btnCloseModal").addEventListener("click", closeModal);
document.getElementById("btnCloseModal2").addEventListener("click", closeModal);
coverModal.addEventListener("click", (e) => { if (e.target === coverModal) closeModal(); });
document.getElementById("btnCopyCover").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(coverBody.textContent || ""); } catch (_) {}
});

function formatAuthTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString("es", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return iso;
  }
}

async function refreshAuthSessions() {
  const box = document.getElementById("authSessionsBody");
  const hint = document.getElementById("authBrowserHint");
  try {
    const res = await fetch(`${API_BASE}/auth/sessions`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const sessions = data.sessions || {};
    const browser = data.browser || {};
    const pendingAll = data.pending || {};
    if (hint) {
      hint.textContent = browser.channel_label
        ? `Se usará ${browser.channel_label} (perfil JobSearch, sin reiniciar el tuyo).`
        : "";
    }
    Object.keys(pendingAll).forEach((k) => {
      const p = pendingAll[k];
      if (p && (p.status === "starting" || p.status === "running")) {
        show(document.getElementById("step2Ok"), p.message || "Captura en curso…");
        pollPendingCapture(k).then(() => refreshAuthSessions()).catch(() => {});
      } else if (p && p.status === "done") {
        show(document.getElementById("step2Ok"), p.message || "Sesión capturada.");
      }
    });
    const order = ["linkedin", "computrabajo"];
    box.innerHTML = order.map((key) => {
      const s = sessions[key] || { label: key, logged_in: false };
      const ok = !!s.logged_in;
      const status = ok
        ? `● Activa${s.updated_at ? ` · ${escapeHtml(formatAuthTime(s.updated_at))}` : ""}`
        : "○ Sin sesión capturada";
      return `<div class="session-card">
        <div class="session-row">
          <div>
            <div class="session-name">${escapeHtml(s.label || key)}</div>
            <div class="session-status ${ok ? "ok" : "off"}">${status}</div>
          </div>
          <div class="session-actions">
            <button type="button" class="icon-btn" data-auth-login="${escapeHtml(key)}"
              title="${ok ? "Renovar sesión" : "Iniciar sesión"}">${ok ? "↻" : "→"}</button>
            <button type="button" class="icon-btn" data-auth-import="${escapeHtml(key)}"
              title="Importar cookies del navegador diario">⇩</button>
            <button type="button" class="icon-btn" data-auth-clear="${escapeHtml(key)}"
              title="Cerrar sesión" ${ok ? "" : "disabled"}>✕</button>
          </div>
        </div>
      </div>`;
    }).join("");
  } catch (err) {
    box.innerHTML = `<p class="msg msg-error">${escapeHtml(err.message || String(err))}</p>`;
  }
}

document.getElementById("btnRefreshAuth").addEventListener("click", () => refreshAuthSessions());

async function runAuthLogin(site, { forceRestart = false, mode = "profile" } = {}) {
  const res = await fetch(`${API_BASE}/auth/login/${encodeURIComponent(site)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      timeout_sec: 600,
      mode,
      force_restart: !!forceRestart,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 409 && data.detail && data.detail.code === "browser_restart_required") {
    const label = data.detail.channel_label || "tu navegador";
    const ok = window.confirm(
      `${data.detail.message || ""}\n\n` +
      `¿Cerrar ${label} ahora y reabrirlo una vez?\n\n` +
      `(Solo para “Importar diario”. El botón normal ya no pide esto.)`
    );
    if (!ok) throw new Error("Cancelado.");
    return runAuthLogin(site, { forceRestart: true, mode: "system" });
  }
  if (!res.ok) {
    const detail = typeof data.detail === "string"
      ? data.detail
      : (data.detail && data.detail.message) || JSON.stringify(data.detail || data);
    throw new Error(detail || `HTTP ${res.status}`);
  }
  if (data.pending) {
    show(document.getElementById("step2Ok"), data.message || "Captura en curso…");
    await pollPendingCapture(site);
    return data;
  }
  return data;
}

async function pollPendingCapture(site) {
  const started = Date.now();
  while (Date.now() - started < 120000) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const res = await fetch(`${API_BASE}/auth/sessions`);
      const data = await res.json().catch(() => ({}));
      const pending = (data.pending && data.pending[site]) || null;
      const sess = (data.sessions && data.sessions[site]) || {};
      if (pending && pending.status === "done") {
        show(document.getElementById("step2Ok"), pending.message || "Sesión capturada.");
        return;
      }
      if (pending && pending.status === "error") {
        throw new Error(pending.error || pending.message || "Error al capturar sesión");
      }
      if (sess.logged_in && (!pending || pending.status === "done")) {
        show(document.getElementById("step2Ok"), `Sesión de ${sess.label || site} lista.`);
        return;
      }
      if (pending && pending.message) {
        show(document.getElementById("step2Ok"), pending.message);
      }
    } catch (err) {
      if (Date.now() - started > 90000) throw err;
    }
  }
  throw new Error("La captura tardó demasiado. Probá python -m backend.auth.login " + site + " --force-restart");
}

document.getElementById("authSessionsBody").addEventListener("click", async (e) => {
  const loginBtn = e.target.closest("[data-auth-login]");
  const importBtn = e.target.closest("[data-auth-import]");
  const clearBtn = e.target.closest("[data-auth-clear]");
  if (loginBtn || importBtn) {
    const btn = loginBtn || importBtn;
    const site = btn.getAttribute(loginBtn ? "data-auth-login" : "data-auth-import");
    const mode = importBtn ? "system" : "profile";
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "…";
    show(document.getElementById("step2Error"), "");
    try {
      const data = await runAuthLogin(site, { mode });
      show(document.getElementById("step2Ok"), data.message || "Sesión guardada.");
    } catch (err) {
      show(document.getElementById("step2Error"), err.message || String(err));
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
      await refreshAuthSessions();
    }
    return;
  }
  if (clearBtn) {
    const site = clearBtn.getAttribute("data-auth-clear");
    clearBtn.disabled = true;
    try {
      const res = await fetch(`${API_BASE}/auth/sessions/${encodeURIComponent(site)}`, { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    } catch (err) {
      show(document.getElementById("step2Error"), err.message || String(err));
    } finally {
      await refreshAuthSessions();
    }
  }
});

(function restoreSavedProfile() {
  let raw = null;
  try { raw = localStorage.getItem(PROFILE_STORAGE_KEY); } catch (e) { return; }
  if (!raw) return;
  try {
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      applyProfile(obj, "Perfil válido · restaurado");
    }
  } catch (e) {}
})();

updatePulse(null);
updateFooterNote();
bindFilterChips();
bindStatusFilterChips();
refreshAuthSessions();

// ─── API Key de Gemini ───────────────────────────────────────────────────────

const apiKeyModal   = document.getElementById("apiKeyModal");
const apiKeyInput   = document.getElementById("apiKeyInput");
const apiKeyError   = document.getElementById("apiKeyError");
const apiKeySpinner = document.getElementById("apiKeySpinner");
const apiKeyBtnLabel= document.getElementById("apiKeyBtnLabel");
const apiKeyBadge   = document.getElementById("apiKeyBadge");
const apiKeyBadgeIcon  = document.getElementById("apiKeyBadgeIcon");
const apiKeyBadgeLabel = document.getElementById("apiKeyBadgeLabel");

function showApiKeyError(msg) {
  apiKeyError.textContent = msg || "";
  apiKeyError.classList.toggle("hidden", !msg);
}

function openApiKeyModal() {
  apiKeyModal.classList.add("open");
  apiKeyInput.value = "";
  showApiKeyError("");
  setTimeout(() => apiKeyInput.focus(), 60);
}

function closeApiKeyModal() {
  apiKeyModal.classList.remove("open");
}

function setApiKeyBadge(source) {
  if (source === "env" || source === "runtime") {
    apiKeyBadge.classList.remove("hidden");
    apiKeyBadge.classList.toggle("badge--ok", true);
    apiKeyBadge.classList.toggle("badge--warn", false);
    apiKeyBadgeIcon.textContent  = "✓";
    apiKeyBadgeLabel.textContent = source === "env" ? "API Key (.env)" : "API Key (sesión)";
    apiKeyBadge.title = source === "env"
      ? "API Key cargada desde .env"
      : "API Key configurada para esta sesión (se pierde al reiniciar el servidor)";
  } else {
    apiKeyBadge.classList.remove("hidden");
    apiKeyBadge.classList.toggle("badge--ok", false);
    apiKeyBadge.classList.toggle("badge--warn", true);
    apiKeyBadgeIcon.textContent  = "⚠";
    apiKeyBadgeLabel.textContent = "Sin API Key";
    apiKeyBadge.title = "No hay API Key de Gemini configurada. Hacé clic para configurarla.";
  }
}

async function checkApiKeyStatus() {
  try {
    const res  = await fetch(`${API_BASE}/api/key-status`);
    const data = await res.json().catch(() => ({}));
    setApiKeyBadge(data.source || "none");
    if (!data.has_key) {
      openApiKeyModal();
    }
  } catch {
    // Si el servidor no responde no bloqueamos la UI
    setApiKeyBadge("none");
  }
}

async function saveApiKey() {
  const key = (apiKeyInput.value || "").trim();
  if (!key) { showApiKeyError("Ingresá tu API key antes de continuar."); return; }

  apiKeySpinner.classList.remove("hidden");
  apiKeyBtnLabel.textContent = "Guardando…";
  showApiKeyError("");

  try {
    const res  = await fetch(`${API_BASE}/api/set-key`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(
        typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`
      );
    }
    setApiKeyBadge("runtime");
    closeApiKeyModal();
  } catch (err) {
    showApiKeyError(err.message || "No se pudo guardar la API key.");
  } finally {
    apiKeySpinner.classList.add("hidden");
    apiKeyBtnLabel.textContent = "Guardar y continuar";
  }
}

document.getElementById("btnSaveApiKey").addEventListener("click", saveApiKey);
document.getElementById("btnCancelApiKey").addEventListener("click", closeApiKeyModal);
document.getElementById("btnCloseApiKey").addEventListener("click", closeApiKeyModal);

apiKeyInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveApiKey();
});

apiKeyModal.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeApiKeyModal();
});

document.getElementById("apiKeyShow").addEventListener("change", (e) => {
  apiKeyInput.type = e.target.checked ? "text" : "password";
});

apiKeyBadge.addEventListener("click", openApiKeyModal);

checkApiKeyStatus();

/** Cierra la ventana cuando el servidor deja de responder (Ctrl+C / cerrar CMD). */
function watchServerAlive() {
  if (!(location.hostname === "127.0.0.1" || location.hostname === "localhost")) return;

  let failing = false;
  let failStreak = 0;

  function tryCloseUi() {
    document.title = "Servidor detenido — JobSearch";
    try {
      window.close();
    } catch {
      /* ignore */
    }
    // Si el navegador bloquea close() (pestaña normal), al menos avisar.
    setTimeout(() => {
      if (!document.hidden) {
        document.body.innerHTML = `
          <div style="font-family:system-ui;padding:48px;max-width:420px;margin:10vh auto;text-align:center;color:#12181C">
            <h1 style="font-size:1.25rem;margin:0 0 8px">Servidor detenido</h1>
            <p style="margin:0;color:#4B565E;line-height:1.5">
              La terminal de JobSearch se cerró. Podés cerrar esta pestaña.
            </p>
          </div>`;
      }
    }, 250);
  }

  async function probe() {
    try {
      const res = await fetch(`${API_BASE}/health`, { cache: "no-store" });
      if (!res.ok) throw new Error("bad status");
      failStreak = 0;
      failing = false;
    } catch {
      failStreak += 1;
      failing = true;
      if (failStreak >= 2) tryCloseUi();
    }
  }

  try {
    const es = new EventSource(`${API_BASE}/events/alive`);
    es.onmessage = () => {
      failStreak = 0;
      failing = false;
    };
    es.onerror = () => {
      // EventSource reintenta solo; confirmamos con /health.
      if (!failing) failing = true;
      probe();
    };
  } catch {
    /* ignore */
  }

  setInterval(probe, 3000);
}

watchServerAlive();
