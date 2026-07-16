"""Tests de backend.utils: slugify, parseo de JSON del LLM y lectura de PDF."""

from __future__ import annotations

import pytest

from backend.utils import (
    PDFExtractionError,
    extract_text_from_pdf,
    parse_llm_json,
    slugify,
    strip_json_fences,
)


def test_slugify_removes_accents_and_symbols():
    assert slugify("Señor .NET Développer!") == "senor-net-developper"
    assert slugify("  Multiple   Spaces ") == "multiple-spaces"


def test_slugify_empty_fallback():
    assert slugify("") == "empleo"
    assert slugify("###") == "empleo"


def test_strip_json_fences():
    assert strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_json_fences('{"a": 1}') == '{"a": 1}'


def test_parse_llm_json_plain():
    assert parse_llm_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_parse_llm_json_fenced():
    assert parse_llm_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_parse_llm_json_embedded_object():
    assert parse_llm_json('Aquí tienes: {"x": 10} fin') == {"x": 10}


def test_parse_llm_json_invalid_raises():
    with pytest.raises(ValueError):
        parse_llm_json("esto no es json")


def test_parse_llm_json_non_object_raises():
    with pytest.raises(ValueError):
        parse_llm_json("[1, 2, 3]")


def test_extract_text_from_pdf_empty_raises():
    with pytest.raises(PDFExtractionError):
        extract_text_from_pdf(b"")


def test_extract_text_from_pdf_invalid_bytes_raises():
    with pytest.raises(PDFExtractionError):
        extract_text_from_pdf(b"no soy un pdf real")
