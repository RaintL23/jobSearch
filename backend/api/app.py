"""
API FastAPI: CV, búsqueda (análisis local) y cover letter / email bajo demanda.

=============================================================================
PIPELINE (mismas etapas en todas las fuentes)
  PASO 1–2 · scraping.search_jobs / scraping.sources.api  → ofertas crudas
  PASO 3   · analysis.local.analyze_job_local    → ubicación, salario, skills, email
  PASO 4   · _analyze_raw_jobs                 → filtros + match; email IA on-demand
=============================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from backend.ai.engine import (
    AIEngineError,
    batch_analyze_relevance,
    extract_profile_from_cv,
    generate_application_email,
    generate_cover_letter,
)
from backend.auth.sessions import (
    AUTH_SITES,
    BrowserRestartRequired,
    cdp_status,
    clear_session,
    interactive_login,
    session_status,
)
from backend.core.config import get_settings
from backend.core.runtime_key import has_runtime_key, set_runtime_key
from backend.analysis.local import (
    analyze_job_local,
    linkedin_hiring_location_ok,
    passes_gob_country_filter,
    passes_language_filters,
    salary_in_range,
)
from backend.core.dates import hours_since_published
from backend.core.query_match import extract_location
from backend.scraping import (
    SOURCE_LABELS,
    SOURCE_LATAM_RANK,
    is_linkedin_hiring_permalink,
    search_jobs,
)
from backend.core.utils import PDFExtractionError, extract_text_from_pdf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

_login_locks: dict[str, threading.Lock] = {s: threading.Lock() for s in AUTH_SITES}
_pending_captures: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()


def _set_pending(site: str, **fields: Any) -> None:
    with _pending_lock:
        cur = dict(_pending_captures.get(site) or {})
        cur.update(fields)
        cur["site"] = site
        _pending_captures[site] = cur


def _get_pending(site: str) -> dict[str, Any] | None:
    with _pending_lock:
        return dict(_pending_captures[site]) if site in _pending_captures else None


def _clear_pending(site: str) -> None:
    with _pending_lock:
        _pending_captures.pop(site, None)

app = FastAPI(
    title="AI Job Scraper & Matcher",
    description="Perfil CV/JSON, scraping LATAM multi-fuente, match local y cover letter on-demand.",
    version="1.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # El wildcard con credenciales es inválido según la spec CORS y la app no
    # usa cookies entre orígenes; se desactivan para una config correcta.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProfilePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "Candidato"
    roles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    experience_years: int | float = 0
    summary: str = ""
    location: str = ""
    country: str = "mx"


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="ignore")

    queries: list[str] = Field(default_factory=list)
    query: str = ""
    locations: list[str] = Field(default_factory=list)
    location: str = ""
    # Multi-selección (vacío = cualquiera)
    posted_within: list[str] = Field(default_factory=list)
    experience_levels: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    # Compat
    experience_level: str = "any"
    work_mode: str = "any"
    country: str = ""
    salary_min_usd: float | None = None
    salary_max_usd: float | None = None
    posting_languages: list[str] = Field(default_factory=list)
    required_languages: list[str] = Field(default_factory=list)
    posting_language: str = "any"
    required_language: str = "any"
    sources: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    profile: ProfilePayload = Field(default_factory=lambda: ProfilePayload(country=""))
    filters: SearchFilters = Field(default_factory=SearchFilters)


class CoverLetterRequest(BaseModel):
    profile: ProfilePayload
    job: dict[str, Any]


class ApplicationEmailRequest(BaseModel):
    """PASO 4 · borrador de email cuando la oferta trae contact_email."""

    profile: ProfilePayload
    job: dict[str, Any]


@app.get("/")
async def serve_index() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html no encontrado.")
    return FileResponse(index_path)


@app.post("/upload-cv")
async def upload_cv(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")

    try:
        cv_text = extract_text_from_pdf(content)
    except PDFExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        profile = await asyncio.to_thread(extract_profile_from_cv, cv_text)
    except AIEngineError as exc:
        logger.exception("Error de IA en /upload-cv")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"profile": profile, "chars_extracted": len(cv_text)}


def _prepare_search(payload: SearchRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    profile_dict = payload.profile.model_dump()
    filters_dict = payload.filters.model_dump()

    queries = list(filters_dict.get("queries") or [])
    if filters_dict.get("query"):
        queries.append(filters_dict["query"])
    queries = [q.strip() for q in queries if str(q).strip()]
    filters_dict["queries"] = queries

    has_roles = bool(profile_dict.get("roles"))
    has_skills = bool(profile_dict.get("skills"))
    if not queries and not has_roles and not has_skills:
        raise HTTPException(
            status_code=400,
            detail="Indica al menos un texto de búsqueda, o roles/skills en el perfil.",
        )
    return profile_dict, filters_dict


def _analyze_raw_jobs(
    profile_dict: dict[str, Any],
    filters_dict: dict[str, Any],
    raw_jobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    PASO 3–4 unificado (todas las fuentes).

    PASO 3: analyze_job_local (ubicación, salario, skills, contact_email).
    PASO 4: filtros país/ubicación/idioma/salario + match; IA solo si hace falta
    (ubicación ambigua) o bajo demanda (email / cover letter).
    """
    filter_countries: list[str] = list(filters_dict.get("countries") or [])
    profile_country: str = str(profile_dict.get("country") or "")
    filter_locations: list[str] = list(filters_dict.get("locations") or [])
    if profile_dict.get("location"):
        filter_locations = filter_locations + [str(profile_dict["location"])]
    match_available = bool(profile_dict.get("skills") or profile_dict.get("roles"))

    posting_langs = list(filters_dict.get("posting_languages") or [])
    if filters_dict.get("posting_language") and filters_dict["posting_language"] != "any":
        posting_langs.append(filters_dict["posting_language"])
    required_langs = list(filters_dict.get("required_languages") or [])
    if filters_dict.get("required_language") and filters_dict["required_language"] != "any":
        required_langs.append(filters_dict["required_language"])

    results: list[dict[str, Any]] = []
    discard_counts: dict[str, int] = {}
    discard_sample: list[dict[str, Any]] = []

    def _note_discard(job: dict[str, Any], reason: str) -> None:
        discard_counts[reason] = discard_counts.get(reason, 0) + 1
        if len(discard_sample) < 12:
            discard_sample.append(
                {
                    "title": str(job.get("title") or "")[:120],
                    "company": str(job.get("company") or "")[:80],
                    "reason": reason,
                    "reason_label": {
                        "country": "país",
                        "language": "idioma",
                        "salary": "salario",
                        "invalid_link": "enlace no específico",
                    }.get(reason, reason),
                }
            )

    hiring_user_country = (filter_countries[0] if filter_countries else profile_country) or ""

    for job in raw_jobs:
        # --- PASO 4 · filtro país (GetOnBoard, datos estructurados) ---
        if job.get("source") == "getonboard":
            if not passes_gob_country_filter(job, filter_countries, profile_country):
                logger.debug(
                    "GOB country filter: descartando '%s' (%s) — países oferta: %s",
                    job.get("title"),
                    job.get("company"),
                    job.get("_countries_raw"),
                )
                _note_discard(job, "country")
                continue

        # --- PASO 4 · filtro ubicación (LinkedIn #Hiring) ---
        needs_ai_location = False
        if job.get("source") == "linkedin_hiring":
            if not is_linkedin_hiring_permalink(str(job.get("url") or "")):
                _note_discard(job, "invalid_link")
                continue
            verdict = linkedin_hiring_location_ok(
                str(job.get("description") or ""),
                hiring_user_country,
                filter_locations,
            )
            if verdict is False:
                logger.debug(
                    "LinkedIn #Hiring location filter: descartando '%s' (%s)",
                    job.get("title"),
                    job.get("company"),
                )
                _note_discard(job, "country")
                continue
            needs_ai_location = verdict is None

        # --- PASO 3 · revisar descripción (skills, salario, email, …) ---
        analyzed = analyze_job_local(profile_dict, job)
        if needs_ai_location:
            analyzed["_needs_ai_location"] = True
        if not match_available:
            analyzed["match_percent"] = None
            analyzed["matched_skills"] = []
            analyzed["missing_skills"] = []
            analyzed["advice"] = "Cargá un perfil CV para calcular el match y recibir recomendaciones."

        # --- PASO 4 · clasificación: idioma + salario + skills (match ya calculado) ---
        if not passes_language_filters(analyzed, posting_langs, required_langs):
            _note_discard(job, "language")
            continue
        if not salary_in_range(
            {
                "min_usd": analyzed.get("salary_min_usd"),
                "max_usd": analyzed.get("salary_max_usd"),
            },
            filters_dict.get("salary_min_usd"),
            filters_dict.get("salary_max_usd"),
        ):
            _note_discard(job, "salary")
            continue

        analyzed["description"] = (job.get("description") or "")[:4000]
        analyzed["source"] = job.get("source") or analyzed.get("source") or ""
        analyzed["company"] = job.get("company") or analyzed.get("company") or "Empresa no indicada"
        analyzed["published_at"] = job.get("published_at") or analyzed.get("published_at")
        analyzed["location"] = (
            job.get("location")
            or analyzed.get("location")
            or extract_location(job)
            or ""
        )
        # Pasar datos de países al resultado para el paso de IA (se eliminan al final).
        analyzed["_countries_raw"] = job.get("_countries_raw") or []
        results.append(analyzed)

    # --- PASO 4 · IA batch solo para ubicación ambigua (GOB + #Hiring) ---
    if match_available:
        dropped = _apply_ai_batch(profile_dict, filter_countries, profile_country, results)
        for job in dropped:
            _note_discard(job, "country")

    # Eliminar campos internos antes de devolver al frontend.
    for job in results:
        job.pop("_countries_raw", None)
        job.pop("_needs_ai_location", None)

    def _sort_key(job: dict[str, Any]) -> tuple[int, float, int]:
        hours = hours_since_published(job.get("published_at"))
        return (
            SOURCE_LATAM_RANK.get(str(job.get("source") or ""), 99),
            hours if hours is not None else float("inf"),
            -(int(job.get("match_percent") or 0)),
        )

    results.sort(key=_sort_key)
    analyze_meta = {
        "input_count": len(raw_jobs),
        "kept_count": len(results),
        "match_available": match_available,
        "discarded_by_reason": discard_counts,
        "discarded_sample": discard_sample,
    }
    return results, analyze_meta


