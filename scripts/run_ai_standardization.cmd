@echo off
REM Run AI-assisted standardization on Windows without activating the venv.
REM Uses .venv\Scripts\python.exe directly (no PS activation script).

setlocal EnableExtensions

set "REPO_ROOT=%~dp0.."
cd /d "%REPO_ROOT%"

set "PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Virtual environment not found. Creating .venv with Python 3.11...
    py -3.11 -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment.
        exit /b 1
    )
    echo Virtual environment created.
)

echo Installing/updating Python dependencies...
"%PYTHON%" -m pip install --upgrade pip --quiet
"%PYTHON%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo pip install failed. Check requirements.txt and network access.
    exit /b 1
)

if "%~1"=="" (
    set "PAIR_ID=pair_001_baby_and_mom__good_luck"
) else (
    set "PAIR_ID=%~1"
)

echo Running AI standardization for pair: %PAIR_ID%
"%PYTHON%" -m src.standardization.standardize_pair_with_ai --pair-id %PAIR_ID%
if errorlevel 1 (
    echo Standardization failed.
    exit /b 1
)

echo Done.
exit /b 0
