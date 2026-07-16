"""
Utilidades generales: lectura de PDF y helpers de JSON.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import pdfplumber


class PDFExtractionError(Exception):
    """Error al extraer texto de un PDF."""


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """
    Extrae texto de un PDF a partir de sus bytes.

    Raises:
        PDFExtractionError: si el archivo está vacío, no es PDF válido o no tiene texto.
    """
    if not file_bytes:
        raise PDFExtractionError("El archivo PDF está vacío.")

    try:
        pages_text: list[str] = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if not pdf.pages:
                raise PDFExtractionError("El PDF no contiene páginas legibles.")

            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages_text.append(text.strip())

        full_text = "\n\n".join(pages_text).strip()
        if not full_text:
            raise PDFExtractionError(
                "No se pudo extraer texto del PDF. "
                "Puede ser un escaneo (imagen) o un archivo protegido."
            )
        return full_text
    except PDFExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PDFExtractionError(f"Error al leer el PDF: {exc}") from exc


def strip_json_fences(raw: str) -> str:
    """Elimina fences markdown ```json ... ``` si el LLM las incluye."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def parse_llm_json(raw: str) -> dict[str, Any]:
    """
    Parsea un JSON producido por un LLM, tolerando fences markdown.
    """
    cleaned = strip_json_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Intento extraer el primer objeto {...} embebido
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise ValueError(f"La respuesta del LLM no es JSON válido: {exc}") from exc
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise ValueError("Se esperaba un objeto JSON (dict).")
    return data


def slugify(text: str) -> str:
    """Convierte texto a slug URL-friendly (sin acentos)."""
    import unicodedata

    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower().strip()
    ascii_text = re.sub(r"[^a-z0-9\s-]", "", ascii_text)
    ascii_text = re.sub(r"[\s_]+", "-", ascii_text)
    ascii_text = re.sub(r"-+", "-", ascii_text).strip("-")
    return ascii_text or "empleo"
