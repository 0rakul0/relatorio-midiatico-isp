@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=.\.venv\Scripts\python.exe"
set "PORT=8000"

if not exist "%PYTHON%" (
    echo Ambiente nao encontrado. Rode antes: uv sync --group dev
    pause
    exit /b 1
)

netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo A porta %PORT% ja esta em uso.
    echo Feche a instancia atual antes de executar este arquivo novamente.
    pause
    exit /b 1
)

echo Iniciando o Relatorio de Repercussao Midiatica em http://127.0.0.1:%PORT%
"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo A aplicacao foi encerrada com erro %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
