// Utilidades puras de UI (formato, escape, parsing de fechas/URLs).
// Script clásico cargado antes de app.js: comparte el scope global, sin estado
// propio ni acceso al DOM. Extraído de app.js para reducir su tamaño.
"use strict";

function splitMulti(text) {
  return String(text || "")
    .split(/[\n,;|]+/)
    .map((s) => s.trim())
    .filter(Boolean);
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

function formatAuthTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString("es", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return iso;
  }
}
