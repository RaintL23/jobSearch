"""
Relevancia de ofertas respecto a los textos de búsqueda del usuario.

Los boards (sobre todo LinkedIn) devuelven resultados laxos; este módulo
exige que título/descripción encajen con al menos uno de los textos.
"""

from __future__ import annotations

import re
from typing import Any

_SYNONYMS: tuple[tuple[str, str], ...] = (
    (".net", "dotnet"),
    ("c#", "csharp"),
    ("full stack", "fullstack"),
    ("full-stack", "fullstack"),
    ("node.js", "nodejs"),
    ("asp.net", "aspnet"),
)

_STOP = frozenset(
    {
        "and",
        "or",
        "the",
        "a",
        "an",
        "for",
        "with",
        "de",
        "del",
        "la",
        "el",
        "en",
        "y",
        "o",
        "con",
        "sr",
        "jr",
    }
)

# Tokens demasiado genéricos para validar solos el título
_WEAK_ALONE = frozenset(
    {
        "engineer",
        "developer",
        "software",
        "analyst",
        "analista",
        "especialista",
        "senior",
        "junior",
        "lead",
        "remote",
        "remoto",
    }
)


def normalize_query_text(text: str) -> str:
    low = str(text or "").lower().strip()
    for src, dst in _SYNONYMS:
        low = low.replace(src, dst)
    return low


def query_tokens(query: str) -> list[str]:
    norm = normalize_query_text(query)
    tokens = re.findall(r"[a-z0-9#.+]+", norm)
    return [t for t in tokens if len(t) >= 2 and t not in _STOP]


def extract_location(job: dict[str, Any]) -> str:
    """Ubicación explícita del job o inferida de la descripción."""
    loc = str(job.get("location") or "").strip()
    if loc and loc.lower() not in ("n/d", "nd", "—", "-"):
        return loc[:120]
    desc = str(job.get("description") or "")
    m = re.search(
        r"(?i)ubicaci[oó]n\s*:\s*([^\n.|;]{2,80})",
        desc,
    )
    if m:
        return m.group(1).strip()[:120]
    m = re.search(r"(?i)location\s*:\s*([^\n.|;]{2,80})", desc)
    if m:
        return m.group(1).strip()[:120]
    return ""


def matches_search_queries(job: dict[str, Any], queries: list[str] | None) -> bool:
    """
    True si la oferta encaja con al menos un texto de búsqueda.

    Criterio por texto:
    - frase completa (normalizada) en título o descripción, o
    - ≥2 tokens del texto en el título, o
    - 1 token fuerte (no genérico) en el título, o
    - ≥2 tokens en título+descripción con al menos 1 en el título.
    """
    if not queries:
        return True

    title = normalize_query_text(str(job.get("title") or ""))
    blob = normalize_query_text(
        " ".join(
            [
                str(job.get("title") or ""),
                str(job.get("company") or ""),
                str(job.get("description") or "")[:2500],
            ]
        )
    )
    if not title and not blob:
        return False

    for raw in queries:
        q = str(raw or "").strip()
        if not q:
            continue
        norm = normalize_query_text(q)
        if len(norm) >= 4 and (norm in title or norm in blob):
            return True

        tokens = query_tokens(q)
        if not tokens:
            continue

        title_hits = [t for t in tokens if t in title]
        blob_hits = [t for t in tokens if t in blob]
        strong_in_title = [t for t in title_hits if t not in _WEAK_ALONE]

        if len(title_hits) >= 2:
            return True
        if strong_in_title:
            return True
        if len(tokens) == 1 and tokens[0] in title:
            return True
        if title_hits and len(blob_hits) >= 2:
            return True

    return False
