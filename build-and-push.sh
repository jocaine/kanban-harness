#!/bin/bash
# Kanban Harness - Build & Push to Aliyun Registry
# Usage: ./build-and-push.sh [tag]
#   tag:       optional, defaults to "latest"
#   --test:    build as test package, auto-increment v0.0.x

set -e

REGISTRY="crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com"
REPO="kanban_harnness_web/kanban_harness_web"
LOCAL_IMAGE="kh-web"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION_FILE="$PROJECT_DIR/.test_version"
KH_DIR="${KH_HOME:-$HOME/kanban-harness}"

# --- Mode ---
if [ "${1:-}" = "--test" ]; then
    CURRENT="0"
    if [ -f "$VERSION_FILE" ]; then
        CURRENT=$(cat "$VERSION_FILE" | tr -d '[:space:]')
    fi
    NEXT=$((CURRENT + 1))
    echo "$NEXT" > "$VERSION_FILE"
    TAG="v0.0.${NEXT}"
    echo "  [TEST] v0.0.${CURRENT} → v0.0.${NEXT}"
else
    TAG="${1:-latest}"
fi

FULL_IMAGE="$REGISTRY/$REPO:$TAG"

echo ""
echo "  === Build & Push ==="
echo "  Tag: $TAG"
echo ""

# Build
echo "  [1/3] Building..."
docker build --build-arg "APP_VERSION=$TAG" -t "$LOCAL_IMAGE" "$PROJECT_DIR"
echo "  [OK] Build complete"

# Tag & Push
echo "  [2/3] Pushing $TAG..."
docker tag "$LOCAL_IMAGE" "$FULL_IMAGE"
docker push "$FULL_IMAGE"

# Always push latest so kh update works
if [ "$TAG" != "latest" ]; then
    echo "  [3/3] Pushing latest..."
    docker tag "$LOCAL_IMAGE" "$REGISTRY/$REPO:latest"
    docker push "$REGISTRY/$REPO:latest"
else
    echo "  [3/3] Already latest"
fi

echo ""
echo "  [OK] Pushed: $FULL_IMAGE + latest"
echo ""

# Recreate local container if running
if docker ps -a --format '{{.Names}}' | grep -qx kanban-harness; then
    echo "  [*] Recreating local container..."
    docker rm -f kanban-harness >/dev/null 2>&1
    if [ -f "$KH_DIR/.env" ]; then
        docker run -d --name kanban-harness \
            --network host \
            --env-file "$KH_DIR/.env" \
            -v "$KH_DIR/data:/app/data" \
            kh-web >/dev/null 2>&1
        echo "  [OK] Running: http://localhost:8765"
    else
        echo "  [!] No .env, run: kh start"
    fi
fi
