# Local startup script for both FastAPI services (no Docker required)
# Usage: .\start_services.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment not found at $Python. Create .venv and install requirements first."
    exit 1
}

Write-Host "Starting Main Backend on http://127.0.0.1:8000 ..." -ForegroundColor Cyan
$backend = Start-Process -FilePath $Python `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory (Join-Path $Root "main_backend") `
    -PassThru

Start-Sleep -Seconds 2
if ($backend.HasExited) {
    Write-Error "Main Backend exited unexpectedly (exit code $($backend.ExitCode)). Check if port 8000 is already in use or run manually to see logs."
    exit 1
}

Write-Host "Starting Rule-Based AI Booking Microservice on http://127.0.0.1:8001 ..." -ForegroundColor Green
$aiService = Start-Process -FilePath $Python `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001" `
    -WorkingDirectory (Join-Path $Root "ai_booking_service") `
    -PassThru

Start-Sleep -Seconds 2
if ($aiService.HasExited) {
    if (-not $backend.HasExited) { Stop-Process -Id $backend.Id -Force }
    Write-Error "AI Booking Microservice exited unexpectedly (exit code $($aiService.ExitCode)). Check if port 8001 is already in use or run manually to see logs."
    exit 1
}

Write-Host ""
Write-Host "Both services are running!" -ForegroundColor Yellow
Write-Host "  Web Chat UI : http://127.0.0.1:8000/"
Write-Host "  Main Docs   : http://127.0.0.1:8000/docs"
Write-Host "  AI Docs     : http://127.0.0.1:8001/docs"
Write-Host ""
Write-Host "Press Ctrl+C to stop both services..."

try {
    while (-not $backend.HasExited -and -not $aiService.HasExited) {
        Start-Sleep -Milliseconds 500
    }
    if ($backend.HasExited) {
        Write-Warning "Main Backend stopped (exit code $($backend.ExitCode))."
    }
    if ($aiService.HasExited) {
        Write-Warning "AI Booking Microservice stopped (exit code $($aiService.ExitCode))."
    }
} finally {
    if ($backend -and -not $backend.HasExited) { Stop-Process -Id $backend.Id -Force }
    if ($aiService -and -not $aiService.HasExited) { Stop-Process -Id $aiService.Id -Force }
}
