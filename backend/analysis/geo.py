"""
Filtros de país / ubicación para ofertas (GetOnBoard y LinkedIn #Hiring).

Tablas de mapeo país→ISO2, términos de apertura global/LATAM y restricciones
regionales explícitas, más los predicados que deciden si una oferta es
compatible con la ubicación del usuario. Sin LLM.
"""

from __future__ import annotations

from typing import Any

from backend.analysis.text import _norm


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
