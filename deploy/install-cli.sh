#!/bin/bash
set -eo pipefail

BASE_URL="https://aipitabox.site/docker-images"
KH_DIR="${KH_HOME:-$HOME/kanban-harness}"

show_help() {
    echo ""
    echo "  Kanban Harness CLI"
    echo ""
    echo "  Usage: kh <command>"
    echo ""
    echo "  Commands:"
    echo "    install    First-time setup (download image, configure, start)"
    echo "    start      Start the service"
    echo "    stop       Stop the service"
    echo "    update     Update to latest version"
    echo "    status     Show running status"
    echo "    logs       Show logs"
    echo "    config     Reconfigure API key/url/model"
    echo "    uninstall  Remove container and image (keeps data)"
    echo ""
}

wait_apt_lock() {
    local i=0
    while sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend 2>/dev/null; do
        if [ $i -eq 0 ]; then echo "  [*] Waiting for apt lock..."; fi
        i=$((i+1))
        sleep 2
        if [ $i -gt 30 ]; then echo "  [!] apt lock timeout"; exit 1; fi
    done
}

install_docker_cn() {
    local distro
    distro=$(. /etc/os-release && echo "$ID")
    case "$distro" in
        ubuntu|debian)
            wait_apt_lock
            sudo apt-get update -qq
            sudo apt-get install -y -qq ca-certificates curl gnupg >/dev/null
            sudo install -m 0755 -d /etc/apt/keyrings
            curl -fsSL "https://mirrors.aliyun.com/docker-ce/linux/$distro/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            sudo chmod a+r /etc/apt/keyrings/docker.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/$distro $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
            sudo apt-get update -qq
            sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io >/dev/null
            ;;
        centos|rhel|fedora)
            sudo yum install -y -q yum-utils >/dev/null
            sudo yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo >/dev/null
            sudo yum install -y -q docker-ce docker-ce-cli containerd.io >/dev/null
            ;;
        *)
            echo "  [!] Unsupported distro: $distro"
            echo "  Please install Docker manually: https://docs.docker.com/engine/install/"
            exit 1
            ;;
    esac
}

ensure_docker() {
    if ! command -v docker &>/dev/null; then
        echo "  [*] Docker not found, installing..."
        echo ""
        if grep -qi microsoft /proc/version 2>/dev/null; then
            echo "  [*] WSL detected, installing Docker Engine directly..."
            install_docker_cn
        elif curl -fsSL --connect-timeout 5 https://get.docker.com -o /dev/null 2>/dev/null; then
            curl -fsSL https://get.docker.com | sh
        else
            echo "  [*] get.docker.com unreachable, using Aliyun mirror..."
            install_docker_cn
        fi
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER"
        echo ""
        echo "  [OK] Docker installed"
        echo "  [!] If you get permission errors, run: newgrp docker"
        echo ""
    fi
    if ! docker info &>/dev/null 2>&1; then
        sudo systemctl start docker
        sleep 2
    fi
}

install_kh_cli() {
    local target_dir="${XDG_DATA_HOME:-$HOME/.local}/bin"
    mkdir -p "$target_dir"

    if [ -f "$target_dir/kh" ]; then
        echo "  [*] Updating kh CLI..."
    else
        echo "  [*] Installing kh CLI..."
    fi

    # Download self from server (can't rely on $0 when piped via curl | bash)
    if ! curl -fsSL "$BASE_URL/install-cli.sh" -o "$target_dir/kh"; then
        echo "  [!] Failed to download kh CLI"
        return 1
    fi
    chmod +x "$target_dir/kh"

    echo "  [OK] kh CLI installed to $target_dir/kh"

    if ! echo ":$PATH:" | grep -qF ":$target_dir:"; then
        local rc_file=""
        if [ -n "$BASH_VERSION" ]; then
            rc_file="$HOME/.bashrc"
        elif [ -n "$ZSH_VERSION" ]; then
            rc_file="$HOME/.zshrc"
        fi
        if [ -n "$rc_file" ] && [ -f "$rc_file" ]; then
            echo "export PATH=\"$target_dir:\$PATH\"" >> "$rc_file"
            echo "  [*] Added $target_dir to PATH in $rc_file"
        fi
        echo "  [!] $target_dir not in PATH. For current session run: export PATH=\"$target_dir:\$PATH\""
    fi
}

