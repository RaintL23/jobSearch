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

let currentJobs = [];
let displayJobs = [];
let profileReady = false;
let lastProfile = null;
let lastSources = {};
let tableFilter = "all";
let tableQuery = "";
let tableSort = "match";
let discardedIds = new Set();
let savedIds = new Set();

const PROFILE_STORAGE_KEY = "jobsearch_profile_v1";
function saveProfileToStorage(obj) {
  try { localStorage.setItem(PROFILE_STORAGE_KEY, JSON.stringify(obj)); } catch (e) {}
}

function show(el, msg) {
  el.textContent = msg || "";
  el.classList.toggle("hidden", !msg);
}

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
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = !wrap.classList.contains("open");
    document.querySelectorAll(".ms-wrap.open").forEach((el) => {
      if (el !== wrap) el.classList.remove("open");
    });
    setOpen(willOpen);
  });

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
  countries: createMultiSelect(document.querySelector('[data-ms="countries"]'), {
    label: "Países",
    placeholder: "Del perfil / cualquiera",
    options: [
      ["mx", "México"], ["co", "Colombia"], ["ar", "Argentina"], ["pe", "Perú"],
      ["cl", "Chile"], ["ec", "Ecuador"], ["uy", "Uruguay"], ["ve", "Venezuela"],
      ["cr", "Costa Rica"], ["pa", "Panamá"], ["gt", "Guatemala"], ["bo", "Bolivia"],
      ["py", "Paraguay"], ["do", "Rep. Dominicana"], ["hn", "Honduras"],
      ["sv", "El Salvador"], ["ni", "Nicaragua"], ["cu", "Cuba"], ["pr", "Puerto Rico"],
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
  document.querySelectorAll(".ms-wrap.open").forEach((el) => el.classList.remove("open"));
});

function updateFooterNote() {
  const srcs = multiFilters.sources.values;
  const n = srcs.length || 7;
  document.getElementById("footerNote").textContent = profileReady
    ? `${n} fuente${n === 1 ? "" : "s"} · listo para buscar`
    : "Elegí fuentes y perfil";
}

function updateStep2Summary() {
  const modes = multiFilters.workMode.values;
  const countries = multiFilters.countries.values;
  const modeLabels = { remote: "Remoto", hybrid: "Híbrido", onsite: "Presencial" };
  const countryLabels = Object.fromEntries([
    ["mx", "México"], ["co", "Colombia"], ["ar", "Argentina"], ["pe", "Perú"],
    ["cl", "Chile"], ["ec", "Ecuador"], ["uy", "Uruguay"], ["ve", "Venezuela"],
    ["cr", "Costa Rica"], ["pa", "Panamá"], ["gt", "Guatemala"], ["bo", "Bolivia"],
    ["py", "Paraguay"], ["do", "RD"], ["hn", "Honduras"], ["sv", "El Salvador"],
    ["ni", "Nicaragua"], ["cu", "Cuba"], ["pr", "PR"],
  ]);
  const parts = [];
  if (modes.length) parts.push(modes.map((m) => modeLabels[m] || m).join("/"));
  if (countries.length) parts.push(countries.map((c) => countryLabels[c] || c).slice(0, 2).join(", "));
  document.getElementById("step2Summary").textContent = parts.length ? parts.join(" · ") : "Configura fuentes";
}

function setLoading(btn, labelEl, spinnerEl, loading, idle, busy) {
  if (btn === btnSearch) btn.disabled = loading || !profileReady;
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
  btnSearch.disabled = !ready;
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
  if (obj.country) {
    const code = String(obj.country).toLowerCase();
    if (!multiFilters.countries.values.length) {
      multiFilters.countries.setValues([code]);
    }
  }
  setProfileReady(true, okMsg || "Perfil válido · cargado", { collapse: true });
  updateStep2Summary();
}

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
    countries: multiFilters.countries.values,
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
  discardedIds = new Set();
  savedIds = new Set();
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
    const count = typeof info.count === "number" ? ` (${info.count})` : "";
    return `<div class="log-line ${ok ? "ok" : "warn"}"><strong>${ok ? "✓" : "!"} ${name}${count}:</strong> ${escapeHtml(info.message || "Sin información.")}</div>`;
  }).join("");
  box.innerHTML = `<div class="src-title">Estado del scraping</div>${html}`;

  tally.innerHTML = order.map(([key, name]) => {
    const info = (sources && sources[key]) || {};
    const ok = !!info.ok;
    const count = typeof info.count === "number" ? info.count : 0;
    return `<span class="tally-pill"><span class="dot" style="background:${ok ? "var(--green)" : "var(--amber)"}"></span>${escapeHtml(name)} · ${count}</span>`;
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

function applyTableView() {
  let list = currentJobs.map((job, idx) => ({ job, idx }));
  if (tableFilter !== "all") {
    list = list.filter(({ job }) => sourceLabel(job.source).key === tableFilter);
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
    const pct = Number(job.match_percent) || 0;
    const tier = tierOf(pct);
    const src = sourceLabel(job.source);
    const company = job.company || "Empresa no indicada";
    const pub = formatPublishedParts(job.published_at);
    const id = jobId(job, idx);
    const tr = document.createElement("tr");
    tr.className = `tier-${tier}` + (discardedIds.has(id) ? " discarded" : "");
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
          <div class="match-bars">${matchBars(pct)}</div>
          <span class="match-pct">${pct}%</span>
        </div>
      </td>
      <td>
        <div class="actions-cell">
          ${job.url
            ? `<a class="act-btn view" href="${escapeHtml(job.url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer" title="Ver oferta">↗</a>`
            : `<button type="button" class="act-btn view" disabled title="Sin link">↗</button>`}
          <button type="button" class="act-btn cover" data-cover-gen="${idx}" title="Cover letter">CL</button>
          <button type="button" class="act-btn save ${savedIds.has(id) ? "active" : ""}" data-save="${escapeHtml(id)}" title="Guardar">★</button>
          <button type="button" class="act-btn discard" data-discard="${escapeHtml(id)}" title="Descartar">✕</button>
        </div>
      </td>`;
    frag.appendChild(tr);
  });
  resultsBody.appendChild(frag);
}

function renderJobs(jobs, sources) {
  currentJobs = jobs || [];
  discardedIds = new Set();
  savedIds = new Set();
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
      btnSearch.disabled = true;
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

btnSearch.addEventListener("click", async () => {
  show(document.getElementById("step2Error"), "");
  show(document.getElementById("step2Ok"), "");
  show(document.getElementById("step2Empty"), "");
  clearResultsTable();

  let profile;
  try {
    profile = getProfile();
    lastProfile = profile;
    setProfileReady(true, "Perfil válido · cargado", { collapse: true });
  } catch (err) {
    show(document.getElementById("step2Error"), err.message || String(err));
    setStep1Collapsed(false);
    return;
  }

  setLoading(btnSearch, document.getElementById("searchLabel"), document.getElementById("searchSpinner"), true, "Iniciar búsqueda", "Buscando…");
  btnProcess.disabled = true;
  setStatus({
    summary: `<strong>Buscando…</strong> multi-fuente en curso`,
    detail: `API: <code>${escapeHtml(API_BASE)}</code>`,
    tone: "busy",
    expandLog: true,
  });
  appendSearchProgress("Lanzando búsqueda multi-fuente…");
  try {
    const res = await fetch(`${API_BASE}/search-jobs-stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ profile, filters: getFilters() }),
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
      } else if (type === "error") {
        throw new Error(evt.message || "Error en la búsqueda");
      } else if (type === "done") {
        finished = true;
        appendSearchProgress(
          `Listo · ${evt.count || (evt.jobs || []).length} oferta(s) tras filtros de match.`,
          { tone: "ok" },
        );
        renderJobs(evt.jobs || [], evt.sources || {});
      }
    });
    if (!finished) {
      throw new Error("La búsqueda terminó sin resultados finales.");
    }
  } catch (err) {
    show(document.getElementById("step2Error"), err.message || String(err));
    setStatus({
      summary: `<strong>Error</strong> en la búsqueda`,
      detail: escapeHtml(err.message || String(err)),
      tone: "warn",
      expandLog: true,
    });
  } finally {
    setLoading(btnSearch, document.getElementById("searchLabel"), document.getElementById("searchSpinner"), false, "Iniciar búsqueda", "Buscando…");
    btnProcess.disabled = false;
    btnSearch.disabled = !profileReady;
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
  const saveBtn = e.target.closest("[data-save]");
  if (saveBtn) {
    const id = saveBtn.getAttribute("data-save");
    if (savedIds.has(id)) savedIds.delete(id);
    else savedIds.add(id);
    saveBtn.classList.toggle("active");
    return;
  }

  const discardBtn = e.target.closest("[data-discard]");
  if (discardBtn) {
    const id = discardBtn.getAttribute("data-discard");
    const tr = discardBtn.closest("tr");
    if (discardedIds.has(id)) discardedIds.delete(id);
    else discardedIds.add(id);
    tr.classList.toggle("discarded");
    return;
  }

  const btn = e.target.closest("[data-cover-gen]");
  if (!btn) return;
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
  throw new Error("La captura tardó demasiado. Probá python -m backend.login_session " + site + " --force-restart");
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

apiKeyInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveApiKey();
});

document.getElementById("apiKeyShow").addEventListener("change", (e) => {
  apiKeyInput.type = e.target.checked ? "text" : "password";
});

apiKeyBadge.addEventListener("click", openApiKeyModal);

checkApiKeyStatus();
