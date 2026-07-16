"""Tests de backend.config: settings tipadas, derivaciones y overrides."""

from __future__ import annotations

from backend.config import DEFAULT_FX_TO_USD, Settings, get_settings


def _settings(**kwargs) -> Settings:
    # _env_file=None evita leer .env para pruebas deterministas.
    return Settings(_env_file=None, **kwargs)


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_has_api_key_logic():
    assert _settings(google_api_key="real-key").has_api_key is True
    assert _settings(google_api_key="").has_api_key is False
    assert _settings(google_api_key="tu_api_key_aqui").has_api_key is False
    assert _settings(google_api_key="  tu_api_key_aqui  ").has_api_key is False


def test_cdp_url_derived_from_port():
    assert _settings(browser_cdp_port=9999).cdp_url == "http://127.0.0.1:9999"


def test_cdp_url_explicit_wins():
    s = _settings(browser_cdp_url="http://host:1234", browser_cdp_port=9999)
    assert s.cdp_url == "http://host:1234"


def test_default_country_lowercased():
    assert _settings(default_country="AR").default_country == "ar"
    assert _settings(default_country="").default_country == "mx"


def test_fx_defaults_present():
    fx = _settings().fx_to_usd
    assert fx["usd"] == 1.0
    assert fx["ars"] == DEFAULT_FX_TO_USD["ars"]


def test_fx_override_via_json():
    s = _settings(fx_rates_json='{"USD": 2.0, "abc": 3}')
    fx = s.fx_to_usd
    assert fx["usd"] == 2.0
    assert fx["abc"] == 3.0
    # No pisa el resto de defaults
    assert fx["eur"] == DEFAULT_FX_TO_USD["eur"]


def test_fx_override_invalid_json_ignored():
    fx = _settings(fx_rates_json="{not valid").fx_to_usd
    assert fx["usd"] == 1.0
