#!/bin/bash
set -e

# 让容器创建的文件对宿主机用户可读写（解决 daemon 权限问题）
umask 0000

# Reconcile: ensure claude binary is functional
if ! claude --version >/dev/null 2>&1; then
  echo "[entrypoint] claude binary broken, attempting repair..."
  npm install -g @anthropic-ai/claude-code-linux-x64 2>&1 \
    && node /usr/lib/node_modules/@anthropic-ai/claude-code/install.cjs 2>&1
  if claude --version >/dev/null 2>&1; then
    echo "[entrypoint] claude repaired: $(claude --version)"
  else
    echo "[entrypoint] WARNING: claude native binary unavailable, using Node.js wrapper (slower)"
    ln -sf /usr/lib/node_modules/@anthropic-ai/claude-code/cli-wrapper.cjs /usr/local/bin/claude
  fi
fi

exec "$@"
