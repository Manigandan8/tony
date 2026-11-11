# =====================================================================
# tony_core/tools/__init__.py
# Central TOOLS registry and execution wrappers.
# =====================================================================

import functools
from tony_core import pc_index
from tony_core.safety import log_action, TURN, _FAIL_MARKERS, _remote, REMOTE_ALLOW_TOOLS

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
    read_clipboard, write_clipboard, active_window_info,
    search_in_bar, browser_action, window_control, camera_control
)
from tony_core.tools.system import (
    run_command, set_volume, set_brightness, media_control, top_processes,
    system_status, system_power
)
from tony_core.tools.info import (
    set_reminder, take_note, weather
)
from tony_core.tools.routines import run_routine
from tony_core.memory import remember_fact


def _disabled_get_secret(name: str = "") -> str:
    return "Blocked for security: reading .env API keys into chat is disabled."


TOOLS = {
    "web_search": web_search,
    "browser_search": browser_search,
    "search_in_bar": search_in_bar,
    "browser_action": browser_action,
    "window_control": window_control,
    "calculator_math": calculator_math,
    "write_file": write_file,
    "append_to_file": append_to_file,
    "read_file": read_file,
    "list_files": list_files,
    "open_folder": open_folder,
    "create_folder": create_folder,
    "delete_folder": delete_folder,
    "find_anywhere": pc_index.find_anywhere,
    "open_anything": pc_index.open_anything,
    "recent_pc_activity": pc_index.recent_pc_activity,
    "open_on_site": open_on_site,
    "open_app": open_app,
    "open_website": open_website,
    "open_chrome_profile": open_chrome_profile,
    "close_window": close_window,
    "restore_tabs": restore_tabs,
    "press_hotkey": press_hotkey,
    "type_text": type_text,
    "ui_list": ui_list,
    "ui_click": ui_click,
    "click_screen": click_screen,
    "scroll_screen": scroll_screen,
    "look_at_screen": look_at_screen,
    "verify_screen": verify_screen,
    "run_command": run_command,
    "set_volume": set_volume,
    "set_brightness": set_brightness,
    "media_control": media_control,
    "set_reminder": set_reminder,
    "take_note": take_note,
    "weather": weather,
    "system_status": system_status,
    "run_routine": run_routine,
    "remember_fact": remember_fact,
    "read_clipboard": read_clipboard,
    "write_clipboard": write_clipboard,
    "fetch_webpage": fetch_webpage,
    "deep_research": deep_research,
    "top_processes": top_processes,
    "active_window_info": active_window_info,
    "system_power": system_power,
    "get_secret": _disabled_get_secret,
}


def _is_failed_output(out_str: str) -> bool:
    low = (out_str or "").lower()
    return any(m.lower() in low for m in _FAIL_MARKERS)


def run_tool(name, args):
    fn = TOOLS.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    if _remote() and name not in REMOTE_ALLOW_TOOLS:
        log_action(name, args, "[blocked remote]")
        return f"Blocked: '{name}' is not allowed from remote Telegram."

    log_action(name, args)
    try:
        out = fn(**(args or {}))
    except TypeError as e:
        out = f"Tool error (bad arguments): {str(e)[:120]}"
    except Exception as e:
        out = f"Tool error: {str(e)[:150]}"

    out = str(out)
    if _is_failed_output(out):
        TURN.setdefault("failures", []).append(f"{name}: {out[:200]}")
    else:
        TURN.setdefault("actions", []).append(name)
    return out


def _wrap(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        name = fn.__name__
        if _remote() and name not in REMOTE_ALLOW_TOOLS:
            log_action(name, k, "[blocked remote]")
            return f"Blocked: '{name}' is not allowed from remote Telegram."
        log_action(name, k)
        try:
            out = fn(*a, **k)
        except Exception as e:
            out = f"Tool error: {str(e)[:150]}"
        out = str(out)
        if _is_failed_output(out):
            TURN.setdefault("failures", []).append(f"{name}: {out[:200]}")
        else:
            TURN.setdefault("actions", []).append(name)
        return out
    return w