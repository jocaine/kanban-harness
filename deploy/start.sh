#!/bin/bash
set -eo pipefail

echo ""
echo "  === Kanban Harness ===  "
echo ""

# Docker CN mirror installer (fallback when get.docker.com is unreachable)
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

# Check and install Docker
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
    echo "  [*] Starting Docker..."
    sudo systemctl start docker
    sleep 2
fi

echo "  [OK] Docker ready"
echo ""

# Config
run_config() {
    echo "  === API Configuration ==="
    echo ""
    echo "  Supports OpenAI / Anthropic / DeepSeek / any compatible provider"
    if [ -n "$OLD_KEY" ]; then
        echo "  (Press Enter to keep current value)"
    fi
    echo ""

    API_KEY=""
    while [ -z "$API_KEY" ]; do
        if [ -n "$OLD_KEY" ]; then
            read -p "  [1/3] API Key [${OLD_KEY:0:8}...]: " API_KEY
            [ -z "$API_KEY" ] && API_KEY="$OLD_KEY"
        else
            read -p "  [1/3] API Key: " API_KEY
            if [ -z "$API_KEY" ]; then echo "        Cannot be empty"; fi
        fi
    done

    API_BASE=""
    while [ -z "$API_BASE" ]; do
        echo ""
        if [ -n "$OLD_BASE" ]; then
            read -p "  [2/3] API URL [$OLD_BASE]: " API_BASE
            [ -z "$API_BASE" ] && API_BASE="$OLD_BASE"
        else
            read -p "  [2/3] API URL (e.g. https://api.openai.com/v1): " API_BASE
            if [ -z "$API_BASE" ]; then echo "        Cannot be empty"; fi
        fi
    done

    MODEL=""
    while [ -z "$MODEL" ]; do
        echo ""
        if [ -n "$OLD_MODEL" ]; then
            read -p "  [3/3] Model name [$OLD_MODEL]: " MODEL
            [ -z "$MODEL" ] && MODEL="$OLD_MODEL"
        else
            read -p "  [3/3] Model name (e.g. claude-sonnet-4-6, gpt-4o): " MODEL
            if [ -z "$MODEL" ]; then echo "        Cannot be empty"; fi
        fi
    done

    cat > .env << EOF
API_KEY=$API_KEY
API_BASE_URL=$API_BASE
CHAT_MODEL=$MODEL
ANTHROPIC_AUTH_TOKEN=$API_KEY
ANTHROPIC_BASE_URL=$API_BASE
EOF

    echo ""
    echo "  [OK] Config saved"
    echo ""
}

# Load existing config if present
OLD_KEY="" OLD_BASE="" OLD_MODEL=""
if [ -f .env ]; then
    OLD_KEY=$(grep -m1 '^API_KEY=' .env | cut -d= -f2-)
    OLD_BASE=$(grep -m1 '^API_BASE_URL=' .env | cut -d= -f2-)
    OLD_MODEL=$(grep -m1 '^CHAT_MODEL=' .env | cut -d= -f2-)
fi

if [ ! -f .env ] || [ "$1" = "config" ]; then
    run_config
fi

# Load image
if ! docker image inspect kh-web &>/dev/null 2>&1; then
    echo "  [*] Loading Docker image..."
    if [ -f kh-web.tar.gz ]; then
        docker load < kh-web.tar.gz
        echo "  [OK] Image loaded"
    else
        echo "  [!] kh-web.tar.gz not found!"
        echo "  Make sure this script is in the same directory as kh-web.tar.gz"
        exit 1
    fi
else
    echo "  [OK] Image ready"
fi

echo ""

# Start container (recreate if .env changed since last creation)
mkdir -p data
if docker ps -a --format '{{.Names}}' | grep -qx kanban-harness; then
    CONTAINER_CREATED=$(docker inspect kanban-harness --format '{{.Created}}' 2>/dev/null)
    ENV_MODIFIED=$(stat -c %Y .env 2>/dev/null || echo 0)
    CONTAINER_TS=$(date -d "$CONTAINER_CREATED" +%s 2>/dev/null || echo 0)

    if [ "$ENV_MODIFIED" -gt "$CONTAINER_TS" ]; then
        echo "  [*] Config changed, recreating..."
        docker rm -f kanban-harness >/dev/null 2>&1
        docker run -d --name kanban-harness \
            --network host \
            --restart unless-stopped \
            --env-file .env \
            -v "$(pwd)/data:/app/data" \
            kh-web >/dev/null 2>&1
    else
        echo "  [*] Starting..."
        docker start kanban-harness >/dev/null 2>&1
    fi
else
    echo "  [*] Creating and starting..."
    docker run -d --name kanban-harness \
        --network host \
        --restart unless-stopped \
        --env-file .env \
        -v "$(pwd)/data:/app/data" \
        kh-web >/dev/null 2>&1
fi

sleep 2

echo ""
echo "  [OK] Started! http://localhost:8765"
echo ""
echo "  Stop: ./stop.sh"
echo ""
