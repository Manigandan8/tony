# =====================================================================
# tony_core/tools/__init__.py
# Central TOOLS registry and execution wrappers.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - TOOLS: dict mapping tool name string to callable
# =====================================================================

import functools
from tony_core.safety import log_action, TURN, _FAIL_MARKERS

from tony_core.tools.files import (
    calculator_math, write_file, append_to_file, read_file,
    list_files, open_folder, create_folder, delete_folder
)
from tony_core.tools.web import (
    web_search, browser_search, open_on_site, fetch_webpage, deep_research
)
from tony_core.tools.ui import (
    open_app, open_website, open_chrome_profile, close_window,
    restore_tabs, press_hotkey, type_text, ui_list, ui_click,
    click_screen, scroll_screen, look_at_screen, verify_screen,
    read_clipboard, write_clipboard, active_window_info
)
from tony_core.tools.system import (
    run_command, set_volume, media_control, top_processes,
    system_status, system_power
)
from tony_core.tools.info import (
    set_reminder, take_note, weather
)
from tony_core.tools.routines import run_routine
from tony_core.memory import remember_fact
from tony_core.triggers import get_secret

TOOLS = {
    "web_search": web_search, "browser_search": browser_search, "calculator_math": calculator_math,
    "write_file": write_file, "append_to_file": append_to_file, "read_file": read_file,
    "list_files": list_files, "open_folder": open_folder, "create_folder": create_folder,
    "delete_folder": delete_folder, "open_on_site": open_on_site, "open_app": open_app,
    "open_website": open_website, "open_chrome_profile": open_chrome_profile,
    "close_window": close_window, "restore_tabs": restore_tabs, "press_hotkey": press_hotkey,
    "type_text": type_text, "ui_list": ui_list, "ui_click": ui_click, "click_screen": click_screen,
    "scroll_screen": scroll_screen, "look_at_screen": look_at_screen, "verify_screen": verify_screen,
    "run_command": run_command, "set_volume": set_volume, "media_control": media_control,
    "set_reminder": set_reminder, "take_note": take_note, "weather": weather,
    "system_status": system_status, "run_routine": run_routine, "remember_fact": remember_fact,
    "read_clipboard": read_clipboard, "write_clipboard": write_clipboard, "fetch_webpage": fetch_webpage,
    "deep_research": deep_research, "top_processes": top_processes,
    "active_window_info": active_window_info, "system_power": system_power,
    "get_secret": get_secret,
}

def run_tool(name, args):
    fn = TOOLS.get(name)
    if not fn: return f"Unknown tool: {name}"
    log_action(name, args)
    try: out = fn(**(args or {}))
    except TypeError as e: out = f"Tool error (bad arguments): {str(e)[:120]}"
    except Exception as e: out = f"Tool error: {str(e)[:150]}"
    TURN["actions"].append(name)
    out = str(out)
    if any(m.lower() in out.lower() for m in _FAIL_MARKERS):
        TURN.setdefault("failures", []).append(f"{name}: {out[:200]}")
    return out

def _wrap(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        log_action(fn.__name__, k)
        TURN["actions"].append(fn.__name__)
        try: out = fn(*a, **k)
        except Exception as e: out = f"Tool error: {str(e)[:150]}"
        out = str(out)
        if any(m.lower() in out.lower() for m in _FAIL_MARKERS):
            TURN.setdefault("failures", []).append(f"{fn.__name__}: {out[:200]}")
        return out
    return w
