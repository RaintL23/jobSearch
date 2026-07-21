"""Tests del origen y la reutilización de sesiones autenticadas."""

from __future__ import annotations

from backend.auth import sessions as auth_sessions


def _create_linkedin_session(tmp_path, *, mode: str | None = None) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "linkedin.json").write_text('{"cookies": [{"name": "li_at"}]}')
    (tmp_path / "linkedin.channel").write_text("msedge")
    profile = tmp_path / "browser_profiles" / "msedge"
    profile.mkdir(parents=True)
    (profile / "Local State").write_text("{}")
    if mode:
        (tmp_path / "linkedin.mode").write_text(mode)


def test_detects_existing_dedicated_profile_and_migrates_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_sessions, "AUTH_DIR", tmp_path)
    _create_linkedin_session(tmp_path)

    result = auth_sessions.dedicated_profile_for_scrape_source("linkedin")

    assert result == ("msedge", str(tmp_path / "browser_profiles" / "msedge"))
    assert (tmp_path / "linkedin.mode").read_text() == "profile"


def test_does_not_use_dedicated_profile_for_imported_system_session(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(auth_sessions, "AUTH_DIR", tmp_path)
    _create_linkedin_session(tmp_path, mode="system")

    assert auth_sessions.dedicated_profile_for_scrape_source("linkedin") is None
