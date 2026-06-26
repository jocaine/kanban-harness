#!/bin/bash
echo ""
echo "  Stopping Kanban Harness..."
docker stop kanban-harness >/dev/null 2>&1
echo "  [OK] Stopped. Run ./start.sh to restart."
echo ""
