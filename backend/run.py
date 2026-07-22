"""
Launcher: arranca uvicorn y una ventana de UI aislada.
Al cerrar la terminal / Ctrl+C, cierra también esa ventana del navegador.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"

_browser_proc: subprocess.Popen | None = None
_ui_profile_dir = Path(tempfile.gettempdir()) / "jobsearch-ui-profile"


def _browser_candidates() -> list[tuple[str, list[str]]]:
    """(ejecutable, args extra) para abrir la UI en modo app aislado."""
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    home = Path.home()

    paths: list[str] = [
        str(Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        str(Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        str(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        str(Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        str(home / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"),
    ]

    # Linux: resolvemos por PATH (los .exe/.app de arriba no existen ahí).
    if sys.platform.startswith("linux"):
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
        ):
            found = shutil.which(name)
            if found:
                paths.append(found)

    common_args = [
        f"--app={URL}",
        f"--user-data-dir={_ui_profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=TranslateUI",
    ]
    return [(p, common_args) for p in paths if Path(p).is_file()]


def open_ui_window() -> subprocess.Popen | None:
    global _browser_proc
    _ui_profile_dir.mkdir(parents=True, exist_ok=True)

    for exe, args in _browser_candidates():
        try:
            popen_kwargs = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform != "win32":
                # Nueva sesión/grupo de procesos para poder cerrar todo el árbol
                # del navegador de forma fiable al terminar (killpg), igual que
                # taskkill /T en Windows.
                popen_kwargs["start_new_session"] = True
            _browser_proc = subprocess.Popen([exe, *args], **popen_kwargs)
            print(f"  UI abierta en ventana dedicada ({Path(exe).name}).")
            return _browser_proc
        except OSError:
            continue

    # Fallback: abre pestaña normal (puede no poder cerrarse sola).
    try:
        import webbrowser

        webbrowser.open(URL)
        print("  UI abierta en el navegador predeterminado.")
    except Exception:  # noqa: BLE001
        print(f"  Abrí manualmente: {URL}")
    return None


def close_ui_window() -> None:
    global _browser_proc
    proc = _browser_proc
    _browser_proc = None
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # Mata el árbol del perfil aislado sin tocar el Edge/Chrome del usuario.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            # POSIX: cerramos todo el grupo de procesos (Chrome lanza hijos).
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _wait_until_ready(timeout: float = 20.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    atexit.register(close_ui_window)

    def _on_signal(signum, _frame):  # noqa: ANN001
        close_ui_window()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except Exception:  # noqa: BLE001
            pass

    if sys.platform == "win32":
        try:
            signal.signal(signal.SIGBREAK, _on_signal)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        # Al cerrar la ventana CMD con la X, Windows manda CTRL_CLOSE_EVENT.
        try:
            import ctypes

            HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def _console_handler(ctrl_type: int) -> bool:
                # 0 CTRL_C, 1 CTRL_BREAK, 2 CLOSE, 5 LOGOFF, 6 SHUTDOWN
                if ctrl_type in (0, 1, 2, 5, 6):
                    close_ui_window()
                return False

            # Mantener referencia global para que no lo recolecte el GC.
            global _WIN_CONSOLE_HANDLER  # noqa: PLW0603
            _WIN_CONSOLE_HANDLER = HandlerRoutine(_console_handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_WIN_CONSOLE_HANDLER, True)
        except Exception:  # noqa: BLE001
            pass

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.api.app:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    server = subprocess.Popen(cmd)
    try:
        if _wait_until_ready():
            open_ui_window()
        else:
            print("  [ADVERTENCIA] El servidor no respondió a tiempo.")
            open_ui_window()
        return server.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        close_ui_window()
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


_WIN_CONSOLE_HANDLER = None


if __name__ == "__main__":
    raise SystemExit(main())
