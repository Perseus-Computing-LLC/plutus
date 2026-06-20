#!/bin/bash
# Plutus — provider credit monitor refresh (watchdog pattern: silent on success).
# Regenerates the HTML dashboard and appends a burn-rate history snapshot.
set -e
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"

PLUTUS_DIR=/opt/data/webui/minions/.minions-data/workspace/plutus
HTML="$PLUTUS_DIR/plutus.html"

if [ ! -f "$PLUTUS_DIR/plutus.py" ]; then
    echo "Plutus not found at $PLUTUS_DIR/plutus.py"
    exit 1
fi

python3 "$PLUTUS_DIR/plutus.py" --html "$HTML" --snapshot >/dev/null 2>>"$PLUTUS_DIR/plutus.err"
exit_code=$?
if [ $exit_code -ne 0 ]; then
    echo "Plutus refresh FAILED (exit $exit_code) — see $PLUTUS_DIR/plutus.err"
    exit 1
fi

# Balancing arm: rebalance model routing by runway. Self-verifies + backs up
# config before writing; refuses the write if any provider/key would be lost.
python3 "$PLUTUS_DIR/plutus_route.py" --apply >>"$PLUTUS_DIR/plutus.routing.log" 2>>"$PLUTUS_DIR/plutus.err"
route_code=$?
if [ $route_code -ne 0 ]; then
    echo "Plutus routing FAILED (exit $route_code) — see $PLUTUS_DIR/plutus.err"
    exit 1
fi
exit 0
