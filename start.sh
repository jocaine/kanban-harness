#!/bin/bash
set -e

echo ""
echo "  === Kanban Harness ===  "
echo ""

# Check and install Docker
if ! command -v docker &>/dev/null; then
    echo "  [*] Docker not found, installing..."
    echo ""
    curl -fsSL https://get.docker.com | sh
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
SEARXNG_URL=http://localhost:8888
FIRECRAWL_API_URL=http://localhost:3002
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

# Ensure search service URLs are in .env (for upgrades from older versions)
grep -q "^SEARXNG_URL=" .env 2>/dev/null || echo "SEARXNG_URL=http://localhost:8888" >> .env
grep -q "^FIRECRAWL_API_URL=" .env 2>/dev/null || echo "FIRECRAWL_API_URL=http://localhost:3002" >> .env

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

# Start search services (searxng + firecrawl-lite)
start_searxng() {
    if docker ps --format '{{.Names}}' | grep -qx kh-searxng; then
        return
    fi
    docker rm -f kh-searxng >/dev/null 2>&1 || true
    docker run -d --name kh-searxng \
        --network host \
        --restart unless-stopped \
        -v "$(pwd)/docker/searxng/settings.yml:/etc/searxng/settings.yml:ro" \
        -v "$(pwd)/docker/searxng/limiter.toml:/etc/searxng/limiter.toml:ro" \
        -e SEARXNG_BASE_URL=http://localhost:8888/ \
        -e BIND_ADDRESS=0.0.0.0:8888 \
        -e GRANIAN_PORT=8888 \
        searxng/searxng:latest >/dev/null 2>&1
}

start_firecrawl() {
    if docker ps --format '{{.Names}}' | grep -qx kh-firecrawl; then
        return
    fi
    if ! docker image inspect kanban_harness-firecrawl-lite &>/dev/null 2>&1; then
        echo "  [*] Building firecrawl-lite..."
        docker build -t kanban_harness-firecrawl-lite docker/firecrawl-lite >/dev/null 2>&1
    fi
    docker rm -f kh-firecrawl >/dev/null 2>&1 || true
    docker run -d --name kh-firecrawl \
        --network host \
        --restart unless-stopped \
        kanban_harness-firecrawl-lite >/dev/null 2>&1
}

start_searxng
start_firecrawl
echo "  [OK] Search services ready"
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
