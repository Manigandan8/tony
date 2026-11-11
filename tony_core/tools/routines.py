# =====================================================================
# tony_core/tools/routines.py
# Routines execution and default routines management.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - None
# =====================================================================

import os
import json
from tony_core import config
from tony_core.safety import _FAIL_MARKERS

# Fixed Audit Part 5: Default 'focus' routine no longer kills Chrome without asking
DEFAULT_ROUTINES = {
    "morning": [
        {"tool": "open_app", "args": {"app": "chrome"}},
        {"tool": "weather", "args": {}},
        {"tool": "system_status", "args": {}},
    ],
    "focus": [
        {"tool": "set_volume", "args": {"level": 25}},
        {"tool": "open_app", "args": {"app": "notepad"}},
    ],
}

if not os.path.exists(config.ROUTINES_FILE):
    try:
        with open(config.ROUTINES_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_ROUTINES, f, indent=2)
    except Exception:
        pass


def run_routine(name: str = "") -> str:
    from tony_core.tools import run_tool
    try:
        with open(config.ROUTINES_FILE, encoding="utf-8") as f:
            routines = json.load(f)
    except Exception:
        return "No routines.json found. Do NOT claim success."
    want = (name or "").strip().lower()
    steps = routines.get(name) or routines.get(want)
    if not steps:
        return f"No routine named '{name}'. Available: {', '.join(routines) or 'none'}. Do NOT claim success."

    outs = []
    failures = 0
    for s in steps[:12]:
        t_name = s.get("tool")
        res = str(run_tool(t_name, s.get("args") or {}))
        if any(m.lower() in res.lower() for m in _FAIL_MARKERS):
            failures += 1
        outs.append(f"{t_name}: {res[:120]}")

    status = f"Routine '{name}' completed with {failures} failed step(s)." if failures else f"Routine '{name}' completed."
    return f"{status} " + " | ".join(outs)