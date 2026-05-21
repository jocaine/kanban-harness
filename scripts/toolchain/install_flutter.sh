#!/bin/bash
# Idempotent Flutter SDK installer for Kanban Harness containers.
set -e

FLUTTER_HOME="/opt/flutter"
FLUTTER_BIN="$FLUTTER_HOME/bin/flutter"

if [ -x "$FLUTTER_BIN" ]; then
    echo "Flutter already installed: $($FLUTTER_BIN --version --machine | head -1)"
    exit 0
fi

# Prerequisites
apt-get update && apt-get install -y --no-install-recommends \
    clang libgtk-3-dev liblzma-dev libstdc++-12-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone Flutter stable
git clone --depth 1 --branch stable https://github.com/flutter/flutter.git "$FLUTTER_HOME"

# Precache artifacts
"$FLUTTER_BIN" precache --no-ios --no-macos --no-windows

# Disable analytics
"$FLUTTER_BIN" config --no-analytics

cat > /etc/profile.d/kh_flutter.sh << EOF
export FLUTTER_HOME="$FLUTTER_HOME"
export PATH="\$FLUTTER_HOME/bin:\$FLUTTER_HOME/bin/cache/dart-sdk/bin:\$PATH"
EOF

source /etc/profile.d/kh_flutter.sh

echo "Flutter installed: $(flutter --version | head -1)"
