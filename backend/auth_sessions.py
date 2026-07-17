"""
Sesiones autenticadas locales vía Playwright storage_state.

Flujo principal (sin reiniciar tu navegador diario):
1. Abre Chrome/Edge con un perfil dedicado de JobSearch (persistente).
2. Te logueás una vez (2FA/captcha ok); la próxima ya queda la sesión.
3. Guarda cookies en playwright/.auth/*.json para el scraper.

Opcional mode=system: importa cookies de tu perfil diario vía CDP
(requiere reiniciar Chrome/Edge una vez — molesto; no es el default).

IMPORTANTE: esos JSON equivalen a estar logueado. No los subas a git.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from backend.config import get_settings

_IS_MAC = sys.platform == "darwin"
_IS_WIN = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
AUTH_DIR = ROOT_DIR / "playwright" / ".auth"

AUTH_SITES: dict[str, dict[str, Any]] = {
    "linkedin": {
        "label": "LinkedIn",
        "login_url": "https://www.linkedin.com/login",
        "home_url": "https://www.linkedin.com/feed/",
        "file": "linkedin.json",
        "used_by": ["linkedin", "linkedin_hiring"],
    },
    "computrabajo": {
        "label": "Computrabajo",
        "login_url": None,
        "home_url": None,
        "file": "computrabajo.json",
        "used_by": ["computrabajo"],
    },
}

USER_AGENT = get_settings().user_agent

LOGIN_TIMEOUT_SEC = get_settings().login_timeout_sec
CDP_PORT = get_settings().browser_cdp_port
CDP_URL = get_settings().cdp_url


class BrowserRestartRequired(Exception):
    """Hay que cerrar/reabrir el navegador del sistema con depuración remota."""

    def __init__(self, message: str, *, channel: str):
        super().__init__(message)
        self.channel = channel


def ensure_auth_dir() -> Path:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    gitignore = AUTH_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")
    return AUTH_DIR


def session_path(site: str) -> Path:
    meta = AUTH_SITES.get(site)
    if not meta:
        raise ValueError(f"Sitio de auth desconocido: {site}")
    return AUTH_DIR / str(meta["file"])


def _channel_hint_path(site: str) -> Path:
    return AUTH_DIR / f"{site}.channel"


def _auth_mode_hint_path(site: str) -> Path:
    return AUTH_DIR / f"{site}.mode"


def _save_auth_mode(site: str, mode: str) -> None:
    """Recuerda de dónde salió la sesión para reutilizarla de la misma forma."""
    try:
        ensure_auth_dir()
        _auth_mode_hint_path(site).write_text(mode, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _load_auth_mode(site: str) -> str | None:
    try:
        path = _auth_mode_hint_path(site)
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except Exception:  # noqa: BLE001
        pass
    return None


def _save_channel_hint(site: str, channel: str) -> None:
    """Recuerda qué navegador (chrome/msedge) se usó para loguearse en `site`,
    para reutilizar el mismo en cada scrape (misma huella = misma sesión)."""
    if channel not in ("chrome", "msedge"):
        return
    try:
        ensure_auth_dir()
        _channel_hint_path(site).write_text(channel, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _load_channel_hint(site: str) -> str | None:
    try:
        path = _channel_hint_path(site)
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except Exception:  # noqa: BLE001
        pass
    return None


def preferred_system_channel(
    user_agent: str | None = None, *, site: str | None = None
) -> str | None:
    """
    Devuelve 'msedge'/'chrome' si el ejecutable existe; si no, None.

    Prioridad: 1) navegador con el que se inició sesión en `site` (persistido,
    para no romper la sesión guardada usando un canal distinto), 2) el que
    sugiera el User-Agent del cliente que llamó a la API, 3) default de
    plataforma (Edge primero en Windows, Chrome en macOS/Linux).
    """
    hinted = _load_channel_hint(site) if site else None
    detected = detect_channel_from_ua(user_agent)
    order: tuple[str | None, ...] = (hinted, detected, "chrome", "msedge")
    if _IS_WIN:
        order = (hinted, detected, "msedge", "chrome")
    for ch in order:
        if ch and _browser_exe(ch):
            return ch
    return None


def storage_state_for_scrape_source(source: str) -> str | None:
    ensure_auth_dir()
    for site, meta in AUTH_SITES.items():
        if source in meta["used_by"]:
            path = session_path(site)
            return str(path) if path.is_file() and path.stat().st_size > 20 else None
    return None


def dedicated_profile_for_scrape_source(source: str) -> tuple[str, str] | None:
    """
    Devuelve (canal, directorio) cuando la sesión pertenece al perfil JobSearch.

    Las instalaciones anteriores no tienen el archivo ``*.mode``. En ese caso
    reconocemos un perfil Chromium ya inicializado para migrarlas sin exigir
    otro login.
    """
    site = next(
        (key for key, meta in AUTH_SITES.items() if source in meta["used_by"]),
        None,
    )
    if not site or not storage_state_for_scrape_source(source):
        return None

    mode = _load_auth_mode(site)
    if mode and mode != "profile":
        return None

    channel = _load_channel_hint(site)
    if channel not in ("chrome", "msedge"):
        return None

    profile = AUTH_DIR / "browser_profiles" / channel
    initialized = (profile / "Local State").is_file() or (
        profile / "Default" / "Preferences"
    ).is_file()
    if not initialized:
        return None

    if mode is None:
        _save_auth_mode(site, "profile")
    return channel, str(profile)


def detect_channel_from_ua(user_agent: str | None) -> str | None:
    """chrome | msedge según el User-Agent del cliente; None si no hay pista clara."""
    ua = (user_agent or "").lower()
    if "edg/" in ua or "edgios" in ua:
        return "msedge"
    if "chrome/" in ua or "chromium/" in ua:
        return "chrome"
    return None


def _local_app_data() -> Path:
    raw = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(raw)


def _browser_exe(channel: str) -> Path | None:
    candidates: list[Path] = []
    home = Path.home()

    if channel == "msedge":
        if _IS_WIN:
            local = _local_app_data()
            candidates = [
                local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
                / "Microsoft"
                / "Edge"
                / "Application"
                / "msedge.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
                / "Microsoft"
                / "Edge"
                / "Application"
                / "msedge.exe",
            ]
        elif _IS_MAC:
            candidates = [
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                home / "Applications" / "Microsoft Edge.app" / "Contents" / "MacOS" / "Microsoft Edge",
            ]
        elif _IS_LINUX:
            for name in ("microsoft-edge", "microsoft-edge-stable", "msedge"):
                which = shutil.which(name)
                if which:
                    candidates.append(Path(which))
        which = shutil.which("msedge")
        if which:
            candidates.insert(0, Path(which))
    else:
        if _IS_WIN:
            local = _local_app_data()
            candidates = [
                local / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe",
            ]
        elif _IS_MAC:
            candidates = [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                home / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]
        elif _IS_LINUX:
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
                which = shutil.which(name)
                if which:
                    candidates.append(Path(which))
        which = shutil.which("chrome") or shutil.which("google-chrome")
        if which:
            candidates.insert(0, Path(which))

    for path in candidates:
        if path and path.is_file():
            return path
    return None


def _user_data_dir(channel: str) -> Path:
    home = Path.home()
    if _IS_MAC:
        support = home / "Library" / "Application Support"
        if channel == "msedge":
            return support / "Microsoft Edge"
        return support / "Google" / "Chrome"
    if _IS_LINUX:
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        if channel == "msedge":
            return config / "microsoft-edge"
        return config / "google-chrome"
    local = _local_app_data()
    if channel == "msedge":
        return local / "Microsoft" / "Edge" / "User Data"
    return local / "Google" / "Chrome" / "User Data"


def _process_names(channel: str) -> tuple[str, ...]:
    if _IS_MAC:
        if channel == "msedge":
            return ("Microsoft Edge",)
        return ("Google Chrome", "Chromium")
    if _IS_LINUX:
        if channel == "msedge":
            return ("microsoft-edge", "msedge")
        return ("chrome", "google-chrome", "chromium", "chromium-browser")
    if channel == "msedge":
        return ("msedge.exe",)
    return ("chrome.exe",)


def cdp_available(url: str | None = None) -> bool:
    endpoint = (url or CDP_URL).rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(endpoint, timeout=1.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _kill_browser(channel: str) -> None:
    for name in _process_names(channel):
        try:
            if _IS_WIN:
                subprocess.run(
                    ["taskkill", "/IM", name, "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                # killall / pkill por nombre de proceso (macOS / Linux)
                subprocess.run(
                    ["killall", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("kill browser %s: %s", name, exc)
    time.sleep(2.0)


def _start_browser_with_cdp(channel: str) -> None:
    exe = _browser_exe(channel)
    if not exe:
        raise RuntimeError(
            f"No se encontró el ejecutable de {'Edge' if channel == 'msedge' else 'Chrome'}."
        )
    user_data = _user_data_dir(channel)
    if not user_data.is_dir():
        raise RuntimeError(f"No existe el perfil del navegador: {user_data}")

    cmd = [
        str(exe),
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
    ]
    logger.info("Iniciando %s con CDP en puerto %s", channel, CDP_PORT)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 25
    while time.time() < deadline:
        if cdp_available():
            return
        time.sleep(0.4)
    raise RuntimeError(
        f"El navegador arrancó pero no respondió en {CDP_URL}. "
        "Probá cerrarlo manualmente y reintentar."
    )


def ensure_system_browser_cdp(
    channel: str,
    *,
    force_restart: bool = False,
) -> str:
    """
    Deja disponible CDP en el navegador del sistema (con tu perfil).
    Si el navegador ya está abierto sin depuración, hace falta reiniciarlo.
    """
    if cdp_available() and not force_restart:
        return CDP_URL

    exe = _browser_exe(channel)
    if not exe:
        raise RuntimeError(
            "No hay Chrome/Edge instalado. Instalá uno o usá mode=playwright."
        )

    # Si hay procesos del browser y no hay CDP, hay que reiniciar el perfil.
    running = _browser_process_running(channel)
    if running and not force_restart:
        label = "Microsoft Edge" if channel == "msedge" else "Google Chrome"
        raise BrowserRestartRequired(
            f"{label} está abierto sin depuración remota. "
            "Para reutilizar tus cookies/credenciales hay que cerrarlo y "
            "reabrirlo una vez con acceso seguro. "
            "Confirmá en la UI (se reabrirá con tu perfil).",
            channel=channel,
        )

    if running and force_restart:
        _kill_browser(channel)

    if not cdp_available():
        _start_browser_with_cdp(channel)
    return CDP_URL


def _browser_process_running(channel: str) -> bool:
    for name in _process_names(channel):
        try:
            if _IS_WIN:
                proc = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if name.lower() in (proc.stdout or "").lower():
                    return True
            else:
                proc = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0 and (proc.stdout or "").strip():
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _computrabajo_login_url() -> str:
    country = str(os.getenv("COMPUTRABAJO_COUNTRY", "ar")).lower().strip() or "ar"
    return f"https://{country}.computrabajo.com/login"


def _computrabajo_home_url() -> str:
    country = str(os.getenv("COMPUTRABAJO_COUNTRY", "ar")).lower().strip() or "ar"
    return f"https://{country}.computrabajo.com/"


def _login_url(site: str) -> str:
    if site == "computrabajo":
        return _computrabajo_login_url()
    return str(AUTH_SITES[site]["login_url"])


def _home_url(site: str) -> str:
    if site == "computrabajo":
        return _computrabajo_home_url()
    return str(AUTH_SITES[site].get("home_url") or _login_url(site))


def _linkedin_logged_in(url: str) -> bool:
    low = (url or "").lower()
    if any(
        x in low
        for x in ("/login", "checkpoint", "authwall", "challenge", "uas/login")
    ):
        return False
    return any(
        x in low
        for x in (
            "/feed",
            "/jobs",
            "/in/",
            "/mynetwork",
            "/messaging",
            "/notifications",
            "linkedin.com/hp",
        )
    )


def _computrabajo_logged_in(url: str, cookie_names: set[str]) -> bool:
    low = (url or "").lower()
    if "computrabajo.com" not in low:
        return False
    if "/login" in low or "/candidato/login" in low or "/account/login" in low:
        return False
    sessionish = {
        n.lower()
        for n in cookie_names
        if any(
            k in n.lower()
            for k in ("session", "auth", "token", "login", "user", "jwt", "asp.net")
        )
    }
    if sessionish:
        return True
    return any(
        x in low
        for x in ("/candidato", "/postulante", "/mi-cuenta", "/cv", "/aplicaciones")
    )


def _is_logged_in(site: str, page_url: str, cookie_names: set[str]) -> bool:
    if site == "linkedin":
        return _linkedin_logged_in(page_url)
    if site == "computrabajo":
        return _computrabajo_logged_in(page_url, cookie_names)
    return False


def session_status(site: str | None = None) -> dict[str, Any]:
    ensure_auth_dir()
    sites = [site] if site else list(AUTH_SITES.keys())
    out: dict[str, Any] = {}
    for s in sites:
        if s not in AUTH_SITES:
            continue
        path = session_path(s)
        exists = path.is_file() and path.stat().st_size > 20
        info: dict[str, Any] = {
            "site": s,
            "label": AUTH_SITES[s]["label"],
            "logged_in": exists,
            "path": str(path) if exists else None,
            "updated_at": None,
            "used_by": list(AUTH_SITES[s]["used_by"]),
        }
        if exists:
            info["updated_at"] = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        out[s] = info
    return out


def cdp_status(*, channel: str | None = None, user_agent: str | None = None) -> dict[str, Any]:
    ch = channel or preferred_system_channel(user_agent=user_agent) or "chrome"
    return {
        "cdp_url": CDP_URL,
        "cdp_ready": cdp_available(),
        "channel": ch,
        "channel_label": "Microsoft Edge" if ch == "msedge" else "Google Chrome",
        "browser_running": _browser_process_running(ch),
        "browser_installed": bool(_browser_exe(ch)),
    }


def clear_session(site: str) -> dict[str, Any]:
    if site not in AUTH_SITES:
        raise ValueError(f"Sitio desconocido: {site}")
    path = session_path(site)
    if path.exists():
        path.unlink()
        logger.info("Sesión eliminada: %s", path)
    mode_path = _auth_mode_hint_path(site)
    if mode_path.exists():
        mode_path.unlink()
    return session_status(site)[site]


def _wait_and_save(
    site: str,
    context: BrowserContext,
    page: Page,
    path: Path,
    *,
    timeout_sec: int,
) -> bool:
    label = AUTH_SITES[site]["label"]
    deadline = time.time() + timeout_sec
    last_url = ""
    while time.time() < deadline:
        try:
            if page.is_closed():
                break
            last_url = page.url or ""
            cookies = context.cookies()
            names = {str(c.get("name") or "") for c in cookies}
            # También revisar cookies del dominio aunque la URL aún sea login
            if site == "linkedin":
                li_cookies = [
                    c
                    for c in cookies
                    if "linkedin" in str(c.get("domain") or "").lower()
                ]
                li_names = {str(c.get("name") or "") for c in li_cookies}
                if "li_at" in li_names or _is_logged_in(site, last_url, names):
                    time.sleep(1.0)
                    context.storage_state(path=str(path))
                    logger.info("Sesión %s guardada en %s (url=%s)", label, path, last_url)
                    return True
            elif _is_logged_in(site, last_url, names):
                time.sleep(1.0)
                context.storage_state(path=str(path))
                logger.info("Sesión %s guardada en %s", label, path)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("poll login: %s", exc)
        time.sleep(1.0)
    logger.warning("Timeout login %s · última URL: %s", label, last_url)
    return False


def browser_profile_dir(channel: str) -> Path:
    """Perfil persistente de JobSearch (no toca tu perfil diario)."""
    ensure_auth_dir()
    path = AUTH_DIR / "browser_profiles" / channel
    path.mkdir(parents=True, exist_ok=True)
    return path


def _capture_via_persistent_profile(
    site: str,
    *,
    channel: str,
    timeout_sec: int,
) -> dict[str, Any]:
    """
    Abre Chrome/Edge real con perfil propio de JobSearch.
    No cierra tu navegador diario ni pide reinicio.
    """
    ensure_auth_dir()
    path = session_path(site)
    label = AUTH_SITES[site]["label"]
    profile = browser_profile_dir(channel)
    exe_ok = bool(_browser_exe(channel))

    logger.info(
        "Login %s con perfil JobSearch (%s) en %s",
        label,
        channel if exe_ok else "chromium",
        profile,
    )

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": False,
            "locale": "es-AR",
            "viewport": {"width": 1280, "height": 900},
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignore_default_args": ["--enable-automation"],
        }
        if exe_ok:
            launch_kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(45000)

            try:
                page.goto(_home_url(site), wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                logger.warning("goto home falló (%s): %s", label, exc)
                page.goto(_login_url(site), wait_until="domcontentloaded")

            time.sleep(1.2)
            cookies = context.cookies()
            names = {str(c.get("name") or "") for c in cookies}
            if site == "linkedin":
                already = "li_at" in names or _is_logged_in(site, page.url or "", names)
            else:
                already = _is_logged_in(site, page.url or "", names)

            if already:
                context.storage_state(path=str(path))
                if exe_ok:
                    _save_channel_hint(site, channel)
                _save_auth_mode(site, "profile")
                logger.info("Sesión %s ya activa en perfil JobSearch", label)
                return {
                    **session_status(site)[site],
                    "captured_from": "jobsearch_profile",
                    "channel": channel if exe_ok else "chromium",
                    "already_logged_in": True,
                }

            try:
                page.goto(_login_url(site), wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"No se pudo abrir {label}: {exc}") from exc

            saved = _wait_and_save(site, context, page, path, timeout_sec=timeout_sec)
            if not saved:
                raise TimeoutError(
                    f"No se detectó sesión en {label} a tiempo ({timeout_sec}s). "
                    "Completá el login en la ventana que se abrió; la próxima vez "
                    "no hará falta volver a loguearte."
                )
            if exe_ok:
                _save_channel_hint(site, channel)
            _save_auth_mode(site, "profile")
            return {
                **session_status(site)[site],
                "captured_from": "jobsearch_profile",
                "channel": channel if exe_ok else "chromium",
                "already_logged_in": False,
            }
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


def _capture_via_cdp(
    site: str,
    *,
    channel: str,
    timeout_sec: int,
    force_restart: bool,
) -> dict[str, Any]:
    ensure_auth_dir()
    path = session_path(site)
    label = AUTH_SITES[site]["label"]
    cdp = ensure_system_browser_cdp(channel, force_restart=force_restart)

    with sync_playwright() as p:
        browser: Browser = p.chromium.connect_over_cdp(cdp)
        # Contexto por defecto = tu perfil real (cookies ya cargadas)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(45000)

        # Primero el home: si ya hay sesión, no hace falta login
        try:
            page.goto(_home_url(site), wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            logger.warning("goto home falló (%s), pruebo login: %s", label, exc)
            page.goto(_login_url(site), wait_until="domcontentloaded")

        time.sleep(1.2)
        cookies = context.cookies()
        names = {str(c.get("name") or "") for c in cookies}
        already = False
        if site == "linkedin":
            already = "li_at" in names or _is_logged_in(site, page.url or "", names)
        else:
            already = _is_logged_in(site, page.url or "", names)

        if already:
            context.storage_state(path=str(path))
            _save_channel_hint(site, channel)
            _save_auth_mode(site, "system")
            logger.info("Ya había sesión en %s · capturada desde tu navegador", label)
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            # No cerrar el browser CDP: es el del usuario
            return {
                **session_status(site)[site],
                "captured_from": "system_browser",
                "channel": channel,
                "already_logged_in": True,
            }

        # Pedir login en pestaña del mismo navegador
        try:
            page.goto(_login_url(site), wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"No se pudo abrir {label}: {exc}") from exc

        saved = _wait_and_save(site, context, page, path, timeout_sec=timeout_sec)
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

        if not saved:
            raise TimeoutError(
                f"No se detectó sesión en {label} a tiempo ({timeout_sec}s). "
                "Completá el login en la pestaña que se abrió en tu navegador."
            )

        _save_channel_hint(site, channel)
        _save_auth_mode(site, "system")
        return {
            **session_status(site)[site],
            "captured_from": "system_browser",
            "channel": channel,
            "already_logged_in": False,
        }


def _capture_via_playwright_chromium(site: str, *, timeout_sec: int) -> dict[str, Any]:
    """Fallback: Chromium embebido (sin tus cookies del día a día)."""
    ensure_auth_dir()
    path = session_path(site)
    label = AUTH_SITES[site]["label"]
    login_url = _login_url(site)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=50,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="es-AR",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "es-AR,es;q=0.9,en;q=0.8"},
        )
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(login_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            browser.close()
            raise RuntimeError(f"No se pudo abrir {login_url}: {exc}") from exc

        saved = _wait_and_save(site, context, page, path, timeout_sec=timeout_sec)
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    if not saved:
        raise TimeoutError(
            f"No se detectó sesión en {label} a tiempo ({timeout_sec}s)."
        )
    _save_auth_mode(site, "playwright")
    return {
        **session_status(site)[site],
        "captured_from": "playwright_chromium",
        "already_logged_in": False,
    }


def interactive_login(
    site: str,
    *,
    timeout_sec: int = LOGIN_TIMEOUT_SEC,
    mode: str = "profile",
    channel: str | None = None,
    user_agent: str | None = None,
    force_restart: bool = False,
) -> dict[str, Any]:
    """
    mode=profile → Chrome/Edge con perfil JobSearch (default, sin reiniciar).
    mode=system → importa desde perfil diario vía CDP (puede pedir reinicio).
    mode=playwright → Chromium embebido.
    """
    if site not in AUTH_SITES:
        raise ValueError(f"Sitio desconocido: {site}. Usa: {', '.join(AUTH_SITES)}")

    ch = channel or preferred_system_channel(user_agent=user_agent, site=site) or "chrome"
    mode = (mode or "profile").lower().strip()
    if mode in ("system_browser", "daily", "import"):
        mode = "system"
    if mode in ("persistent", "jobsearch", "default"):
        mode = "profile"

    if mode == "playwright":
        return _capture_via_playwright_chromium(site, timeout_sec=timeout_sec)

    if mode == "system":
        return _capture_via_cdp(
            site,
            channel=ch,
            timeout_sec=timeout_sec,
            force_restart=force_restart,
        )

    return _capture_via_persistent_profile(
        site,
        channel=ch,
        timeout_sec=timeout_sec,
    )
