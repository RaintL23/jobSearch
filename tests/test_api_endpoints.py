"""Tests de endpoints de backend.main con TestClient (scraping / IA mockeados)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import main


@pytest.fixture()
def client() -> TestClient:
    return TestClient(main.app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_upload_cv_rejects_non_pdf(client):
    r = client.post("/upload-cv", files={"file": ("cv.txt", b"hola", "text/plain")})
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_upload_cv_rejects_empty(client):
    r = client.post(
        "/upload-cv", files={"file": ("cv.pdf", b"", "application/pdf")}
    )
    assert r.status_code == 400


def test_search_requires_query_or_profile(client):
    r = client.post("/search-jobs", json={"profile": {}, "filters": {}})
    assert r.status_code == 400


def test_search_happy_path(monkeypatch, client):
    def fake_search(profile, max_jobs, filters, on_progress=None):
        return {
            "jobs": [
                {
                    "title": "Python Developer",
                    "company": "ACME",
                    "description": "Requisitos: Python. Ofrecemos USD 5000. Remoto.",
                    "url": "https://example.com/1",
                    "source": "remotive",
                    "published_at": None,
                }
            ],
            "sources": {"remotive": {"ok": True, "count": 1, "message": "OK"}},
        }

    monkeypatch.setattr(main, "search_jobs", fake_search)
    payload = {
        "profile": {"roles": ["Python Developer"], "skills": ["Python"]},
        "filters": {"query": "python"},
    }
    r = client.post("/search-jobs", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["jobs"][0]["title"] == "Python Developer"
    assert body["sources"]["remotive"]["ok"] is True


def test_generate_cover_letter(monkeypatch, client):
    monkeypatch.setattr(main, "generate_cover_letter", lambda p, j: "Carta de prueba")
    r = client.post(
        "/generate-cover-letter",
        json={"profile": {"name": "Ada"}, "job": {"title": "Dev"}},
    )
    assert r.status_code == 200
    assert r.json()["cover_letter"] == "Carta de prueba"


def test_auth_sessions_status(client):
    r = client.get("/auth/sessions")
    assert r.status_code == 200
    assert set(["sessions", "browser", "pending"]).issubset(r.json().keys())


def test_auth_login_invalid_site(client):
    r = client.post("/auth/login/facebook")
    assert r.status_code == 400
