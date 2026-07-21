"""Tests de backend.core.dates: parseo de fechas y ventanas de antigüedad."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.core.dates import (
    hours_since_published,
    parse_published_at,
    parse_relative_published,
    within_posted_window,
)

NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_published_at_none_and_empty():
    assert parse_published_at(None) is None
    assert parse_published_at("") is None


def test_parse_published_at_epoch_seconds():
    iso = parse_published_at(1704067200)  # 2024-01-01 UTC
    assert iso is not None and iso.startswith("2024-01-01")


def test_parse_published_at_epoch_millis():
    iso = parse_published_at(1704067200000)
    assert iso is not None and iso.startswith("2024-01-01")


def test_parse_published_at_iso_and_date():
    assert parse_published_at("2024-03-15").startswith("2024-03-15")
    assert parse_published_at("2024-03-15T10:30:00Z").startswith("2024-03-15T10:30")


def test_parse_relative_hours_spanish():
    iso = parse_relative_published("hace 2 horas", now=NOW)
    assert iso == (NOW - timedelta(hours=2)).isoformat()


def test_parse_relative_days_english():
    iso = parse_relative_published("3 days ago", now=NOW)
    assert iso == (NOW - timedelta(days=3)).isoformat()


def test_parse_relative_yesterday_and_today():
    assert parse_relative_published("ayer", now=NOW).startswith("2024-06-14")
    assert parse_relative_published("today", now=NOW).startswith("2024-06-15")


def test_hours_since_published():
    assert hours_since_published(None) is None
    past = (NOW - timedelta(hours=5)).isoformat()
    assert abs(hours_since_published(past, now=NOW) - 5.0) < 0.01


def test_within_posted_window_no_filter_or_no_date():
    assert within_posted_window(None, {}) is True
    assert within_posted_window(None, {"posted_within": ["24h"]}) is True


def test_within_posted_window_recent_vs_old():
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    assert within_posted_window(recent, {"posted_within": ["24h"]}) is True
    assert within_posted_window(old, {"posted_within": ["24h"]}) is False
    # Ventana más amplia gana si hay varias
    assert within_posted_window(old, {"posted_within": ["24h", "month"]}) is False
    week_old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert within_posted_window(week_old, {"posted_within": ["24h", "week"]}) is True
