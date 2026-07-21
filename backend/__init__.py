"""
AI Job Scraper & Matcher — backend.

Arquitectura de paquetes:

  api/         FastAPI (HTTP)
  scraping/    Playwright + APIs de ofertas (PASO 1–2)
  analysis/    Análisis local de ofertas (PASO 3)
  ai/          Gemini (perfil, relevancia, emails)
  auth/        Sesiones de navegador / login
  core/        Config, fechas, utils, query match

Entrypoints:
  python -m backend.run
  uvicorn backend.api.app:app
"""
