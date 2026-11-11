# =====================================================================
# tony_core/brain.py
# Brain orchestration, provider chain (9 brains), cooldown ladder,
# category micro-tool routing, 1s screen telemetry, and multi-step fast path.
# =====================================================================

import os
import re
import json
import time
import datetime
from google.genai import types as gemini_types

from tony_core import config
from tony_core.net import HTTP
from tony_core import overlay
from tony_core import speech
from tony_core import safety
from tony_core import memory
from tony_core import pc_index
from tony_core.tools import TOOLS, run_tool, _wrap
from tony_core.tools import ui, files, system
from tony_core.ears import _mic_command
from tony_core.providers import (
    groqai, gemini, zai, cohereai, openrouter, sambanova,
    github_models, mistralai, cerebras
)

try:
    from tony_core import screen_pulse
except ImportError:
    screen_pulse = None

# Force max_retries=0 on Groq client so it NEVER sleeps 30-60s on 429 rate limits!
try:
    groqai.groq = groqai.groq.with_options(max_retries=0, timeout=10.0)
except Exception:
    pass

# Per-model cooldown tracker: {model_id: cooldown_until_timestamp}
_MODEL_COOLDOWNS = {}

# Auto-register all UI, System, and Screen Pulse tools into TOOLS
for _ui_tool in ("camera_control", "search_in_bar", "browser_action", "window_control"):
    if hasattr(ui, _ui_tool):
        TOOLS[_ui_tool] = getattr(ui, _ui_tool)
for _sys_tool in ("set_brightness", "step_volume", "step_brightness", "take_screenshot",
                  "kill_process", "disk_space", "wifi_control", "system_control"):
    if hasattr(system, _sys_tool):
        TOOLS[_sys_tool] = getattr(system, _sys_tool)
if screen_pulse and hasattr(screen_pulse, "watch_screen_for"):
    TOOLS["watch_screen_for"] = screen_pulse.watch_screen_for

_SINGLE_SHOT_TOOLS = {
    "open_folder", "open_anything", "open_on_site", "open_website",
    "open_app", "open_chrome_profile", "restore_tabs", "click_screen",
    "ui_click", "browser_search", "search_in_bar", "browser_action",
    "window_control", "close_window", "press_hotkey", "type_text",
    "scroll_screen", "set_volume", "set_brightness", "step_volume",
    "step_brightness", "take_screenshot", "kill_process", "wifi_control",
    "system_control", "media_control", "set_reminder", "take_note",
    "system_power", "run_command", "run_routine", "create_folder",
    "delete_folder", "write_file", "append_to_file", "write_clipboard",
    "remember_fact", "camera_control", "watch_screen_for"
}

_KNOWN_FOLDER_NAMES = {"desktop", "downloads", "documents", "pictures", "music", "videos", "tony", "home"}


def _live_foreground_title() -> str:
    try:
        import pyautogui
        w = pyautogui.getActiveWindow()
        if w and w.title:
            return w.title.strip()[:90]
    except Exception:
        pass
    return "unknown"


def system_prompt():
    now = datetime.datetime.now().astimezone()
    dt_str = now.strftime("%A, %d %B %Y, %I:%M %p %Z")
    pc_ctx = pc_index.live_context_summary()
    screen_ctx = (
        screen_pulse.live_screen_telemetry()
        if (screen_pulse and hasattr(screen_pulse, "live_screen_telemetry"))
        else f"Foreground window = '{_live_foreground_title()}'."
    )
    last_dir = f" Last opened Explorer folder = '{files.LAST_OPENED_FOLDER}'." if files.LAST_OPENED_FOLDER else ""
    return config.SYSTEM + (
        f"\nCURRENT DATE & TIME: {dt_str}.\n"
        f"CURRENT MACHINE STATE: Active browser = '{ui.STATE.get('active_browser', 'chrome')}'; "
        f"Browser running = {ui.STATE['chrome_running']}; last site = {ui.STATE['last_site']}.{last_dir}\n"
        f"{screen_ctx}\n"
        f"{pc_ctx}\n"
        f"STRICT ROUTING RULES:\n"
        f"- NEVER pretend you clicked, opened, or searched something without actually calling the tool!\n"
        f"- For browser tabs, history, downloads, bookmarks, incognito, zoom, or video controls in ANY browser, ALWAYS call `browser_action(action=..., browser=...)`.\n"
        f"- For OS/hardware controls (screenshots, Wi-Fi, Bluetooth, brightness, volume up/down, settings, virtual desktops, recycle bin, disk space), call `system_control`.\n"
        f"- For webcam/camera actions, call `camera_control`.\n"
        f"- When asked how many folders or files are in Desktop, Downloads, Documents, or any folder, ALWAYS call `list_files(folder=...)`."
    ) + memory.facts_block()


def T(name, desc, props, req):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req},
        },
    }


