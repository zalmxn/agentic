# Local startup script for both FastAPI services (no Docker required)
# Usage: .\start_services.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Uvicorn = Join-Path $Root ".venv\Scripts\uvicorn.exe"

if (-not (Test-Path $Uvicorn)) {
    Write-Error "Virtual environment not found at $Uvicorn. Create .venv and install requirements first."
    exit 1
}

Write-Host "Starting Main Backend on http://127.0.0.1:8000 ..." -ForegroundColor Cyan
$backend = Start-Process -FilePath $Uvicorn `
    -ArgumentList "app.main:app --host 127.0.0.1 --port 8000" `
    -WorkingDirectory (Join-Path $Root "main_backend") `
    -PassThru

Start-Sleep -Seconds 2

Write-Host "Starting Rule-Based AI Booking Microservice on http://127.0.0.1:8001 ..." -ForegroundColor Green
$aiService = Start-Process -FilePath $Uvicorn `
    -ArgumentList "app.main:app --host 127.0.0.1 --port 8001" `
    -WorkingDirectory (Join-Path $Root "ai_booking_service") `
    -PassThru

Write-Host ""
Write-Host "Both services are running!" -ForegroundColor Yellow
Write-Host "  Web Chat UI : http://127.0.0.1:8000/"
Write-Host "  Main Docs   : http://127.0.0.1:8000/docs"
Write-Host "  AI Docs     : http://127.0.0.1:8001/docs"
Write-Host ""
Write-Host "Press Ctrl+C to stop both services..."

try {
    Wait-Process -Id $backend.Id, $aiService.Id
} finally {
    if (-not $backend.HasExited) { Stop-Process -Id $backend.Id -Force }
    if (-not $aiService.HasExited) { Stop-Process -Id $aiService.Id -Force }
}
