@echo off
setlocal EnableDelayedExpansion

title JobSearch - Configurar Sesion de Scraping

echo.
echo  ================================================
echo    JobSearch - Configurar Sesion de Scraping
echo  ================================================
echo.
echo  Para buscar empleos en LinkedIn y Computrabajo
echo  necesitas iniciar sesion una vez. Las cookies se
echo  guardan localmente y no se comparten con nadie.
echo.
echo  Elige el sitio:
echo.
echo    [1] LinkedIn
echo    [2] Computrabajo
echo    [3] Ambos
echo    [0] Cancelar
echo.
set /p OPCION="  Ingresa una opcion (0-3): "

if "%OPCION%"=="0" goto :FIN_CANCELADO
if "%OPCION%"=="1" goto :LOGIN_LINKEDIN
if "%OPCION%"=="2" goto :LOGIN_COMPUTRABAJO
if "%OPCION%"=="3" goto :LOGIN_AMBOS

echo  Opcion no valida.
pause
exit /b 1

:ACTIVAR_VENV
if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo  [ERROR] Entorno virtual no encontrado.
    echo  Ejecuta primero start.bat para configurar el proyecto.
    echo.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
goto :EOF

:LOGIN_LINKEDIN
call :ACTIVAR_VENV
echo.
echo  Abriendo navegador para iniciar sesion en LinkedIn...
echo  Completa el login en la ventana del navegador.
echo.
python -m backend.login_session linkedin
goto :FIN_OK

:LOGIN_COMPUTRABAJO
call :ACTIVAR_VENV
echo.
echo  Abriendo navegador para iniciar sesion en Computrabajo...
echo  Completa el login en la ventana del navegador.
echo.
python -m backend.login_session computrabajo
goto :FIN_OK

:LOGIN_AMBOS
call :ACTIVAR_VENV
echo.
echo  Paso 1/2 - LinkedIn
echo  Completa el login y cierra la ventana del navegador.
echo.
python -m backend.login_session linkedin
echo.
echo  Paso 2/2 - Computrabajo
echo  Completa el login y cierra la ventana del navegador.
echo.
python -m backend.login_session computrabajo
goto :FIN_OK

:FIN_OK
echo.
echo  Sesion configurada. Ya puedes usar start.bat.
echo.
pause
exit /b 0

:FIN_CANCELADO
echo  Cancelado.
pause
exit /b 0
