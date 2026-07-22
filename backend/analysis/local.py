"""
Análisis local de ofertas (sin LLM): match, requisitos, salario USD, idiomas.

=============================================================================
PASO 3 · REVISIÓN DE DETALLES  (todas las fuentes — analyze_job_local)
=============================================================================
  A partir de la descripción cruda del PASO 2, extrae:
    - ubicación, salario, requisitos/skills, idiomas, email de contacto
  Misma función para LinkedIn Jobs, #Hiring, Computrabajo y APIs.

PASO 4 · CLASIFICACIÓN  (match skills aquí; filtros + email IA en main.py)
=============================================================================
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from backend.core.config import get_settings
from backend.core.query_match import extract_location

# Tasas aproximadas → USD (centralizadas en config; override vía FX_RATES_JSON).
FX_TO_USD: dict[str, float] = get_settings().fx_to_usd

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


def _parse_amount(raw: str) -> float | None:
    s = raw.strip()
    s = s.replace("\xa0", " ")
    # 1.200.000,50 → 1200000.50 | 1,200,000.50 → 1200000.50
    if re.search(r",\d{2}$", s) and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{2}$", s) and "," in s:
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        if re.search(r",\d{2}$", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def extract_salary_usd(text: str) -> dict[str, Any]:
    """
    Intenta extraer un rango salarial y convertirlo a USD.
    Devuelve {min_usd, max_usd, raw, currency} o valores None.
    """
    blob = text or ""

    def _currency_from(fragment: str) -> str:
        g0 = _norm(fragment)
        for key in FX_TO_USD:
            if key in g0:
                return key
        if "$" in fragment:
            return "usd"
        return "usd"

    def _apply_k(amin: float, amax: float, raw_match: str) -> tuple[float, float]:
        if re.search(r"\d\s*k\b", raw_match, re.I) or re.search(r"[\d.,]+k", raw_match, re.I):
            if amin < 1000:
                amin *= 1000
            if amax < 1000:
                amax *= 1000
        return amin, amax

    def _to_usd(amin: float, amax: float, currency: str, context: str) -> dict[str, Any]:
        rate = 1.0
        cur = _norm(currency).strip()
        # Match exacto / alias (evitar que "ars" coincida dentro de "dollars")
        aliases = {
            "usd": "usd", "us$": "usd", "u$s": "usd", "$": "usd",
            "dolar": "usd", "dolares": "usd", "dollar": "usd", "dollars": "usd",
            "eur": "eur", "€": "eur", "euro": "eur", "euros": "eur",
            "mxn": "mxn", "mx$": "mxn", "peso mexicano": "mxn",
            "ars": "ars", "arg$": "ars", "$ar": "ars", "peso argentino": "ars",
            "cop": "cop", "col$": "cop", "peso colombiano": "cop",
            "clp": "clp", "peso chileno": "clp",
            "pen": "pen", "sol": "pen", "soles": "pen",
            "uyu": "uyu",
            "brl": "brl", "r$": "brl", "real": "brl", "reales": "brl",
            "peso": "ars", "pesos": "ars",
        }
        code = aliases.get(cur, cur)
        if code in FX_TO_USD:
            rate = FX_TO_USD[code]
        elif "peso" in cur and rate == 1.0:
            nb = _norm(context)
            if "mexic" in nb:
                rate = FX_TO_USD["mxn"]
            elif "colomb" in nb:
                rate = FX_TO_USD["cop"]
            elif "chile" in nb:
                rate = FX_TO_USD["clp"]
            else:
                rate = FX_TO_USD["ars"]
        return {
            "min_usd": round(amin * rate, 2),
            "max_usd": round(amax * rate, 2),
            "currency": code,
        }

    # Rango con moneda al inicio: ARS 2.500.000 - 3.000.000 | USD 3000-4500
    range_leading = re.compile(
        r"(?i)\b(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|uyu|brl|"
        r"pesos?(?:\s+(?:argentinos?|mexicanos?|colombianos?|chilenos?))?|"
        r"soles?|reales?|d[oó]lares?)\b\s*"
        r"([\d.,]+)\s*(?:k)?\s*(?:-|–|—|a|to|/)\s*"
        r"(?:(?:usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|brl|\$)\s*)?"
        r"([\d.,]+)\s*(?:k)?",
    )
    m = range_leading.search(blob)
    if m:
        cur, a, b = m.group(1), m.group(2), m.group(3)
        amin, amax = _parse_amount(a), _parse_amount(b)
        if amin is not None and amax is not None:
            amin, amax = _apply_k(amin, amax, m.group(0))
            out = _to_usd(amin, amax, _norm(cur), blob)
            out["raw"] = m.group(0).strip()
            return out

    # Rango con moneda al final: 3000-4500 USD
    range_trailing = re.compile(
        r"(?i)([\d.,]+)\s*(?:k)?\s*(?:-|–|—|a|to)\s*([\d.,]+)\s*(?:k)?\s*"
        r"(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|uyu|brl|"
        r"pesos?(?:\s+(?:argentinos?|mexicanos?|colombianos?|chilenos?))?|"
        r"soles?|reales?|d[oó]lares?)",
    )
    m = range_trailing.search(blob)
    if m:
        a, b, cur = m.group(1), m.group(2), m.group(3)
        amin, amax = _parse_amount(a), _parse_amount(b)
        if amin is not None and amax is not None:
            amin, amax = _apply_k(amin, amax, m.group(0))
            out = _to_usd(amin, amax, _norm(cur), blob)
            out["raw"] = m.group(0).strip()
            return out

    # $3000 - $4500
    range_dollar = re.compile(
        r"(?i)\$\s*([\d.,]+)\s*(?:k)?\s*(?:-|–|—|a|to)\s*\$?\s*([\d.,]+)\s*(?:k)?",
    )
    m = range_dollar.search(blob)
    if m:
        amin, amax = _parse_amount(m.group(1)), _parse_amount(m.group(2))
        if amin is not None and amax is not None:
            amin, amax = _apply_k(amin, amax, m.group(0))
            cur = _currency_from(m.group(0) + " " + blob[:80])
            out = _to_usd(amin, amax, cur, blob)
            out["raw"] = m.group(0).strip()
            return out

    # Monto único: USD 4000 | 4000 USD | $4000
    single = re.compile(
        r"(?i)(?:\b(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|brl)\b\s*([\d.,]+)\s*(?:k)?|"
        r"([\d.,]+)\s*(?:k)?\s*\b(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|brl|"
        r"pesos?|soles?|reales?|d[oó]lares?)\b|"
        r"\$\s*([\d.,]+)\s*(?:k)?)",
    )
    m = single.search(blob)
    if m:
        if m.group(1) and m.group(2):
            cur, amount = m.group(1), m.group(2)
        elif m.group(3) and m.group(4):
            amount, cur = m.group(3), m.group(4)
        else:
            amount, cur = m.group(5), "usd"
        amin = _parse_amount(amount)
        if amin is not None:
            amin, _ = _apply_k(amin, amin, m.group(0))
            out = _to_usd(amin, amin, _norm(cur), blob)
            out["raw"] = m.group(0).strip()
            return out

    return {"min_usd": None, "max_usd": None, "raw": "", "currency": ""}


def compute_match(profile: dict[str, Any], job: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    """Retorna (percent, matched_skills, missing_skills)."""
    blob = _norm(
        " ".join(
            [
                str(job.get("title") or ""),
                str(job.get("description") or ""),
                str(job.get("requirements") or ""),
            ]
        )
    )
    skills = [_norm(s) for s in (profile.get("skills") or []) if str(s).strip()]
    roles = [_norm(r) for r in (profile.get("roles") or []) if str(r).strip()]

    matched: list[str] = []
    missing: list[str] = []
    for s in skills:
        if len(s) < 2:
            continue
        if s in blob or s.replace(".", "") in blob:
            matched.append(s)
        else:
            missing.append(s)

    role_hit = any(r in blob for r in roles if len(r) > 2)
    skill_score = (len(matched) / len(skills) * 70) if skills else 35
    role_score = 25 if role_hit else 8
    years = float(profile.get("experience_years") or 0)
    exp_score = 5 if years >= 1 else 0
    percent = int(max(5, min(98, round(skill_score + role_score + exp_score))))
    return percent, matched[:12], missing[:8]


def build_advice(matched: list[str], missing: list[str], job: dict[str, Any]) -> str:
    lines: list[str] = []
    if matched:
        lines.append(f"• Destaca en tu CV/perfil: {', '.join(matched[:6])}.")
    if missing:
        lines.append(
            f"• Cubre o menciona honestamente gaps: {', '.join(missing[:5])}."
        )
    else:
        lines.append("• Buen solapamiento de skills; personaliza ejemplos recientes.")
    title = job.get("title") or "el puesto"
    lines.append(f"• Adapta la postulación al título «{title}» y a la empresa.")
    if job.get("url"):
        lines.append("• Revisa la oferta original antes de enviar (requisitos pueden cambiar).")
    if job.get("contact_email"):
        lines.append(
            f"• Email de contacto ({job['contact_email']}): "
            "generá asunto + cuerpo con IA y recordá adjuntar el CV."
        )
    return "\n".join(lines)


def salary_in_range(
    salary: dict[str, Any],
    min_usd: float | None,
    max_usd: float | None,
) -> bool:
    """Si no hay salario en la oferta, no se excluye. Si hay filtro y hay salario, se valida solape."""
    if min_usd is None and max_usd is None:
        return True
    smin = salary.get("min_usd")
    smax = salary.get("max_usd")
    if smin is None and smax is None:
        return True  # sin dato salarial → no filtrar
    smin = float(smin if smin is not None else smax)
    smax = float(smax if smax is not None else smin)
    fmin = float(min_usd if min_usd is not None else 0)
    fmax = float(max_usd if max_usd is not None else 10**12)
    return smax >= fmin and smin <= fmax


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


def analyze_job_local(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """
    PASO 3 · REVISIÓN + match de skills local (PASO 4 parcial).

    Misma función para LinkedIn, #Hiring, Computrabajo y APIs:
    ubicación, salario, requisitos, email de contacto, match_percent.
    Filtros de país/idioma y borrador de email con IA → main._analyze_raw_jobs.
    """
    description = str(job.get("description") or "")
    requirements = extract_requirements(description)
    offerings = extract_offerings(description)
    salary = extract_salary_usd(description + " " + offerings)
    posting_lang = detect_posting_language(description or str(job.get("title") or ""))
    required_langs = detect_required_languages(description)
    contact_email = extract_contact_email(
        " ".join(
            [
                description,
                str(job.get("title") or ""),
                str(job.get("company") or ""),
                requirements,
                offerings,
            ]
        )
    )

    enriched = {**job, "requirements": requirements, "contact_email": contact_email}
    match_percent, matched, missing = compute_match(profile, enriched)
    advice = build_advice(matched, missing, enriched)

    salary_label = ""
    if salary.get("min_usd") is not None:
        if salary["min_usd"] == salary.get("max_usd"):
            salary_label = f"≈ USD {salary['min_usd']:,.0f}"
        else:
            salary_label = f"≈ USD {salary['min_usd']:,.0f}–{salary['max_usd']:,.0f}"
        if salary.get("raw"):
            salary_label += f" ({salary['raw']})"

    return {
        "title": job.get("title", "Sin título"),
        "company": job.get("company", "Empresa no indicada"),
        "url": job.get("url", ""),
        "source": job.get("source", ""),
        "location": job.get("location") or extract_location(job) or "",
        "published_at": job.get("published_at"),
        "requirements": requirements,
        "offerings": offerings,
        "match_percent": match_percent,
        "advice": advice,
        "cover_letter": "",
        "application_email": None,
        "contact_email": contact_email,
        "salary_usd": salary_label,
        "salary_min_usd": salary.get("min_usd"),
        "salary_max_usd": salary.get("max_usd"),
        "posting_language": posting_lang,
        "required_languages": required_langs,
        "matched_skills": matched,
        "missing_skills": missing,
    }


# ---------------------------------------------------------------------------
# Filtro de país para ofertas de GetOnBoard
# ---------------------------------------------------------------------------

# Mapeo nombre de país (API GOB) → ISO2. Insensible a mayúsculas/acentos.
_GOB_NAME_TO_ISO: dict[str, str] = {
    "argentina": "ar",
    "chile": "cl",
    "colombia": "co",
    "mexico": "mx",
    "méxico": "mx",
    "mejico": "mx",
    "peru": "pe",
    "perú": "pe",
    "uruguay": "uy",
    "brazil": "br",
    "brasil": "br",
    "ecuador": "ec",
    "venezuela": "ve",
    "costa rica": "cr",
    "panama": "pa",
    "panamá": "pa",
    "bolivia": "bo",
    "paraguay": "py",
    "dominican republic": "do",
    "república dominicana": "do",
    "republica dominicana": "do",
    "rep. dominicana": "do",
    "honduras": "hn",
    "guatemala": "gt",
    "el salvador": "sv",
    "nicaragua": "ni",
    "cuba": "cu",
    "puerto rico": "pr",
    "spain": "es",
    "españa": "es",
    "united states": "us",
    "usa": "us",
    "eeuu": "us",
}

# Términos que indican "abierto a todo el mundo" → no filtrar por país.
_GOB_OPEN_TERMS: frozenset[str] = frozenset(
    {
        "worldwide",
        "everywhere",
        "anywhere",
        "global",
        "latam",
        "latin america",
        "latinoamerica",
        "latinoamérica",
        "sudamerica",
        "sudamérica",
        "south america",
        "america latina",
    }
)


def _gob_country_name_to_iso(raw: str) -> str | None:
    """Intenta convertir un nombre de país (como viene de GOB) a código ISO2."""
    name = _norm(raw).strip()
    if name in _GOB_NAME_TO_ISO:
        return _GOB_NAME_TO_ISO[name]
    # Coincidencia parcial: "great britain" → "gb", etc.
    for key, code in _GOB_NAME_TO_ISO.items():
        if key in name or name in key:
            return code
    return None


def passes_gob_country_filter(
    job: dict[str, Any],
    filter_countries: list[str],
    profile_country: str = "",
) -> bool:
    """
    Para ofertas de GetOnBoard: verifica si el candidato puede postular desde su país.

    Reglas:
    - Si el job no trae `_countries_raw` → no filtrar (abierto o desconocido).
    - Si algún país en la lista es un término "global/worldwide" → no filtrar.
    - Si la lista contiene el país del usuario (ISO2) → incluir.
    - Si la lista no incluye el país del usuario → excluir.
    - Si no se puede inferir ningún código ISO2 de la lista → no filtrar (evitar falsos negativos).
    """
    countries_raw: list[str] = job.get("_countries_raw") or []
    if not countries_raw:
        return True  # Sin datos → no filtrar

    # Detectar términos de "apertura global"
    for raw in countries_raw:
        low = _norm(raw)
        if any(term in low for term in _GOB_OPEN_TERMS):
            return True

    # País(es) que quiere el usuario
    wanted: set[str] = set()
    if filter_countries:
        wanted = {c.lower().strip() for c in filter_countries if c.strip()}
    elif profile_country:
        wanted = {profile_country.lower().strip()}
    if not wanted:
        return True  # Sin preferencia → no filtrar

    # Convertir lista del job a ISO2
    job_iso: set[str] = set()
    for raw in countries_raw:
        iso = _gob_country_name_to_iso(raw)
        if iso:
            job_iso.add(iso)

    if not job_iso:
        return True  # No se pudo parsear → no filtrar (evitar falso negativo)

    return bool(wanted & job_iso)


# --- Validación de ubicación para posts LinkedIn #Hiring ---------------------

# Frases que abren la búsqueda a todo el mundo / LATAM → siempre permitidas.
_HIRING_OPEN_TERMS: tuple[str, ...] = (
    "worldwide",
    "world wide",
    "anywhere in the world",
    "fully remote",
    "remote (global)",
    "global remote",
    "work from anywhere",
    "open to latam",
    "latam",
    "latin america",
    "latinoam",
    "sudamerica",
    "south america",
    "remoto global",
    "remoto latam",
    "remote latam",
    "desde cualquier lugar",
    "oportunidad internacional",
    "international opportunity",
    "international role",
    "100% remoto",
    "100% remote",
)

# Restricciones por región: si aparece la frase, la oferta se limita a esa
# región. Cada región mapea a los códigos ISO2 que SÍ pueden postular.
_HIRING_REGION_RESTRICTIONS: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "us": (
        frozenset({"us"}),
        (
            "us based",
            "u.s. based",
            "us-based",
            "usa based",
            "based in the us",
            "based in the u.s.",
            "based in the united states",
            "located in the us",
            "located in the united states",
            "must be located in the united states",
            "must reside in the us",
            "must reside in the united states",
            "us only",
            "usa only",
            "us residents only",
            "us citizens only",
            "us citizen or green card",
            "authorized to work in the us",
            "authorized to work in the united states",
            "work authorization in the united states",
            "must be authorized to work in the us",
            "green card holder",
            "eligible to work in the united states",
        ),
    ),
    "ca": (
        frozenset({"ca"}),
        (
            "canada only",
            "based in canada",
            "must be located in canada",
            "canadian residents only",
        ),
    ),
    "gb": (
        frozenset({"gb", "uk"}),
        (
            "uk based",
            "uk-based",
            "based in the uk",
            "united kingdom only",
            "uk only",
            "must be located in the uk",
        ),
    ),
    "eu": (
        frozenset({"es", "de", "fr", "it", "nl", "pt", "pl", "ie", "be", "at", "se", "dk", "fi"}),
        (
            "eu only",
            "eu-based",
            "based in the eu",
            "european union only",
            "must be based in europe",
            "europe only",
            "eu residents only",
            "must be located in europe",
        ),
    ),
    "in": (
        frozenset({"in"}),
        (
            "india only",
            "based in india",
            "must be located in india",
            "across india",
            "openings across india",
            "openings in india",
            "hiring in india",
            "jobs in india",
            "roles in india",
            "pan india",
            "pan-india",
            "india openings",
            "bangalore",
            "bengaluru",
            "hyderabad",
            "pune",
            "chennai",
            "noida",
            "gurgaon",
            "gurugram",
            "mumbai",
            "delhi ncr",
            "delhi,",
            "kolkata",
            "ahmedabad",
            "jaipur",
            "coimbatore",
            "trivandrum",
            "thiruvananthapuram",
        ),
    ),
    "ph": (
        frozenset({"ph"}),
        (
            "philippines only",
            "based in the philippines",
            "manila",
            "cebu",
            "makati",
        ),
    ),
    "pk": (
        frozenset({"pk"}),
        (
            "pakistan only",
            "based in pakistan",
            "karachi",
            "lahore",
            "islamabad",
        ),
    ),
    # Países LATAM: "contratando en México" ≠ abierto a Argentina.
    # Frases en ASCII (se comparan vía _norm).
    "mx": (
        frozenset({"mx"}),
        (
            "en mexico",
            "mexico only",
            "based in mexico",
            "located in mexico",
            "contratando en mexico",
            "cdmx",
            "ciudad de mexico",
            "mexico city",
            "roles in mexico",
            "position in mexico",
            "job in mexico",
            "vacante en mexico",
            "para mexico",
        ),
    ),
    "ar": (
        frozenset({"ar"}),
        (
            "en argentina",
            "argentina only",
            "based in argentina",
            "located in argentina",
            "contratando en argentina",
            "caba",
            "buenos aires only",
            "vacante en argentina",
            "para argentina",
        ),
    ),
    "co": (
        frozenset({"co"}),
        (
            "en colombia",
            "colombia only",
            "based in colombia",
            "contratando en colombia",
            "bogota",
            "vacante en colombia",
            "para colombia",
        ),
    ),
    "cl": (
        frozenset({"cl"}),
        (
            "en chile",
            "chile only",
            "based in chile",
            "contratando en chile",
            "santiago de chile",
            "vacante en chile",
            "para chile",
        ),
    ),
    "pe": (
        frozenset({"pe"}),
        (
            "en peru",
            "peru only",
            "based in peru",
            "contratando en peru",
            "lima, peru",
            "vacante en peru",
            "para peru",
        ),
    ),
    "br": (
        frozenset({"br"}),
        (
            "en brasil",
            "en brazil",
            "brazil only",
            "brasil only",
            "based in brazil",
            "based in brasil",
            "contratando en brasil",
            "sao paulo",
        ),
    ),
}


def _hiring_user_iso(user_country: str, user_locations: list[str] | None) -> set[str]:
    """Deriva los códigos ISO2 del usuario desde su país y ubicaciones de texto."""
    iso: set[str] = set()
    uc = (user_country or "").strip().lower()
    if uc and len(uc) == 2:
        iso.add(uc)
    for loc in user_locations or []:
        code = _gob_country_name_to_iso(loc)
        if code:
            iso.add(code)
    return iso


def _user_wants_latam_scope(user_locations: list[str] | None) -> bool:
    """True si el usuario filtró explícitamente por LATAM / remoto LATAM."""
    blob = _norm(" ".join(user_locations or []))
    return any(
        term in blob
        for term in (
            "latam",
            "latin america",
            "latinoam",
            "sudamerica",
            "south america",
            "remoto latam",
            "remote latam",
        )
    )


def linkedin_hiring_location_ok(
    text: str,
    user_country: str = "",
    user_locations: list[str] | None = None,
) -> bool | None:
    """
    Evalúa si un post #Hiring es compatible con la ubicación del usuario.

    Devuelve:
      True  → abierto / compatible (LATAM, global, o menciona el país del usuario)
      False → claramente restringido a otra región (p. ej. Bangalore / Across India)
      None  → ambiguo (conviene apoyarse en la IA)

    Si el usuario filtró por LATAM y el post no menciona LATAM/global/su país,
    se rechaza (False) en lugar de dejarlo pasar ambiguo.
    """
    if not text:
        return None
    low = _norm(text)
    user_iso = _hiring_user_iso(user_country, user_locations)
    wants_latam = _user_wants_latam_scope(user_locations)

    # 1) Apertura global / LATAM → permitido.
    if any(term in low for term in _HIRING_OPEN_TERMS):
        return True

    # 2) Restricciones regionales explícitas.
    restricted_hit = False
    for _, (allowed_iso, phrases) in _HIRING_REGION_RESTRICTIONS.items():
        if any(_norm(p) in low for p in phrases):
            restricted_hit = True
            # Si el usuario pertenece a la región permitida → OK.
            if user_iso & allowed_iso:
                return True
    if restricted_hit:
        # Se detectó una restricción y el usuario no encaja en ninguna.
        return False

    # 3) Menciona explícitamente el país del usuario → permitido.
    if user_iso:
        for iso in user_iso:
            for name, code in _GOB_NAME_TO_ISO.items():
                if code == iso and name in low:
                    return True

    # 4) Filtro LATAM activo y sin señal geográfica útil → rechazar.
    if wants_latam:
        return False

    # 5) Sin señales claras → ambiguo.
    return None


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
