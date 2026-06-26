# Kanban Harness CLI Installer for Windows
# Usage: irm https://aipitabox.site/docker-images/install-cli.ps1 | iex

$ErrorActionPreference = "Stop"
$KH_DIR = "$env:USERPROFILE\kanban-harness"
$KH_LIB = "$KH_DIR\.kh"
$KH_SCRIPT = "$KH_LIB\kh.ps1"

Write-Host ""
Write-Host "  Installing Kanban Harness CLI..." -ForegroundColor Cyan
Write-Host ""

# Create directories
if (-not (Test-Path $KH_DIR)) { New-Item -ItemType Directory -Path $KH_DIR | Out-Null }
if (-not (Test-Path $KH_LIB)) { New-Item -ItemType Directory -Path $KH_LIB | Out-Null }

# Download kh.ps1 to hidden subfolder
Invoke-WebRequest -Uri "https://aipitabox.site/docker-images/kh.ps1" -OutFile $KH_SCRIPT

# Create kh.cmd wrapper (this is what PATH resolves to)
$wrapperContent = "@echo off`npowershell -NoProfile -ExecutionPolicy Bypass -File `"$KH_SCRIPT`" %*"
Set-Content -Path "$KH_DIR\kh.cmd" -Value $wrapperContent -Encoding ASCII

# Remove old kh.ps1 from KH_DIR if exists (prevents PowerShell from finding it)
if (Test-Path "$KH_DIR\kh.ps1") { Remove-Item "$KH_DIR\kh.ps1" -Force }

# Add to PATH if not already there
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$KH_DIR*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$KH_DIR", "User")
    $env:Path = "$env:Path;$KH_DIR"
}

Write-Host "  [OK] kh 命令已安装" -ForegroundColor Green
Write-Host ""
Write-Host "  用法:"
Write-Host "    kh install   — 首次安装"
Write-Host "    kh start     — 启动服务"
Write-Host "    kh stop      — 停止服务"
Write-Host "    kh update    — 更新版本"
Write-Host "    kh status    — 查看状态"
Write-Host ""
Write-Host "  请关闭此窗口，重新打开 PowerShell，然后运行: kh install" -ForegroundColor Yellow
Write-Host ""