GROQ_TOOLS = [
    T("web_search", "Silently research a topic (for ANSWERING verbally).", {"query": {"type": "string"}}, ["query"]),
    T("camera_control", "Control the Windows Camera / Webcam app: 'open', 'close', 'take_photo', 'start_video', 'stop_video', 'pause_video', 'resume_video', 'switch_photo_mode', 'switch_video_mode', 'switch_camera', 'open_last'.", {"action": {"type": "string"}, "timer_seconds": {"type": "integer"}}, ["action"]),
    T("browser_action", "Universal browser & web-video controller (Chrome, Brave, Edge, Firefox, Opera, Vivaldi, Neo). Actions: back, forward, refresh, hard_refresh, stop_loading, home, new_tab, close_tab, reopen_tab, duplicate_tab, next_tab, prev_tab, first_tab, last_tab, tab_number (value='1'..'9'), new_window, incognito, full_screen_browser, address_bar (value=url), copy_url, bookmark, bookmarks_bar, bookmarks_manager, history, downloads, clear_browsing_data, extensions, browser_settings, devtools, view_source, print_page, save_page, find_on_page (value=text), find_next, find_prev, zoom_in, zoom_out, zoom_reset, scroll_top, scroll_bottom, page_down, page_up, play_pause, fullscreen, theater, miniplayer, captions, mute_video, mute_tab, skip_forward, skip_backward, speed_up, slow_down, next_video, prev_video, skip_ad.", {"action": {"type": "string"}, "value": {"type": "string"}, "browser": {"type": "string"}}, ["action"]),
    T("system_control", "Master Windows OS & hardware controller. Actions: volume_up, volume_down, mute, unmute, mute_mic, volume_mixer, brightness_up, brightness_down, screenshot, open_last_screenshot, snipping_tool, night_light, display_settings, project_screen, wifi_status, wifi_list, wifi_connect (value=ssid), wifi_disconnect, wifi_settings, bluetooth_settings, hotspot_settings, airplane_mode, ip_address, start_menu, quick_settings, notifications, clipboard_history, emoji_panel, task_manager, run_dialog, settings (value=page), new_desktop, close_desktop, next_desktop, prev_desktop, disk_space, battery_status, empty_recycle_bin, kill_process (value=app_name).", {"action": {"type": "string"}, "value": {"type": "string"}}, ["action"]),
    T("window_control", "Hands-free window management: minimize, maximize, restore, show_desktop, switch, snap_left, snap_right, task_view, close.", {"action": {"type": "string"}, "app": {"type": "string"}}, ["action"]),
    T("watch_screen_for", "Watch the screen every 1 second and alert when `target_text` appears, or toggle `auto_skip_ads`.", {"target_text": {"type": "string"}, "auto_skip_ads": {"type": "boolean"}}, []),
    T("type_text", "Type text into any window or specific on-screen input box (`element`), with optional `clear_first` and `press_enter`.", {"text": {"type": "string"}, "app": {"type": "string"}, "element": {"type": "string"}, "press_enter": {"type": "boolean"}, "clear_first": {"type": "boolean"}}, ["text"]),
    T("click_screen", "Click any visible item on screen (`click_type`: 'single', 'double', 'right', or 'hover').", {"app": {"type": "string"}, "element": {"type": "string"}, "click_type": {"type": "string", "enum": ["single", "double", "right", "hover"]}}, ["element"]),
    T("set_brightness", "Set laptop screen brightness 0-100.", {"level": {"type": "integer"}}, ["level"]),
    T("calculator_math", "Do ANY math safely: '3137*3371', '15% of 840', 'sqrt(144)'.", {"expression": {"type": "string"}}, ["expression"]),
    T("open_folder", "Open a single folder in File Explorer INSTANTLY.", {"folder": {"type": "string"}}, ["folder"]),
    T("create_folder", "Create a folder (any depth), VERIFIED on disk.", {"path": {"type": "string"}}, ["path"]),
    T("delete_folder", "Delete an EMPTY folder (safe).", {"path": {"type": "string"}}, ["path"]),
    T("write_file", "Create or overwrite a file.", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    T("append_to_file", "Append a chunk to a file.", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    T("read_file", "Read a text, code, Word (.docx), or PDF (.pdf) file.", {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]),
    T("list_files", "Count and list all FOLDERS and FILES inside any directory (Desktop, Downloads, Documents, etc.).", {"folder": {"type": "string"}, "pattern": {"type": "string"}}, []),
    T("find_anywhere", "Search the ENTIRE PC for any file or folder by name.", {"query": {"type": "string"}, "kind": {"type": "string", "enum": ["any", "file", "folder"]}}, ["query"]),
    T("open_anything", "Open ANY file, folder, or installed app anywhere on the PC.", {"target": {"type": "string"}}, ["target"]),
    T("recent_pc_activity", "Check what files were recently created, downloaded, or edited on the computer.", {"count": {"type": "integer"}}, []),
    T("search_in_bar", "Type a query into the active page's search bar and press Enter WITHOUT opening a new tab.", {"query": {"type": "string"}, "site": {"type": "string"}, "browser": {"type": "string"}}, ["query"]),
    T("browser_search", "Open a Google search in the browser.", {"query": {"type": "string"}}, ["query"]),
    T("open_on_site", "Open and play a specific video or page on a site (YouTube, GitHub, Spotify) in a single tab.", {"site": {"type": "string"}, "what": {"type": "string"}}, ["site", "what"]),
    T("open_app", "Open a DESKTOP application by name.", {"app": {"type": "string"}}, ["app"]),
    T("open_website", "Open a website in any browser (chrome, brave, edge, firefox, opera, vivaldi, neo).", {"url": {"type": "string"}, "browser": {"type": "string"}}, ["url"]),
    T("open_chrome_profile", "Switch Chrome to a named profile.", {"profile": {"type": "string"}}, ["profile"]),
    T("restore_tabs", "Restore the last closed browser tab.", {}, []),
    T("press_hotkey", "Keyboard shortcut: 'ctrl+t', 'f5', 'alt+tab', YouTube keys (k/l/j/f/m).", {"keys": {"type": "string"}, "app": {"type": "string"}}, ["keys"]),
    T("ui_list", "List clickable elements BY NAME in an open app.", {"app": {"type": "string"}}, ["app"]),
    T("ui_click", "Click a named control in a native desktop app via UI Automation.", {"app": {"type": "string"}, "element_name": {"type": "string"}}, ["app", "element_name"]),
    T("close_window", "Close an app's window or active browser tab.", {"what": {"type": "string"}}, ["what"]),
    T("scroll_screen", "Scroll the mouse wheel on the FOCUSED window.", {"direction": {"type": "string", "enum": ["up", "down"]}, "seconds": {"type": "number"}, "speed": {"type": "string", "enum": ["slow", "normal", "fast"]}}, ["direction"]),
    T("look_at_screen", "Screenshot + describe visual content when asked what is displayed on the monitor.", {"question": {"type": "string"}}, ["question"]),
    T("verify_screen", "Screenshot + check expectation when asked.", {"expectation": {"type": "string"}}, ["expectation"]),
    T("run_command", "Run a Windows shell command or launch a Python (.py) script.", {"command": {"type": "string"}}, ["command"]),
    T("set_volume", "Set speaker volume 0-100.", {"level": {"type": "integer"}}, ["level"]),
    T("media_control", "Media keys: play, pause, next, previous, mute.", {"action": {"type": "string", "enum": ["play", "pause", "next", "previous", "mute"]}}, ["action"]),
    T("set_reminder", "Set a spoken reminder after N minutes.", {"minutes": {"type": "number"}, "text": {"type": "string"}}, ["minutes", "text"]),
    T("take_note", "Save a timestamped note (or pass 'read' to read saved notes).", {"text": {"type": "string"}}, ["text"]),
    T("weather", "Current weather via city name.", {"city": {"type": "string"}}, []),
    T("system_status", "RAM, battery, disk space.", {}, []),
    T("run_routine", "Run a saved routine from routines.json.", {"name": {"type": "string"}}, ["name"]),
    T("remember_fact", "Store a lasting fact about the user.", {"fact": {"type": "string"}}, ["fact"]),
    T("read_clipboard", "Read text currently copied in the Windows clipboard.", {}, []),
    T("write_clipboard", "Copy text directly into the Windows clipboard.", {"text": {"type": "string"}}, ["text"]),
    T("fetch_webpage", "Extract full readable text from any webpage URL.", {"url": {"type": "string"}}, ["url"]),
    T("deep_research", "Multi-source internet research on a topic and optionally save a report file.", {"topic": {"type": "string"}, "save_to_file": {"type": "string"}}, ["topic"]),
    T("top_processes", "List top 8 Windows processes by memory or CPU usage.", {"sort_by": {"type": "string", "enum": ["memory", "cpu"]}}, []),
    T("active_window_info", "Get the title of the currently focused foreground window.", {}, []),
    T("system_power", "Lock ('lock'), 'sleep', 'hibernate', 'sign_out', 'shutdown', 'restart', or 'cancel'.", {"action": {"type": "string", "enum": ["lock", "sleep", "hibernate", "sign_out", "shutdown", "restart", "cancel"]}}, ["action"]),
]

_CHAT_TOOL_NAMES = {
    "web_search", "calculator_math", "weather", "remember_fact",
    "look_at_screen", "system_status", "list_files", "find_anywhere", "recent_pc_activity"
}
_CHAT_TOOLS_SUBSET = [t for t in GROQ_TOOLS if t["function"]["name"] in _CHAT_TOOL_NAMES]

_DESKTOP_ACTION_PAT = re.compile(
    r"\b(open|close|launch|start|play|watch|click|pick|choose|select|tap|type|write|create|delete|read|list|"
    r"find|search|go to|navigate|link|result|button|video|thumbnail|first|second|third|fourth|folder|folders|"
    r"directory|directories|file|files|desktop|downloads|documents|pictures|music|videos|explorer|app|website|"
    r"youtube|yt|google|chrome|brave|edge|firefox|opera|vivaldi|neo|browser|history|bookmarks|bookmark|incognito|"
    r"tab|profile|scroll|hotkey|press|key|volume|brightness|wifi|wi-fi|bluetooth|hotspot|airplane|ip|screenshot|"
    r"snipping|recycle|storage|disk|kill|mute|pause|resume|next|previous|remind|reminder|note|clipboard|copy|"
    r"paste|process|cpu|ram|memory|window|lock|sleep|shutdown|restart|routine|run|execute|command|script|code|"
    r"screen|camera|webcam|photo|picture|selfie|record|recording|snap|cheese|"
    r"pannu|pண்ணு|ஓபன்|திற|மூடு|க்ளோஸ்|ப்ளே|போடு|தேடு|சர்ச்|கிளிக்|போட்டோ|வீடியோ|ஃபோல்டர்|ஃபைல்|"
    r"சவுண்ட்|வால்யூம்|ஸ்கிரீன்|கேமரா|யூடியூப்|குரோம்|பிரேவ்)\b",
    re.I
)

_STREAM_TOOL_PAT = _DESKTOP_ACTION_PAT


def _select_tools_for_turn(messages: list) -> list:
    """
    Category-Based Micro-Tool Routing:
    Sends only ~8 relevant tools (~500 tokens) instead of all 45 tools (~3,500 tokens),
    cutting LLM latency by 4x and preventing Groq TPM rate limits!
    """
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "").lower()
    if not _DESKTOP_ACTION_PAT.search(last_user):
        return _CHAT_TOOLS_SUBSET

    wanted = {"look_at_screen", "click_screen", "open_app", "open_website"}
    if any(k in last_user for k in ("camera", "webcam", "photo", "picture", "selfie", "record")):
        wanted.update({"camera_control"})
    if any(k in last_user for k in ("browser", "chrome", "brave", "edge", "firefox", "neo", "tab", "history", "bookmark", "incognito", "youtube", "yt", "video", "search", "link", "url", "page", "scroll", "zoom")):
        wanted.update({"browser_action", "search_in_bar", "open_on_site", "open_website", "browser_search", "scroll_screen", "open_chrome_profile", "close_window"})
    if any(k in last_user for k in ("folder", "directory", "file", "desktop", "downloads", "documents", "pictures", "write", "create", "delete", "read", "list", "find")):
        wanted.update({"open_folder", "list_files", "find_anywhere", "open_anything", "read_file", "write_file", "append_to_file", "create_folder", "delete_folder", "recent_pc_activity"})
    if any(k in last_user for k in ("volume", "brightness", "mute", "pause", "play", "wifi", "wi-fi", "bluetooth", "hotspot", "ip", "screenshot", "task manager", "recycle", "disk", "storage", "battery", "status", "process", "kill", "lock", "sleep", "shutdown", "restart", "window", "minimize", "maximize", "snap", "hotkey", "press", "type", "clipboard", "run", "command", "script")):
        wanted.update({"system_control", "window_control", "set_volume", "set_brightness", "media_control", "system_power", "system_status", "top_processes", "run_command", "press_hotkey", "type_text", "close_window", "read_clipboard", "write_clipboard"})

    selected = [t for t in GROQ_TOOLS if t["function"]["name"] in wanted]
    return selected if selected else GROQ_TOOLS[:15]


def _tok_limit_for_turn(messages: list) -> int:
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    if re.search(r"\b(write|create|code|script|file|detail|detailed|explain|elaborate|research)\b", last_user, re.I):
        return 2000
    return 350


def _reset_turn_state():
    safety.TURN["actions"] = []
    safety.TURN["failures"] = []
    safety.TURN["executed_calls"] = {}
    safety.TURN["last_output"] = ""


def _normalize_assistant_msg(m) -> dict:
    if isinstance(m, dict):
        msg = {"role": "assistant", "content": m.get("content") or ""}
        if m.get("tool_calls"):
            msg["tool_calls"] = m["tool_calls"]
        return msg
    msg = {"role": "assistant", "content": getattr(m, "content", None) or ""}
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            }
            for tc in tcs
        ]
    return msg


