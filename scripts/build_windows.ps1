Param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $pythonExe = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    if (-not (Test-Path $pythonExe)) {
        $pythonExe = "py"
    }

    if ($Clean) {
        if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
        if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
    }

    Write-Host "Installing build dependencies..."
    if ($pythonExe -eq "py") {
        py -m pip install -U pip
        py -m pip install -r requirements.txt
        py -m pip install "pyinstaller>=6.10.0" pytest pytest-asyncio
    }
    else {
        & $pythonExe -m pip install -U pip
        & $pythonExe -m pip install -r requirements.txt
        & $pythonExe -m pip install "pyinstaller>=6.10.0" pytest pytest-asyncio
    }

    Write-Host "Running packaging smoke tests..."
    if ($pythonExe -eq "py") {
        py -m pytest -q tests/test_main.py tests/test_server.py
    }
    else {
        & $pythonExe -m pytest -q tests/test_main.py tests/test_server.py
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Pytest smoke tests failed with exit code $LASTEXITCODE"
    }

    Write-Host "Building Windows distribution with PyInstaller..."
    if ($pythonExe -eq "py") {
        py -m PyInstaller --noconfirm idm.spec
    }
    else {
        & $pythonExe -m PyInstaller --noconfirm idm.spec
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }

    Write-Host "Build completed. Output directory: dist/IDM"
}
finally {
    Pop-Location
}
