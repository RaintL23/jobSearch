"""Tests del pipeline collect-then-filter con motivos de descarte."""

from __future__ import annotations

from backend.scraper import (
    _discard_reason,
    _format_source_filter_message,
    _partition_jobs,
)


def test_linkedin_keeps_board_list_without_query_rematch():
    jobs = [
        {
            "title": "Software Engineer",
            "company": "Acme",
            "description": "Oferta en LinkedIn",
            "source": "linkedin",
            "published_at": None,
        },
        {
            "title": "Backend Developer",
            "company": "Beta",
            "description": "Oferta en LinkedIn",
            "source": "linkedin",
            "published_at": None,
        },
    ]
    filters = {"queries": [".NET Developer"], "posted_within": [], "work_modes": [], "experience_levels": []}
    kept, discarded, counts = _partition_jobs(jobs, filters, source="linkedin")
    assert len(kept) == 2
    assert counts == {}
    assert discarded == []


def test_api_source_discards_by_query_with_reason():
    jobs = [
        {
            "title": "Java Developer",
            "company": "X",
            "description": "Spring Boot",
            "source": "remotive",
            "published_at": None,
        },
        {
            "title": ".NET Backend",
            "company": "Y",
            "description": "C# ASP.NET",
            "source": "remotive",
            "published_at": None,
        },
    ]
    filters = {
        "queries": [".NET"],
        "posted_within": [],
        "work_modes": [],
        "experience_levels": [],
    }
    kept, discarded, counts = _partition_jobs(jobs, filters, source="remotive")
    assert len(kept) == 1
    assert kept[0]["title"] == ".NET Backend"
    assert counts.get("query") == 1
    assert discarded[0]["reason"] == "query"


def test_format_message_shows_list_vs_kept():
    msg = _format_source_filter_message(
        raw_count=25,
        kept_count=7,
        reason_counts={"query": 15, "date": 3},
    )
    assert "25 en listado" in msg
    assert "7 guardada(s)" in msg
    assert "texto de búsqueda: 15" in msg
    assert "antigüedad: 3" in msg


def test_discard_reason_date():
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    job = {
        "title": ".NET Dev",
        "company": "Z",
        "description": ".NET",
        "published_at": old,
    }
    filters = {
        "queries": [".NET"],
        "posted_within": ["24h"],
        "work_modes": [],
        "experience_levels": [],
    }
    assert _discard_reason(job, filters, source="linkedin") == "date"
