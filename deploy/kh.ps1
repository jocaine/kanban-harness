$ErrorActionPreference = "Stop"
$KH_DIR = "$env:USERPROFILE\kanban-harness"
$BASE_URL = "https://aipitabox.site/docker-images"

function Show-Help {
    Write-Host ""
    Write-Host "  Kanban Harness CLI (Windows)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Usage: kh <command>"
    Write-Host ""
    Write-Host "  Commands:"
    Write-Host "    install    First-time setup (download image, configure, start)"
    Write-Host "    start      Start the service"
    Write-Host "    stop       Stop the service"
    Write-Host "    update     Update to latest version"
    Write-Host "    status     Show running status"
    Write-Host "    logs       Show logs"
    Write-Host "    config     Reconfigure API key/url/model"
    Write-Host "    uninstall  Remove container and image (keeps data)"
    Write-Host ""
}

function Ensure-Docker {
    $dockerExists = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerExists) {
        Write-Host ""
        Write-Host "  [!] Docker not found." -ForegroundColor Red
        Write-Host ""
        Write-Host "  Two options to get Docker on Windows:"
        Write-Host ""
        Write-Host "    [1] WSL + Linux Docker (recommended, works in China)" -ForegroundColor Cyan
        Write-Host "        Install WSL, then use Linux commands inside WSL."
        Write-Host ""
        Write-Host "    [2] Docker Desktop (requires access to docker.com)" -ForegroundColor Cyan
        Write-Host "        GUI app, installs docker command directly on Windows."
        Write-Host ""
        $choice = Read-Host "  Enter 1 or 2"
        if ($choice -eq "2") {
            Write-Host ""
            Write-Host "  Opening Docker Desktop download page..." -ForegroundColor Cyan
            Start-Process "https://www.docker.com/products/docker-desktop/"
            Write-Host ""
            Write-Host "  After installing Docker Desktop, rerun: kh install" -ForegroundColor Yellow
            return $false
        }
        # WSL path
        Write-Host ""
        Write-Host "  === Installing WSL ===" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  [*] Enabling WSL (admin prompt may appear, click Yes)..."
        Start-Process wsl -ArgumentList "--install" -Verb RunAs -Wait
        Write-Host ""
        Write-Host "  [OK] WSL installed." -ForegroundColor Green
        Write-Host ""
        Write-Host "  Next steps:" -ForegroundColor Yellow
        Write-Host "    1. Reboot your computer"
        Write-Host "    2. After reboot, open Ubuntu from Start Menu"
        Write-Host "    3. Set a username and password (password won't show when typing)"
        Write-Host "    4. Inside Ubuntu terminal, run the Linux install command:"
        Write-Host ""
        Write-Host "       curl -fsSL $BASE_URL/install-cli.sh | bash" -ForegroundColor White
        Write-Host "       kh install" -ForegroundColor White
        Write-Host ""
        Write-Host "    Then access http://localhost:8765 from your Windows browser."
        Write-Host ""
        return $false
    }

    # Docker exists, check if running
    $info = docker info 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [*] Waiting for Docker to start..."
        $attempts = 0
        do {
            Start-Sleep -Seconds 3
            $info = docker info 2>&1
            $attempts++
            if ($attempts -gt 20) {
                Write-Host "  [!] Docker not responding. Please start Docker Desktop manually." -ForegroundColor Red
                return $false
            }
        } while ($LASTEXITCODE -ne 0)
    }
    Write-Host "  [OK] Docker ready" -ForegroundColor Green
    return $true
}

function Do-Config {
    Write-Host ""
    Write-Host "  === API Configuration ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Supports OpenAI / Anthropic / DeepSeek / any compatible provider"
    Write-Host ""

    do {
        $key = Read-Host "  [1/3] API Key"
        if (-not $key) { Write-Host "        Cannot be empty" -ForegroundColor Red }
    } while (-not $key)

    Write-Host ""
    do {
        $base = Read-Host "  [2/3] API URL (e.g. https://api.openai.com/v1)"
        if (-not $base) { Write-Host "        Cannot be empty" -ForegroundColor Red }
    } while (-not $base)

    Write-Host ""
    do {
        $model = Read-Host "  [3/3] Model name (e.g. claude-sonnet-4-6, gpt-4o)"
        if (-not $model) { Write-Host "        Cannot be empty" -ForegroundColor Red }
    } while (-not $model)

    $lines = @(
        "API_KEY=$key",
        "API_BASE_URL=$base",
        "CHAT_MODEL=$model",
        "ANTHROPIC_AUTH_TOKEN=$key",
        "ANTHROPIC_BASE_URL=$base"
    )
    $lines | Set-Content -Path "$KH_DIR\.env" -Encoding ASCII

    Write-Host ""
    Write-Host "  [OK] Config saved" -ForegroundColor Green
}

