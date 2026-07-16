"""Tests de backend.ai_engine con un cliente Gemini falso (sin llamadas reales)."""

from __future__ import annotations

import pytest

from backend import ai_engine


class _FakeResp:
    def __init__(self, text: str):
        self.text = text


class _FakeModels:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[tuple[str, object]] = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, config.response_mime_type))
        return _FakeResp(self._text)


class _FakeClient:
    def __init__(self, text: str):
        self.models = _FakeModels(text)


def _patch_client(monkeypatch, text: str) -> _FakeClient:
    client = _FakeClient(text)
    monkeypatch.setattr(
        ai_engine, "_active_client_and_model", lambda: (client, "gemini-test")
    )
    return client


def test_extract_profile_normalizes_and_uses_json_mode(monkeypatch):
    client = _patch_client(
        monkeypatch,
        '{"name": "Ada", "roles": "Dev", "skills": ["C#"], "experience_years": 3}',
    )
    profile = ai_engine.extract_profile_from_cv("Ada, C# dev, 3 años")
    assert profile["name"] == "Ada"
    # string -> list normalization
    assert profile["roles"] == ["Dev"]
    assert profile["skills"] == ["C#"]
    # country default se rellena
    assert profile["country"]
    # se pidió JSON nativo
    assert client.models.calls[0][1] == "application/json"


def test_extract_profile_defaults_when_fields_missing(monkeypatch):
    _patch_client(monkeypatch, "{}")
    profile = ai_engine.extract_profile_from_cv("texto")
    assert profile["name"] == "Candidato"
    assert profile["roles"] == []
    assert profile["skills"] == []


def test_cover_letter_plain_text_no_json_mode(monkeypatch):
    client = _patch_client(monkeypatch, "Estimados, me postulo con entusiasmo.")
    letter = ai_engine.generate_cover_letter({"name": "Ada"}, {"title": "Dev"})
    assert "postulo" in letter
    assert client.models.calls[0][1] is None


def test_missing_api_key_raises(monkeypatch):
    class _NoKey:
        has_api_key = False
        google_api_key = ""
        ai_request_timeout_sec = 60
        gemini_model = "m"

    monkeypatch.setattr(ai_engine, "get_settings", lambda: _NoKey())
    with pytest.raises(ai_engine.AIEngineError):
        ai_engine._active_client_and_model()


def test_generate_json_retries_on_bad_json(monkeypatch):
    calls = {"n": 0}

    def fake_generate_text(prompt, *, json_mode=False):
        calls["n"] += 1
        return "no json" if calls["n"] == 1 else '{"ok": true}'

    monkeypatch.setattr(ai_engine, "_generate_text", fake_generate_text)
    out = ai_engine._generate_json("prompt")
    assert out == {"ok": True}
    assert calls["n"] == 2  # reintentó una vez
