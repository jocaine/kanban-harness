#!/bin/bash
# Reload KH config from .env without restarting the server.
# Usage: ./reload_config.sh [port]
PORT="${1:-8765}"
echo "Reloading config on port ${PORT} ..."
curl -s -X POST "http://localhost:${PORT}/api/config/reload" | python3 -m json.tool
