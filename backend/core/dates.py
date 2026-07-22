"""Parseo de fechas de publicación y filtros de antigüedad."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

POSTED_HOURS = {
    "24h": 24,
    "week": 24 * 7,
    "month": 24 * 30,
}


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_published_at(value: Any) -> str | None:
    """Normaliza epoch / ISO / texto relativo a ISO-8601 UTC."""
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return _to_iso(value)

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milisegundos
            ts /= 1000.0
        try:
            return _to_iso(datetime.fromtimestamp(ts, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # Epoch en string
    if re.fullmatch(r"\d{9,13}", text):
        return parse_published_at(int(text))

    # ISO / formatos comunes
    candidates = [
        text,
        text.replace("Z", "+00:00"),
        re.sub(r"\s+", "T", text, count=1),
    ]
    for cand in candidates:
        try:
            return _to_iso(datetime.fromisoformat(cand))
        except ValueError:
            continue

    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%b %d, %Y",
        "%d %b %Y",
    ):
        try:
            return _to_iso(datetime.strptime(text[:32], fmt))
        except ValueError:
            continue

    return parse_relative_published(text)


def parse_relative_published(text: str, *, now: datetime | None = None) -> str | None:
    """Convierte 'hace 2 días' / '3 hours ago' / 'Yesterday' a ISO."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    low = text.strip().lower()

    if re.search(r"\b(just now|ahora mismo|recién|recien)\b", low):
        return _to_iso(now)
    if re.search(r"\b(today|hoy)\b", low):
        return _to_iso(now.replace(hour=12, minute=0, second=0, microsecond=0))
    if re.search(r"\b(yesterday|ayer)\b", low):
        return _to_iso((now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0))

    patterns: list[tuple[str, str]] = [
        (r"(?:hace\s+)?(\d+)\s*min(?:uto)?s?", "minutes"),
        (r"(\d+)\s*minutes?\s*ago", "minutes"),
        (r"(?:hace\s+)?(\d+)\s*m(?:in)?(?![a-z])", "minutes"),  # 15m
        (r"(?:hace\s+)?(\d+)\s*h(?:ora|r|rs)?s?(?![a-z])", "hours"),  # 7h / 7hr
        (r"(\d+)\s*hours?\s*ago", "hours"),
        (r"(?:hace\s+)?(\d+)\s*d(?:[ií]a)?s?(?![a-z])", "days"),  # 2d
        (r"(\d+)\s*days?\s*ago", "days"),
        (r"(?:hace\s+)?(\d+)\s*w(?:k|eek)?s?(?![a-z])", "weeks"),  # 3w
        (r"(?:hace\s+)?(\d+)\s*sem(?:ana)?s?", "weeks"),
        (r"(\d+)\s*weeks?\s*ago", "weeks"),
        (r"(?:hace\s+)?(\d+)\s*mo(?:nth)?s?(?![a-z])", "months"),
        (r"(?:hace\s+)?(\d+)\s*mes(?:es)?", "months"),
        (r"(\d+)\s*months?\s*ago", "months"),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, low)
        if not m:
            continue
        n = int(m.group(1))
        delta = {
            "minutes": timedelta(minutes=n),
            "hours": timedelta(hours=n),
            "days": timedelta(days=n),
            "weeks": timedelta(weeks=n),
            "months": timedelta(days=30 * n),
        }[unit]
        return _to_iso(now - delta)

    return None


def hours_since_published(iso: str | None, *, now: datetime | None = None) -> float | None:
    """Horas transcurridas desde la publicación. None si la fecha no es parseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def within_posted_window(iso: str | None, filters: dict[str, Any]) -> bool:
    """
    True si la oferta entra en el filtro posted_within.
    Sin fecha conocida → se conserva (no se descarta).
    """
    posted_list = list(filters.get("posted_within") or [])
    if not posted_list:
        return True
    if not iso:
        return True

    rank = {"24h": 1, "week": 2, "month": 3}
    widest = max(posted_list, key=lambda x: rank.get(x, 0))
    hours = POSTED_HOURS.get(widest)
    if not hours:
        return True

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True

    return datetime.now(timezone.utc) - dt <= timedelta(hours=hours)
