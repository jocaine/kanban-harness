#!/bin/bash
# Idempotent Android SDK installer for Kanban Harness containers.
set -e

ANDROID_HOME="/opt/android-sdk"
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"

if [ -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
    echo "Android SDK already installed at $ANDROID_HOME"
    "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --version
    exit 0
fi

# Prerequisites
apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk wget unzip zip \
    && rm -rf /var/lib/apt/lists/*

# Download cmdline-tools
mkdir -p "$ANDROID_HOME/cmdline-tools"
cd /tmp
wget -q "$CMDLINE_TOOLS_URL" -O cmdline-tools.zip
unzip -q cmdline-tools.zip
mv cmdline-tools "$ANDROID_HOME/cmdline-tools/latest"
rm cmdline-tools.zip

# Accept licenses
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses > /dev/null 2>&1 || true

# Install essential components
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
    "platform-tools" \
    "build-tools;34.0.0" \
    "platforms;android-34"

cat > /etc/profile.d/kh_android.sh << EOF
export ANDROID_HOME="$ANDROID_HOME"
export PATH="\$ANDROID_HOME/cmdline-tools/latest/bin:\$ANDROID_HOME/platform-tools:\$PATH"
export JAVA_HOME="/usr/lib/jvm/java-17-openjdk-amd64"
EOF

source /etc/profile.d/kh_android.sh

echo "Android SDK installed: build-tools 34.0.0, pm android-34"
