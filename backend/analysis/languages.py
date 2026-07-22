"""
Detección de idioma de la oferta y filtro por idioma (posting / requerido).

Heurística ligera es | en | pt basada en marcadores; sin LLM.
"""

from __future__ import annotations

import re
from typing import Any

from backend.analysis.text import _norm

ES_MARKERS = [
    "requisitos", "experiencia", "ofrecemos", "empresa", "jornada",
    "contrato", "puesto", "desarrollo", "años", "trabajo", "remoto",
]
EN_MARKERS = [
    "requirements", "experience", "we offer", "company", "full-time",
    "remote", "salary", "responsibilities", "years of", "looking for",
]
PT_MARKERS = [
    "requisitos", "experiência", "oferecemos", "empresa", "vaga",
    "salário", "remoto", "desenvolvimento", "anos de",
]

LANG_REQ_PATTERNS = {
    "es": [
        r"espa[nñ]ol",
        r"spanish",
        r"castellano",
    ],
    "en": [
        r"\bingl[eé]s\b",
        r"\benglish\b",
        r"fluent english",
        r"advanced english",
    ],
    "pt": [
        r"portugu[eé]s",
        r"\bportuguese\b",
    ],
}


def detect_posting_language(text: str) -> str:
    """Heurística simple: es | en | pt | unknown."""
    blob = _norm(text)
    scores = {
        "es": sum(1 for m in ES_MARKERS if m in blob),
        "en": sum(1 for m in EN_MARKERS if m in blob),
        "pt": sum(1 for m in PT_MARKERS if m in blob),
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "unknown"
    return best


def detect_required_languages(text: str) -> list[str]:
    blob = text or ""
    found: list[str] = []
    for lang, patterns in LANG_REQ_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, blob, re.IGNORECASE):
                found.append(lang)
                break
    return found


def passes_language_filters(
    analyzed: dict[str, Any],
    posting_languages: str | list[str] | None,
    required_languages: str | list[str] | None,
) -> bool:
    """
    posting_languages / required_languages: lista de códigos (es, en, pt).
    Vacío o ['any'] = sin filtro.
    """

    def _as_set(value: str | list[str] | None) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            parts = [p.strip().lower() for p in re.split(r"[\n,;|]+", value) if p.strip()]
        else:
            parts = [str(p).strip().lower() for p in value if str(p).strip()]
        parts = [p for p in parts if p and p != "any"]
        return set(parts)

    wanted_posting = _as_set(posting_languages)
    wanted_required = _as_set(required_languages)

    if wanted_posting:
        pl = (analyzed.get("posting_language") or "unknown").lower()
        # Si no se pudo detectar, no excluir
        if pl != "unknown" and pl not in wanted_posting:
            return False

    if wanted_required:
        reqs = [str(x).lower() for x in (analyzed.get("required_languages") or [])]
        # Si la oferta no menciona idiomas, no excluir
        if reqs and not wanted_required.intersection(reqs):
            return False

    return True
