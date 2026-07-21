"""
Configuración centralizada de la aplicación.

Una sola fuente de verdad para claves, modelos, timeouts, caps y tasas de
cambio. Lee variables de entorno / `.env` mediante pydantic-settings, de modo
que todo el comportamiento es ajustable sin tocar el código.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Carga .env en os.environ una vez, de forma que cualquier lectura directa de
# entorno del proyecto siga funcionando además de la config tipada de abajo.
load_dotenv()

# User-Agent compartido por todos los clientes HTTP / Playwright del proyecto.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Tasas aproximadas → USD. Suficientes para filtrar rangos salariales; no son
# cotizaciones en vivo. Ajustables vía la variable de entorno FX_RATES_JSON.
DEFAULT_FX_TO_USD: dict[str, float] = {
    "usd": 1.0,
    "us$": 1.0,
    "u$s": 1.0,
    "dolar": 1.0,
    "dolares": 1.0,
    "dollar": 1.0,
    "dollars": 1.0,
    "eur": 1.08,
    "€": 1.08,
    "euro": 1.08,
    "euros": 1.08,
    "mxn": 0.055,
    "mx$": 0.055,
    "peso mexicano": 0.055,
    "ars": 0.0011,
    "arg$": 0.0011,
    "$ar": 0.0011,
    "peso argentino": 0.0011,
    "cop": 0.00025,
    "col$": 0.00025,
    "peso colombiano": 0.00025,
    "clp": 0.00105,
    "peso chileno": 0.00105,
    "pen": 0.27,
    "sol": 0.27,
    "soles": 0.27,
    "uyu": 0.024,
    "brl": 0.18,
    "r$": 0.18,
    "real": 0.18,
    "reales": 0.18,
}


class Settings(BaseSettings):
    """Ajustes de la aplicación cargados desde entorno / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Google Gemini -----------------------------------------------------
    google_api_key: str = Field(default="", validation_alias="GOOGLE_API_KEY")
    # Modelo único (compat. con versiones anteriores). Si se define GEMINI_MODELS
    # este campo actúa solo como fallback cuando la lista queda vacía.
    gemini_model: str = Field(
        default="gemini-3.1-flash-lite", validation_alias="GEMINI_MODEL"
    )
    # Lista de modelos separados por coma, en orden de preferencia.
    # Si un modelo devuelve error de cuota (429), se intenta el siguiente.
    gemini_models_raw: str = Field(default="", validation_alias="GEMINI_MODELS")
    ai_request_timeout_sec: int = Field(
        default=60, ge=10, le=600, validation_alias="AI_REQUEST_TIMEOUT_SEC"
    )
    ai_max_cv_chars: int = Field(
        default=12000, ge=1000, validation_alias="AI_MAX_CV_CHARS"
    )

    # --- Búsqueda / scraping ----------------------------------------------
    default_country: str = Field(
        default="mx",
        validation_alias=AliasChoices("DEFAULT_COUNTRY", "COMPUTRABAJO_COUNTRY"),
    )
    user_agent: str = Field(default=DEFAULT_USER_AGENT, validation_alias="USER_AGENT")
    scrape_safety_cap: int = Field(
        default=70, ge=1, le=1000, validation_alias="SCRAPE_SAFETY_CAP"
    )
    per_source_cap: int = Field(
        default=12, ge=1, le=200, validation_alias="PER_SOURCE_CAP"
    )
    http_timeout_sec: float = Field(
        default=25.0, gt=0, validation_alias="HTTP_TIMEOUT_SEC"
    )

    # --- Análisis IA de ofertas (batch, best-effort) ----------------------
    # Activa con AI_MATCH_ENABLED=true en .env. Usa 1 llamada Gemini por grupo
    # de hasta 6 ofertas borderline (30-75% match, ubicación ambigua).
    ai_match_enabled: bool = Field(
        default=False, validation_alias="AI_MATCH_ENABLED"
    )

    # --- Sesiones de navegador --------------------------------------------
    browser_cdp_port: int = Field(
        default=9222, ge=1, le=65535, validation_alias="BROWSER_CDP_PORT"
    )
    browser_cdp_url: str = Field(default="", validation_alias="BROWSER_CDP_URL")
    login_timeout_sec: int = Field(
        default=600, ge=60, le=3600, validation_alias="LOGIN_TIMEOUT_SEC"
    )

    # --- Tasas de cambio (override opcional en JSON) ----------------------
    fx_rates_json: str = Field(default="", validation_alias="FX_RATES_JSON")

    @field_validator("default_country")
    @classmethod
    def _lower_country(cls, value: str) -> str:
        return (value or "mx").strip().lower() or "mx"

    @property
    def cdp_url(self) -> str:
        """URL de depuración remota; se deriva del puerto si no se fija explícita."""
        return self.browser_cdp_url.strip() or f"http://127.0.0.1:{self.browser_cdp_port}"

    @property
    def has_api_key(self) -> bool:
        key = self.google_api_key.strip()
        return bool(key) and key != "tu_api_key_aqui"

    @property
    def model_list(self) -> list[str]:
        """
        Lista ordenada de modelos Gemini a intentar.

        Si GEMINI_MODELS está definido (varios separados por coma) los usa en ese
        orden, haciendo fallback al siguiente cuando hay error de cuota (429).
        Si solo está GEMINI_MODEL, devuelve ese único modelo.
        """
        raw = self.gemini_models_raw.strip()
        if raw:
            models = [m.strip() for m in raw.split(",") if m.strip()]
            if models:
                return models
        single = self.gemini_model.strip()
        return [single] if single else ["gemini-3.1-flash-lite"]

    @property
    def fx_to_usd(self) -> dict[str, float]:
        """Tabla de conversión a USD, combinando defaults + override JSON."""
        rates = dict(DEFAULT_FX_TO_USD)
        raw = self.fx_rates_json.strip()
        if raw:
            try:
                override = json.loads(raw)
                if isinstance(override, dict):
                    rates.update(
                        {str(k).lower(): float(v) for k, v in override.items()}
                    )
            except (ValueError, TypeError) as exc:
                logger.warning("FX_RATES_JSON inválido, se ignora: %s", exc)
        return rates


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la instancia única de settings (cacheada)."""
    return Settings()


def _fx_table() -> dict[str, float]:
    return get_settings().fx_to_usd


# Acceso conveniente para módulos que solo necesitan constantes simples.
def __getattr__(name: str) -> Any:  # pragma: no cover - azúcar de importación
    if name == "USER_AGENT":
        return get_settings().user_agent
    if name == "FX_TO_USD":
        return get_settings().fx_to_usd
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
