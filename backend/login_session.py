"""
CLI: capturar sesión desde Chrome/Edge (o Chromium).

Uso:
  python -m backend.login_session linkedin
  python -m backend.login_session computrabajo --force-restart
  python -m backend.login_session status
  python -m backend.login_session clear linkedin
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from backend.auth_sessions import (
    AUTH_SITES,
    BrowserRestartRequired,
    cdp_status,
    clear_session,
    interactive_login,
    session_status,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Captura sesión LinkedIn/Computrabajo desde tu Chrome/Edge."
    )
    parser.add_argument(
        "action",
        choices=["linkedin", "computrabajo", "status", "clear"],
        help="Sitio a capturar, 'status' o 'clear'",
    )
    parser.add_argument(
        "site",
        nargs="?",
        choices=list(AUTH_SITES.keys()),
        help="Sitio para 'clear'",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Segundos para completar el login (default 600)",
    )
    parser.add_argument(
        "--mode",
        choices=["profile", "system", "playwright"],
        default="profile",
        help="profile=Edge/Chrome JobSearch (default); system=perfil diario (puede reiniciar)",
    )
    parser.add_argument(
        "--channel",
        choices=["chrome", "msedge"],
        default=None,
        help="Forzar Chrome o Edge",
    )
    parser.add_argument(
        "--force-restart",
        action="store_true",
        help="Cierra y reabre el navegador con tu perfil + depuración remota",
    )
    args = parser.parse_args(argv)

    if args.action == "status":
        print(json.dumps(
            {"sessions": session_status(), "browser": cdp_status(channel=args.channel)},
            indent=2,
            ensure_ascii=False,
        ))
        return 0

    if args.action == "clear":
        site = args.site
        if not site:
            print("Indicá el sitio: python -m backend.login_session clear linkedin", file=sys.stderr)
            return 2
        print(json.dumps(clear_session(site), indent=2, ensure_ascii=False))
        return 0

    site = args.action
    print(f"\n→ Captura de {AUTH_SITES[site]['label']} (mode={args.mode}).")
    if args.mode == "profile":
        print("  Se abre Edge/Chrome con perfil JobSearch (no cierra tu navegador).")
    elif args.mode == "system":
        print("  Importa desde tu perfil diario (puede pedir reiniciar el navegador).")
    else:
        print("  Chromium vacío de Playwright.")
    print()

    try:
        info = interactive_login(
            site,
            timeout_sec=args.timeout,
            mode=args.mode,
            channel=args.channel,
            force_restart=args.force_restart,
        )
    except BrowserRestartRequired as exc:
        print(str(exc), file=sys.stderr)
        print(
            "\nReintentá con: python -m backend.login_session "
            f"{site} --force-restart",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(info, indent=2, ensure_ascii=False))
    print("\nListo. Las búsquedas usarán esta sesión.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