def _tool_loop(call_fn, messages: list):
    final = None
    executed = safety.TURN.setdefault("executed_calls", {})
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    allow_multi = bool(re.search(r"\b(and|then|after\s+that|also)\b", last_user, re.I))
    single_shot_done = False

    for _ in range(3):
        if speech._stop_requested():
            speech.STOP["now"] = False
            return "Stopped by your request."
        m = _normalize_assistant_msg(call_fn(messages))
        tcs = m.get("tool_calls")
        if not tcs:
            final = (m.get("content") or "").strip()
            break

        names_in_batch = {tc["function"]["name"] for tc in tcs}
        if "open_on_site" in names_in_batch and "open_website" in names_in_batch:
            tcs = [tc for tc in tcs if tc["function"]["name"] != "open_website"]
        if "list_files" in names_in_batch and "look_at_screen" in names_in_batch:
            tcs = [tc for tc in tcs if tc["function"]["name"] != "look_at_screen"]
        if "search_in_bar" in names_in_batch and "browser_search" in names_in_batch:
            tcs = [tc for tc in tcs if tc["function"]["name"] != "browser_search"]

        filtered_tcs = []
        for tc in tcs:
            fn_name = tc["function"]["name"]
            if fn_name in _SINGLE_SHOT_TOOLS:
                if single_shot_done and not allow_multi:
                    continue
                single_shot_done = True
            filtered_tcs.append(tc)

        if not filtered_tcs:
            break

        m["tool_calls"] = filtered_tcs
        messages.append(m)
        outputs_this_round = []

        for tc in filtered_tcs:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}

            call_key = f"{fn_name}:{json.dumps(args, sort_keys=True)}"
            if call_key in executed:
                out = executed[call_key]
            else:
                isfile = fn_name in ("write_file", "append_to_file")
                print(f"  [tool: {fn_name} {args if not isfile else {'path': args.get('path'), 'content_len': len(args.get('content', ''))}}]")
                out = str(run_tool(fn_name, args))[:1500]
                executed[call_key] = out
                safety.TURN["last_output"] = out

            outputs_this_round.append(out)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})

        if single_shot_done and not allow_multi:
            final = " ".join(outputs_this_round)
            break

    return final or safety.TURN.get("last_output")


def _run_brain_with_nudge(call_fn, msg: str) -> str:
    speech.STOP["now"] = False
    overlay.ui_state("THINKING")
    with memory.MEMORY_LOCK:
        hist_snapshot = list(memory.HISTORY)
    messages = [{"role": "system", "content": system_prompt()}] + hist_snapshot + [{"role": "user", "content": msg}]
    final = _tool_loop(call_fn, messages)

    if not safety.TURN["actions"] and not safety.TURN["failures"] and (
        not (final or "").strip() or (final or "").strip().lower() in ("done", "done.", "ok", "okay")
    ):
        messages.append({"role": "user", "content": "You replied 'Done' without calling any tool. Call the required tool now."})
        final = _tool_loop(call_fn, messages)

    final = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", (final or safety.TURN.get("last_output") or "Done.")).strip()
    memory.remember("user", msg)
    memory.remember("assistant", final)
    if safety.TURN["actions"]:
        final += f" [Done this turn: {'; '.join(safety.TURN['actions'])}]"
    return final


