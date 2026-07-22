"""Tests de merge_profile_filters: defaults desde profile.filters."""

from __future__ import annotations

from backend.scraping.filters import merge_profile_filters


def test_merge_fills_empty_from_profile():
    profile = {
        "filters": {
            "posted_within": ["24h"],
            "work_modes": ["remote"],
            "posting_languages": ["en", "es"],
        }
    }
    out = merge_profile_filters(profile, {"posted_within": [], "work_modes": []})
    assert out["posted_within"] == ["24h"]
    assert out["work_modes"] == ["remote"]
    assert out["posting_languages"] == ["en", "es"]


def test_request_overrides_profile_defaults():
    profile = {"filters": {"posted_within": ["24h"], "work_modes": ["remote"]}}
    out = merge_profile_filters(
        profile,
        {"posted_within": ["week"], "work_modes": ["hybrid"]},
    )
    assert out["posted_within"] == ["week"]
    assert out["work_modes"] == ["hybrid"]


def test_merge_salary_defaults():
    profile = {"filters": {"salary_min_usd": 2000, "salary_max_usd": 6000}}
    out = merge_profile_filters(profile, {"salary_min_usd": None})
    assert out["salary_min_usd"] == 2000.0
    assert out["salary_max_usd"] == 6000.0


def test_merge_noop_without_profile_filters():
    req = {"posted_within": ["week"]}
    assert merge_profile_filters({}, req) == req
    assert merge_profile_filters({"filters": "nope"}, req) == req
