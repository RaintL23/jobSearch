"""Tests de backend.query_match: normalización y relevancia de ofertas."""

from __future__ import annotations

from backend.query_match import (
    extract_location,
    matches_search_queries,
    normalize_query_text,
    query_tokens,
)


def test_normalize_query_text_synonyms():
    assert normalize_query_text(".NET Developer") == "dotnet developer"
    assert normalize_query_text("C# Backend") == "csharp backend"
    assert normalize_query_text("Full Stack") == "fullstack"


def test_query_tokens_filters_stopwords_and_short():
    tokens = query_tokens("Developer de la React")
    assert "de" not in tokens and "la" not in tokens
    assert "developer" in tokens and "react" in tokens


def test_matches_empty_queries_is_true():
    assert matches_search_queries({"title": "Cualquier cosa"}, []) is True
    assert matches_search_queries({"title": "Cualquier cosa"}, None) is True


def test_matches_phrase_in_title():
    job = {"title": "Senior .NET Developer", "description": ""}
    assert matches_search_queries(job, [".net developer"]) is True


def test_matches_strong_token_in_title():
    job = {"title": "React Native Engineer", "description": ""}
    assert matches_search_queries(job, ["react"]) is True


def test_no_match_for_unrelated():
    job = {"title": "Java Frontend Engineer", "description": "React and Vue"}
    assert matches_search_queries(job, ["python data engineer"]) is False


def test_extract_location_from_field():
    assert extract_location({"location": "Buenos Aires, Argentina"}) == "Buenos Aires, Argentina"


def test_extract_location_ignores_placeholder():
    assert extract_location({"location": "n/d"}) == ""


def test_extract_location_from_description():
    job = {"description": "Puesto remoto. Ubicación: Córdoba, Argentina\nOtros datos"}
    assert extract_location(job) == "Córdoba, Argentina"
