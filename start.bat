@echo off
setlocal EnableDelayedExpansion

title JobSearch - Buscador de Empleo con IA

echo.
echo  ================================================
echo    JobSearch - Buscador de Empleo con IA
echo  ================================================
echo.

REM 1. Verificar Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python no encontrado en el sistema.
    echo.
    echo  Instala Python 3.10+ desde: https://python.org/downloads
    echo  Durante la instalacion marca "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  Python %PYVER% detectado.

REM 2. Crear entorno virtual si no existe
if not exist ".venv\Scripts\activate.bat" (
    echo  Creando entorno virtual .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo  [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
    echo  Entorno virtual creado.
)

REM 3. Activar entorno virtual
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo  [ERROR] No se pudo activar el entorno virtual.
    pause
    exit /b 1
)

REM 4. Instalar dependencias (solo la primera vez)
if not exist ".venv\.setup_done" (
    echo.
    echo  Primera ejecucion: instalando dependencias...
    echo  Puede tardar entre 2 y 5 minutos segun tu conexion.
    echo.

    pip install -r requirements.txt --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo  [ERROR] Fallo al instalar las dependencias.
        echo  Revisa tu conexion a internet y vuelve a intentarlo.
        pause
        exit /b 1
    )

    echo.
    echo  Instalando navegador Chromium para el scraping...
    playwright install chromium
    if errorlevel 1 (
        echo.
        echo  [ERROR] Fallo al instalar Chromium.
        pause
        exit /b 1
    )

    echo setup_done > .venv\.setup_done
    echo.
    echo  Configuracion inicial completada.
)

REM 5. Crear .env si no existe
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo.
        echo  ================================================
        echo   CONFIGURACION REQUERIDA
        echo.
        echo   Se creo .env desde la plantilla.
        echo   Debes agregar tu GOOGLE_API_KEY.
        echo.
        echo   Obtenla gratis en:
        echo   https://aistudio.google.com/apikey
        echo  ================================================
        echo.
        echo  Abriendo .env en el Bloc de notas...
        timeout /t 2 /nobreak >nul
        notepad .env
        echo.
        echo  Presiona cualquier tecla cuando hayas guardado la API key...
        pause >nul
    ) else (
        echo.
        echo  [ERROR] No se encontro .env.example.
        pause
        exit /b 1
    )
)

REM 6. Advertir si la API key es el placeholder
findstr /i "GOOGLE_API_KEY=tu_api_key_aqui" ".env" >nul 2>&1
if %errorlevel% equ 0 (
    echo.
    echo  [ADVERTENCIA] La GOOGLE_API_KEY es el valor de ejemplo.
    echo  Las funciones de IA no van a funcionar hasta que la configures.
    echo.
    timeout /t 3 /nobreak >nul
)

REM 7. Arrancar servidor y abrir navegador (se cierra la UI al detener el server)
echo.
echo  Iniciando servidor en http://127.0.0.1:8000
echo  Presiona Ctrl+C o cierra esta ventana para detener.
echo.

python -m backend.run

echo.
echo  Servidor detenido.
pause
