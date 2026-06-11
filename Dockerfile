FROM python:3.11-slim

WORKDIR /app

# === AI-friendly environment (KH-076) ===
ENV DEBIAN_FRONTEND=noninteractive
ENV GIT_TERMINAL_PROMPT=0
ENV NO_COLOR=1
ENV TERM=dumb
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8
ENV PYTHONIOENCODING=utf-8
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL=1
ENV DISABLE_AUTOUPDATER=1
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

# 使用国内镜像源加速
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true

# === 工具链安装 (KH-077) — 不常变，放最前面利用缓存 ===
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq \
    ripgrep fd-find tree procps less file \
    cmake g++ make \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

# Node.js (不常变)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set registry https://registry.npmmirror.com \
    && npm install -g pnpm

# --- AI CLI Tools (不常变) ---
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

RUN pip install --no-cache-dir hermes-agent

RUN SITE=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    echo 'name: searxng\nkind: backend\ndescription: SearXNG metasearch (self-hosted, free)' \
    > "$SITE/plugins/web/searxng/plugin.yaml" && \
    printf 'name: web-firecrawl\nversion: 1.0.0\ndescription: Firecrawl web search + content extraction\nauthor: NousResearch\nkind: backend\nprovides_web_providers:\n  - firecrawl\n' \
    > "$SITE/plugins/web/firecrawl/plugin.yaml"

RUN npm install -g @anthropic-ai/claude-code

# Git config for Coach-Dev
RUN git config --global user.name "KH-Coach" && \
    git config --global user.email "coach@kanban-harness.local"

RUN mkdir -p /root/.hermes /root/.claude /tmp/kh-worktrees
COPY skills/ /root/.hermes/skills/

# === AI shell wrapper (KH-078) — 偶尔变 ===
COPY scripts/ai-exec /usr/local/bin/ai-exec

# === Toolchain map (KH-079) — 偶尔变 ===
COPY config/toolchain_map.json /etc/kh/toolchain_map.json

# --- Application (常变，放最后) ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

EXPOSE 8765

COPY entrypoint.sh /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/projects')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765", "--reload", "--no-access-log"]
