"""Bridge to the live runway monitor (repo-root ``plutus.py``).

Decoupled by design: rather than importing the production monitor (which has its
own module-level paths and is pulled to the Hermes host independently), the
engine shells out to it for ``--json`` exactly as ``plutus_route.py`` does. If
the monitor isn't configured or fails, the dashboard simply omits the live
provider-runway panel — the rest of Plutus works fully offline.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys


def runway(monitor_cfg: dict) -> dict | None:
    """Return the monitor's ``collect()`` JSON, or ``None`` if unavailable."""
    if not monitor_cfg or not monitor_cfg.get("enabled"):
        return None
    cmd = monitor_cfg.get("command", "").strip()
    if not cmd:
        return None
    try:
        argv = shlex.split(cmd) + ["--json"]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception as e:  # never let the bridge break the dashboard
        sys.stderr.write(f"plutus: monitor bridge unavailable: {e}\n")
        return None
