"""
Modelos Pydantic (request bodies) de la API.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class CancelSearchRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=100)


class CoverLetterRequest(BaseModel):
    profile: ProfilePayload
    job: dict[str, Any]


class ApplicationEmailRequest(BaseModel):
    """PASO 4 · borrador de email cuando la oferta trae contact_email."""

    profile: ProfilePayload
    job: dict[str, Any]


class AuthLoginRequest(BaseModel):
    timeout_sec: int = Field(default=600, ge=60, le=1800)
    mode: str = "profile"  # profile | system | playwright
    force_restart: bool = False
    channel: str | None = None  # chrome | msedge


class SetKeyRequest(BaseModel):
    api_key: str
