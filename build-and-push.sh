#!/bin/bash
# Kanban Harness - Build & Push to Aliyun Registry
# Usage: ./build-and-push.sh [tag]
#   tag: optional, defaults to "latest"

set -e

REGISTRY="crpi-dzz52onuqk3qfwz4.cn-shanghai.personal.cr.aliyuncs.com"
REPO="kanban_harnness_web/kanban_harness_web"
TAG="${1:-latest}"
FULL_IMAGE="$REGISTRY/$REPO:$TAG"
LOCAL_IMAGE="kh-web"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "  === Build & Push ==="
echo ""
echo "  Project:  $PROJECT_DIR"
echo "  Image:    $FULL_IMAGE"
echo ""

# Build
echo "  [1/3] Building image..."
docker build -t "$LOCAL_IMAGE" "$PROJECT_DIR"
echo "  [OK] Build complete"
echo ""

# Tag
echo "  [2/3] Tagging..."
docker tag "$LOCAL_IMAGE" "$FULL_IMAGE"

# Push
echo "  [3/3] Pushing to registry..."
docker push "$FULL_IMAGE"
echo ""
echo "  [OK] Done!"
echo "  Image: $FULL_IMAGE"
echo ""
