"""Bridge to the live runway monitor (repo-root ``plutus.py``).

Decoupled by design: rather than importing the production monitor (which has its
own module-level paths and is pulled to the Hermes host independently), the
engine shells out to it for ``--json`` exactly as ``plutus_route.py`` does. If
the monitor isn't configured or fails, the dashboard simply omits the live
provider-runway panel — the rest of Plutus works fully offline.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys


def runway(monitor_cfg: dict) -> dict | None:
    """Return the monitor's ``collect()`` JSON, or ``None`` if unavailable.

    Fix #65: the bridge is a subprocess shell-out, so the executable is locked
    down — the first token of ``command`` must be an ABSOLUTE path that appears
    in ``monitor.allowed_binaries``. This keeps the exec surface from ever being
    arbitrary even if config later becomes tenant-influenceable. An unset
    allow-list refuses to run (fail-closed).
    """
    if not monitor_cfg or not monitor_cfg.get("enabled"):
        return None
    cmd = monitor_cfg.get("command", "").strip()
    if not cmd:
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        sys.stderr.write(f"plutus: monitor command unparseable: {e}\n")
        return None
    if not argv:
        return None
    binary = argv[0]
    allowed = monitor_cfg.get("allowed_binaries") or []
    if not os.path.isabs(binary):
        sys.stderr.write(
            "plutus: monitor command must be an absolute path "
            f"(got {binary!r}); refusing to run\n")
        return None
    if binary not in allowed:
        sys.stderr.write(
            f"plutus: monitor binary {binary!r} not in monitor.allowed_binaries; "
            "refusing to run\n")
        return None
    try:
        out = subprocess.run(argv + ["--json"], capture_output=True,
                             text=True, timeout=30, shell=False)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception as e:  # never let the bridge break the dashboard
        sys.stderr.write(f"plutus: monitor bridge unavailable: {e}\n")
        return None
