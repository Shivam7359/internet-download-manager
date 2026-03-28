$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $exePath = Join-Path (Get-Location) "dist\IDM\IDM.exe"
    if (-not (Test-Path $exePath)) {
        throw "Packaged executable not found at $exePath. Run scripts/build_windows.ps1 first."
    }

    Write-Host "Launching packaged app for smoke check..."
    $process = Start-Process -FilePath $exePath -PassThru
    Start-Sleep -Seconds 6

    if (-not $process.HasExited) {
        Write-Host "App started successfully. Stopping process..."
        Stop-Process -Id $process.Id -Force
    }
    else {
        Write-Host "App exited quickly with code $($process.ExitCode). Check logs in dist/IDM/logs."
    }
}
finally {
    Pop-Location
}