do_install() {
    echo ""
    echo "  === Kanban Harness Install ==="
    echo ""

    ensure_docker

    DOCKER_CMD="docker"
    if ! docker info &>/dev/null 2>&1; then
        DOCKER_CMD="sudo docker"
    fi

    mkdir -p "$KH_DIR"
    cd "$KH_DIR"

    echo "  [*] Downloading scripts..."
    curl -fsSL "$BASE_URL/start.sh" -o start.sh
    curl -fsSL "$BASE_URL/stop.sh" -o stop.sh
    chmod +x start.sh stop.sh

    if ! $DOCKER_CMD image inspect kh-web &>/dev/null 2>&1; then
        echo "  [*] Pulling Docker image..."
        $DOCKER_CMD pull crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest
        $DOCKER_CMD tag crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest kh-web
        echo "  [OK] Image ready"
    else
        echo "  [OK] Image already exists, skipping download"
    fi

    install_kh_cli

    if [ ! -f .env ]; then
        do_config
    fi

    echo ""
    echo "  [OK] Install complete!"
    echo "  Working directory: $KH_DIR"
    echo ""
    echo "  Use 'kh start' to launch the service"
    echo "  If kh is not found, restart your terminal or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
}

do_config() {
    echo ""
    echo "  === API Configuration ==="
    echo ""
    echo "  Select API provider:"
    echo ""
    echo "    [1] OpenAI / OpenAI-compatible proxy"
    echo "    [2] Anthropic / Anthropic-compatible proxy"
    echo ""

    local provider=""
    while [ -z "$provider" ]; do
        read -p "  Provider [1/2]: " pchoice
        case "$pchoice" in
            1) provider="openai" ;;
            2) provider="anthropic" ;;
            *) echo "        Please enter 1 or 2" ;;
        esac
    done

    echo ""
    local key=""
    while [ -z "$key" ]; do
        read -p "  [1/3] API Key: " key
        [ -z "$key" ] && echo "        Cannot be empty"
    done

    local base=""
    while [ -z "$base" ]; do
        echo ""
        if [ "$provider" = "openai" ]; then
            echo "  Hint: e.g. https://api.openai.com/v1"
        else
            echo "  Hint: e.g. https://api.anthropic.com"
        fi
        read -p "  [2/3] API URL: " base
        [ -z "$base" ] && echo "        Cannot be empty"
    done

    local model=""
    while [ -z "$model" ]; do
        echo ""
        if [ "$provider" = "openai" ]; then
            echo "  Hint: e.g. gpt-4o, deepseek-chat"
        else
            echo "  Hint: e.g. claude-sonnet-4-6"
        fi
        read -p "  [3/3] Model name: " model
        [ -z "$model" ] && echo "        Cannot be empty"
    done

    if [ "$provider" = "openai" ]; then
        cat > "$KH_DIR/.env" << EOF
API_PROVIDER=openai
OPENAI_API_KEY=$key
OPENAI_BASE_URL=$base
API_KEY=$key
API_BASE_URL=$base
CHAT_MODEL=$model
EOF
    else
        cat > "$KH_DIR/.env" << EOF
API_PROVIDER=anthropic
API_KEY=$key
ANTHROPIC_API_KEY=$key
ANTHROPIC_AUTH_TOKEN=$key
API_BASE_URL=$base
ANTHROPIC_BASE_URL=$base
CHAT_MODEL=$model
EOF
    fi

    echo ""
    echo "  [OK] Config saved ($provider)"
}

start_daemon() {
    # Start host daemon if not already running
    if curl -s http://127.0.0.1:8770/health >/dev/null 2>&1; then
        return
    fi

    if [ ! -f "$KH_DIR/scripts/host_daemon.py" ]; then
        local tmp_id
        tmp_id=$($DOCKER_CMD create kh-web 2>/dev/null)
        if [ -n "$tmp_id" ]; then
            mkdir -p "$KH_DIR/scripts"
            $DOCKER_CMD cp "$tmp_id:/app/scripts/host_daemon.py" "$KH_DIR/scripts/host_daemon.py" 2>/dev/null
            $DOCKER_CMD rm "$tmp_id" >/dev/null 2>&1
        fi
    fi

    if [ -f "$KH_DIR/scripts/host_daemon.py" ]; then
        mkdir -p ~/.kh "$KH_DIR/data/workspaces"
        nohup python3 "$KH_DIR/scripts/host_daemon.py" \
            --port 8770 \
            --allowed-dirs "$KH_DIR/data/workspaces" \
            >> ~/.kh/daemon.log 2>&1 &
        sleep 1
        if curl -s http://127.0.0.1:8770/health >/dev/null 2>&1; then
            echo "  [OK] Host daemon ready (port 8770)"
        else
            echo "  [!] Host daemon failed to start, check ~/.kh/daemon.log"
        fi
    fi
}

