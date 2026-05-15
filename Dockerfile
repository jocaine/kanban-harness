FROM python:3.11-slim

WORKDIR /app

# 使用国内镜像源加速
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq \
    gcc g++ make cmake pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set registry https://registry.npmmirror.com \
    && npm install -g pnpm

# --- AI CLI Tools ---
# pip 国内源
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# Hermes Agent (Python-based AI agent with tool use)
RUN pip install --no-cache-dir hermes-agent

# Claude Code (Node.js-based AI coding assistant)
RUN npm install -g @anthropic-ai/claude-code

# Git config for Coach-Dev (needed for commits in worktrees)
RUN git config --global user.name "KH-Coach" && \
    git config --global user.email "coach@kanban-harness.local"

# Prepare config directories
RUN mkdir -p /root/.hermes /root/.claude /tmp/kh-worktrees

# Claude CLI: skip update checks and interactive prompts in container
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL=1
ENV DISABLE_AUTOUPDATER=1

# --- Application ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/projects')" || exit 1

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
