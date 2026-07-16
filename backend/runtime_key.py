"""
Almacenamiento en memoria de la API key de Google Gemini.

Si el usuario no tiene GOOGLE_API_KEY en .env puede proveerla desde el
frontend. La clave se mantiene solo mientras el proceso del servidor esté
activo; al reiniciar se pierde (no se persiste en disco).
"""

from __future__ import annotations

_runtime_api_key: str = ""


def set_runtime_key(key: str) -> None:
    """Guarda la clave en memoria para esta sesión del servidor."""
    global _runtime_api_key
    _runtime_api_key = (key or "").strip()


def get_runtime_key() -> str:
    """Devuelve la clave en memoria (vacía si no fue configurada)."""
    return _runtime_api_key


def has_runtime_key() -> bool:
    key = _runtime_api_key.strip()
    return bool(key) and key != "tu_api_key_aqui"
