# Run AI-assisted standardization on Windows without activating the venv.
# Uses .venv\Scripts\python.exe directly (no PS activation script, no ExecutionPolicy change).
param(
    [string]$PairId = "pair_001_baby_and_mom__good_luck"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "Virtual environment not found. Creating .venv with Python 3.11..."
    py -3.11 -m venv .venv
    if (-not (Test-Path $Python)) {
        Write-Error "Failed to create virtual environment at $Python"
    }
    Write-Host "Virtual environment created."
}

Write-Host "Installing/updating Python dependencies..."
& $Python -m pip install --upgrade pip --quiet
& $Python -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed. Check requirements.txt and network access."
}

Write-Host "Running AI standardization for pair: $PairId"
& $Python -m src.standardization.standardize_pair_with_ai --pair-id $PairId
if ($LASTEXITCODE -ne 0) {
    Write-Error "Standardization failed with exit code $LASTEXITCODE"
}

Write-Host "Done."
