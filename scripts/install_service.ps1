# Run in an elevated PowerShell on Windows Server 2019 from the repo root.
# Creates a venv, installs deps, registers the service with automatic start + restart on failure.
param([string]$Python = "py -3.12", [int]$Port = 8080)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path .venv)) { Invoke-Expression "$Python -m venv .venv" }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt pywin32
.\.venv\Scripts\python .venv\Scripts\pywin32_postinstall.py -install | Out-Null

# Service environment (keys are read from the machine environment, never committed)
[Environment]::SetEnvironmentVariable("DIAC_PORT", "$Port", "Machine")
[Environment]::SetEnvironmentVariable("DIAC_DB", "$root\data\diacritizer.db", "Machine")
New-Item -ItemType Directory -Force -Path "$root\data" | Out-Null

.\.venv\Scripts\python -m diacritizer.win_service --startup auto install
# Restart after 5s, 10s, 30s on crash; reset failure counter after 1 day
sc.exe failure ArabicDiacritizer reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
sc.exe failureflag ArabicDiacritizer 1 | Out-Null
Start-Service ArabicDiacritizer
New-NetFirewallRule -DisplayName "ArabicDiacritizer $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -ErrorAction SilentlyContinue | Out-Null
Start-Sleep 3
Invoke-RestMethod "http://localhost:$Port/health"