def _groq_call(messages):
    """
    Rotates across fast Groq models with a cooldown ladder (60s on 429, 300s on 503/504)
    so a rate-limited model is skipped in 0.0ms instead of stalling for 30-60 seconds!
    """
    active_tools = _select_tools_for_turn(messages)
    tok_limit = _tok_limit_for_turn(messages)
    pool = ("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b", "openai/gpt-oss-120b")
    now = time.time()
    candidates = [
        m for m in pool
        if (not groqai.AVAILABLE_MODELS or m in groqai.AVAILABLE_MODELS) and now >= _MODEL_COOLDOWNS.get(m, 0.0)
    ]
    if not candidates:
        candidates = [m for m in pool if not groqai.AVAILABLE_MODELS or m in groqai.AVAILABLE_MODELS] or [groqai.BRAIN_MODEL]

    last_err = None
    for mod in candidates:
        try:
            resp = groqai.groq.chat.completions.create(
                model=mod,
                messages=messages,
                tools=active_tools,
                temperature=0.1,
                max_tokens=tok_limit,
            )
            groqai.BRAIN_MODEL = mod
            return resp.choices[0].message
        except Exception as e:
            last_err = e
            err_s = str(e)
            if "429" in err_s or "rate_limit" in err_s.lower():
                _MODEL_COOLDOWNS[mod] = time.time() + 65.0
            elif "503" in err_s or "504" in err_s:
                _MODEL_COOLDOWNS[mod] = time.time() + 300.0
            print(f"  [groq/{mod} busy ({err_s[:42]}) — rotating to next model]")
            continue
    raise last_err


def brain_groq(msg: str) -> str:
    return _run_brain_with_nudge(_groq_call, msg)


def brain_gemini(msg: str):
    if not gemini.client:
        return None
    with memory.MEMORY_LOCK:
        history = [{"role": "model" if t["role"] == "assistant" else "user", "parts": [{"text": t["content"]}]} for t in memory.HISTORY]
    try:
        chat = gemini.client.chats.create(
            model=gemini.GEMINI_BRAIN_MODEL,
            config=gemini_types.GenerateContentConfig(
                system_instruction=system_prompt(),
                tools=[_wrap(f) for f in TOOLS.values() if getattr(f, "__name__", "") != "get_secret"],
            ),
            history=history,
        )
        answer = (chat.send_message(msg).text or safety.TURN.get("last_output") or "Done.").strip()
        answer = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", answer).strip()
        memory.remember("user", msg)
        memory.remember("assistant", answer)
        if safety.TURN["actions"]:
            answer += f" [Done this turn: {'; '.join(safety.TURN['actions'])}]"
        return answer
    except Exception as e:
        print(f"  [gemini brain failed: {str(e)[:60]}]")
        return None


def brain_zai(msg: str):
    if not zai.ZAI_API_KEY or not zai.ZAI_BRAIN_MODELS:
        return None
    base = zai._zai_base()
    for zmodel in list(zai.ZAI_BRAIN_MODELS):
        def _call(messages, model_id=zmodel, disable_thinking=True):
            payload = {
                "model": model_id,
                "messages": messages,
                "tools": _select_tools_for_turn(messages),
                "temperature": 0.1,
                "max_tokens": _tok_limit_for_turn(messages),
            }
            if disable_thinking:
                payload["thinking"] = {"type": "disabled"}
            r = HTTP.post(f"{base}/chat/completions", headers=zai._zai_headers(), json=payload, timeout=15)
            if r.status_code == 400 and disable_thinking:
                return _call(messages, model_id, disable_thinking=False)
            if r.status_code != 200:
                raise RuntimeError(zai._zai_err(r))
            return r.json()["choices"][0]["message"]
        try:
            out = _run_brain_with_nudge(_call, msg)
            zai.ZAI_BRAIN_MODEL = zmodel
            return out
        except Exception as e:
            code = str(e)
            if any(m in code for m in ("HTTP 400", "HTTP 404", "[1211]")):
                if zmodel in zai.ZAI_BRAIN_MODELS:
                    zai.ZAI_BRAIN_MODELS.remove(zmodel)
                continue
            return None
    return None


def brain_cohere(msg: str):
    if not cohereai.COHERE_API_KEY or not cohereai.COHERE_BRAIN_MODELS:
        return None
    for cm in list(cohereai.COHERE_BRAIN_MODELS):
        def _call(messages, model_id=cm):
            r = HTTP.post(
                f"{cohereai.COHERE_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {cohereai.COHERE_API_KEY}"},
                json={"model": model_id, "messages": messages, "tools": _select_tools_for_turn(messages), "temperature": 0.1, "max_tokens": _tok_limit_for_turn(messages)},
                timeout=18,
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json()["choices"][0]["message"]
        try:
            return _run_brain_with_nudge(_call, msg)
        except Exception as e:
            code = str(e)
            if "HTTP 401" in code:
                return None
            if "HTTP 404" in code or "HTTP 400" in code:
                if cm in cohereai.COHERE_BRAIN_MODELS:
                    cohereai.COHERE_BRAIN_MODELS.remove(cm)
                continue
            return None
    return None


def brain_or(msg: str):
    if not openrouter.OPENROUTER_API_KEY or not openrouter.OR_BRAIN_MODELS:
        return None
    for orm in list(openrouter.OR_BRAIN_MODELS):
        def _call(messages, model_id=orm):
            r = HTTP.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {openrouter.OPENROUTER_API_KEY}"},
                json={"model": model_id, "messages": messages, "tools": _select_tools_for_turn(messages), "temperature": 0.1, "max_tokens": _tok_limit_for_turn(messages)},
                timeout=18,
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json()["choices"][0]["message"]
        try:
            return _run_brain_with_nudge(_call, msg)
        except Exception:
            continue
    return None


def brain_sambanova(msg: str):
    if not sambanova.SAMBANOVA_API_KEY:
        return None
    def _call(messages):
        r = HTTP.post(
            "https://api.sambanova.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {sambanova.SAMBANOVA_API_KEY}"},
            json={"model": "gpt-oss-120b", "messages": messages, "tools": _select_tools_for_turn(messages), "temperature": 0.1, "max_tokens": _tok_limit_for_turn(messages)},
            timeout=18,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()["choices"][0]["message"]
    try:
        return _run_brain_with_nudge(_call, msg)
    except Exception:
        return None


def _generic_openai_brain(msg, base, key, models, tag, timeout=18):
    if not key or not models:
        return None
    for m in list(models):
        def _call(messages, model_id=m):
            r = HTTP.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model_id, "messages": messages, "tools": _select_tools_for_turn(messages), "temperature": 0.1, "max_tokens": _tok_limit_for_turn(messages)},
                timeout=timeout,
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json()["choices"][0]["message"]
        try:
            return _run_brain_with_nudge(_call, msg)
        except Exception as e:
            code = str(e)
            if "HTTP 401" in code:
                return None
            if "HTTP 404" in code or "HTTP 400" in code:
                if m in models:
                    models.remove(m)
                continue
            return None
    return None


def brain_github(msg: str):
    return _generic_openai_brain(msg, github_models.GITHUB_BASE, github_models.GITHUB_TOKEN, github_models.GITHUB_BRAIN_MODELS, "github")


