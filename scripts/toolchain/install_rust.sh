#!/bin/bash
# Idempotent Rust toolchain installer for Kanban Harness containers.
set -e

RUST_HOME="/usr/local/rust"
CARGO_BIN="$RUST_HOME/bin/cargo"

if [ -x "$CARGO_BIN" ]; then
    echo "Rust already installed: $($CARGO_BIN --version)"
    exit 0
fi

export RUSTUP_HOME="$RUST_HOME"
export CARGO_HOME="$RUST_HOME"

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --no-modify-path --default-toolchain stable

cat > /etc/profile.d/kh_rust.sh << 'EOF'
export RUSTUP_HOME="/usr/local/rust"
export CARGO_HOME="/usr/local/rust"
export PATH="/usr/local/rust/bin:$PATH"
EOF

source /etc/profile.d/kh_rust.sh

echo "Rust installed: $(rustc --version), $(cargo --version)"
