"""Constantes y tipos compartidos del pipeline de scraping."""

from __future__ import annotations

from typing import Any, Callable

from playwright.sync_api import Browser, BrowserContext

from backend.core.config import get_settings
from backend.scraping.sources.api import SOURCE_SCRAPERS

SAFETY_CAP = get_settings().scrape_safety_cap

ProgressCb = Callable[[dict[str, Any]], None]
BrowserTarget = Browser | BrowserContext

SOURCE_LABELS = {
    "computrabajo": "Computrabajo",
    "linkedin": "LinkedIn Jobs",
    "linkedin_hiring": "LinkedIn #Hiring",
    "getonboard": "GetOnBoard",
    "remotive": "Remotive",
    "remoteok": "RemoteOK",
    "jobicy": "Jobicy",
}

SOURCE_LATAM_RANK: dict[str, int] = {
    "linkedin": 0,
    "getonboard": 1,
    "computrabajo": 2,
    "linkedin_hiring": 3,
    "remotive": 4,
    "jobicy": 5,
    "remoteok": 6,
}

PLAYWRIGHT_SOURCES = ("computrabajo", "linkedin", "linkedin_hiring")
API_SOURCES = tuple(SOURCE_SCRAPERS.keys())
ALL_SOURCES = PLAYWRIGHT_SOURCES + API_SOURCES

USER_AGENT = get_settings().user_agent

COUNTRY_META: dict[str, dict[str, str]] = {
    "mx": {"name": "Mexico", "ct": "mx", "geo": "103323778"},
    "co": {"name": "Colombia", "ct": "co", "geo": "100876405"},
    "ar": {"name": "Argentina", "ct": "ar", "geo": "100446943"},
    "pe": {"name": "Peru", "ct": "pe", "geo": "102890719"},
    "cl": {"name": "Chile", "ct": "cl", "geo": "104621616"},
    "ec": {"name": "Ecuador", "ct": "ec", "geo": "106373116"},
    "uy": {"name": "Uruguay", "ct": "uy", "geo": "100867946"},
    "ve": {"name": "Venezuela", "ct": "ve", "geo": "101490751"},
    "cr": {"name": "Costa Rica", "ct": "cr", "geo": "101174742"},
    "pa": {"name": "Panama", "ct": "pa", "geo": "100808673"},
    "gt": {"name": "Guatemala", "ct": "gt", "geo": "100247235"},
    "bo": {"name": "Bolivia", "ct": "bo", "geo": "104383590"},
    "py": {"name": "Paraguay", "ct": "py", "geo": "104065273"},
    "do": {"name": "Dominican Republic", "ct": "do", "geo": "109705310"},
    "hn": {"name": "Honduras", "ct": "hn", "geo": "101733784"},
    "sv": {"name": "El Salvador", "ct": "sv", "geo": "106693272"},
    "ni": {"name": "Nicaragua", "ct": "ni", "geo": "105531867"},
    "cu": {"name": "Cuba", "ct": "cu", "geo": "106670759"},
    "pr": {"name": "Puerto Rico", "ct": "pr", "geo": "105556783"},
}

LINKEDIN_F_TPR = {
    "24h": "r86400",
    "week": "r604800",
    "month": "r2592000",
}

LINKEDIN_F_E = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid": "4",
    "senior": "4",
    "director": "5",
}

LINKEDIN_F_WT = {
    "onsite": "1",
    "remote": "2",
    "hybrid": "3",
}

WORK_MODE_KEYWORDS = {
    "remote": ["remoto", "remote", "teletrabajo", "home office", "work from home"],
    "hybrid": ["híbrido", "hibrido", "hybrid"],
    "onsite": ["presencial", "on-site", "onsite", "oficina"],
}

EXPERIENCE_KEYWORDS = {
    "internship": ["internship", "becario", "pasante", "prácticas", "practicas"],
    "entry": ["junior", "entry", "jr", "trainee", "sin experiencia"],
    "associate": ["semi", "ssr", "mid-level", "intermedio"],
    "mid": ["semi", "ssr", "mid", "intermedio", "pleno"],
    "senior": ["senior", "sr", "lead", "principal"],
    "director": ["director", "head of", "gerente"],
}

DISCARD_REASON_LABELS = {
    "query": "texto de búsqueda",
    "date": "antigüedad",
    "work_mode": "modalidad",
    "experience": "experiencia",
    "country": "país",
    "language": "idioma",
    "salary": "salario",
    "duplicate": "duplicada",
    "invalid_link": "enlace no específico",
}

_BOARD_SCOPED_SOURCES = frozenset({"linkedin", "linkedin_hiring"})
