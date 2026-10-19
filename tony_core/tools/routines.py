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

DEFAULT_ROUTINES = {
    "morning": [{"tool": "open_app", "args": {"app": "chrome"}}, {"tool": "weather", "args": {}}, {"tool": "system_status", "args": {}}],
    "focus": [{"tool": "close_window", "args": {"what": "chrome"}}, {"tool": "set_volume", "args": {"level": 25}}, {"tool": "open_app", "args": {"app": "notepad"}}]
}

if not os.path.exists(config.ROUTINES_FILE):
    try:
        with open(config.ROUTINES_FILE, "w", encoding="utf-8") as f: json.dump(DEFAULT_ROUTINES, f, indent=2)
    except Exception: pass

def run_routine(name: str = "") -> str:
    from tony_core.tools import run_tool
    try:
        with open(config.ROUTINES_FILE, encoding="utf-8") as f: routines = json.load(f)
    except Exception: return "No routines.json found."
    steps = routines.get(name)
    if not steps: return f"No routine named '{name}'. Available: {', '.join(routines) or 'none'}."
    outs = [f"{s.get('tool')}: {run_tool(s.get('tool'), s.get('args') or {})}" for s in steps[:12]]
    return f"Routine '{name}' done. " + " | ".join(o[:60] for o in outs)
