# Claude worker: Claude Code CLI (headless) + a tiny stdlib Python HTTP server. Talks only to the bot.
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Pinned Claude Code version (stable channel); bump deliberately.
RUN npm install -g @anthropic-ai/claude-code@2.1.326 && npm cache clean --force
ENV HOME=/tmp/home \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY server.py ./
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=5)"
EXPOSE 8787
CMD ["sh", "-c", "mkdir -p \"$HOME\" && exec python3 server.py"]
