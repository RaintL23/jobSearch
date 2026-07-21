"""Tests del plan de ubicaciones LinkedIn (país vs ciudades)."""

from __future__ import annotations

from backend.scraping import _is_country_name_location, _linkedin_search_locations


def test_country_selected_always_includes_country_wide_geo():
    locs = _linkedin_search_locations(
        ["Buenos Aires", "Remoto LATAM"],
        has_country=True,
    )
    assert locs[0] == ""
    assert "Buenos Aires" in locs
    assert "Remoto LATAM" in locs


def test_country_selected_skips_redundant_country_name():
    locs = _linkedin_search_locations(["Argentina", "Córdoba"], has_country=True)
    assert locs[0] == ""
    assert "Argentina" not in locs
    assert "Córdoba" in locs


def test_no_country_uses_explicit_locations_only():
    locs = _linkedin_search_locations(["Buenos Aires"], has_country=False)
    assert locs == ["Buenos Aires"]


def test_no_country_empty_falls_back_to_geo_slot():
    assert _linkedin_search_locations([], has_country=False) == [""]
    assert _linkedin_search_locations([""], has_country=True) == [""]


def test_is_country_name_location():
    assert _is_country_name_location("Argentina")
    assert _is_country_name_location("mexico")
    assert not _is_country_name_location("Buenos Aires")
    assert not _is_country_name_location("")
