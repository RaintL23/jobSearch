# AI Job Scraper & Matcher

Aplicación web que extrae el perfil de un CV (PDF) con Gemini, busca ofertas en Computrabajo y LinkedIn (LATAM) con Playwright, y muestra match, consejos y cover letter.

## Inicio rápido (Windows)

**Requisito único: tener [Python 3.10+](https://python.org/downloads) instalado** (marca "Add Python to PATH" durante la instalación).

### 1. Ejecutar la app

Haz doble clic en `start.bat`.

La primera vez instala las dependencias automáticamente (~2–5 min). Las siguientes veces arranca en segundos y abre el navegador solo.

### 2. Configurar sesiones de scraping (opcional, una sola vez)

Para buscar en LinkedIn y Computrabajo con tu cuenta:

Haz doble clic en `login.bat` y sigue las instrucciones.

---

## Instalación manual (alternativa)

```bash
# Desde la raíz del proyecto
pip install -r requirements.txt

# Instalar el navegador Chromium para Playwright
playwright install chromium

# Configurar variables de entorno
cp .env.example .env
# Edita .env y pega tu GOOGLE_API_KEY
```

## Ejecución manual

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Abre en el navegador: [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Requisitos

- Python 3.10+
- Cuenta de Google AI Studio con `GOOGLE_API_KEY` (gratis en [aistudio.google.com/apikey](https://aistudio.google.com/apikey))
- En free tier, usa un modelo con cuota > 0 (p. ej. `gemini-3.1-flash-lite`). Varios Flash/Pro aparecen con límite 0.

## Configuración

Toda la configuración vive en `backend/config.py` y se ajusta por variables de
entorno o `.env` (ver `.env.example`). Nada está hardcodeado en la lógica.

| Variable | Default | Descripción |
| --- | --- | --- |
| `GOOGLE_API_KEY` | — | Clave de Google AI Studio (obligatoria). |
| `GEMINI_MODELS` | `gemini-2.5-flash-lite,...` | Lista de modelos separados por coma. El motor prueba cada uno en orden si el anterior agota su cuota (429). |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Modelo único de respaldo (solo se usa si `GEMINI_MODELS` está vacío). |
| `DEFAULT_COUNTRY` | `mx` | País ISO2 si el perfil no lo indica (alias: `COMPUTRABAJO_COUNTRY`). |
| `AI_REQUEST_TIMEOUT_SEC` | `60` | Timeout por llamada a Gemini. |
| `AI_MAX_CV_CHARS` | `12000` | Máx. de caracteres del CV enviados al modelo. |
| `AI_MATCH_ENABLED` | `false` | Activa análisis batch con Gemini para ofertas GetOnBoard con ubicación ambigua (hasta 6 ofertas por llamada). Usa ~900 tokens por batch; muy eficiente en Free Tier. |
| `SCRAPE_SAFETY_CAP` | `70` | Tope de ofertas por búsqueda. |
| `PER_SOURCE_CAP` | `12` | Tope de ofertas por fuente de API. |
| `HTTP_TIMEOUT_SEC` | `25` | Timeout de las APIs públicas. |
| `BROWSER_CDP_PORT` | `9222` | Puerto de depuración remota del navegador. |
| `LOGIN_TIMEOUT_SEC` | `600` | Segundos para completar el login interactivo. |
| `FX_RATES_JSON` | — | Override de tasas a USD, ej: `{"ars": 0.0009}`. |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

La suite cubre la lógica pura (parseo de fechas, matching, salario, idiomas),
la configuración, el motor de IA (con cliente Gemini simulado) y los endpoints
de la API (con scraping/IA mockeados), sin consumir cuota ni red.

## Flujo de uso

1. **Paso 1:** Sube tu CV en PDF → se genera un JSON de perfil editable (descargable).
2. **Paso 2:** Pulsa **Iniciar Búsqueda** → se scrapean ~5 ofertas y Gemini evalúa match, consejos y cover letter.

El perfil se guarda en `localStorage` del navegador: si recargás no lo perdés
ni hace falta reprocesar el CV con Gemini.

## Estructura

```
backend/     API FastAPI, motor Gemini, scraping y config centralizada
frontend/    index.html (marcado) + styles.css (estilos) + app.js (lógica)
tests/       Suite pytest (lógica, config, IA simulada, endpoints)
```

El frontend se sirve estático: `index.html` referencia `/static/styles.css`
y `/static/app.js`.

## Sesiones LinkedIn / Computrabajo (seguro)

No guardamos contraseña. Por defecto se abre **Edge/Chrome con un perfil de JobSearch**
(no cierra ni reinicia tu navegador diario). Te logueás una vez; después se reutiliza.

**Opción fácil:** doble clic en `login.bat`.

**Opción manual:**
```bash
python -m backend.login_session linkedin
python -m backend.login_session computrabajo
```

Opcional: importar cookies del perfil diario (sí puede pedir reiniciar Edge/Chrome una vez):

```bash
python -m backend.login_session linkedin --mode system --force-restart
```

Cookies en `playwright/.auth/` (ignorado por git).



- Si ambas fuentes fallan o no hay resultados, la tabla queda vacía (sin datos inventados).
- El scraping puede fallar si los sitios cambian su HTML o bloquean el acceso.
