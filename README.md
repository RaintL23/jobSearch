# AI Job Scraper & Matcher

Aplicación web que extrae el perfil de un CV (PDF) con Gemini, busca ofertas en Computrabajo, LinkedIn y otras fuentes LATAM, y muestra match, consejos y cover letter.

---

## Qué necesitás antes de empezar

| Requisito | Detalle |
| --- | --- |
| Python 3.10+ | [python.org/downloads](https://www.python.org/downloads/) — en Windows, marcá **Add Python to PATH** |
| API Key de Gemini | Gratis en [Google AI Studio](https://aistudio.google.com/apikey) |
| (Opcional) Edge o Chrome | Para iniciar sesión en LinkedIn / Computrabajo |

En free tier de Gemini, usá un modelo con cuota > 0 (p. ej. `gemini-3.1-flash-lite`). Varios Flash/Pro aparecen con límite 0.

---

## Guía rápida: instalar y arrancar

Elegí tu sistema. El resto del flujo de uso (sección siguiente) es igual en todos.

### Windows

1. Abrí la carpeta del proyecto.
2. Hacé doble clic en `start.bat`.
3. La **primera vez**:
   - Instala dependencias y Chromium (~2–5 min).
   - Crea `.env` desde la plantilla y abre el Bloc de notas.
   - Pegá tu `GOOGLE_API_KEY` (reemplazá `tu_api_key_aqui`), guardá y volvé a la ventana.
4. Las siguientes veces arranca en segundos.
5. Se abre el navegador en [http://127.0.0.1:8000](http://127.0.0.1:8000).
6. Para detener: `Ctrl+C` o cerrá la ventana de la consola.

### macOS

1. En Terminal, desde la carpeta del proyecto:

```bash
chmod +x start.command login.command
```

2. Doble clic en `start.command` (o clic derecho → Abrir).
   - Si macOS bloquea el archivo: **clic derecho → Abrir → Abrir**.
3. La primera vez instala dependencias (~2–5 min) y pide configurar `.env` con tu `GOOGLE_API_KEY`.
4. Se abre [http://127.0.0.1:8000](http://127.0.0.1:8000).

En Mac se usa **Google Chrome** si está instalado; si no, Chromium de Playwright.

### Instalación manual (Linux u otra terminal)

```bash
# Desde la raíz del proyecto
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Editá .env y pegá tu GOOGLE_API_KEY

python -m backend.run
```

Solo el API (sin abrir el navegador):

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Luego abrí [http://127.0.0.1:8000](http://127.0.0.1:8000).

---

## Cómo usar la aplicación (paso a paso)

Con el servidor corriendo y la UI abierta:

### Paso 0 — API Key de Gemini

- Si configuraste `.env` correctamente, verás el badge **API Key (.env)** en la barra superior.
- Si falta la clave, aparece un modal o el badge **Sin API Key**:
  1. Obtené una clave en [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
  2. Pegala en el modal **o** en `.env` como `GOOGLE_API_KEY=...` y reiniciá el servidor.
  - La clave del modal solo vive en memoria mientras el servidor esté activo.

Sin API Key no podés procesar el CV ni generar cover letters.

### Paso 1 — Perfil (opcional, pero recomendado)

El perfil mejora el match y rellena roles/ubicaciones por defecto. Podés buscar sin perfil, pero el % de match no se calcula.

**Opción A — Desde PDF**

1. En el panel izquierdo, abrí **1 · Perfil CV**.
2. **Subir CV (PDF)** → elegí tu curriculum.
3. Pulsá **Procesar CV** (usa Gemini; puede tardar unos segundos).
4. Revisá el JSON en el editor. Ajustá roles, skills, ubicación, etc. si hace falta.
5. Pulsá **Validar**. Cuando esté listo, el paso se marca con ✓.

**Opción B — JSON manual**

1. **JSON manual** → elegí un `.json`, o **Plantilla** para empezar desde cero.
2. Completá / editá el editor → **Validar**.
3. **Descargar** guarda una copia local del perfil.

El perfil se guarda en `localStorage`: si recargás la página no lo perdés ni hace falta reprocesar el CV.

### Paso 2 — Filtros y búsqueda

1. Abrí **2 · Filtros y búsqueda**.
2. Completá lo que necesites:

| Campo | Qué hace |
| --- | --- |
| Textos de búsqueda | Uno por línea. Vacío = usa los roles del perfil. |
| Ubicaciones | Separadas por comas o líneas. Vacío = ubicación del perfil. |
| Salario mín. / máx. USD | Filtra por rango (opcional). |
| Fuentes, fecha, experiencia, modalidad, idiomas | Chips / selects del panel. |

3. Pulsá **Iniciar búsqueda** (abajo a la izquierda).
4. En el panel central verás el progreso por fuente y, al terminar, la tabla de ofertas.

### Paso 3 — Revisar resultados

En la tabla podés:

- Filtrar por fuente o estado (**Sin revisar / Interesan / No interesan**).
- Buscar por puesto o empresa en el cuadro de búsqueda.
- Ordenar por match, fecha o salario.
- Marcar ★ (interesa) / ✕ (no interesa) / visitada — el estado se conserva al recargar.
- Abrir la oferta original.
- Pulsar **CL** para generar una cover letter con Gemini (hace falta perfil + API Key).

---

## Sesiones LinkedIn / Computrabajo (opcional, una sola vez)

Sin sesión, las fuentes públicas siguen funcionando. LinkedIn y Computrabajo rinden mejor (o solo funcionan) con login.

**No se guarda tu contraseña.** Se abre Edge/Chrome con un perfil de JobSearch; te logueás una vez y las cookies quedan en `playwright/.auth/` (ignorado por git).

### Opción fácil

1. Primero ejecutá `start.bat` / `start.command` al menos una vez (crea el entorno `.venv`).
2. Doble clic en `login.bat` (Windows) o `login.command` (macOS).
3. Elegí LinkedIn, Computrabajo o ambos.
4. Completá el login en el navegador que se abre.
5. Volvé a la UI y, si hace falta, pulsá ↻ en **Sesiones LinkedIn / Computrabajo**.

También podés iniciar sesión desde el panel de la propia UI (botón → en cada fuente).

### Opción manual

```bash
python -m backend.login_session linkedin
python -m backend.login_session computrabajo
```

Importar cookies del perfil diario del sistema (puede pedir reiniciar Edge/Chrome una vez):

```bash
python -m backend.login_session linkedin --mode system --force-restart
```

---

## Checklist de validación

1. `start.bat` / `./start.command` arranca sin error y abre la UI.
2. El badge muestra API Key configurada (`.env` o sesión).
3. Subís un CV PDF y se genera el perfil JSON.
4. (Opcional) Login LinkedIn / Computrabajo y el panel muestra sesión OK.
5. Iniciás una búsqueda y aparecen ofertas en la tabla.
6. Marcás ★ / ✕ / visitada, recargás: el estado se mantiene.
7. Generás una cover letter (**CL**) en una oferta.

---

## Configuración (`.env`)

Toda la configuración vive en `backend/config.py` y se ajusta por variables de entorno o `.env` (ver `.env.example`).

| Variable | Default | Descripción |
| --- | --- | --- |
| `GOOGLE_API_KEY` | — | Clave de Google AI Studio (**obligatoria** para IA). |
| `GEMINI_MODELS` | `gemini-3.1-flash-lite,...` | Lista de modelos separados por coma. Si uno falla por cuota (429) o no existe (404), prueba el siguiente. |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Modelo de respaldo si `GEMINI_MODELS` está vacío. |
| `DEFAULT_COUNTRY` | `mx` | País ISO2 si el perfil no lo indica (alias: `COMPUTRABAJO_COUNTRY`). |
| `AI_REQUEST_TIMEOUT_SEC` | `60` | Timeout por llamada a Gemini. |
| `AI_MAX_CV_CHARS` | `12000` | Máx. de caracteres del CV enviados al modelo. |
| `AI_MATCH_ENABLED` | `false` | Análisis batch con Gemini para ofertas GetOnBoard con ubicación ambigua. |
| `SCRAPE_SAFETY_CAP` | `70` | Tope de ofertas por búsqueda. |
| `PER_SOURCE_CAP` | `12` | Tope de ofertas por fuente de API. |
| `HTTP_TIMEOUT_SEC` | `25` | Timeout de las APIs públicas. |
| `BROWSER_CDP_PORT` | `9222` | Puerto CDP para importar sesión del sistema. |
| `LOGIN_TIMEOUT_SEC` | `600` | Segundos para completar el login interactivo. |
| `FX_RATES_JSON` | — | Override de tasas a USD, ej: `{"ars": 0.0009}`. |

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

La suite cubre lógica pura (fechas, matching, salario, idiomas), configuración, motor de IA (cliente Gemini simulado) y endpoints de la API (scraping/IA mockeados), sin consumir cuota ni red.

---

## Estructura

```
backend/     API FastAPI, motor Gemini, scraping y config
frontend/    index.html + styles.css + app.js
tests/       Suite pytest
start.bat / start.command    Arranque con setup automático
login.bat / login.command    Login LinkedIn / Computrabajo
```

El frontend se sirve estático: `index.html` referencia `/static/styles.css` y `/static/app.js`.

---

## Problemas frecuentes

| Problema | Qué hacer |
| --- | --- |
| `Python no encontrado` | Instalá Python 3.10+ y marcá Add to PATH; reabrí la terminal. |
| Badge Sin API Key / falla Procesar CV | Configurá `GOOGLE_API_KEY` en `.env` o en el modal de la UI. |
| Error 429 / cuota Gemini | Cambiá a un modelo lite en `GEMINI_MODELS` o esperá el reset de cuota. |
| Tabla vacía tras buscar | Revisá filtros; sin sesión, LinkedIn/Computrabajo pueden fallar. Corré `login.bat` / `login.command`. |
| `login.bat` dice que no hay `.venv` | Ejecutá primero `start.bat` una vez. |
| Scraping fallido | Los sitios pueden cambiar HTML o bloquear acceso; reintentá más tarde o con sesión iniciada. |

Si todas las fuentes fallan o no hay resultados, la tabla queda vacía (sin datos inventados).
