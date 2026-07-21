"""Tests de backend.analysis.local: salario, match, idiomas y filtros locales."""

from __future__ import annotations

from backend.analysis.local import (
    analyze_job_local,
    compute_match,
    detect_posting_language,
    detect_required_languages,
    extract_contact_email,
    extract_salary_usd,
    passes_language_filters,
    salary_in_range,
)


def test_extract_salary_usd_range():
    out = extract_salary_usd("Ofrecemos USD 3000 - 4500 mensuales")
    assert out["min_usd"] == 3000.0
    assert out["max_usd"] == 4500.0
    assert out["currency"] == "usd"


def test_extract_salary_single_dollar():
    out = extract_salary_usd("Sueldo $4000 al mes")
    assert out["min_usd"] == 4000.0
    assert out["max_usd"] == 4000.0


def test_extract_salary_ars_converted():
    out = extract_salary_usd("ARS 2.500.000 - 3.000.000")
    assert out["currency"] == "ars"
    assert out["min_usd"] == round(2_500_000 * 0.0011, 2)


def test_extract_salary_none_when_absent():
    out = extract_salary_usd("Buscamos gente con ganas de crecer")
    assert out["min_usd"] is None and out["max_usd"] is None


def test_compute_match_high_overlap():
    profile = {
        "skills": ["C#", "SQL Server", "React"],
        "roles": ["Backend Developer"],
        "experience_years": 3,
    }
    job = {
        "title": "C# Backend Developer",
        "description": "We use SQL Server and React daily",
        "requirements": "",
    }
    percent, matched, missing = compute_match(profile, job)
    assert percent >= 90
    assert "c#" in matched and "react" in matched
    assert missing == []


def test_detect_posting_language():
    es = "Requisitos: experiencia en desarrollo. Ofrecemos buen ambiente."
    en = "Requirements: experience with software. We offer remote work."
    assert detect_posting_language(es) == "es"
    assert detect_posting_language(en) == "en"
    assert detect_posting_language("") == "unknown"


def test_detect_required_languages():
    langs = detect_required_languages("Se requiere inglés avanzado y portugués")
    assert "en" in langs and "pt" in langs


def test_salary_in_range():
    assert salary_in_range({"min_usd": 3000, "max_usd": 4000}, None, None) is True
    assert salary_in_range({"min_usd": None, "max_usd": None}, 2000, 5000) is True
    assert salary_in_range({"min_usd": 3000, "max_usd": 4000}, 2000, 5000) is True
    assert salary_in_range({"min_usd": 1000, "max_usd": 1500}, 3000, 5000) is False


def test_passes_language_filters():
    assert passes_language_filters({"posting_language": "es"}, ["es"], []) is True
    assert passes_language_filters({"posting_language": "en"}, ["es"], []) is False
    # Idioma no detectado nunca se excluye
    assert passes_language_filters({"posting_language": "unknown"}, ["es"], []) is True
    # Requerido sin intersección se excluye
    assert (
        passes_language_filters({"required_languages": ["es"]}, [], ["en"]) is False
    )
    # Oferta que no menciona idioma no se excluye
    assert passes_language_filters({"required_languages": []}, [], ["en"]) is True


def test_analyze_job_local_shape():
    profile = {"skills": ["Python"], "roles": ["Data Engineer"], "experience_years": 4}
    job = {
        "title": "Python Data Engineer",
        "company": "ACME",
        "description": "Requisitos: Python y SQL. Ofrecemos USD 5000. Remoto. Send CV to jobs@acme.dev",
        "url": "https://example.com/job/1",
        "source": "remotive",
    }
    result = analyze_job_local(profile, job)
    for key in (
        "title",
        "company",
        "match_percent",
        "requirements",
        "salary_usd",
        "posting_language",
        "required_languages",
        "matched_skills",
        "contact_email",
    ):
        assert key in result
    assert result["title"] == "Python Data Engineer"
    assert isinstance(result["match_percent"], int)
    assert "python" in result["matched_skills"]
    assert result["contact_email"] == "jobs@acme.dev"


def test_extract_contact_email_skips_noise():
    assert extract_contact_email("mailto:hr@startup.io") == "hr@startup.io"
    assert extract_contact_email("img@linkedin.com") == ""
    assert extract_contact_email("sin correo aquí") == ""