def _format_analyze_discard_message(meta: dict[str, Any]) -> str:
    input_n = int(meta.get("input_count") or 0)
    kept_n = int(meta.get("kept_count") or 0)
    counts = meta.get("discarded_by_reason") or {}
    discarded_n = input_n - kept_n
    suffix = "con match calculado" if meta.get("match_available") else "sin cálculo de match"
    msg = f"Listo · {kept_n} oferta(s) {suffix}"
    if discarded_n > 0 and counts:
        labels = {
            "country": "país",
            "language": "idioma",
            "salary": "salario",
            "invalid_link": "enlace no específico",
        }
        detail = ", ".join(
            f"{labels.get(k, k)}: {v}"
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        msg += f" · {discarded_n} descartada(s) en análisis ({detail})"
    elif discarded_n > 0:
        msg += f" · {discarded_n} descartada(s) en análisis"
    return msg + "."


def _apply_ai_batch(
    profile_dict: dict[str, Any],
    filter_countries: list[str],
    profile_country: str,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Analiza en batch los casos de ubicación ambigua y match borderline con IA.

    Cubre dos fuentes:
      - GetOnBoard sin países estructurados (match 30-75).
      - LinkedIn #Hiring marcado como ambiguo por la heurística de ubicación.

    Solo actúa si AI_MATCH_ENABLED=true en .env y hay API key configurada.
    Una sola llamada a Gemini por grupo de hasta 6 ofertas (token-eficiente).
    Modifica `results` en lugar (ajusta match_percent y advice) y devuelve la
    lista de ofertas eliminadas por país no compatible (para el conteo de
    descartes).
    """
    settings = get_settings()
    if not settings.ai_match_enabled or (not settings.has_api_key and not has_runtime_key()):
        return []

    # Ofertas con ubicación ambigua que más se benefician del análisis IA:
    #  - GOB sin países estructurados y con match borderline.
    #  - #Hiring que la heurística no pudo clasificar (posible US-only, etc.).
    ambiguous_idxs = [
        i for i, j in enumerate(results)
        if (
            j.get("source") == "getonboard"
            and not j.get("_countries_raw")
            and 30 <= int(j.get("match_percent") or 0) <= 75
        )
        or (j.get("source") == "linkedin_hiring" and j.get("_needs_ai_location"))
    ]

    if not ambiguous_idxs:
        return []

    user_country = (filter_countries[0] if filter_countries else profile_country) or ""
    batch_jobs = [results[i] for i in ambiguous_idxs]

    try:
        ai_entries = batch_analyze_relevance(profile_dict, batch_jobs, user_country)
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch_analyze_relevance falló (se ignora): %s", exc)
        return []

    drop_idxs: set[int] = set()
    for entry in ai_entries:
        batch_pos = int(entry.get("idx", 0)) - 1
        if batch_pos < 0 or batch_pos >= len(ambiguous_idxs):
            continue
        result_idx = ambiguous_idxs[batch_pos]
        job = results[result_idx]

        country_ok = entry.get("country_ok")
        delta = int(entry.get("match_delta") or 0)
        reason = str(entry.get("reason") or "").strip()

        # #Hiring incompatible según la IA → excluir (validación de ubicación).
        if country_ok is False and job.get("source") == "linkedin_hiring":
            drop_idxs.add(result_idx)
            continue

        # GOB: penalización extra si la IA detecta incompatibilidad de país.
        if country_ok is False:
            delta = min(delta, -15)
            reason = f"⚠️ País posiblemente no disponible. {reason}".strip()

        job["match_percent"] = max(5, min(98, int(job.get("match_percent") or 0) + delta))

        if reason:
            existing_advice = job.get("advice") or ""
            job["advice"] = f"[IA] {reason}\n{existing_advice}".strip()

    dropped = [results[i] for i in sorted(drop_idxs)]
    if drop_idxs:
        results[:] = [r for i, r in enumerate(results) if i not in drop_idxs]
    return dropped


@app.post("/search-jobs")
async def search_jobs_endpoint(payload: SearchRequest) -> dict[str, Any]:
    """
    Scrapea ofertas y analiza match/requisitos/salario LOCALMENTE (sin IA).
    Cover letter se genera aparte con /generate-cover-letter.
    """
    profile_dict, filters_dict = _prepare_search(payload)

    try:
        scrape_result = await asyncio.to_thread(search_jobs, profile_dict, None, filters_dict)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error de scraping en /search-jobs")
        raise HTTPException(
            status_code=502,
            detail=f"Error al scrapear ofertas: {exc}",
        ) from exc

    if isinstance(scrape_result, dict):
        raw_jobs = scrape_result.get("jobs") or []
        sources = scrape_result.get("sources") or {}
    else:
        raw_jobs = scrape_result or []
        sources = {}

    results, analyze_meta = _analyze_raw_jobs(profile_dict, filters_dict, raw_jobs)

    return {
        "jobs": results,
        "count": len(results),
        "filters_applied": filters_dict,
        "sources": sources,
        "analyze_meta": analyze_meta,
    }


@app.post("/search-jobs-stream")
async def search_jobs_stream(payload: SearchRequest) -> StreamingResponse:
    """
    Misma búsqueda que /search-jobs pero con eventos SSE de progreso por fuente.
    Formato: lines `data: {json}\\n\\n` (eventos progress | source_done | done | error).
    """
    profile_dict, filters_dict = _prepare_search(payload)
    q: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def on_progress(evt: dict[str, Any]) -> None:
        q.put(evt)

    def worker() -> None:
        try:
            scrape_result = search_jobs(
                profile_dict,
                None,
                filters_dict,
                on_progress=on_progress,
            )
            raw_jobs = scrape_result.get("jobs") or []
            sources = scrape_result.get("sources") or {}
            on_progress(
                {
                    "event": "progress",
                    "source": "all",
                    "message": (
                        f"Analizando match de {len(raw_jobs)} oferta(s)…"
                        if profile_dict.get("skills") or profile_dict.get("roles")
                        else f"Procesando {len(raw_jobs)} oferta(s) sin perfil…"
                    ),
                    "count": len(raw_jobs),
                }
            )
            results, analyze_meta = _analyze_raw_jobs(profile_dict, filters_dict, raw_jobs)
            on_progress(
                {
                    "event": "progress",
                    "source": "all",
                    "message": _format_analyze_discard_message(analyze_meta),
                    "count": len(results),
                }
            )
            q.put(
                {
                    "event": "done",
                    "jobs": results,
                    "count": len(results),
                    "filters_applied": filters_dict,
                    "sources": sources,
                    "analyze_meta": analyze_meta,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error en /search-jobs-stream")
            q.put({"event": "error", "message": f"Error al scrapear ofertas: {exc}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def event_gen() -> Any:
        boot = {
            "event": "progress",
            "source": "all",
            "message": "Conectado. Fuentes: " + ", ".join(SOURCE_LABELS.values()),
        }
        yield f"data: {json.dumps(boot, ensure_ascii=False)}\n\n"
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/generate-cover-letter")
async def cover_letter_endpoint(payload: CoverLetterRequest) -> dict[str, Any]:
    """Genera cover letter con Gemini solo cuando el usuario lo pide."""
    try:
        letter = await asyncio.to_thread(
            generate_cover_letter,
            payload.profile.model_dump(),
            payload.job,
        )
    except AIEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"cover_letter": letter}


@app.post("/generate-application-email")
async def application_email_endpoint(payload: ApplicationEmailRequest) -> dict[str, Any]:
    """
    PASO 4 · genera asunto + cuerpo para postular por email (si hay contact_email).
    Incluye recordatorio de adjuntar el CV. Bajo demanda (no en el scrape).
    """
    job = dict(payload.job or {})
    if not str(job.get("contact_email") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Esta oferta no tiene email de contacto detectado.",
        )
    try:
        draft = await asyncio.to_thread(
            generate_application_email,
            payload.profile.model_dump(),
            job,
        )
    except AIEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"application_email": draft}


@app.get("/auth/sessions")
async def auth_sessions_status(request: Request) -> dict[str, Any]:
    """Estado de sesiones locales + disponibilidad del navegador del sistema."""
    ua = request.headers.get("user-agent")
    pending = {}
    for s in AUTH_SITES:
        p = _get_pending(s)
        if p:
            pending[s] = p
    return {
        "sessions": session_status(),
        "browser": cdp_status(user_agent=ua),
        "pending": pending,
    }


class AuthLoginRequest(BaseModel):
    timeout_sec: int = Field(default=600, ge=60, le=1800)
    mode: str = "profile"  # profile | system | playwright
    force_restart: bool = False
    channel: str | None = None  # chrome | msedge


@app.post("/auth/login/{site}")
async def auth_login(
    site: str,
    request: Request,
    payload: AuthLoginRequest | None = None,
) -> dict[str, Any]:
    """
    Login seguro sin guardar contraseña.

    Default mode=profile: abre Chrome/Edge con perfil JobSearch (no reinicia
    tu navegador diario). mode=system importa el perfil diario vía CDP.
    """
    site = site.lower().strip()
    if site not in AUTH_SITES:
        raise HTTPException(
            status_code=400,
            detail=f"Sitio inválido. Usa: {', '.join(AUTH_SITES)}",
        )
    lock = _login_locks[site]
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Ya hay un login de {AUTH_SITES[site]['label']} en curso.",
        )

    body = payload or AuthLoginRequest()
    ua = request.headers.get("user-agent")
    timeout = body.timeout_sec
    mode = (body.mode or "profile").lower().strip()

    # Solo el import desde perfil diario con reinicio va en background
    if mode == "system" and body.force_restart:
        _set_pending(
            site,
            status="starting",
            message="Reiniciando tu navegador y capturando sesión…",
            error=None,
        )

        def _bg() -> None:
            try:
                _set_pending(site, status="running", message="Capturando sesión…")
                info = interactive_login(
                    site,
                    timeout_sec=timeout,
                    mode="system",
                    channel=body.channel,
                    user_agent=ua,
                    force_restart=True,
                )
                _set_pending(
                    site,
                    status="done",
                    message=(
                        f"Sesión de {info['label']} capturada."
                        + (
                            " Ya estabas logueado."
                            if info.get("already_logged_in")
                            else ""
                        )
                    ),
                    session=info,
                    error=None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Captura en background falló (%s)", site)
                _set_pending(
                    site,
                    status="error",
                    message=str(exc),
                    error=str(exc),
                )
            finally:
                lock.release()

        threading.Thread(target=_bg, name=f"auth-capture-{site}", daemon=True).start()
        return {
            "ok": True,
            "pending": True,
            "message": (
                "Se está reiniciando tu navegador para importar la sesión. "
                "Cuando vuelva a abrir, entrá de nuevo a http://127.0.0.1:8000."
            ),
        }

    def _run() -> dict[str, Any]:
        try:
            return interactive_login(
                site,
                timeout_sec=timeout,
                mode=mode,
                channel=body.channel,
                user_agent=ua,
                force_restart=False,
            )
        finally:
            lock.release()

    try:
        info = await asyncio.to_thread(_run)
    except BrowserRestartRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "browser_restart_required",
                "message": str(exc),
                "channel": exc.channel,
                "channel_label": (
                    "Microsoft Edge" if exc.channel == "msedge" else "Google Chrome"
                ),
            },
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Login interactivo falló (%s)", site)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    src = info.get("captured_from")
    already = info.get("already_logged_in")
    if src == "jobsearch_profile" and already:
        msg = (
            f"Sesión de {info['label']} lista (perfil JobSearch). "
            "No hizo falta volver a loguearte."
        )
    elif src == "jobsearch_profile":
        msg = (
            f"Sesión de {info['label']} guardada. "
            "La próxima vez se reutilizará sin reiniciar tu navegador."
        )
    elif src == "system_browser" and already:
        msg = (
            f"Sesión de {info['label']} importada desde tu navegador diario."
        )
    else:
        msg = f"Sesión de {info['label']} guardada. No se almacenó tu contraseña."

    return {"ok": True, "pending": False, "message": msg, "session": info}


@app.delete("/auth/sessions/{site}")
async def auth_logout(site: str) -> dict[str, Any]:
    site = site.lower().strip()
    if site not in AUTH_SITES:
        raise HTTPException(
            status_code=400,
            detail=f"Sitio inválido. Usa: {', '.join(AUTH_SITES)}",
        )
    info = clear_session(site)
    return {"ok": True, "message": f"Sesión de {info['label']} eliminada.", "session": info}


@app.get("/api/key-status")
async def key_status() -> dict[str, Any]:
    """
    Informa si hay una API key de Gemini disponible y cuál es su origen.
    El frontend la consulta al cargar para decidir si pedir la clave al usuario.
    """
    settings = get_settings()
    if settings.has_api_key:
        return {"has_key": True, "source": "env"}
    if has_runtime_key():
        return {"has_key": True, "source": "runtime"}
    return {"has_key": False, "source": "none"}


class SetKeyRequest(BaseModel):
    api_key: str


@app.post("/api/set-key")
async def set_key(payload: SetKeyRequest) -> dict[str, Any]:
    """
    Recibe una API key de Gemini desde el frontend y la guarda en memoria.
    No se escribe en disco; se pierde al reiniciar el servidor.
    """
    key = (payload.api_key or "").strip()
    if not key or key == "tu_api_key_aqui":
        raise HTTPException(status_code=400, detail="La API key proporcionada no es válida.")
    set_runtime_key(key)
    logger.info("API key de Gemini configurada en memoria desde el frontend.")
    return {"ok": True, "message": "API key configurada para esta sesión."}


@app.delete("/api/set-key")
async def clear_runtime_key() -> dict[str, Any]:
    """Elimina la API key en memoria (útil si el usuario quiere reemplazarla)."""
    set_runtime_key("")
    return {"ok": True, "message": "API key de sesión eliminada."}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/events/alive")
async def alive_stream(request: Request) -> StreamingResponse:
    """
    Heartbeat SSE: la UI lo usa para detectar cuando el servidor se apaga
    y cerrar la pestaña/ventana automáticamente.
    """

    async def event_gen():
        while True:
            if await request.is_disconnected():
                break
            yield "data: ok\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
