#!/usr/bin/env bash
# One-time setup on the server (safe to re-run):
#   venv + tests, git hooks, the `jobhunter` command, and the Docker container (auto-starts on boot).
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$PWD"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Python environment + tests"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q

say "Git hooks (tests run before every commit and push)"
git config core.hooksPath scripts/hooks

say "jobhunter command"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/scripts/jobhunter" "$HOME/.local/bin/jobhunter"
echo "Linked ~/.local/bin/jobhunter -> $DIR/scripts/jobhunter"

say "Config"
[[ -f .env ]] || { cp .env.example .env; echo "Created .env: put TELEGRAM_BOT_TOKEN in it, then re-run."; exit 1; }
chmod 600 .env
mkdir -p data

say "Docker (builds, runs tests again, starts; restarts on boot)"
systemctl is-enabled docker >/dev/null 2>&1 || sudo systemctl enable docker
export UID; export GID="$(id -g)"
docker compose up -d --build
docker compose ps
echo -e "\nDone. Try: jobhunter health"
