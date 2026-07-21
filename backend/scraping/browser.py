"""Playwright: lanzamiento de browser, páginas y señales anti-bot."""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from threading import Lock
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page

from backend.auth.sessions import (
    preferred_system_channel,
    storage_state_for_scrape_source,
)
from backend.scraping.constants import USER_AGENT, BrowserTarget

logger = logging.getLogger(__name__)

_DEDICATED_PROFILE_LOCK = Lock()

_BLOCKED_MARKERS = (
    "403 forbidden",
    "403 error",
    "access denied",
    "access forbidden",
    "request blocked",
    "attention required",
    "just a moment",
    "cf-browser-verification",
    "enable javascript and cookies",
    "sorry, you have been blocked",
)


def _gentle_pause(lo: float = 0.15, hi: float = 0.45) -> None:
    time.sleep(random.uniform(lo, hi))


def _looks_blocked(text: str) -> bool:
    low = (text or "").lower()
    if not low.strip():
        return False
    if any(m in low for m in _BLOCKED_MARKERS):
        return True
    if "forbidden" in low and len(low) < 400:
        return True
    return False


def _first_text(page: Page, selectors: list[str], *, long: bool = False) -> str:
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            text = (el.inner_text() or "").strip()
            if text:
                return text if long else text.splitlines()[0].strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


def _new_page(browser: BrowserTarget, *, site: str | None = None) -> Page:
    """
    Crea página. Si `site` es linkedin/computrabajo y hay storage_state local,
    reutiliza la sesión (sin contraseña en el proyecto).
    """
    ctx_kwargs: dict[str, Any] = {
        "locale": "es-AR",
        "viewport": {"width": 1365, "height": 900},
    }
    # linkedin_hiring comparte sesión con linkedin
    state_key = "linkedin" if site == "linkedin_hiring" else site
    state = storage_state_for_scrape_source(state_key or "") if site else None

    # Con sesión de LinkedIn usamos Chrome/Edge real (headed); forzar un
    # User-Agent/Accept-Language sintéticos ahí desincroniza el navegador real
    # de sus propios Client Hints (Sec-CH-UA) y es justo el tipo de señal que
    # el anti-bot de LinkedIn usa para invalidar la sesión y mostrar contenido
    # de invitado aunque las cookies (li_at) sean válidas. Solo forzamos estos
    # headers cuando NO hay navegador real detrás (headless Chromium genérico).
    real_browser_session = bool(state) and state_key in ("linkedin",)
    if not real_browser_session:
        ctx_kwargs["user_agent"] = USER_AGENT
        ctx_kwargs["extra_http_headers"] = {
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }

    if state and isinstance(browser, Browser):
        ctx_kwargs["storage_state"] = state
        logger.info("Usando sesión guardada para %s (%s)", site, state)

    if isinstance(browser, BrowserContext):
        context = browser
        logger.info("Usando directamente el perfil persistente para %s", site)
        # Migración de sesiones creadas por versiones anteriores: si el perfil
        # todavía no tiene la cookie de LinkedIn, sembrarla una sola vez desde
        # el storage_state que produjo el login. Después Chromium la persiste.
        if state and state_key == "linkedin":
            try:
                cookie_names = {
                    str(cookie.get("name") or "")
                    for cookie in context.cookies("https://www.linkedin.com")
                }
                if "li_at" not in cookie_names:
                    saved = json.loads(Path(state).read_text(encoding="utf-8"))
                    cookies = saved.get("cookies") or []
                    if cookies:
                        context.add_cookies(cookies)
                        logger.info("Sesión anterior migrada al perfil persistente")
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo migrar la sesión al perfil: %s", exc)
    else:
        context = browser.new_context(**ctx_kwargs)
    page = context.new_page()
    page.set_default_timeout(30000)
    # Reduce señales de automatización
    try:
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
    except Exception:  # noqa: BLE001
        pass
    return page


def _linkedin_session_ready() -> bool:
    return bool(storage_state_for_scrape_source("linkedin"))


def _looks_like_linkedin_authwall(url: str) -> bool:
    low = (url or "").lower()
    if "authwall" in low or "checkpoint" in low or "challenge" in low:
        return True
    if "/uas/login" in low or "/login" in low:
        # /feed/login no es típico; login real sí
        if "/feed" in low or "/jobs" in low or "/search" in low:
            return False
        return True
    return False


def _launch_browser_for_source(
    p: Any,
    source: str,
    dedicated_profile: tuple[str, str] | None = None,
) -> BrowserTarget:
    """
    LinkedIn con sesión: usa Edge/Chrome headed (headless suele disparar authwall
    aunque las cookies sean válidas).
    """
    if dedicated_profile:
        channel, profile_dir = dedicated_profile
        logger.info(
            "Abriendo %s con el perfil persistente JobSearch (%s)",
            source,
            channel,
        )
        return p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel=channel,
            headless=False,
            locale="es-AR",
            viewport={"width": 1365, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )

    linkedin_like = source in ("linkedin", "linkedin_hiring")
    has_session = linkedin_like and _linkedin_session_ready()
    # site="linkedin": prioriza el navegador (chrome/msedge) con el que
    # efectivamente se guardó la sesión, en vez de asumir Chrome por defecto.
    channel = preferred_system_channel(site="linkedin") if has_session else None
    headed = bool(has_session)

    launch_kwargs: dict[str, Any] = {
        "headless": not headed,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    if channel:
        launch_kwargs["channel"] = channel
        logger.info(
            "Lanzando %s headed=%s channel=%s (sesión LinkedIn)",
            source,
            headed,
            channel,
        )
    elif headed:
        logger.info("Lanzando %s headed Chromium (sesión LinkedIn)", source)

    return p.chromium.launch(**launch_kwargs)