function Do-Install {
    Write-Host ""
    Write-Host "  === Kanban Harness Install ===" -ForegroundColor Yellow
    Write-Host ""

    $dockerReady = Ensure-Docker
    if (-not $dockerReady) { return }

    if (-not (Test-Path $KH_DIR)) { New-Item -ItemType Directory -Path $KH_DIR | Out-Null }
    Set-Location $KH_DIR

    $imageExists = docker image inspect kh-web 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [*] Pulling Docker image..." -ForegroundColor Cyan
        docker pull crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest
        docker tag crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest kh-web
        Write-Host "  [OK] Image ready" -ForegroundColor Green
    } else {
        Write-Host "  [OK] Image already exists, skipping download" -ForegroundColor Green
    }

    if (-not (Test-Path "$KH_DIR\.env")) {
        Do-Config
    }

    Write-Host ""
    Write-Host "  [OK] Install complete!" -ForegroundColor Green
    Write-Host "  Working directory: $KH_DIR"
    Write-Host ""
    Write-Host "  Run: kh start"
    Write-Host ""
}

function Do-Start {
    $dockerReady = Ensure-Docker
    if (-not $dockerReady) { return }

    Set-Location $KH_DIR
    if (-not (Test-Path ".env")) {
        Write-Host "  [!] No config found. Run: kh install" -ForegroundColor Red
        return
    }

    $running = docker ps -q --filter "name=kanban-harness" 2>$null
    if ($running) {
        Write-Host "  [OK] Already running" -ForegroundColor Green
        Write-Host "  http://localhost:8765"
        return
    }

    $exists = docker ps -aq --filter "name=kanban-harness" 2>$null
    if ($exists) {
        Write-Host "  [*] Starting..."
        docker start kanban-harness | Out-Null
    } else {
        Write-Host "  [*] Creating and starting..."
        if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }
        docker run -d --name kanban-harness --network host --env-file .env -v "${KH_DIR}\data:/app/data" kh-web | Out-Null
    }

    Start-Sleep -Seconds 2
    Write-Host ""
    Write-Host "  [OK] Started! http://localhost:8765" -ForegroundColor Green
    Start-Process "http://localhost:8765"
    Write-Host ""
}

function Do-Stop {
    $result = docker ps -q --filter "name=kanban-harness" 2>$null
    if ($result) {
        docker stop kanban-harness | Out-Null
        Write-Host "  [OK] Stopped" -ForegroundColor Green
    } else {
        # 也检查已停止的容器
        $stopped = docker ps -aq --filter "name=kanban-harness" 2>$null
        if ($stopped) {
            Write-Host "  [OK] Already stopped"
        } else {
            Write-Host "  [OK] No container found. Run: kh install"
        }
    }
}

function Do-Update {
    Write-Host ""
    Write-Host "  [*] Checking for updates..." -ForegroundColor Cyan

    $dockerReady = Ensure-Docker
    if (-not $dockerReady) { return }

    Set-Location $KH_DIR

    Write-Host "  [*] Pulling latest image..."
    docker pull crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest
    docker tag crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest kh-web
    Write-Host "  [OK] Image updated" -ForegroundColor Green

    $exists = docker ps -aq --filter "name=kanban-harness" 2>$null
    if ($exists) {
        Write-Host "  [*] Rebuilding container..."
        docker rm -f kanban-harness | Out-Null
        Do-Start
    } else {
        Write-Host ""
        Write-Host "  [OK] Update complete. Run: kh start" -ForegroundColor Green
    }
}

function Do-Status {
    $running = docker ps -q --filter "name=kanban-harness" 2>$null
    if ($running) {
        Write-Host "  Status: Running" -ForegroundColor Green
        Write-Host "  URL: http://localhost:8765"
        docker ps --filter name=kanban-harness --format "  Container: {{.ID}}  Uptime: {{.RunningFor}}"
    } else {
        $exists = docker ps -aq --filter "name=kanban-harness" 2>$null
        if ($exists) {
            Write-Host "  Status: Stopped" -ForegroundColor Yellow
            Write-Host "  Run: kh start"
        } else {
            Write-Host "  Status: Not installed" -ForegroundColor Red
            Write-Host "  Run: kh install"
        }
    }
}

function Do-Logs {
    docker logs -f --tail 50 kanban-harness
}

function Do-Uninstall {
    Write-Host ""
    $confirm = Read-Host "  Remove container and image? Data in data/ will be kept. [y/N]"
    if ($confirm -ne "y" -and $confirm -ne "Y") {
        Write-Host "  Cancelled"
        return
    }
    docker rm -f kanban-harness 2>$null
    docker rmi kh-web 2>$null
    Write-Host "  [OK] Uninstalled. Data kept in $KH_DIR\data\" -ForegroundColor Green
}

switch ($args[0]) {
    "install"   { Do-Install }
    "start"     { Do-Start }
    "stop"      { Do-Stop }
    "update"    { Do-Update }
    "status"    { Do-Status }
    "logs"      { Do-Logs }
    "config"    { Do-Config }
    "uninstall" { Do-Uninstall }
    default     { Show-Help }
}