def brain_mistral(msg: str):
    return _generic_openai_brain(msg, mistralai.MISTRAL_BASE, mistralai.MISTRAL_API_KEY, mistralai.MISTRAL_BRAIN_MODELS, "mistral")


def brain_cerebras(msg: str):
    return _generic_openai_brain(msg, cerebras.CEREBRAS_BASE, cerebras.CEREBRAS_API_KEY, cerebras.CEREBRAS_BRAIN_MODELS, "cerebras")


def ask_tony(msg: str) -> str:
    t0 = time.time()
    _reset_turn_state()
    chain = ((brain_zai, brain_groq) if config.ZAI_AS_PRIMARY else (brain_groq, brain_zai)) + (
        brain_cerebras, brain_github, brain_mistral, brain_gemini, brain_cohere, brain_or, brain_sambanova
    )
    tags = {brain_groq: f"groq/{groqai.BRAIN_MODEL}", brain_zai: f"zai/{zai.ZAI_BRAIN_MODEL}"}
    for fn in chain:
        try:
            ans = fn(msg)
            if ans:
                print(f"  [brain: {tags.get(fn, fn.__name__.replace('brain_', ''))}, {time.time()-t0:.1f}s]")
                return ans
        except Exception as e:
            print(f"  [{fn.__name__.replace('brain_', '')} failed: {str(e)[:60]} — falling back]")
        if safety.TURN.get("actions") and safety.TURN.get("last_output"):
            clean_out = safety.TURN["last_output"]
            memory.remember("user", msg)
            memory.remember("assistant", clean_out)
            return f"{clean_out} [Done this turn: {'; '.join(safety.TURN['actions'])}]"
    return "All of my brains are exhausted right now. Try again in a bit."


def can_stream_conversation(msg: str) -> bool:
    return bool(msg and not _STREAM_TOOL_PAT.search(msg))


def ask_tony_stream(msg: str, on_sentence, expected_generation=None):
    if not can_stream_conversation(msg):
        return None
    _reset_turn_state()
    if expected_generation is None:
        expected_generation = speech.current_generation()
    overlay.ui_state("THINKING")
    with memory.MEMORY_LOCK:
        hist_snapshot = list(memory.HISTORY)
    messages = [{"role": "system", "content": system_prompt()}] + hist_snapshot + [{"role": "user", "content": msg}]

    stream = None
    full_text = []
    pending = ""
    try:
        chat_mod = "llama-3.1-8b-instant" if "llama-3.1-8b-instant" in groqai.AVAILABLE_MODELS else groqai.CHAT_MODEL
        stream = groqai.groq.chat.completions.create(
            model=chat_mod,
            messages=messages,
            temperature=0.45,
            max_tokens=_tok_limit_for_turn(messages),
            stream=True,
        )
        for event in stream:
            if speech._stop_requested(expected_generation):
                break
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            piece = getattr(delta, "content", None) if delta is not None else None
            if not piece:
                continue
            full_text.append(piece)
            pending += piece

            parts = memory._split_sentences_safe(pending)
            if len(parts) > 1:
                completed = parts[:-1]
                pending = parts[-1]
                for sentence in completed:
                    sentence = sentence.strip()
                    if sentence:
                        on_sentence(sentence)

        answer = "".join(full_text).strip()
        if pending.strip() and not speech._stop_requested(expected_generation):
            on_sentence(pending.strip())
        if not answer:
            return None
        if not speech._stop_requested(expected_generation):
            memory.remember("user", msg)
            memory.remember("assistant", answer)
        return answer
    except Exception as exc:
        if not full_text:
            print(f"  [streaming chat unavailable: {str(exc)[:80]}]")
            return None
        if pending.strip() and not speech._stop_requested(expected_generation):
            on_sentence(pending.strip())
        answer = "".join(full_text).strip()
        if answer and not speech._stop_requested(expected_generation):
            memory.remember("user", msg)
            memory.remember("assistant", answer)
        return answer or None
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _strip_conversational_prefix(msg: str) -> str:
    s = (msg or "").strip()
    for _ in range(5):
        s_new = re.sub(
            r"^\s*(?:okay|ok|hey|well|so|alright|umm+|uh+|oh+|ah+|hmm+|then|no|i\s+mean|now|please|jarvis|tony|can\s+you|could\s+you|just)[,\s]+",
            "", s, flags=re.I
        ).strip()
        if s_new == s:
            break
        s = s_new
    return s


