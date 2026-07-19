"""
Motor de IA con Google Gemini (SDK moderno `google-genai`).

Extracción de perfil desde el CV y generación de cover letters. El cliente se
crea una sola vez (cacheado) y se reutiliza en cada request.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from google import genai
from google.genai import types

from backend.config import get_settings
from backend.runtime_key import get_runtime_key, has_runtime_key
from backend.utils import parse_llm_json

logger = logging.getLogger(__name__)

# Compat: se mantiene por si algo externo lo importaba.
DEFAULT_MODEL = "gemini-3.1-flash-lite"


class AIEngineError(Exception):
    """Error en la comunicación o parseo con el LLM."""


@lru_cache(maxsize=8)
def _client(api_key: str, timeout_ms: int) -> genai.Client:
    """Crea (y cachea) el cliente Gemini. El timeout va en milisegundos."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


def _active_client_and_models() -> tuple[genai.Client, list[str]]:
    """
    Devuelve el cliente Gemini y la lista ordenada de modelos a intentar.

    Prioridad de API key: 1) .env  2) runtime (provista desde el frontend).
    """
    settings = get_settings()

    if settings.has_api_key:
        api_key = settings.google_api_key.strip()
    elif has_runtime_key():
        api_key = get_runtime_key()
    else:
        raise AIEngineError(
            "Falta GOOGLE_API_KEY. Configúrala en el archivo .env o "
            "ingrésala desde la interfaz web."
        )

    client = _client(api_key, settings.ai_request_timeout_sec * 1000)
    return client, settings.model_list


def _is_quota_error(exc: Exception) -> bool:
    """Devuelve True si el error indica cuota agotada o rate-limit (HTTP 429)."""
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in ("quota", "resource_exhausted", "rate_limit", "429", "too many requests")
    )


def _is_model_unavailable_error(exc: Exception) -> bool:
    """True si el modelo no existe / no está disponible para esta cuenta (404)."""
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "404",
            "not_found",
            "not found",
            "no longer available",
            "is not found",
        )
    )


def _should_try_next_model(exc: Exception) -> bool:
    """True si el error justifica intentar el siguiente modelo de la lista."""
    return _is_quota_error(exc) or _is_model_unavailable_error(exc)