do_start() {
    ensure_docker
    DOCKER_CMD="docker"
    if ! docker info &>/dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
    cd "$KH_DIR"

    if [ ! -f .env ]; then
        echo "  [!] No config found. Run: kh install"
        exit 1
    fi

    start_daemon

    if $DOCKER_CMD ps --format '{{.Names}}' | grep -qx kanban-harness; then
        echo "  [OK] Already running"
        echo "  http://localhost:8765"
        return
    fi

    if $DOCKER_CMD ps -a --format '{{.Names}}' | grep -qx kanban-harness; then
        echo "  [*] Starting..."
        $DOCKER_CMD start kanban-harness >/dev/null 2>&1
    else
        echo "  [*] Creating and starting..."
        mkdir -p data
        $DOCKER_CMD run -d --name kanban-harness \
            --network host \
            --env-file .env \
            -v "$KH_DIR/data:/app/data" \
            kh-web >/dev/null 2>&1
    fi

    sleep 2
    echo ""
    echo "  [OK] Started! http://localhost:8765"
    echo ""
}

do_stop() {
    if curl -s http://127.0.0.1:8770/health >/dev/null 2>&1; then
        pkill -f "host_daemon.py.*--port 8770" 2>/dev/null && echo "  [OK] Daemon stopped"
    fi
    if docker ps --format '{{.Names}}' | grep -qx kanban-harness; then
        docker stop kanban-harness >/dev/null 2>&1
        echo "  [OK] Stopped"
    else
        echo "  [OK] Not running"
    fi
}

do_update() {
    echo ""
    echo "  [*] Checking for updates..."
    echo ""

    # Self-update kh CLI first
    echo "  [*] Updating kh CLI..."
    local tmp_kh="/tmp/kh_update_$$"
    if curl -fsSL --connect-timeout 5 "$BASE_URL/install-cli.sh" -o "$tmp_kh" 2>/dev/null; then
        if [ -s "$tmp_kh" ] && head -1 "$tmp_kh" | grep -q "^#!/bin/bash"; then
            if ! cmp -s "$tmp_kh" "$(which kh)"; then
                sudo cp "$tmp_kh" "$(which kh)" && sudo chmod +x "$(which kh)"
                rm -f "$tmp_kh"
                echo "  [OK] kh CLI updated"
                echo "  [*] Restarting update with new version..."
                echo ""
                exec kh update
            fi
        fi
    fi
    rm -f "$tmp_kh"
    echo "  [OK] kh CLI already up to date"
    echo ""
    ensure_docker
    DOCKER_CMD="docker"
    if ! docker info &>/dev/null 2>&1; then DOCKER_CMD="sudo docker"; fi
    cd "$KH_DIR"

    curl -fsSL "$BASE_URL/start.sh" -o start.sh
    curl -fsSL "$BASE_URL/stop.sh" -o stop.sh
    chmod +x start.sh stop.sh
    echo "  [OK] Scripts updated"

    echo "  [*] Pulling latest image..."
    $DOCKER_CMD pull crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest
    $DOCKER_CMD tag crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com/kanban_harnness_web/kanban_harness_web:latest kh-web
    echo "  [OK] Image updated"

    if $DOCKER_CMD ps -a --format '{{.Names}}' | grep -qx kanban-harness; then
        echo "  [*] Rebuilding container..."
        $DOCKER_CMD rm -f kanban-harness >/dev/null 2>&1
        do_start
    else
        echo ""
        echo "  [OK] Update complete. Run: kh start"
    fi
}

do_status() {
    if docker ps --format '{{.Names}}' | grep -qx kanban-harness; then
        echo "  Status: Running"
        echo "  URL: http://localhost:8765"
        docker ps --filter name=kanban-harness --format "  Container: {{.ID}}  Uptime: {{.RunningFor}}  Image: {{.Image}}"
    elif docker ps -a --format '{{.Names}}' | grep -qx kanban-harness; then
        echo "  Status: Stopped"
        echo "  Run: kh start"
    else
        echo "  Status: Not installed"
        echo "  Run: kh install"
    fi
}

do_logs() {
    docker logs -f --tail 50 kanban-harness 2>&1
}

do_uninstall() {
    echo ""
    read -p "  Remove container and image? Data in data/ will be kept. [y/N] " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "  Cancelled"
        return
    fi
    docker rm -f kanban-harness >/dev/null 2>&1 || true
    docker rmi kh-web >/dev/null 2>&1 || true
    echo "  [OK] Uninstalled. Data kept in $KH_DIR/data/"
}

case "${1:-}" in
    install)  do_install ;;
    start)    do_start ;;
    stop)     do_stop ;;
    update)   do_update ;;
    status)   do_status ;;
    logs)     do_logs ;;
    config)   do_config ;;
    uninstall) do_uninstall ;;
    *)        show_help ;;
esac
