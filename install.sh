#!/usr/bin/env bash
# Plutus — the billing layer for AI agents. One-line install.
#   curl -fsSL https://raw.githubusercontent.com/Perseus-Computing-LLC/plutus/main/install.sh | bash
set -euo pipefail

YELLOW='\033[33m'; GREEN='\033[32m'; DIM='\033[2m'; NC='\033[0m'
echo -e "${YELLOW}◆ Installing Plutus — the billing layer for AI agents${NC}"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then echo "Python 3.9+ is required." >&2; exit 1; fi

if command -v pipx >/dev/null 2>&1; then
  pipx install "plutus-agent[all]" || pipx install plutus-agent
else
  "$PY" -m pip install --user --upgrade "plutus-agent[all]" || "$PY" -m pip install --user --upgrade plutus-agent
fi

echo -e "${GREEN}✓ installed${NC}"
echo
echo -e "  ${DIM}Try the demo dashboard:${NC}  plutus demo        ${DIM}→ http://localhost:8420${NC}"
echo -e "  ${DIM}Meter Claude Code spend:${NC} plutus install-claude-hook"
echo -e "  ${DIM}Set up real billing:${NC}     see BILLING.md"
