#!/bin/bash
# JobSearch - launcher macOS / Linux
# Doble clic en Finder (macOS): click derecho > Abrir, o Terminal: ./start.command

cd "$(dirname "$0")" || exit 1

echo ""
echo "  ================================================"
echo "    JobSearch - Buscador de Empleo con IA"
echo "  ================================================"
echo ""

# 1. Detectar Python
PYTHON=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
fi

if [ -z "$PYTHON" ]; then
  echo "  [ERROR] Python no encontrado."
  echo ""
  echo "  Instala Python 3.10+ desde:"
  echo "  https://www.python.org/downloads/  o  brew install python"
  echo ""
  read -r -p "  Presiona Enter para cerrar..."
  exit 1
fi

PYVER=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "  Python $PYVER detectado ($PYTHON)."

# 2. Crear entorno virtual
if [ ! -f ".venv/bin/activate" ]; then
  echo "  Creando entorno virtual .venv ..."
  $PYTHON -m venv .venv
  if [ $? -ne 0 ]; then
    echo "  [ERROR] No se pudo crear el entorno virtual."
    read -r -p "  Presiona Enter para cerrar..."
    exit 1
  fi
  echo "  Entorno virtual creado."
fi

# 3. Activar
# shellcheck source=/dev/null
source .venv/bin/activate
if [ $? -ne 0 ]; then
  echo "  [ERROR] No se pudo activar el entorno virtual."
  read -r -p "  Presiona Enter para cerrar..."
  exit 1
fi

# 4. Setup primera vez
if [ ! -f ".venv/.setup_done" ]; then
  echo ""
  echo "  Primera ejecucion: instalando dependencias..."
  echo "  Puede tardar entre 2 y 5 minutos segun tu conexion."
  echo ""

  pip install -r requirements.txt --disable-pip-version-check
  if [ $? -ne 0 ]; then
    echo ""
    echo "  [ERROR] Fallo al instalar las dependencias."
    read -r -p "  Presiona Enter para cerrar..."
    exit 1
  fi

  echo ""
  echo "  Instalando navegador Chromium para el scraping..."
  playwright install chromium
  if [ $? -ne 0 ]; then
    echo ""
    echo "  [ERROR] Fallo al instalar Chromium."
    read -r -p "  Presiona Enter para cerrar..."
    exit 1
  fi

  echo "setup_done" > .venv/.setup_done
  echo ""
  echo "  Configuracion inicial completada."
fi

# 5. Crear .env si falta
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    echo ""
    echo "  ================================================"
    echo "   CONFIGURACION REQUERIDA"
    echo ""
    echo "   Se creo .env desde la plantilla."
    echo "   Debes agregar tu GOOGLE_API_KEY."
    echo ""
    echo "   Obtenla gratis en:"
    echo "   https://aistudio.google.com/apikey"
    echo "  ================================================"
    echo ""
    if command -v open >/dev/null 2>&1; then
      open -e .env
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open .env
    else
      ${EDITOR:-nano} .env
    fi
    echo ""
    read -r -p "  Presiona Enter cuando hayas guardado la API key..."
  else
    echo "  [ERROR] No se encontro .env.example."
    read -r -p "  Presiona Enter para cerrar..."
    exit 1
  fi
fi

# 6. Advertir placeholder
if grep -qi "GOOGLE_API_KEY=tu_api_key_aqui" .env 2>/dev/null; then
  echo ""
  echo "  [ADVERTENCIA] La GOOGLE_API_KEY es el valor de ejemplo."
  echo "  Las funciones de IA no van a funcionar hasta que la configures."
  echo ""
  sleep 3
fi

# 7. Arrancar y abrir navegador (se cierra la UI al detener el server)
echo ""
echo "  Iniciando servidor en http://127.0.0.1:8000"
echo "  Presiona Ctrl+C o cierra esta ventana para detener."
echo ""

python -m backend.run

echo ""
echo "  Servidor detenido."
read -r -p "  Presiona Enter para cerrar..."
