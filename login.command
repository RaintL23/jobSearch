#!/bin/bash
# JobSearch - configurar sesion LinkedIn / Computrabajo (macOS / Linux)

cd "$(dirname "$0")" || exit 1

echo ""
echo "  ================================================"
echo "    JobSearch - Configurar Sesion de Scraping"
echo "  ================================================"
echo ""
echo "  Para buscar empleos en LinkedIn y Computrabajo"
echo "  necesitas iniciar sesion una vez. Las cookies se"
echo "  guardan localmente y no se comparten con nadie."
echo ""
echo "  Elige el sitio:"
echo ""
echo "    [1] LinkedIn"
echo "    [2] Computrabajo"
echo "    [3] Ambos"
echo "    [0] Cancelar"
echo ""
read -r -p "  Ingresa una opcion (0-3): " OPCION

activate_venv() {
  if [ ! -f ".venv/bin/activate" ]; then
    echo ""
    echo "  [ERROR] Entorno virtual no encontrado."
    echo "  Ejecuta primero start.command para configurar el proyecto."
    echo ""
    read -r -p "  Presiona Enter para cerrar..."
    exit 1
  fi
  # shellcheck source=/dev/null
  source .venv/bin/activate
}

case "$OPCION" in
  0)
    echo "  Cancelado."
    read -r -p "  Presiona Enter para cerrar..."
    exit 0
    ;;
  1)
    activate_venv
    echo ""
    echo "  Abriendo navegador para iniciar sesion en LinkedIn..."
    echo "  Completa el login en la ventana del navegador."
    echo ""
    python -m backend.auth.login linkedin
    ;;
  2)
    activate_venv
    echo ""
    echo "  Abriendo navegador para iniciar sesion en Computrabajo..."
    echo "  Completa el login en la ventana del navegador."
    echo ""
    python -m backend.auth.login computrabajo
    ;;
  3)
    activate_venv
    echo ""
    echo "  Paso 1/2 - LinkedIn"
    echo "  Completa el login y cierra la ventana del navegador."
    echo ""
    python -m backend.auth.login linkedin
    echo ""
    echo "  Paso 2/2 - Computrabajo"
    echo "  Completa el login y cierra la ventana del navegador."
    echo ""
    python -m backend.auth.login computrabajo
    ;;
  *)
    echo "  Opcion no valida."
    read -r -p "  Presiona Enter para cerrar..."
    exit 1
    ;;
esac

echo ""
echo "  Sesion configurada. Ya puedes usar start.command."
echo ""
read -r -p "  Presiona Enter para cerrar..."