def _generate_text(prompt: str, *, json_mode: bool = False) -> str:
    """
    Llama a Gemini y devuelve texto crudo.

    Itera sobre la lista de modelos configurados: si uno falla por cuota (429) o
    porque no está disponible (404), pasa automáticamente al siguiente. Si todos
    fallan por esos motivos lanza AIEngineError con el resumen de intentos.
    """
    client, models = _active_client_and_models()
    gen_config = types.GenerateContentConfig(
        temperature=0.4,
        response_mime_type="application/json" if json_mode else None,
    )

    last_exc: Exception | None = None
    for idx, model in enumerate(models):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=gen_config,
            )
            if idx > 0:
                logger.info("Gemini: respondió el modelo de respaldo '%s'", model)
            return (response.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            if _should_try_next_model(exc) and idx < len(models) - 1:
                reason = (
                    "no disponible"
                    if _is_model_unavailable_error(exc)
                    else "cuota agotada"
                )
                logger.warning(
                    "Gemini: %s en '%s' → probando '%s'",
                    reason,
                    model,
                    models[idx + 1],
                )
                last_exc = exc
                continue
            # Error no recuperable, o último modelo: propaga directamente.
            raise

    raise AIEngineError(
        f"Ningún modelo Gemini configurado respondió "
        f"({', '.join(models)}). Último error: {last_exc}"
    ) from last_exc


def _generate_json(prompt: str, *, retry: bool = True) -> dict[str, Any]:
    """
    Llama a Gemini pidiendo JSON nativo (response_mime_type) y lo parsea.
    Un único reintento reforzando la instrucción si el parseo falla.
    """
    try:
        raw = _generate_text(prompt, json_mode=True)
        if not raw:
            raise AIEngineError("El modelo devolvió una respuesta vacía.")
        return parse_llm_json(raw)
    except AIEngineError:
        raise
    except ValueError as exc:
        if not retry:
            raise AIEngineError(str(exc)) from exc
        logger.warning("JSON inválido del LLM; reintentando una vez: %s", exc)
        retry_prompt = (
            prompt
            + "\n\nIMPORTANTE: Responde ÚNICAMENTE con un objeto JSON válido, "
            "sin markdown ni texto adicional."
        )
        return _generate_json(retry_prompt, retry=False)
    except Exception as exc:  # noqa: BLE001
        raise AIEngineError(f"Error al llamar a Gemini: {exc}") from exc


def _generate_json_array(prompt: str) -> list[Any]:
    """
    Variante de _generate_json para respuestas que son un array JSON.
    No reintenta; devuelve [] ante cualquier fallo (uso best-effort).
    """
    try:
        raw = _generate_text(prompt, json_mode=True)
        if not raw:
            return []
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("_generate_json_array: parseo fallido: %s", exc)
        return []


def extract_profile_from_cv(cv_text: str) -> dict[str, Any]:
    """
    Extrae un perfil estructurado del texto del CV.

    Campos esperados:
      name, roles, skills, experience_years, summary, location, country
    """
    prompt = f"""Eres un experto en reclutamiento para Latinoamérica.
Analiza el siguiente CV y extrae un perfil estructurado.

Devuelve ÚNICAMENTE un JSON válido con esta forma exacta:
{{
  "name": "nombre completo o 'Candidato' si no aparece",
  "roles": ["rol o título objetivo 1", "rol 2"],
  "skills": ["habilidad1", "habilidad2"],
  "experience_years": 0,
  "summary": "resumen breve del perfil en 2-3 oraciones",
  "location": "ciudad/país si se menciona, o ''",
  "country": "código ISO2 del país (mx, co, ar, pe, cl, etc.). Usa mx si no está claro"
}}

Reglas:
- roles: 1 a 4 títulos de puesto relevantes para búsqueda de empleo.
- skills: habilidades técnicas y blandas más importantes (máx. 20).
- experience_years: número entero estimado de años de experiencia.
- No inventes experiencia que no esté en el CV; puedes inferir roles objetivo razonables.
- Responde solo JSON, sin markdown.

--- CV ---
{cv_text[: get_settings().ai_max_cv_chars]}
"""
    profile = _generate_json(prompt)

    default_country = get_settings().default_country

    # Normalización mínima
    profile.setdefault("name", "Candidato")
    profile.setdefault("roles", [])
    profile.setdefault("skills", [])
    profile.setdefault("experience_years", 0)
    profile.setdefault("summary", "")
    profile.setdefault("location", "")
    profile.setdefault("country", default_country)

    if isinstance(profile.get("roles"), str):
        profile["roles"] = [profile["roles"]]
    if isinstance(profile.get("skills"), str):
        profile["skills"] = [profile["skills"]]
    if not profile.get("country"):
        profile["country"] = default_country

    return profile


def generate_cover_letter(profile: dict[str, Any], job: dict[str, Any]) -> str:
    """
    Genera una cover letter bajo demanda (única llamada IA por oferta, salvo el CV).
    """
    prompt = f"""Eres un coach de carrera para Latinoamérica.
Redacta una carta de presentación corta (180-280 palabras) en español,
profesional y personalizada para esta oferta. No inventes empresas previas
que no estén en el perfil. Responde ÚNICAMENTE con el texto de la carta,
sin JSON ni markdown.

--- PERFIL ---
{profile}

--- OFERTA ---
Título: {job.get("title", "")}
Empresa: {job.get("company", "")}
Requisitos: {str(job.get("requirements", ""))[:1500]}
Descripción: {str(job.get("description", job.get("offerings", "")))[:3000]}
"""
    try:
        text = _generate_text(prompt)
        if not text:
            raise AIEngineError("El modelo devolvió una cover letter vacía.")
        return text
    except AIEngineError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AIEngineError(f"Error al generar cover letter: {exc}") from exc


def batch_analyze_relevance(
    profile: dict[str, Any],
    jobs: list[dict[str, Any]],
    user_country: str = "",
) -> list[dict[str, Any]]:
    """
    Analiza hasta MAX_BATCH ofertas en UNA sola llamada a Gemini.

    Diseñado para ser token-eficiente: el prompt es compacto y la respuesta
    mínima. Solo se llama para ofertas con ubicación ambigua (sin `_countries_raw`)
    y match borderline (30-75 %).

    Devuelve lista de dicts:
      {"idx": 1, "country_ok": true/false/null, "match_delta": -15..+10, "reason": "..."}

    Errores se silencian (best-effort): cualquier fallo devuelve [].
    """
    MAX_BATCH = 6
    batch = jobs[:MAX_BATCH]
    if not batch:
        return []

    country_label = user_country or str(profile.get("country") or "")
    roles_str = ", ".join(str(r) for r in (profile.get("roles") or [])[:3])
    skills_str = ", ".join(str(s) for s in (profile.get("skills") or [])[:10])
    exp = int(profile.get("experience_years") or 0)

    jobs_block = ""
    for i, job in enumerate(batch, 1):
        reqs_snippet = str(job.get("requirements") or "")[:200]
        # Descripción más amplia: en posts #Hiring la pista de ubicación
        # (US based, LATAM, remoto, etc.) puede aparecer en cualquier parte.
        desc_snippet = str(job.get("description") or "")[:500]
        jobs_block += (
            f"\n[{i}] {job.get('title', '')} | {job.get('company', '')} | "
            f"Loc: {job.get('location', '?')} | "
            f"Req: {reqs_snippet} | Desc: {desc_snippet}\n"
        )

    prompt = (
        f"Asistente de empleo LATAM. Candidato en {country_label}, roles: {roles_str}, "
        f"skills: {skills_str}, {exp} años exp.\n\n"
        f"Para cada oferta evalúa:\n"
        f"- country_ok: ¿puede postular desde {country_label}? (true/false/null si no está claro)\n"
        f"- match_delta: ajuste entero al score de match [-15, +10] según fit real con el perfil\n"
        f"- reason: motivo en 10 palabras máx\n\n"
        f"OFERTAS:{jobs_block}\n"
        f"Responde SOLO con un array JSON (sin markdown):\n"
        f'[{{"idx":1,"country_ok":true,"match_delta":5,"reason":"..."}},...]'
    )

    logger.info(
        "batch_analyze_relevance: %d oferta(s) → 1 llamada Gemini", len(batch)
    )
    return _generate_json_array(prompt)


# Mantener alias por compatibilidad si algo externo lo importaba
def analyze_job_match(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Deprecated: usar job_analyzer.analyze_job_local (sin tokens)."""
    from backend.job_analyzer import analyze_job_local

    return analyze_job_local(profile, job)
