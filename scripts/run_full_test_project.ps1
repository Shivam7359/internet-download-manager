Param(
    [switch]$VerboseOutput
)

$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $pythonExe = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    if (-not (Test-Path $pythonExe)) {
        $pythonExe = "py"
    }

    $reportsDir = Join-Path (Get-Location) "test-reports"
    if (-not (Test-Path $reportsDir)) {
        New-Item -ItemType Directory -Path $reportsDir | Out-Null
    }

    $junitPath = Join-Path $reportsDir "junit.xml"

    Write-Host "Running full IDM test project..."

    if ($pythonExe -eq "py") {
        if ($VerboseOutput) {
            py -m pytest tests --junitxml "$junitPath"
        }
        else {
            py -m pytest tests -q --junitxml "$junitPath"
        }
    }
    else {
        if ($VerboseOutput) {
            & $pythonExe -m pytest tests --junitxml "$junitPath"
        }
        else {
            & $pythonExe -m pytest tests -q --junitxml "$junitPath"
        }
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Full test project failed with exit code $LASTEXITCODE"
    }

    Write-Host "All tests passed. JUnit report: $junitPath"
}
finally {
    Pop-Location
}
