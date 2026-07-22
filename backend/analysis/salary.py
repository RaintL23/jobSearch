"""
Extracción y normalización de salarios a USD (sin LLM).

Parseo de rangos/montos en varias monedas LATAM + conversión aproximada a USD
(tabla en `core.config`, override vía FX_RATES_JSON) y filtro por rango.
"""

from __future__ import annotations

import re
from typing import Any

from backend.analysis.text import _norm
from backend.core.config import get_settings

# Tasas aproximadas → USD (centralizadas en config; override vía FX_RATES_JSON).
FX_TO_USD: dict[str, float] = get_settings().fx_to_usd


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


# Patrones de salario compilados una sola vez (extract_salary_usd corre por
# oferta, hasta SCRAPE_SAFETY_CAP veces por búsqueda).
_SAL_RANGE_LEADING = re.compile(  # ARS 2.500.000 - 3.000.000 | USD 3000-4500
    r"(?i)\b(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|uyu|brl|"
    r"pesos?(?:\s+(?:argentinos?|mexicanos?|colombianos?|chilenos?))?|"
    r"soles?|reales?|d[oó]lares?)\b\s*"
    r"([\d.,]+)\s*(?:k)?\s*(?:-|–|—|a|to|/)\s*"
    r"(?:(?:usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|brl|\$)\s*)?"
    r"([\d.,]+)\s*(?:k)?"
)
_SAL_RANGE_TRAILING = re.compile(  # 3000-4500 USD
    r"(?i)([\d.,]+)\s*(?:k)?\s*(?:-|–|—|a|to)\s*([\d.,]+)\s*(?:k)?\s*"
    r"(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|uyu|brl|"
    r"pesos?(?:\s+(?:argentinos?|mexicanos?|colombianos?|chilenos?))?|"
    r"soles?|reales?|d[oó]lares?)"
)
_SAL_RANGE_DOLLAR = re.compile(  # $3000 - $4500
    r"(?i)\$\s*([\d.,]+)\s*(?:k)?\s*(?:-|–|—|a|to)\s*\$?\s*([\d.,]+)\s*(?:k)?"
)
_SAL_SINGLE = re.compile(  # USD 4000 | 4000 USD | $4000
    r"(?i)(?:\b(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|brl)\b\s*([\d.,]+)\s*(?:k)?|"
    r"([\d.,]+)\s*(?:k)?\s*\b(usd|us\$|u\$s|eur|€|mxn|ars|cop|clp|pen|brl|"
    r"pesos?|soles?|reales?|d[oó]lares?)\b|"
    r"\$\s*([\d.,]+)\s*(?:k)?)"
)


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
    m = _SAL_RANGE_LEADING.search(blob)
    if m:
        cur, a, b = m.group(1), m.group(2), m.group(3)
        amin, amax = _parse_amount(a), _parse_amount(b)
        if amin is not None and amax is not None:
            amin, amax = _apply_k(amin, amax, m.group(0))
            out = _to_usd(amin, amax, _norm(cur), blob)
            out["raw"] = m.group(0).strip()
            return out

    # Rango con moneda al final: 3000-4500 USD
    m = _SAL_RANGE_TRAILING.search(blob)
    if m:
        a, b, cur = m.group(1), m.group(2), m.group(3)
        amin, amax = _parse_amount(a), _parse_amount(b)
        if amin is not None and amax is not None:
            amin, amax = _apply_k(amin, amax, m.group(0))
            out = _to_usd(amin, amax, _norm(cur), blob)
            out["raw"] = m.group(0).strip()
            return out

    # $3000 - $4500
    m = _SAL_RANGE_DOLLAR.search(blob)
    if m:
        amin, amax = _parse_amount(m.group(1)), _parse_amount(m.group(2))
        if amin is not None and amax is not None:
            amin, amax = _apply_k(amin, amax, m.group(0))
            cur = _currency_from(m.group(0) + " " + blob[:80])
            out = _to_usd(amin, amax, cur, blob)
            out["raw"] = m.group(0).strip()
            return out

    # Monto único: USD 4000 | 4000 USD | $4000
    m = _SAL_SINGLE.search(blob)
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
