"""
Utilidades de texto para el análisis local de ofertas.

Base compartida por los módulos de salario, idiomas, geografía y matching:
normalización, extracción de secciones (requisitos / beneficios) y email de
contacto. Sin dependencias de los demás módulos de `analysis`.
"""

from __future__ import annotations

import re
from typing import Any
import unicodedata

REQ_HEADERS = re.compile(
    r"(?im)^(?:requisitos?|requirements?|qué buscamos|que buscamos|"
    r"perfil (?:buscado|requerido)|necesarios?|must have|required|"
    r"conocimientos|skills|habilidades|experiencia requerida)\b.*$"
)
OFFER_HEADERS = re.compile(
    r"(?im)^(?:ofrecemos|we offer|beneficios?|benefits?|qué ofrecemos|"
    r"que ofrecemos|condiciones|compensaci[oó]n|salary|salario|"
    r"package|perks)\b.*$"
)

# PASO 3 · emails en descripción / mailto (cualquier fuente)
_EMAIL_RE = re.compile(
    r"(?i)(?:mailto:)?([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})"
)
_EMAIL_SKIP_DOMAINS = frozenset(
    {
        "example.com",
        "email.com",
        "domain.com",
        "sentry.io",
        "wixpress.com",
        "linkedin.com",
        "getonbrd.com",
        "computrabajo.com",
    }
)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def _split_list_field(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"[\n,;|]+", text)
    return [p.strip() for p in parts if p.strip()]


def extract_section(text: str, header_re: re.Pattern[str], stop_re: re.Pattern[str]) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if header_re.search(line.strip()):
            start = i + 1
            break
    if start is None:
        return ""
    collected: list[str] = []
    for line in lines[start : start + 25]:
        if stop_re.search(line.strip()) and collected:
            break
        if line.strip():
            collected.append(line.strip())
    return "\n".join(collected)[:800]


def extract_requirements(text: str) -> str:
    section = extract_section(text, REQ_HEADERS, OFFER_HEADERS)
    if section:
        return section
    # Fallback: bullets / líneas con "años", "experiencia", tecnologías
    bullets = []
    for line in (text or "").splitlines():
        s = line.strip()
        if re.match(r"^[-•*·]\s+", s) or re.match(r"^\d+[.)]\s+", s):
            bullets.append(re.sub(r"^[-•*·\d.)\s]+", "", s))
        if len(bullets) >= 8:
            break
    return "\n".join(bullets[:8]) if bullets else (text or "")[:400]


def extract_offerings(text: str) -> str:
    section = extract_section(text, OFFER_HEADERS, REQ_HEADERS)
    if section:
        return section
    blob = text or ""
    hits = []
    for m in re.finditer(
        r"(?i)(?:beneficio|salary|salario|remoto|híbrido|hibrido|vacation|"
        r"vacaciones|bono|bonus|obra social|health|seguro).{0,80}",
        blob,
    ):
        hits.append(m.group(0).strip())
        if len(hits) >= 5:
            break
    return " · ".join(hits) if hits else ""


def extract_contact_email(text: str) -> str:
    """
    PASO 3 · busca el primer email usable en el texto de la oferta/post.
    Aplica a todas las fuentes (mismo parser).
    """
    if not text:
        return ""
    for match in _EMAIL_RE.finditer(text):
        email = (match.group(1) or "").strip().lower()
        if not email or "@" not in email:
            continue
        domain = email.rsplit("@", 1)[-1]
        if domain in _EMAIL_SKIP_DOMAINS:
            continue
        if email.endswith((".png", ".jpg", ".gif", ".svg", ".webp")):
            continue
        return email
    return ""
