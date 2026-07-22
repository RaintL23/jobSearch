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
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.ai.engine import (
    AIEngineError,
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
from backend.scraping import SOURCE_LABELS, search_jobs
from backend.scraping.filters import _normalize_filters
from backend.core.utils import PDFExtractionError, extract_text_from_pdf
from backend.api.schemas import (
    ApplicationEmailRequest,
    AuthLoginRequest,
    CancelSearchRequest,
    CoverLetterRequest,
    SearchRequest,
    SetKeyRequest,
)
from backend.api.pipeline import (
    _analyze_raw_jobs,
    _filters_from_payload,
    _format_analyze_discard_message,
    _prepare_search,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

# Tope de tamaño para el CV subido (evita agotar memoria con archivos enormes).
MAX_CV_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

_login_locks: dict[str, threading.Lock] = {s: threading.Lock() for s in AUTH_SITES}
_pending_captures: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()
_search_runs: dict[str, threading.Event] = {}
_search_runs_lock = threading.Lock()


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

# La UI se sirve desde el mismo origen que la API (127.0.0.1:8000), así que el
# flujo normal no necesita CORS. Restringimos a los orígenes locales conocidos
# para que ninguna página web externa pueda invocar endpoints con efectos
# secundarios (login de navegador, set-key, scraping con tu sesión de LinkedIn).
# Nunca usar "*" aquí: habilitaría CSRF / DNS-rebinding contra la app local.
ALLOWED_ORIGINS = [
    f"http://{host}:{port}"
    for host in ("127.0.0.1", "localhost")
    for port in ("8000", "8080")
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    # Rechazo temprano por Content-Length si el cliente lo informa.
    if file.size is not None and file.size > MAX_CV_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"El PDF supera el máximo de {MAX_CV_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    # Lectura acotada: si excede el tope, cortamos sin cargar todo en memoria.
    content = await file.read(MAX_CV_UPLOAD_BYTES + 1)
    if len(content) > MAX_CV_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"El PDF supera el máximo de {MAX_CV_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
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


@app.post("/resolve-filters")
async def resolve_filters(payload: SearchRequest) -> dict[str, Any]:
    """
    Resuelve los filtros efectivos del backend a partir del perfil.

    `profile.filters` del JSON completa lo que venga vacío en `filters`
    (p. ej. antigüedad, modalidad, idiomas). La UI usa esto para sincronizar
    el panel con la config del backend.
    """
    _profile, filters_dict = _filters_from_payload(payload)
    return {"filters": _normalize_filters(filters_dict)}


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
async def search_jobs_stream(
    payload: SearchRequest,
    request: Request,
) -> StreamingResponse:
    """
    Misma búsqueda que /search-jobs pero con eventos SSE de progreso por fuente.
    Formato: lines `data: {json}\\n\\n` (eventos progress | source_done | done | error).
    """
    profile_dict, filters_dict = _prepare_search(payload)
    q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    run_id = request.headers.get("x-search-run-id") or uuid.uuid4().hex
    cancel_event = threading.Event()
    with _search_runs_lock:
        _search_runs[run_id] = cancel_event

    def on_progress(evt: dict[str, Any]) -> None:
        q.put(evt)

    def worker() -> None:
        try:
            scrape_result = search_jobs(
                profile_dict,
                None,
                filters_dict,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                q.put({"event": "cancelled", "message": "Búsqueda cancelada."})
                return
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
            if cancel_event.is_set():
                q.put({"event": "cancelled", "message": "Búsqueda cancelada."})
                return
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
            with _search_runs_lock:
                _search_runs.pop(run_id, None)
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def event_gen() -> Any:
        boot = {
            "event": "progress",
            "source": "all",
            "run_id": run_id,
            "message": "Conectado. Fuentes: " + ", ".join(SOURCE_LABELS.values()),
        }
        yield f"data: {json.dumps(boot, ensure_ascii=False)}\n\n"
        while True:
            if await request.is_disconnected():
                cancel_event.set()
                break
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


@app.post("/search-jobs/cancel")
async def cancel_search(payload: CancelSearchRequest) -> dict[str, Any]:
    """Solicita la cancelación cooperativa de una búsqueda en curso."""
    with _search_runs_lock:
        cancel_event = _search_runs.get(payload.run_id)
    if cancel_event is None:
        return {"cancelled": False, "message": "La búsqueda ya había terminado."}
    cancel_event.set()
    return {"cancelled": True, "message": "Cancelación solicitada."}


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