def fast_path(msg: str, _allow_split: bool = True):
    _r = _mic_command(msg)
    if _r is not None:
        return _r
    if _allow_split:
        _reset_turn_state()

    clean_msg = _strip_conversational_prefix(msg)
    low_c = clean_msg.lower().rstrip(".!?").strip()

    # Instant Multi-Step Fast-Path ("close the chrome and open task manager", "close brave and open youtube in brave")
    if _allow_split and re.search(r"\s+(?:and\s+then|and\s+again|and|then)\s+", clean_msg, re.I):
        parts = [p.strip() for p in re.split(r"\s+(?:and\s+then|and\s+again|and|then)\s+", clean_msg, flags=re.I) if p.strip()]
        if 2 <= len(parts) <= 3:
            results = []
            all_matched = True
            for part in parts:
                sub_res = fast_path(part, _allow_split=False)
                if sub_res is None:
                    all_matched = False
                    break
                results.append(sub_res)
                time.sleep(0.25)
            if all_matched:
                combined = " ".join(results)
                memory.remember("user", msg)
                memory.remember("assistant", combined)
                return memory.core_reply(combined)
            return None

    b_target = ""
    m_in_b = re.match(r"^(.+?)\s+in\s+(chrome|brave|edge|firefox|opera|vivaldi|neo)(?:\s+browser)?$", low_c)
    cmd_core = m_in_b.group(1).strip() if m_in_b else low_c
    if m_in_b:
        b_target = m_in_b.group(2).strip()

    # 0-CAM) Webcam Controls
    m_cam_timer = re.match(
        r"^(?:take|click|capture|snap)\s+(?:a\s+)?(?:photo|picture|selfie|pic|shot)(?:\s+in\s+(\d+)\s*seconds?)?$",
        low_c
    )
    if m_cam_timer:
        t_sec = int(m_cam_timer.group(1)) if m_cam_timer.group(1) else 0
        out = run_tool("camera_control", {"action": "take_photo", "timer_seconds": t_sec})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    _INSTANT_CAMERA = {
        "open camera": "open", "open webcam": "open", "start camera": "open", "start webcam": "open",
        "close camera": "close", "close webcam": "close",
        "take photo": "take_photo", "take picture": "take_photo", "take selfie": "take_photo",
        "click photo": "take_photo", "click picture": "take_photo", "capture photo": "take_photo", "cheese": "take_photo",
        "start recording": "start_video", "start video": "start_video", "record video": "start_video",
        "record a video": "start_video", "take a video": "start_video", "take video": "start_video",
        "start recording video": "start_video",
        "stop recording": "stop_video", "stop video": "stop_video", "stop taking video": "stop_video",
        "stop recording video": "stop_video", "finish recording": "stop_video",
        "pause recording": "pause_video", "resume recording": "resume_video",
        "switch to video mode": "switch_video_mode", "video mode": "switch_video_mode",
        "switch to photo mode": "switch_photo_mode", "photo mode": "switch_photo_mode",
        "switch camera": "switch_camera", "change camera": "switch_camera", "flip camera": "switch_camera",
        "open camera roll": "open_last", "show last photo": "open_last", "open last photo": "open_last",
        "show the photo i just took": "open_last", "show the video i just recorded": "open_last",
    }
    if low_c in _INSTANT_CAMERA:
        out = run_tool("camera_control", {"action": _INSTANT_CAMERA[low_c]})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    # 0A) Universal Browser & Web-Video Shortcuts
    _INSTANT_BROWSER = {
        "new tab": ("new_tab", ""), "open a new tab": ("new_tab", ""), "open new tab": ("new_tab", ""),
        "close tab": ("close_tab", ""), "close this tab": ("close_tab", ""),
        "reopen tab": ("reopen_tab", ""), "reopen closed tab": ("reopen_tab", ""), "restore tab": ("reopen_tab", ""),
        "duplicate tab": ("duplicate_tab", ""), "duplicate this tab": ("duplicate_tab", ""),
        "next tab": ("next_tab", ""), "switch to next tab": ("next_tab", ""),
        "previous tab": ("prev_tab", ""), "switch to previous tab": ("prev_tab", ""),
        "first tab": ("first_tab", ""), "go to first tab": ("first_tab", ""),
        "last tab": ("last_tab", ""), "go to last tab": ("last_tab", ""),
        "new window": ("new_window", ""), "open new window": ("new_window", ""), "open a new window": ("new_window", ""),
        "incognito": ("incognito", ""), "incognito mode": ("incognito", ""), "open incognito": ("incognito", ""),
        "open incognito window": ("incognito", ""), "open a new incognito window": ("incognito", ""),
        "open private window": ("incognito", ""), "private window": ("incognito", ""),
        "go back": ("back", ""), "navigate back": ("back", ""),
        "go forward": ("forward", ""), "navigate forward": ("forward", ""),
        "refresh": ("refresh", ""), "refresh page": ("refresh", ""), "reload": ("refresh", ""), "reload page": ("refresh", ""),
        "hard refresh": ("hard_refresh", ""), "stop loading": ("stop_loading", ""),
        "bookmark": ("bookmark", ""), "bookmark this page": ("bookmark", ""), "bookmark page": ("bookmark", ""),
        "toggle bookmarks bar": ("bookmarks_bar", ""), "show bookmarks": ("bookmarks_manager", ""),
        "open bookmarks": ("bookmarks_manager", ""), "go to bookmarks": ("bookmarks_manager", ""), "bookmarks": ("bookmarks_manager", ""),
        "history": ("history", ""), "my history": ("history", ""), "the history": ("history", ""),
        "history of me": ("history", ""), "the history of me": ("history", ""),
        "open history": ("history", ""), "go to history": ("history", ""), "go to my history": ("history", ""),
        "go to the history": ("history", ""), "browser history": ("history", ""), "show history": ("history", ""), "show my history": ("history", ""),
        "open browser downloads": ("downloads", ""), "go to browser downloads": ("downloads", ""), "show downloads": ("downloads", ""),
        "clear browsing data": ("clear_browsing_data", ""),
        "open extensions": ("extensions", ""), "go to extensions": ("extensions", ""), "manage extensions": ("extensions", ""),
        "open devtools": ("devtools", ""), "inspect element": ("devtools", ""),
        "view page source": ("view_source", ""), "print page": ("print_page", ""), "save page": ("save_page", ""),
        "copy url": ("copy_url", ""), "copy page url": ("copy_url", ""), "copy link": ("copy_url", ""),
        "full screen": ("fullscreen", ""), "fullscreen": ("fullscreen", ""), "exit full screen": ("fullscreen", ""),
        "theater mode": ("theater", ""), "miniplayer": ("miniplayer", ""), "mini player": ("miniplayer", ""),
        "captions": ("captions", ""), "toggle captions": ("captions", ""), "subtitles": ("captions", ""), "toggle subtitles": ("captions", ""),
        "mute tab": ("mute_tab", ""), "unmute tab": ("mute_tab", ""),
        "skip forward": ("skip_forward", ""), "fast forward": ("skip_forward", ""),
        "skip backward": ("skip_backward", ""), "rewind": ("skip_backward", ""),
        "speed up": ("speed_up", ""), "speed up video": ("speed_up", ""), "play faster": ("speed_up", ""),
        "slow down": ("slow_down", ""), "slow down video": ("slow_down", ""), "play slower": ("slow_down", ""),
        "next video": ("next_video", ""), "previous video": ("prev_video", ""),
        "skip ad": ("skip_ad", ""), "skip the ad": ("skip_ad", ""),
        "zoom in": ("zoom_in", ""), "zoom out": ("zoom_out", ""), "reset zoom": ("zoom_reset", ""),
        "scroll to top": ("scroll_top", ""), "go to top": ("scroll_top", ""), "top of page": ("scroll_top", ""),
        "scroll to bottom": ("scroll_bottom", ""), "go to bottom": ("scroll_bottom", ""), "bottom of page": ("scroll_bottom", ""),
    }
    if cmd_core in _INSTANT_BROWSER:
        act, val = _INSTANT_BROWSER[cmd_core]
        out = run_tool("browser_action", {"action": act, "value": val, "browser": b_target})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^(?:switch\s+to\s+|go\s+to\s+)?tab\s+(\d)\s*$", cmd_core)
    if m:
        out = run_tool("browser_action", {"action": "tab_number", "value": m.group(1), "browser": b_target})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^find\s+(.{1,40}?)\s+(?:on|in)\s+(?:this\s+|the\s+)?page\s*$", clean_msg, re.I)
    if m:
        out = run_tool("browser_action", {"action": "find_on_page", "value": m.group(1).strip()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^(?:open|go\s+to|launch)\s+(?:the\s+|my\s+)?([\w.\-]+)\s+in\s+(chrome|brave|edge|firefox|opera|vivaldi|neo)(?:\s+browser)?\s*$", low_c)
    if m:
        site_key, b_name = m.group(1).strip(), m.group(2).strip()
        target_url = ui.resolve_site_url(site_key)
        out = run_tool("open_website", {"url": target_url, "browser": b_name})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    # 0B) Window & Keyboard Shortcuts
    _INSTANT_WINDOW = {
        "show desktop": ("show_desktop", ""), "minimize all": ("show_desktop", ""),
        "minimize": ("minimize", ""), "minimize window": ("minimize", ""), "minimize this window": ("minimize", ""),
        "maximize": ("maximize", ""), "maximize window": ("maximize", ""), "maximize this window": ("maximize", ""),
        "restore window": ("restore", ""), "switch window": ("switch", ""),
        "snap left": ("snap_left", ""), "snap right": ("snap_right", ""), "task view": ("task_view", ""),
    }
    if low_c in _INSTANT_WINDOW:
        act, app_t = _INSTANT_WINDOW[low_c]
        out = run_tool("window_control", {"action": act, "app": app_t})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^(minimize|maximize|restore|switch\s+to)\s+(?:the\s+)?([\w .\-]{2,25})$", low_c)
    if m:
        act = "switch" if m.group(1).startswith("switch") else m.group(1)
        out = run_tool("window_control", {"action": act, "app": m.group(2).strip()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    _INSTANT_KEYS = {
        "press enter": "enter", "hit enter": "enter", "press escape": "esc", "press esc": "esc",
        "press space": "space", "press backspace": "backspace", "select all": "ctrl+a",
        "copy": "ctrl+c", "copy that": "ctrl+c", "paste": "ctrl+v", "paste that": "ctrl+v",
        "cut": "ctrl+x", "undo": "ctrl+z", "undo that": "ctrl+z", "redo": "ctrl+y",
        "save": "ctrl+s", "save file": "ctrl+s", "page down": "pgdn", "page up": "pgup",
    }
    if low_c in _INSTANT_KEYS:
        out = run_tool("press_hotkey", {"keys": _INSTANT_KEYS[low_c]})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    # 0C) Search in search bar
    m = re.match(
        r"^search(?:\s+for)?\s+(.{1,60}?)\s+(?:in|on|inside)\s+(?:the\s+)?(youtube\s+)?search\s*(?:bar|box)(?:\s+in\s+(chrome|brave|edge|firefox|opera|vivaldi|neo))?\s*[.!]?$",
        clean_msg, re.I
    )
    if m:
        out = run_tool("search_in_bar", {"query": m.group(1).strip(), "site": "youtube" if m.group(2) else "", "browser": (m.group(3) or "").strip().lower()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^search(?:\s+for)?\s+(.{1,60}?)\s+(?:in|on)\s+youtube(?:\s+in\s+(chrome|brave|edge|firefox|opera|vivaldi|neo))?\s*[.!]?$", clean_msg, re.I)
    if m:
        out = run_tool("search_in_bar", {"query": m.group(1).strip(), "site": "youtube", "browser": (m.group(2) or "").strip().lower()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    # 0D) Instant folder/file count
    m = re.match(
        r"^(?:how\s+many|count(?:\s+the)?|what(?:\s+are\s+the)?|list(?:\s+the)?)\s*(folders?|directories|files?|items?)\s+(?:are\s+)?(?:there\s+)?(?:in|on|inside)\s+(?:the\s+|my\s+)?([\w\-/ ]{2,30}?)(?:\s+folder)?\s*[.?]?$",
        clean_msg, re.I
    )
    if m:
        what_kind, target_dir = m.group(1).lower(), m.group(2).strip()
        raw_info = run_tool("list_files", {"folder": target_dir})
        mf = re.search(
            r"In\s+(.+?):\s*(\d+)\s+folder\(s\)\s+and\s+(\d+)\s+file\(s\)\.\s*Folders\s*\(\d+\):\s*([^.]+)\.\s*Files\s*\(\d+\):\s*([^.]+)\.",
            raw_info
        )
        if mf:
            loc_name = os.path.basename(mf.group(1).rstrip("\\/")) or target_dir
            n_dirs, n_files = mf.group(2), mf.group(3)
            dir_list, file_list = mf.group(4).strip(), mf.group(5).strip()
            if "folder" in what_kind or "director" in what_kind:
                ans = f"There are {n_dirs} folders in {loc_name}: {dir_list}."
            elif "file" in what_kind:
                ans = f"There are {n_files} files in {loc_name}: {file_list}."
            else:
                ans = f"In {loc_name}, there are {n_dirs} folders and {n_files} files."
            memory.remember("user", msg)
            memory.remember("assistant", ans)
            return memory.core_reply(ans, detail=("what" in clean_msg.lower() or "list" in clean_msg.lower()))
        return memory.core_reply(raw_info)

    # 1) Open folder
    m = re.match(r"^(?:open|go\s+to)\s+(?:the\s+|my\s+)?([\w\-/ ]{1,30}?)\s+folder(?:\s+on\s+(?:my\s+)?desktop)?\s*[.!]?$", clean_msg, re.I)
    if m:
        name = m.group(1).strip().replace("/", "\\").replace("desktop\\", "").replace("Desktop\\", "")
        out = run_tool("open_folder", {"folder": f"Desktop\\{name}" if "\\" in name else name})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^(?:open|go\s+to)\s+(?:the\s+|my\s+)?(desktop|downloads|documents|pictures|music|videos|tony)(?:\s+folder)?\s*[.!]?$", clean_msg, re.I)
    if m:
        out = run_tool("open_folder", {"folder": m.group(1).lower()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    # 2) Direct screen click
    m = re.match(
        r"^(double\s+click|right\s+click|click|pick|choose|select|tap|play|open)(?:\s+on)?\s+(?:the\s+)?(.{2,60}?)(?:\s+(?:in|on)\s+(?:the\s+)?screen)?\s*[.!]?$",
        clean_msg, re.I
    )
    if m:
        verb, elem = m.group(1).lower(), m.group(2).strip()
        if verb in ("play", "open") and not re.search(r"\b(screen|link|result|first|second|third|fourth|video)\b", clean_msg, re.I):
            pass
        elif not re.search(r"\b(?:and|then|after)\b", elem, re.I):
            ctype = "double" if "double" in verb else ("right" if "right" in verb else "single")
            out = run_tool("click_screen", {"element": elem, "click_type": ctype})
            memory.remember("user", msg)
            memory.remember("assistant", out)
            return memory.core_reply(out)

    # 3) Direct YouTube play
    m = re.match(r"^(?:(?:open|go\s+to)\s+youtube\s+and\s+)?(?:play|watch)\s+(.{2,70}?)(?:\s+on\s+youtube)?\s*[.!]?$", clean_msg, re.I)
    if m and ("youtube" in clean_msg.lower()):
        out = run_tool("open_on_site", {"site": "youtube", "what": m.group(1).strip()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    # 4) Open / Go To app, website, or folder
    m = re.match(r"^(?:open|go\s+to|launch)\s+(?:the\s+|my\s+)?([\w .\-]{2,25})\s*[.!]?$", clean_msg, re.I)
    if m and m.group(1).strip().lower() not in ("the", "my", "folder", "new tab", "a new tab"):
        target = m.group(1).strip()
        t_low = target.lower()
        if "video" not in t_low and "link" not in t_low:
            if t_low in _KNOWN_FOLDER_NAMES:
                out = run_tool("open_folder", {"folder": t_low})
                memory.remember("user", msg)
                memory.remember("assistant", out)
                return memory.core_reply(out)
            if t_low in ui._EXTRA_SITE_ALIASES or (t_low in config.WEBSITE_ALIASES and t_low not in config.APPS):
                out = run_tool("open_website", {"url": ui.resolve_site_url(t_low)})
                memory.remember("user", msg)
                memory.remember("assistant", out)
                return memory.core_reply(out)
            if t_low in config.APPS or t_low in ui._APP_ALIASES or t_low in pc_index.INSTALLED_APPS or t_low in ui._BROWSERS:
                out = run_tool("open_app", {"app": target})
                memory.remember("user", msg)
                memory.remember("assistant", out)
                return memory.core_reply(out)
            desk_hit = files._find_subfolder_fuzzy(files.FOLDERS["desktop"], target)
            if desk_hit:
                out = run_tool("open_folder", {"folder": desk_hit})
                memory.remember("user", msg)
                memory.remember("assistant", out)
                return memory.core_reply(out)
            f_hits = pc_index.search_pc(target, kind="folder", limit=1)
            if f_hits and f_hits[0]["name"].lower() == t_low:
                out = run_tool("open_folder", {"folder": f_hits[0]["path"]})
                memory.remember("user", msg)
                memory.remember("assistant", out)
                return memory.core_reply(out)
            out = run_tool("open_app", {"app": target})
            memory.remember("user", msg)
            memory.remember("assistant", out)
            return memory.core_reply(out)

    # 5) Close window / tab ("close chrome", "close to youtube" -> closes active tab)
    m = re.match(r"^(?:can\s+i\s+)?close(?:\s+to)?\s+(?:the\s+|my\s+|this\s+)?([\w .\-]{2,25})\s*[.!]?$", clean_msg, re.I)
    if m and m.group(1).strip().lower() not in ("the", "my"):
        out = run_tool("close_window", {"what": m.group(1).strip()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    # 6) Complete Windows OS, Audio, Display, Network & Power Fast-Paths
    _INSTANT_SYS = {
        "volume up": ("volume_up", ""), "increase volume": ("volume_up", ""), "turn up volume": ("volume_up", ""), "louder": ("volume_up", ""),
        "volume down": ("volume_down", ""), "decrease volume": ("volume_down", ""), "turn down volume": ("volume_down", ""), "quieter": ("volume_down", ""),
        "mute mic": ("mute_mic", ""), "unmute mic": ("mute_mic", ""), "mute microphone": ("mute_mic", ""), "unmute microphone": ("mute_mic", ""),
        "open volume mixer": ("volume_mixer", ""), "volume mixer": ("volume_mixer", ""),
        "brightness up": ("brightness_up", ""), "increase brightness": ("brightness_up", ""), "brighter": ("brightness_up", ""),
        "brightness down": ("brightness_down", ""), "decrease brightness": ("brightness_down", ""), "dimmer": ("brightness_down", ""),
        "take a screenshot": ("screenshot", ""), "take screenshot": ("screenshot", ""), "screenshot": ("screenshot", ""), "capture screen": ("screenshot", ""),
        "show last screenshot": ("open_last_screenshot", ""), "open last screenshot": ("open_last_screenshot", ""),
        "snipping tool": ("snipping_tool", ""), "night light": ("night_light", ""), "project screen": ("project_screen", ""),
        "wifi status": ("wifi_status", ""), "wi-fi status": ("wifi_status", ""), "check wifi": ("wifi_status", ""),
        "list wifi": ("wifi_list", ""), "available wifi": ("wifi_list", ""), "scan wifi": ("wifi_list", ""),
        "disconnect wifi": ("wifi_disconnect", ""), "open wifi settings": ("wifi_settings", ""), "wifi settings": ("wifi_settings", ""),
        "open bluetooth": ("bluetooth_settings", ""), "open bluetooth settings": ("bluetooth_settings", ""), "bluetooth settings": ("bluetooth_settings", ""),
        "open hotspot": ("hotspot_settings", ""), "hotspot settings": ("hotspot_settings", ""), "airplane mode": ("airplane_mode", ""),
        "what is my ip": ("ip_address", ""), "what's my ip": ("ip_address", ""), "ip address": ("ip_address", ""),
        "open start menu": ("start_menu", ""), "start menu": ("start_menu", ""),
        "open quick settings": ("quick_settings", ""), "quick settings": ("quick_settings", ""), "open action center": ("quick_settings", ""),
        "open notifications": ("notifications", ""), "show notifications": ("notifications", ""), "notifications": ("notifications", ""),
        "clipboard history": ("clipboard_history", ""), "open clipboard history": ("clipboard_history", ""),
        "emoji panel": ("emoji_panel", ""), "open task manager": ("task_manager", ""), "task manager": ("task_manager", ""),
        "new virtual desktop": ("new_desktop", ""), "create virtual desktop": ("new_desktop", ""),
        "close virtual desktop": ("close_desktop", ""), "next virtual desktop": ("next_desktop", ""), "previous virtual desktop": ("prev_desktop", ""),
        "disk space": ("disk_space", ""), "check disk space": ("disk_space", ""), "storage status": ("disk_space", ""), "how much storage do i have": ("disk_space", ""),
        "battery status": ("battery_status", ""), "battery percentage": ("battery_status", ""), "how much battery do i have": ("battery_status", ""), "system status": ("battery_status", ""),
        "empty recycle bin": ("empty_recycle_bin", ""), "clear recycle bin": ("empty_recycle_bin", ""),
    }
    if low_c in _INSTANT_SYS:
        act, val = _INSTANT_SYS[low_c]
        out = run_tool("system_control", {"action": act, "value": val})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^connect\s+to\s+(?:wifi|wi-fi)\s+(.+)$", clean_msg, re.I)
    if m:
        out = run_tool("system_control", {"action": "wifi_connect", "value": m.group(1).strip()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^(?:kill|force\s+close|terminate)\s+(?:process\s+)?([\w .\-]{2,25})$", low_c)
    if m:
        out = run_tool("system_control", {"action": "kill_process", "value": m.group(1).strip()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    m = re.match(r"^(?:open|go\s+to)\s+(wifi|wi-fi|bluetooth|sound|audio|display|night\s+light|battery|power|storage|startup|windows\s+update|update|hotspot|personalization|privacy|gaming)\s+settings$", low_c)
    if m:
        out = run_tool("system_control", {"action": "settings", "value": m.group(1).strip()})
        memory.remember("user", msg)
        memory.remember("assistant", out)
        return memory.core_reply(out)

    if low_c in ("lock", "lock pc", "lock computer", "lock screen", "lock workstation"):
        return memory.core_reply(run_tool("system_power", {"action": "lock"}))
    if low_c in ("cancel shutdown", "abort shutdown", "stop shutdown"):
        return memory.core_reply(run_tool("system_power", {"action": "cancel"}))

    m = re.match(r"^(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\s*%?\s*[.!]?$", clean_msg, re.I)
    if m:
        return memory.core_reply(run_tool("set_volume", {"level": int(m.group(1))}))
    m = re.match(r"^(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})\s*%?\s*[.!]?$", clean_msg, re.I)
    if m:
        return memory.core_reply(run_tool("set_brightness", {"level": int(m.group(1))}))
    if re.match(r"^(?:mute|unmute)\s*[.!]?$", clean_msg, re.I):
        return memory.core_reply(run_tool("media_control", {"action": "mute"}))
    if re.match(r"^(?:pause|pause\s+(?:the\s+)?video|pause\s+music|resume|resume\s+(?:the\s+)?video|play\s+(?:the\s+)?video)\s*[.!]?$", clean_msg, re.I):
        act = "pause" if "pause" in clean_msg.lower() else "play"
        if ui._chrome_running() and ("youtube" in ui._get_active_title().lower() or "youtube" in ui.STATE.get("last_site", "").lower()):
            run_tool("browser_action", {"action": "play_pause"})
            return "Paused the YouTube video." if act == "pause" else "Resumed the YouTube video."
        return memory.core_reply(run_tool("media_control", {"action": act}))

    # 7) Scroll & Routine fast-paths
    m = re.match(r"^scroll\s+(up|down)(?:\s+(slow|fast))?\s*[.!]?$", clean_msg, re.I)
    if m:
        return memory.core_reply(run_tool("scroll_screen", {"direction": m.group(1).lower(), "speed": (m.group(2) or "normal").lower()}))
    m = re.match(r"^(?:run|start|do)\s+(?:the\s+)?([\w\- ]{1,20}?)\s+routine\s*[.!]?$", clean_msg, re.I)
    if m:
        return memory.core_reply(run_tool("run_routine", {"name": m.group(1).strip()}))
    return None