# NVR Viewer — Install Script (Windows)
# Run: powershell -ExecutionPolicy Bypass -File scripts\install.ps1

param(
    [switch]$Dev,
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"

Write-Host "=== NVR Viewer Installer ===" -ForegroundColor Cyan
Write-Host ""

# Check Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[ERROR] Python not found. Install Python 3.10+ from https://python.org" -ForegroundColor Red
    exit 1
}

$pyVer = python --version 2>&1
Write-Host "[OK] $pyVer" -ForegroundColor Green

# Navigate to project root
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
Set-Location $projectDir
Write-Host "[OK] Project: $projectDir" -ForegroundColor Green

# Create virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "[...] Creating virtual environment..."
    python -m venv .venv
}
Write-Host "[OK] Virtual environment ready" -ForegroundColor Green

# Activate and install
Write-Host "[...] Installing dependencies..."
& .\.venv\Scripts\pip.exe install --quiet --upgrade pip
& .\.venv\Scripts\pip.exe install --quiet -e "."

if ($Dev) {
    Write-Host "[...] Installing dev dependencies..."
    & .\.venv\Scripts\pip.exe install --quiet -e ".[dev]"
}

Write-Host "[OK] Dependencies installed" -ForegroundColor Green

# Create data directories
$dataDir = Join-Path $env:USERPROFILE ".nvr-viewer"
$dirs = @("recordings", "snapshots", "models")
foreach ($d in $dirs) {
    $p = Join-Path $dataDir $d
    if (-not (Test-Path $p)) {
        New-Item -ItemType Directory -Path $p -Force | Out-Null
    }
}
Write-Host "[OK] Data directories created at $dataDir" -ForegroundColor Green

# Download YOLO model
if (-not $SkipModels) {
    $modelPath = Join-Path $dataDir "models\yolov8n.pt"
    if (-not (Test-Path $modelPath)) {
        Write-Host "[...] Downloading YOLOv8 nano model (~6 MB)..."
        try {
            & .\.venv\Scripts\python.exe -c "from ultralytics import YOLO; m = YOLO('yolov8n.pt'); print('Model downloaded')"
            # Move to our models dir
            if (Test-Path "yolov8n.pt") {
                Move-Item "yolov8n.pt" $modelPath -Force
            }
            Write-Host "[OK] YOLO model ready" -ForegroundColor Green
        } catch {
            Write-Host "[WARN] Model download failed — will download on first use" -ForegroundColor Yellow
        }
    } else {
        Write-Host "[OK] YOLO model already present" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Usage:" -ForegroundColor White
Write-Host "  .\.venv\Scripts\activate" -ForegroundColor Yellow
Write-Host "  nvr-viewer scan                           # Find cameras on network" -ForegroundColor Yellow
Write-Host "  nvr-viewer creds set --host 192.168.1.3 -p PASSWORD  # Store credentials" -ForegroundColor Yellow
Write-Host "  nvr-viewer view --discover                # Auto-detect and view" -ForegroundColor Yellow
Write-Host "  nvr-viewer view -c 192.168.1.3 -p PASSWORD   # View specific camera" -ForegroundColor Yellow
Write-Host "  nvr-viewer events                         # View detection events" -ForegroundColor Yellow
Write-Host ""
